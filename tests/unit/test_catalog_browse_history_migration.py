"""Migration contract for explicit historical catalog browsing."""

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
        / "0132_catalog_browse_explicit_history.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_bounded_history_without_weakening_live_browse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0132"
    assert migration.down_revision == "0131"
    browse = operations.statements[0]
    providers = operations.statements[1]
    assert "CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v5" in browse
    assert "LANGUAGE plpgsql" in browse
    assert "STABLE" in browse
    assert "SECURITY DEFINER" in browse
    assert "SET search_path = pg_catalog, public" in browse
    assert "((p_window_start IS NULL) <> (p_window_end IS NULL))" in browse
    assert "p_window_end - p_window_start > interval '370 days'" in browse
    assert "event.start_at >= coalesce(p_window_start, statement_timestamp())" in browse
    assert "p_window_end IS NULL OR event.start_at < p_window_end" in browse
    assert "event.start_at >= statement_timestamp()" not in browse
    assert "event.entity_profiles" in browse
    assert "public.fn_ingestion_admin_source_is_fixture" in browse
    assert "public.fn_ingestion_admin_run_is_fixture" in browse
    assert "p_after_start IS NULL" in browse
    assert "event.canonical_event_id > p_after_id" in browse
    assert browse.index("p_after_start IS NULL") < browse.index("LIMIT p_limit")
    assert "ORDER BY eligible.start_at, eligible.canonical_event_id" in browse
    assert "CREATE FUNCTION public.fn_list_current_catalog_providers_v2" in providers
    assert "event.start_at >= coalesce(p_window_start, statement_timestamp())" in providers
    assert "p_window_end IS NULL OR event.start_at < p_window_end" in providers
    assert all("DROP FUNCTION" not in statement for statement in operations.statements)
    assert any(
        "fn_browse_filtered_current_catalog_events_v5" in statement and "FROM PUBLIC" in statement
        for statement in operations.statements
    )
    assert any(
        "fn_browse_filtered_current_catalog_events_v5" in statement
        and statement.endswith("TO ec_app")
        for statement in operations.statements
    )
    assert any(
        "fn_list_current_catalog_providers_v2" in statement
        and "FROM PUBLIC" in statement
        for statement in operations.statements
    )
    assert any(
        "fn_list_current_catalog_providers_v2" in statement
        and statement.endswith("TO ec_app")
        for statement in operations.statements
    )


def test_downgrade_removes_only_the_v5_history_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_browse_filtered_current_catalog_events_v5" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_list_current_catalog_providers_v2" in sql
    assert "fn_browse_filtered_current_catalog_events_v4" not in sql
    assert "DROP COLUMN" not in sql
