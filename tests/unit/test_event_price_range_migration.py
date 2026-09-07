"""Migration contract for exact event pricing and pre-pagination people search."""

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
        / "0123_event_price_range_and_people_search.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_is_additive_and_searches_people_before_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0123"
    assert migration.down_revision == "0122"
    schema = "\n".join(operations.statements[:4])
    for table in (
        "canonical_events",
        "event_source_links",
        "catalog_event_observations",
        "catalog_refresh_stage_candidates",
    ):
        assert f"ALTER TABLE public.{table}" in schema
    assert "price_min_cents BETWEEN 1 AND 100000000" in schema
    assert "price_max_cents BETWEEN price_min_cents AND 100000000" in schema
    assert "price_status = 'paid'" in schema
    assert "price_currency ~ '^[A-Z]{3}$'" in schema

    stage = operations.statements[4]
    read = operations.statements[5]
    browse = operations.statements[6]
    assert "CREATE FUNCTION public.fn_stage_paged_catalog_refresh_page_v3" in stage
    assert "public.fn_stage_paged_catalog_refresh_page_v2(" in stage
    assert "CREATE FUNCTION public.fn_read_paged_catalog_refresh_stage_v3" in read
    assert "price_min_cents integer" in read
    assert "CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v2" in browse
    for field in (
        "event.organizer_name",
        "event.host_names",
        "event.speaker_names",
        "event.partner_names",
    ):
        assert field in browse
    assert browse.index("event.organizer_name") < browse.index("LIMIT p_limit")
    assert "event.price_min_cents" in browse
    assert all("DROP FUNCTION" not in statement for statement in operations.statements)
    assert any(
        "fn_stage_paged_catalog_refresh_page_v3" in statement
        and statement.endswith("TO ec_app")
        for statement in operations.statements
    )
    assert any(
        "fn_browse_filtered_current_catalog_events_v2" in statement
        and statement.endswith("TO ec_app")
        for statement in operations.statements
    )


def test_downgrade_removes_only_new_capabilities_constraints_and_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_stage_paged_catalog_refresh_page_v3" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_read_paged_catalog_refresh_stage_v3" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_browse_filtered_current_catalog_events_v2" in sql
    assert "DROP COLUMN IF EXISTS price_min_cents" in sql
    assert "DROP COLUMN IF EXISTS price_max_cents" in sql
    assert "DROP COLUMN IF EXISTS price_currency" in sql
    assert "fn_stage_paged_catalog_refresh_page_v2" not in sql
    assert "fn_browse_filtered_current_catalog_events(" not in sql
