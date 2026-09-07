"""Offline ADR-008 lifecycle-watch projection recovery tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from events_concierge.application.watch_projection import (
    LifecycleWatchProjectionWorker,
    WatchProjectionStats,
)
from events_concierge.domain.enums import Source
from events_concierge.ports.change_detection import LifecycleWatch, WatchedEvent
from events_concierge.ports.watch_projection import (
    WatchProjectionAction,
    WatchProjectionRecord,
)


class _ProjectionOutbox:
    """Leased outbox double that can lose its final authority or one acknowledgement."""

    def __init__(
        self,
        record: WatchProjectionRecord,
        *,
        lose_delivery_ack_once: bool = False,
        lose_live_lease_once: bool = False,
        lose_reschedule_lease_once: bool = False,
    ) -> None:
        self._template = record
        self._lose_delivery_ack_once = lose_delivery_ack_once
        self._lose_live_lease_once = lose_live_lease_once
        self._lose_reschedule_lease_once = lose_reschedule_lease_once
        self._ready = True
        self._leased: WatchProjectionRecord | None = None
        self._claim_count = 0
        self.claimed: list[WatchProjectionRecord] = []
        self.marked: list[WatchProjectionRecord] = []
        self.rescheduled: list[tuple[WatchProjectionRecord, str]] = []

    @property
    def pending(self) -> bool:
        """Expose whether the one fixture record remains recoverable."""
        return self._ready

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[WatchProjectionRecord]:
        """Return each recovery lease with a fresh opaque token and attempt count."""
        assert limit > 0
        assert lease_seconds > 0
        if not self._ready or self._leased is not None:
            return []
        self._claim_count += 1
        record = replace(
            self._template,
            attempt_count=self._template.attempt_count + self._claim_count,
            lease_token=f"projection-lease-{self._claim_count}",
        )
        self._leased = record
        self.claimed.append(record)
        return [record]

    async def has_live_lease(self, record: WatchProjectionRecord) -> bool:
        """Model a stale/reclaimed lease observed immediately before registry mutation."""
        if self._leased != record:
            return False
        if self._lose_live_lease_once:
            self._lose_live_lease_once = False
            self._leased = None
            return False
        return True

    async def mark_delivered(self, record: WatchProjectionRecord) -> bool:
        """Model a lost acknowledgement by retaining the durable record for a later lease."""
        if self._leased != record:
            return False
        self.marked.append(record)
        self._leased = None
        if self._lose_delivery_ack_once:
            self._lose_delivery_ack_once = False
            return False
        self._ready = False
        return True

    async def reschedule(self, record: WatchProjectionRecord, *, error: str) -> bool:
        """Release only the exact held lease while retaining the fixture record."""
        if self._leased != record:
            return False
        if self._lose_reschedule_lease_once:
            self._lose_reschedule_lease_once = False
            self._leased = None
            return False
        self.rescheduled.append((record, error))
        self._leased = None
        return True


class _WatchRegistry:
    """Idempotent registry double with an optional pre-mutation failure seam."""

    def __init__(self, *, register_error: Exception | None = None) -> None:
        self._register_error = register_error
        self.register_calls: list[LifecycleWatch] = []
        self.unregister_calls: list[LifecycleWatch] = []
        self.subscriptions: set[LifecycleWatch] = set()
        self.register_mutations = 0

    async def register(self, watch: LifecycleWatch) -> bool:
        """Insert one opaque subscription once, or fail before it mutates state."""
        self.register_calls.append(watch)
        if self._register_error is not None:
            raise self._register_error
        if watch in self.subscriptions:
            return False
        self.subscriptions.add(watch)
        self.register_mutations += 1
        return True

    async def unregister(self, watch: LifecycleWatch) -> bool:
        """Satisfy the complete registry protocol for this register-focused test double."""
        self.unregister_calls.append(watch)
        if watch not in self.subscriptions:
            return False
        self.subscriptions.remove(watch)
        return True

    async def list_watches(self) -> list[WatchedEvent]:
        """The worker never reads registry rows; retain a complete protocol surface."""
        return []


def _record() -> WatchProjectionRecord:
    """Build one deterministic-shape registration projection ready for its first lease."""
    tenant_id = uuid4()
    canonical_event_id = uuid4()
    return WatchProjectionRecord(
        projection_id=41,
        tenant_id=tenant_id,
        workflow_id=f"{tenant_id}:{canonical_event_id}",
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        action=WatchProjectionAction.REGISTER,
        active_since=datetime(2026, 7, 18, 12, tzinfo=UTC),
        attempt_count=0,
        lease_token="unleased-fixture",
    )


def _watch(record: WatchProjectionRecord) -> LifecycleWatch:
    """Project exactly the record fields used by the central watch registry."""
    return LifecycleWatch(
        tenant_id=record.tenant_id,
        workflow_id=record.workflow_id,
        canonical_event_id=record.canonical_event_id,
        source=record.source,
        active_since=record.active_since,
    )


async def test_projection_ack_loss_replays_idempotently_then_acknowledges() -> None:
    """A post-register ACK loss cannot duplicate a watch and converges on the recovered lease (ADR-008)."""
    record = _record()
    outbox = _ProjectionOutbox(record, lose_delivery_ack_once=True)
    watches = _WatchRegistry()
    delivery = LifecycleWatchProjectionWorker(outbox, watches, lease_seconds=45)

    first = await delivery.run_once()
    recovered = await delivery.run_once()

    expected_watch = _watch(record)
    assert first == WatchProjectionStats(claimed=1, applied=1)
    assert recovered == WatchProjectionStats(claimed=1, acknowledged=1)
    assert watches.register_calls == [expected_watch, expected_watch]
    assert watches.register_mutations == 1
    assert watches.subscriptions == {expected_watch}
    assert [claimed.attempt_count for claimed in outbox.claimed] == [1, 2]
    assert [claimed.lease_token for claimed in outbox.claimed] == [
        "projection-lease-1",
        "projection-lease-2",
    ]
    assert outbox.marked == outbox.claimed
    assert outbox.rescheduled == []
    assert outbox.pending is False


async def test_projection_skips_a_stale_pre_registry_lease_until_a_fresh_claim() -> None:
    """A projection observed stale at its final fence makes no watch-registry effect (NFR-8)."""
    record = _record()
    outbox = _ProjectionOutbox(record, lose_live_lease_once=True)
    watches = _WatchRegistry()
    delivery = LifecycleWatchProjectionWorker(outbox, watches, lease_seconds=45)

    first = await delivery.run_once()

    assert first == WatchProjectionStats(claimed=1, lost_leases=1)
    assert watches.register_calls == []
    assert watches.unregister_calls == []
    assert watches.subscriptions == set()
    assert outbox.marked == []
    assert outbox.rescheduled == []
    assert outbox.pending is True

    recovered = await delivery.run_once()

    assert recovered == WatchProjectionStats(claimed=1, applied=1, acknowledged=1)
    assert watches.register_calls == [_watch(record)]
    assert watches.register_mutations == 1
    assert watches.subscriptions == {_watch(record)}
    assert [claimed.attempt_count for claimed in outbox.claimed] == [1, 2]
    assert [claimed.lease_token for claimed in outbox.claimed] == [
        "projection-lease-1",
        "projection-lease-2",
    ]
    assert outbox.marked == [outbox.claimed[1]]
    assert outbox.pending is False


async def test_projection_registry_error_reschedules_the_exact_lease_without_a_watch_mutation() -> (
    None
):
    """A registry outage keeps its exact projection recoverable and makes no subscription change (ADR-008)."""
    record = _record()
    outbox = _ProjectionOutbox(record)
    watches = _WatchRegistry(register_error=RuntimeError("registry unavailable"))
    delivery = LifecycleWatchProjectionWorker(outbox, watches, lease_seconds=45)

    result = await delivery.run_once()

    [claimed] = outbox.claimed
    assert result == WatchProjectionStats(claimed=1, retried=1)
    assert watches.register_calls == [_watch(record)]
    assert watches.register_mutations == 0
    assert watches.subscriptions == set()
    assert outbox.marked == []
    assert outbox.rescheduled == [(claimed, "registry unavailable")]
    assert outbox.pending is True


async def test_projection_error_with_a_lost_retry_lease_is_not_reported_as_a_retry() -> None:
    """A failed terminal retry after registry failure leaves the projection for a fresh owner."""
    record = _record()
    outbox = _ProjectionOutbox(record, lose_reschedule_lease_once=True)
    watches = _WatchRegistry(register_error=RuntimeError("registry unavailable"))
    delivery = LifecycleWatchProjectionWorker(outbox, watches, lease_seconds=45)

    result = await delivery.run_once()

    assert result == WatchProjectionStats(claimed=1, lost_leases=1)
    assert watches.register_calls == [_watch(record)]
    assert watches.register_mutations == 0
    assert watches.subscriptions == set()
    assert outbox.marked == []
    assert outbox.rescheduled == []
    assert outbox.pending is True
