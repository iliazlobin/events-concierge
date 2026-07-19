"""Shared secure Temporal client construction for every worker entrypoint."""

from __future__ import annotations

import asyncio

from temporalio.client import Client, TLSConfig

from ..config import Settings
from ..ports.object_store import ObjectStorePort
from .claim_check import build_claim_check_data_converter


async def connect_temporal(
    settings: Settings,
    object_store: ObjectStorePort,
    *,
    lazy: bool = False,
) -> Client:
    """Connect with one consistent namespace, claim-check converter, TLS, and API-key posture.

    API composition uses a lazy client so a cold-start engine outage can retain database-backed
    intake and reconnect through the same client later. Workers use the eager default so an
    unavailable task queue makes the process exit and restart under its supervisor.
    """
    api_key = _temporal_api_key(settings)
    tls = _temporal_tls(settings, api_key=api_key)
    connection = Client.connect(
        settings.temporal_target,
        namespace=settings.temporal_namespace,
        api_key=api_key,
        tls=tls,
        lazy=lazy,
        data_converter=build_claim_check_data_converter(
            object_store, settings.claim_check_threshold_bytes
        ),
    )
    if lazy:
        # The SDK's lazy client deliberately performs no eager transport handshake. Keep API
        # composition immediate and let deadlines on each later start/signal own network liveness.
        return await connection
    async with asyncio.timeout(settings.temporal_rpc_timeout_seconds):
        return await connection


def validate_temporal_settings(settings: Settings) -> None:
    """Reject insecure transport/credential combinations without requiring engine reachability."""
    api_key = _temporal_api_key(settings)
    _temporal_tls(settings, api_key=api_key)


def _temporal_api_key(settings: Settings) -> str | None:
    """Reveal a configured API key only at the Temporal transport boundary."""
    if settings.temporal_api_key is None:
        return None
    api_key = settings.temporal_api_key.get_secret_value().strip()
    if not api_key:
        raise ValueError("Temporal API key must not be empty when configured")
    return api_key


def _temporal_tls(settings: Settings, *, api_key: str | None) -> bool | TLSConfig | None:
    """Build TLS settings and reject credential transport over plaintext."""
    configured_domain = settings.temporal_tls_domain
    domain = configured_domain.strip() if configured_domain is not None else None
    if configured_domain is not None and not domain:
        raise ValueError("Temporal TLS domain must not be empty when configured")
    if api_key is not None and not settings.temporal_tls_enabled:
        raise ValueError("Temporal API-key authentication requires TLS")
    if not settings.temporal_tls_enabled:
        if domain is not None:
            raise ValueError("Temporal TLS domain requires TLS")
        return None
    return TLSConfig(domain=domain) if domain is not None else True
