"""Static contract for history-covering entity projection and catalog-derived insights."""

from __future__ import annotations

from pathlib import Path

_MIGRATION = Path("migrations/versions/0152_entity_history_projection_and_insights.py")


def test_projection_covers_retained_history_without_dropping_the_live_window() -> None:
    sql = _MIGRATION.read_text()

    assert "CREATE FUNCTION public.fn_refresh_catalog_entity_index_v2" in sql
    # The live call must survive verbatim: an elapsed-event window alone would drop every
    # upcoming appearance, and the retained capability rejects a window wider than its ceiling.
    assert "p_source_key, NULL, NULL" in sql
    assert "statement_timestamp() - interval '365 days'" in sql
    assert sql.index("p_source_key, NULL, NULL") < sql.index(
        "statement_timestamp() - interval '365 days'"
    )
    assert "UNION ALL" in sql


def test_history_window_stays_inside_the_unscoped_observation_ceiling() -> None:
    sql = _MIGRATION.read_text()

    # fn_list_retained_catalog_browse_observations_v1 raises on an unscoped window wider than
    # 370 days, and the full rebuild passes p_source_key = NULL.
    assert "interval '370 days'" not in sql
    assert "interval '400 days'" not in sql
    assert "interval '365 days'" in sql


def test_entity_events_expose_temporal_position_and_order_upcoming_first() -> None:
    sql = _MIGRATION.read_text()

    assert "CREATE FUNCTION public.fn_list_catalog_entity_events_v2" in sql
    assert "is_past boolean" in sql
    assert "<= statement_timestamp() AS is_past" in sql
    assert "ORDER BY mentioned.is_past" in sql


def _insights_body() -> str:
    return _MIGRATION.read_text().split(
        "CREATE FUNCTION public.fn_get_catalog_entity_insights_v1"
    )[1]


def test_insights_are_catalog_derived_and_cover_name_only_entities() -> None:
    body = _insights_body()

    # Insights read admitted mentions and canonical events only.  No enrichment table, no
    # provider identity, and therefore no identity_status precondition: a source_scoped entity
    # the enrichment plane will never research still gets its own catalog evidence.
    assert "public.catalog_entity_event_mentions" in body
    assert "public.canonical_events" in body
    assert "catalog_entity_external_facts" not in body
    assert "catalog_entity_external_sources" not in body
    assert "identity_status" not in body


def test_cadence_is_measured_over_a_bounded_activity_window() -> None:
    # The catalog holds start dates decades out; a whole-span rate would round to zero.
    body = _insights_body()
    assert "v_window_start timestamptz := statement_timestamp() - interval '365 days'" in body
    assert "v_window_end timestamptz := statement_timestamp() + interval '90 days'" in body
    assert "greatest(cadence.active_months, 1)" in body


def test_capabilities_are_revoked_from_public_and_granted_to_the_app_role() -> None:
    sql = _MIGRATION.read_text()

    assert 'op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")' in sql
    assert 'op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")' in sql
    for signature in (
        "public.fn_refresh_catalog_entity_index_v2(text)",
        "public.fn_list_catalog_entity_events_v2(uuid,integer)",
        "public.fn_get_catalog_entity_insights_v1(uuid)",
    ):
        assert signature in sql


def test_downgrade_drops_the_new_capabilities_and_restores_the_live_projection() -> None:
    sql = _MIGRATION.read_text()
    downgrade = sql.split("def downgrade()")[1].split("def _create_refresh_v2")[0]

    assert "DROP FUNCTION IF EXISTS" in downgrade
    assert "fn_refresh_catalog_entity_index_v1(NULL)" in downgrade
    assert "fn_prune_catalog_entity_index_v1(NULL)" in downgrade
