"""Migration contract for terminal retired-source command handling."""

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
        / "0140_retired_source_command_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_terminalizes_and_fences_retired_source_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    sql = "\n".join(operations.statements)
    assert migration.revision == "0140"
    assert migration.down_revision == "0139"
    assert "'source_retired'" in sql
    assert "command.status = 'queued'" in sql
    assert "command.lease_expires_at <= clock_timestamp()" in sql
    assert "source.retired_at IS NOT NULL" in sql
    assert "CREATE OR REPLACE FUNCTION public.fn_claim_ingestion_admin_commands_v2" in sql
    assert "source.retired_at IS NULL" in sql
    assert "CREATE OR REPLACE FUNCTION public.fn_renew_ingestion_admin_command_lease" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "TO ec_app" in sql


def test_downgrade_restores_prior_contract_and_normalizes_terminal_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "SET error_code = 'source_refresh_failed'" in sql
    assert "WHERE error_code = 'source_retired'" in sql
    assert "source.retired_at IS NULL" not in sql
    assert "source.retired_at IS NOT NULL" not in sql
    assert "'worker_unavailable', 'source_retired'" not in sql
