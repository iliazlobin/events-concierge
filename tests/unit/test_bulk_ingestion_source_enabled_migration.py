"""Migration contract for atomic bulk ingestion-source enabled-state revisions."""

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
        / "0125_atomic_bulk_source_enabled.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_config_revision_and_atomic_audited_bulk_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0125"
    assert migration.down_revision == "0124"
    assert len(operations.statements) == 6
    source_list, source_revoke, source_grant, bulk, bulk_revoke, bulk_grant = (
        operations.statements
    )
    assert "CREATE FUNCTION public.fn_list_ingestion_admin_sources_v4" in source_list
    assert "source_revision integer" in source_list
    assert "source.source_revision" in source_list
    assert "latest_run_source_revision integer" in source_list
    assert "fn_list_ingestion_admin_sources_v3" in source_list
    assert source_revoke.endswith("FROM PUBLIC")
    assert source_grant.endswith("TO ec_app")

    assert "CREATE FUNCTION public.fn_set_ingestion_admin_sources_enabled" in bulk
    assert "jsonb_array_length(p_targets)" in bulk
    assert "jsonb_object_keys(target.value)" in bulk
    assert "jsonb_object_length" not in bulk
    assert "v_requested NOT BETWEEN 1 AND 100" in bulk
    assert "count(DISTINCT target.value ->> 'source_key')" in bulk
    assert "ORDER BY parsed.source_key" in bulk
    assert bulk.index("FOR UPDATE") < bulk.index("UPDATE public.catalog_sources")
    assert bulk.index("source_revision <> v_target.expected_revision") < bulk.index(
        "UPDATE public.catalog_sources"
    )
    assert bulk.index("review_expires_at <= v_now") < bulk.index(
        "UPDATE public.catalog_sources"
    )
    assert "catalog_source_configuration_audit" in bulk
    assert "source_revision = source.source_revision + 1" in bulk
    assert "IF v_source.enabled IS NOT DISTINCT FROM p_enabled" in bulk
    assert bulk_revoke.endswith("FROM PUBLIC")
    assert bulk_grant.endswith("TO ec_app")


def test_downgrade_removes_only_additive_bulk_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert len(operations.statements) == 4
    assert operations.statements[0].startswith(
        "REVOKE ALL ON FUNCTION public.fn_set_ingestion_admin_sources_enabled"
    )
    assert operations.statements[1].startswith(
        "DROP FUNCTION IF EXISTS public.fn_set_ingestion_admin_sources_enabled"
    )
    assert operations.statements[2].startswith(
        "REVOKE ALL ON FUNCTION public.fn_list_ingestion_admin_sources_v4"
    )
    assert operations.statements[3].startswith(
        "DROP FUNCTION IF EXISTS public.fn_list_ingestion_admin_sources_v4"
    )
    assert all(
        "fn_list_ingestion_admin_sources_v3" not in statement
        for statement in operations.statements
    )
