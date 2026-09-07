"""Fail-closed validation for a production-shaped application configuration.

Pydantic validates individual field shapes.  This module validates the relationships that make a
whole deployment safe: no mock graph, HTTPS public capabilities, shared pacing, TLS Temporal, and
an explicit deployment provider.  It never renders DSNs, API keys, or provider configuration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..config import Settings
from ..domain.enums import Source
from ..ports.sources import SourceCapability
from ..runtime import RuntimePorts, load_runtime_ports
from ..workflows.temporal_client import validate_temporal_settings
from .network_safety import is_non_remote_host

_LOCAL_ENVIRONMENTS = frozenset({"", "dev", "development", "local", "test", "testing"})
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SEMANTIC_VERSION = re.compile(
    r"^v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_HOST_COOKIE_NAME = re.compile(r"^__Host-[A-Za-z0-9_-]{1,100}$")
_HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_MAX_PORT = 65_535
_MAX_OIDC_URL_LENGTH = 2048
_MAX_OIDC_CLIENT_ID_BYTES = 512
_MAX_OIDC_CLIENT_SECRET_BYTES = 4096
_MAX_OIDC_CLAIM_LENGTH = 256
_MIN_LOGIN_TTL_SECONDS = 60
_MAX_LOGIN_TTL_SECONDS = 900
_MIN_SESSION_TTL_SECONDS = 300
_MAX_SESSION_TTL_SECONDS = 86_400
_ALLOWED_OIDC_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})
_DIRECT_PORT_METHODS: Mapping[str, tuple[str, ...]] = {
    "object_store": ("put", "get", "delete_tenant"),
    "notifier": ("send",),
    "notification_secret_protector": ("protect_completion_url", "reveal_completion_url"),
    "credential_vault": ("store", "get", "revoke", "delete_tenant"),
    "calendar": ("free_busy", "upsert_event", "delete_event", "delete_tenant_events"),
}
_OPTIONAL_PORT_METHODS: Mapping[str, tuple[str, ...]] = {
    "google_calendar_access": ("get_access",),
    "google_calendar_bindings": ("get_binding", "upsert_binding"),
    "action_audit": ("append",),
    "registration_consent": ("resolve", "validate"),
}
_SOURCE_PORT_METHODS = (
    "discover",
    "read_membership_state",
    "read_registration_state",
    "register",
)
_WITHDRAWAL_PORT_METHODS = ("read_registration_state", "withdraw")
_AUTH_CONTEXT_METHODS = ("resolve_tenant_id",)
_CSRF_METHODS = ("verify_state_change",)
_BROWSER_SESSION_METHODS = (
    *_AUTH_CONTEXT_METHODS,
    *_CSRF_METHODS,
    "start_login",
    "start_reauthentication",
    "complete_login",
    "issue_session",
    "revoke_session",
    "revoke_tenant_sessions",
    "verify_recent_auth",
    "is_ready",
    "aclose",
)
_REQUIRED_RUNTIME_PORTS = (
    "object_store",
    "notifier",
    "notification_secret_protector",
    "credential_vault",
    "calendar",
    "discovery_sources",
    "register_sources",
    "withdrawal_sources",
)


@dataclass(frozen=True, slots=True)
class ConfigCheck:
    """One non-secret production configuration assertion."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ProductionConfigReport:
    """Serializable aggregate used by CI and deployment preflight jobs."""

    checks: tuple[ConfigCheck, ...]
    mode: str

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "events-concierge-production-config",
            "evidence_class": ("wiring_preflight" if self.mode == "full" else "example_contract"),
            "mode": self.mode,
            "preflight_eligible": self.mode == "full" and self.passed,
            # Callable-surface inspection cannot establish provider behavior, reachability, or
            # external launch gates; the release controller must combine separate evidence.
            "release_eligible": False,
            "status": "passed" if self.passed else "failed",
            "checks": [asdict(check) for check in self.checks],
        }


