"""Retained registration timeline boundaries and operator-only aggregate authority."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.integration.test_command_investigation import _scope
from tests.integration.test_operator_management import _denied, _owner_transaction, _role, _source

from events_concierge.adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from events_concierge.api.admin import IngestionSourceRegistrationHistoryOut

pytestmark = pytest.mark.integration
_SIGNATURE = "public.fn_get_operator_source_registration_history_v1(integer,boolean)"


async def test_registration_boundaries_quiet_buckets_and_lifecycle_inclusion() -> None:
    async with _owner_transaction() as connection:
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        before = await repo.source_registration_history(window_days=7, include_fixtures=False)
        await _role(connection, None)
        keys = [await _source(connection) for _ in range(5)]
        # A single outer statement gives fixtures and the projection exactly the same clock.
        # All writes are in this disposable database's rolled-back owner transaction.
        await connection.execute(
            text("""
            CREATE FUNCTION pg_temp.registration_boundary_read(p_keys text[])
            RETURNS jsonb LANGUAGE plpgsql AS $$
            DECLARE v_now timestamptz := statement_timestamp();
            BEGIN
                UPDATE public.catalog_sources SET created_at=v_now - CASE array_position(p_keys,source_key)
                    WHEN 1 THEN INTERVAL '8 days'
                    WHEN 2 THEN INTERVAL '7 days'
                    WHEN 3 THEN INTERVAL '6 days'
                    ELSE INTERVAL '0 days' END
                WHERE source_key=ANY(p_keys);
                UPDATE public.catalog_sources SET enabled=false WHERE source_key=p_keys[2];
                UPDATE public.catalog_sources SET enabled=false,retired_at=v_now,
                    retired_reason='operator_test_retirement' WHERE source_key=p_keys[3];
                UPDATE public.catalog_sources SET publisher='Tests' WHERE source_key=p_keys[5];
                RETURN public.fn_get_operator_source_registration_history_v1(7,false);
            END $$
        """)
        )
        data = (
            await connection.execute(
                text("SELECT pg_temp.registration_boundary_read(CAST(:keys AS text[]))"),
                {"keys": keys},
            )
        ).scalar_one()
        history = IngestionSourceRegistrationHistoryOut.model_validate(data)
        assert history.total_sources == before.total_sources + 4
        assert history.baseline_sources == before.baseline_sources + 1
        assert history.added_sources == before.added_sources + 3
        assert history.items[0].added_sources == before.items[0].added_sources + 1
        assert history.items[1].added_sources == before.items[1].added_sources + 1
        assert [bucket.added_sources for bucket in history.items[2:6]] == [0, 0, 0, 0]
        assert history.items[-1].added_sources == before.items[-1].added_sources + 1
        assert history.items[-1].registered_sources == history.total_sources
        assert history.items[0].bucket_start == history.window_start
        assert history.items[-1].bucket_end == history.generated_at
        included = await repo.source_registration_history(window_days=7, include_fixtures=True)
        assert included.total_sources > history.total_sources and included.include_fixtures
        # Registry revisions/enablement/retirement do not act like source removal.
        assert history.items[1].registered_sources >= 3
        for days in (30, 90):
            result = await repo.source_registration_history(
                window_days=days, include_fixtures=False
            )
            IngestionSourceRegistrationHistoryOut.model_validate(result)
            assert len(result.items) == days
            assert result.generated_at - result.window_start == timedelta(days=days)
            assert result.total_sources == history.total_sources


async def test_fixture_scope_is_global_and_empty_history_has_all_zero_buckets() -> None:
    async with _owner_transaction() as connection:
        # Scope all retained rows out using the established fixture predicate; no rows are deleted.
        await connection.execute(text("UPDATE public.catalog_sources SET publisher='Tests'"))
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        empty = await repo.source_registration_history(window_days=7, include_fixtures=False)
        assert empty.total_sources == empty.baseline_sources == empty.added_sources == 0
        assert len(empty.items) == 7
        assert all(bucket.registered_sources == bucket.added_sources == 0 for bucket in empty.items)
        included = await repo.source_registration_history(window_days=7, include_fixtures=True)
        assert included.total_sources > 0


async def test_registration_projection_grants_only_aggregate_execution() -> None:
    async with _owner_transaction() as connection:
        for role in ("ec_operator_viewer", "ec_operator_controller"):
            repo = PostgresIngestionAdminRepository(session_scope=_scope(connection, role))
            history = await repo.source_registration_history(window_days=90, include_fixtures=False)
            dumped = IngestionSourceRegistrationHistoryOut.model_validate(history).model_dump_json()
            assert not any(
                raw in dumped for raw in ("seed_url", "source_key", "requested_by", "before_config")
            )
            await _role(connection, role)
            for table in (
                "catalog_sources",
                "catalog_source_configuration_audit",
                "event_requests",
            ):
                await _denied(connection, f"SELECT * FROM public.{table} LIMIT 1")
        await _role(connection, None)
        info = (
            await connection.execute(
                text("""
            SELECT r.rolname,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
            WHERE p.oid=CAST(:signature AS regprocedure)
        """),
                {"signature": _SIGNATURE},
            )
        ).one()
        assert info.rolname == "ec_operator_aggregate_definer" and info.prosecdef
        assert "search_path=pg_catalog, public" in info.proconfig
        for role in ("ec_app", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(
                connection, "SELECT public.fn_get_operator_source_registration_history_v1()"
            )


@pytest.mark.parametrize("args", ["NULL,false", "1,false", "8,false", "91,false", "7,NULL"])
async def test_invalid_registration_history_scopes_fail(args: str) -> None:
    async with _owner_transaction() as connection:
        await _role(connection, "ec_operator_viewer")
        with pytest.raises(DBAPIError, match="invalid source registration history query"):
            await connection.execute(
                text(f"SELECT public.fn_get_operator_source_registration_history_v1({args})")
            )


@pytest.mark.parametrize(
    "stamp", ["statement_timestamp()+INTERVAL '1 day'", "'infinity'::timestamptz"]
)
async def test_invalid_stored_registration_time_is_unavailable(stamp: str) -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        await connection.execute(
            text(f"UPDATE public.catalog_sources SET created_at={stamp} WHERE source_key=:source"),
            {"source": source},
        )
        await _role(connection, "ec_operator_viewer")
        with pytest.raises(DBAPIError, match="source registration history unavailable"):
            await connection.execute(
                text("SELECT public.fn_get_operator_source_registration_history_v1()")
            )
