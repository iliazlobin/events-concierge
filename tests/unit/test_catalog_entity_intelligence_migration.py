"""Contract coverage for source-backed catalog entity intelligence."""

from pathlib import Path


def test_entity_intelligence_migration_is_bounded_and_provenance_backed() -> None:
    sql = Path("migrations/versions/0150_catalog_entity_intelligence.py").read_text()

    assert "CREATE TABLE public.catalog_entity_external_sources" in sql
    assert "CREATE TABLE public.catalog_entity_external_facts" in sql
    assert "fn_replace_catalog_entity_external_source_v1" in sql
    assert "jsonb_array_length(coalesce(p_facts, '[]'::jsonb)) > 100" in sql
    assert "source_url ~ '^https://" in sql
    assert "fact_url ~ '^https://" in sql
    assert 'for table in ("catalog_entity_external_sources", "catalog_entity_external_facts")' in sql
    assert "REVOKE ALL ON TABLE public.{table} FROM PUBLIC, ec_app" in sql
    assert "_restore_entity_search()" in sql


def test_entity_directory_searches_structured_facts_without_raw_payloads() -> None:
    sql = Path("migrations/versions/0150_catalog_entity_intelligence.py").read_text()

    assert "ix_catalog_entity_external_facts_search" in sql
    assert "fact.search_document @@ plainto_tsquery('simple', p_query)" in sql
    assert "raw_payload" not in sql
