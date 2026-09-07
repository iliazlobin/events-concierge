"""Migration contract for additive locations and conservative price ceilings."""

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
        / "0127_advanced_catalog_location_price_filters.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_scoped_locations_and_known_usd_price_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0127"
    assert migration.down_revision == "0126"
    browse = operations.statements[0]
    assert "CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v3" in browse
    assert "p_cities text[]" in browse
    assert "p_location_scopes text[]" in browse
    for scope in ("bay_area", "manhattan", "los_angeles_area"):
        assert scope in browse
    assert "cardinality(coalesce(p_cities" in browse
    assert "event.price_status = 'free'" in browse
    assert "event.price_status = 'paid'" in browse
    assert "event.price_currency = 'USD'" in browse
    assert "event.price_max_cents <= p_price_max_cents" in browse
    assert "event.price_status = 'unknown'" not in browse
    assert browse.index("p_price_max_cents") < browse.index("LIMIT p_limit")
    assert all("DROP FUNCTION" not in statement for statement in operations.statements)
    assert any(
        "fn_browse_filtered_current_catalog_events_v3" in statement
        and statement.endswith("TO ec_app")
        for statement in operations.statements
    )


def test_downgrade_removes_only_the_v3_browse_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_browse_filtered_current_catalog_events_v3" in sql
    assert "fn_browse_filtered_current_catalog_events_v2" not in sql
    assert "DROP COLUMN" not in sql
