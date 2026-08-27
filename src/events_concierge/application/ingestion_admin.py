"""Application service for bounded ingestion visibility and durable refresh commands."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from unicodedata import category
from uuid import UUID

from ..domain.catalog_browse import CatalogBrowseCursor, CatalogBrowseEvent
from ..domain.catalog_sources import CatalogSource
from ..domain.enums import CatalogSourceMode
from ..domain.ingestion_admin import (
    CatalogConcentrationEntry,
    CatalogFreshnessBucket,
    IngestionBuildIdentity,
    IngestionCatalogEvent,
    IngestionCatalogEventPage,
    IngestionCatalogQualityIssue,
    IngestionCommand,
    IngestionCommandAction,
    IngestionCommandDetail,
    IngestionCommandLease,
    IngestionCommandRunTarget,
    IngestionFilterMetadata,
    IngestionFleetShapeEntry,
    IngestionFleetSummary,
    IngestionOverview,
    IngestionProcessReport,
    IngestionRunPage,
    IngestionSourceConfigurationUpdate,
    IngestionSourceDetail,
    IngestionSourceEnabledBulkUpdate,
    IngestionSourceHealth,
    IngestionSourcePage,
    IngestionSourceRevisionTarget,
    IngestionStageSummaryEntry,
    IngestionThroughputBucket,
    SafeCommandResult,
)
from ..ports.ingestion_admin import (
    IngestionAdminRepository,
    IngestionCommandRejectedError,
    IngestionSourceConfigurationUnavailableError,
)
from ..ports.repositories import CatalogRepository
from .catalog_refresh import CatalogRefreshOutcome, CatalogRefreshResult
from .catalog_refresh_dispatcher import (
    CatalogCadenceDispatcher,
    CatalogCadenceDispatchReport,
    CatalogCadencePlannedRun,
    CatalogRefreshRunner,
)

_SOURCE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{1,79}")
_SOURCE_STATES = frozenset({"all", "active", "due", "blocked", "failed"})
_SOURCE_SORT_FIELDS = frozenset(
    {"source", "health", "catalog", "last_success", "latest_run", "output"}
)
_SORT_DIRECTIONS = frozenset({"asc", "desc"})
_RUN_STATUSES = frozenset({"running", "paused", "succeeded", "failed"})
_MAX_QUERY_LENGTH = 200
_MAX_CATALOG_QUERY_LENGTH = 160
_MAX_CATALOG_DESCRIPTION_LENGTH = 2_000
_MAX_REQUESTED_BY_LENGTH = 256
_MAX_PAGE_LIMIT = 100
_MAX_CADENCE_LIMIT = 500
_MAX_OFFSET = 100_000
_MAX_FACET_LENGTH = 300
_MIN_WINDOW_HOURS = 1
_MAX_WINDOW_HOURS = 2_160
_MAX_BUCKET_HOURS = 168
_MAX_HISTORY_BUCKETS = 120
_DEFAULT_SUMMARY_WINDOW_HOURS = 168
_MAX_CONCENTRATION_ROWS = 200
_DEFAULT_CONCENTRATION_ROWS = 15
_MIN_LEASE_SECONDS = 300
_MAX_LEASE_SECONDS = 21_600
_MAX_DEFERRED_ATTEMPTS = 5
_MAX_FLEET_DEFERRED_ATTEMPTS = 50
_MAX_DEFERRED_RETRY_SECONDS = 21_600
_MIN_REFRESH_INTERVAL_MINUTES = 5
_MAX_REFRESH_INTERVAL_MINUTES = 10_080
_MIN_PACING_INTERVAL_MS = 250
_MAX_PACING_INTERVAL_MS = 60_000
_MAX_SOURCE_PAGE_LIMIT = 500
_MAX_APPROVED_ORIGINS = 20
_MAX_BULK_SOURCE_TARGETS = 100
_MAX_SOURCE_REVISION = 2_147_483_647
_SUPERSEDED_BULK_ENABLE_SOURCE_KEYS = frozenset(
    {
        "alameda-county-library-fremont-events",
        "sccld-milpitas-events",
        "sccld-saratoga-events",
        "smcl-millbrae-events",
    }
)
type _AttemptDisposition = Literal["completed", "deferred", "failed", "lost_lease"]


class IngestionAdminService:
    """Expose fixed read projections and execute leased refresh commands off-request."""

    def __init__(
        self,
        repository: IngestionAdminRepository,
        refresh_router: CatalogRefreshRunner | None = None,
        *,
        catalog: CatalogRepository | None = None,
        lease_seconds: int = 300,
        lease_heartbeat_seconds: float | None = None,
        cadence_batch_size: int = 50,
        deferred_attempt_limit: int = _MAX_DEFERRED_ATTEMPTS,
        fleet_deferred_attempt_limit: int = _MAX_FLEET_DEFERRED_ATTEMPTS,
        release_revision: str = "development",
        image_digest: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not _MIN_LEASE_SECONDS <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError("ingestion command lease must be between 300 and 21600 seconds")
        heartbeat_seconds = (
            min(60.0, lease_seconds / 3)
            if lease_heartbeat_seconds is None
            else lease_heartbeat_seconds
        )
        if (
            not math.isfinite(heartbeat_seconds)
            or heartbeat_seconds <= 0
            or heartbeat_seconds >= lease_seconds
        ):
            raise ValueError(
                "ingestion command heartbeat must be positive and shorter than its lease"
            )
        if not 1 <= cadence_batch_size <= _MAX_CADENCE_LIMIT:
            raise ValueError("ingestion cadence batch size must be between 1 and 500")
        if not 1 <= deferred_attempt_limit <= _MAX_DEFERRED_ATTEMPTS:
            raise ValueError("ingestion deferred attempt limit must be between 1 and 5")
        if not 1 <= fleet_deferred_attempt_limit <= _MAX_FLEET_DEFERRED_ATTEMPTS:
            raise ValueError("ingestion fleet deferred attempt limit must be between 1 and 50")
        self._repository = repository
        self._catalog = catalog
        self._refresh_router = refresh_router
        self._lease_seconds = lease_seconds
        self._lease_heartbeat_seconds = heartbeat_seconds
        self._deferred_attempt_limit = deferred_attempt_limit
        self._fleet_deferred_attempt_limit = fleet_deferred_attempt_limit
        self._build_identity = IngestionBuildIdentity(
            release_revision=release_revision,
            image_digest=image_digest,
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._cadence = (
            CatalogCadenceDispatcher(
                repository,
                refresh_router,
                batch_size=cadence_batch_size,
                now=self._now,
            )
            if refresh_router is not None
            else None
        )

    async def overview(self) -> IngestionOverview:
        return await self._repository.overview()

    async def get_overview(self) -> IngestionOverview:
        """Compatibility alias used by the HTTP adapter."""
        return await self.overview()

    async def fleet_summary(
        self,
        *,
        window_hours: int = _DEFAULT_SUMMARY_WINDOW_HOURS,
        include_fixtures: bool = False,
    ) -> IngestionFleetSummary:
        """Return one database-computed fleet rollup for the requested window."""
        _validate_window_hours(window_hours)
        return await self._repository.fleet_summary(
            window_hours=window_hours,
            include_fixtures=include_fixtures,
        )

    async def stage_summary(
        self,
        *,
        window_hours: int = _DEFAULT_SUMMARY_WINDOW_HOURS,
        include_fixtures: bool = False,
    ) -> list[IngestionStageSummaryEntry]:
        """Return every declared pipeline stage with the evidence grade that applies to it."""
        _validate_window_hours(window_hours)
        return await self._repository.stage_summary(
            window_hours=window_hours,
            include_fixtures=include_fixtures,
        )

    async def catalog_freshness(self) -> list[CatalogFreshnessBucket]:
        """Return served upcoming events graded by their source's last successful fetch."""
        return await self._repository.catalog_freshness()

    async def source_health(
        self,
        *,
        include_fixtures: bool = False,
    ) -> list[IngestionSourceHealth]:
        """Return the whole registry graded, unpaginated -- a roster is a set, not a page."""
        return await self._repository.source_health(include_fixtures=include_fixtures)

    async def fleet_shape(self) -> list[IngestionFleetShapeEntry]:
        """Return each adapter mode's share of the fleet against its share of the catalog."""
        return await self._repository.fleet_shape()

    async def throughput(
        self,
        *,
        window_hours: int = _DEFAULT_SUMMARY_WINDOW_HOURS,
        bucket_hours: int = 24,
    ) -> list[IngestionThroughputBucket]:
        """Return gap-filled pipeline volume; an empty bucket is a fact, not a missing row."""
        _validate_history_window(window_hours, bucket_hours)
        return await self._repository.throughput(
            window_hours=window_hours,
            bucket_hours=bucket_hours,
        )

    async def catalog_concentration(
        self,
        *,
        limit: int = _DEFAULT_CONCENTRATION_ROWS,
    ) -> list[CatalogConcentrationEntry]:
        """Return which sources actually carry the served catalog."""
        if limit < 1 or limit > _MAX_CONCENTRATION_ROWS:
            raise ValueError("catalog concentration limit is invalid")
        return await self._repository.catalog_concentration(limit=limit)

    async def list_sources(
        self,
        *,
        query: str | None = None,
        state: str = "all",
        mode: str | None = None,
        publisher: str | None = None,
        region: str | None = None,
        include_fixtures: bool = False,
        sort_by: str = "source",
        sort_direction: str = "asc",
        limit: int = 50,
        offset: int = 0,
        source_key: str | None = None,
    ) -> IngestionSourcePage:
        if state not in _SOURCE_STATES:
            raise ValueError("ingestion source state is invalid")
        if query is not None and len(query) > _MAX_QUERY_LENGTH:
            raise ValueError("ingestion source query is too long")
        if sort_by not in _SOURCE_SORT_FIELDS:
            raise ValueError("ingestion source sort field is invalid")
        if sort_direction not in _SORT_DIRECTIONS:
            raise ValueError("ingestion source sort direction is invalid")
        _validate_facet(mode, "mode")
        _validate_facet(publisher, "publisher")
        _validate_facet(region, "region")
        _validate_source_key(source_key, optional=True)
        _validate_page(limit, offset)
        return await self._repository.list_sources(
            query=query,
            state=state,
            mode=mode,
            publisher=publisher,
            region=region,
            source_key=source_key,
            include_fixtures=include_fixtures,
            sort_by=sort_by,
            sort_direction=sort_direction,
            limit=limit,
            offset=offset,
        )

    async def list_runs(
        self,
        *,
        status: str | None = None,
        source_key: str | None = None,
        mode: str | None = None,
        publisher: str | None = None,
        region: str | None = None,
        window_hours: int | None = None,
        include_fixtures: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> IngestionRunPage:
        if status is not None and status not in _RUN_STATUSES:
            raise ValueError("ingestion run status is invalid")
        _validate_source_key(source_key, optional=True)
        _validate_facet(mode, "mode")
        _validate_facet(publisher, "publisher")
        _validate_facet(region, "region")
        if window_hours is not None:
            _validate_window_hours(window_hours)
        _validate_page(limit, offset)
        return await self._repository.list_runs(
            status=status,
            source_key=source_key,
            mode=mode,
            publisher=publisher,
            region=region,
            window_hours=window_hours,
            include_fixtures=include_fixtures,
            limit=limit,
            offset=offset,
        )

    async def get_filter_metadata(
        self,
        *,
        query: str | None = None,
        state: str = "all",
        mode: str | None = None,
        publisher: str | None = None,
        region: str | None = None,
        include_fixtures: bool = False,
    ) -> IngestionFilterMetadata:
        if state not in _SOURCE_STATES:
            raise ValueError("ingestion source state is invalid")
        if query is not None and len(query) > _MAX_QUERY_LENGTH:
            raise ValueError("ingestion source query is too long")
        _validate_facet(mode, "mode")
        _validate_facet(publisher, "publisher")
        _validate_facet(region, "region")
        return await self._repository.get_filter_metadata(
            query=query,
            state=state,
            mode=mode,
            publisher=publisher,
            region=region,
            include_fixtures=include_fixtures,
        )

    async def get_source_detail(
        self,
        source_key: str,
        *,
        window_hours: int = 168,
        bucket_hours: int = 24,
        include_fixtures: bool = False,
    ) -> IngestionSourceDetail | None:
        _validate_source_key(source_key, optional=False)
        _validate_history_window(window_hours, bucket_hours)
        detail = await self._repository.get_source_detail(
            source_key,
            window_hours=window_hours,
            bucket_hours=bucket_hours,
            include_fixtures=include_fixtures,
        )
        if detail is None:
            return None
        return replace(detail, current_build=self._build_identity)

    async def list_source_events(
        self,
        source_key: str,
        *,
        query: str | None = None,
        after_start_at: datetime | None = None,
        after_canonical_event_id: UUID | None = None,
        limit: int = 20,
    ) -> IngestionCatalogEventPage:
        """Return parsed current output and provenance without raw provider material."""
        _validate_source_key(source_key, optional=False)
        _validate_limit(limit)
        normalized_query = _catalog_query(query)
        if (after_start_at is None) != (after_canonical_event_id is None):
            raise ValueError("source event cursor must provide both fields")
        after = None
        if after_start_at is not None and after_canonical_event_id is not None:
            if after_start_at.tzinfo is None or after_start_at.utcoffset() is None:
                raise ValueError("source event cursor timestamp must be timezone-aware")
            after = CatalogBrowseCursor(
                after_start_at.astimezone(UTC),
                after_canonical_event_id,
            )
        if self._catalog is None:
            raise RuntimeError("source event catalog is unavailable")
        rows, providers = await self._catalog.browse_current(
            source_keys=(source_key,),
            after=after,
            limit=limit + 1,
            query=normalized_query,
        )
        page_rows = rows[:limit]
        items = tuple(_ingestion_catalog_event(source_key, row) for row in page_rows)
        has_more = len(rows) > limit
        last = items[-1] if has_more and items else None
        source_total = next(
            (provider.event_count for provider in providers if provider.source_key == source_key),
            0,
        )
        return IngestionCatalogEventPage(
            items=items,
            source_total=source_total,
            limit=limit,
            has_more=has_more,
            next_start_at=last.start_at if last is not None else None,
            next_canonical_event_id=(last.canonical_event_id if last is not None else None),
            query=normalized_query,
        )

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
        requested_by: str = "local-admin",
    ) -> IngestionSourceConfigurationUpdate:
        """Validate and persist one explicit owner-reviewed source revision."""
        _validate_source_key(source_key, optional=False)
        if isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("source configuration revision is invalid")
        if not handoff_only:
            raise ValueError("catalog discovery sources must remain handoff-only")
        if (
            not _MIN_REFRESH_INTERVAL_MINUTES
            <= refresh_interval_minutes
            <= (_MAX_REFRESH_INTERVAL_MINUTES)
        ):
            raise ValueError("source refresh cadence is outside its reviewed bounds")
        if not _MIN_PACING_INTERVAL_MS <= min_interval_ms <= _MAX_PACING_INTERVAL_MS:
            raise ValueError("source pacing is outside its reviewed bounds")
        if not 1 <= page_limit <= _MAX_SOURCE_PAGE_LIMIT:
            raise ValueError("source page limit is outside its reviewed bounds")
        if not approved_origins or len(approved_origins) > _MAX_APPROVED_ORIGINS:
            raise ValueError("source approved origins are outside their reviewed bounds")
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("ingestion admin clock must be timezone-aware")
        reviewed_at = now.astimezone(UTC)
        if review_expires_at is not None:
            if review_expires_at.tzinfo is None or review_expires_at.utcoffset() is None:
                raise ValueError("source review expiry must be timezone-aware")
            review_expires_at = review_expires_at.astimezone(UTC)
            if review_expires_at <= reviewed_at:
                raise ValueError("source review expiry must be in the future")
        if (
            not requested_by
            or len(requested_by) > _MAX_REQUESTED_BY_LENGTH
            or any(category(character) == "Cc" for character in requested_by)
        ):
            raise ValueError("source configuration operator is invalid")
        try:
            CatalogSource(
                source_key=source_key,
                display_name="Validated source",
                publisher="Validated publisher",
                seed_url=seed_url,
                approved_origins=approved_origins,
                region="validated-region",
                mode=CatalogSourceMode(mode),
                enabled=enabled,
                reviewed_at=reviewed_at,
                review_expires_at=review_expires_at,
                refresh_interval_minutes=refresh_interval_minutes,
                min_interval_ms=min_interval_ms,
                page_limit=page_limit,
                handoff_only=handoff_only,
                source_revision=expected_revision,
            )
        except ValueError as error:
            raise ValueError("source configuration contract is invalid") from error
        return await self._repository.update_source_configuration(
            source_key,
            expected_revision=expected_revision,
            seed_url=seed_url,
            approved_origins=approved_origins,
            mode=mode,
            enabled=enabled,
            handoff_only=handoff_only,
            review_expires_at=review_expires_at,
            refresh_interval_minutes=refresh_interval_minutes,
            min_interval_ms=min_interval_ms,
            page_limit=page_limit,
            requested_by=requested_by,
        )

    async def set_sources_enabled(
        self,
        targets: tuple[IngestionSourceRevisionTarget, ...],
        *,
        enabled: bool,
        requested_by: str = "local-admin",
    ) -> IngestionSourceEnabledBulkUpdate:
        """Atomically revise the enabled state of exact optimistic source targets."""
        if not isinstance(enabled, bool):
            raise ValueError("source enabled state is invalid")
        if not 1 <= len(targets) <= _MAX_BULK_SOURCE_TARGETS:
            raise ValueError("source enabled targets are outside their bounds")
        seen: set[str] = set()
        for target in targets:
            _validate_source_key(target.source_key, optional=False)
            if (
                isinstance(target.expected_revision, bool)
                or target.expected_revision < 1
                or target.expected_revision > _MAX_SOURCE_REVISION
            ):
                raise ValueError("source configuration revision is invalid")
            if target.source_key in seen:
                raise ValueError("source enabled targets must be unique")
            seen.add(target.source_key)
        if enabled and seen & _SUPERSEDED_BULK_ENABLE_SOURCE_KEYS:
            # These branch-only seeds were retired by reviewed migrations after their fleet
            # replacements became authoritative. A single-source configuration edit remains the
            # explicit owner-review escape hatch; a broad fleet action must not revive duplicates.
            raise IngestionSourceConfigurationUnavailableError("source_unavailable")
        if (
            not requested_by
            or len(requested_by) > _MAX_REQUESTED_BY_LENGTH
            or any(category(character) == "Cc" for character in requested_by)
        ):
            raise ValueError("source configuration operator is invalid")
        return await self._repository.set_sources_enabled(
            targets,
            enabled=enabled,
            requested_by=requested_by,
        )

    async def list_commands(self, limit: int = 50) -> tuple[IngestionCommand, ...]:
        _validate_limit(limit)
        return await self._repository.list_commands(limit)

    async def get_command_detail(self, command_id: UUID) -> IngestionCommandDetail | None:
        """Return one command with exact, bounded child-run state."""
        return await self._repository.get_command_detail(command_id)

    async def enqueue(
        self,
        command_id: UUID,
        action: IngestionCommandAction | str,
        source_key: str | None,
        requested_by: str,
    ) -> IngestionCommand:
        command_action = _command_action(action)
        if command_action is IngestionCommandAction.REFRESH_SOURCE:
            _validate_source_key(source_key, optional=False)
        elif source_key is not None:
            raise IngestionCommandRejectedError("invalid_command")
        if (
            not requested_by
            or len(requested_by) > _MAX_REQUESTED_BY_LENGTH
            or any(category(character) == "Cc" for character in requested_by)
        ):
            raise IngestionCommandRejectedError("invalid_command")
        return await self._repository.enqueue(
            command_id,
            command_action,
            source_key,
            requested_by,
            self._build_identity.release_revision,
            self._build_identity.image_digest,
        )

    async def enqueue_command(
        self,
        command_id: UUID,
        action: IngestionCommandAction | str,
        source_key: str | None,
        requested_by: str = "local-admin",
    ) -> IngestionCommand:
        """Compatibility alias used by the HTTP adapter."""
        return await self.enqueue(command_id, action, source_key, requested_by)

    async def process_once(self, limit: int = 10) -> IngestionProcessReport:
        """Claim a bounded batch and terminally fence every execution attempt."""
        _validate_limit(limit)
        dispositions: list[_AttemptDisposition] = []
        for _ in range(limit):
            leases = await self._repository.claim_batch(
                1,
                self._lease_seconds,
                self._build_identity.release_revision,
                self._build_identity.image_digest,
            )
            if not leases:
                break
            dispositions.append(await self._process_lease(leases[0]))
        return IngestionProcessReport(
            claimed=len(dispositions),
            completed=dispositions.count("completed"),
            deferred=dispositions.count("deferred"),
            failed=dispositions.count("failed"),
            lost_leases=dispositions.count("lost_lease"),
        )

    async def _process_lease(self, lease: IngestionCommandLease) -> _AttemptDisposition:
        """Execute and fence one claim, preserving a durable Pacer retry when needed."""
        if self._refresh_router is None:
            return await self._fail_lease(lease, "worker_unavailable")
        try:
            result = await self._execute_with_heartbeat(lease)
        except _IngestionCommandLeaseLostError:
            return "lost_lease"
        except Exception:
            # Refresh services own provider-detail persistence. Only a fixed operational code
            # crosses the admin boundary; exception strings can contain unreviewed payloads.
            error_code = (
                "source_refresh_failed"
                if lease.action is IngestionCommandAction.REFRESH_SOURCE
                else "due_refresh_failed"
            )
            return await self._fail_lease(lease, error_code)
        retry_after_seconds = _deferred_retry_seconds(result)
        deferred_attempt_limit = (
            self._fleet_deferred_attempt_limit
            if lease.action is IngestionCommandAction.REFRESH_DUE
            else self._deferred_attempt_limit
        )
        if retry_after_seconds is not None and lease.attempt_count < deferred_attempt_limit:
            deferred = await self._repository.defer(lease, retry_after_seconds)
            return "deferred" if deferred else "lost_lease"
        try:
            completed = await self._repository.complete(lease, result)
        except ValueError:
            completed = False
        if completed:
            return "completed"
        return await self._fail_lease(lease, "invalid_result")

    async def _execute_with_heartbeat(
        self,
        lease: IngestionCommandLease,
    ) -> SafeCommandResult:
        """Keep a live command claim renewable without extending crash recovery time."""
        operation = asyncio.create_task(self._execute(lease))
        try:
            while True:
                done, _ = await asyncio.wait(
                    {operation},
                    timeout=self._lease_heartbeat_seconds,
                )
                if operation in done:
                    return await operation
                try:
                    renewed = await self._repository.renew_lease(
                        lease,
                        self._lease_seconds,
                    )
                except Exception as error:
                    # A timed-out/failed renewal has an unknown commit result. Stop provider work
                    # and leave the durable command to database-clock reclaim; never attempt a
                    # terminal write with authority we can no longer prove.
                    operation.cancel()
                    with suppress(asyncio.CancelledError):
                        await operation
                    raise _IngestionCommandLeaseLostError from error
                if not renewed:
                    operation.cancel()
                    with suppress(asyncio.CancelledError):
                        await operation
                    raise _IngestionCommandLeaseLostError
        finally:
            if not operation.done():
                operation.cancel()
                with suppress(asyncio.CancelledError):
                    await operation

    async def _fail_lease(
        self,
        lease: IngestionCommandLease,
        error_code: str,
    ) -> _AttemptDisposition:
        return "failed" if await self._repository.fail(lease, error_code) else "lost_lease"

    async def _execute(self, lease: IngestionCommandLease) -> SafeCommandResult:
        if lease.action is IngestionCommandAction.REFRESH_SOURCE:
            if lease.source_key is None:
                raise RuntimeError("refresh_source command is missing its source key")
            assert self._refresh_router is not None
            run_key = f"admin:{lease.command_id}"
            await self._repository.link_command_runs(
                lease.command_id,
                lease.attempt_count,
                lease.lease_token,
                (
                    IngestionCommandRunTarget(
                        position=0,
                        source_key=lease.source_key,
                        run_key=run_key,
                    ),
                ),
            )
            refresh = await self._refresh_router.refresh(lease.source_key, run_key)
            return _source_result(refresh)
        if lease.action is not IngestionCommandAction.REFRESH_DUE:
            raise RuntimeError("unknown ingestion command action")
        if self._cadence is None:
            raise RuntimeError("ingestion cadence worker is unavailable")

        async def link_plan(plan: tuple[CatalogCadencePlannedRun, ...]) -> None:
            # The dispatcher owns deterministic ordering and stable cadence run keys.  Persist the
            # complete plan transactionally before it crosses the first provider boundary.
            await self._repository.link_command_runs(
                lease.command_id,
                lease.attempt_count,
                lease.lease_token,
                tuple(
                    IngestionCommandRunTarget(
                        position=item.position,
                        source_key=item.source_key,
                        run_key=item.run_key,
                    )
                    for item in plan
                ),
            )

        report = await self._cadence.dispatch_once(on_plan=link_plan)
        return _due_result(report)


