"""Adversarial domain tests for provider-neutral entity enrichment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from events_concierge.domain.entity_enrichment import (
    EntityEnrichmentField,
    EntityEnrichmentLease,
    EntityEnrichmentMatchBasis,
    EntityFieldObservationDraft,
    EntityIdentityBasis,
    EntityKind,
    EntityObservationReviewStatus,
    EntityRole,
    SourceEntityFactInput,
    direct_entity_profile_url,
    observation_is_materializable,
)

NOW = datetime(2026, 7, 31, 18, tzinfo=UTC)


def _fact(**overrides: object) -> SourceEntityFactInput:
    values: dict[str, object] = {
        "source_key": "luma-sf",
        "source": "luma_discover",
        "source_event_id": "event-42",
        "source_run_key": "cadence:luma-sf:20260731T180000Z",
        "source_entity_key": "host_ada_42",
        "identity_basis": EntityIdentityBasis.SOURCE_ENTITY_ID,
        "role": EntityRole.HOST,
        "entity_kind": EntityKind.PERSON,
        "display_name": "Ada Host",
        "direct_profile_url": "https://www.linkedin.com/in/ada-host",
        "confidence_bps": 10_000,
        "observed_at": NOW,
        "expires_at": NOW + timedelta(days=30),
    }
    values.update(overrides)
    return SourceEntityFactInput(**values)  # type: ignore[arg-type]


def test_source_fact_requires_non_name_identity_and_direct_role_compatible_profile() -> None:
    fact = _fact()
    assert fact.source_entity_key == "host_ada_42"
    assert fact.direct_profile_url == "https://www.linkedin.com/in/ada-host"

    with pytest.raises(ValueError, match="cannot be a display name"):
        _fact(source_entity_key="Ada Host")
    with pytest.raises(ValueError, match="direct profile"):
        _fact(direct_profile_url="https://www.linkedin.com/search/results/people")
    with pytest.raises(ValueError, match="direct profile"):
        _fact(direct_profile_url="https://www.linkedin.com/company/ada-host")
    with pytest.raises(ValueError, match="direct HTTPS"):
        _fact(direct_profile_url="https://www.linkedin.com/in/ada-host?trk=guess")


def test_profile_identity_basis_must_equal_the_source_direct_url() -> None:
    profile = "https://www.linkedin.com/company/example-org"
    fact = _fact(
        source_entity_key=profile,
        identity_basis=EntityIdentityBasis.SOURCE_PROFILE_URL,
        entity_kind=EntityKind.ORGANIZATION,
        display_name="Example Org",
        direct_profile_url=profile,
    )
    assert fact.source_entity_key == fact.direct_profile_url

    with pytest.raises(ValueError, match="must equal"):
        _fact(
            source_entity_key="org-42",
            identity_basis=EntityIdentityBasis.SOURCE_PROFILE_URL,
            entity_kind=EntityKind.ORGANIZATION,
            display_name="Example Org",
            direct_profile_url=profile,
        )


def test_observation_is_field_bounded_and_never_accepts_search_profile_urls() -> None:
    observation = EntityFieldObservationDraft(
        observation_id=uuid4(),
        field=EntityEnrichmentField.JOB_TITLE,
        value="Principal Engineer",
        provenance_url="https://provider.example/records/opaque-42",
        provider_record_id="opaque-provider-42",
        match_basis=EntityEnrichmentMatchBasis.PROVIDER_CROSSWALK,
        confidence_bps=9_500,
        observed_at=NOW,
        expires_at=NOW + timedelta(days=90),
    )
    assert observation.value == "Principal Engineer"
    assert "payload" not in observation.__dataclass_fields__

    with pytest.raises(ValueError, match="direct HTTPS"):
        EntityFieldObservationDraft(
            observation_id=uuid4(),
            field=EntityEnrichmentField.PROFILE_URL,
            value="https://www.linkedin.com/in/ada-host?trk=search",
            provenance_url="https://provider.example/records/opaque-42",
            provider_record_id="opaque-provider-42",
            match_basis=EntityEnrichmentMatchBasis.PROVIDER_CROSSWALK,
            confidence_bps=9_500,
            observed_at=NOW,
        )


def test_materialization_requires_approval_current_evidence_and_source_profile_precedence() -> None:
    future = NOW + timedelta(days=1)
    past = NOW - timedelta(seconds=1)
    assert observation_is_materializable(
        review_status=EntityObservationReviewStatus.APPROVED,
        entity_kind=EntityKind.PERSON,
        field=EntityEnrichmentField.JOB_TITLE,
        direct_profile_url=None,
        expires_at=future,
        now=NOW,
    )
    assert not observation_is_materializable(
        review_status=EntityObservationReviewStatus.PENDING,
        entity_kind=EntityKind.PERSON,
        field=EntityEnrichmentField.JOB_TITLE,
        direct_profile_url=None,
        expires_at=future,
        now=NOW,
    )
    assert not observation_is_materializable(
        review_status=EntityObservationReviewStatus.APPROVED,
        entity_kind=EntityKind.PERSON,
        field=EntityEnrichmentField.JOB_TITLE,
        direct_profile_url=None,
        expires_at=past,
        now=NOW,
    )
    assert not observation_is_materializable(
        review_status=EntityObservationReviewStatus.APPROVED,
        entity_kind=EntityKind.PERSON,
        field=EntityEnrichmentField.PROFILE_URL,
        direct_profile_url="https://www.linkedin.com/in/source-authoritative",
        expires_at=future,
        now=NOW,
    )
    assert not observation_is_materializable(
        review_status=EntityObservationReviewStatus.APPROVED,
        entity_kind=EntityKind.ORGANIZATION,
        field=EntityEnrichmentField.ORGANIZATION_PROFILE_URL,
        direct_profile_url="https://www.linkedin.com/company/source-authoritative",
        expires_at=future,
        now=NOW,
    )


def test_claimed_job_cannot_carry_name_only_identity() -> None:
    with pytest.raises(ValueError, match="name-only"):
        EntityEnrichmentLease(
            job_id=uuid4(),
            entity_id=uuid4(),
            provider_key="licensed_linkedin",
            requested_entity_revision=1,
            attempt_count=1,
            lease_token=uuid4(),
            source_key="luma-sf",
            source_entity_key="Ada Host",
            identity_basis=EntityIdentityBasis.SOURCE_ENTITY_ID,
            entity_kind=EntityKind.PERSON,
            display_name="Ada Host",
            direct_profile_url=None,
        )


def test_direct_profile_helper_allows_explicit_org_site_but_not_person_site() -> None:
    assert direct_entity_profile_url(
        "https://example.org/team",
        EntityKind.ORGANIZATION,
    ) == "https://example.org/team"
    with pytest.raises(ValueError, match="person profiles"):
        direct_entity_profile_url("https://example.org/ada", EntityKind.PERSON)
