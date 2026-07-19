"""ADR-008's delayed direct reconciliation safety net for closed workflow fanout targets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..ports.calendar_repair import CalendarRepairRecord, ClosedWorkflowCalendarRepairPort
from ..ports.repositories import CatalogRepository
from .reconciliation import LifecycleReconciliationService, OrganizerChange, ReconcileStatus


@dataclass(frozen=True, slots=True)
class CalendarRepairStats:
    """Observable result of one bounded closed-workflow repair drain (ADR-008)."""

    claimed: int = 0
    repaired: int = 0
    acknowledged: int = 0
    retried: int = 0
    lost_leases: int = 0


class _RepairOutcome(StrEnum):
    """Internal projection of one repair attempt before its queue acknowledgement."""

    REPAIRED = "repaired"
    ACK_ONLY = "ack_only"
    LEASE_LOST = "lease_lost"


class ClosedWorkflowCalendarRepairWorker:
    """Run delayed repair only while the exact lifecycle still needs reconciliation.

    The normal workflow may have already committed a terminal cancellation or user withdrawal.
    ``LifecycleReconciliationService`` checks the active lifecycle and matching workflow id first,
    so an ignored result is an intentional safe acknowledgement: a late reschedule never recreates
    an entry after un-RSVP, while an orphaned-but-active lifecycle gets the same guarded calendar
    and transition sequence as the normal Temporal path (ADR-007/ADR-008).
    """

    def __init__(
        self,
        repairs: ClosedWorkflowCalendarRepairPort,
        catalog: CatalogRepository,
        reconciliation: LifecycleReconciliationService,
        *,
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("calendar repair lease_seconds must be positive")
        self._repairs = repairs
        self._catalog = catalog
        self._reconciliation = reconciliation
        self._lease_seconds = lease_seconds

    async def repair_once(self, *, limit: int = 50) -> CalendarRepairStats:
        """Repair a bounded ready batch with durable retry after an application/port failure."""
        if limit < 1:
            raise ValueError("calendar repair limit must be positive")
        records = await self._repairs.claim_batch(limit, self._lease_seconds)
        repaired = 0
        acknowledged = 0
        retried = 0
        lost_leases = 0
        for record in records:
            try:
                outcome = await self._repair(record)
            except Exception as error:
                await self._repairs.reschedule(record, error=str(error))
                retried += 1
                continue
            if outcome is _RepairOutcome.LEASE_LOST:
                # A fresh repair claimant may now own this row. Do not reconcile, acknowledge,
                # or reschedule under the stale token; its queue lease will recover naturally.
                lost_leases += 1
                continue
            repaired += int(outcome is _RepairOutcome.REPAIRED)
            acknowledged += int(await self._repairs.mark_repaired(record))
        return CalendarRepairStats(
            claimed=len(records),
            repaired=repaired,
            acknowledged=acknowledged,
            retried=retried,
            lost_leases=lost_leases,
        )

    async def _repair(self, record: CalendarRepairRecord) -> _RepairOutcome:
        """Invoke the normal guarded reconcile path with a deterministic sweeper transition id."""
        if await self._repairs.organizer_change_applied(record):
            # The normal workflow committed calendar + lifecycle/outbox truth before its fanout
            # acknowledgement was lost.  Do not repeat a RECONCILED self-transition merely to
            # repair the caller's missing ACK (ADR-007/ADR-008).
            return _RepairOutcome.ACK_ONLY
        event = await self._catalog.get(record.change.canonical_event_id)
        if event is None:
            # Deleting a canonical event cascades its change/repair rows.  If a rare concurrent
            # catalog removal wins just before this read, no calendar mutation is safer than
            # guessing an event payload that has been intentionally erased.
            return _RepairOutcome.ACK_ONLY
        if not await self._repairs.has_live_repair_lease(record):
            # The marker's false result intentionally remains ambiguous: it means only that the
            # normal transition is not known to have committed. A separate exact-token/database-
            # clock lease projection is required before this worker can reach CalendarPort
            # (ADR-008, NFR-8).
            return _RepairOutcome.LEASE_LOST
        result = await self._reconciliation.reconcile_organizer_change(
            record.tenant_id,
            event,
            record.workflow_id,
            OrganizerChange(
                fingerprint=record.change.fingerprint,
                source=record.change.source,
                event_status=record.change.event_status,
                start_at=record.change.start_at,
                end_at=record.change.end_at,
                time_zone=record.change.time_zone,
                title=record.change.title,
                venue_name=record.change.venue_name,
            ),
            transition_id=(f"calendar-repair:{record.workflow_id}:{record.change.fingerprint}"),
        )
        return (
            _RepairOutcome.REPAIRED
            if result.status in {ReconcileStatus.CANCELLED, ReconcileStatus.RECONCILED}
            else _RepairOutcome.ACK_ONLY
        )
