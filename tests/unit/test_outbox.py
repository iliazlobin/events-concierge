"""Unit tests for the ADR-009 outbox relay's retry and visible-deduplication boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from events_concierge.adapters.mock.notifier import MockNotifier
from events_concierge.application.outbox import OutboxRelay, RelayStats
from events_concierge.ports.notifications import Notification, NotificationKind
from events_concierge.ports.repositories import (
    NotificationClaim,
    OutboxQueueSnapshot,
    OutboxRecord,
)


class FakeOutbox:
    """In-memory repository seam exposing relay decisions without a database dependency."""

    def __init__(self, batches: list[list[OutboxRecord]]) -> None:
        self._batches = batches
        self.delivered: list[int] = []
        self.rescheduled: list[tuple[int, datetime | None, str, bool]] = []
        self.released: list[str] = []
        self.ledger_marked: list[str] = []
        self.ledger_result = True
        self.claim = NotificationClaim.ACQUIRED
        self.send_authorized = True

    async def queue_snapshot(self) -> OutboxQueueSnapshot:
        return OutboxQueueSnapshot(pending=0, ready=0, leased=0, oldest_ready_at=None)

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[OutboxRecord]:
        del limit, lease_seconds
        return self._batches.pop(0) if self._batches else []

    async def mark_delivered(self, record: OutboxRecord) -> bool:
        self.delivered.append(record.outbox_id)
        return True

    async def reschedule(
        self,
        record: OutboxRecord,
        *,
        retry_at: datetime | None,
        error: str,
        consume_attempt: bool,
    ) -> bool:
        self.rescheduled.append((record.outbox_id, retry_at, error, consume_attempt))
        return True

    async def claim_notification(
        self, record: OutboxRecord, *, dedup_key: str, lease_seconds: int
    ) -> NotificationClaim:
        del record, dedup_key, lease_seconds
        return self.claim

    async def has_notification_send_authority(
        self, record: OutboxRecord, *, dedup_key: str
    ) -> bool:
        del record, dedup_key
        return self.send_authorized

    async def mark_notification_delivered(self, record: OutboxRecord, *, dedup_key: str) -> bool:
        del record
        self.ledger_marked.append(dedup_key)
        return self.ledger_result

    async def release_notification(self, record: OutboxRecord, *, dedup_key: str) -> bool:
        del record
        self.released.append(dedup_key)
        return True


class RaiseAfterVisibleSend:
    """Crash seam: the external port makes one visible send then loses the activity acknowledgement."""

    def __init__(self, delegate: MockNotifier) -> None:
        self.delegate = delegate
        self.calls = 0

    async def send(self, notification: Notification) -> None:
        self.calls += 1
        await self.delegate.send(notification)
        if self.calls == 1:
            raise RuntimeError("simulated notifier acknowledgement loss")


class AlwaysFailNotifier:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, notification: Notification) -> None:
        del notification
        self.calls += 1
        raise RuntimeError("simulated permanent notifier failure")


def _record(*, attempt_count: int, lease_token: str) -> OutboxRecord:
    return OutboxRecord(
        outbox_id=17,
        tenant_id=uuid4(),
        topic="lifecycle.handoff",
        payload={
            "workflow_id": "tenant:event",
            "transition_id": "handoff:1",
            "event_summary": "Jazz at the Blue Note",
            "deep_link": "https://example.test/rsvp",
        },
        attempt_count=attempt_count,
        lease_token=lease_token,
    )


async def test_relay_retries_after_lost_notifier_ack_with_one_visible_notification() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    fake = FakeOutbox(
        [
            [_record(attempt_count=0, lease_token="first")],
            [_record(attempt_count=1, lease_token="second")],
        ]
    )
    delivered = MockNotifier()
    relay = OutboxRelay(fake, RaiseAfterVisibleSend(delivered), now=lambda: now)

    first = await relay.relay_once()
    second = await relay.relay_once()

    assert first.retried == 1
    assert first.sent == 0
    assert fake.rescheduled == [
        (17, now + timedelta(seconds=30), "simulated notifier acknowledgement loss", True)
    ]
    assert second.sent == 1
    assert fake.delivered == [17]
    assert len(delivered.sent) == 1
    assert delivered.sent[0].dedup_key == "tenant:event:handoff_available:handoff:1"


async def test_relay_renders_expired_handoff_once_across_redelivery() -> None:
    """An expired handoff has a stable lifecycle dedup key and deadline-specific copy (ADR-009)."""
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    tenant_id = uuid4()
    payload: dict[str, object] = {
        "workflow_id": "tenant:event",
        "transition_id": "handoff-expired:1",
        "event_summary": "Jazz at the Blue Note",
    }
    fake = FakeOutbox(
        [
            [
                OutboxRecord(
                    outbox_id=23,
                    tenant_id=tenant_id,
                    topic="lifecycle.expired",
                    payload=payload,
                    attempt_count=0,
                    lease_token="first",
                )
            ],
            [
                OutboxRecord(
                    outbox_id=23,
                    tenant_id=tenant_id,
                    topic="lifecycle.expired",
                    payload=payload,
                    attempt_count=1,
                    lease_token="redelivery",
                )
            ],
        ]
    )
    notifier = MockNotifier()
    relay = OutboxRelay(fake, RaiseAfterVisibleSend(notifier), now=lambda: now)

    first = await relay.relay_once()
    second = await relay.relay_once()

    assert first.retried == 1
    assert second.sent == 1
    assert fake.delivered == [23]
    assert len(notifier.sent) == 1
    notification = notifier.sent[0]
    assert notification.kind is NotificationKind.HANDOFF_EXPIRED
    assert notification.subject == "Action window expired for Jazz at the Blue Note"
    assert notification.body == "The registration follow-up was not completed before its deadline."
    assert notification.dedup_key == "tenant:event:handoff_expired:handoff-expired:1"


async def test_relay_renders_handoff_reminder_once_across_redelivery() -> None:
    """Reminder outbox retries retain their cadence-specific visible-deduplication key (B23)."""
    now = datetime(2026, 7, 17, 12, tzinfo=UTC)
    tenant_id = uuid4()
    payload: dict[str, object] = {
        "workflow_id": "tenant:event",
        "task_id": "tenant:event:handoff",
        "reminder_id": "tenant:event:t24h:1",
        "reminder_kind": "t24h",
        "event_summary": "Jazz at the Blue Note",
        "deep_link": "https://example.test/rsvp",
    }
    fake = FakeOutbox(
        [
            [
                OutboxRecord(
                    outbox_id=27,
                    tenant_id=tenant_id,
                    topic="handoff.reminder",
                    payload=payload,
                    attempt_count=0,
                    lease_token="first",
                )
            ],
            [
                OutboxRecord(
                    outbox_id=27,
                    tenant_id=tenant_id,
                    topic="handoff.reminder",
                    payload=payload,
                    attempt_count=1,
                    lease_token="redelivery",
                )
            ],
        ]
    )
    notifier = MockNotifier()
    relay = OutboxRelay(fake, RaiseAfterVisibleSend(notifier), now=lambda: now)

    first = await relay.relay_once()
    second = await relay.relay_once()

    assert first.retried == 1
    assert second.sent == 1
    assert fake.delivered == [27]
    assert len(notifier.sent) == 1
    notification = notifier.sent[0]
    assert notification.kind is NotificationKind.HANDOFF_REMINDER
    assert notification.subject == "Reminder: action needed for Jazz at the Blue Note"
    assert notification.body == (
        "Your registration follow-up is still waiting. Open: https://example.test/rsvp"
    )
    assert notification.dedup_key == "tenant:event:handoff_reminder:tenant:event:t24h:1"


async def test_relay_renders_request_no_result_once_across_redelivery() -> None:
    """A request-scoped empty-discovery terminal is a deduplicated NO_RESULT notification."""
    now = datetime(2026, 7, 17, 12, tzinfo=UTC)
    tenant_id = uuid4()
    payload: dict[str, object] = {
        "workflow_id": "req:tenant:request",
        "transition_id": "req:tenant:request:failed-no-candidate:1",
        "event_summary": "your request",
        "request_id": "request",
    }
    fake = FakeOutbox(
        [
            [
                OutboxRecord(
                    outbox_id=29,
                    tenant_id=tenant_id,
                    topic="request.failed_no_candidate",
                    payload=payload,
                    attempt_count=0,
                    lease_token="first",
                )
            ],
            [
                OutboxRecord(
                    outbox_id=29,
                    tenant_id=tenant_id,
                    topic="request.failed_no_candidate",
                    payload=payload,
                    attempt_count=1,
                    lease_token="redelivery",
                )
            ],
        ]
    )
    notifier = MockNotifier()
    relay = OutboxRelay(fake, RaiseAfterVisibleSend(notifier), now=lambda: now)

    first = await relay.relay_once()
    second = await relay.relay_once()

    assert first.retried == 1
    assert second.sent == 1
    assert fake.delivered == [29]
    assert len(notifier.sent) == 1
    notification = notifier.sent[0]
    assert notification.kind is NotificationKind.NO_RESULT
    assert notification.subject == "No matching event found"
    assert notification.body == "No candidate could be registered automatically."
    assert (
        notification.dedup_key
        == "req:tenant:request:no_result:req:tenant:request:failed-no-candidate:1"
    )


async def test_relay_acknowledges_suppressed_candidate_close_without_sending() -> None:
    """A parent fall-through persists its audit event without a false user-facing cancellation."""
    record = OutboxRecord(
        outbox_id=31,
        tenant_id=uuid4(),
        topic="lifecycle.cancelled",
        payload={
            "workflow_id": "tenant:event",
            "transition_id": "tenant:event:close:1",
            "event_summary": "Candidate being skipped",
            "candidate_close_reason": "parent_fallthrough",
            "notification_suppressed": True,
        },
        attempt_count=0,
        lease_token="candidate-close",
    )
    fake = FakeOutbox([[record]])
    notifier = MockNotifier()
    relay = OutboxRelay(fake, notifier)

    stats = await relay.relay_once()

    assert stats.acknowledged == 1
    assert stats.sent == 0
    assert fake.delivered == [31]
    assert fake.ledger_marked == []
    assert notifier.sent == []


async def test_relay_marks_fifth_delivery_failure_terminal() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    fake = FakeOutbox([[_record(attempt_count=4, lease_token="final")]])
    relay = OutboxRelay(fake, AlwaysFailNotifier(), now=lambda: now)

    stats = await relay.relay_once()

    assert stats.failed == 1
    assert fake.rescheduled == [(17, None, "simulated permanent notifier failure", True)]
    assert fake.released == ["tenant:event:handoff_available:handoff:1"]


async def test_relay_busy_deferrals_do_not_consume_the_delivery_failure_budget() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    fake = FakeOutbox(
        [[_record(attempt_count=0, lease_token=f"busy-{index}")] for index in range(5)]
        + [[_record(attempt_count=0, lease_token="send")]]
    )
    notifier = AlwaysFailNotifier()
    relay = OutboxRelay(fake, notifier, now=lambda: now)

    fake.claim = NotificationClaim.BUSY
    busy_stats = [await relay.relay_once() for _ in range(5)]
    fake.claim = NotificationClaim.ACQUIRED
    failure_stats = await relay.relay_once()

    assert all(stats.retried == 1 and stats.failed == 0 for stats in busy_stats)
    assert notifier.calls == 1
    assert failure_stats.retried == 1
    assert failure_stats.failed == 0
    assert fake.rescheduled == [
        (17, now + timedelta(seconds=2), "notification ledger is leased by another relay", False),
        (17, now + timedelta(seconds=2), "notification ledger is leased by another relay", False),
        (17, now + timedelta(seconds=2), "notification ledger is leased by another relay", False),
        (17, now + timedelta(seconds=2), "notification ledger is leased by another relay", False),
        (17, now + timedelta(seconds=2), "notification ledger is leased by another relay", False),
        (17, now + timedelta(seconds=30), "simulated permanent notifier failure", True),
    ]


async def test_relay_acknowledges_a_durable_delivered_ledger_without_sending() -> None:
    """A ledger-delivered redelivery acknowledges only the outbox row (FR-6.6, ADR-009)."""
    fake = FakeOutbox([[_record(attempt_count=4, lease_token="already-delivered")]])
    fake.claim = NotificationClaim.DELIVERED
    notifier = MockNotifier()
    relay = OutboxRelay(fake, notifier)

    stats = await relay.relay_once()

    assert stats.claimed == 1
    assert stats.acknowledged == 1
    assert stats.sent == 0
    assert stats.retried == 0
    assert stats.failed == 0
    assert notifier.sent == []
    assert fake.delivered == [17]
    assert fake.rescheduled == []
    assert fake.ledger_marked == []
    assert fake.released == []


async def test_relay_does_not_send_or_reschedule_after_losing_its_outbox_lease() -> None:
    """A stale relay record cannot turn into a late user-visible effect (ADR-009, NFR-8)."""
    fake = FakeOutbox([[_record(attempt_count=4, lease_token="stale")]])
    fake.claim = NotificationClaim.LEASE_LOST
    notifier = MockNotifier()
    relay = OutboxRelay(fake, notifier)

    stats = await relay.relay_once()

    assert stats.claimed == 1
    assert stats.acknowledged == 0
    assert stats.sent == 0
    assert stats.retried == 0
    assert stats.failed == 0
    assert notifier.sent == []
    assert fake.delivered == []
    assert fake.rescheduled == []
    assert fake.ledger_marked == []
    assert fake.released == []


async def test_relay_rechecks_send_authority_after_a_successful_ledger_claim() -> None:
    """A pause after a claim cannot reach the port after durable ownership has changed (ADR-009)."""
    fake = FakeOutbox([[_record(attempt_count=0, lease_token="paused")]])
    fake.send_authorized = False
    notifier = MockNotifier()
    relay = OutboxRelay(fake, notifier)

    stats = await relay.relay_once()

    assert stats == RelayStats(claimed=1)
    assert notifier.sent == []
    assert fake.delivered == []
    assert fake.rescheduled == []
    assert fake.ledger_marked == []
    assert fake.released == []


async def test_relay_lost_ledger_lease_defers_without_consuming_delivery_budget() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    fake = FakeOutbox([[_record(attempt_count=4, lease_token="lost-ledger")]])
    fake.ledger_result = False
    notifier = MockNotifier()
    relay = OutboxRelay(fake, notifier, now=lambda: now)

    stats = await relay.relay_once()

    assert stats.retried == 1
    assert stats.failed == 0
    assert fake.delivered == []
    assert len(notifier.sent) == 1
    assert fake.rescheduled == [
        (17, now + timedelta(seconds=2), "notification ledger lease was lost", False)
    ]
