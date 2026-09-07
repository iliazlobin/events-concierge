"""Ports for reviewed public source configuration and durable catalog refresh (FR-3.1/FR-10.3)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from ..domain.catalog_sources import (
    CatalogPagedRefreshPreparation,
    CatalogPagedRefreshPromotion,
    CatalogPagedStageResult,
    CatalogRefreshClaim,
    CatalogRefreshCommit,
    CatalogRefreshDue,
    CatalogRefreshRun,
    CatalogRunExecutionEvidence,
    CatalogRunStageEvidence,
    CatalogSource,
    CatalogSourceObservation,
    CatalogSourcePage,
)
from ..domain.events import CandidateEvent


class CatalogRefreshDueReader(Protocol):
    """Read the bounded set of current schedule slots without owning source dispatch."""

    async def list_due_refreshes(self, now: datetime, *, limit: int) -> list[CatalogRefreshDue]:
        """Return eligible sources in stable due-time/source-key order (NFR-1/NFR-8)."""
        ...


class CatalogRunEvidenceRecorder(Protocol):
    """Persist only bounded resource aggregates and typed stage observations."""

    async def record_run_execution(
        self,
        source_key: str,
        run_key: str,
        evidence: CatalogRunExecutionEvidence,
    ) -> bool: ...

    async def record_run_stage(
        self,
        source_key: str,
        run_key: str,
        evidence: CatalogRunStageEvidence,
    ) -> bool: ...


class CatalogSourceRepository(CatalogRefreshDueReader, Protocol):
    """Read an owner-reviewed source registry and advance its guarded refresh-run ledger.

    Registry changes are migration-owner operations, not application capabilities.  The worker may
    read reviewed source metadata, but database-owned timestamps and exact leases fence every run
    transition (FR-10.3, NFR-8).
    """

    async def get(self, source_key: str) -> CatalogSource | None: ...

    async def list_refreshable(self, now: datetime) -> list[CatalogSource]: ...

    async def claim_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_seconds: int,
    ) -> CatalogRefreshClaim:
        """Claim one source/time-bucket run using the database clock (NFR-8)."""
        ...

    async def has_live_refresh_lease(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
    ) -> bool:
        """Confirm the exact run/token remains live at the database clock before source egress (NFR-8)."""
        ...

    async def complete_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        candidate_count: int,
        canonical_count: int,
    ) -> bool:
        """Commit success at the database clock only while the caller owns the lease."""
        ...

    async def fail_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        """Record a retryable database-timestamped failure only while the caller owns the lease."""
        ...

    async def pause_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        """Release a no-egress deferred run without classifying it as a source failure."""
        ...

    async def get_refresh_run(self, source_key: str, run_key: str) -> CatalogRefreshRun | None: ...


class CatalogSourceFetcher(Protocol):
    """Read-only adapter for one approved catalog source. It never registers or signs in."""

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        """Fetch and ACL-normalize a source only after the application has claimed its run."""
        ...


class CatalogPagedSourceFetcher(Protocol):
    """Fetch exactly one reviewed source page after a P15b Pacer admission (ADR-003/005)."""

    async def fetch_page(
        self,
        source: CatalogSource,
        *,
        window_start_day: date,
        page_number: int,
    ) -> CatalogSourcePage:
        """Read and normalize one bounded page without local sleeps or a follow-up request."""
        ...


class CatalogPagedRefreshRepository(Protocol):
    """Capability-only P15b/P15c/P15d/P15e cursor/staging transitions for reviewed Legistar profiles (NFR-8)."""

    async def prepare_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
    ) -> CatalogPagedRefreshPreparation:
        """Initialize or read the frozen page plan only while holding its exact active lease."""
        ...

    async def stage_paged_page(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
        page: CatalogSourcePage,
    ) -> CatalogPagedStageResult:
        """Atomically stage one normalized page and either pause or mark it terminal."""
        ...

    async def pause_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        """Release a P15b run without discarding its durable page cursor/stage."""
        ...

    async def abort_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        """Discard incomplete P15b stage data and record a retryable failed run."""
        ...


class CatalogPagedRefreshPromoter(Protocol):
    """Promote a terminal P15b stage with catalog/provenance/run completion in one transaction."""

    async def promote_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
    ) -> CatalogPagedRefreshPromotion:
        """Return committed counts or raise so all catalog writes roll back together."""
        ...


class CatalogRefreshCommitter(Protocol):
    """Atomically publish one generic refresh only while its current lease completes (NFR-8).

    The port owns the one transaction joining canonical-event merge, source provenance, and the
    live database-clock ``fn_complete_catalog_refresh`` transition. It returns ``None`` only after
    a rejected final capability rolls every publication write back (FR-3.8/FR-10.3, ADR-001/003).
    """

    async def commit_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        candidates: list[CandidateEvent],
    ) -> CatalogRefreshCommit | None:
        """Return committed counts, or ``None`` when the final guard rejects the effect."""
        ...


class CatalogObservationRepository(Protocol):
    """Append/update source-specific catalog provenance after a normalized ingest (FR-3.8/NFR-1)."""

    async def record(self, observations: list[CatalogSourceObservation], run_key: str) -> None:
        """Preserve first/last seen time and the exact refresh run that last observed each event."""
        ...

    async def list_for_source(self, source_key: str) -> list[CatalogSourceObservation]: ...
