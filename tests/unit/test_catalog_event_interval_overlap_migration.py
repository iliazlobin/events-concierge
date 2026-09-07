"""Migration contract for interval-overlap catalog browsing."""

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
        / "0141_catalog_event_interval_overlap.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_uses_interval_overlap_and_end_aware_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    sql = "\n".join(operations.statements)
    assert migration.revision == "0141"
    assert migration.down_revision == "0140"
    assert (
        "coalesce(event.end_at, event.start_at)\n"
        "                    > coalesce(p_window_start, statement_timestamp())"
    ) in sql
    assert "coalesce(event.end_at, event.start_at) <= statement_timestamp()" in sql
    assert "event.start_at < p_window_end" in sql
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert "DROP FUNCTION" not in sql
    assert "FROM PUBLIC, ec_app" in sql


def test_downgrade_restores_start_only_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "event.start_at >= coalesce(p_window_start, statement_timestamp())" in sql
    assert "event.start_at < statement_timestamp()" in sql
    assert "coalesce(event.end_at, event.start_at)" not in sql
    assert "DROP FUNCTION" not in sql
