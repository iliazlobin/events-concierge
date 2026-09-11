"""Fixture equivalence, unchanged authority and a bounded per-row performance proof."""

from __future__ import annotations

from importlib import import_module
from statistics import median

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.integration.test_operator_management import _denied, _owner_transaction, _role

pytestmark = pytest.mark.integration

_migration = import_module("migrations.versions.0186_fast_ingestion_fixture_predicate")
_FUNCTION = "public.fn_ingestion_admin_run_is_fixture(text,text)"
_KEYS = [
    None,
    "",
    "admin:00000000-0000-0000-0000-000000000001",
    "cadence:source:slot",
    "manual:p0",
    "manual:p123suffix",
    "manual:p",
    "manual:p-no-number",
    "manual:P1",
    "manual:paged-stage-contract-race-",
    "manual:paged-promotion-contract-race-retry",
    "manual:paged-stage-contract-race",
    "manual:paged-stage-other-",
    "prefix-manual:p1",
    "test-source",
    "manual:normal",
    "manual:p1\ntrailing",
]
_ERRORS = [
    None,
    "",
    "   ",
    "fixture",
    "FIXTURE",
    "prefix fixture suffix",
    "test_fixture",
    "FiXtUrE\ncontract",
    "rate limit fixture",
    "timeout",
    "rate limit",
    "retry-after",
    "access denied",
    "forbidden",
    "quarantine",
    "policy denied",
    "automation not allowed",
    "pacer deferred",
    "pacer unavailable",
    "lease expired",
    "source revision changed",
    "page cap exceeded",
    "parse schema error",
    "timed out",
    "unclassified",
    "fixtur",
    "fixtures",
]


async def _metadata(connection: AsyncConnection) -> dict[str, object]:
    return dict(
        (
            await connection.execute(
                text("""
        SELECT p.oid, p.proowner, p.proacl::text, p.proconfig, p.provolatile,
               p.proparallel, p.prosecdef, p.proisstrict, p.proargtypes::text,
               p.prorettype, p.procost
        FROM pg_catalog.pg_proc p WHERE p.oid=CAST(:signature AS regprocedure)
    """),
                {"signature": _FUNCTION},
            )
        )
        .mappings()
        .one()
    )


async def _matrix(connection: AsyncConnection) -> list[bool]:
    return list(
        (
            await connection.execute(
                text("""
        SELECT public.fn_ingestion_admin_run_is_fixture(k.value,e.value)
        FROM unnest(CAST(:keys AS text[])) WITH ORDINALITY k(value,position)
        CROSS JOIN unnest(CAST(:errors AS text[])) WITH ORDINALITY e(value,position)
        ORDER BY k.position,e.position
    """),
                {"keys": _KEYS, "errors": _ERRORS},
            )
        )
        .scalars()
        .all()
    )


async def test_fixture_predicate_matches_original_for_null_patterns_and_error_precedence() -> None:
    async with _owner_transaction() as connection:
        replacement = await _matrix(connection)
        await connection.execute(text(_migration.DOWNGRADE_SQL))
        original = await _matrix(connection)
        assert replacement == original
        assert all(isinstance(value, bool) for value in replacement)
        # Existing rows are also compared in the same transaction, without recording raw errors.
        original_rows = (
            await connection.execute(
                text("""
            SELECT source_key,run_key,public.fn_ingestion_admin_run_is_fixture(run_key,error)
            FROM public.catalog_refresh_runs ORDER BY source_key,run_key
        """)
            )
        ).all()
        await connection.execute(text(_migration.UPGRADE_SQL))
        new_rows = (
            await connection.execute(
                text("""
            SELECT source_key,run_key,public.fn_ingestion_admin_run_is_fixture(run_key,error)
            FROM public.catalog_refresh_runs ORDER BY source_key,run_key
        """)
            )
        ).all()
        assert new_rows == original_rows


async def test_predicate_replacement_retains_identity_acl_and_projection_capability() -> None:
    async with _owner_transaction() as connection:
        before = await _metadata(connection)
        await connection.execute(text(_migration.DOWNGRADE_SQL))
        assert await _metadata(connection) == before
        await connection.execute(text(_migration.UPGRADE_SQL))
        assert await _metadata(connection) == before
        assert before["proconfig"] == ["search_path=pg_catalog"]
        assert before["provolatile"] == "i" and before["proparallel"] == "s"
        assert before["prosecdef"] is False and before["proisstrict"] is False
        for role in (
            "ec_app",
            "ec_operator_viewer",
            "ec_operator_controller",
            "ec_ingestion_executor",
        ):
            await _role(connection, role)
            await _denied(connection, "SELECT public.fn_ingestion_admin_run_is_fixture(NULL,NULL)")
        await _role(connection, "ec_operator_viewer")
        assert (
            await connection.execute(
                text("""
            SELECT count(*) FROM public.fn_list_ingestion_admin_sources_v6(
                NULL,'all',NULL,NULL,NULL,NULL,false,'source','asc',100,0
            )
        """)
            )
        ).scalar_one() > 0
        assert (
            await connection.execute(
                text("""
            SELECT count(*) FROM public.fn_list_ingestion_admin_filter_values_v2(
                NULL,'all',NULL,NULL,NULL,false
            )
        """)
            )
        ).scalar_one() > 0


async def _scan_ms(connection: AsyncConnection) -> float:
    plan = (
        await connection.execute(
            text("""
        EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF)
        WITH input AS MATERIALIZED (
            SELECT 'cadence:source:' || i AS run_key,
                   CASE i % 5 WHEN 0 THEN 'fixture case ' || i
                              WHEN 1 THEN 'timeout ' || i ELSE NULL END AS error
            FROM generate_series(1,12000) i
        )
        SELECT count(*) FROM input
        WHERE NOT public.fn_ingestion_admin_run_is_fixture(run_key,error)
    """)
        )
    ).scalar_one()
    assert plan[0]["Plan"]["Actual Rows"] == 1
    return float(plan[0]["Execution Time"])


async def test_compiled_predicate_avoids_repeated_sql_normalizer_overhead() -> None:
    async with _owner_transaction() as connection:
        await connection.execute(text("SET LOCAL statement_timeout='5s'"))
        await connection.execute(text(_migration.DOWNGRADE_SQL))
        original = median([await _scan_ms(connection) for _ in range(3)])
        await connection.execute(text(_migration.UPGRADE_SQL))
        replacement = median([await _scan_ms(connection) for _ in range(3)])
        print(
            f"fixture predicate 12k rows median: original={original:.1f}ms replacement={replacement:.1f}ms"
        )
        # Relative repeated-query comparison tolerates host load and does not enforce a
        # machine-specific latency target. The old implementation is an order of magnitude slower.
        assert replacement < original * 0.65
