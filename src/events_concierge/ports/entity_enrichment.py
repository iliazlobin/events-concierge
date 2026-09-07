"""Persistence port for reviewed, provider-neutral entity enrichment."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from ..domain.entity_enrichment import (
    EntityEnrichmentEnqueueOutcome,
    EntityEnrichmentFailureCode,
    EntityEnrichmentJobStatus,
    EntityEnrichmentLease,
    EntityFieldObservationDraft,
    EntityObservationReviewDecision,
    EntityObservationReviewOutcome,
    EntityObservationWriteOutcome,
    MaterializableEntityObservation,
    SourceEntityFact,
    SourceEntityFactInput,
)


class EntityEnrichmentConflictError(RuntimeError):
    """An idempotency key was reused for a different durable entity operation."""


class EntityEnrichmentSourceFactNotFoundError(LookupError):
    """The exact source event/run anchor for a fact does not exist."""


class EntityEnrichmentInvariantError(RuntimeError):
    """The database capability returned an unknown or internally inconsistent result."""


class EntityEnrichmentRepository(Protocol):
    """Persist source facts, leased jobs, observations, review, and guarded read candidates."""

    async def upsert_source_fact(self, fact: SourceEntityFactInput) -> SourceEntityFact:
        """Record an exact source entity/event-role assertion without name-only matching."""
        ...

    async def enqueue(
        self,
        job_id: UUID,
        entity_id: UUID,
        provider_key: str,
        requested_by: str,
    ) -> EntityEnrichmentEnqueueOutcome:
        """Queue the entity's current revision only when the provider gate is enabled."""
        ...

    async def claim_batch(
        self,
        limit: int,
        lease_seconds: int,
    ) -> list[EntityEnrichmentLease]:
        """Lease bounded due jobs with exact non-name identity evidence."""
        ...

    async def renew_lease(
        self,
        lease: EntityEnrichmentLease,
        lease_seconds: int,
    ) -> bool:
        """Renew only the exact live lease for the entity revision that was claimed."""
        ...

    async def record_observation(
        self,
        lease: EntityEnrichmentLease,
        observation: EntityFieldObservationDraft,
    ) -> EntityObservationWriteOutcome:
        """Persist one bounded field observation under an exact live lease."""
        ...

    async def complete(self, lease: EntityEnrichmentLease) -> bool:
        """Mark the exact live job successful after all observations are durable."""
        ...

    async def release(
        self,
        lease: EntityEnrichmentLease,
        failure_code: EntityEnrichmentFailureCode,
        retry_after_seconds: int,
    ) -> EntityEnrichmentJobStatus:
        """Requeue or terminally fail using a fixed classification, never provider error text."""
        ...

    async def list_materializable(
        self,
        fact_id: UUID,
    ) -> list[MaterializableEntityObservation]:
        """Return only approved/current fields; a direct source profile URL always wins."""
        ...


class EntityEnrichmentReviewRepository(Protocol):
    """Privileged review boundary, intentionally separate from collection workers."""

    async def review_observation(
        self,
        observation_id: UUID,
        decision: EntityObservationReviewDecision,
        reviewed_by: str,
    ) -> EntityObservationReviewOutcome:
        """Approve/reject through a separately authenticated operator connection."""
        ...
