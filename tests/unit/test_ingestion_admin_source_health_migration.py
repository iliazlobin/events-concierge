"""Migration contract for the unpaginated, component-graded source health projection."""

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
        / "0172_ingestion_admin_source_health.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, str]:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    return migration, "\n".join(operations.statements)


def test_upgrade_chains_onto_the_current_head(monkeypatch: pytest.MonkeyPatch) -> None:
    migration, _ = _upgrade_sql(monkeypatch)

    assert migration.revision == "0172"
    assert migration.down_revision == "0171"


def test_upgrade_creates_the_projection_with_the_owner_defined_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    assert "CREATE FUNCTION public.fn_list_ingestion_admin_source_health_v1" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, public" in sql


def test_upgrade_returns_three_distinct_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    # Collapsing these is what hid a thirteen-day outage behind minutes-old activity.
    for column in ("last_attempt_at", "last_success_at", "last_catalog_change_at"):
        assert f"{column} timestamptz" in sql


def test_upgrade_grades_four_independent_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    for column in ("run_state", "freshness_state", "retry_state", "yield_state"):
        assert f"{column} text" in sql


def test_upgrade_keeps_paused_and_retired_sources_in_the_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    # The bug being fixed: sourceAttentionScore returns 0 for any disabled source, so twelve
    # paused/retired sources backing 1,547 upcoming events can never raise an alarm.
    assert "'retired'" in sql
    assert "'paused'" in sql
    assert "NOT graded.enabled" in sql
    assert "paused" in migration._HEALTH_TOKENS
    assert "retired" in migration._HEALTH_TOKENS
    # No WHERE clause may drop a source for being disabled.
    assert "WHERE source.enabled" not in sql


def test_upgrade_derives_freshness_from_each_source_own_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    # Clock arithmetic against the source's interval, not the last run's self-report.
    assert "3 * src.refresh_interval_minutes" in sql
    assert "1.5 * src.refresh_interval_minutes" in sql
    assert "INTERVAL '7 days'" in sql


def test_upgrade_treats_a_runaway_attempt_count_as_severe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    assert migration._RETRY_SEVERE_ATTEMPTS == 20
    assert f">= {migration._RETRY_SEVERE_ATTEMPTS} THEN 'severe'" in sql
    # A source that just succeeded but is burning thousands of attempts must still grade down.
    assert "graded.retry_state = 'severe' THEN 'down'" in sql


def test_upgrade_reports_blast_radius_and_orders_by_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    assert "upcoming_events bigint" in sql
    assert "ORDER BY graded.upcoming DESC" in sql


def test_upgrade_excludes_fixture_sources_and_fixture_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    assert "fn_ingestion_admin_source_is_fixture" in sql
    assert "NOT public.fn_ingestion_admin_run_is_fixture" in sql


def test_upgrade_adds_the_observation_index_and_grants_only_to_the_runtime_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    assert f"CREATE INDEX IF NOT EXISTS {migration._OBSERVATION_INDEX}" in sql
    qualified = "public.fn_list_ingestion_admin_source_health_v1(boolean)"
    assert f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC" in sql
    assert f"GRANT EXECUTE ON FUNCTION {qualified} TO ec_app" in sql


def test_downgrade_removes_both_the_projection_and_its_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_list_ingestion_admin_source_health_v1" in sql
    assert f"DROP INDEX IF EXISTS public.{migration._OBSERVATION_INDEX}" in sql
