"""Verify the cadence recovery migration against actual PostgreSQL function contracts."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_SIGNATURE = "public.fn_list_ingestion_admin_due_sources_v3(timestamptz,integer)"


def _roundtrip(connection: Connection, *, spacing_variation: bool) -> None:
    path = Path(__file__).parents[2] / "migrations/versions/0206_cadence_success_recovery.py"
    spec = importlib.util.spec_from_file_location("cadence_recovery_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    definition_query = text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))")
    security_query = text("""
        SELECT proowner, proacl::text, prosecdef, proconfig
        FROM pg_catalog.pg_proc WHERE oid=CAST(:signature AS regprocedure)
    """)
    parameters = {"signature": _SIGNATURE}
    definition = connection.execute(definition_query, parameters).scalar_one()
    security = connection.execute(security_query, parameters).one()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert connection.execute(definition_query, parameters).scalar_one() != definition
        assert connection.execute(security_query, parameters).one() == security
        if spacing_variation:
            original = connection.execute(definition_query, parameters).scalar_one()
            assert original.count(migration._FAILED_GATE) == 1
            connection.execute(
                text(
                    original.replace(
                        migration._FAILED_GATE, "OR\n\tlatest.attempt_status\t<>   'failed'"
                    )
                )
            )
        migration.upgrade()
    assert connection.execute(definition_query, parameters).scalar_one() == definition
    assert connection.execute(security_query, parameters).one() == security


@pytest.mark.parametrize("spacing_variation", [False, True])
async def test_recovery_migration_roundtrip_preserves_owner_acl_and_contract(
    db: None, spacing_variation: bool
) -> None:
    engine = create_async_engine(os.environ["EC_MIGRATION_URL"])
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: _roundtrip(
                    sync_connection, spacing_variation=spacing_variation
                )
            )
    finally:
        await engine.dispose()


def _reject_changed_gate(connection: Connection, gate: str | None) -> None:
    path = Path(__file__).parents[2] / "migrations/versions/0206_cadence_success_recovery.py"
    spec = importlib.util.spec_from_file_location("cadence_recovery_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    query = text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))")
    parameters = {"signature": _SIGNATURE}
    original = connection.execute(query, parameters).scalar_one()
    with Operations.context(MigrationContext.configure(connection)):
        if gate is not None:
            migration.downgrade()
            definition = connection.execute(query, parameters).scalar_one()
            connection.execute(text(definition.replace(migration._FAILED_GATE, gate)))
        before = connection.execute(query, parameters).scalar_one()
        with pytest.raises(RuntimeError, match="cadence failure gate changed"):
            migration.upgrade()
        assert connection.execute(query, parameters).scalar_one() == before
    connection.execute(text(original))


@pytest.mark.parametrize(
    "gate",
    [
        "OR latest.attempt_status = 'failed'",
        "OR latest.attempt_status <> 'failed' OR latest.attempt_status <> 'failed'",
        None,
    ],
    ids=["different-expression", "duplicate-gate", "already-recovered"],
)
async def test_recovery_migration_refuses_unrecognized_or_duplicate_gates(
    db: None, gate: str | None
) -> None:
    engine = create_async_engine(os.environ["EC_MIGRATION_URL"])
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: _reject_changed_gate(sync_connection, gate)
            )
    finally:
        await engine.dispose()
