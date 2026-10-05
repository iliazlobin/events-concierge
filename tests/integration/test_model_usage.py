"""Real PostgreSQL ledger, period budgets, audit, privacy and role boundaries."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from events_concierge.adapters.postgres.model_usage import PostgresModelUsageStore
from events_concierge.domain.model_usage import ModelCallUsage

pytestmark = pytest.mark.integration
_ROLES = {"ec_app", "ec_operator_viewer", "ec_operator_controller", "ec_ingestion_executor"}


@asynccontextmanager
async def transaction() -> AsyncIterator[tuple[AsyncConnection, PostgresModelUsageStore]]:
    url = os.environ.get("EC_MIGRATION_URL")
    if not url:
        pytest.skip("use the isolated integration runner")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(
                text("TRUNCATE public.model_usage_calls, public.model_usage_budget_audit")
            )
            await connection.execute(
                text(
                    "UPDATE public.model_usage_budget SET revision=1, mode='enforce', daily_limit_usd=NULL, monthly_limit_usd=NULL"
                )
            )

            @asynccontextmanager
            async def scope() -> AsyncIterator[AsyncSession]:
                async with AsyncSession(bind=connection) as session:
                    yield session

            yield connection, PostgresModelUsageStore(session_scope=scope)
            await connection.rollback()
    finally:
        await engine.dispose()


async def role(connection: AsyncConnection, name: str | None) -> None:
    assert name is None or name in _ROLES
    await connection.execute(text("RESET ROLE" if name is None else f"SET LOCAL ROLE {name}"))


async def configure(store: PostgresModelUsageStore, **overrides: object) -> dict:
    settings = {
        "expected_revision": 1,
        "mode": "enforce",
        "daily_limit_usd": None,
        "monthly_limit_usd": None,
        "alert_percent": 80,
        **overrides,
    }
    result = await store.update_budget(settings, "fixture-reviewer")
    assert result is not None
    return result


async def test_runtime_roles_have_only_their_named_model_capabilities() -> None:
    async with transaction() as (connection, store):
        for name in _ROLES:
            assert not (
                await connection.execute(
                    text("SELECT pg_has_role(:role, 'ec_model_usage_definer', 'MEMBER')"),
                    {"role": name},
                )
            ).scalar_one()
            assert not (
                await connection.execute(
                    text(
                        "SELECT has_table_privilege(:role, 'public.model_usage_calls', 'SELECT,INSERT,UPDATE,DELETE')"
                    ),
                    {"role": name},
                )
            ).scalar_one()
            await role(connection, name)
            async with connection.begin_nested() as savepoint:
                with pytest.raises(DBAPIError, match="permission denied"):
                    await connection.execute(text("SELECT * FROM public.model_usage_budget"))
                await savepoint.rollback()
            if name in {"ec_app", "ec_ingestion_executor"}:
                async with connection.begin_nested() as savepoint:
                    with pytest.raises(DBAPIError, match="permission denied"):
                        await store.budget()
                    await savepoint.rollback()
            if name != "ec_operator_controller":
                async with connection.begin_nested() as savepoint:
                    with pytest.raises(DBAPIError, match="permission denied"):
                        await configure(store)
                    await savepoint.rollback()
            await role(connection, None)
        await role(connection, "ec_operator_controller")
        assert (await configure(store, daily_limit_usd="5"))["daily_limit_usd"].startswith("5")
        await role(connection, None)
        assert (
            await connection.execute(text("SELECT actor FROM model_usage_budget_audit"))
        ).scalar_one() == "fixture-reviewer"


async def test_actual_model_history_zero_unknown_and_budget_scope_are_distinct() -> None:
    async with transaction() as (connection, store):
        await role(connection, "ec_app")
        charged, free, missing = uuid4(), uuid4(), uuid4()
        for call in (charged, free, missing):
            assert await store.begin_call(call, "requested/model") == "allowed"
        await store.finish_call(
            charged,
            ModelCallUsage(
                status="ok",
                actual_model="fallback/model",
                generation_id="gen-charged",
                cost_usd=Decimal(".025"),
                input_tokens=100,
                output_tokens=20,
                cached_tokens=40,
                reasoning_tokens=5,
            ),
        )
        await store.finish_call(
            free,
            ModelCallUsage(
                status="ok",
                actual_model="free/model",
                cost_usd=Decimal(0),
                input_tokens=0,
                output_tokens=0,
            ),
        )
        await store.finish_call(
            missing, ModelCallUsage(status="failed", error_code="request_failed")
        )
        await role(connection, "ec_operator_viewer")
        now = (await connection.execute(text("SELECT clock_timestamp()"))).scalar_one()
        all_usage = await store.report(now - timedelta(days=1), now, 1, None)
        assert all_usage["totals"]["calls"] == 3
        assert all_usage["totals"]["unknown_cost_calls"] == 1
        assert Decimal(all_usage["totals"]["cost_usd"]) == Decimal(".025")
        assert all_usage["totals"]["cached_tokens"] == 40
        assert sum(bucket["calls"] for bucket in all_usage["series"]) == 3
        filtered = await store.report(now - timedelta(days=1), now, 1, "free/model")
        assert filtered["totals"]["calls"] == 1 and Decimal(filtered["totals"]["cost_usd"]) == 0
        assert (await store.budget())["monthly_used_usd"] == all_usage["totals"]["cost_usd"]
        assert set(all_usage["model_options"]) == {"fallback/model", "free/model", "unreported"}
        assert "gen-charged" not in json.dumps(all_usage)


async def test_enforced_budget_stops_later_calls_warning_mode_continues_and_edits_use_occ() -> None:
    async with transaction() as (connection, store):
        await role(connection, "ec_operator_controller")
        await configure(store, daily_limit_usd="1", monthly_limit_usd="10")
        assert await store.update_budget({"expected_revision": 1}, "other-reviewer") is None
        await role(connection, "ec_app")
        call = uuid4()
        assert await store.begin_call(call, "model/a") == "allowed"
        assert await store.begin_call(call, "model/a") == "already_started"
        await store.finish_call(call, ModelCallUsage(status="ok", cost_usd=Decimal("1")))
        assert await store.begin_call(uuid4(), "model/a") == "budget_exhausted"
        async with connection.begin_nested() as savepoint:
            with pytest.raises(DBAPIError, match="not pending"):
                await store.finish_call(call, ModelCallUsage(status="ok", cost_usd=Decimal(0)))
            await savepoint.rollback()
        await role(connection, "ec_operator_controller")
        await configure(store, expected_revision=2, mode="warn", daily_limit_usd="1")
        await role(connection, "ec_app")
        assert await store.begin_call(uuid4(), "model/a") == "allowed"
        await role(connection, None)
        assert (
            await connection.execute(text("SELECT count(*) FROM model_usage_budget_audit"))
        ).scalar_one() == 2


async def test_unknown_charges_and_abandoned_calls_hold_only_an_active_budget_period() -> None:
    async with transaction() as (connection, store):
        await configure(store, daily_limit_usd="5")
        call = uuid4()
        assert await store.begin_call(call, "model/a") == "allowed"
        # In-flight work can complete; a stale pending receipt prevents silent spend growth.
        assert await store.begin_call(uuid4(), "model/b") == "allowed"
        await connection.execute(
            text(
                "UPDATE model_usage_calls SET started_at=clock_timestamp()-interval '3 minutes' WHERE call_id=:id"
            ),
            {"id": call},
        )
        assert await store.begin_call(uuid4(), "model/c") == "cost_unknown"
        assert (await store.budget())["unknown_calls"] == 1
        await connection.execute(
            text(
                "UPDATE model_usage_calls SET started_at=date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'-interval '1 minute', status='failed', completed_at=clock_timestamp() WHERE call_id=:id"
            ),
            {"id": call},
        )
        assert await store.begin_call(uuid4(), "model/c") == "allowed"
        assert (await store.budget())["unknown_calls"] == 0
        await configure(store, expected_revision=2, monthly_limit_usd="50")
        # Only assert a monthly hold when yesterday belongs to the current month.
        if datetime.now(UTC).day > 1:
            assert await store.begin_call(uuid4(), "model/c") == "cost_unknown"


async def test_window_bounds_and_empty_intervals_do_not_invent_usage() -> None:
    async with transaction() as (_, store):
        now = datetime.now(UTC)
        report = await store.report(now - timedelta(days=7), now, 24, None)
        assert len(report["series"]) == 7
        assert report["totals"]["calls"] == 0 and report["totals"]["latency_ms"] is None
        assert report["tracked_since"] is None
        assert all(bucket["calls"] == 0 for bucket in report["series"])
