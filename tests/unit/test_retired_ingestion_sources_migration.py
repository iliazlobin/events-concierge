"""Migration contract for immutable, superseded ingestion sources."""

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
        / "0139_retired_ingestion_sources.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_persists_retirement_and_installs_fail_closed_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    sql = "\n".join(operations.statements)
    assert migration.revision == "0139"
    assert migration.down_revision == "0138"
    assert "ADD COLUMN retired_at timestamptz" in sql
    assert "ck_catalog_sources_retired_disabled" in sql
    assert "catalog source retirement is immutable" in sql
    assert "superseded_by_aggregate_source" in sql
    assert "alameda-county-library-all-physical-branches-events" in sql
    assert "sccld-all-physical-branches-events" in sql
    assert "smcl-all-physical-branches-events" in sql
    assert "CREATE FUNCTION public.fn_list_ingestion_admin_sources_v6" in sql
    assert "WHEN registry.retired_at IS NOT NULL THEN 'retired'" in sql
    assert "CREATE FUNCTION public.fn_get_ingestion_admin_source_detail_v2" in sql
    assert "CREATE FUNCTION public.fn_list_ingestion_admin_due_sources_v2" in sql
    assert "WHERE registry.retired_at IS NULL" in sql
    assert "CREATE FUNCTION public.fn_update_ingestion_admin_source_configuration_v2" in sql
    assert "CREATE FUNCTION public.fn_set_ingestion_admin_sources_enabled_v2" in sql
    assert sql.count("SELECT 'unavailable'") == 2


def test_downgrade_removes_only_additive_retirement_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_list_ingestion_admin_sources_v6" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_get_ingestion_admin_source_detail_v2" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_list_ingestion_admin_due_sources_v2" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_update_ingestion_admin_source_configuration_v2" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_set_ingestion_admin_sources_enabled_v2" in sql
    assert "DROP COLUMN IF EXISTS retired_at" in sql
    assert "UPDATE public.catalog_sources" not in sql