def _source_result(result: CatalogRefreshResult) -> SafeCommandResult:
    safe: SafeCommandResult = {
        "action": IngestionCommandAction.REFRESH_SOURCE.value,
        "source_key": result.source_key,
        "run_key": result.run_key,
        "outcome": result.outcome.value,
        "candidate_count": max(0, result.candidate_count),
        "canonical_count": max(0, result.canonical_count),
    }
    if result.retry_after_seconds is not None:
        safe["retry_after_seconds"] = max(0, math.ceil(result.retry_after_seconds))
    return safe


def _due_result(report: CatalogCadenceDispatchReport) -> SafeCommandResult:
    safe: SafeCommandResult = {
        "action": IngestionCommandAction.REFRESH_DUE.value,
        "due_sources": report.due_sources,
        "attempted": report.attempted,
        "succeeded": report.outcome_count(CatalogRefreshOutcome.SUCCEEDED),
        "queued": report.outcome_count(CatalogRefreshOutcome.QUEUED),
        "skipped": report.outcome_count(CatalogRefreshOutcome.SKIPPED),
        "deferred": report.outcome_count(CatalogRefreshOutcome.DEFERRED),
        "already_succeeded": report.outcome_count(CatalogRefreshOutcome.ALREADY_SUCCEEDED),
        "busy": report.outcome_count(CatalogRefreshOutcome.BUSY),
        "progressed": report.outcome_count(CatalogRefreshOutcome.PROGRESSED),
        "failed": len(report.failures),
    }
    retry_delays = [
        result.retry_after_seconds
        for result in report.results
        if result.outcome is CatalogRefreshOutcome.DEFERRED
        and result.retry_after_seconds is not None
        and math.isfinite(result.retry_after_seconds)
        and result.retry_after_seconds > 0
    ]
    if retry_delays:
        safe["retry_after_seconds"] = min(
            max(1, math.ceil(max(retry_delays))),
            _MAX_DEFERRED_RETRY_SECONDS,
        )
    return safe


