"""NotificationPort: outbound user-facing delivery over the launch channel (email to the real
address at launch; SMS is a stubbed adapter behind the same port, FR-6.6). At-least-once + dedup."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class NotificationKind(StrEnum):
    HANDOFF_AVAILABLE = "handoff_available"
    HANDOFF_REMINDER = "handoff_reminder"
    HANDOFF_REVIEW_REQUIRED = "handoff_review_required"
    HANDOFF_EXPIRED = "handoff_expired"
    COMPLETION = "completion"
    RECONCILE = "reconcile"
    NO_RESULT = "no_result"
    ATTENDANCE_NUDGE = "attendance_nudge"
    DIGEST = "digest"


@dataclass(frozen=True, slots=True)
class Notification:
    tenant_id: UUID
    kind: NotificationKind
    subject: str
    body: str
    dedup_key: str
    deep_link: str | None = None


class NotificationPort(Protocol):
    async def send(self, notification: Notification) -> None:
        """Deliver through a channel that receives a stable application deduplication key.

        The durable worker is at-least-once. A channel may use ``dedup_key`` for true provider-side
        suppression only when its contract supports that primitive. SES does not, so a crash after
        send but before the ledger acknowledgement remains an explicit availability-versus-
        duplicate launch decision rather than an at-most-once promise.
        """
        ...