def validate_production_config(
    settings: Settings,
    *,
    load_provider: bool = True,
    migration_credential_present: bool = False,
) -> ProductionConfigReport:
    """Validate a production-shaped settings snapshot without connecting to dependencies.

    ``load_provider=False`` is the structural CI/example mode: it verifies that a provider factory
    is named without importing deployment-owned code that is intentionally absent from this
    repository.  A real pre-deploy job must keep the default and load the factory.
    """

    checks = [
        _check(
            "environment",
            settings.env.strip().casefold() not in _LOCAL_ENVIRONMENTS,
            "environment is explicitly non-local",
            "EC_ENV must identify staging or production, not a local/test environment",
        ),
        _check(
            "mock_cloud_disabled",
            not settings.mock_cloud,
            "mock adapters are disabled",
            "EC_MOCK_CLOUD must be false",
        ),
        _check(
            "runtime_provider_configured",
            bool(settings.runtime_provider_factory and settings.runtime_provider_factory.strip()),
            "deployment runtime provider is named",
            "EC_RUNTIME_PROVIDER_FACTORY is required",
        ),
        _check_public_origin(settings.public_base_url),
        _check(
            "ui_auth_entrypoint",
            bool(settings.ui_auth_start_url),
            "deployment login entrypoint is configured",
            "EC_UI_AUTH_START_URL is required for the consumer login flow",
        ),
        _check(
            "built_in_bff_routes",
            not settings.oidc_bff_enabled or settings.ui_auth_start_url == "/auth/login",
            (
                (
                    "repository OIDC BFF owns fixed same-origin /auth/login, /auth/reauth, "
                    "and /auth/logout routes"
                )
                if settings.oidc_bff_enabled
                else "deployment provider owns the configured login entrypoint"
            ),
            (
                "built-in OIDC BFF requires EC_UI_AUTH_START_URL=/auth/login and its fixed "
                "reauthentication/logout routes"
            ),
        ),
        _check(
            "production_identity_profile",
            settings.oidc_bff_enabled,
            "current production profile uses the repository OIDC BFF",
            (
                "current production UI and canary require EC_OIDC_BFF_ENABLED=true; an external "
                "BFF needs a separately parameterized contract"
            ),
        ),
        _check_builtin_identity_configuration(settings),
        _check(
            "shared_pacer",
            settings.uses_shared_pacer_redis,
            "shared Redis pacing/admission is selected",
            "production cannot use process-local pacing",
        ),
        _check_redis(settings.redis_url),
        _check_database(settings.database_url, mode=settings.database_connection_mode),
        _check(
            "database_pool_budget",
            settings.database_max_overflow == 0
            and settings.database_pool_size >= settings.temporal_worker_max_concurrent_activities,
            "database connections are bounded and cover configured activity concurrency",
            (
                "EC_DATABASE_MAX_OVERFLOW must be zero and EC_DATABASE_POOL_SIZE must be at "
                "least EC_TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES"
            ),
        ),
        _check(
            "migration_credential_absent",
            not migration_credential_present,
            "application runtime does not receive EC_MIGRATION_URL",
            "remove the migration-owner credential from the application runtime",
        ),
        _check(
            "temporal_tls",
            settings.temporal_tls_enabled,
            "Temporal TLS is enabled",
            "EC_TEMPORAL_TLS_ENABLED must be true",
        ),
        _check(
            "temporal_credentials",
            bool(
                settings.temporal_api_key and settings.temporal_api_key.get_secret_value().strip()
            ),
            "Temporal API credential is supplied by deployment configuration",
            "EC_TEMPORAL_API_KEY is required by the ratified Temporal Cloud posture",
        ),
        _check(
            "temporal_namespace",
            bool(settings.temporal_namespace.strip())
            and settings.temporal_namespace.strip().casefold() != "default",
            "an explicit non-default Temporal namespace is selected",
            "EC_TEMPORAL_NAMESPACE must not use the local default namespace",
        ),
        _check_remote_target("temporal_target", settings.temporal_target),
        _check_temporal_transport(settings),
        _check(
            "temporal_workload_isolation",
            bool(settings.temporal_transactional_task_queue)
            and bool(settings.temporal_catalog_task_queue)
            and settings.temporal_transactional_queue != settings.temporal_catalog_queue,
            "transactional and catalog workloads use distinct explicit task queues",
            (
                "production requires distinct EC_TEMPORAL_TRANSACTIONAL_TASK_QUEUE and "
                "EC_TEMPORAL_CATALOG_TASK_QUEUE values"
            ),
        ),
        _check(
            "temporal_worker_versioning",
            settings.temporal_worker_versioning_enabled
            and _is_release_revision(settings.temporal_effective_worker_build_id),
            "Temporal Worker Deployment versioning uses an immutable build identity",
            (
                "EC_TEMPORAL_WORKER_VERSIONING_ENABLED must be true and its effective build ID "
                "must be an immutable revision"
            ),
        ),
        _check(
            "gcs_claim_check",
            bool(settings.gcp_project and settings.gcp_project.strip())
            and bool(settings.gcs_claim_check_bucket and settings.gcs_claim_check_bucket.strip())
            and bool(settings.gcs_claim_check_prefix.strip()),
            "GCS claim-check project, bucket, and prefix are explicit",
            "EC_GCP_PROJECT, EC_GCS_CLAIM_CHECK_BUCKET, and a non-empty prefix are required",
        ),
    ]

    release_revision = settings.release_revision
    image_digest = settings.image_digest
    if "release_revision" in type(settings).model_fields:
        checks.append(
            _check(
                "release_revision",
                _is_release_revision(str(release_revision)),
                "immutable release revision is declared",
                "EC_RELEASE_REVISION must be an immutable non-development identifier",
            )
        )
    if "image_digest" in type(settings).model_fields:
        checks.append(
            _check(
                "image_digest",
                image_digest is not None and bool(_SHA256_DIGEST.fullmatch(image_digest)),
                "immutable OCI image digest is declared",
                "EC_IMAGE_DIGEST must use sha256:<64 lowercase hex characters>",
            )
        )

    if load_provider:
        checks.extend(_provider_checks(settings))
    else:
        checks.append(
            ConfigCheck(
                name="runtime_provider_loaded",
                passed=True,
                detail="structural mode: deployment-owned provider import was intentionally skipped",
            )
        )
    return ProductionConfigReport(
        tuple(checks),
        mode="full" if load_provider else "structural_only",
    )


