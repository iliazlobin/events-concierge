from __future__ import annotations

from pathlib import Path


def test_city_facet_migration_uses_the_admitted_upcoming_catalog() -> None:
    path = Path("migrations/versions/0146_catalog_city_facets.py")
    sql = path.read_text()

    assert "fn_list_current_catalog_city_facets_v1" in sql
    assert "fn_list_retained_catalog_browse_observations_v1(NULL, NULL, NULL)" in sql
    assert "count(DISTINCT event.canonical_event_id)" in sql
    assert "nullif(btrim(event.city_norm), '') IS NOT NULL" in sql
    assert "LIMIT 500" in sql
    assert "GRANT EXECUTE" in sql
    assert "DROP FUNCTION IF EXISTS" in sql