def _deferred_retry_seconds(result: SafeCommandResult) -> int | None:
    """Return a bounded durable retry delay for explicit source or fleet Pacer deferrals."""
    action = result.get("action")
    source_deferred = (
        action == IngestionCommandAction.REFRESH_SOURCE.value
        and result.get("outcome") == CatalogRefreshOutcome.DEFERRED.value
    )
    deferred_count = result.get("deferred")
    fleet_deferred = (
        action == IngestionCommandAction.REFRESH_DUE.value
        and isinstance(deferred_count, int)
        and not isinstance(deferred_count, bool)
        and deferred_count > 0
    )
    if not source_deferred and not fleet_deferred:
        return None
    retry_after = result.get("retry_after_seconds")
    if isinstance(retry_after, bool) or not isinstance(retry_after, int | float):
        return None
    if not math.isfinite(retry_after) or retry_after <= 0:
        return None
    return min(max(1, math.ceil(retry_after)), _MAX_DEFERRED_RETRY_SECONDS)


def _command_action(action: IngestionCommandAction | str) -> IngestionCommandAction:
    try:
        return IngestionCommandAction(action)
    except ValueError as error:
        raise IngestionCommandRejectedError("invalid_command") from error


def _catalog_query(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > _MAX_CATALOG_QUERY_LENGTH or any(
        category(character) == "Cc" for character in normalized
    ):
        raise ValueError("source event query is invalid")
    return normalized


class _IngestionCommandLeaseLostError(RuntimeError):
    """Stop one read-only refresh when another worker may own the durable command."""


def _ingestion_catalog_event(
    source_key: str,
    row: CatalogBrowseEvent,
) -> IngestionCatalogEvent:
    event = row.canonical_event
    source = next(
        (candidate for candidate in row.sources if candidate.source_key == source_key),
        None,
    )
    if source is None:
        raise RuntimeError("current catalog row lost its requested source provenance")
    registration_url = source.registration_url.strip() or None
    issues: list[IngestionCatalogQualityIssue] = []
    if not event.description.strip():
        issues.append(IngestionCatalogQualityIssue.DESCRIPTION)
    if event.end_at is None:
        issues.append(IngestionCatalogQualityIssue.END_TIME)
    if not event.venue_name or not event.venue_name.strip():
        issues.append(IngestionCatalogQualityIssue.VENUE)
    if not event.city_norm or not event.city_norm.strip():
        issues.append(IngestionCatalogQualityIssue.CITY)
    if event.geo is None:
        issues.append(IngestionCatalogQualityIssue.GEO)
    if registration_url is None:
        issues.append(IngestionCatalogQualityIssue.REGISTRATION_URL)
    return IngestionCatalogEvent(
        canonical_event_id=event.canonical_event_id,
        title=event.title,
        start_at=event.start_at,
        end_at=event.end_at,
        venue_name=event.venue_name,
        city=event.city_norm,
        latitude=event.geo.lat if event.geo is not None else None,
        longitude=event.geo.lon if event.geo is not None else None,
        description=event.description[:_MAX_CATALOG_DESCRIPTION_LENGTH],
        description_length=len(event.description),
        price_status=event.price_status.value,
        price_min_cents=event.price_min_cents,
        price_max_cents=event.price_max_cents,
        price_currency=event.price_currency,
        event_status=event.event_status.value,
        normalizer_version=event.normalizer_version,
        merge_version=event.merge_version,
        source_event_id=source.source_event_id,
        registration_url=registration_url,
        last_seen_at=source.last_seen_at,
        refresh_run_key=source.refresh_run_key,
        quality_issues=tuple(issues),
        organizer_name=event.organizer_name,
        host_names=event.host_names,
        speaker_names=event.speaker_names,
        partner_names=event.partner_names,
        entity_profiles=event.entity_profiles,
        attendance_count=event.attendance_count,
        registration_status=event.registration_status.value,
    )


def _validate_source_key(source_key: str | None, *, optional: bool) -> None:
    if source_key is None:
        if optional:
            return
        raise IngestionCommandRejectedError("invalid_command")
    if _SOURCE_KEY.fullmatch(source_key) is None:
        if optional:
            raise ValueError("ingestion source key is invalid")
        raise IngestionCommandRejectedError("invalid_command")


def _validate_page(limit: int, offset: int) -> None:
    _validate_limit(limit)
    if not 0 <= offset <= _MAX_OFFSET:
        raise ValueError("ingestion admin offset is invalid")


def _validate_facet(value: str | None, name: str) -> None:
    if value is None:
        return
    if (
        not value
        or len(value) > _MAX_FACET_LENGTH
        or any(category(character) == "Cc" for character in value)
    ):
        raise ValueError(f"ingestion source {name} is invalid")


def _validate_window_hours(window_hours: int) -> None:
    if isinstance(window_hours, bool) or not _MIN_WINDOW_HOURS <= window_hours <= _MAX_WINDOW_HOURS:
        raise ValueError("ingestion history window must be between 1 and 2160 hours")


def _validate_history_window(window_hours: int, bucket_hours: int) -> None:
    _validate_window_hours(window_hours)
    if (
        isinstance(bucket_hours, bool)
        or not 1 <= bucket_hours <= _MAX_BUCKET_HOURS
        or window_hours % bucket_hours != 0
        or window_hours // bucket_hours > _MAX_HISTORY_BUCKETS
    ):
        raise ValueError("ingestion history bucket is invalid")


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= _MAX_PAGE_LIMIT:
        raise ValueError("ingestion admin limit must be between 1 and 100")
