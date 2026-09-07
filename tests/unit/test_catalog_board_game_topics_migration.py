"""Migration contract for chess and board-game catalog topics."""

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
        / "0144_catalog_board_game_topics.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_expands_validation_extraction_backfill_and_paged_browse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    sql = "\n".join(operations.statements)
    assert migration.revision == "0144"
    assert migration.down_revision == "0143"
    assert "cardinality(topics) <= 19" in sql
    assert "'board-games', 'chess'" in sql
    assert "board[ -]?games?" in sql
    assert "(chess|bughouse)" in sql
    assert "event.extraction_evidence ||" in sql
    assert "fn_extract_catalog_event_semantics_v1" in sql
    assert "CREATE OR REPLACE FUNCTION public.fn_browse_filtered_current_catalog_events_v7" in sql
    assert "FOR v_row IN" in sql
    assert "CREATE TEMP TABLE" not in sql
    assert "GRANT EXECUTE" in sql


def test_downgrade_removes_only_added_topics_and_restores_legacy_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "array_remove(array_remove(event.topics, 'board-games'), 'chess')" in sql
    assert "cardinality(topics) <= 17" in sql
    legacy_constraint = next(
        statement
        for statement in operations.statements
        if "cardinality(topics) <= 17" in statement
    )
    assert "board-games" not in legacy_constraint
    assert "'chess'" not in legacy_constraint
    assert "GRANT EXECUTE" in sql
