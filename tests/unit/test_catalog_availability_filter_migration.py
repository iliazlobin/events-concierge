"""Contract checks for the registration-availability browse capability."""

from __future__ import annotations

from pathlib import Path

import pytest

from events_concierge.adapters.postgres.catalog import _validated_catalog_filter_inputs
from events_concierge.api.app import _catalog_filter_scope

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "0179_catalog_registration_availability_filter.py"
)


def _scope(availability: str | None) -> str:
    return _catalog_filter_scope(
        source_keys=(),
        starts_after=None,
        starts_before=None,
        query=None,
        cities=(),
        location_scopes=(),
        price=None,
        price_max_cents=None,
        price_min_cents=None,
        topics=(),
        availability=availability,
    )


def test_availability_is_bound_into_catalog_cursors() -> None:
    assert _scope(None) != _scope("available")
    assert _scope("available") != _scope("sold_out")


def test_repository_rejects_unknown_availability_tokens() -> None:
    with pytest.raises(ValueError, match="availability"):
        _validated_catalog_filter_inputs(
            query=None,
            city_filters=(),
            location_scopes=(),
            price=None,
            price_max_cents=None,
            topics=(),
            availability="maybe",
        )


def test_migration_filters_before_paging_and_keeps_aggregates_aligned() -> None:
    sql = MIGRATION.read_text()

    assert "fn_browse_filtered_current_catalog_events_v10" in sql
    assert "fn_list_catalog_topic_facets_v4" in sql
    assert "fn_list_catalog_day_facets_v3" in sql
    assert "candidate.registration_status = 'open'" in sql
    assert "candidate.registration_status = 'sold_out'" in sql
    assert "WITH eligible_rows AS MATERIALIZED" in sql
    assert sql.index("WITH eligible_rows AS MATERIALIZED") < sql.index(
        "page AS MATERIALIZED"
    )
    assert "p_limit IS NULL OR p_limit < 1 OR p_limit > 101" in sql
    assert "REVOKE ALL ON FUNCTION {_BROWSE_UNBOUNDED_V1} FROM PUBLIC" in sql
