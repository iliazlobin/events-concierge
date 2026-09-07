"""Static contract for the searchable event-entity projection."""

from __future__ import annotations

from pathlib import Path


def test_entity_catalog_is_identity_fenced_indexed_and_capability_only() -> None:
    sql = Path("migrations/versions/0147_catalog_entities.py").read_text()

    assert "CREATE TABLE public.catalog_entities" in sql
    assert "CREATE TABLE public.catalog_entity_event_mentions" in sql
    assert "CREATE TABLE public.catalog_entity_source_links" in sql
    assert "profile_verified" in sql
    assert "source_scoped" in sql
    assert "profile:' || md5(lower(profile ->> 'profile_url'))" in sql
    assert "source:' || md5(" in sql
    assert "fn_resolve_catalog_event_entity_v1" in sql
    assert "fn_list_catalog_entities_v1" in sql
    assert "fn_list_catalog_entity_events_v1" in sql
    assert "REVOKE ALL ON TABLE" in sql
    assert "GRANT EXECUTE" in sql


def test_entity_quality_gate_is_applied_after_every_projection_refresh() -> None:
    first_gate = Path(
        "migrations/versions/0148_catalog_entity_quality_gate.py"
    ).read_text()
    strict_gate = Path(
        "migrations/versions/0149_catalog_speaker_name_quality.py"
    ).read_text()
    commit = Path(
        "src/events_concierge/adapters/postgres/catalog_refresh_commit.py"
    ).read_text()
    promotion = Path(
        "src/events_concierge/adapters/postgres/catalog_paged_promotion.py"
    ).read_text()

    assert "identity_status = 'source_scoped'" in first_gate
    assert "mention.role = 'speaker'" in first_gate
    assert "mention.observed_name ~ '[[:digit:]]'" in first_gate
    assert "SELECT public.fn_prune_catalog_entity_index_v1(NULL)" in first_gate
    assert "CREATE OR REPLACE FUNCTION" in strict_gate
    assert "OR NOT (" in strict_gate
    assert "Founder|Co[- ]?Founder|CEO|CTO" in strict_gate
    assert "SELECT public.fn_prune_catalog_entity_index_v1(NULL)" in strict_gate
    history = Path(
        "migrations/versions/0152_entity_history_projection_and_insights.py"
    ).read_text()

    assert commit.index("fn_refresh_catalog_entity_index_v3") < commit.index(
        "fn_prune_catalog_entity_index_v1"
    )
    assert promotion.index("fn_refresh_catalog_entity_index_v3") < promotion.index(
        "fn_prune_catalog_entity_index_v1"
    )
    # A projection rebuild inside a migration is subject to the same gate as the runtime paths.
    assert history.index("fn_refresh_catalog_entity_index_v2(NULL)") < history.index(
        "fn_prune_catalog_entity_index_v1(NULL)"
    )
