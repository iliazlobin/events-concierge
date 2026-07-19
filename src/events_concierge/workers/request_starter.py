"""Durable ADR-003 start-outbox worker for parent EventRequest workflows.

The API performs a low-latency attempt after committing an intake. This worker is the loss-proof
path: it reconnects to Temporal after an outage and replays leased starts until reject-duplicate or
a fresh start confirms the deterministic parent workflow exists.
"""

from __future__ import annotations

import asyncio

from ..application.request_start import RequestStartRelay
from ..composition import build_container
from ..config import Settings, get_settings
from ..infra.logging import configure_logging, get_logger
from ..ports.object_store import ObjectStorePort
from ..workflows.start import TemporalRequestWorkflowStarter
from ..workflows.temporal_client import connect_temporal

_log = get_logger(__name__)


async def run_request_starter() -> None:
    """Continuously drain durable starts; engine startup failures wait without dropping rows."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    container = build_container(settings)
    starter = await _connect_starter(settings, container.object_store)
    relay = RequestStartRelay(
        container.request_repo,
        starter,
        lease_seconds=settings.request_start_lease_seconds,
    )
    _log.info(
        "request start worker started",
        batch_size=settings.request_start_batch_size,
        poll_seconds=settings.request_start_poll_seconds,
    )
    while True:
        try:
            stats = await relay.relay_once(limit=settings.request_start_batch_size)
        except Exception as exc:
            _log.warning("request start relay poll failed", error=str(exc))
            await asyncio.sleep(settings.request_start_poll_seconds)
            continue
        if stats.claimed == 0:
            await asyncio.sleep(settings.request_start_poll_seconds)


async def _connect_starter(
    settings: Settings, object_store: ObjectStorePort
) -> TemporalRequestWorkflowStarter:
    """Retry initial Temporal connectivity; queue rows remain authoritative during an outage."""
    while True:
        try:
            client = await connect_temporal(settings, object_store)
            return TemporalRequestWorkflowStarter(client, settings)
        except Exception as exc:
            _log.warning("temporal unavailable for request starts; retrying", error=str(exc))
            await asyncio.sleep(2)


def main() -> None:
    asyncio.run(run_request_starter())


if __name__ == "__main__":
    main()
