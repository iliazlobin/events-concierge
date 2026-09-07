"""Migration contract for bounded ingestion source-registry sorting."""

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
        / "0131_sort_ingestion_admin_sources.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_closed_pre_page_sorting_with_stable_nulls_and_ties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0131"
    assert migration.down_revision == "0130"
    assert len(operations.statements) == 3
    function, revoke, grant = operations.statements
    assert "CREATE FUNCTION public.fn_list_ingestion_admin_sources_v5" in function
    assert "p_sort_by NOT IN (" in function
    assert "'source', 'health', 'catalog', 'last_success', 'latest_run', 'output'" in function
    assert "p_sort_direction NOT IN ('asc', 'desc')" in function
    assert "SECURITY DEFINER" in function
    assert "SET search_path = pg_catalog, public" in function
    assert function.index("ORDER BY") < function.index("LIMIT p_limit")
    assert "NULLS LAST" in function
    assert "ranked.source_key ASC" in function
    assert "enriched.source_key ASC" in function
    assert "THEN ranked.latest_run_canonical_count" in function
    assert "THEN ranked.latest_run_candidate_count" in function
    assert revoke.endswith("FROM PUBLIC")
    assert grant.endswith("TO ec_app")


def test_downgrade_removes_only_the_additive_sorted_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert len(operations.statements) == 2
    assert operations.statements[0].startswith(
        "REVOKE ALL ON FUNCTION public.fn_list_ingestion_admin_sources_v5"
    )
    assert operations.statements[0].endswith("FROM ec_app")
    assert operations.statements[1].startswith(
        "DROP FUNCTION IF EXISTS public.fn_list_ingestion_admin_sources_v5"
    )
