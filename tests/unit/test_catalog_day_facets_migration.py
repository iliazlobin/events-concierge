"""Migration contract for single-scan catalog day facets."""

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
        Path(__file__).parents[2] / "migrations" / "versions" / "0153_catalog_day_facets.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, str]:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    return migration, "\n".join(operations.statements)


def test_upgrade_counts_days_from_one_unbounded_eligibility_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    assert migration.revision == "0153"
    assert migration.down_revision == "0152"
    assert "CREATE FUNCTION public.fn_list_catalog_day_facets_v1" in sql
    assert "start_day date" in sql
    assert "day_event_count bigint" in sql
    assert "topic_event_count bigint" in sql
    # One scan, aggregated in place: the calendar grid must never page a range.
    assert "LOOP" not in sql
    assert "CREATE TEMP TABLE" not in sql
    assert "p_after_start" not in sql
    assert "p_after_id" not in sql
    assert "p_limit" not in sql
    assert "GRANT EXECUTE" in sql


def test_upgrade_buckets_days_in_the_caller_time_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    # A calendar day is local wall clock. Bucketing in UTC would move a Pacific
    # evening event onto the following day for every viewer in that zone.
    assert "(eligible.start_at AT TIME ZONE p_time_zone)::date" in sql
    assert "pg_catalog.pg_timezone_names" in sql
    assert "p_time_zone !~ '^[A-Za-z0-9+_/-]{1,64}$'" in sql


def test_upgrade_applies_the_topic_selection_unlike_the_topic_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    # Topic facets count before topic selection so the filter composer can offer
    # unselected topics; the grid instead must agree with the agenda its own
    # filters produce.
    assert "event.topics @> coalesce(p_topics, '{}'::text[])" in sql


def test_upgrade_counts_untopiced_events_under_the_synthetic_other_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    assert "ARRAY['other']::text[]" in sql
    # Day totals are distinct events, so a multi-topic event is counted once.
    assert "count(DISTINCT day_event.canonical_event_id)" in sql


def test_upgrade_rejects_the_same_invalid_filters_the_page_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    assert "ERRCODE = '22023'" in sql
    assert "interval '370 days'" in sql
    assert "interval '7305 days'" in sql
    assert "p_price NOT IN ('free', 'paid', 'unknown')" in sql
    assert "p_price_max_cents NOT BETWEEN 1 AND 100000000" in sql
    assert "'bay_area', 'manhattan', 'los_angeles_area'" in sql


def test_upgrade_is_least_privilege_and_reversible(monkeypatch: pytest.MonkeyPatch) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, public" in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "TO ec_app" in sql

    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    migration.downgrade()
    downgrade_sql = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_list_catalog_day_facets_v1" in downgrade_sql
