"""Migration contract for bounded, operator-safe catalog run execution evidence."""

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
        / "0135_catalog_run_execution_evidence.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_installs_closed_payload_free_evidence_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    emitted = "\n".join(operations.statements)
    assert migration.revision == "0135"
    assert migration.down_revision == "0134"
    assert "CREATE TABLE public.catalog_refresh_run_execution_metrics" in emitted
    assert "CREATE TABLE public.catalog_refresh_run_stage_metrics" in emitted
    assert "CREATE FUNCTION public.fn_record_catalog_refresh_run_execution_v1" in emitted
    assert "CREATE FUNCTION public.fn_record_catalog_refresh_run_stage_v1" in emitted
    assert "CREATE FUNCTION public.fn_list_ingestion_admin_runs_v4" in emitted
    assert all(
        stage in emitted
        for stage in (
            "'admission'",
            "'collect'",
            "'extract_enrich'",
            "'normalize_dedupe'",
            "'catalog_publish'",
        )
    )
    assert "activity_wall_clock_only" in emitted
    assert "process_metrics_omitted_shared_worker" in emitted
    assert "best_effort_process_delta_sequential_worker" in emitted
    assert "LEFT JOIN public.catalog_sources AS source" in emitted
    assert "FROM public.fn_list_ingestion_admin_runs_v3" in emitted
    assert "REVOKE ALL ON TABLE public.catalog_refresh_run_execution_metrics FROM ec_app" in emitted
    assert "REVOKE ALL ON TABLE public.catalog_refresh_run_stage_metrics FROM ec_app" in emitted
    for forbidden in (
        "provider_payload",
        "raw_payload",
        "credential",
        "stack_trace",
        "lease_token text",
        "log_message",
    ):
        assert forbidden not in emitted.lower()


def test_downgrade_removes_only_additive_run_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    emitted = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_list_ingestion_admin_runs_v4" in emitted
    assert "DROP FUNCTION IF EXISTS public.fn_record_catalog_refresh_run_stage_v1" in emitted
    assert "DROP FUNCTION IF EXISTS public.fn_record_catalog_refresh_run_execution_v1" in emitted
    assert "DROP TABLE IF EXISTS public.catalog_refresh_run_stage_metrics" in emitted
    assert "DROP TABLE IF EXISTS public.catalog_refresh_run_execution_metrics" in emitted
    assert "runs_v3" not in emitted
