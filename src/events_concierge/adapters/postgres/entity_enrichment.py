"""PostgreSQL adapter for the dormant, reviewed entity-enrichment control plane."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import text

from ...domain.entity_enrichment import (
    EntityEnrichmentEnqueueOutcome,
    EntityEnrichmentFailureCode,
    EntityEnrichmentField,
    EntityEnrichmentJobStatus,
    EntityEnrichmentLease,
    EntityFieldObservationDraft,
    EntityIdentityBasis,
    EntityKind,
    EntityObservationWriteOutcome,
    EntityRole,
    MaterializableEntityObservation,
    SourceEntityFact,
    SourceEntityFactInput,
)
from ...infra.db import system_session_scope
from ...ports.entity_enrichment import (
    EntityEnrichmentConflictError,
    EntityEnrichmentInvariantError,
    EntityEnrichmentSourceFactNotFoundError,
)

_PROVIDER_KEY = re.compile(r"[a-z][a-z0-9_]{1,79}")
_MAX_BATCH = 100
_MIN_LEASE_SECONDS = 30
_MAX_LEASE_SECONDS = 3_600
_MAX_RETRY_SECONDS = 86_400
_MAX_ACTOR_LENGTH = 256
_CONTROL_CODEPOINT_MAX = 31
_DELETE_CODEPOINT = 127


class _SourceFactRow(Protocol):
    outcome: str
    entity_id: UUID
    fact_id: UUID
    entity_revision: int


class _LeaseRow(Protocol):
    job_id: UUID
    entity_id: UUID
    provider_key: str
    requested_entity_revision: int
    attempt_count: int
    lease_token: UUID
    source_key: str
    source_entity_key: str
    identity_basis: str
    entity_kind: str
    display_name: str
    direct_profile_url: str | None


class _MaterializableRow(Protocol):
    observation_id: UUID
    fact_id: UUID
    entity_id: UUID
    role: str
    field_name: str
    field_value: str
    provider_key: str
    provenance_url: str
    confidence_bps: int
    reviewed_at: datetime
    expires_at: datetime | None


def _actor(value: str, label: str) -> str:
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > _MAX_ACTOR_LENGTH
        or any(
            ord(character) <= _CONTROL_CODEPOINT_MAX
            or ord(character) == _DELETE_CODEPOINT
            for character in normalized
        )
    ):
        raise ValueError(f"{label} is invalid")
    return normalized


def _provider(value: str) -> str:
    if not _PROVIDER_KEY.fullmatch(value):
        raise ValueError("entity enrichment provider key is invalid")
    return value


class PostgresEntityEnrichmentRepository:
    """Use owner-defined capabilities; direct tables and provider calls remain unavailable."""

    async def upsert_source_fact(self, fact: SourceEntityFactInput) -> SourceEntityFact:
        async with system_session_scope() as session:
            row = cast(
                _SourceFactRow,
                (
                    await session.execute(
                        text(
                            """SELECT outcome, entity_id, fact_id, entity_revision
                               FROM public.fn_upsert_entity_enrichment_source_fact(
                                   :source_key, :source, :source_event_id, :source_run_key,
                                   :source_entity_key, :identity_basis, :role, :entity_kind,
                                   :display_name, :direct_profile_url, :confidence_bps,
                                   :observed_at, :expires_at
                               )"""
                        ),
                        {
                            "source_key": fact.source_key,
                            "source": fact.source,
                            "source_event_id": fact.source_event_id,
                            "source_run_key": fact.source_run_key,
                            "source_entity_key": fact.source_entity_key,
                            "identity_basis": fact.identity_basis.value,
                            "role": fact.role.value,
                            "entity_kind": fact.entity_kind.value,
                            "display_name": fact.display_name,
                            "direct_profile_url": fact.direct_profile_url,
                            "confidence_bps": fact.confidence_bps,
                            "observed_at": fact.observed_at,
                            "expires_at": fact.expires_at,
                        },
                    )
                ).one(),
            )
        outcome = str(row.outcome)
        if outcome == "not_found":
            raise EntityEnrichmentSourceFactNotFoundError(
                "source entity fact has no exact catalog observation/run anchor"
            )
        if outcome == "conflict":
            raise EntityEnrichmentConflictError("source entity identity conflicts")
        if outcome == "invalid":
            raise ValueError("source entity fact was rejected")
        if outcome not in {"inserted", "updated", "replayed"}:
            raise EntityEnrichmentInvariantError(
                "source entity fact capability returned an invalid outcome"
            )
        return SourceEntityFact(
            entity_id=row.entity_id,
            fact_id=row.fact_id,
            entity_revision=int(row.entity_revision),
            value=fact,
        )

    async def enqueue(
        self,
        job_id: UUID,
        entity_id: UUID,
        provider_key: str,
        requested_by: str,
    ) -> EntityEnrichmentEnqueueOutcome:
        provider = _provider(provider_key)
        actor = _actor(requested_by, "entity enrichment requester")
        async with system_session_scope() as session:
            outcome = str(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_enqueue_entity_enrichment_job(
                                   :job_id, :entity_id, :provider_key, :requested_by
                               )"""
                        ),
                        {
                            "job_id": job_id,
                            "entity_id": entity_id,
                            "provider_key": provider,
                            "requested_by": actor,
                        },
                    )
                ).scalar_one()
            )
        if outcome == "invalid":
            raise ValueError("entity enrichment job was rejected")
        try:
            return EntityEnrichmentEnqueueOutcome(outcome)
        except ValueError as error:
            raise EntityEnrichmentInvariantError(
                "entity enrichment enqueue capability returned an invalid outcome"
            ) from error

    async def claim_batch(
        self,
        limit: int,
        lease_seconds: int,
    ) -> list[EntityEnrichmentLease]:
        self._lease_bounds(limit, lease_seconds)
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """SELECT job_id, entity_id, provider_key,
                                  requested_entity_revision, attempt_count, lease_token,
                                  source_key, source_entity_key, identity_basis, entity_kind,
                                  display_name, direct_profile_url
                           FROM public.fn_claim_entity_enrichment_jobs(
                               :limit, :lease_seconds
                           )"""
                    ),
                    {"limit": limit, "lease_seconds": lease_seconds},
                )
            ).all()
        return [self._lease(cast(_LeaseRow, row)) for row in rows]

    async def renew_lease(
        self,
        lease: EntityEnrichmentLease,
        lease_seconds: int,
    ) -> bool:
        self._lease_bounds(1, lease_seconds)
        async with system_session_scope() as session:
            return bool(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_renew_entity_enrichment_job_lease(
                                   :job_id, :lease_token, :lease_seconds
                               )"""
                        ),
                        {
                            "job_id": lease.job_id,
                            "lease_token": lease.lease_token,
                            "lease_seconds": lease_seconds,
                        },
                    )
                ).scalar_one()
            )

    async def record_observation(
        self,
        lease: EntityEnrichmentLease,
        observation: EntityFieldObservationDraft,
    ) -> EntityObservationWriteOutcome:
        async with system_session_scope() as session:
            outcome = str(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_record_entity_enrichment_observation(
                                   :job_id, :lease_token, :observation_id, :field_name,
                                   :field_value, :provenance_url, :provider_record_id,
                                   :match_basis, :confidence_bps, :observed_at, :expires_at
                               )"""
                        ),
                        {
                            "job_id": lease.job_id,
                            "lease_token": lease.lease_token,
                            "observation_id": observation.observation_id,
                            "field_name": observation.field.value,
                            "field_value": observation.value,
                            "provenance_url": observation.provenance_url,
                            "provider_record_id": observation.provider_record_id,
                            "match_basis": observation.match_basis.value,
                            "confidence_bps": observation.confidence_bps,
                            "observed_at": observation.observed_at,
                            "expires_at": observation.expires_at,
                        },
                    )
                ).scalar_one()
            )
        if outcome == "invalid":
            raise ValueError("entity enrichment observation was rejected")
        try:
            return EntityObservationWriteOutcome(outcome)
        except ValueError as error:
            raise EntityEnrichmentInvariantError(
                "entity observation capability returned an invalid outcome"
            ) from error

    async def complete(self, lease: EntityEnrichmentLease) -> bool:
        async with system_session_scope() as session:
            return bool(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_complete_entity_enrichment_job(
                                   :job_id, :lease_token
                               )"""
                        ),
                        {"job_id": lease.job_id, "lease_token": lease.lease_token},
                    )
                ).scalar_one()
            )

    async def release(
        self,
        lease: EntityEnrichmentLease,
        failure_code: EntityEnrichmentFailureCode,
        retry_after_seconds: int,
    ) -> EntityEnrichmentJobStatus:
        if not 1 <= retry_after_seconds <= _MAX_RETRY_SECONDS:
            raise ValueError("entity enrichment retry delay must be between 1 and 86400")
        async with system_session_scope() as session:
            outcome = str(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_release_entity_enrichment_job(
                                   :job_id, :lease_token, :error_code,
                                   :retry_after_seconds
                               )"""
                        ),
                        {
                            "job_id": lease.job_id,
                            "lease_token": lease.lease_token,
                            "error_code": failure_code.value,
                            "retry_after_seconds": retry_after_seconds,
                        },
                    )
                ).scalar_one()
            )
        if outcome == "lease_lost":
            raise EntityEnrichmentConflictError("entity enrichment lease was lost")
        if outcome == "invalid":
            raise ValueError("entity enrichment release was rejected")
        try:
            return EntityEnrichmentJobStatus(outcome)
        except ValueError as error:
            raise EntityEnrichmentInvariantError(
                "entity enrichment release capability returned an invalid outcome"
            ) from error

    async def list_materializable(
        self,
        fact_id: UUID,
    ) -> list[MaterializableEntityObservation]:
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """SELECT observation_id, fact_id, entity_id, role, field_name,
                                  field_value, provider_key, provenance_url,
                                  confidence_bps, reviewed_at, expires_at
                           FROM public.fn_list_materializable_entity_enrichment(:fact_id)"""
                    ),
                    {"fact_id": fact_id},
                )
            ).all()
        return [self._materializable(cast(_MaterializableRow, row)) for row in rows]

    @staticmethod
    def _lease_bounds(limit: int, lease_seconds: int) -> None:
        if not 1 <= limit <= _MAX_BATCH:
            raise ValueError("entity enrichment claim limit must be between 1 and 100")
        if not _MIN_LEASE_SECONDS <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError("entity enrichment lease must be between 30 and 3600 seconds")

    @staticmethod
    def _lease(row: _LeaseRow) -> EntityEnrichmentLease:
        return EntityEnrichmentLease(
            job_id=row.job_id,
            entity_id=row.entity_id,
            provider_key=str(row.provider_key),
            requested_entity_revision=int(row.requested_entity_revision),
            attempt_count=int(row.attempt_count),
            lease_token=row.lease_token,
            source_key=str(row.source_key),
            source_entity_key=str(row.source_entity_key),
            identity_basis=EntityIdentityBasis(str(row.identity_basis)),
            entity_kind=EntityKind(str(row.entity_kind)),
            display_name=str(row.display_name),
            direct_profile_url=(
                str(row.direct_profile_url) if row.direct_profile_url is not None else None
            ),
        )

    @staticmethod
    def _materializable(row: _MaterializableRow) -> MaterializableEntityObservation:
        if not isinstance(row.reviewed_at, datetime):
            raise EntityEnrichmentInvariantError("approved entity observation lacks review time")
        if row.expires_at is not None and not isinstance(row.expires_at, datetime):
            raise EntityEnrichmentInvariantError("entity observation expiry is invalid")
        return MaterializableEntityObservation(
            observation_id=row.observation_id,
            fact_id=row.fact_id,
            entity_id=row.entity_id,
            role=EntityRole(str(row.role)),
            field=EntityEnrichmentField(str(row.field_name)),
            value=str(row.field_value),
            provider_key=str(row.provider_key),
            provenance_url=str(row.provenance_url),
            confidence_bps=int(row.confidence_bps),
            reviewed_at=row.reviewed_at,
            expires_at=row.expires_at,
        )
