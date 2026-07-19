"""Offline orphan-only handoff-expiry repair tests (FR-6.6, ADR-007)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from events_concierge.application.handoff_expiry import HandoffExpiryWorker
from events_concierge.config import Settings
from events_concierge.domain.enums import PriceStatus
from events_concierge.domain.events import CanonicalEvent
from events_concierge.ports.handoff_expiry import (
    HandoffExpiryPort,
    HandoffExpiryRecord,
    HandoffExpiryRepairOutcome,
)
from events_concierge.ports.workflows import WorkflowLivenessInspector


class _ExpiryQueue:
    """Small lease-aware queue double retaining atomic repair and retry decisions."""

    def __init__(
        self,
        records: list[HandoffExpiryRecord],
        outcome: HandoffExpiryRepairOutcome | Exception = HandoffExpiryRepairOutcome.EXPIRED,
    ) -> None:
        self._records = list(records)
        self._outcome = outcome
        self.expired: list[HandoffExpiryRecord] = []
        self.acknowledged: list[HandoffExpiryRecord] = []
        self.rescheduled: list[tuple[HandoffExpiryRecord, datetime, str]] = []

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[HandoffExpiryRecord]:
        assert limit > 0
        assert lease_seconds > 0
        records, self._records = self._records[:limit], self._records[limit:]
        return records

    async def expire_orphan(self, record: HandoffExpiryRecord) -> HandoffExpiryRepairOutcome:
        self.expired.append(record)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def acknowledge(self, record: HandoffExpiryRecord) -> bool:
        self.acknowledged.append(record)
        return True

    async def reschedule(
        self, record: HandoffExpiryRecord, *, retry_at: datetime, error: str
    ) -> bool:
        self.rescheduled.append((record, retry_at, error))
        return True


class _Liveness:
    """Temporal inspection seam whose closed result authorizes the repair attempt."""

    def __init__(self, is_open: bool) -> None:
        self._is_open = is_open
        self.calls: list[str] = []

    async def is_open(self, workflow_id: str) -> bool:
        self.calls.append(workflow_id)
        return self._is_open


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Handoff expiry fixture",
        start_at=datetime(2026, 8, 1, 18, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 20, tzinfo=UTC),
        price_status=PriceStatus.FREE,
    )


def _record(
    event: CanonicalEvent, tenant_id: UUID, *, attempt_count: int = 1
) -> HandoffExpiryRecord:
    workflow_id = f"{tenant_id}:{event.canonical_event_id}"
    return HandoffExpiryRecord(
        task_id=f"{workflow_id}:handoff",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=event.canonical_event_id,
        expiry_transition_id=f"{workflow_id}:handoff-expired:1",
        ttl_expires_at=datetime(2026, 7, 20, tzinfo=UTC),
        attempt_count=attempt_count,
        lease_token="expiry-lease",
    )


def _worker(
    queue: _ExpiryQueue,
    liveness: _Liveness,
    *,
    now: datetime,
) -> HandoffExpiryWorker:
    return HandoffExpiryWorker(
        cast(HandoffExpiryPort, queue),
        cast(WorkflowLivenessInspector, liveness),
        now=lambda: now,
    )


def test_orphan_expiry_sweeper_uses_the_ratified_fifteen_minute_cadence() -> None:
    """Only a closed-workflow repair runs at ADR-007's 15-minute cadence after DB grace."""
    assert Settings().handoff_expiry_poll_seconds == 900.0


async def test_live_workflow_is_rescheduled_without_invoking_expiry() -> None:
    """A polling repair must never preempt Temporal's still-live handoff timer (ADR-007)."""
    now = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    event = _event()
    record = _record(event, uuid4())
    queue = _ExpiryQueue([record])
    worker = _worker(queue, _Liveness(True), now=now)

    stats = await worker.expire_once()

    assert stats.claimed == 1
    assert stats.expired == 0
    assert stats.acknowledged == 0
    assert stats.deferred_live == 1
    assert stats.retried == 0
    assert stats.lost_leases == 0
    assert queue.expired == []
    assert queue.acknowledged == []
    assert queue.rescheduled == [
        (
            record,
            now + timedelta(minutes=1),
            "workflow remains open; Temporal owns this handoff timer",
        )
    ]


