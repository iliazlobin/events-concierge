"""At-least-once transactional-outbox relay with durable notification deduplication.

The relay is a projection: lifecycle transitions remain the source of truth, while leased outbox
rows are rendered to ``NotificationPort`` only after the transition transaction commits (FR-6.6,
FR-8.9, ADR-007/009). A provider call and database commit cannot be one atomic effect, so the
stable ledger key plus port idempotency makes crash redelivery at-most-once user-visible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..infra.logging import get_logger
from ..ports.notification_secrets import NotificationSecretProtector
from ..ports.notifications import Notification, NotificationKind, NotificationPort
from ..ports.repositories import NotificationClaim, OutboxRecord, OutboxRepository

_RETRY_DELAYS = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
)
_BUSY_RETRY_DELAY = timedelta(seconds=2)
_MAX_ATTEMPTS = 5
_AUDIT_ONLY_TOPICS = frozenset(
    {
        "lifecycle.found",
        "lifecycle.awaiting_confirmation",
        "lifecycle.registered",
        "lifecycle.withdrawing",
        "lifecycle.completed",
    }
)
_NON_CAPABILITY_HANDOFF_REASONS = frozenset(
    {
        "calendar_write_failed",
        "withdrawal_required",
    }
)

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RelayStats:
    """One poll's observable relay outcomes for worker metrics and focused tests."""

    claimed: int = 0
    acknowledged: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class _NotificationDraft:
    """Non-secret notification projection sufficient to acquire the durable send lease."""

    kind: NotificationKind
    event_summary: str
    deep_link: str | None
    protected_completion_url: str | None
    dedup_key: str