def _provider_checks(settings: Settings) -> list[ConfigCheck]:
    try:
        runtime = load_runtime_ports(settings)
    except Exception as error:
        return [
            ConfigCheck(
                name="runtime_provider_loaded",
                passed=False,
                detail=f"provider failed closed ({type(error).__name__})",
            )
        ]

    checks = [
        ConfigCheck(
            name="runtime_provider_loaded",
            passed=True,
            detail="deployment-owned provider loaded without dependency I/O",
        )
    ]
    checks.extend(_runtime_port_checks(runtime, settings=settings))
    return checks


def _runtime_port_checks(runtime: RuntimePorts, *, settings: Settings) -> list[ConfigCheck]:
    checks: list[ConfigCheck] = []
    if settings.oidc_bff_enabled:
        identity_ready = (
            runtime.auth_context is None
            and runtime.csrf_protection is None
            and runtime.browser_session is None
        )
        checks.append(
            _check(
                "runtime_identity_boundary",
                identity_ready,
                "repository OIDC BFF is the sole session authentication and CSRF authority",
                "built-in OIDC BFF cannot be mixed with provider identity ports",
            )
        )
    elif runtime.browser_session is not None:
        identity_ready = (
            _browser_session_ready(runtime.browser_session)
            and (runtime.auth_context is None or runtime.auth_context is runtime.browser_session)
            and (
                runtime.csrf_protection is None
                or runtime.csrf_protection is runtime.browser_session
            )
        )
        checks.append(
            _check(
                "runtime_identity_boundary",
                identity_ready,
                "one coherent browser-session adapter supplies authentication and CSRF",
                "browser_session must be callable and the sole authentication/CSRF authority",
            )
        )
    else:
        checks.append(
            _check(
                "runtime_identity_boundary",
                False,
                "",
                (
                    "without built-in OIDC BFF, RuntimePorts.browser_session must supply one "
                    "coherent authentication, CSRF, lifecycle, and tenant-revocation authority"
                ),
            )
        )
    for name in _REQUIRED_RUNTIME_PORTS:
        value = getattr(runtime, name)
        passed = _runtime_port_ready(name, value)
        checks.append(
            _check(
                f"runtime_port_{name}",
                passed,
                f"{name} is explicitly provisioned by a non-mock adapter",
                f"deployment provider omitted or mock-provisioned RuntimePorts.{name}",
            )
        )
    for name, methods in _OPTIONAL_PORT_METHODS.items():
        value = getattr(runtime, name)
        if value is None:
            continue
        checks.append(
            _check(
                f"runtime_port_{name}",
                _adapter_ready(value, methods),
                f"optional {name} has its callable non-mock surface",
                f"deployment provider supplied malformed or mock RuntimePorts.{name}",
            )
        )
    return checks


def _check_public_origin(value: str) -> ConfigCheck:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = urlsplit("")
        port = None
    passed = (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"", "/"}
        and (port is None or 1 <= port <= _MAX_PORT)
        and not is_non_remote_host(parsed.hostname)
    )
    return _check(
        "public_origin",
        passed,
        "public capability origin is a remote HTTPS origin",
        "EC_PUBLIC_BASE_URL must be a remote HTTPS origin without path/query/userinfo",
    )


