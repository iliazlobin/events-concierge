"""Unit tests for the ADR-009 outbox relay's retry and visible-deduplication boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from events_concierge.adapters.mock.notification_secrets import (
    DevelopmentNotificationSecretProtector,
)
from events_concierge.adapters.mock.notifier import MockNotifier
from events_concierge.application.outbox import OutboxRelay, RelayStats
from events_concierge.ports.notifications import Notification, NotificationKind
from events_concierge.ports.repositories import (
    NotificationClaim,
    OutboxQueueSnapshot,
    OutboxRecord,
)

_SECRETS = DevelopmentNotificationSecretProtector()


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


class EchoingFailNotifier:
    """Adversarial provider seam whose exception repeats a bearer-bearing rendered body."""

    async def send(self, notification: Notification) -> None:
        raise RuntimeError(notification.body)


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
            "reason": "calendar_write_failed",
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
    relay = OutboxRelay(fake, RaiseAfterVisibleSend(delivered), _SECRETS, now=lambda: now)

    first = await relay.relay_once()
    second = await relay.relay_once()

    assert first.retried == 1
    assert first.sent == 0
    assert fake.rescheduled == [
        (
            17,
            now + timedelta(seconds=30),
            "notification materialization or delivery failed",
            True,
        )
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
    relay = OutboxRelay(fake, RaiseAfterVisibleSend(notifier), _SECRETS, now=lambda: now)

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
    relay = OutboxRelay(fake, RaiseAfterVisibleSend(notifier), _SECRETS, now=lambda: now)

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


async def test_relay_delivers_handoff_completion_review_instead_of_silently_acknowledging() -> None:
    """A consumed mark-done discrepancy remains visible through the durable notifier path."""
    tenant_id = uuid4()
    record = OutboxRecord(
        outbox_id=28,
        tenant_id=tenant_id,
        topic="handoff_completion_review_required",
        payload={
            "workflow_id": "tenant:event",
            "task_id": "tenant:event:handoff",
            "completion_id": "tenant:event:handoff-completion:1",
            "event_summary": "Jazz at the Blue Note",
            "deep_link": "https://example.test/rsvp",
        },
        attempt_count=0,
        lease_token="review",
    )
    fake = FakeOutbox([[record]])
    notifier = MockNotifier()

    stats = await OutboxRelay(fake, notifier, _SECRETS).relay_once()

    assert stats.sent == 1
    assert stats.acknowledged == 1
    assert fake.delivered == [28]
    assert len(notifier.sent) == 1
    notification = notifier.sent[0]
    assert notification.kind is NotificationKind.HANDOFF_REVIEW_REQUIRED
    assert notification.subject == "Registration needs review: Jazz at the Blue Note"
    assert "no calendar entry was added" in notification.body
    assert notification.dedup_key == (
        "tenant:event:handoff_review_required:tenant:event:handoff-completion:1"
    )


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
    relay = OutboxRelay(fake, RaiseAfterVisibleSend(notifier), _SECRETS, now=lambda: now)

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
    relay = OutboxRelay(fake, notifier, _SECRETS)

    stats = await relay.relay_once()

    assert stats.acknowledged == 1
    assert stats.sent == 0
    assert fake.delivered == [31]
    assert fake.ledger_marked == []
    assert notifier.sent == []


async def test_relay_marks_fifth_delivery_failure_terminal() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    fake = FakeOutbox([[_record(attempt_count=4, lease_token="final")]])
    relay = OutboxRelay(fake, AlwaysFailNotifier(), _SECRETS, now=lambda: now)

    stats = await relay.relay_once()

    assert stats.failed == 1
    assert fake.rescheduled == [(17, None, "notification materialization or delivery failed", True)]
    assert fake.released == ["tenant:event:handoff_available:handoff:1"]


async def test_relay_busy_deferrals_do_not_consume_the_delivery_failure_budget() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    fake = FakeOutbox(
        [[_record(attempt_count=0, lease_token=f"busy-{index}")] for index in range(5)]
        + [[_record(attempt_count=0, lease_token="send")]]
    )
    notifier = AlwaysFailNotifier()
    relay = OutboxRelay(fake, notifier, _SECRETS, now=lambda: now)

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
        (
            17,
            now + timedelta(seconds=30),
            "notification materialization or delivery failed",
            True,
        ),
    ]


async def test_relay_acknowledges_a_durable_delivered_ledger_without_sending() -> None:
    """A ledger-delivered redelivery acknowledges only the outbox row (FR-6.6, ADR-009)."""
    fake = FakeOutbox([[_record(attempt_count=4, lease_token="already-delivered")]])
    fake.claim = NotificationClaim.DELIVERED
    notifier = MockNotifier()
    relay = OutboxRelay(fake, notifier, _SECRETS)

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
    relay = OutboxRelay(fake, notifier, _SECRETS)

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
    relay = OutboxRelay(fake, notifier, _SECRETS)

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
    relay = OutboxRelay(fake, notifier, _SECRETS, now=lambda: now)

    stats = await relay.relay_once()

    assert stats.retried == 1
    assert stats.failed == 0
    assert fake.delivered == []
    assert len(notifier.sent) == 1
    assert fake.rescheduled == [
        (17, now + timedelta(seconds=2), "notification ledger lease was lost", False)
    ]


async def test_relay_reveals_protected_completion_link_only_for_visible_delivery() -> None:
    tenant_id = uuid4()
    completion_url = "http://localhost:8000/v1/tasks/local-capability/done"
    protected = await _SECRETS.protect_completion_url(tenant_id, completion_url)
    record = OutboxRecord(
        outbox_id=61,
        tenant_id=tenant_id,
        topic="lifecycle.handoff",
        payload={
            "workflow_id": "tenant:event",
            "transition_id": "handoff:protected",
            "event_summary": "Protected event",
            "deep_link": "https://example.test/register",
            "protected_completion_url": protected,
        },
        attempt_count=0,
        lease_token="protected",
    )
    fake = FakeOutbox([[record]])
    notifier = MockNotifier()

    stats = await OutboxRelay(fake, notifier, _SECRETS).relay_once()

    assert stats.sent == 1
    assert len(notifier.sent) == 1
    if f"Mark done: {completion_url}" not in notifier.sent[0].body:
        raise AssertionError("protected completion link was not rendered")
    assert protected not in notifier.sent[0].body


async def test_relay_quarantines_unknown_topic_instead_of_silent_ack() -> None:
    record = OutboxRecord(
        outbox_id=62,
        tenant_id=uuid4(),
        topic="future.unclassified",
        payload={"workflow_id": "tenant:event"},
        attempt_count=0,
        lease_token="unknown",
    )
    fake = FakeOutbox([[record]])

    stats = await OutboxRelay(fake, MockNotifier(), _SECRETS).relay_once()

    assert stats.failed == 1
    assert fake.delivered == []
    assert fake.rescheduled == [
        (
            62,
            None,
            "outbox topic has no notification or audit-only classification",
            False,
        )
    ]


async def test_notification_suppressed_cannot_silence_an_unknown_topic() -> None:
    record = OutboxRecord(
        outbox_id=66,
        tenant_id=uuid4(),
        topic="future.unclassified",
        payload={
            "notification_suppressed": True,
            "candidate_close_reason": "parent_fallthrough",
        },
        attempt_count=0,
        lease_token="unknown-suppressed",
    )
    fake = FakeOutbox([[record]])

    stats = await OutboxRelay(fake, MockNotifier(), _SECRETS).relay_once()

    assert stats.failed == 1
    assert fake.delivered == []
    assert fake.rescheduled[0][1:] == (
        None,
        "outbox topic has no notification or audit-only classification",
        False,
    )


async def test_completion_capable_handoff_without_protected_url_is_quarantined() -> None:
    record = OutboxRecord(
        outbox_id=67,
        tenant_id=uuid4(),
        topic="lifecycle.handoff",
        payload={
            "workflow_id": "tenant:event",
            "reason": "deferred_register",
        },
        attempt_count=0,
        lease_token="missing-protected-url",
    )
    fake = FakeOutbox([[record]])

    stats = await OutboxRelay(fake, MockNotifier(), _SECRETS).relay_once()

    assert stats.failed == 1
    assert fake.delivered == []
    assert fake.rescheduled[0][1:] == (
        None,
        "completion-capable handoff has no protected completion URL",
        False,
    )


async def test_relay_acknowledges_only_explicit_audit_topic_without_send() -> None:
    record = OutboxRecord(
        outbox_id=63,
        tenant_id=uuid4(),
        topic="lifecycle.registered",
        payload={"workflow_id": "tenant:event"},
        attempt_count=0,
        lease_token="audit",
    )
    fake = FakeOutbox([[record]])
    notifier = MockNotifier()

    stats = await OutboxRelay(fake, notifier, _SECRETS).relay_once()

    assert stats.acknowledged == 1
    assert fake.delivered == [63]
    assert notifier.sent == []


async def test_plaintext_capability_is_quarantined_even_when_notification_is_suppressed() -> None:
    record = OutboxRecord(
        outbox_id=64,
        tenant_id=uuid4(),
        topic="lifecycle.cancelled",
        payload={
            "notification_suppressed": True,
            "completion_url": "http://localhost.invalid/bearer",
        },
        attempt_count=0,
        lease_token="forbidden",
    )
    fake = FakeOutbox([[record]])

    stats = await OutboxRelay(fake, MockNotifier(), _SECRETS).relay_once()

    assert stats.failed == 1
    assert fake.delivered == []
    assert fake.rescheduled == [
        (
            64,
            None,
            "outbox projection contains a forbidden plaintext completion URL",
            False,
        )
    ]


async def test_notifier_exception_text_cannot_enter_persisted_outbox_error() -> None:
    tenant_id = uuid4()
    bearer_fragment = "do-not-persist-this-capability"
    completion_url = f"http://localhost:8000/v1/tasks/{bearer_fragment}/done"
    protected = await _SECRETS.protect_completion_url(tenant_id, completion_url)
    record = OutboxRecord(
        outbox_id=65,
        tenant_id=tenant_id,
        topic="lifecycle.handoff",
        payload={
            "workflow_id": "tenant:event",
            "transition_id": "handoff:provider-error",
            "protected_completion_url": protected,
        },
        attempt_count=0,
        lease_token="provider-error",
    )
    fake = FakeOutbox([[record]])

    stats = await OutboxRelay(fake, EchoingFailNotifier(), _SECRETS).relay_once()

    assert stats.retried == 1
    assert fake.rescheduled[0][2] == "notification materialization or delivery failed"
    assert bearer_fragment not in fake.rescheduled[0][2]
