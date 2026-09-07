"""Migration contract for verified entity-profile persistence and capabilities."""

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
        / "0128_verified_event_entity_profiles.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_checked_storage_and_lossless_v4_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0128"
    assert migration.down_revision == "0127"
    validator, canonical, stage_table, stage_v4, read_v4, browse_v4 = (
        operations.statements[:6]
    )
    assert "CREATE FUNCTION public.fn_event_entity_profiles_valid" in validator
    assert "jsonb_array_length(p_profiles) > 64" in validator
    assert "v_role NOT IN ('host', 'organizer', 'speaker', 'partner')" in validator
    assert "/in/" in validator
    assert "/company/" in validator
    assert "search/results" not in validator
    assert "lower(v_name) <> lower(p_organizer_name)" in validator
    assert "ALTER TABLE public.canonical_events" in canonical
    assert "entity_profiles jsonb NOT NULL DEFAULT '[]'::jsonb" in canonical
    assert "ALTER TABLE public.catalog_refresh_stage_candidates" in stage_table
    assert "CREATE FUNCTION public.fn_stage_paged_catalog_refresh_page_v4" in stage_v4
    assert "public.fn_stage_paged_catalog_refresh_page_v3(" in stage_v4
    assert "candidate.value - 'entity_profiles'" in stage_v4
    assert "CREATE FUNCTION public.fn_read_paged_catalog_refresh_stage_v4" in read_v4
    assert "entity_profiles jsonb" in read_v4
    assert "CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v4" in browse_v4
    assert "public.fn_browse_filtered_current_catalog_events_v3(" in browse_v4
    assert "event.entity_profiles" in browse_v4
    assert all("DROP FUNCTION" not in statement for statement in operations.statements)
    for function_name in (
        "fn_event_entity_profiles_valid",
        "fn_stage_paged_catalog_refresh_page_v4",
        "fn_read_paged_catalog_refresh_stage_v4",
        "fn_browse_filtered_current_catalog_events_v4",
    ):
        assert any(
            function_name in statement and "FROM PUBLIC" in statement
            for statement in operations.statements
        )
        assert any(
            function_name in statement and statement.endswith("TO ec_app")
            for statement in operations.statements
        )


def test_downgrade_removes_only_0128_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    for function_name in (
        "fn_stage_paged_catalog_refresh_page_v4",
        "fn_read_paged_catalog_refresh_stage_v4",
        "fn_browse_filtered_current_catalog_events_v4",
        "fn_event_entity_profiles_valid",
    ):
        assert f"DROP FUNCTION IF EXISTS public.{function_name}" in sql
    assert sql.count("DROP COLUMN IF EXISTS entity_profiles") == 2
    assert "fn_stage_paged_catalog_refresh_page_v3" not in sql
    assert "fn_read_paged_catalog_refresh_stage_v3" not in sql
    assert "fn_browse_filtered_current_catalog_events_v3" not in sql
