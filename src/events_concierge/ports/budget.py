"""Durable Ticketmaster pre-dispatch budget port (ADR-002)."""

from __future__ import annotations

from typing import Protocol

from ..domain.budget import (
    BudgetUsage,
    TicketmasterAuthorization,
    TicketmasterBudgetClass,
    TicketmasterDispatchOutcome,
)


class TicketmasterBudgetLedger(Protocol):
    """Authorize exactly one physical Ticketmaster request before it reaches the wire.

    This deliberately is not a generic counter.  Only Ticketmaster uses the one shared app-key
    daily ceiling, and its ``dispatch_key`` must represent one physical request attempt.
    """

    async def authorize(
        self,
        quota_scope: str,
        dispatch_key: str,
        request_fingerprint: str,
        budget_class: TicketmasterBudgetClass,
        *,
        cost: int,
    ) -> TicketmasterAuthorization:
        """Atomically charge once or reject; duplicate keys never permit a second request."""
        ...

    async def record_outcome(
        self,
        quota_scope: str,
        dispatch_key: str,
        outcome: TicketmasterDispatchOutcome,
    ) -> bool:
        """Record a terminal wire result without ever refunding its authorization."""
        ...

    async def usage(self, quota_scope: str) -> BudgetUsage:
        """Read the PostgreSQL UTC-day budget projection without mutating it."""
        ...
