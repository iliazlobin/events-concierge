"""Durable ADR-003 start-outbox worker for parent EventRequest workflows.

The API performs a low-latency attempt after committing an intake. This worker is the loss-proof
path: it reconnects to Temporal after an outage and replays leased starts until reject-duplicate or
a fresh start confirms the deterministic parent workflow exists.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Protocol

from ..application.request_start import RequestStartRelay, RequestStartRelayStats
from ..composition import build_container
from ..config import Settings, get_settings
from ..deployment.startup import preflight_application_runtime
from ..infra.logging import configure_logging, get_logger
from ..ports.object_store import ObjectStorePort
from ..ports.tenant_effects import TenantEffectAuthority
from ..workflows.start import TemporalRequestWorkflowStarter
from ..workflows.temporal_client import connect_temporal

_log = get_logger(__name__)


class _RequestStartRelayPort(Protocol):
    """Minimal relay seam used by the paced worker loop and its offline tests."""

    async def relay_once(self, *, limit: int = 50) -> RequestStartRelayStats: ...


async def run_request_starter() -> None:
    """Continuously drain durable starts; engine startup failures wait without dropping rows."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    container = build_container(settings, runtime_ports=preflight_application_runtime(settings))
    starter = await _connect_starter(
        settings,
        container.object_store,
        container.tenant_effect_authority,
    )
    relay = RequestStartRelay(
        container.request_repo,
        starter,
        lease_seconds=settings.request_start_lease_seconds,
        tenant_effect_authority=container.tenant_effect_authority,
        tenant_effect_timeout_seconds=settings.tenant_effect_timeout_seconds,
    )
    _log.info(
        "request start worker started",
        batch_size=settings.request_start_batch_size,
        poll_seconds=settings.request_start_poll_seconds,
    )
    await _run_request_start_relay(
        relay,
        batch_size=settings.request_start_batch_size,
        minimum_cycle_seconds=settings.request_start_poll_seconds,
    )


async def _run_request_start_relay(
    relay: _RequestStartRelayPort,
    *,
    batch_size: int,
    minimum_cycle_seconds: float,
    clock: Callable[[], float] = monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Drain bounded batches at a stable cadence, including while backlog remains.

    Measuring from the beginning of one pass to the beginning of the next avoids adding needless
    latency when Temporal itself is slow.  A zero-second sleep is still awaited after an overrun,
    yielding to cancellation and the rest of the event loop before another database claim.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if minimum_cycle_seconds <= 0:
        raise ValueError("minimum_cycle_seconds must be positive")

    while True:
        cycle_started = clock()
        try:
            stats = await relay.relay_once(limit=batch_size)
        except Exception as exc:
            _log.warning("request start relay poll failed", error=str(exc))
        else:
            if stats.claimed:
                _log.info(
                    "request start relay cycle",
                    claimed=stats.claimed,
                    started=stats.started,
                    retried=stats.retried,
                    lost_leases=stats.lost_leases,
                )
        elapsed = max(clock() - cycle_started, 0.0)
        await sleep(max(minimum_cycle_seconds - elapsed, 0.0))


async def _connect_starter(
    settings: Settings,
    object_store: ObjectStorePort,
    tenant_effect_authority: TenantEffectAuthority,
) -> TemporalRequestWorkflowStarter:
    """Retry initial Temporal connectivity; queue rows remain authoritative during an outage."""
    while True:
        try:
            client = await connect_temporal(
                settings,
                object_store,
                tenant_effect_authority=tenant_effect_authority,
            )
            return TemporalRequestWorkflowStarter(client, settings)
        except Exception as exc:
            _log.warning("temporal unavailable for request starts; retrying", error=str(exc))
            await asyncio.sleep(2)


def main() -> None:
    asyncio.run(run_request_starter())


if __name__ == "__main__":
    main()
