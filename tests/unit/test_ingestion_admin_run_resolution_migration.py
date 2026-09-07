"""Migration-contract coverage for authoritative ingestion-run resolution annotations."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


class _RecordingOperations:
    """Capture Alembic SQL without requiring a migration connection."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        """Record one emitted migration statement."""
        self.statements.append(statement)


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0122_ingestion_admin_run_resolution.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_authoritative_annotations_before_run_slicing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0122"
    assert migration.down_revision == "0121"
    assert len(operations.statements) == 3
    capability = operations.statements[0]
    assert "CREATE FUNCTION public.fn_list_ingestion_admin_runs_v3" in capability
    assert "is_latest_for_source boolean" in capability
    assert "resolved_by_newer_success boolean" in capability
    assert "FROM public.fn_ingestion_admin_run_facts_v2" in capability
    assert "scoped AS MATERIALIZED" in capability
    assert "annotated AS MATERIALIZED" in capability
    assert "ORDER BY scoped.started_at DESC, scoped.run_key DESC" in capability
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING" in capability
    assert capability.index("annotated AS MATERIALIZED") < capability.index(
        "WHERE (p_status IS NULL"
    )
    assert capability.index("WHERE (p_status IS NULL") < capability.index("LIMIT p_limit")
    assert operations.statements[1].startswith(
        "REVOKE ALL ON FUNCTION public.fn_list_ingestion_admin_runs_v3"
    )
    assert operations.statements[1].endswith("FROM PUBLIC")
    assert operations.statements[2].endswith("TO ec_app")
    assert all("DROP FUNCTION" not in statement for statement in operations.statements)


def test_downgrade_removes_only_the_additive_v3_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert len(operations.statements) == 2
    assert operations.statements[0].startswith(
        "REVOKE ALL ON FUNCTION public.fn_list_ingestion_admin_runs_v3"
    )
    assert operations.statements[0].endswith("FROM ec_app")
    assert operations.statements[1].startswith(
        "DROP FUNCTION IF EXISTS public.fn_list_ingestion_admin_runs_v3"
    )
    assert all("runs_v2" not in statement for statement in operations.statements)
