"""Ticketmaster's Pacer-then-one-time-ledger dispatch gate (FR-3.4, ADR-002/005)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ..domain.budget import (
    TicketmasterAuthorization,
    TicketmasterAuthorizationStatus,
    TicketmasterBudgetClass,
    TicketmasterDispatchOutcome,
)
from ..domain.enums import Source
from ..ports.budget import TicketmasterBudgetLedger
from ..ports.policy import Pacer, PacerLease, PacerOperation, PacerRequest


class TicketmasterDispatchStatus(StrEnum):
    """Whether a future adapter may make exactly one immediate Ticketmaster request."""

    GRANTED = "granted"
    PACED = "paced"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ALREADY_AUTHORIZED = "already_authorized"


@dataclass(frozen=True, slots=True)
class TicketmasterDispatchDecision:
    """The result of the only permitted pre-wire sequence for a Ticketmaster request."""

    status: TicketmasterDispatchStatus
    authorization: TicketmasterAuthorization | None = None
    lease: PacerLease | None = None

    def permits_dispatch_at(self, at: datetime) -> bool:
        """Reject a stored permit after its database-authorized UTC day has ended."""
        return (
            self.status is TicketmasterDispatchStatus.GRANTED
            and self.authorization is not None
            and self.authorization.permits_dispatch_at(at)
        )


class TicketmasterDispatchGate:
    """Pace, then irreversibly authorize immediate same-UTC-day source initiation (ADR-002/005).

    The Pacer is deliberately first: a paced workflow can sleep and retry the same attempt key
    without carrying a daily permit over a durable timer or across midnight.  Once the ledger has
    granted a key, a crash/lost acknowledgement returns ``ALREADY_AUTHORIZED`` on replay and the
    caller must not send again. A physical retry must mint a different deterministic attempt key.
    The resulting authorization is valid only for its database-authorized UTC day; a future adapter
    must call ``decision.permits_dispatch_at(now)`` in the same immediate pre-wire frame.
    """

    def __init__(
        self,
        ledger: TicketmasterBudgetLedger,
        pacer: Pacer,
        quota_scope: str,
    ) -> None:
        if not quota_scope:
            raise ValueError("Ticketmaster quota_scope must not be empty")
        self._ledger = ledger
        self._pacer = pacer
        self._quota_scope = quota_scope

    async def acquire(
        self,
        dispatch_key: str,
        request_fingerprint: str,
        budget_class: TicketmasterBudgetClass,
        *,
        cost: int = 1,
    ) -> TicketmasterDispatchDecision:
        """Pace once, then atomically charge an immediate one-request authorization."""
        lease = await self._pacer.acquire(
            PacerRequest(
                source=Source.TICKETMASTER,
                quota_scope=self._quota_scope,
                operation=PacerOperation.CATALOG_REFRESH,
                cost=cost,
                queue_item_id=dispatch_key,
            )
        )
        if not lease.granted:
            return TicketmasterDispatchDecision(TicketmasterDispatchStatus.PACED, lease=lease)

        authorization = await self._ledger.authorize(
            self._quota_scope,
            dispatch_key,
            request_fingerprint,
            budget_class,
            cost=cost,
        )
        if authorization.status is TicketmasterAuthorizationStatus.GRANTED:
            return TicketmasterDispatchDecision(
                TicketmasterDispatchStatus.GRANTED,
                authorization,
                lease,
            )
        if authorization.status is TicketmasterAuthorizationStatus.ALREADY_AUTHORIZED:
            return TicketmasterDispatchDecision(
                TicketmasterDispatchStatus.ALREADY_AUTHORIZED,
                authorization,
                lease,
            )
        return TicketmasterDispatchDecision(
            TicketmasterDispatchStatus.BUDGET_EXHAUSTED,
            authorization,
            lease,
        )

    async def record_outcome(
        self,
        dispatch_key: str,
        outcome: TicketmasterDispatchOutcome,
    ) -> bool:
        """Persist source-call audit state without ever refunding the daily charge (ADR-002)."""
        return await self._ledger.record_outcome(self._quota_scope, dispatch_key, outcome)
