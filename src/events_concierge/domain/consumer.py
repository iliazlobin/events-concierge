"""Consumer-facing, tenant-scoped read models.

These projections deliberately omit workflow identifiers, completion capabilities, audit payloads,
and provider credentials. They give the product UI enough durable truth to explain recent asks,
upcoming registrations, and handoffs without making the HTTP layer query Temporal or reconstruct
state from notification text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .enums import (
    EventStatus,
    HandoffReason,
    HandoffState,
    Lane,
    LifecycleState,
    PriceStatus,
    Source,
)


@dataclass(frozen=True, slots=True)
class ConsumerIdentity:
    """The non-sensitive account facts the signed-in consumer may see."""

    tenant_id: UUID
    notify_email: str
    preference_revision: int = 0
    interests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsumerRequestOutcome:
    """The selected lifecycle truth for one durable request, when explicitly linked."""

    canonical_event_id: UUID
    title: str
    start_at: datetime
    state: LifecycleState
    lane: Lane | None
    source: Source | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConsumerRequestSummary:
    """One recent ask plus its explicitly linked selected outcome, if one exists."""

    request_id: UUID
    text: str
    state: str
    created_at: datetime
    categories: tuple[str, ...] = ()
    budget_free: bool = False
    window_start: datetime | None = None
    window_end: datetime | None = None
    outcome: ConsumerRequestOutcome | None = None


@dataclass(frozen=True, slots=True)
class ConsumerRegistrationSummary:
    """An event lifecycle projected into consumer language without internal control identities."""

    canonical_event_id: UUID
    title: str
    start_at: datetime
    end_at: datetime | None
    venue_name: str | None
    city: str | None
    description: str
    price_status: PriceStatus
    event_status: EventStatus
    state: LifecycleState
    lane: Lane | None
    source: Source | None
    conflict_warning: bool
    registration_url: str | None
    updated_at: datetime

    @property
    def can_withdraw(self) -> bool:
        """Only stable post-booking states accept the current withdrawal API command."""
        return self.state in {LifecycleState.SCHEDULED, LifecycleState.RECONCILED}


@dataclass(frozen=True, slots=True)
class ConsumerTaskSummary:
    """A handoff the authenticated user can act on, without its completion capability."""

    task_id: str
    canonical_event_id: UUID
    event_summary: str
    title: str
    start_at: datetime
    venue_name: str | None
    city: str | None
    reason: HandoffReason
    state: HandoffState
    deep_link: str
    expires_at: datetime
    created_at: datetime
