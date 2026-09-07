"""Static migration contract for the provider-neutral enrichment control plane."""

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
        / "0133_provider_neutral_entity_enrichment.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_is_additive_disabled_and_identity_evidence_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0133"
    assert migration.down_revision == "0132"
    sql = "\n".join(operations.statements)
    assert "CREATE TABLE public.entity_enrichment_source_entities" in sql
    assert "CREATE TABLE public.entity_enrichment_source_facts" in sql
    assert "CREATE TABLE public.entity_enrichment_jobs" in sql
    assert "CREATE TABLE public.entity_enrichment_observations" in sql
    assert "identity_basis IN ('source_entity_id', 'source_profile_url')" in sql
    assert "lower(btrim(source_entity_key)) <> lower(btrim(display_name))" in sql
    assert "FOREIGN KEY (source_key, source, source_event_id)" in sql
    assert "REFERENCES public.catalog_event_observations" in sql
    assert "requested_entity_revision" in sql
    assert "attempt_count      integer NOT NULL" in sql
    assert "UNIQUE (job_id, attempt_count, field_name, provider_record_id)" in sql
    assert "FOREIGN KEY (job_id, entity_id)" in sql
    assert "lease_token" in sql
    assert "FOR UPDATE OF job SKIP LOCKED" in sql
    assert "provider_record_id" in sql
    assert "confidence_bps" in sql
    assert "review_status" in sql
    assert "raw_payload" not in sql

    # Licensed provider names are registry placeholders only and start fail-closed.
    assert "'linkedin_licensed'" in sql
    assert "'levels_licensed'" in sql
    assert "ARRAY['organization']::text[], false, false, NULL, NULL" in sql
    assert "NOT enabled" in sql
    assert "credentials_configured" in sql
    assert "terms_approved_at IS NOT NULL" in sql
    assert "operator_approved_at IS NOT NULL" in sql
    assert "CREATE TRIGGER" not in sql


def test_materialization_is_approved_current_and_preserves_direct_source_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    materializable = next(
        statement
        for statement in operations.statements
        if "CREATE FUNCTION public.fn_list_materializable_entity_enrichment" in statement
    )
    assert "observation.review_status = 'approved'" in materializable
    assert "observation.reviewed_at IS NOT NULL" in materializable
    assert "observation.expires_at > statement_timestamp()" in materializable
    assert "job.requested_entity_revision = entity.entity_revision" in materializable
    assert "job.status = 'succeeded'" in materializable
    assert "observation.attempt_count = job.attempt_count" in materializable
    assert "observation.field_name = 'profile_url'" in materializable
    assert "entity.direct_profile_url IS NOT NULL" in materializable
    assert "current_observation.last_run_key = fact.source_run_key" in materializable
    assert "DISTINCT ON (observation.field_name)" in materializable
    assert "pending" not in materializable


def test_observation_capability_rejects_name_only_and_unfenced_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    record = next(
        statement
        for statement in operations.statements
        if "CREATE FUNCTION public.fn_record_entity_enrichment_observation" in statement
    )
    assert "name_only" not in record
    assert "p_match_basis NOT IN" in record
    assert "'source_entity_id', 'source_profile_url'" in record
    assert "'provider_crosswalk', 'manual_review'" in record
    assert "lower(btrim(p_provider_record_id)) = lower(btrim(v_entity.display_name))" in record
    assert "v_job.lease_token IS DISTINCT FROM p_lease_token" in record
    assert "v_job.lease_expires_at <= clock_timestamp()" in record
    assert "v_job.requested_entity_revision <> v_entity.entity_revision" in record
    assert "FOR UPDATE OF job" in record
    assert "observation.job_id = p_job_id" in record
    assert "observation.provider_record_id = p_provider_record_id" in record
    assert "observation.attempt_count = v_job.attempt_count" in record
    assert "v_entity.entity_kind <> ALL(v_provider.allowed_entity_kinds)" in record
    assert "review_status" not in record.partition("INSERT INTO")[0]


def test_tables_are_not_directly_available_and_capabilities_are_narrowly_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    sql = "\n".join(operations.statements)
    for table in (
        "entity_enrichment_providers",
        "entity_enrichment_source_entities",
        "entity_enrichment_source_facts",
        "entity_enrichment_jobs",
        "entity_enrichment_observations",
    ):
        assert f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, ec_app" in sql
    application_functions = (
        "fn_upsert_entity_enrichment_source_fact",
        "fn_enqueue_entity_enrichment_job",
        "fn_claim_entity_enrichment_jobs",
        "fn_renew_entity_enrichment_job_lease",
        "fn_record_entity_enrichment_observation",
        "fn_complete_entity_enrichment_job",
        "fn_release_entity_enrichment_job",
        "fn_list_materializable_entity_enrichment",
    )
    for function_name in application_functions:
        assert any(
            function_name in statement and statement.endswith("TO ec_app")
            for statement in operations.statements
        )
        assert any(
            function_name in statement and statement.endswith("FROM PUBLIC")
            for statement in operations.statements
        )
    assert not any(
        "fn_review_entity_enrichment_observation" in statement
        and statement.endswith("TO ec_app")
        for statement in operations.statements
    )
    assert any(
        "fn_review_entity_enrichment_observation" in statement
        and statement.endswith("FROM PUBLIC")
        for statement in operations.statements
    )


def test_capabilities_handle_nulls_monotonicity_and_provider_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    sql = "\n".join(operations.statements)
    for nullable_guard in (
        "p_identity_basis IS NULL",
        "p_role IS NULL",
        "p_entity_kind IS NULL",
        "p_confidence_bps IS NULL",
        "p_provider_key IS NULL",
        "p_error_code IS NULL",
        "p_field_name IS NULL",
        "p_match_basis IS NULL",
        "p_decision IS NULL",
    ):
        assert nullable_guard in sql
    assert "p_observed_at >= v_entity.last_observed_at" in sql
    assert "p_observed_at >= v_fact.observed_at" in sql
    assert "verification_status = 'source_asserted'" not in next(
        statement
        for statement in operations.statements
        if "CREATE FUNCTION public.fn_upsert_entity_enrichment_source_fact" in statement
    ).partition("UPDATE public.entity_enrichment_source_facts")[2]
    assert "SET status = 'failed', error_code = 'policy_blocked'" in sql
    assert "provider.enabled" in sql
    assert "provider.credentials_configured" in sql
    assert "p_error_code IN (" in sql


def test_downgrade_removes_only_0133_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "DROP TABLE IF EXISTS public.entity_enrichment_observations" in sql
    assert "DROP TABLE IF EXISTS public.entity_enrichment_source_entities" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_entity_enrichment_direct_profile_valid" in sql
    assert "catalog_event_observations" not in sql
    assert "canonical_events" not in sql
