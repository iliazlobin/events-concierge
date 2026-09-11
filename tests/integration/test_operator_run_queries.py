"""Global query, interval, exact identity and least-privilege run drilldowns."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.integration.test_command_investigation import _scope
from tests.integration.test_operator_management import _denied, _owner_transaction, _role, _source

from events_concierge.adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from events_concierge.api.admin import IngestionRunOut

pytestmark = pytest.mark.integration


async def _run(
    connection: AsyncConnection,
    source: str,
    key: str,
    started: datetime,
    *,
    duration: int = 1,
    status: str = "succeeded",
    error: str | None = None,
    attempts: int = 1,
) -> None:
    await connection.execute(
        text("""
      INSERT INTO public.catalog_refresh_runs(source_key,run_key,status,started_at,completed_at,candidate_count,canonical_count,attempt_count,error)
      VALUES(:source,:key,:status,:started,:completed,10,8,:attempts,:error)
    """),
        {
            "source": source,
            "key": key,
            "status": status,
            "started": started,
            "completed": started + timedelta(seconds=duration),
            "attempts": attempts,
            "error": error,
        },
    )


async def test_global_search_sort_and_exact_lookup_are_not_limited_to_first_page() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        now = datetime.now(UTC)
        old_key = f"cadence:{source}:old-slow"
        await _run(connection, source, old_key, now - timedelta(days=12), duration=900, attempts=7)
        for index in range(103):
            await _run(
                connection,
                source,
                f"cadence:{source}:recent-{index:03}",
                now - timedelta(minutes=index + 1),
            )
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        latest = await repo.list_runs(source_key=source, limit=100)
        assert latest.total == 104 and old_key not in {run.run_key for run in latest.items}
        slow = await repo.list_runs(source_key=source, sort_by="duration", limit=1)
        assert slow.total == 104 and slow.items[0].run_key == old_key
        searched = await repo.list_runs(source_key=source, query="OLD-SLOW", limit=1)
        assert searched.total == 1 and searched.items[0].run_key == old_key
        exhausted = await repo.list_runs(source_key=source, query="OLD-SLOW", offset=10)
        assert exhausted.total == 1 and exhausted.items == ()
        exact = await repo.lookup_run(source, old_key)
        assert exact is not None and exact.run_key == old_key and exact.attempt_count == 7
        assert not exact.is_latest_for_source
        assert IngestionRunOut.model_validate(asdict(exact)).duration_ms == 900000
        assert await repo.lookup_run(source, "unknown") is None


async def test_anchored_half_open_interval_overrides_window_and_stage_filter_is_global() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        start = datetime.now(UTC) - timedelta(days=10)
        end = start + timedelta(hours=1)
        for suffix, at in (
            ("before", start - timedelta(seconds=1)),
            ("start", start),
            ("middle", start + timedelta(minutes=30)),
            ("end", end),
        ):
            await _run(connection, source, f"cadence:{source}:{suffix}", at)
        for suffix, duration, outcome in (("start", 0, "succeeded"), ("middle", 900, "failed")):
            await connection.execute(
                text("""
              INSERT INTO public.catalog_refresh_run_stage_metrics(source_key,run_key,stage,observation_count,duration_ms,last_outcome_code,first_observed_at,last_observed_at)
              VALUES(:source,:run,'collect',1,:duration,:outcome,:at,:at)
            """),
                {
                    "source": source,
                    "run": f"cadence:{source}:{suffix}",
                    "duration": duration,
                    "outcome": outcome,
                    "at": start,
                },
            )
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        page = await repo.list_runs(
            source_key=source,
            window_hours=1,
            started_after=start,
            started_before=end,
            sort_by="started",
            sort_direction="asc",
        )
        assert [run.run_key.rsplit(":", 1)[-1] for run in page.items] == ["start", "middle"]
        measured = await repo.list_runs(
            source_key=source, stage="collect", sort_by="stage_duration", limit=1
        )
        assert measured.total == 2 and measured.items[0].run_key.endswith(":middle")
        failed = await repo.list_runs(source_key=source, stage="collect", stage_outcome="failed")
        assert failed.total == 1 and failed.items[0].run_key.endswith(":middle")
        folded = await repo.list_runs(source_key=source, stage="extract_enrich")
        assert folded.total == 0


async def test_lookup_obeys_fixture_visibility_and_never_searches_raw_errors() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        key = f"cadence:{source}:failed"
        await _run(
            connection,
            source,
            key,
            datetime.now(UTC),
            status="failed",
            error="supersecret-provider-payload",
        )
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        assert (await repo.list_runs(source_key=source, query="supersecret")).total == 0
        exact = await repo.lookup_run(source, key)
        assert exact is not None and "supersecret" not in str(asdict(exact))
        await _role(connection, None)
        await connection.execute(
            text("UPDATE public.catalog_sources SET publisher='Tests' WHERE source_key=:source"),
            {"source": source},
        )
        assert await repo.lookup_run(source, key) is None
        assert await repo.lookup_run(source, key, include_fixtures=True) is not None
        assert (await repo.list_runs(source_key=source)).total == 0
        assert (await repo.list_runs(source_key=source, include_fixtures=True)).total == 1


async def test_fixed_capabilities_reject_raw_access_and_invalid_database_queries() -> None:
    async with _owner_transaction() as connection:
        for role in ("ec_app", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(connection, "SELECT public.fn_query_ingestion_admin_runs_v1('{}')")
            await _denied(
                connection,
                "SELECT public.fn_lookup_ingestion_admin_run_v1('known-source','run',false)",
            )
        await _role(connection, "ec_operator_viewer")
        await _denied(connection, "SELECT public.fn_operator_run_evidence_v1('{}',false)")
        await _denied(connection, "SELECT error FROM public.catalog_refresh_runs")
        for payload in (
            None,
            {"sort_by": "raw_sql"},
            {"limit": 101},
            {"stage_outcome": "failed"},
            {"query": "x\nsecret"},
            {"started_after": "2020-01-01T00:00:00Z"},
            {"started_after": "2020-01-01T00:00:00", "started_before": "2020-01-02T00:00:00"},
            {"started_after": "2020-01-01T00:00:00Z", "started_before": "2021-01-01T00:00:00Z"},
        ):
            async with connection.begin_nested() as savepoint:
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text("SELECT public.fn_query_ingestion_admin_runs_v1(CAST(:p AS jsonb))"),
                        {"p": json.dumps(payload) if payload is not None else None},
                    )
                await savepoint.rollback()
