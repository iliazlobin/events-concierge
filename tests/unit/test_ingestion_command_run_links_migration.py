"""Migration contract for authoritative command-to-run operational detail."""

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
        / "0142_ingestion_command_run_links.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_links_attempt_aware_run_identity_and_exposes_safe_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0142"
    assert migration.down_revision == "0141"
    sql = "\n".join(operations.statements)
    assert "CREATE TABLE public.ingestion_admin_command_runs" in sql
    assert "PRIMARY KEY (command_id, command_attempt, source_key, run_key)" in sql
    assert "UNIQUE (command_id, command_attempt, position)" in sql
    assert ("CONSTRAINT ck_admin_command_runs_attempt_positive CHECK (command_attempt >= 1)") in sql
    assert "command_attempt BETWEEN 1 AND 50" not in sql
    assert "p_command_attempt < 1" in sql
    assert "p_command_attempt NOT BETWEEN 1 AND 50" not in sql
    assert "command.status = 'running'" in sql
    assert "command.attempt_count = p_command_attempt" in sql
    assert "command.lease_token = p_lease_token" in sql
    assert "command.lease_expires_at > statement_timestamp()" in sql
    assert "p_run_key = 'admin:' || command.command_id::text" in sql
    assert "p_run_key NOT LIKE 'cadence:' || p_source_key || ':%'" in sql
    assert "CREATE FUNCTION public.fn_get_ingestion_admin_command_v3" in sql
    assert "CREATE FUNCTION public.fn_list_ingestion_admin_command_runs_v1" in sql
    assert "COALESCE(refresh.status, 'pending')" in sql
    assert "public.fn_normalize_catalog_refresh_error(refresh.error)" in sql
    assert "command.attempt_count = link.command_attempt" in sql
    assert "LIMIT 500" in sql
    child_projection = next(
        statement
        for statement in operations.statements
        if "CREATE FUNCTION public.fn_list_ingestion_admin_command_runs_v1" in statement
    )
    assert "run_position integer" in child_projection
    assert "link.position AS run_position" in child_projection
    assert "lease_token" not in child_projection
    assert "provider_payload" not in sql
    assert "stack_trace" not in sql
    assert "GRANT EXECUTE" in sql
    assert "TO ec_app" in sql


def test_downgrade_revokes_capabilities_before_dropping_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "FROM ec_app" in sql
    assert "DROP FUNCTION IF EXISTS" in sql
    assert "DROP TABLE IF EXISTS public.ingestion_admin_command_runs" in sql
