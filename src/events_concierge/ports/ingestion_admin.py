"""Ports for capability-only ingestion administration."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from ..domain.catalog_sources import CatalogRefreshDue
from ..domain.ingestion_admin import (
    CatalogFreshnessBucket,
    IngestionCommand,
    IngestionCommandAction,
    IngestionCommandDetail,
    IngestionCommandLease,
    IngestionCommandRunTarget,
    IngestionFilterMetadata,
    IngestionFleetSummary,
    IngestionOverview,
    IngestionRunExecutionDescriptor,
    IngestionRunPage,
    IngestionSourceConfigurationUpdate,
    IngestionSourceDetail,
    IngestionSourceEnabledBulkUpdate,
    IngestionSourcePage,
    IngestionSourceHealth,
    IngestionSourceRevisionTarget,
    IngestionStageSummaryEntry,
    SafeCommandResult,
)


class IngestionExecutionDescriptorRegistry(Protocol):
    """Resolve fixed composition metadata for a reviewed source execution path."""

    def describe(
        self,
        *,
        source_key: str,
        mode: str | None,
        page_limit: int | None,
        trigger: str,
    ) -> IngestionRunExecutionDescriptor | None: ...


class IngestionAdminError(RuntimeError):
    """Base class carrying a fixed API-safe reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IngestionSourceNotFoundError(IngestionAdminError, LookupError):
    """A requested source key is absent from the reviewed registry."""


class IngestionCommandUnavailableError(IngestionAdminError):
    """The source or fleet policy currently refuses a refresh command."""


class IngestionCommandConflictError(IngestionAdminError):
    """A UUID has different input, or queued/running work already owns the command target."""


class IngestionCommandRejectedError(IngestionAdminError, ValueError):
    """Command input is malformed or outside the fixed admin vocabulary."""


class IngestionSourceConfigurationConflictError(IngestionAdminError):
    """A source changed after the operator loaded its configuration form."""


class IngestionSourceConfigurationUnavailableError(IngestionAdminError):
    """A requested enablement is not backed by a current reviewed source."""


class IngestionAdminRepository(Protocol):
    """Read fixed projections and advance only exact command-queue capabilities."""

    async def overview(self) -> IngestionOverview: ...

    async def fleet_summary(
        self,
        *,
        window_hours: int,
        include_fixtures: bool,
    ) -> IngestionFleetSummary: ...

    async def stage_summary(
        self,
        *,
        window_hours: int,
        include_fixtures: bool,
    ) -> list[IngestionStageSummaryEntry]: ...

    async def catalog_freshness(self) -> list[CatalogFreshnessBucket]: ...

    async def source_health(
        self,
        *,
        include_fixtures: bool,
    ) -> list[IngestionSourceHealth]: ...

    async def list_sources(
        self,
        *,
        query: str | None,
        state: str,
        mode: str | None,
        publisher: str | None,
        region: str | None,
        source_key: str | None,
        include_fixtures: bool,
        sort_by: str,
        sort_direction: str,
        limit: int,
        offset: int,
    ) -> IngestionSourcePage: ...

    async def list_runs(
        self,
        *,
        status: str | None,
        source_key: str | None,
        mode: str | None,
        publisher: str | None,
        region: str | None,
        window_hours: int | None,
        include_fixtures: bool,
        limit: int,
        offset: int,
    ) -> IngestionRunPage: ...

    async def get_filter_metadata(
        self,
        *,
        query: str | None,
        state: str,
        mode: str | None,
        publisher: str | None,
        region: str | None,
        include_fixtures: bool,
    ) -> IngestionFilterMetadata: ...

    async def get_source_detail(
        self,
        source_key: str,
        *,
        window_hours: int,
        bucket_hours: int,
        include_fixtures: bool,
    ) -> IngestionSourceDetail | None: ...

    async def update_source_configuration(
        self,
        source_key: str,
        *,
        expected_revision: int,
        seed_url: str,
        approved_origins: tuple[str, ...],
        mode: str,
        enabled: bool,
        handoff_only: bool,
        review_expires_at: datetime | None,
        refresh_interval_minutes: int,
        min_interval_ms: int,
        page_limit: int,
        requested_by: str,
    ) -> IngestionSourceConfigurationUpdate: ...

    async def set_sources_enabled(
        self,
        targets: tuple[IngestionSourceRevisionTarget, ...],
        *,
        enabled: bool,
        requested_by: str,
    ) -> IngestionSourceEnabledBulkUpdate: ...

    async def list_commands(self, limit: int) -> tuple[IngestionCommand, ...]: ...

    async def get_command(self, command_id: UUID) -> IngestionCommand | None: ...

    async def get_command_detail(self, command_id: UUID) -> IngestionCommandDetail | None: ...

    async def link_command_runs(
        self,
        command_id: UUID,
        attempt_count: int,
        lease_token: UUID,
        targets: tuple[IngestionCommandRunTarget, ...],
    ) -> None:
        """Persist the complete bounded cadence plan before any child provider work starts."""
        ...

    async def list_due_refreshes(self, now: datetime, *, limit: int) -> list[CatalogRefreshDue]:
        """Return fixture-filtered due sources for an admin-only cadence dispatcher."""
        ...

    async def enqueue(
        self,
        command_id: UUID,
        action: IngestionCommandAction,
        source_key: str | None,
        requested_by: str,
        release_revision: str,
        image_digest: str | None,
    ) -> IngestionCommand: ...

    async def claim_batch(
        self,
        limit: int,
        lease_seconds: int,
        executor_release_revision: str,
        executor_image_digest: str | None,
    ) -> tuple[IngestionCommandLease, ...]: ...

    async def renew_lease(
        self,
        lease: IngestionCommandLease,
        lease_seconds: int,
    ) -> bool:
        """Extend one still-live command lease without changing its identity."""
        ...

    async def complete(
        self,
        lease: IngestionCommandLease,
        result: SafeCommandResult,
    ) -> bool: ...

    async def defer(
        self,
        lease: IngestionCommandLease,
        retry_after_seconds: int,
    ) -> bool:
        """Return a live lease to the queue at a database-clock retry time."""
        ...

    async def fail(self, lease: IngestionCommandLease, error_code: str) -> bool: ...