def _check_builtin_identity_configuration(settings: Settings) -> ConfigCheck:
    if not settings.oidc_bff_enabled:
        return ConfigCheck(
            name="built_in_identity_configuration",
            passed=True,
            detail="deployment-owned identity mode selected",
        )
    urls = (
        (settings.oidc_issuer, False),
        (settings.oidc_authorization_url, True),
        (settings.oidc_token_url, True),
        (settings.oidc_jwks_url, True),
    )
    client_id = settings.oidc_client_id or ""
    tenant_claim = settings.oidc_tenant_claim or ""
    secret = (
        settings.oidc_client_secret.get_secret_value()
        if settings.oidc_client_secret is not None
        else ""
    )
    algorithms = settings.oidc_algorithm_allowlist
    passed = (
        all(
            value is not None and _is_remote_https_url(value, allow_query=allow_query)
            for value, allow_query in urls
        )
        and 0 < len(client_id.encode("utf-8")) <= _MAX_OIDC_CLIENT_ID_BYTES
        and 0 < len(secret.encode("utf-8")) <= _MAX_OIDC_CLIENT_SECRET_BYTES
        and 0 < len(tenant_claim) <= _MAX_OIDC_CLAIM_LENGTH
        and bool(algorithms)
        and len(set(algorithms)) == len(algorithms)
        and set(algorithms) <= _ALLOWED_OIDC_ALGORITHMS
    )
    return _check(
        "built_in_identity_configuration",
        passed,
        "built-in OIDC endpoints, client binding, claim, and algorithms are coherent",
        "built-in OIDC requires remote HTTPS endpoints and a bounded asymmetric client graph",
    )


def _check_redis(value: str) -> ConfigCheck:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = urlsplit("")
        port = None
    passed = (
        parsed.scheme == "rediss"
        and bool(parsed.hostname)
        and (port is None or 1 <= port <= _MAX_PORT)
        and not is_non_remote_host(parsed.hostname)
        and not parsed.fragment
        and _redis_tls_query_is_verified(parsed.query)
    )
    return _check(
        "redis_tls",
        passed,
        "remote certificate-verified TLS Redis endpoint is selected",
        "EC_REDIS_URL must use verified rediss:// and must not target loopback",
    )


def _check_database(value: str, *, mode: str) -> ConfigCheck:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = urlsplit("")
        port = None
    common = (
        parsed.scheme == "postgresql+psycopg"
        and bool(parsed.hostname)
        and parsed.username == "ec_app"
        and bool(parsed.password)
        and bool(parsed.path.strip("/"))
        and (port is None or 1 <= port <= _MAX_PORT)
        and not parsed.fragment
    )
    if mode == "cloud_sql_proxy":
        passed = (
            common
            and parsed.hostname == "127.0.0.1"
            and port is not None
            and _postgres_sslmode_is(parsed.query, "disable")
        )
        success = "loopback Cloud SQL Auth Proxy application-role DSN is configured"
        failure = (
            "cloud_sql_proxy mode requires ec_app at 127.0.0.1:<port> with exactly sslmode=disable"
        )
    else:
        passed = (
            common
            and not is_non_remote_host(parsed.hostname)
            and _postgres_sslmode_is(parsed.query, "verify-full")
        )
        success = "remote certificate-and-hostname-verified PostgreSQL ec_app role is configured"
        failure = (
            "direct_tls mode requires postgresql+psycopg, role ec_app, a remote host, and "
            "exactly sslmode=verify-full"
        )
    return _check(
        "application_database",
        passed,
        success,
        failure,
    )


def _check_remote_target(name: str, value: str) -> ConfigCheck:
    try:
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        parsed = urlsplit("")
        host = None
        port = None
    passed = (
        bool(host)
        and port is not None
        and 1 <= port <= _MAX_PORT
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
        and value.isprintable()
        and not any(character.isspace() for character in value)
        and not is_non_remote_host(host)
    )
    return _check(
        name,
        passed,
        "remote dependency target is configured",
        "Temporal target must not resolve to a loopback/local-development host",
    )


def _check_temporal_transport(settings: Settings) -> ConfigCheck:
    try:
        validate_temporal_settings(settings)
    except ValueError:
        passed = False
    else:
        passed = True
    return _check(
        "temporal_transport_configuration",
        passed,
        "Temporal credential and TLS settings form a valid transport",
        "Temporal credential, TLS, or TLS-domain settings are inconsistent",
    )


def _runtime_port_ready(name: str, value: object) -> bool:
    methods = _DIRECT_PORT_METHODS.get(name)
    if methods is not None:
        return _adapter_ready(value, methods)
    if name == "discovery_sources":
        return _source_sequence_ready(value)
    if name == "register_sources":
        return _source_mapping_ready(value)
    if name == "withdrawal_sources":
        return _withdrawal_mapping_ready(value)
    return False


