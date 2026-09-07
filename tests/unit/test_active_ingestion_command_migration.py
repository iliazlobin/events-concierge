"""Migration contract for atomic active ingestion-command admission."""

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
        / "0124_dedupe_active_ingestion_commands.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_enforces_one_active_source_and_fleet_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0124"
    assert migration.down_revision == "0123"
    assert len(operations.statements) == 4
    preflight, source_index, due_index, enqueue = operations.statements
    assert "active ingestion command duplicates must be drained" in preflight
    assert "GROUP BY command.source_key" in preflight
    assert "CREATE UNIQUE INDEX ux_ingestion_admin_commands_active_source" in source_index
    assert "action = 'refresh_source'" in source_index
    assert "status IN ('queued', 'running')" in source_index
    assert "CREATE UNIQUE INDEX ux_ingestion_admin_commands_active_due" in due_index
    assert "action = 'refresh_due'" in due_index
    assert "CREATE OR REPLACE FUNCTION public.fn_enqueue_ingestion_admin_command" in enqueue
    assert "ON CONFLICT DO NOTHING" in enqueue
    assert "ON CONFLICT (command_id)" not in enqueue
    assert "RETURN 'replayed'" in enqueue
    assert "RETURN 'conflict'" in enqueue


def test_downgrade_restores_uuid_only_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert len(operations.statements) == 3
    enqueue, due_index, source_index = operations.statements
    assert "ON CONFLICT (command_id) DO NOTHING" in enqueue
    assert "DROP INDEX IF EXISTS public.ux_ingestion_admin_commands_active_due" in due_index
    assert "DROP INDEX IF EXISTS public.ux_ingestion_admin_commands_active_source" in source_index
