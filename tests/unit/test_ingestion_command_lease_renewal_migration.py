"""Migration contract for exact-token ingestion-command lease heartbeats."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


class _RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0130_heartbeat_ingestion_command_leases.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_installs_live_exact_token_renewal_with_least_privilege(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0130"
    assert migration.down_revision == "0129"
    renewal = next(
        statement
        for statement in operations.statements
        if "CREATE FUNCTION public.fn_renew_ingestion_admin_command_lease" in statement
    )
    overview = next(
        statement
        for statement in operations.statements
        if "CREATE FUNCTION public.fn_get_ingestion_admin_overview_v2" in statement
    )
    reconciliation = next(
        statement
        for statement in operations.statements
        if "SET lease_expires_at = clock_timestamp()" in statement
    )
    renewal_permissions = [
        statement
        for statement in operations.statements
        if "fn_renew_ingestion_admin_command_lease" in statement
        and "CREATE FUNCTION" not in statement
    ]
    overview_permissions = [
        statement
        for statement in operations.statements
        if "fn_get_ingestion_admin_overview_v2" in statement
        and "CREATE FUNCTION" not in statement
    ]
    assert "CREATE FUNCTION public.fn_renew_ingestion_admin_command_lease" in renewal
    assert "command.status = 'running'" in renewal
    assert "command.lease_token = p_lease_token" in renewal
    assert "command.lease_expires_at > clock_timestamp()" in renewal
    assert "clock_timestamp() + p_lease_seconds * INTERVAL '1 second'" in renewal
    assert "p_lease_seconds < 300" in renewal
    assert "p_lease_seconds > 21600" in renewal
    assert "RETURN FOUND" in renewal
    assert any(
        "REVOKE ALL" in statement and statement.endswith("FROM PUBLIC")
        for statement in renewal_permissions
    )
    assert any(
        "GRANT EXECUTE" in statement and statement.endswith("TO ec_app")
        for statement in renewal_permissions
    )
    assert "CREATE FUNCTION public.fn_get_ingestion_admin_overview_v2" in overview
    assert "FROM public.fn_get_ingestion_admin_overview()" in overview
    assert "FROM public.fn_ingestion_admin_run_facts_v2" in overview
    assert "normalized_runs AS MATERIALIZED" in overview
    assert "annotated_runs AS MATERIALIZED" in overview
    assert "PARTITION BY normalized_runs.source_key" in overview
    assert "normalized_runs.started_at DESC" in overview
    assert "normalized_runs.run_key DESC" in overview
    assert "annotated_runs.status = 'running'" in overview
    assert "annotated_runs.is_latest_for_source" in overview
    assert "annotated_runs.status = 'failed'" in overview
    assert "legacy.generated_at - INTERVAL '24 hours'" in overview
    assert any(
        "REVOKE ALL" in statement and statement.endswith("FROM PUBLIC")
        for statement in overview_permissions
    )
    assert any(
        "GRANT EXECUTE" in statement and statement.endswith("TO ec_app")
        for statement in overview_permissions
    )
    assert "UPDATE public.ingestion_admin_commands" in reconciliation
    assert "command.status = 'running'" in reconciliation
    assert "command.lease_token IS NOT NULL" in reconciliation
    assert "command.lease_expires_at > clock_timestamp()" in reconciliation
    assert "SET lease_expires_at = clock_timestamp()" in reconciliation


def test_downgrade_revokes_runtime_before_dropping_additive_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    revoke_overview = next(
        statement
        for statement in operations.statements
        if "REVOKE ALL" in statement
        and "fn_get_ingestion_admin_overview_v2" in statement
    )
    drop_overview = next(
        statement
        for statement in operations.statements
        if "DROP FUNCTION IF EXISTS" in statement
        and "fn_get_ingestion_admin_overview_v2" in statement
    )
    revoke_runtime = next(
        statement
        for statement in operations.statements
        if "REVOKE ALL" in statement
        and "fn_renew_ingestion_admin_command_lease" in statement
    )
    drop_renewal = next(
        statement
        for statement in operations.statements
        if "DROP FUNCTION IF EXISTS" in statement
        and "fn_renew_ingestion_admin_command_lease" in statement
    )
    assert "REVOKE ALL" in revoke_overview
    assert "fn_get_ingestion_admin_overview_v2" in revoke_overview
    assert "FROM ec_app" in revoke_overview
    assert "DROP FUNCTION IF EXISTS" in drop_overview
    assert "fn_get_ingestion_admin_overview_v2" in drop_overview
    assert "REVOKE ALL" in revoke_runtime
    assert "FROM ec_app" in revoke_runtime
    assert "DROP FUNCTION IF EXISTS" in drop_renewal
    assert "fn_renew_ingestion_admin_command_lease" in drop_renewal
