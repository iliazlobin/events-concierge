"""PostgreSQL Ticketmaster hard-cap, retry, and privilege regressions (ADR-002)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from events_concierge.adapters.postgres.budget import PostgresBudgetLedger
from events_concierge.domain.budget import (
    TicketmasterAuthorizationStatus,
    TicketmasterBudgetClass,
    TicketmasterDispatchOutcome,
)
from events_concierge.infra.db import system_session_scope, tenant_session_scope

pytestmark = pytest.mark.integration

type ScopeConfigurator = Callable[[str], Awaitable[None]]


def _fingerprint(value: str) -> str:
    """Build the required opaque SHA-256 request binding without storing request content."""
    return sha256(value.encode("utf-8")).hexdigest()


async def _authorize(
    ledger: PostgresBudgetLedger,
    scope: str,
    key: str,
    budget_class: TicketmasterBudgetClass,
    *,
    cost: int = 1,
) -> TicketmasterAuthorizationStatus:
    authorization = await ledger.authorize(
        scope,
        key,
        _fingerprint(key),
        budget_class,
        cost=cost,
    )
    return authorization.status


async def _authorize_with_tenant_context(
    tenant_id: UUID,
    scope: str,
    key: str,
    *,
    cost: int,
) -> str:
    """Call the global guard under an otherwise unrelated tenant GUC."""
    async with tenant_session_scope(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    """SELECT authorization_status
                       FROM public.fn_authorize_ticketmaster_dispatch(
                           :scope, :key, :fingerprint, 'crawl', :cost
                       )"""
                ),
                {"scope": scope, "key": key, "fingerprint": _fingerprint(key), "cost": cost},
            )
        ).one()
    return str(row.authorization_status)


async def test_ticketmaster_ledger_enforces_the_4500_500_partition_without_refunds(
    db: None,
    configure_ticketmaster_budget_scope: ScopeConfigurator,
) -> None:
    """Crawl cannot consume reserve and an audit failure never returns a durable charge (ADR-002)."""
    scope = f"tm-budget-{uuid4().hex}"
    await configure_ticketmaster_budget_scope(scope)
    ledger = PostgresBudgetLedger()

    assert (
        await _authorize(ledger, scope, "crawl:bulk", TicketmasterBudgetClass.CRAWL, cost=4_499)
        is TicketmasterAuthorizationStatus.GRANTED
    )
    assert (
        await _authorize(ledger, scope, "crawl:last", TicketmasterBudgetClass.CRAWL)
        is TicketmasterAuthorizationStatus.GRANTED
    )
    assert (
        await _authorize(ledger, scope, "crawl:reserve-guard", TicketmasterBudgetClass.CRAWL)
        is TicketmasterAuthorizationStatus.EXHAUSTED
    )
    assert (
        await _authorize(ledger, scope, "repoll:bulk", TicketmasterBudgetClass.RESERVE, cost=500)
        is TicketmasterAuthorizationStatus.GRANTED
    )
    assert (
        await _authorize(ledger, scope, "repoll:overflow", TicketmasterBudgetClass.RESERVE)
        is TicketmasterAuthorizationStatus.EXHAUSTED
    )

    usage = await ledger.usage(scope)
    assert (usage.crawl_used, usage.reserve_used, usage.total_used) == (4_500, 500, 5_000)
    assert await ledger.record_outcome(scope, "repoll:bulk", TicketmasterDispatchOutcome.FAILED)
    assert not await ledger.record_outcome(scope, "repoll:bulk", TicketmasterDispatchOutcome.FAILED)
    assert (await ledger.usage(scope)).total_used == 5_000
    with pytest.raises(Exception):  # noqa: B017 -- changing terminal audit state must fail closed
        await ledger.record_outcome(scope, "repoll:bulk", TicketmasterDispatchOutcome.SUCCEEDED)

    async with system_session_scope() as session:
        row = (
            await session.execute(
                text(
                    """SELECT count(*) AS authorization_count
                       FROM public.provider_budget_ledger
                       WHERE source = 'ticketmaster' AND quota_scope = :scope"""
                ),
                {"scope": scope},
            )
        ).one()
    assert int(row.authorization_count) == 3


async def test_ticketmaster_ledger_concurrent_duplicate_and_distinct_attempts_never_overspend(
    db: None,
    configure_ticketmaster_budget_scope: ScopeConfigurator,
) -> None:
    """One shared key charges once; competing remaining capacity has one winner (ADR-002)."""
    ledger = PostgresBudgetLedger()
    duplicate_scope = f"tm-duplicate-{uuid4().hex}"
    await configure_ticketmaster_budget_scope(duplicate_scope)
    duplicate_key = "crawl:sf:music:attempt-1"
    duplicate_barrier = asyncio.Barrier(8)

    async def duplicate_attempt() -> TicketmasterAuthorizationStatus:
        await duplicate_barrier.wait()
        return await _authorize(
            ledger, duplicate_scope, duplicate_key, TicketmasterBudgetClass.CRAWL
        )

    statuses = await asyncio.gather(*(duplicate_attempt() for _ in range(8)))
    assert statuses.count(TicketmasterAuthorizationStatus.GRANTED) == 1
    assert statuses.count(TicketmasterAuthorizationStatus.ALREADY_AUTHORIZED) == 7
    assert (await ledger.usage(duplicate_scope)).total_used == 1
    with pytest.raises(Exception):  # noqa: B017 -- key binding prevents a semantic replay
        await ledger.authorize(
            duplicate_scope,
            duplicate_key,
            _fingerprint("different request"),
            TicketmasterBudgetClass.CRAWL,
            cost=1,
        )
    assert (await ledger.usage(duplicate_scope)).total_used == 1

    contention_scope = f"tm-contention-{uuid4().hex}"
    await configure_ticketmaster_budget_scope(contention_scope)
    assert (
        await _authorize(
            ledger, contention_scope, "crawl:bulk", TicketmasterBudgetClass.CRAWL, cost=4_499
        )
        is TicketmasterAuthorizationStatus.GRANTED
    )
    contention_barrier = asyncio.Barrier(2)

    async def remaining_attempt(index: int) -> TicketmasterAuthorizationStatus:
        await contention_barrier.wait()
        return await _authorize(
            ledger,
            contention_scope,
            f"crawl:last-{index}",
            TicketmasterBudgetClass.CRAWL,
        )

    remaining = await asyncio.gather(*(remaining_attempt(index) for index in range(2)))
    assert remaining.count(TicketmasterAuthorizationStatus.GRANTED) == 1
    assert remaining.count(TicketmasterAuthorizationStatus.EXHAUSTED) == 1
    usage = await ledger.usage(contention_scope)
    assert (usage.crawl_used, usage.reserve_used, usage.total_used) == (4_500, 0, 4_500)


async def test_ticketmaster_ledger_uses_database_utc_and_the_app_role_cannot_bypass_it(
    db: None,
    configure_ticketmaster_budget_scope: ScopeConfigurator,
) -> None:
    """The SECURITY DEFINER function works without tenant context while direct DML is denied."""
    scope = f"tm-utc-{uuid4().hex}"
    await configure_ticketmaster_budget_scope(scope)
    key = "crawl:utc:attempt-1"
    async with system_session_scope() as session:
        await session.execute(text("SET LOCAL TIME ZONE 'Pacific/Auckland'"))
        authorization = (
            await session.execute(
                text(
                    """SELECT authorization_status, budget_day
                       FROM public.fn_authorize_ticketmaster_dispatch(
                           :scope, :key, :fingerprint, 'crawl', 1
                       )"""
                ),
                {"scope": scope, "key": key, "fingerprint": _fingerprint(key)},
            )
        ).one()
        database_utc_day = (
            (
                await session.execute(
                    text("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date AS budget_day")
                )
            )
            .one()
            .budget_day
        )
        privileges = (
            await session.execute(
                text(
                    """SELECT has_table_privilege(current_user, 'public.provider_budget_daily', 'INSERT')
                                      AS can_insert_daily,
                               has_table_privilege(current_user, 'public.provider_budget_ledger', 'UPDATE')
                                      AS can_update_ledger,
                               has_table_privilege(current_user, 'public.provider_budget_ledger', 'DELETE')
                                      AS can_delete_ledger"""
                )
            )
        ).one()
        function_metadata = (
            await session.execute(
                text(
                    """SELECT procedure.prosecdef, procedure.proconfig
                       FROM pg_proc AS procedure
                       JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
                       WHERE namespace.nspname = 'public'
                         AND procedure.proname = 'fn_authorize_ticketmaster_dispatch'"""
                )
            )
        ).one()
        ledger_primary_key = (
            await session.execute(
                text(
                    """SELECT array_agg(attribute.attname ORDER BY key_column.ordinality)
                                      AS key_columns
                       FROM pg_constraint AS constraint_row
                       JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
                           AS key_column(attnum, ordinality) ON TRUE
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_row.conrelid
                        AND attribute.attnum = key_column.attnum
                       WHERE constraint_row.conrelid = 'public.provider_budget_ledger'::regclass
                         AND constraint_row.contype = 'p'
                       GROUP BY constraint_row.oid"""
                )
            )
        ).one()

    assert authorization.authorization_status == TicketmasterAuthorizationStatus.GRANTED.value
    assert authorization.budget_day == database_utc_day
    assert (
        privileges.can_insert_daily,
        privileges.can_update_ledger,
        privileges.can_delete_ledger,
    ) == (
        False,
        False,
        False,
    )
    assert function_metadata.prosecdef
    assert "search_path=pg_catalog, public" in str(function_metadata.proconfig)
    # The attempt key is global to the app scope, so a lost acknowledgement cannot become a fresh
    # permit merely because the physical retry lands on the next UTC day.
    assert list(ledger_primary_key.key_columns) == ["source", "quota_scope", "dispatch_key"]

    # An app role cannot invent a second 5,000-call bucket, and tenant context does not partition
    # the one configured shared app-key scope.
    with pytest.raises(Exception):  # noqa: B017 -- owner-controlled scope registry fails closed
        await PostgresBudgetLedger().authorize(
            f"{scope}-unconfigured",
            "crawl:unconfigured:attempt-1",
            _fingerprint("crawl:unconfigured:attempt-1"),
            TicketmasterBudgetClass.CRAWL,
            cost=1,
        )
    assert (
        await _authorize_with_tenant_context(uuid4(), scope, "crawl:tenant-one", cost=4_498)
        == TicketmasterAuthorizationStatus.GRANTED.value
    )
    assert (
        await _authorize_with_tenant_context(uuid4(), scope, "crawl:tenant-two", cost=2)
        == TicketmasterAuthorizationStatus.EXHAUSTED.value
    )

    with pytest.raises(Exception):  # noqa: B017 -- direct table mutation must not bypass the guard
        async with system_session_scope() as session:
            await session.execute(
                text(
                    """INSERT INTO public.provider_budget_daily
                           (source, quota_scope, budget_day)
                       VALUES ('ticketmaster', :scope, CURRENT_DATE)"""
                ),
                {"scope": f"tm-bypass-{uuid4().hex}"},
            )
