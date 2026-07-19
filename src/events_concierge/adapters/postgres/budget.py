"""PostgreSQL Ticketmaster pre-dispatch budget ledger (FR-3.4, ADR-002)."""

from __future__ import annotations

from datetime import date
from typing import Protocol, cast

from sqlalchemy import text

from ...domain.budget import (
    BudgetUsage,
    TicketmasterAuthorization,
    TicketmasterAuthorizationStatus,
    TicketmasterBudgetClass,
    TicketmasterDispatchOutcome,
)
from ...domain.enums import Source
from ...infra.db import system_session_scope

_MAX_DAILY_COST = 5_000
_SHA256_HEX_LENGTH = 64
_SHA256_HEX_CHARACTERS = "0123456789abcdef"


class _AuthorizationRow(Protocol):
    authorization_status: str
    budget_day: date
    dispatch_key: str
    budget_class: str
    cost: int


class _UsageRow(Protocol):
    budget_day: date
    crawl_used: int
    reserve_used: int


class PostgresBudgetLedger:
    """Call the guarded, tenant-neutral authorization function under the app role.

    ``ec_app`` has no direct DML privilege on either table and may use only the owner-configured
    app-key scope, so no future adapter can bypass the decrement-before-dispatch invariant.
    PostgreSQL's UTC clock, rather than a caller timestamp, picks the charged day. A crash after
    authorization conservatively burns the charge; an ambiguous retry gets ``ALREADY_AUTHORIZED``
    and never receives another wire permit (ADR-002).
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
        """Atomically authorize one physical Ticketmaster attempt through the SQL choke point."""
        _validate_authorization(quota_scope, dispatch_key, request_fingerprint, cost)
        async with system_session_scope() as session:
            result = await session.execute(
                text(
                    """SELECT authorization_status, budget_day, dispatch_key, budget_class, cost
                       FROM public.fn_authorize_ticketmaster_dispatch(
                           :quota_scope, :dispatch_key, :request_fingerprint, :budget_class, :cost
                       )"""
                ),
                {
                    "quota_scope": quota_scope,
                    "dispatch_key": dispatch_key,
                    "request_fingerprint": request_fingerprint,
                    "budget_class": budget_class.value,
                    "cost": cost,
                },
            )
            row = cast(_AuthorizationRow, result.one())
        return TicketmasterAuthorization(
            status=TicketmasterAuthorizationStatus(row.authorization_status),
            quota_scope=quota_scope,
            budget_day=row.budget_day,
            dispatch_key=row.dispatch_key,
            request_fingerprint=request_fingerprint,
            budget_class=TicketmasterBudgetClass(row.budget_class),
            cost=int(row.cost),
        )

    async def record_outcome(
        self,
        quota_scope: str,
        dispatch_key: str,
        outcome: TicketmasterDispatchOutcome,
    ) -> bool:
        """Attach an audit result to an authorization; this never changes daily counters."""
        if not quota_scope:
            raise ValueError("Ticketmaster quota_scope must not be empty")
        if not dispatch_key:
            raise ValueError("Ticketmaster dispatch_key must not be empty")
        async with system_session_scope() as session:
            result = await session.execute(
                text(
                    """SELECT public.fn_record_ticketmaster_dispatch_outcome(
                           :quota_scope, :dispatch_key, :outcome
                       ) AS recorded"""
                ),
                {
                    "quota_scope": quota_scope,
                    "dispatch_key": dispatch_key,
                    "outcome": outcome.value,
                },
            )
            return bool(result.scalar_one())

    async def usage(self, quota_scope: str) -> BudgetUsage:
        """Read the current database UTC-day counters without creating a daily row."""
        if not quota_scope:
            raise ValueError("Ticketmaster quota_scope must not be empty")
        async with system_session_scope() as session:
            result = await session.execute(
                text(
                    """WITH current_day AS (
                           SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date AS budget_day
                       )
                       SELECT current_day.budget_day,
                              COALESCE(daily.crawl_used, 0) AS crawl_used,
                              COALESCE(daily.reserve_used, 0) AS reserve_used
                       FROM current_day
                       LEFT JOIN public.provider_budget_daily AS daily
                         ON daily.source = 'ticketmaster'
                        AND daily.quota_scope = :quota_scope
                        AND daily.budget_day = current_day.budget_day"""
                ),
                {"quota_scope": quota_scope},
            )
            row = cast(_UsageRow, result.one())
        return BudgetUsage(
            source=Source.TICKETMASTER,
            quota_scope=quota_scope,
            budget_day=row.budget_day,
            crawl_used=int(row.crawl_used),
            reserve_used=int(row.reserve_used),
        )


def _validate_authorization(
    quota_scope: str, dispatch_key: str, request_fingerprint: str, cost: int
) -> None:
    if not quota_scope:
        raise ValueError("Ticketmaster quota_scope must not be empty")
    if not dispatch_key:
        raise ValueError("Ticketmaster dispatch_key must not be empty")
    if len(request_fingerprint) != _SHA256_HEX_LENGTH or any(
        character not in _SHA256_HEX_CHARACTERS for character in request_fingerprint
    ):
        raise ValueError("Ticketmaster request_fingerprint must be a lowercase SHA-256 hex digest")
    if not 1 <= cost <= _MAX_DAILY_COST:
        raise ValueError("Ticketmaster budget cost must be between 1 and 5,000")
