"""Migration contract for repairing already-installed command attempt caps."""

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
        / "0143_unbound_ingestion_command_run_attempts.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_repairs_old_and_fresh_constraints_and_preserves_capability_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0143"
    assert migration.down_revision == "0142"
    sql = "\n".join(operations.statements)
    assert "DROP CONSTRAINT IF EXISTS ingestion_admin_command_runs_command_attempt_check" in sql
    assert "DROP CONSTRAINT IF EXISTS ck_admin_command_runs_attempt_positive" in sql
    assert "DROP CONSTRAINT IF EXISTS ck_admin_command_runs_attempt_1_50" in sql
    assert (
        "ADD CONSTRAINT ck_admin_command_runs_attempt_positive CHECK (command_attempt >= 1)"
    ) in sql
    assert "CREATE OR REPLACE FUNCTION public.fn_link_ingestion_admin_command_run_v1" in sql
    assert "p_command_attempt < 1" in sql
    assert "p_command_attempt NOT BETWEEN 1 AND 50" not in sql
    assert "command.attempt_count = p_command_attempt" in sql
    assert "command.lease_token = p_lease_token" in sql
    assert "command.lease_expires_at > statement_timestamp()" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, public" in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "FROM PUBLIC" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "TO ec_app" in sql


def test_downgrade_restores_cap_only_when_no_retained_attempt_would_be_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "WHERE command_attempt > 50" in sql
    assert "FROM public.ingestion_admin_commands" in sql
    assert "WHERE attempt_count > 50" in sql
    assert "cannot restore command attempt cap while attempts above 50 exist" in sql
    assert (
        "ADD CONSTRAINT ck_admin_command_runs_attempt_1_50 CHECK (command_attempt BETWEEN 1 AND 50)"
    ) in sql
    assert "p_command_attempt NOT BETWEEN 1 AND 50" in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
