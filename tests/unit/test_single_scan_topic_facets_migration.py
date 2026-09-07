"""Migration contract for single-scan catalog topic facets."""

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
        / "0151_single_scan_catalog_topic_facets.py"
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


def test_upgrade_aggregates_one_unbounded_eligibility_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    assert migration.revision == "0151"
    assert migration.down_revision == "0150"
    assert "CREATE FUNCTION public.fn_list_catalog_topic_facets_v2" in sql
    assert "RETURNS TABLE (topic text, event_count bigint)" in sql
    # One scan, aggregated in place: no paging loop, no keyset cursor, no temp relation.
    assert "LOOP" not in sql
    assert "CREATE TEMP TABLE" not in sql
    assert "p_after_start" not in sql
    assert "p_after_id" not in sql
    assert "p_limit" not in sql
    assert "fn_browse_filtered_current_catalog_events_v6" not in sql
    assert "GRANT EXECUTE" in sql


def test_upgrade_reuses_the_v6_eligibility_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The facet scan must stay filter-identical to the browse page beside it."""
    _, sql = _upgrade_sql(monkeypatch)

    assert "fn_list_retained_catalog_browse_observations_v1" in sql
    assert "eligible_events AS MATERIALIZED" in sql
    assert "catalog browse query is invalid" in sql
    # Window, query, city, scope, and price rules all still gate the count.
    assert "interval '370 days'" in sql
    assert "interval '7305 days'" in sql
    assert "length(coalesce(p_query, '')) > 160" in sql
    assert "'bay_area'" in sql and "'manhattan'" in sql and "'los_angeles_area'" in sql
    assert "event.price_status = p_price" in sql
    assert "event.price_max_cents <= p_price_max_cents" in sql
    # Counts stay distinct per canonical event and are grouped by topic only.
    assert "count(DISTINCT eligible.canonical_event_id)" in sql
    assert "unnest(event.topics) AS selected(topic)" in sql
    assert "GROUP BY selected.topic" in sql


def test_downgrade_drops_only_the_new_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_list_catalog_topic_facets_v2" in sql
    # v1 keeps serving the pre-0151 repository after a rollback.
    assert "fn_list_catalog_topic_facets_v1" not in sql
