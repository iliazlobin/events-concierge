"""Pure durable-budget vocabulary for Ticketmaster discovery dispatches (ADR-002)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from .enums import Source


class TicketmasterBudgetClass(StrEnum):
    """The ratified 4,500 crawl / 500 reconciliation-reserve partition."""

    CRAWL = "crawl"
    RESERVE = "reserve"


class TicketmasterAuthorizationStatus(StrEnum):
    """One physical dispatch attempt's irreversible authorization result."""

    GRANTED = "granted"
    ALREADY_AUTHORIZED = "already_authorized"
    EXHAUSTED = "exhausted"


class TicketmasterDispatchOutcome(StrEnum):
    """Audit-only terminal result; neither outcome refunds a daily authorization."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TicketmasterBudgetLimits:
    """ADR-002's fixed partition, retained as a typed documentation/test invariant."""

    hard_limit: int = 5_000
    crawl_limit: int = 4_500
    reserve_limit: int = 500

    def __post_init__(self) -> None:
        if min(self.hard_limit, self.crawl_limit, self.reserve_limit) < 1:
            raise ValueError("Ticketmaster budget limits must be positive")
        if self.crawl_limit + self.reserve_limit != self.hard_limit:
            raise ValueError("Ticketmaster crawl and reserve limits must partition the hard limit")


@dataclass(frozen=True, slots=True)
class TicketmasterAuthorization:
    """A database-authorized physical request attempt, never a reusable source-call lease.

    ``dispatch_key`` identifies precisely one physical HTTP attempt.  A duplicate key records an
    ambiguous/lost acknowledgement and must *not* issue another request; a later physical retry
    gets a new deterministic attempt key and is charged again (ADR-002).
    """

    status: TicketmasterAuthorizationStatus
    quota_scope: str
    budget_day: date
    dispatch_key: str
    request_fingerprint: str
    budget_class: TicketmasterBudgetClass
    cost: int

    def permits_dispatch_at(self, at: datetime) -> bool:
        """Allow source-call initiation only on the database-authorized UTC day.

        The ledger defines a Ticketmaster ``day`` at authorization/source-initiation time. A future
        adapter must call this in its immediate pre-wire frame, never retain a permit across UTC
        midnight, and mint a fresh attempt key if it deliberately retries (ADR-002).
        """
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Ticketmaster dispatch time must be timezone-aware")
        return (
            self.status is TicketmasterAuthorizationStatus.GRANTED
            and at.astimezone(UTC).date() == self.budget_day
        )


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """A source/day/scope projection for scheduler metrics and deterministic tests."""

    source: Source
    quota_scope: str
    budget_day: date
    crawl_used: int
    reserve_used: int

    @property
    def total_used(self) -> int:
        return self.crawl_used + self.reserve_used
