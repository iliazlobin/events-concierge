"""Migration contract for OID-safe paged catalog topic filtering."""

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
        / "0138_catalog_topic_browse_paging.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_streams_bounded_scans_without_temp_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    sql = "\n".join(operations.statements)
    assert migration.revision == "0138"
    assert migration.down_revision == "0137"
    assert "CREATE OR REPLACE FUNCTION public.fn_browse_filtered_current_catalog_events_v7" in sql
    assert "FOR v_row IN" in sql
    assert "RETURN NEXT" in sql
    assert "event_topics @> coalesce(p_topics" in sql
    assert "fn_browse_filtered_current_catalog_events_v6" in sql
    assert "CREATE TEMP TABLE" not in operations.statements[0]
    assert "GRANT EXECUTE" in sql


def test_downgrade_restores_0136_browse_body(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "CREATE TEMP TABLE catalog_topic_scan" in sql
    assert "CREATE TEMP TABLE catalog_topic_selected" in sql
    assert "GRANT EXECUTE" in sql
