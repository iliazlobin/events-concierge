"""Offline ADR-008 closed-workflow calendar-repair safety-net tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from events_concierge.application.calendar_repair import ClosedWorkflowCalendarRepairWorker
from events_concierge.application.reconciliation import (
    LifecycleReconciliationService,
    OrganizerChange,
    ReconcileResult,
    ReconcileStatus,
)
from events_concierge.domain.enums import EventStatus, PriceStatus, Source
from events_concierge.domain.events import CanonicalEvent
from events_concierge.ports.calendar_repair import CalendarRepairRecord
from events_concierge.ports.change_detection import (
    DetectedEventChange,
    OrganizerChangeDelivery,
    organizer_change_fingerprint,
)
from events_concierge.ports.repositories import CatalogRepository


class _RepairQueue:
    """Minimal leased repair queue that exposes acknowledgement/retry behavior to the test."""

    def __init__(
        self, records: list[CalendarRepairRecord], *, organizer_change_applied: bool = False
    ) -> None:
        self._records = list(records)
        self._organizer_change_applied = organizer_change_applied
        self.acknowledged: list[int] = []
        self.retried: list[tuple[int, str]] = []

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[CalendarRepairRecord]:
        assert limit > 0
        assert lease_seconds > 0
        records, self._records = self._records[:limit], self._records[limit:]
        return records

    async def enqueue(self, delivery: OrganizerChangeDelivery, *, error: str) -> bool:
        """Satisfy the complete port; this worker test never receives a closed-workflow signal."""
        del delivery, error
        raise AssertionError("calendar repair worker must not enqueue a new closed-workflow repair")

    async def mark_repaired(self, record: CalendarRepairRecord) -> bool:
        self.acknowledged.append(record.repair_id)
        return True

    async def reschedule(self, record: CalendarRepairRecord, *, error: str) -> bool:
        self.retried.append((record.repair_id, error))
        return True

    async def organizer_change_applied(self, record: CalendarRepairRecord) -> bool:
        del record
        return self._organizer_change_applied

    async def has_live_repair_lease(self, record: CalendarRepairRecord) -> bool:
        """Keep ordinary repair fixtures authorized through their final effect fence."""
        del record
        return True


class _AckLossRepairQueue:
    """Two leased views of one repair whose first post-effect acknowledgement is lost."""

    def __init__(self, record: CalendarRepairRecord) -> None:
        self._first = record
        self._fresh = CalendarRepairRecord(
            repair_id=record.repair_id,
            change=record.change,
            tenant_id=record.tenant_id,
            workflow_id=record.workflow_id,
            attempt_count=record.attempt_count + 1,
            lease_token="fresh-repair-lease",
        )
        self._claim_count = 0
        self._first_ack_lost = False
        self._organizer_change_applied = False
        self.acknowledged: list[str] = []
        self.applied_checks: list[tuple[str, bool]] = []
        self.retried: list[tuple[int, str]] = []

    @property
    def fresh(self) -> CalendarRepairRecord:
        """Expose the replacement lease for the crash/reclaim assertion only."""
        return self._fresh

    def note_organizer_change_applied(self) -> None:
        """Model normal reconciliation committing before this queue caller loses its ACK."""
        self._organizer_change_applied = True

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[CalendarRepairRecord]:
        assert limit > 0
        assert lease_seconds > 0
        if self._claim_count == 0:
            self._claim_count += 1
            return [self._first]
        if self._claim_count == 1:
            assert self._first_ack_lost
            self._claim_count += 1
            return [self._fresh]
        return []

    async def enqueue(self, delivery: OrganizerChangeDelivery, *, error: str) -> bool:
        del delivery, error
        raise AssertionError("calendar repair worker must not enqueue a new closed-workflow repair")

    async def mark_repaired(self, record: CalendarRepairRecord) -> bool:
        self.acknowledged.append(record.lease_token)
        if record.lease_token == self._first.lease_token:
            self._first_ack_lost = True
            return False
        assert record == self._fresh
        return True

    async def reschedule(self, record: CalendarRepairRecord, *, error: str) -> bool:
        self.retried.append((record.repair_id, error))
        return True

    async def organizer_change_applied(self, record: CalendarRepairRecord) -> bool:
        assert record in (self._first, self._fresh)
        applied = self._organizer_change_applied
        self.applied_checks.append((record.lease_token, applied))
        return applied

    async def has_live_repair_lease(self, record: CalendarRepairRecord) -> bool:
        """The P36 post-effect replay owns a still-current lease on each pass."""
        assert record in (self._first, self._fresh)
        return True


class _EntryLeaseLossRepairQueue:
    """Two claims where the first repair loses authority before CalendarPort can run."""

    def __init__(self, record: CalendarRepairRecord) -> None:
        self._first = record
        self._fresh = CalendarRepairRecord(
            repair_id=record.repair_id,
            change=record.change,
            tenant_id=record.tenant_id,
            workflow_id=record.workflow_id,
            attempt_count=record.attempt_count + 1,
            lease_token="fresh-entry-lease",
        )
        self._claim_count = 0
        self.lease_checks: list[str] = []
        self.acknowledged: list[str] = []
        self.retried: list[tuple[int, str]] = []

    @property
    def fresh(self) -> CalendarRepairRecord:
        """Expose the fresh record for exact token/attempt assertions only."""
        return self._fresh

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[CalendarRepairRecord]:
        assert limit > 0
        assert lease_seconds > 0
        if self._claim_count == 0:
            self._claim_count += 1
            return [self._first]
        if self._claim_count == 1:
            self._claim_count += 1
            return [self._fresh]
        return []

    async def enqueue(self, delivery: OrganizerChangeDelivery, *, error: str) -> bool:
        del delivery, error
        raise AssertionError("calendar repair worker must not enqueue a new closed-workflow repair")

    async def mark_repaired(self, record: CalendarRepairRecord) -> bool:
        self.acknowledged.append(record.lease_token)
        return record == self._fresh

    async def reschedule(self, record: CalendarRepairRecord, *, error: str) -> bool:
        self.retried.append((record.repair_id, error))
        return True

    async def organizer_change_applied(self, record: CalendarRepairRecord) -> bool:
        assert record in (self._first, self._fresh)
        return False

    async def has_live_repair_lease(self, record: CalendarRepairRecord) -> bool:
        assert record in (self._first, self._fresh)
        self.lease_checks.append(record.lease_token)
        return record == self._fresh


class _Catalog:
    """Catalog seam retaining exactly one canonical event."""

    def __init__(self, event: CanonicalEvent | None) -> None:
        self._event = event

    async def get(self, canonical_event_id: UUID) -> CanonicalEvent | None:
        if self._event is None or self._event.canonical_event_id != canonical_event_id:
            return None
        return self._event


class _Reconciliation:
    """Record the direct safety-net invocation without emulating its separately tested internals."""

    def __init__(
        self,
        result: ReconcileResult | Exception,
        *,
        on_effect: Callable[[], None] | None = None,
    ) -> None:
        self._result = result
        self._on_effect = on_effect
        self.calls: list[tuple[UUID, CanonicalEvent, str, OrganizerChange, str]] = []

    async def reconcile_organizer_change(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        change: OrganizerChange,
        *,
        transition_id: str,
    ) -> ReconcileResult:
        self.calls.append((tenant_id, event, workflow_id, change, transition_id))
        if isinstance(self._result, Exception):
            raise self._result
        if (
            self._result.status in {ReconcileStatus.CANCELLED, ReconcileStatus.RECONCILED}
            and self._on_effect is not None
        ):
            self._on_effect()
        return self._result


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Repair fixture",
        start_at=datetime(2026, 8, 1, 18, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 20, tzinfo=UTC),
        price_status=PriceStatus.FREE,
    )


def _record(event: CanonicalEvent, tenant_id: UUID) -> CalendarRepairRecord:
    start_at = event.start_at + timedelta(days=1)
    end_at = start_at + timedelta(hours=2)
    change = DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            event.canonical_event_id, EventStatus.RESCHEDULED, start_at, end_at
        ),
        canonical_event_id=event.canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.RESCHEDULED,
        start_at=start_at,
        end_at=end_at,
        time_zone="America/Los_Angeles",
        title="Repair fixture moved",
        venue_name="Repair Hall",
    )
    return CalendarRepairRecord(
        repair_id=41,
        change=change,
        tenant_id=tenant_id,
        workflow_id=f"{tenant_id}:{event.canonical_event_id}",
        attempt_count=1,
        lease_token="repair-lease",
    )


async def test_closed_workflow_repair_reenters_the_guarded_reconcile_path_once() -> None:
    """An orphaned active lifecycle receives the normal idempotent reconciliation command (ADR-008)."""
    event = _event()
    tenant_id = uuid4()
    record = _record(event, tenant_id)
    queue = _RepairQueue([record])
    reconciliation = _Reconciliation(ReconcileResult(ReconcileStatus.RECONCILED))
    worker = ClosedWorkflowCalendarRepairWorker(
        queue,
        cast(CatalogRepository, _Catalog(event)),
        cast(LifecycleReconciliationService, reconciliation),
    )

    stats = await worker.repair_once()

    assert stats.claimed == 1
    assert stats.repaired == 1
    assert stats.acknowledged == 1
    assert stats.retried == 0
    assert queue.acknowledged == [41]
    assert reconciliation.calls[0][0:3] == (tenant_id, event, record.workflow_id)
    assert reconciliation.calls[0][3].fingerprint == record.change.fingerprint
    assert reconciliation.calls[0][4] == (
        f"calendar-repair:{record.workflow_id}:{record.change.fingerprint}"
    )


async def test_closed_workflow_repair_acknowledges_a_terminal_or_mismatched_lifecycle_without_writing() -> (
    None
):
    """An ignored normal reconcile means a user withdrawal/terminal transition already won safely."""
    event = _event()
    record = _record(event, uuid4())
    queue = _RepairQueue([record])
    reconciliation = _Reconciliation(ReconcileResult(ReconcileStatus.IGNORED))
    worker = ClosedWorkflowCalendarRepairWorker(
        queue,
        cast(CatalogRepository, _Catalog(event)),
        cast(LifecycleReconciliationService, reconciliation),
    )

    stats = await worker.repair_once()

    assert stats.repaired == 0
    assert stats.acknowledged == 1
    assert queue.acknowledged == [41]


async def test_lost_fanout_ack_after_a_normal_reschedule_skips_a_second_transition() -> None:
    """The durable organizer fingerprint prevents a repair from emitting another reconcile notification."""
    event = _event()
    record = _record(event, uuid4())
    queue = _RepairQueue([record], organizer_change_applied=True)
    reconciliation = _Reconciliation(ReconcileResult(ReconcileStatus.RECONCILED))
    worker = ClosedWorkflowCalendarRepairWorker(
        queue,
        cast(CatalogRepository, _Catalog(event)),
        cast(LifecycleReconciliationService, reconciliation),
    )

    stats = await worker.repair_once()

    assert stats.repaired == 0
    assert stats.acknowledged == 1
    assert reconciliation.calls == []


async def test_lost_post_effect_repair_ack_reclaims_once_without_repeating_reconciliation() -> None:
    """A fresh repair lease sees the committed organizer change and only acknowledges it (ADR-008)."""
    event = _event()
    record = _record(event, uuid4())
    queue = _AckLossRepairQueue(record)
    reconciliation = _Reconciliation(
        ReconcileResult(ReconcileStatus.RECONCILED),
        on_effect=queue.note_organizer_change_applied,
    )
    worker = ClosedWorkflowCalendarRepairWorker(
        queue,
        cast(CatalogRepository, _Catalog(event)),
        cast(LifecycleReconciliationService, reconciliation),
    )

    first = await worker.repair_once()
    recovered = await worker.repair_once()

    assert (first.claimed, first.repaired, first.acknowledged, first.retried) == (1, 1, 0, 0)
    assert (recovered.claimed, recovered.repaired, recovered.acknowledged, recovered.retried) == (
        1,
        0,
        1,
        0,
    )
    assert len(reconciliation.calls) == 1
    assert queue.fresh.attempt_count == record.attempt_count + 1
    assert queue.fresh.lease_token != record.lease_token
    assert queue.applied_checks == [
        (record.lease_token, False),
        (queue.fresh.lease_token, True),
    ]
    assert queue.acknowledged == [record.lease_token, "fresh-repair-lease"]
    assert queue.retried == []


async def test_stale_repair_lease_skips_reconciliation_until_a_fresh_claim() -> None:
    """A final stale-lease observation blocks CalendarPort work before the guarded reconcile (ADR-008)."""
    event = _event()
    record = _record(event, uuid4())
    queue = _EntryLeaseLossRepairQueue(record)
    reconciliation = _Reconciliation(ReconcileResult(ReconcileStatus.RECONCILED))
    worker = ClosedWorkflowCalendarRepairWorker(
        queue,
        cast(CatalogRepository, _Catalog(event)),
        cast(LifecycleReconciliationService, reconciliation),
    )

    first = await worker.repair_once()
    recovered = await worker.repair_once()

    assert (
        first.claimed,
        first.repaired,
        first.acknowledged,
        first.retried,
        first.lost_leases,
    ) == (
        1,
        0,
        0,
        0,
        1,
    )
    assert (
        recovered.claimed,
        recovered.repaired,
        recovered.acknowledged,
        recovered.retried,
        recovered.lost_leases,
    ) == (1, 1, 1, 0, 0)
    assert len(reconciliation.calls) == 1
    assert queue.lease_checks == [record.lease_token, queue.fresh.lease_token]
    assert queue.fresh.attempt_count == record.attempt_count + 1
    assert queue.fresh.lease_token != record.lease_token
    assert queue.acknowledged == [queue.fresh.lease_token]
    assert queue.retried == []


async def test_closed_workflow_repair_retries_a_transient_reconciliation_failure() -> None:
    """A repair crash remains leased/retryable rather than silently losing calendar truth (ADR-008)."""
    event = _event()
    record = _record(event, uuid4())
    queue = _RepairQueue([record])
    reconciliation = _Reconciliation(RuntimeError("calendar unavailable"))
    worker = ClosedWorkflowCalendarRepairWorker(
        queue,
        cast(CatalogRepository, _Catalog(event)),
        cast(LifecycleReconciliationService, reconciliation),
    )

    stats = await worker.repair_once()

    assert stats.repaired == 0
    assert stats.acknowledged == 0
    assert stats.retried == 1
    assert queue.retried == [(41, "calendar unavailable")]