class OutboxRelay:
    """Deliver notification-worthy outbox rows with leased at-least-once semantics (ADR-009)."""

    def __init__(
        self,
        outbox: OutboxRepository,
        notifier: NotificationPort,
        secret_protector: NotificationSecretProtector,
        *,
        now: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._outbox = outbox
        self._notifier = notifier
        self._secret_protector = secret_protector
        self._now = now or (lambda: datetime.now(UTC))
        self._lease_seconds = lease_seconds

    async def relay_once(self, *, limit: int = 50) -> RelayStats:
        """Claim and process one bounded batch; the notifier worker supplies the idle poll loop."""
        if limit < 1:
            raise ValueError("limit must be positive")
        records = await self._outbox.claim_batch(limit, self._lease_seconds)
        stats = RelayStats(claimed=len(records))
        for record in records:
            stats = await self._relay_record(record, stats)
        return stats

    async def _relay_record(self, record: OutboxRecord, stats: RelayStats) -> RelayStats:
        prepared = await self._prepare_notification(record, stats)
        if isinstance(prepared, RelayStats):
            return prepared
        return await self._relay_notification(record, prepared, stats)

    async def _prepare_notification(
        self,
        record: OutboxRecord,
        stats: RelayStats,
    ) -> _NotificationDraft | RelayStats:
        """Classify one projection before acquiring any notification-ledger state."""
        if "completion_url" in record.payload:
            return await self._quarantine_projection(
                record,
                stats,
                "outbox projection contains a forbidden plaintext completion URL",
            )
        if _is_silent_audit(record):
            acknowledged = await self._outbox.mark_delivered(record)
            return _with(stats, acknowledged=stats.acknowledged + int(acknowledged))
        kind = _notification_kind(record.topic)
        if kind is None:
            return await self._quarantine_projection(
                record,
                stats,
                "outbox topic has no notification or audit-only classification",
            )
        return _notification_draft(record, kind)

    async def _relay_notification(
        self,
        record: OutboxRecord,
        draft: _NotificationDraft,
        stats: RelayStats,
    ) -> RelayStats:
        """Acquire authority, reveal any secret just in time, and settle one visible send."""
        claim_outcome = await self._claim_notification_authority(record, draft, stats)
        if claim_outcome is not None:
            return claim_outcome
        try:
            notification = await self._materialize_notification(record, draft)
            await self._notifier.send(notification)
        except Exception:
            await self._outbox.release_notification(record, dedup_key=draft.dedup_key)
            # Provider/protector exceptions are untrusted and may echo the URL or bearer. Keep
            # persisted relay errors and structured logs independent of exception text.
            return await self._schedule_failure(
                record,
                stats,
                "notification materialization or delivery failed",
            )

        ledger_recorded = await self._outbox.mark_notification_delivered(
            record, dedup_key=draft.dedup_key
        )
        if not ledger_recorded:
            # A send may already be visible. Do not acknowledge the outbox: lease expiry will retry
            # with the same key, which NotificationPort must deduplicate (FR-6.6, AC-46).
            return await self._reschedule_contention(
                record, stats, "notification ledger lease was lost"
            )
        acknowledged = await self._outbox.mark_delivered(record)
        return _with(
            stats,
            acknowledged=stats.acknowledged + int(acknowledged),
            sent=stats.sent + 1,
        )

    async def _claim_notification_authority(
        self,
        record: OutboxRecord,
        draft: _NotificationDraft,
        stats: RelayStats,
    ) -> RelayStats | None:
        """Settle non-send claim outcomes and return ``None`` only for an authorized send."""
        claim = await self._outbox.claim_notification(
            record,
            dedup_key=draft.dedup_key,
            lease_seconds=self._lease_seconds,
        )
        if claim is NotificationClaim.DELIVERED:
            acknowledged = await self._outbox.mark_delivered(record)
            return _with(stats, acknowledged=stats.acknowledged + int(acknowledged))
        if claim is NotificationClaim.BUSY:
            return await self._reschedule_contention(
                record,
                stats,
                "notification ledger is leased by another relay",
            )
        if claim is NotificationClaim.LEASE_LOST:
            # A delayed claimant must never send after another worker reclaimed or terminalized
            # its outbox row. This also closes the interval after a scheduler pause. The next valid
            # claimant owns recovery; NotificationPort's dedup key remains the final external-effect
            # fence for a crash or pause after this local check (ADR-009, NFR-8).
            return stats
        if _requires_protected_completion_url(record) and draft.protected_completion_url is None:
            await self._outbox.release_notification(record, dedup_key=draft.dedup_key)
            return await self._quarantine_projection(
                record,
                stats,
                "completion-capable handoff has no protected completion URL",
            )
        if not await self._outbox.has_notification_send_authority(
            record, dedup_key=draft.dedup_key
        ):
            return stats
        return None

    async def _materialize_notification(
        self,
        record: OutboxRecord,
        draft: _NotificationDraft,
    ) -> Notification:
        """Reveal a capability only after durable authority, immediately before port delivery."""
        completion_url = (
            await self._secret_protector.reveal_completion_url(
                record.tenant_id,
                draft.protected_completion_url,
            )
            if draft.protected_completion_url is not None
            else None
        )
        subject, body = _render_notification(
            draft.kind,
            draft.event_summary,
            draft.deep_link,
            completion_url,
        )
        return Notification(
            tenant_id=record.tenant_id,
            kind=draft.kind,
            subject=subject,
            body=body,
            dedup_key=draft.dedup_key,
            deep_link=draft.deep_link,
        )

    async def _quarantine_projection(
        self,
        record: OutboxRecord,
        stats: RelayStats,
        error: str,
    ) -> RelayStats:
        """Terminalize an unclassified or insecure row instead of silently dropping it."""
        quarantined = await self._outbox.reschedule(
            record,
            retry_at=None,
            error=error,
            consume_attempt=False,
        )
        if quarantined:
            _log.error(
                "outbox projection quarantined",
                outbox_id=record.outbox_id,
                error=error,
            )
        return _with(stats, failed=stats.failed + int(quarantined))

    async def _schedule_failure(
        self, record: OutboxRecord, stats: RelayStats, error: str
    ) -> RelayStats:
        """Consume one retry only after a known NotificationPort send failure (ADR-009)."""
        message = error[:1000]
        next_attempt = record.attempt_count + 1
        if next_attempt >= _MAX_ATTEMPTS:
            await self._outbox.reschedule(
                record,
                retry_at=None,
                error=message,
                consume_attempt=True,
            )
            _log.error(
                "outbox notification failed permanently",
                outbox_id=record.outbox_id,
                attempts=next_attempt,
                error=message,
            )
            return _with(stats, failed=stats.failed + 1)
        delay = _RETRY_DELAYS[next_attempt - 1]
        await self._outbox.reschedule(
            record,
            retry_at=self._now() + delay,
            error=message,
            consume_attempt=True,
        )
        _log.warning(
            "outbox notification delivery will retry",
            outbox_id=record.outbox_id,
            attempts=next_attempt,
            retry_in_seconds=int(delay.total_seconds()),
            error=message,
        )
        return _with(stats, retried=stats.retried + 1)

    async def _reschedule_contention(
        self, record: OutboxRecord, stats: RelayStats, error: str
    ) -> RelayStats:
        """Defer a ledger-contention or lease-loss retry without spending the send budget (ADR-009)."""
        await self._outbox.reschedule(
            record,
            retry_at=self._now() + _BUSY_RETRY_DELAY,
            error=error,
            consume_attempt=False,
        )
        return _with(stats, retried=stats.retried + 1)


def _notification_kind(topic: str) -> NotificationKind | None:
    """Map lifecycle/outbox projection topics to the stable user-facing port vocabulary."""
    return {
        "lifecycle.handoff": NotificationKind.HANDOFF_AVAILABLE,
        "calendar_recovery_required": NotificationKind.HANDOFF_AVAILABLE,
        "withdrawal_handoff_required": NotificationKind.HANDOFF_AVAILABLE,
        "handoff.reminder": NotificationKind.HANDOFF_REMINDER,
        "handoff_completion_review_required": NotificationKind.HANDOFF_REVIEW_REQUIRED,
        "lifecycle.expired": NotificationKind.HANDOFF_EXPIRED,
        "lifecycle.scheduled": NotificationKind.COMPLETION,
        "lifecycle.reconciled": NotificationKind.RECONCILE,
        "lifecycle.cancelled": NotificationKind.RECONCILE,
        "lifecycle.failed_no_candidate": NotificationKind.NO_RESULT,
        "request.failed_no_candidate": NotificationKind.NO_RESULT,
    }.get(topic)


def _is_silent_audit(record: OutboxRecord) -> bool:
    """Recognize only intentional non-notification rows; unknown topics are quarantined."""
    if (
        record.payload.get("notification_suppressed") is True
        and record.topic in {"lifecycle.cancelled", "lifecycle.failed_no_candidate"}
        and record.payload.get("candidate_close_reason") == "parent_fallthrough"
    ):
        # A parent fall-through still needs its atomic lifecycle/ledger/outbox audit record,
        # but it must not tell the user that a candidate was cancelled while the same request
        # is actively trying the next one (FR-5.0/6.6, ADR-003/007).
        return True
    return record.topic in _AUDIT_ONLY_TOPICS


def _requires_protected_completion_url(record: OutboxRecord) -> bool:
    """Identify user-registration handoffs whose notification must carry a mark-done action."""
    return (
        record.topic == "lifecycle.handoff"
        and record.payload.get("reason") not in _NON_CAPABILITY_HANDOFF_REASONS
    )


def _notification_draft(record: OutboxRecord, kind: NotificationKind) -> _NotificationDraft:
    """Build only non-secret send identity before the notifier owns delivery authority."""
    payload = record.payload
    workflow_id = str(payload.get("workflow_id") or f"outbox-{record.outbox_id}")
    transition_id = str(
        payload.get("transition_id")
        or payload.get("completion_id")
        or f"calendar-recovery:{payload.get('task_id', record.outbox_id)}"
    )
    deep_link_value = payload.get("deep_link")
    protected_value = payload.get("protected_completion_url")
    return _NotificationDraft(
        kind=kind,
        event_summary=str(payload.get("event_summary") or "your event"),
        deep_link=str(deep_link_value) if deep_link_value else None,
        protected_completion_url=(
            protected_value if isinstance(protected_value, str) and protected_value else None
        ),
        dedup_key=(
            f"{workflow_id}:handoff_reminder:{payload.get('reminder_id', transition_id)}"
            if kind is NotificationKind.HANDOFF_REMINDER
            else f"{workflow_id}:{kind.value}:{transition_id}"
        ),
    )


def _render_notification(
    kind: NotificationKind,
    event_summary: str,
    deep_link: str | None,
    completion_url: str | None,
) -> tuple[str, str]:
    """Keep rendering deterministic and channel-neutral; a real adapter owns transport formatting."""
    if kind is NotificationKind.HANDOFF_AVAILABLE:
        suffix = f" Open: {deep_link}" if deep_link else ""
        completion = f" Mark done: {completion_url}" if completion_url else ""
        rendered = (
            f"Action needed for {event_summary}",
            f"A registration follow-up is ready.{suffix}{completion}",
        )
    elif kind is NotificationKind.HANDOFF_REMINDER:
        suffix = f" Open: {deep_link}" if deep_link else ""
        rendered = (
            f"Reminder: action needed for {event_summary}",
            f"Your registration follow-up is still waiting.{suffix}",
        )
    elif kind is NotificationKind.HANDOFF_REVIEW_REQUIRED:
        rendered = (
            f"Registration needs review: {event_summary}",
            (
                "We could not independently verify the registration, so no calendar entry was "
                "added. The handoff remains open for review."
            ),
        )
    elif kind is NotificationKind.HANDOFF_EXPIRED:
        rendered = (
            f"Action window expired for {event_summary}",
            "The registration follow-up was not completed before its deadline.",
        )
    elif kind is NotificationKind.COMPLETION:
        rendered = (
            f"Added to your calendar: {event_summary}",
            "Your registration is confirmed and scheduled.",
        )
    elif kind is NotificationKind.RECONCILE:
        rendered = (f"Event update: {event_summary}", "Your concierge event needs review.")
    else:
        rendered = ("No matching event found", "No candidate could be registered automatically.")
    return rendered


def _with(stats: RelayStats, **changes: int) -> RelayStats:
    """Return updated immutable stats without exposing a mutable counter to the worker loop."""
    return RelayStats(
        claimed=changes.get("claimed", stats.claimed),
        acknowledged=changes.get("acknowledged", stats.acknowledged),
        sent=changes.get("sent", stats.sent),
        retried=changes.get("retried", stats.retried),
        failed=changes.get("failed", stats.failed),
    )