async def test_closed_orphan_reenters_guarded_expiry_and_acknowledges() -> None:
    """A closed Temporal execution is repaired through one atomic guarded expiry capability."""
    now = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    tenant_id, event = uuid4(), _event()
    record = _record(event, tenant_id)
    queue = _ExpiryQueue([record])
    worker = _worker(queue, _Liveness(False), now=now)

    stats = await worker.expire_once()

    assert stats.claimed == 1
    assert stats.expired == 1
    assert stats.acknowledged == 1
    assert stats.deferred_live == 0
    assert stats.retried == 0
    assert stats.lost_leases == 0
    assert queue.expired == [record]
    assert queue.acknowledged == []
    assert queue.rescheduled == []


async def test_closed_orphan_expiry_does_not_depend_on_catalog_retention() -> None:
    """Task-scoped expiry terminalizes after catalog retention because the worker has no catalog port."""
    now = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    event = _event()
    record = _record(event, uuid4())
    queue = _ExpiryQueue([record])
    worker = _worker(queue, _Liveness(False), now=now)

    stats = await worker.expire_once()

    assert stats.claimed == 1
    assert stats.expired == 1
    assert stats.acknowledged == 1
    assert stats.deferred_live == 0
    assert stats.retried == 0
    assert stats.lost_leases == 0
    assert queue.expired == [record]
    assert queue.acknowledged == []
    assert queue.rescheduled == []


async def test_transient_orphan_expiry_failure_is_rescheduled_with_bounded_backoff() -> None:
    """An uncertain guarded transition stays durable and retries instead of being silently dropped."""
    now = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    event = _event()
    record = _record(event, uuid4(), attempt_count=4)
    queue = _ExpiryQueue([record], RuntimeError("lifecycle database unavailable"))
    worker = _worker(queue, _Liveness(False), now=now)

    stats = await worker.expire_once()

    assert stats.claimed == 1
    assert stats.expired == 0
    assert stats.acknowledged == 0
    assert stats.deferred_live == 0
    assert stats.retried == 1
    assert stats.lost_leases == 0
    assert queue.expired == [record]
    assert queue.acknowledged == []
    assert queue.rescheduled == [
        (
            record,
            now + timedelta(seconds=240),
            "lifecycle database unavailable",
        )
    ]


async def test_closed_orphan_with_lost_queue_authority_does_not_retry_or_mutate() -> None:
    """A post-liveness stale repair loses silently; a fresh queue owner controls recovery (ADR-007)."""
    now = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    event = _event()
    record = _record(event, uuid4())
    queue = _ExpiryQueue([record], HandoffExpiryRepairOutcome.LEASE_LOST)
    worker = _worker(queue, _Liveness(False), now=now)

    stats = await worker.expire_once()

    assert stats.claimed == 1
    assert stats.expired == 0
    assert stats.acknowledged == 0
    assert stats.deferred_live == 0
    assert stats.retried == 0
    assert stats.lost_leases == 1
    assert queue.expired == [record]
    assert queue.acknowledged == []
    assert queue.rescheduled == []


async def test_ignored_orphan_repair_releases_its_live_lease_for_retry() -> None:
    """A nonterminalizable closed row retains its queue obligation instead of being consumed."""
    now = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    event = _event()
    record = _record(event, uuid4(), attempt_count=2)
    queue = _ExpiryQueue([record], HandoffExpiryRepairOutcome.IGNORED)
    worker = _worker(queue, _Liveness(False), now=now)

    stats = await worker.expire_once()

    assert stats.claimed == 1
    assert stats.expired == 0
    assert stats.acknowledged == 0
    assert stats.deferred_live == 0
    assert stats.retried == 1
    assert stats.lost_leases == 0
    assert queue.expired == [record]
    assert queue.rescheduled == [
        (record, now + timedelta(seconds=60), "handoff expiry state is not yet terminalizable")
    ]
