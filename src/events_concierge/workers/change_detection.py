"""ADR-008 lifecycle-watch projection and organizer-change fanout worker.

Provider-specific polling/webhook ingestion remains deliberately outside this process until its
owner-gated integrations are enabled.  This worker only drains durable work already written by a
guarded lifecycle transition or by a normalized detector: it projects watches first, then delivers
leased organizer-change commands to Temporal.  Either side can crash or be unavailable without
losing the corresponding PostgreSQL record.
"""

from __future__ import annotations

import asyncio

from temporalio.client import Client

from ..application.calendar_repair import ClosedWorkflowCalendarRepairWorker
from ..application.change_detection import ChangeDetectionService
from ..application.watch_projection import LifecycleWatchProjectionRelay
from ..composition import build_container
from ..config import Settings, get_settings
from ..infra.logging import configure_logging, get_logger
from ..ports.object_store import ObjectStorePort
from ..workflows.start import TemporalOrganizerChangeFanout
from ..workflows.temporal_client import connect_temporal

_log = get_logger(__name__)


async def run_change_delivery() -> None:
    """Continuously recover watch projection and Temporal fanout from durable queues (ADR-008)."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    container = build_container(settings)
    projection = LifecycleWatchProjectionRelay(
        container.watch_projection_outbox,
        container.change_detection_repo,
        lease_seconds=settings.change_delivery_lease_seconds,
    )
    repairs = ClosedWorkflowCalendarRepairWorker(
        container.calendar_repair_repo,
        container.catalog,
        container.reconciliation,
        lease_seconds=settings.change_delivery_lease_seconds,
    )
    changes: ChangeDetectionService | None = None
    _log.info(
        "change delivery worker started",
        batch_size=settings.change_delivery_batch_size,
        poll_seconds=settings.change_delivery_poll_seconds,
    )
    while True:
        try:
            projected = await projection.relay_once(limit=settings.change_delivery_batch_size)
        except Exception as exc:
            _log.warning("lifecycle watch projection failed", error=str(exc))
            await asyncio.sleep(settings.change_delivery_poll_seconds)
            continue
        try:
            repaired = await repairs.repair_once(limit=settings.change_delivery_batch_size)
        except Exception as exc:
            _log.warning("closed-workflow calendar repair failed", error=str(exc))
            repaired = None
        if changes is None:
            client = await _try_connect_temporal(settings, container.object_store)
            if client is not None:
                changes = ChangeDetectionService(
                    container.change_detection_repo,
                    container.change_detection_repo,
                    TemporalOrganizerChangeFanout(client, settings),
                    closed_workflow_repairs=container.calendar_repair_repo,
                    fanout_batch_size=settings.change_delivery_batch_size,
                    lease_seconds=settings.change_delivery_lease_seconds,
                )
        try:
            fanned_out = await changes.fanout_pending() if changes is not None else None
        except Exception as exc:
            # The change and its delivery lease remain durable.  Keep projecting DB-only watches
            # on later cycles even while this Temporal client reconnects or recovers.
            _log.warning("organizer-change fanout failed", error=str(exc))
            fanned_out = None
        _log.info(
            "change delivery worker cycle",
            projection_claimed=projected.claimed,
            projection_applied=projected.applied,
            projection_acknowledged=projected.acknowledged,
            projection_retried=projected.retried,
            projection_lost_leases=projected.lost_leases,
            repair_claimed=repaired.claimed if repaired is not None else None,
            repair_applied=repaired.repaired if repaired is not None else None,
            repair_acknowledged=repaired.acknowledged if repaired is not None else None,
            repair_retried=repaired.retried if repaired is not None else None,
            repair_lost_leases=repaired.lost_leases if repaired is not None else None,
            fanout_claimed=fanned_out.claimed if fanned_out is not None else None,
            fanout_delivered=fanned_out.delivered if fanned_out is not None else None,
            fanout_deferred=fanned_out.deferred if fanned_out is not None else None,
            fanout_repair_queued=fanned_out.repair_queued if fanned_out is not None else None,
            fanout_lost_leases=fanned_out.lost_leases if fanned_out is not None else None,
        )
        if (
            projected.claimed == 0
            and (repaired is None or repaired.claimed == 0)
            and (fanned_out is None or fanned_out.claimed == 0)
        ):
            await asyncio.sleep(settings.change_delivery_poll_seconds)


async def _try_connect_temporal(settings: Settings, object_store: ObjectStorePort) -> Client | None:
    """Connect opportunistically; the DB-only watch projector must not wait on Temporal."""
    try:
        return await connect_temporal(settings, object_store)
    except Exception as exc:
        _log.warning("temporal unavailable for organizer-change fanout; retrying", error=str(exc))
        return None


def main() -> None:
    asyncio.run(run_change_delivery())


if __name__ == "__main__":
    main()
