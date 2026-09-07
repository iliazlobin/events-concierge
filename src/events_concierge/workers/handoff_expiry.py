"""Orphan-only repair worker for ADR-007 handoff task TTLs.

Normal expiry is a durable timer in ``RegistrationWorkflow``.  This process runs only after the
queue's five-minute grace, asks Temporal whether the workflow is still open, and repairs a closed
or missing execution through the same guarded lifecycle transition.  It never authoritatively
expires a live child.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from temporalio.client import Client

from ..application.handoff_expiry import HandoffExpiryWorker
from ..composition import build_container
from ..config import Settings, get_settings
from ..infra.logging import configure_logging, get_logger
from ..ports.object_store import ObjectStorePort
from ..workflows.start import TemporalWorkflowLivenessInspector
from ..workflows.temporal_client import connect_temporal

_log = get_logger(__name__)


async def run_handoff_expiry(*, once: bool = False) -> int:
    """Repair task TTLs continuously, or execute one deployment-scheduled pass."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    container = build_container(settings)
    client: Client | None = None
    _log.info(
        "handoff expiry worker started",
        batch_size=settings.handoff_expiry_batch_size,
        poll_seconds=settings.handoff_expiry_poll_seconds,
    )
    while True:
        if client is None:
            client = await _try_connect_temporal(settings, container.object_store)
            if client is None:
                if once:
                    return 1
                await asyncio.sleep(settings.handoff_expiry_poll_seconds)
                continue
        worker = HandoffExpiryWorker(
            container.handoff_expiry_repo,
            TemporalWorkflowLivenessInspector(client, settings),
            lease_seconds=settings.handoff_expiry_lease_seconds,
        )
        try:
            stats = await worker.expire_once(limit=settings.handoff_expiry_batch_size)
        except Exception as error:
            _log.warning("handoff expiry repair failed", error=str(error))
            client = None
            if once:
                return 1
            await asyncio.sleep(settings.handoff_expiry_poll_seconds)
            continue
        _log.info(
            "handoff expiry worker cycle",
            claimed=stats.claimed,
            expired=stats.expired,
            acknowledged=stats.acknowledged,
            deferred_live=stats.deferred_live,
            retried=stats.retried,
            lost_leases=stats.lost_leases,
        )
        if once:
            return 0
        if stats.claimed == 0:
            await asyncio.sleep(settings.handoff_expiry_poll_seconds)


async def _try_connect_temporal(settings: Settings, object_store: ObjectStorePort) -> Client | None:
    """Do not risk orphan expiry while Temporal liveness cannot be checked (ADR-007)."""
    try:
        return await connect_temporal(settings, object_store)
    except Exception as error:
        _log.warning("temporal unavailable for handoff expiry repair", error=str(error))
        return None


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one bounded repair pass for a deployment CronJob, then exit",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    raise SystemExit(asyncio.run(run_handoff_expiry(once=args.once)))


if __name__ == "__main__":
    main()
