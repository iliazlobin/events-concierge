"""Focused SQL-boundary tests for the provider-neutral enrichment repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

import events_concierge.adapters.postgres.entity_enrichment as repository_module
from events_concierge.adapters.postgres.entity_enrichment import (
    PostgresEntityEnrichmentRepository,
)
from events_concierge.domain.entity_enrichment import (
    EntityEnrichmentEnqueueOutcome,
    EntityEnrichmentField,
    EntityEnrichmentLease,
    EntityEnrichmentMatchBasis,
    EntityFieldObservationDraft,
    EntityIdentityBasis,
    EntityKind,
    EntityRole,
    SourceEntityFactInput,
)
from events_concierge.ports.entity_enrichment import (
    EntityEnrichmentInvariantError,
    EntityEnrichmentSourceFactNotFoundError,
)

_NOW = datetime(2026, 7, 31, 18, tzinfo=UTC)


class _Result:
    def __init__(
        self,
        *,
        row: object | None = None,
        rows: list[object] | None = None,
        scalar: object | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or []
        self._scalar = scalar

    def one(self) -> object:
        assert self._row is not None
        return self._row

    def all(self) -> list[object]:
        return self._rows

    def scalar_one(self) -> object:
        assert self._scalar is not None
        return self._scalar


class _Session:
    def __init__(self, *results: _Result) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, statement: object, parameters: dict[str, object]) -> _Result:
        self.calls.append((str(statement), parameters))
        assert self.results
        return self.results.pop(0)


class _SessionScope:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


def _install_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    monkeypatch.setattr(
        repository_module,
        "system_session_scope",
        lambda: _SessionScope(session),
    )


def _fact() -> SourceEntityFactInput:
    return SourceEntityFactInput(
        source_key="luma-sf",
        source="luma_discover",
        source_event_id="event-42",
        source_run_key="cadence:luma-sf:20260731T180000Z",
        source_entity_key="host_ada_42",
        identity_basis=EntityIdentityBasis.SOURCE_ENTITY_ID,
        role=EntityRole.HOST,
        entity_kind=EntityKind.PERSON,
        display_name="Ada Host",
        direct_profile_url="https://www.linkedin.com/in/ada-host",
        confidence_bps=10_000,
        observed_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
    )


def _lease() -> EntityEnrichmentLease:
    return EntityEnrichmentLease(
        job_id=uuid4(),
        entity_id=uuid4(),
        provider_key="linkedin_licensed",
        requested_entity_revision=2,
        attempt_count=1,
        lease_token=uuid4(),
        source_key="luma-sf",
        source_entity_key="host_ada_42",
        identity_basis=EntityIdentityBasis.SOURCE_ENTITY_ID,
        entity_kind=EntityKind.PERSON,
        display_name="Ada Host",
        direct_profile_url="https://www.linkedin.com/in/ada-host",
    )


@pytest.mark.asyncio
async def test_upsert_source_fact_maps_exact_anchor_without_provider_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = uuid4()
    fact_id = uuid4()
    session = _Session(
        _Result(
            row=SimpleNamespace(
                outcome="inserted",
                entity_id=entity_id,
                fact_id=fact_id,
                entity_revision=1,
            )
        )
    )
    _install_session(monkeypatch, session)

    result = await PostgresEntityEnrichmentRepository().upsert_source_fact(_fact())

    assert result.entity_id == entity_id
    assert result.fact_id == fact_id
    sql, parameters = session.calls[0]
    assert "fn_upsert_entity_enrichment_source_fact" in sql
    assert parameters["source_event_id"] == "event-42"
    assert parameters["source_run_key"] == "cadence:luma-sf:20260731T180000Z"
    assert parameters["source_entity_key"] == "host_ada_42"
    assert "raw_payload" not in parameters


@pytest.mark.asyncio
async def test_upsert_source_fact_translates_missing_exact_source_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        _Result(
            row=SimpleNamespace(
                outcome="not_found",
                entity_id=None,
                fact_id=None,
                entity_revision=None,
            )
        )
    )
    _install_session(monkeypatch, session)

    with pytest.raises(EntityEnrichmentSourceFactNotFoundError):
        await PostgresEntityEnrichmentRepository().upsert_source_fact(_fact())


@pytest.mark.asyncio
async def test_enqueue_preserves_disabled_provider_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(_Result(scalar="provider_disabled"))
    _install_session(monkeypatch, session)

    outcome = await PostgresEntityEnrichmentRepository().enqueue(
        uuid4(),
        uuid4(),
        "linkedin_licensed",
        "operator@example.test",
    )

    assert outcome is EntityEnrichmentEnqueueOutcome.PROVIDER_DISABLED
    assert session.calls[0][1]["provider_key"] == "linkedin_licensed"


@pytest.mark.asyncio
async def test_claim_maps_exact_non_name_identity_and_enforces_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _lease()
    session = _Session(
        _Result(
            rows=[
                SimpleNamespace(
                    job_id=lease.job_id,
                    entity_id=lease.entity_id,
                    provider_key=lease.provider_key,
                    requested_entity_revision=lease.requested_entity_revision,
                    attempt_count=lease.attempt_count,
                    lease_token=lease.lease_token,
                    source_key=lease.source_key,
                    source_entity_key=lease.source_entity_key,
                    identity_basis=lease.identity_basis.value,
                    entity_kind=lease.entity_kind.value,
                    display_name=lease.display_name,
                    direct_profile_url=lease.direct_profile_url,
                )
            ]
        )
    )
    _install_session(monkeypatch, session)
    repository = PostgresEntityEnrichmentRepository()

    claimed = await repository.claim_batch(1, 60)

    assert claimed == [lease]
    assert session.calls[0][1] == {"limit": 1, "lease_seconds": 60}
    with pytest.raises(ValueError, match="between 1 and 100"):
        await repository.claim_batch(0, 60)
    with pytest.raises(ValueError, match="between 30 and 3600"):
        await repository.claim_batch(1, 29)


@pytest.mark.asyncio
async def test_observation_write_passes_only_bounded_typed_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _lease()
    observation = EntityFieldObservationDraft(
        observation_id=uuid4(),
        field=EntityEnrichmentField.JOB_TITLE,
        value="Principal Engineer",
        provenance_url="https://licensed.example/records/opaque-42",
        provider_record_id="opaque-42",
        match_basis=EntityEnrichmentMatchBasis.PROVIDER_CROSSWALK,
        confidence_bps=9_200,
        observed_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
    )
    session = _Session(_Result(scalar="recorded"))
    _install_session(monkeypatch, session)

    outcome = await PostgresEntityEnrichmentRepository().record_observation(
        lease,
        observation,
    )

    assert outcome.value == "recorded"
    sql, parameters = session.calls[0]
    assert "fn_record_entity_enrichment_observation" in sql
    assert parameters["lease_token"] == lease.lease_token
    assert parameters["field_name"] == "job_title"
    assert parameters["provider_record_id"] == "opaque-42"
    assert "raw_payload" not in parameters
    assert "display_name" not in parameters


@pytest.mark.asyncio
async def test_materializable_mapping_rejects_invalid_review_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row: Any = SimpleNamespace(
        observation_id=uuid4(),
        fact_id=uuid4(),
        entity_id=uuid4(),
        role="host",
        field_name="job_title",
        field_value="Principal Engineer",
        provider_key="linkedin_licensed",
        provenance_url="https://licensed.example/records/opaque-42",
        confidence_bps=9_200,
        reviewed_at="not-a-datetime",
        expires_at=None,
    )
    session = _Session(_Result(rows=[row]))
    _install_session(monkeypatch, session)

    with pytest.raises(EntityEnrichmentInvariantError, match="review time"):
        await PostgresEntityEnrichmentRepository().list_materializable(uuid4())


def test_uuid_types_remain_opaque_identifiers() -> None:
    assert isinstance(_lease().entity_id, UUID)
