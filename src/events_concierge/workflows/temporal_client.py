"""Shared secure Temporal client construction for every worker entrypoint."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from temporalio.client import (
    Client,
    Interceptor,
    OutboundInterceptor,
    StartWorkflowInput,
    TLSConfig,
    WorkflowHandle,
)

from ..adapters.postgres.tenant_effects import PostgresTenantEffectAuthority
from ..config import Settings
from ..ports.object_store import ObjectStorePort
from ..ports.tenant_effects import TenantEffectAuthority, TenantEffectAuthorityConfig
from ..secret_files import read_secret_file
from .catalog_claim_check import build_catalog_claim_check_data_converter
from .claim_check import build_claim_check_data_converter

_LOCAL_ENVIRONMENTS = frozenset({"", "dev", "development", "local", "test", "testing"})
_COMMIT_BUILD_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SEMANTIC_BUILD_ID = re.compile(
    r"^v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_CATALOG_WORKFLOW_NAMES = frozenset(
    {
        "CatalogPagedRefreshWorkflow",
        "CatalogRefreshWorkflow",
    }
)
_TRANSACTIONAL_WORKFLOW_NAMES = frozenset(
    {
        "EventRequestWorkflow",
        "RegistrationWorkflow",
    }
)
_SPLIT_QUEUE_MIN_WORKFLOW_TASKS = 4
_SPLIT_QUEUE_MIN_ACTIVITIES = 2
_TLS_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z"
)


class TemporalTaskQueueRoutingInterceptor(Interceptor):
    """Route only this service's known workflow types to their isolated workload queues.

    Existing start adapters retain the legacy queue argument for local compatibility. Temporal
    resolves decorated workflow callables to their stable definition names before invoking client
    interceptors, which lets this boundary correct those starts without changing workflow IDs,
    reuse policies, deadlines, or handle-based signal delivery. Unknown workflow types retain the
    caller's queue instead of being silently claimed by this service.
    """

    def __init__(self, *, transactional_queue: str, catalog_queue: str) -> None:
        self._transactional_queue = transactional_queue
        self._catalog_queue = catalog_queue

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return _TemporalTaskQueueRoutingOutboundInterceptor(next, router=self)

    def task_queue_for_workflow(self, workflow_name: str, requested_queue: str) -> str:
        """Return the configured queue for a known definition, preserving unknown callers."""
        if workflow_name in _CATALOG_WORKFLOW_NAMES:
            return self._catalog_queue
        if workflow_name in _TRANSACTIONAL_WORKFLOW_NAMES:
            return self._transactional_queue
        return requested_queue


class _TemporalTaskQueueRoutingOutboundInterceptor(OutboundInterceptor):
    def __init__(
        self,
        next: OutboundInterceptor,
        *,
        router: TemporalTaskQueueRoutingInterceptor,
    ) -> None:
        super().__init__(next)
        self._router = router

    async def start_workflow(
        self,
        input: StartWorkflowInput,
    ) -> WorkflowHandle[Any, Any]:
        task_queue = self._router.task_queue_for_workflow(input.workflow, input.task_queue)
        if task_queue != input.task_queue:
            input = replace(input, task_queue=task_queue)
        return await self.next.start_workflow(input)


async def connect_temporal(
    settings: Settings,
    object_store: ObjectStorePort,
    *,
    lazy: bool = False,
    catalog_only: bool = False,
    tenant_effect_authority: TenantEffectAuthority | None = None,
) -> Client:
    """Connect with one namespace, converter and verified API-key or mTLS transport.

    API composition uses a lazy client so a cold-start engine outage can retain database-backed
    intake and reconnect through the same client later. Workers use the eager default so an
    unavailable task queue makes the process exit and restart under its supervisor.
    The explicit catalog profile installs a separate driver against its own storage prefix;
    it never constructs tenant effects or accepts tenant claim references.
    """
    validate_temporal_settings(settings)
    api_key = _temporal_api_key(settings)
    tls = _temporal_tls(settings, api_key=api_key)
    if catalog_only:
        if tenant_effect_authority is not None:
            raise ValueError("catalog Temporal storage cannot use a tenant effect authority")
        converter = build_catalog_claim_check_data_converter(
            object_store,
            settings.claim_check_threshold_bytes,
        )
    else:
        effect_authority = tenant_effect_authority or PostgresTenantEffectAuthority(
            TenantEffectAuthorityConfig(
                lock_timeout_seconds=settings.tenant_effect_lock_timeout_seconds
            )
        )
        converter = build_claim_check_data_converter(
            object_store,
            settings.claim_check_threshold_bytes,
            tenant_effect_authority=effect_authority,
            tenant_effect_timeout_seconds=settings.tenant_effect_timeout_seconds,
        )
    connection = Client.connect(
        settings.temporal_target,
        namespace=settings.temporal_namespace,
        api_key=api_key,
        tls=tls,
        lazy=lazy,
        interceptors=[
            TemporalTaskQueueRoutingInterceptor(
                transactional_queue=settings.temporal_transactional_queue,
                catalog_queue=settings.temporal_catalog_queue,
            )
        ],
        data_converter=converter,
    )
    if lazy:
        # The SDK's lazy client deliberately performs no eager transport handshake. Keep API
        # composition immediate and let deadlines on each later start/signal own network liveness.
        return await connection
    async with asyncio.timeout(settings.temporal_rpc_timeout_seconds):
        return await connection


def validate_temporal_settings(settings: Settings) -> None:
    """Reject insecure transport and deployed worker routing without engine reachability."""
    api_key = _temporal_api_key(settings)
    _temporal_tls(settings, api_key=api_key)

    deployed = settings.env.strip().casefold() not in _LOCAL_ENVIRONMENTS
    if not deployed:
        return

    transactional_queue = settings.temporal_transactional_task_queue
    catalog_queue = settings.temporal_catalog_task_queue
    if transactional_queue is None or catalog_queue is None:
        raise ValueError(
            "deployed Temporal workers require explicit transactional and catalog queues"
        )
    if transactional_queue == catalog_queue:
        raise ValueError("deployed Temporal transactional and catalog queues must be separate")
    if settings.temporal_task_queue not in {transactional_queue, catalog_queue}:
        raise ValueError("legacy Temporal task queue must alias one deployed workload queue")
    if not settings.temporal_worker_versioning_enabled:
        raise ValueError("deployed Temporal workers require Worker Deployment versioning")
    if not _is_immutable_worker_build_id(settings.temporal_effective_worker_build_id):
        raise ValueError("deployed Temporal worker build ID must be a commit or semantic version")
    if (
        settings.temporal_worker_role == "combined"
        and settings.temporal_worker_max_concurrent_workflow_tasks < _SPLIT_QUEUE_MIN_WORKFLOW_TASKS
    ):
        raise ValueError("a combined split-queue worker requires at least four workflow-task slots")
    if (
        settings.temporal_worker_role == "combined"
        and settings.temporal_worker_max_concurrent_activities < _SPLIT_QUEUE_MIN_ACTIVITIES
    ):
        raise ValueError("a combined split-queue worker requires at least two activity slots")


def _temporal_api_key(settings: Settings) -> str | None:
    """Reveal a configured API key only at the Temporal transport boundary."""
    if settings.temporal_api_key is None:
        return None
    api_key = settings.temporal_api_key.get_secret_value().strip()
    if not api_key:
        raise ValueError("Temporal API key must not be empty when configured")
    return api_key


def _is_immutable_worker_build_id(value: str) -> bool:
    return bool(_COMMIT_BUILD_ID.fullmatch(value) or _SEMANTIC_BUILD_ID.fullmatch(value))


def _temporal_tls(settings: Settings, *, api_key: str | None) -> bool | TLSConfig | None:
    """Build TLS settings and reject credential transport over plaintext."""
    configured_domain = settings.temporal_tls_domain
    domain = configured_domain.strip() if configured_domain is not None else None
    if configured_domain is not None and not domain:
        raise ValueError("Temporal TLS domain must not be empty when configured")
    if domain is not None and not _TLS_DOMAIN.fullmatch(domain):
        raise ValueError("Temporal TLS domain must be a DNS server name without URL or port")
    files = (
        settings.temporal_tls_server_ca_file,
        settings.temporal_tls_client_cert_file,
        settings.temporal_tls_client_key_file,
    )
    if any(value is not None for value in files):
        return _temporal_mtls(settings, api_key=api_key, domain=domain, files=files)
    if api_key is not None and not settings.temporal_tls_enabled:
        raise ValueError("Temporal API-key authentication requires TLS")
    if not settings.temporal_tls_enabled:
        if domain is not None:
            raise ValueError("Temporal TLS domain requires TLS")
        return None
    return TLSConfig(domain=domain) if domain is not None else True


def _temporal_mtls(
    settings: Settings,
    *,
    api_key: str | None,
    domain: str | None,
    files: tuple[str | None, str | None, str | None],
) -> TLSConfig:
    if not settings.temporal_tls_enabled:
        raise ValueError("Temporal mTLS requires TLS")
    if api_key is not None:
        raise ValueError("Temporal API-key and mTLS authentication cannot be combined")
    if not domain or not all(files):
        raise ValueError(
            "Temporal mTLS requires a server name, CA, client certificate and key files"
        )
    try:
        ca, certificate, key = (
            read_secret_file(path, setting_name=f"EC_TEMPORAL_TLS_{name}").encode("utf-8")
            for path, name in zip(files, ("SERVER_CA", "CLIENT_CERT", "CLIENT_KEY"), strict=True)
            if path is not None
        )
        _validate_mtls_material(ca, certificate, key)
    except (
        OSError,
        ValueError,
        TypeError,
        UnsupportedAlgorithm,
        x509.ExtensionNotFound,
        x509.DuplicateExtension,
    ):
        # Crypto and filesystem exceptions can include a path or parsed input. Never expose them.
        raise ValueError(
            "Temporal mTLS files must contain valid CA and matching client credentials"
        ) from None
    return TLSConfig(
        server_root_ca_cert=ca,
        domain=domain,
        client_cert=certificate,
        client_private_key=key,
    )


def _validate_mtls_material(ca: bytes, certificate: bytes, key: bytes) -> None:
    authorities = x509.load_pem_x509_certificates(ca)
    chain = x509.load_pem_x509_certificates(certificate)
    if not authorities or not chain:
        raise ValueError("certificate bundle is empty")
    now = datetime.now(UTC)
    for cert in (*authorities, *chain):
        if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise ValueError("certificate is outside its validity period")
    for authority in authorities:
        if not authority.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError("server trust requires CA certificates")
    client = chain[0]
    purposes = client.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if ExtendedKeyUsageOID.CLIENT_AUTH not in purposes:
        raise ValueError("client certificate does not allow client authentication")
    private_key = serialization.load_pem_private_key(key, password=None)
    public_format = serialization.PublicFormat.SubjectPublicKeyInfo
    if private_key.public_key().public_bytes(
        serialization.Encoding.DER, public_format
    ) != client.public_key().public_bytes(serialization.Encoding.DER, public_format):
        raise ValueError("client certificate and private key do not match")
