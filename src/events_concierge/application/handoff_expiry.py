"""ADR-007's orphan-only repair drain for ignored handoff task TTLs.

The ordinary Temporal child owns the authoritative timer. This worker is intentionally weaker:
after the queue's five-minute grace it confirms that the workflow is closed before it invokes its
own queue-lease-fenced guarded expiry capability. A live workflow is never expired by polling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..ports.handoff_expiry import (
    HandoffExpiryPort,
    HandoffExpiryRecord,
    HandoffExpiryRepairOutcome,
)
from ..ports.workflows import WorkflowLivenessInspector


@dataclass(frozen=True, slots=True)
class HandoffExpiryStats:
    """Observable result of one bounded orphan-expiry repair pass (FR-6.6, ADR-007)."""

    claimed: int = 0
    expired: int = 0
    acknowledged: int = 0
    deferred_live: int = 0
    retried: int = 0
    lost_leases: int = 0


class HandoffExpiryWorker:
    """Repair closed-workflow handoff TTLs without becoming a second lifecycle authority."""

    def __init__(
        self,
        expiries: HandoffExpiryPort,
        liveness: WorkflowLivenessInspector,
        *,
        lease_seconds: int = 60,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("handoff expiry lease_seconds must be positive")
        self._expiries = expiries
        self._liveness = liveness
        self._lease_seconds = lease_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def expire_once(self, *, limit: int = 50) -> HandoffExpiryStats:
        """Repair a bounded post-grace batch, retaining every uncertain record for retry."""
        if limit < 1:
            raise ValueError("handoff expiry limit must be positive")
        records = await self._expiries.claim_batch(limit, self._lease_seconds)
        stats = HandoffExpiryStats(claimed=len(records))
        for record in records:
            stats = await self._expire_record(record, stats)
        return stats

    async def _expire_record(
        self, record: HandoffExpiryRecord, stats: HandoffExpiryStats
    ) -> HandoffExpiryStats:
        try:
            if await self._liveness.is_open(record.workflow_id):
                rescheduled = await self._expiries.reschedule(
                    record,
                    retry_at=self._now() + timedelta(minutes=1),
                    error="workflow remains open; Temporal owns this handoff timer",
                )
                return _with(
                    stats,
                    deferred_live=stats.deferred_live + int(rescheduled),
                    lost_leases=stats.lost_leases + int(not rescheduled),
                )
            outcome = await self._expiries.expire_orphan(record)
            if outcome is HandoffExpiryRepairOutcome.LEASE_LOST:
                return _with(stats, lost_leases=stats.lost_leases + 1)
            if outcome is HandoffExpiryRepairOutcome.IGNORED:
                rescheduled = await self._expiries.reschedule(
                    record,
                    retry_at=self._retry_at(record),
                    error="handoff expiry state is not yet terminalizable",
                )
                return _with(
                    stats,
                    retried=stats.retried + int(rescheduled),
                    lost_leases=stats.lost_leases + int(not rescheduled),
                )
            return _with(
                stats,
                expired=stats.expired + int(outcome is HandoffExpiryRepairOutcome.EXPIRED),
                acknowledged=stats.acknowledged + 1,
            )
        except Exception as error:
            rescheduled = await self._expiries.reschedule(
                record,
                retry_at=self._retry_at(record),
                error=str(error),
            )
            return _with(
                stats,
                retried=stats.retried + int(rescheduled),
                lost_leases=stats.lost_leases + int(not rescheduled),
            )

    def _retry_at(self, record: HandoffExpiryRecord) -> datetime:
        """Use a bounded retry without turning an orphan repair into a busy loop."""
        seconds = min(30 * (2 ** max(record.attempt_count - 1, 0)), 15 * 60)
        return self._now() + timedelta(seconds=seconds)


def _with(stats: HandoffExpiryStats, **changes: int) -> HandoffExpiryStats:
    """Return an immutable stats replacement without mutable cross-record state."""
    return HandoffExpiryStats(
        claimed=changes.get("claimed", stats.claimed),
        expired=changes.get("expired", stats.expired),
        acknowledged=changes.get("acknowledged", stats.acknowledged),
        deferred_live=changes.get("deferred_live", stats.deferred_live),
        retried=changes.get("retried", stats.retried),
        lost_leases=changes.get("lost_leases", stats.lost_leases),
    )
