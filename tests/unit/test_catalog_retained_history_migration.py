"""Migration contract for retained past catalog observations."""

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
        / "0134_retained_catalog_browse_history.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_splits_retained_past_from_latest_success_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0134"
    assert migration.down_revision == "0133"
    sources, observations, browse, providers = operations.statements[:4]

    assert "CREATE FUNCTION public.fn_list_admitted_catalog_browse_sources_v1" in sources
    assert "source.enabled" in sources
    assert "source.handoff_only" in sources
    assert "source.reviewed_at IS NOT NULL" in sources
    assert "public.fn_ingestion_admin_source_is_fixture" in sources

    assert "CREATE FUNCTION public.fn_list_retained_catalog_browse_observations_v1" in observations
    assert "JOIN public.catalog_refresh_runs AS observed_run" in observations
    assert "observed_run.status = 'succeeded'" in observations
    assert "public.fn_ingestion_admin_run_is_fixture" in observations
    assert "event.start_at < statement_timestamp()" in observations
    assert "success.run_key = observation.last_run_key" in observations
    assert "event.event_status <> 'cancelled'" in observations

    assert "CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v6" in browse
    assert "LANGUAGE plpgsql" in browse
    assert "STABLE" in browse
    assert "SECURITY DEFINER" in browse
    assert "SET search_path = pg_catalog, public" in browse
    assert "p_source_key IS NULL" in browse
    assert "interval '370 days'" in browse
    assert "p_source_key IS NOT NULL" in browse
    assert "interval '7305 days'" in browse
    assert browse.index("p_after_start IS NULL") < browse.index("LIMIT p_limit")
    assert "ORDER BY eligible.start_at, eligible.canonical_event_id" in browse

    assert "CREATE FUNCTION public.fn_list_current_catalog_providers_v3" in providers
    assert "LEFT JOIN eligible_observations AS observation" in providers
    assert "count(DISTINCT observation.canonical_event_id)" in providers
    assert "HAVING" not in providers

    assert all("DROP FUNCTION" not in statement for statement in operations.statements)
    sql = "\n".join(operations.statements)
    for name in (
        "fn_list_admitted_catalog_browse_sources_v1",
        "fn_list_retained_catalog_browse_observations_v1",
        "fn_browse_filtered_current_catalog_events_v6",
        "fn_list_current_catalog_providers_v3",
    ):
        assert name in sql
        assert any(
            name in statement and "FROM PUBLIC" in statement for statement in operations.statements
        )
    for name in (
        "fn_browse_filtered_current_catalog_events_v6",
        "fn_list_current_catalog_providers_v3",
    ):
        assert any(
            name in statement and statement.endswith("TO ec_app")
            for statement in operations.statements
        )
    for name in (
        "fn_list_admitted_catalog_browse_sources_v1",
        "fn_list_retained_catalog_browse_observations_v1",
    ):
        assert not any(
            name in statement and statement.startswith("GRANT EXECUTE")
            for statement in operations.statements
        )


def test_downgrade_removes_only_0134_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    for name in (
        "fn_list_admitted_catalog_browse_sources_v1",
        "fn_list_retained_catalog_browse_observations_v1",
        "fn_browse_filtered_current_catalog_events_v6",
        "fn_list_current_catalog_providers_v3",
    ):
        assert f"DROP FUNCTION IF EXISTS public.{name}" in sql
    assert "fn_browse_filtered_current_catalog_events_v5" not in sql
    assert "fn_list_current_catalog_providers_v2" not in sql
    assert "DROP TABLE" not in sql
