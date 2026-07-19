"""Ticketmaster Pacer-then-one-time-ledger gate tests (FR-3.4/10.4, ADR-002/005)."""

from __future__ import annotations

from datetime import UTC, datetime

from events_concierge.application.ticketmaster_budget import (
    TicketmasterDispatchGate,
    TicketmasterDispatchStatus,
)
from events_concierge.domain.budget import (
    BudgetUsage,
    TicketmasterAuthorization,
    TicketmasterAuthorizationStatus,
    TicketmasterBudgetClass,
    TicketmasterDispatchOutcome,
)
from events_concierge.domain.enums import Source
from events_concierge.ports.policy import (
    PacerLease,
    PacerLeaseStatus,
    PacerOperation,
    PacerRequest,
)

_FINGERPRINT = "a" * 64


class _Ledger:
    def __init__(self, events: list[str], status: TicketmasterAuthorizationStatus) -> None:
        self.events = events
        self.status = status
        self.authorizations: list[tuple[str, str, TicketmasterBudgetClass, int]] = []
        self.outcomes: list[tuple[str, str, TicketmasterDispatchOutcome]] = []

    async def authorize(
        self,
        quota_scope: str,
        dispatch_key: str,
        request_fingerprint: str,
        budget_class: TicketmasterBudgetClass,
        *,
        cost: int,
    ) -> TicketmasterAuthorization:
        self.events.append("ledger")
        self.authorizations.append((dispatch_key, request_fingerprint, budget_class, cost))
        return TicketmasterAuthorization(
            self.status,
            quota_scope,
            datetime(2026, 7, 17, tzinfo=UTC).date(),
            dispatch_key,
            request_fingerprint,
            budget_class,
            cost,
        )

    async def record_outcome(
        self,
        quota_scope: str,
        dispatch_key: str,
        outcome: TicketmasterDispatchOutcome,
    ) -> bool:
        self.events.append("outcome")
        self.outcomes.append((quota_scope, dispatch_key, outcome))
        return True

    async def usage(self, quota_scope: str) -> BudgetUsage:
        return BudgetUsage(Source.TICKETMASTER, quota_scope, datetime.now(UTC).date(), 0, 0)


class _Pacer:
    def __init__(self, events: list[str], lease: PacerLease) -> None:
        self.events = events
        self.lease = lease
        self.requests: list[PacerRequest] = []

    async def acquire(self, request: PacerRequest) -> PacerLease:
        self.events.append("pacer")
        self.requests.append(request)
        return self.lease

    async def observe_backoff(
        self,
        request: PacerRequest,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        del request, retry_after_seconds, reset_at


async def test_ticketmaster_gate_paces_then_authorizes_immediately_before_one_wire_call() -> None:
    """A Pacer wait can retry the same attempt without carrying a day permit (ADR-002/005)."""
    events: list[str] = []
    pacer = _Pacer(events, PacerLease(PacerLeaseStatus.GRANTED))
    gate = TicketmasterDispatchGate(
        _Ledger(events, TicketmasterAuthorizationStatus.GRANTED),
        pacer,
        "app-scope",
    )

    decision = await gate.acquire(
        "crawl:sf:music:near:attempt-1",
        _FINGERPRINT,
        TicketmasterBudgetClass.CRAWL,
        cost=2,
    )

    assert decision.status is TicketmasterDispatchStatus.GRANTED
    assert decision.permits_dispatch_at(datetime(2026, 7, 17, 23, 59, tzinfo=UTC))
    assert not decision.permits_dispatch_at(datetime(2026, 7, 18, tzinfo=UTC))
    assert events == ["pacer", "ledger"]
    assert pacer.requests == [
        PacerRequest(
            source=Source.TICKETMASTER,
            quota_scope="app-scope",
            operation=PacerOperation.CATALOG_REFRESH,
            cost=2,
            queue_item_id="crawl:sf:music:near:attempt-1",
        )
    ]


async def test_ticketmaster_gate_pacer_wait_does_not_debit_the_daily_ledger() -> None:
    """A Temporal timer re-enters the Pacer first, so no permit is stranded across midnight."""
    events: list[str] = []
    pacer = _Pacer(events, PacerLease(PacerLeaseStatus.WAIT, retry_after_seconds=3.0))
    ledger = _Ledger(events, TicketmasterAuthorizationStatus.GRANTED)
    gate = TicketmasterDispatchGate(ledger, pacer, "app-scope")

    decision = await gate.acquire(
        "crawl:sf:music:near:attempt-1",
        _FINGERPRINT,
        TicketmasterBudgetClass.CRAWL,
    )

    assert decision.status is TicketmasterDispatchStatus.PACED
    assert not decision.permits_dispatch_at(datetime(2026, 7, 17, tzinfo=UTC))
    assert decision.authorization is None
    assert events == ["pacer"]
    assert ledger.authorizations == []


async def test_ticketmaster_gate_never_reissues_a_lost_acknowledgement_key() -> None:
    """A duplicate authorization is a conservative stop, not a second source-call permit."""
    events: list[str] = []
    pacer = _Pacer(events, PacerLease(PacerLeaseStatus.GRANTED))
    gate = TicketmasterDispatchGate(
        _Ledger(events, TicketmasterAuthorizationStatus.ALREADY_AUTHORIZED),
        pacer,
        "app-scope",
    )

    decision = await gate.acquire(
        "crawl:sf:music:near:attempt-1",
        _FINGERPRINT,
        TicketmasterBudgetClass.CRAWL,
    )

    assert decision.status is TicketmasterDispatchStatus.ALREADY_AUTHORIZED
    assert not decision.permits_dispatch_at(datetime(2026, 7, 17, tzinfo=UTC))
    assert events == ["pacer", "ledger"]


async def test_ticketmaster_gate_records_an_audit_outcome_without_refund_path() -> None:
    """The only exposed completion operation forwards immutable audit state (ADR-002)."""
    events: list[str] = []
    ledger = _Ledger(events, TicketmasterAuthorizationStatus.GRANTED)
    gate = TicketmasterDispatchGate(
        ledger, _Pacer(events, PacerLease(PacerLeaseStatus.GRANTED)), "app"
    )

    assert await gate.record_outcome(
        "crawl:sf:music:near:attempt-1", TicketmasterDispatchOutcome.FAILED
    )
    assert ledger.outcomes == [
        ("app", "crawl:sf:music:near:attempt-1", TicketmasterDispatchOutcome.FAILED)
    ]
