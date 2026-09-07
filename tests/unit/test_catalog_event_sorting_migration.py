"""Migration contract for stable bidirectional consumer catalog paging."""

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
        / "0145_catalog_event_sorting.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_sorts_before_limit_and_pages_in_the_selected_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0145"
    assert migration.down_revision == "0144"
    assert len(operations.statements) == 3
    function, revoke, grant = operations.statements
    assert "CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v8" in function
    assert "p_sort NOT IN ('soonest', 'latest')" in function
    assert "event.start_at > p_after_start" in function
    assert "event.start_at < p_after_start" in function
    assert function.index("CASE WHEN p_sort = 'latest'") < function.index("LIMIT p_limit")
    assert "event.topics @>" in function
    assert "SECURITY DEFINER" in function
    assert revoke.endswith("FROM PUBLIC")
    assert grant.endswith("TO ec_app")


def test_downgrade_removes_only_the_additive_sorted_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert operations.statements == [
        "DROP FUNCTION IF EXISTS " + migration._BROWSE_V8,
    ]
