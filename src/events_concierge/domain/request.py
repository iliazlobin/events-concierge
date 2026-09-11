"""EventRequest, its parsed constraints, and the ranked-candidate output (the personalized feed)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from .enums import ConflictVerdict, Lane, PriceStatus
from .events import CanonicalEvent, GeoPoint


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end


@dataclass(frozen=True, slots=True)
class GeoConstraint:
    center: GeoPoint
    radius_km: float


@dataclass(frozen=True, slots=True)
class RequestConstraints:
    """Parsed hard + soft constraints (FR-4.6).

    Discovery serves all price states by default. A user can explicitly request verified-free
    events, in which case unknown and paid listings are hard-filtered rather than guessed free.
    """

    time_window: TimeWindow | None = None
    geo: GeoConstraint | None = None
    categories: tuple[str, ...] = ()
    budget_free: bool = False
    hard_filters: tuple[str, ...] = ()

    def accepts_price(self, price_status: PriceStatus) -> bool:
        """Apply the explicit free-only hard filter without collapsing unknown into free (FR-4.6)."""
        return not self.budget_free or price_status is PriceStatus.FREE


@dataclass(slots=True)
class EventRequest:
    request_id: UUID
    tenant_id: UUID
    raw_text: str
    constraints: RequestConstraints
    intent_embedding: list[float] | None = None


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """A scored candidate in the feed, with its conflict verdict and the ordered lane plan the
    registration workflow will try (autonomous-on-SLA -> browser -> handoff, FR-5.0)."""

    canonical_event: CanonicalEvent
    score: float
    rationale: str
    conflict_verdict: ConflictVerdict
    lane_plan: tuple[Lane, ...]
    additional_dates: tuple[CanonicalEvent, ...] = ()
    discovery_state: str = "upcoming"
    source_freshness: str = "unknown"

    @property
    def registerable(self) -> bool:
        """A candidate the system may attempt (conflict gate did not hard-block it)."""
        return (
            self.conflict_verdict is not ConflictVerdict.BLOCKED
            and self.discovery_state == "upcoming"
            and self.canonical_event.event_status.value != "cancelled"
        )


@dataclass(frozen=True, slots=True)
class Feed:
    """A cursor-paginated page of ranked recommendations (the scroll surface, FR-4)."""

    request_id: UUID
    items: tuple[RankedCandidate, ...]
    next_cursor: str | None = None
    signals: dict[str, float] = field(default_factory=dict)