def _adapter_ready(value: object, methods: Sequence[str]) -> bool:
    return (
        value is not None
        and not _contains_repository_mock(value)
        and all(callable(getattr(value, method, None)) for method in methods)
    )


def _source_adapter_ready(value: object) -> bool:
    capability = getattr(value, "capability", None)
    return (
        _adapter_ready(value, _SOURCE_PORT_METHODS)
        and isinstance(capability, SourceCapability)
        and isinstance(capability.source, Source)
        and type(capability.supports_api) is bool
        and type(capability.supports_browser_discovery) is bool
        and type(capability.supports_autonomous_register) is bool
    )


def _source_sequence_ready(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (bytes, str))
        and bool(value)
        and all(_source_adapter_ready(adapter) for adapter in value)
        and len({adapter.capability.source for adapter in value}) == len(value)
    )


def _source_mapping_ready(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(source, Source)
        and _source_adapter_ready(adapter)
        and adapter.capability.source is source
        for source, adapter in value.items()
    )


def _withdrawal_mapping_ready(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(source, Source) and _adapter_ready(adapter, _WITHDRAWAL_PORT_METHODS)
        for source, adapter in value.items()
    )


def _browser_session_ready(value: object) -> bool:
    cookie_names = tuple(
        getattr(value, name, None)
        for name in ("login_cookie_name", "session_cookie_name", "csrf_cookie_name")
    )
    csrf_header = getattr(value, "csrf_header_name", None)
    login_ttl = getattr(value, "login_ttl_seconds", None)
    session_ttl = getattr(value, "session_ttl_seconds", None)
    return (
        _adapter_ready(value, _BROWSER_SESSION_METHODS)
        and all(
            isinstance(name, str) and bool(_HOST_COOKIE_NAME.fullmatch(name))
            for name in cookie_names
        )
        and len(set(cookie_names)) == len(cookie_names)
        and isinstance(csrf_header, str)
        and bool(_HTTP_HEADER_NAME.fullmatch(csrf_header))
        and isinstance(login_ttl, int)
        and _MIN_LOGIN_TTL_SECONDS <= login_ttl <= _MAX_LOGIN_TTL_SECONDS
        and isinstance(session_ttl, int)
        and _MIN_SESSION_TTL_SECONDS <= session_ttl <= _MAX_SESSION_TTL_SECONDS
    )


def _postgres_sslmode_is(query: str, expected: str) -> bool:
    pairs = _query_pairs(query)
    if pairs is None or len(pairs) != 1:
        return False
    key, value = pairs[0]
    return key.strip().casefold() == "sslmode" and value.strip().casefold() == expected


def _redis_tls_query_is_verified(query: str) -> bool:
    pairs = _query_pairs(query)
    if pairs is None:
        return False
    options: dict[str, list[str]] = {}
    for key, value in pairs:
        normalized_key = key.strip().casefold()
        if normalized_key in {"ssl", "ssl_cert_reqs", "ssl_check_hostname"}:
            options.setdefault(normalized_key, []).append(value.strip().casefold())
    if any(len(values) != 1 for values in options.values()):
        return False
    certificate_requirements = options.get("ssl_cert_reqs")
    if certificate_requirements is not None and certificate_requirements[0] not in {
        "cert_required",
        "required",
    }:
        return False
    truthy = {"1", "on", "true", "yes"}
    return all(
        options[name][0] in truthy for name in ("ssl", "ssl_check_hostname") if name in options
    )


def _query_pairs(query: str) -> list[tuple[str, str]] | None:
    try:
        return parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=64)
    except ValueError:
        return None


def _is_remote_https_url(value: str, *, allow_query: bool) -> bool:
    if (
        not value
        or len(value) > _MAX_OIDC_URL_LENGTH
        or not value.isprintable()
        or any(char.isspace() for char in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and (allow_query or not parsed.query)
        and (port is None or 1 <= port <= _MAX_PORT)
        and not is_non_remote_host(parsed.hostname)
    )


def _is_release_revision(value: str) -> bool:
    normalized = value.strip()
    return bool(_COMMIT_REVISION.fullmatch(normalized) or _SEMANTIC_VERSION.fullmatch(normalized))


def _contains_repository_mock(value: object) -> bool:
    if _is_repository_mock(value):
        return True
    if isinstance(value, Mapping):
        return any(_contains_repository_mock(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, str)):
        return any(_contains_repository_mock(item) for item in value)
    return False


def _is_repository_mock(value: object) -> bool:
    return type(value).__module__.startswith("events_concierge.adapters.mock")


def _check(name: str, passed: bool, success: str, failure: str) -> ConfigCheck:
    return ConfigCheck(name=name, passed=passed, detail=success if passed else failure)
