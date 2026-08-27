"""PostgreSQL adapter for the capability-only ingestion admin plane.

Every operation invokes a fixed ``SECURITY DEFINER`` function installed by migration 0109.  The
runtime role never receives direct access to the command queue or to the raw refresh-error column.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from ...domain.catalog_sources import CatalogRefreshDue, CatalogSource
from ...domain.enums import CatalogSourceMode
from ...domain.ingestion_admin import (
    CatalogConcentrationEntry,
    CatalogFreshnessBucket,
    IngestionBuildIdentity,
    IngestionCommand,
    IngestionCommandAction,
    IngestionCommandDetail,
    IngestionCommandLease,
    IngestionCommandProgress,
    IngestionCommandRun,
    IngestionCommandRunTarget,
    IngestionCommandStatus,
    IngestionEffectiveStatus,
    IngestionFilterMetadata,
    IngestionFilterValue,
    IngestionFleetShapeEntry,
    IngestionFleetSummary,
    IngestionHistoryBucket,
    IngestionHistorySummary,
    IngestionHistoryWindow,
    IngestionOverview,
    IngestionOverviewSummary,
    IngestionPolicyStatus,
    IngestionReviewStatus,
    IngestionRunCommandLink,
    IngestionRunPage,
    IngestionRunResourceEvidence,
    IngestionRunSourceConfiguration,
    IngestionRunStageEvidence,
    IngestionRunStatus,
    IngestionRunTimeline,
    IngestionRunTimelineEntry,
    IngestionSourceConfiguration,
    IngestionSourceConfigurationUpdate,
    IngestionSourceDetail,
    IngestionSourceEnabledBulkUpdate,
    IngestionSourceHealth,
    IngestionSourcePage,
    IngestionSourceRevisionTarget,
    IngestionSourceStatus,
    IngestionStageSummaryEntry,
    IngestionThroughputBucket,
    SafeCommandResult,
)
from ...infra.db import system_session_scope
from ...ports.ingestion_admin import (
    IngestionCommandConflictError,
    IngestionCommandRejectedError,
    IngestionCommandUnavailableError,
    IngestionExecutionDescriptorRegistry,
    IngestionSourceConfigurationConflictError,
    IngestionSourceConfigurationUnavailableError,
    IngestionSourceNotFoundError,
)

_SOURCE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{1,79}")
_RUN_STATUSES = frozenset({"running", "paused", "succeeded", "failed"})
_SOURCE_STATES = frozenset({"all", "active", "due", "blocked", "failed"})
_SOURCE_SORT_FIELDS = frozenset(
    {"source", "health", "catalog", "last_success", "latest_run", "output"}
)
_SORT_DIRECTIONS = frozenset({"asc", "desc"})
_MAX_QUERY_LENGTH = 200
_MAX_FACET_LENGTH = 300
_MAX_PAGE_LIMIT = 100
_MAX_CADENCE_LIMIT = 500
_MAX_OFFSET = 100_000
_MIN_WINDOW_HOURS = 1
_MAX_WINDOW_HOURS = 2_160
_MAX_BUCKET_HOURS = 168
_MAX_HISTORY_BUCKETS = 120
_MAX_CONCENTRATION_ROWS = 200
_MIN_LEASE_SECONDS = 300
_MAX_LEASE_SECONDS = 21_600
_MIN_RETRY_SECONDS = 1
_MAX_RETRY_SECONDS = 21_600
_MAX_RESULT_BYTES = 4_096
_MAX_STAGE_TIMESTAMP_LENGTH = 64
_COMMAND_ERROR_CODES = frozenset(
    {
        "source_refresh_failed",
        "due_refresh_failed",
        "invalid_result",
        "worker_unavailable",
    }
)
_RUN_STAGE_PLAN = (
    ("admission", "Admission", None),
    ("collect", "Collect", "adapter_boundary_includes_extract_enrich"),
    ("extract_enrich", "Extract / enrich", "included_in_collect_adapter_boundary"),
    ("normalize_dedupe", "Normalize / dedupe", "included_in_catalog_commit_boundary"),
    ("catalog_publish", "Catalog / publish", "commit_boundary_includes_normalize_dedupe"),
)
_RUN_TIMELINE_STAGE_ORDER = {
    stage: index for index, (stage, _label, _note_code) in enumerate(_RUN_STAGE_PLAN)
}
_RUN_TIMELINE_EVENT_ORDER = {
    "command_requested": 0,
    "command_started": 1,
    "run_attempt_started": 2,
    "stage_observed": 3,
    "execution_observed": 4,
    "run_status_observed": 5,
    "command_completed": 6,
}
_MAX_RUN_TIMELINE_ENTRIES = 11


class PostgresIngestionAdminRepository:
    """Read bounded admin projections and advance the leased command capability."""

    def __init__(
        self,
        execution_descriptors: IngestionExecutionDescriptorRegistry | None = None,
    ) -> None:
        self._execution_descriptors = execution_descriptors

    async def overview(self) -> IngestionOverview:
        async with system_session_scope() as session:
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM public.fn_get_ingestion_admin_overview_v2()")
                    )
                )
                .mappings()
                .one()
            )
        return _overview_from_row(row)

    async def fleet_summary(
        self,
        *,
        window_hours: int,
        include_fixtures: bool,
    ) -> IngestionFleetSummary:
        """Return one window-scoped fleet rollup computed entirely in the database."""
        _validate_window_hours(window_hours)
        async with system_session_scope() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM public.fn_get_ingestion_admin_fleet_summary_v1(:hours, :fixtures)
                        """
                        ),
                        {"hours": window_hours, "fixtures": include_fixtures},
                    )
                )
                .mappings()
                .one()
            )
        return _fleet_summary_from_row(row)

    async def stage_summary(
        self,
        *,
        window_hours: int,
        include_fixtures: bool,
    ) -> list[IngestionStageSummaryEntry]:
        """Return all five declared stages, each graded by the evidence that exists for it."""
        _validate_window_hours(window_hours)
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM public.fn_get_ingestion_admin_stage_summary_v1(:hours, :fixtures)
                        """
                        ),
                        {"hours": window_hours, "fixtures": include_fixtures},
                    )
                )
                .mappings()
                .all()
            )
        return [_stage_summary_from_row(row) for row in rows]

    async def catalog_freshness(self) -> list[CatalogFreshnessBucket]:
        """Return served upcoming events bucketed by their source's last successful fetch."""
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM public.fn_get_catalog_freshness_summary_v1(NULL)"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_catalog_freshness_from_row(row) for row in rows]

    async def source_health(
        self,
        *,
        include_fixtures: bool,
    ) -> list[IngestionSourceHealth]:
        """Return the whole graded registry, unpaginated: a roster is a set, not a page."""
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM public.fn_list_ingestion_admin_source_health_v1(:fixtures)
                        """
                        ),
                        {"fixtures": include_fixtures},
                    )
                )
                .mappings()
                .all()
            )
        return [_source_health_from_row(row) for row in rows]

    async def fleet_shape(self) -> list[IngestionFleetShapeEntry]:
        """Return sources and served events per adapter mode."""
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text("SELECT * FROM public.fn_get_ingestion_fleet_shape_v1()")
                    )
                )
                .mappings()
                .all()
            )
        return [_fleet_shape_from_row(row) for row in rows]

    async def throughput(
        self,
        *,
        window_hours: int,
        bucket_hours: int,
    ) -> list[IngestionThroughputBucket]:
        """Return gap-filled pipeline volume buckets."""
        _validate_history_window(window_hours, bucket_hours)
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM public.fn_get_ingestion_throughput_v1(:hours, :bucket)
                        """
                        ),
                        {"hours": window_hours, "bucket": bucket_hours},
                    )
                )
                .mappings()
                .all()
            )
        return [_throughput_from_row(row) for row in rows]

    async def catalog_concentration(
        self,
        *,
        limit: int,
    ) -> list[CatalogConcentrationEntry]:
        """Return per-source share of served upcoming events, ranked."""
        if limit < 1 or limit > _MAX_CONCENTRATION_ROWS:
            raise ValueError("catalog concentration limit is invalid")
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM public.fn_get_catalog_concentration_v1(:limit)"
                        ),
                        {"limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return [_concentration_from_row(row) for row in rows]

    async def list_sources(
        self,
        *,
        query: str | None = None,
        state: str = "all",
        mode: str | None = None,
        publisher: str | None = None,
        region: str | None = None,
        source_key: str | None = None,
        include_fixtures: bool = False,
        sort_by: str = "source",
        sort_direction: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> IngestionSourcePage:
        _validate_page(limit, offset)
        if state not in _SOURCE_STATES:
            raise ValueError("ingestion source state is invalid")
        if query is not None and len(query) > _MAX_QUERY_LENGTH:
            raise ValueError("ingestion source query is too long")
        if sort_by not in _SOURCE_SORT_FIELDS:
            raise ValueError("ingestion source sort field is invalid")
        if sort_direction not in _SORT_DIRECTIONS:
            raise ValueError("ingestion source sort direction is invalid")
        _validate_optional_facet(mode, "mode")
        _validate_optional_facet(publisher, "publisher")
        _validate_optional_facet(region, "region")
        _validate_optional_source_key(source_key)
        parameters = {
            "query": query,
            "state": state,
            "mode": mode,
            "publisher": publisher,
            "region": region,
            "source_key": source_key,
            "include_fixtures": include_fixtures,
            "sort_by": sort_by,
            "sort_direction": sort_direction,
            "limit": limit,
            "offset": offset,
        }
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM public.fn_list_ingestion_admin_sources_v6(
                            :query, :state, :mode, :publisher, :region,
                            :source_key, :include_fixtures, :sort_by, :sort_direction,
                            :limit, :offset
                        )
                        """
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
            total = (
                int(rows[0]["total_count"])
                if rows
                else int(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT public.fn_count_ingestion_admin_sources_v2(
                                    :query, :state, :mode, :publisher, :region,
                                    :source_key, :include_fixtures
                                )
                                """
                            ),
                            parameters,
                        )
                    ).scalar_one()
                )
            )
        return IngestionSourcePage(
            items=tuple(_source_status_from_row(row) for row in rows),
            total=total,
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
        _validate_page(limit, offset)
        if status is not None and status not in _RUN_STATUSES:
            raise ValueError("ingestion run status is invalid")
        _validate_optional_source_key(source_key)
        _validate_optional_facet(mode, "mode")
        _validate_optional_facet(publisher, "publisher")
        _validate_optional_facet(region, "region")
        if window_hours is not None:
            _validate_window_hours(window_hours)
        parameters = {
            "status": status,
            "source_key": source_key,
            "mode": mode,
            "publisher": publisher,
            "region": region,
            "window_hours": window_hours,
            "include_fixtures": include_fixtures,
            "limit": limit,
            "offset": offset,
        }
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM public.fn_list_ingestion_admin_runs_v4(
                            :status, :source_key, :mode, :publisher, :region,
                            :window_hours, :include_fixtures, :limit, :offset
                        )
                        """
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
            total = (
                int(rows[0]["total_count"])
                if rows
                else int(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT public.fn_count_ingestion_admin_runs_v2(
                                    :status, :source_key, :mode, :publisher, :region,
                                    :window_hours, :include_fixtures
                                )
                                """
                            ),
                            parameters,
                        )
                    ).scalar_one()
                )
            )
        return IngestionRunPage(
            items=tuple(_run_status_from_row(row, self._execution_descriptors) for row in rows),
            total=total,
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
        _validate_optional_facet(mode, "mode")
        _validate_optional_facet(publisher, "publisher")
        _validate_optional_facet(region, "region")
        parameters = {
            "query": query,
            "state": state,
            "mode": mode,
            "publisher": publisher,
            "region": region,
            "include_fixtures": include_fixtures,
        }
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_list_ingestion_admin_filter_values_v2(
                                :query, :state, :mode, :publisher, :region,
                                :include_fixtures
                            )
                            """
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
        facets: dict[str, list[IngestionFilterValue]] = {
            "mode": [],
            "publisher": [],
            "region": [],
        }
        for row in rows:
            dimension = str(row["dimension"])
            if dimension not in facets:
                raise RuntimeError("ingestion filter dimension is invalid")
            facets[dimension].append(
                IngestionFilterValue(
                    value=str(row["value"]),
                    count=int(row["source_count"]),
                )
            )
        return IngestionFilterMetadata(
            modes=tuple(facets["mode"]),
            publishers=tuple(facets["publisher"]),
            regions=tuple(facets["region"]),
        )

    async def get_source_detail(
        self,
        source_key: str,
        *,
        window_hours: int,
        bucket_hours: int,
        include_fixtures: bool = False,
    ) -> IngestionSourceDetail | None:
        _validate_optional_source_key(source_key)
        _validate_history_window(window_hours, bucket_hours)
        parameters = {
            "source_key": source_key,
            "window_hours": window_hours,
            "bucket_hours": bucket_hours,
            "include_fixtures": include_fixtures,
        }
        async with system_session_scope() as session:
            source_row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_get_ingestion_admin_source_detail_v2(
                                :source_key, :include_fixtures
                            )
                            """
                        ),
                        parameters,
                    )
                )
                .mappings()
                .first()
            )
            if source_row is None:
                return None
            summary_row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_get_ingestion_admin_source_summary(
                                :source_key, :window_hours, :include_fixtures
                            )
                            """
                        ),
                        parameters,
                    )
                )
                .mappings()
                .one()
            )
            history_rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_list_ingestion_admin_source_history(
                                :source_key, :window_hours, :bucket_hours,
                                :include_fixtures
                            )
                            """
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
            recent_rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_list_ingestion_admin_runs_v4(
                                NULL, :source_key, NULL, NULL, NULL,
                                :window_hours, :include_fixtures, 20, 0
                            )
                            """
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
        return _source_detail_from_rows(
            source_row,
            summary_row,
            history_rows,
            recent_rows,
            window_hours=window_hours,
            bucket_hours=bucket_hours,
            execution_descriptors=self._execution_descriptors,
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
        requested_by: str,
    ) -> IngestionSourceConfigurationUpdate:
        async with system_session_scope() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_update_ingestion_admin_source_configuration_v2(
                                :source_key, :expected_revision, :seed_url,
                                CAST(:approved_origins AS text[]), :mode, :enabled,
                                :handoff_only, :review_expires_at,
                                :refresh_interval_minutes, :min_interval_ms,
                                :page_limit, :requested_by
                            )
                            """
                        ),
                        {
                            "source_key": source_key,
                            "expected_revision": expected_revision,
                            "seed_url": seed_url,
                            "approved_origins": list(approved_origins),
                            "mode": mode,
                            "enabled": enabled,
                            "handoff_only": handoff_only,
                            "review_expires_at": review_expires_at,
                            "refresh_interval_minutes": refresh_interval_minutes,
                            "min_interval_ms": min_interval_ms,
                            "page_limit": page_limit,
                            "requested_by": requested_by,
                        },
                    )
                )
                .mappings()
                .one()
            )
        outcome = str(row["outcome"])
        if outcome == "not_found":
            raise IngestionSourceNotFoundError("source_not_found")
        if outcome == "conflict":
            raise IngestionSourceConfigurationConflictError("source_revision_conflict")
        if outcome == "unavailable":
            raise IngestionSourceConfigurationUnavailableError("source_unavailable")
        if outcome == "invalid":
            raise IngestionCommandRejectedError("invalid_command")
        if outcome != "updated":
            raise RuntimeError("source configuration update returned an unknown outcome")
        return IngestionSourceConfigurationUpdate(
            source_key=source_key,
            source_revision=int(row["source_revision"]),
            reviewed_at=_required_datetime(row["reviewed_at"]),
            updated_at=_required_datetime(row["updated_at"]),
        )

    async def set_sources_enabled(
        self,
        targets: tuple[IngestionSourceRevisionTarget, ...],
        *,
        enabled: bool,
        requested_by: str,
    ) -> IngestionSourceEnabledBulkUpdate:
        serialized_targets = json.dumps(
            [
                {
                    "source_key": target.source_key,
                    "expected_revision": target.expected_revision,
                }
                for target in targets
            ],
            separators=(",", ":"),
        )
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_set_ingestion_admin_sources_enabled_v2(
                                CAST(:targets AS jsonb), :enabled, :requested_by
                            )
                            """
                        ),
                        {
                            "targets": serialized_targets,
                            "enabled": enabled,
                            "requested_by": requested_by,
                        },
                    )
                )
                .mappings()
                .all()
            )
        if not rows:
            raise RuntimeError("source enabled update returned no outcome")
        outcome = str(rows[0]["outcome"])
        if outcome == "not_found":
            raise IngestionSourceNotFoundError("source_not_found")
        if outcome == "conflict":
            raise IngestionSourceConfigurationConflictError("source_revision_conflict")
        if outcome == "unavailable":
            raise IngestionSourceConfigurationUnavailableError("source_unavailable")
        if outcome == "invalid":
            raise IngestionCommandRejectedError("invalid_command")
        if outcome != "updated":
            raise RuntimeError("source enabled update returned an unknown outcome")
        requested = int(rows[0]["requested_count"])
        updated = int(rows[0]["updated_count"])
        unchanged = int(rows[0]["unchanged_count"])
        items = tuple(
            IngestionSourceConfigurationUpdate(
                source_key=str(row["result_source_key"]),
                source_revision=int(row["source_revision"]),
                reviewed_at=_required_datetime(row["reviewed_at"]),
                updated_at=_required_datetime(row["updated_at"]),
            )
            for row in rows
            if row["result_source_key"] is not None
        )
        if requested != len(targets) or updated != len(items) or requested != updated + unchanged:
            raise RuntimeError("source enabled update returned inconsistent counts")
        return IngestionSourceEnabledBulkUpdate(
            enabled=enabled,
            requested=requested,
            updated=updated,
            unchanged=unchanged,
            items=items,
        )

    async def list_commands(self, limit: int = 50) -> tuple[IngestionCommand, ...]:
        _validate_limit(limit)
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text("SELECT * FROM public.fn_list_ingestion_admin_commands_v3(:limit)"),
                        {"limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_command_from_row(row) for row in rows)

    async def get_command(self, command_id: UUID) -> IngestionCommand | None:
        async with system_session_scope() as session:
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM public.fn_get_ingestion_admin_command_v3(:command_id)"),
                        {"command_id": command_id},
                    )
                )
                .mappings()
                .first()
            )
        return _command_from_row(row) if row is not None else None

    async def get_command_detail(self, command_id: UUID) -> IngestionCommandDetail | None:
        """Return a command and its latest attempt's exact cadence/source-run associations."""
        async with system_session_scope() as session:
            command_row = (
                (
                    await session.execute(
                        text("SELECT * FROM public.fn_get_ingestion_admin_command_v3(:command_id)"),
                        {"command_id": command_id},
                    )
                )
                .mappings()
                .first()
            )
            if command_row is None:
                return None
            run_rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_list_ingestion_admin_command_runs_v1(:command_id)
                            """
                        ),
                        {"command_id": command_id},
                    )
                )
                .mappings()
                .all()
            )
            generated_at = _required_datetime(
                (await session.execute(text("SELECT statement_timestamp()"))).scalar_one()
            )
        command = _command_from_row(command_row)
        runs = tuple(_command_run_from_row(row) for row in run_rows)
        return IngestionCommandDetail(
            generated_at=generated_at,
            command=command,
            progress=_command_progress(command, runs, generated_at),
            runs=runs,
        )

    async def link_command_runs(
        self,
        command_id: UUID,
        attempt_count: int,
        lease_token: UUID,
        targets: tuple[IngestionCommandRunTarget, ...],
    ) -> None:
        """Atomically commit one bounded execution plan before child provider work."""
        if isinstance(attempt_count, bool) or attempt_count < 1:
            raise ValueError("ingestion command attempt count is invalid")
        if len(targets) > _MAX_CADENCE_LIMIT:
            raise ValueError("ingestion command run plan is too large")
        if len({target.position for target in targets}) != len(targets):
            raise ValueError("ingestion command run plan positions must be unique")
        async with system_session_scope() as session:
            for target in targets:
                linked = bool(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT public.fn_link_ingestion_admin_command_run_v1(
                                    :command_id, :attempt_count, :lease_token,
                                    :source_key, :run_key, :position
                                )
                                """
                            ),
                            {
                                "command_id": command_id,
                                "attempt_count": attempt_count,
                                "lease_token": lease_token,
                                "source_key": target.source_key,
                                "run_key": target.run_key,
                                "position": target.position,
                            },
                        )
                    ).scalar_one()
                )
                if not linked:
                    raise RuntimeError("ingestion command run plan lost its live lease")

    async def list_due_refreshes(
        self,
        now: datetime,
        *,
        limit: int,
    ) -> list[CatalogRefreshDue]:
        """Return a fixture-filtered, failure-gated due set before applying the caller's limit.

        The projection withholds a slot whose latest run failed until its backoff elapses, and
        withdraws one entirely on a configuration defect or a runaway attempt count, so a source
        that cannot succeed stops displacing healthy sources from the single fleet pass (0155).
        """
        _validate_due_limit(limit)
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM public.fn_list_ingestion_admin_due_sources_v3(:now, :limit)
                        """
                        ),
                        {"now": now, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return [
            CatalogRefreshDue(
                source=_catalog_source_from_row(row),
                due_at=row["due_at"],
                last_succeeded_at=row["last_succeeded_at"],
            )
            for row in rows
        ]

    async def enqueue(
        self,
        command_id: UUID,
        action: IngestionCommandAction,
        source_key: str | None,
        requested_by: str,
        release_revision: str = "development",
        image_digest: str | None = None,
    ) -> IngestionCommand:
        async with system_session_scope() as session:
            outcome = str(
                (
                    await session.execute(
                        text(
                            """
                            SELECT public.fn_enqueue_ingestion_admin_command_v2(
                                :command_id, :action, :source_key, :requested_by,
                                :release_revision, :image_digest
                            )
                            """
                        ),
                        {
                            "command_id": command_id,
                            "action": action.value,
                            "source_key": source_key,
                            "requested_by": requested_by,
                            "release_revision": release_revision,
                            "image_digest": image_digest,
                        },
                    )
                ).scalar_one()
            )
            _raise_for_enqueue_outcome(outcome)
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM public.fn_get_ingestion_admin_command_v3(:command_id)"),
                        {"command_id": command_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise RuntimeError("ingestion command enqueue returned no durable command")
        return _command_from_row(row)

    async def claim_batch(
        self,
        limit: int,
        lease_seconds: int,
        executor_release_revision: str = "development",
        executor_image_digest: str | None = None,
    ) -> tuple[IngestionCommandLease, ...]:
        _validate_limit(limit)
        if not _MIN_LEASE_SECONDS <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError("ingestion command lease must be between 300 and 21600 seconds")
        async with system_session_scope() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT *
                        FROM public.fn_claim_ingestion_admin_commands_v2(
                            :limit, :lease_seconds,
                            :executor_release_revision, :executor_image_digest
                        )
                        """
                        ),
                        {
                            "limit": limit,
                            "lease_seconds": lease_seconds,
                            "executor_release_revision": executor_release_revision,
                            "executor_image_digest": executor_image_digest,
                        },
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            IngestionCommandLease(
                command_id=row["command_id"],
                action=IngestionCommandAction(str(row["action"])),
                source_key=row["source_key"],
                attempt_count=int(row["attempt_count"]),
                lease_token=row["lease_token"],
            )
            for row in rows
        )

    async def renew_lease(
        self,
        lease: IngestionCommandLease,
        lease_seconds: int,
    ) -> bool:
        if not _MIN_LEASE_SECONDS <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError("ingestion command lease must be between 300 and 21600 seconds")
        async with system_session_scope() as session:
            renewed = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_renew_ingestion_admin_command_lease(
                            :command_id, :lease_token, :lease_seconds
                        )
                        """
                    ),
                    {
                        "command_id": lease.command_id,
                        "lease_token": lease.lease_token,
                        "lease_seconds": lease_seconds,
                    },
                )
            ).scalar_one()
        return bool(renewed)

    async def complete(
        self,
        lease: IngestionCommandLease,
        result: SafeCommandResult,
    ) -> bool:
        payload = _safe_result_payload(result)
        async with system_session_scope() as session:
            completed = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_complete_ingestion_admin_command(
                            :command_id, :lease_token, CAST(:result AS jsonb)
                        )
                        """
                    ),
                    {
                        "command_id": lease.command_id,
                        "lease_token": lease.lease_token,
                        "result": payload,
                    },
                )
            ).scalar_one()
        return bool(completed)

    async def defer(
        self,
        lease: IngestionCommandLease,
        retry_after_seconds: int,
    ) -> bool:
        if (
            isinstance(retry_after_seconds, bool)
            or not _MIN_RETRY_SECONDS <= retry_after_seconds <= _MAX_RETRY_SECONDS
        ):
            raise ValueError("ingestion command retry delay must be between 1 and 21600 seconds")
        async with system_session_scope() as session:
            deferred = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_defer_ingestion_admin_command(
                            :command_id, :lease_token, :retry_after_seconds
                        )
                        """
                    ),
                    {
                        "command_id": lease.command_id,
                        "lease_token": lease.lease_token,
                        "retry_after_seconds": retry_after_seconds,
                    },
                )
            ).scalar_one()
        return bool(deferred)

    async def fail(self, lease: IngestionCommandLease, error_code: str) -> bool:
        if error_code not in _COMMAND_ERROR_CODES:
            raise ValueError("ingestion command error code is invalid")
        async with system_session_scope() as session:
            failed = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_fail_ingestion_admin_command(
                            :command_id, :lease_token, :error_code
                        )
                        """
                    ),
                    {
                        "command_id": lease.command_id,
                        "lease_token": lease.lease_token,
                        "error_code": error_code,
                    },
                )
            ).scalar_one()
        return bool(failed)


def _fleet_shape_from_row(row: RowMapping) -> IngestionFleetShapeEntry:
    return IngestionFleetShapeEntry(
        mode=str(row["mode"]),
        sources=int(row["sources"]),
        scheduled=int(row["scheduled"]),
        paused=int(row["paused"]),
        retired=int(row["retired"]),
        upcoming_events=int(row["upcoming_events"]),
        events_per_source=_optional_float(row.get("events_per_source")),
        pct_of_sources=_optional_float(row.get("pct_of_sources")),
        pct_of_events=_optional_float(row.get("pct_of_events")),
    )


def _throughput_from_row(row: RowMapping) -> IngestionThroughputBucket:
    return IngestionThroughputBucket(
        bucket_start=row["bucket_start"],
        runs=int(row["runs"]),
        succeeded=int(row["succeeded"]),
        failed=int(row["failed"]),
        deferred=int(row["deferred"]),
        collected=int(row["collected"]),
        published=int(row["published"]),
        yield_pct=_optional_float(row.get("yield_pct")),
        median_duration_ms=_optional_int(row.get("median_duration_ms")),
    )


def _concentration_from_row(row: RowMapping) -> CatalogConcentrationEntry:
    return CatalogConcentrationEntry(
        rank=int(row["rank"]),
        source_key=str(row["source_key"]),
        display_name=str(row["display_name"]),
        mode=str(row["mode"]),
        publisher=str(row["publisher"]),
        upcoming_events=int(row["upcoming_events"]),
        pct=_optional_float(row.get("pct")),
        cumulative_pct=_optional_float(row.get("cumulative_pct")),
        total_events=int(row["total_events"]),
        total_sources=int(row["total_sources"]),
    )


def _source_health_from_row(row: RowMapping) -> IngestionSourceHealth:
    return IngestionSourceHealth(
        source_key=str(row["source_key"]),
        display_name=str(row["display_name"]),
        publisher=str(row["publisher"]),
        mode=str(row["mode"]),
        region=str(row["region"]),
        enabled=bool(row["enabled"]),
        retired_at=row["retired_at"],
        refresh_interval_minutes=int(row["refresh_interval_minutes"]),
        page_limit=int(row["page_limit"]),
        health=str(row["health"]),
        run_state=str(row["run_state"]),
        freshness_state=str(row["freshness_state"]),
        retry_state=str(row["retry_state"]),
        yield_state=str(row["yield_state"]),
        last_attempt_at=row["last_attempt_at"],
        last_success_at=row["last_success_at"],
        last_catalog_change_at=row["last_catalog_change_at"],
        latest_run_status=_optional_str(row.get("latest_run_status")),
        latest_run_error=_optional_str(row.get("latest_run_error")),
        latest_attempt_count=_optional_int(row.get("latest_attempt_count")),
        upcoming_events=int(row["upcoming_events"]),
        hours_since_success=_optional_float(row.get("hours_since_success")),
    )


def _fleet_summary_from_row(row: RowMapping) -> IngestionFleetSummary:
    return IngestionFleetSummary(
        generated_at=row["generated_at"],
        window_start=row["window_start"],
        window_hours=int(row["window_hours"]),
        runs=int(row["runs"]),
        succeeded=int(row["succeeded"]),
        failed=int(row["failed"]),
        running=int(row["running"]),
        paused=int(row["paused"]),
        sources_run=int(row["sources_run"]),
        sources_failed=int(row["sources_failed"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        retrying_runs=int(row["retrying_runs"]),
        candidates=int(row["candidates"]),
        canonicals=int(row["canonicals"]),
        zero_yield_runs=int(row["zero_yield_runs"]),
        wall_ms=int(row["wall_ms"]),
        duration_p50_ms=_optional_int(row.get("duration_p50_ms")),
        duration_p95_ms=_optional_int(row.get("duration_p95_ms")),
        duration_p99_ms=_optional_int(row.get("duration_p99_ms")),
    )


def _stage_summary_from_row(row: RowMapping) -> IngestionStageSummaryEntry:
    folded = row.get("folded_into")
    return IngestionStageSummaryEntry(
        stage=str(row["stage"]),
        stage_position=int(row["stage_position"]),
        evidence_status=str(row["evidence_status"]),
        folded_into=None if folded is None else str(folded),
        runs_with_evidence=int(row["runs_with_evidence"]),
        observations=int(row["observations"]),
        total_ms=int(row["total_ms"]),
        avg_ms=_optional_int(row.get("avg_ms")),
        p95_ms=_optional_int(row.get("p95_ms")),
        failed_count=int(row["failed_count"]),
        pct_of_wall=_optional_float(row.get("pct_of_wall")),
    )


def _catalog_freshness_from_row(row: RowMapping) -> CatalogFreshnessBucket:
    return CatalogFreshnessBucket(
        bucket=str(row["bucket"]),
        bucket_position=int(row["bucket_position"]),
        sources=int(row["sources"]),
        events=int(row["events"]),
        pct=_optional_float(row.get("pct")),
    )


def _overview_from_row(row: RowMapping) -> IngestionOverview:
    return IngestionOverview(
        generated_at=row["generated_at"],
        policy=IngestionPolicyStatus(
            allowed=bool(row["policy_allowed"]),
            reason=str(row["policy_reason"]),
            code=str(row["policy_code"]),
        ),
        summary=IngestionOverviewSummary(
            sources=int(row["sources"]),
            active_sources=int(row["active_sources"]),
            due_sources=int(row["due_sources"]),
            running_runs=int(row["running_runs"]),
            failed_runs_24h=int(row["failed_runs_24h"]),
            catalog_events=int(row["catalog_events"]),
            pending_commands=int(row["pending_commands"]),
            fixture_sources=int(row["fixture_sources"]),
        ),
        latest_success_at=row["latest_success_at"],
    )


def _source_status_from_row(row: RowMapping) -> IngestionSourceStatus:
    latest_run = None
    if row["latest_run_key"] is not None:
        latest_run = _run_status_from_row(_detail_latest_run_mapping(row))
    return IngestionSourceStatus(
        source_key=str(row["source_key"]),
        display_name=str(row["display_name"]),
        publisher=str(row["publisher"]),
        mode=str(row["mode"]),
        region=str(row["region"]),
        seed_url=str(row["seed_url"]),
        enabled=bool(row["enabled"]),
        source_revision=int(row["source_revision"]),
        review_status=IngestionReviewStatus(str(row["review_status"])),
        effective_status=IngestionEffectiveStatus(str(row["effective_status"])),
        policy_blocked=bool(row["policy_blocked"]),
        due=bool(row["due"]),
        last_succeeded_at=row["last_succeeded_at"],
        next_due_at=row["next_due_at"],
        event_count=int(row["event_count"]),
        latest_run=latest_run,
        retired_at=row["retired_at"],
        retired_reason=_optional_str(row["retired_reason"]),
        superseded_by_source_key=_optional_str(row["superseded_by_source_key"]),
    )


def _run_status_from_row(
    row: RowMapping | Mapping[str, object],
    execution_descriptors: IngestionExecutionDescriptorRegistry | None = None,
) -> IngestionRunStatus:
    trigger = str(row.get("trigger", "cadence_or_manual"))
    status = str(row["status"])
    started_at = _required_datetime(row["started_at"])
    completed_at = _optional_datetime(row["completed_at"])
    duration_ms = _optional_int(row.get("duration_ms"))
    mode = _optional_str(row.get("mode"))
    page_limit = _optional_int(row.get("page_limit"))
    source_configuration = _run_source_configuration(row, mode, page_limit)
    command = _run_command_link(row)
    resources = _run_resource_evidence(row)
    stage_trace = _run_stage_trace(row.get("stage_metrics"), resources is not None)
    timeline = _run_timeline(
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        command=command,
        resources=resources,
        stage_trace=stage_trace,
    )
    execution = (
        execution_descriptors.describe(
            source_key=str(row["source_key"]),
            mode=mode,
            page_limit=page_limit,
            trigger=trigger,
        )
        if execution_descriptors is not None
        else None
    )
    return IngestionRunStatus(
        source_key=str(row["source_key"]),
        display_name=str(row["display_name"]),
        run_key=str(row["run_key"]),
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        candidate_count=_optional_int(row["candidate_count"]),
        canonical_count=_optional_int(row["canonical_count"]),
        error=_optional_str(row["error"]),
        attempt_count=_required_int(row["attempt_count"]),
        duration_ms=duration_ms,
        source_revision=_optional_int(row.get("source_revision")),
        release_revision=_optional_str(row.get("release_revision")),
        image_digest=_optional_str(row.get("image_digest")),
        provenance_status=str(row.get("provenance_status", "legacy_unavailable")),
        trigger=trigger,
        is_latest_for_source=bool(row.get("is_latest_for_source", False)),
        resolved_by_newer_success=bool(row.get("resolved_by_newer_success", False)),
        source_configuration=source_configuration,
        command=command,
        execution=execution,
        resources=resources,
        stage_trace=stage_trace,
        timeline=timeline,
    )


def _run_source_configuration(
    row: RowMapping | Mapping[str, object],
    mode: str | None,
    page_limit: int | None,
) -> IngestionRunSourceConfiguration | None:
    """Return reviewed source facts, or ``None`` for explicitly included fixture rows."""
    refresh_interval = _optional_int(row.get("refresh_interval_minutes"))
    min_interval = _optional_int(row.get("min_interval_ms"))
    if mode is None or page_limit is None or refresh_interval is None or min_interval is None:
        return None
    return IngestionRunSourceConfiguration(
        mode=mode,
        reviewed_at=_optional_datetime(row.get("reviewed_at")),
        review_expires_at=_optional_datetime(row.get("review_expires_at")),
        refresh_interval_minutes=refresh_interval,
        min_interval_ms=min_interval,
        page_limit=page_limit,
    )


def _run_command_link(
    row: RowMapping | Mapping[str, object],
) -> IngestionRunCommandLink | None:
    command_id = row.get("command_id")
    if command_id is None:
        return None
    if not isinstance(command_id, UUID):
        raise RuntimeError("ingestion run command identity is invalid")
    action = _optional_str(row.get("command_action"))
    requested_at = _optional_datetime(row.get("command_requested_at"))
    if action is None or requested_at is None:
        raise RuntimeError("ingestion run command linkage is incomplete")
    return IngestionRunCommandLink(
        command_id=command_id,
        action=action,
        requested_at=requested_at,
        started_at=_optional_datetime(row.get("command_started_at")),
        completed_at=_optional_datetime(row.get("command_completed_at")),
    )


def _run_resource_evidence(
    row: RowMapping | Mapping[str, object],
) -> IngestionRunResourceEvidence | None:
    execution_count = _optional_int(row.get("execution_count"))
    if execution_count is None:
        return None
    wall_time_ms = _required_int(row.get("execution_wall_time_ms"))
    process_cpu_time_ms = _optional_int(row.get("process_cpu_time_ms"))
    cpu_percent = (
        round((process_cpu_time_ms / wall_time_ms) * 100.0, 2)
        if process_cpu_time_ms is not None and wall_time_ms > 0
        else None
    )
    measurement_source = _optional_str(row.get("measurement_source"))
    measurement_scope = _optional_str(row.get("measurement_scope"))
    measurement_quality = _optional_str(row.get("measurement_quality"))
    last_outcome = _optional_str(row.get("execution_last_outcome_code"))
    first_observed = _optional_datetime(row.get("execution_first_observed_at"))
    last_observed = _optional_datetime(row.get("execution_last_observed_at"))
    if None in {
        measurement_source,
        measurement_scope,
        measurement_quality,
        last_outcome,
        first_observed,
        last_observed,
    }:
        raise RuntimeError("ingestion run resource evidence is incomplete")
    assert measurement_source is not None
    assert measurement_scope is not None
    assert measurement_quality is not None
    assert last_outcome is not None
    assert first_observed is not None
    assert last_observed is not None
    return IngestionRunResourceEvidence(
        execution_count=execution_count,
        wall_time_ms=wall_time_ms,
        process_cpu_time_ms=process_cpu_time_ms,
        cpu_utilization_percent=cpu_percent,
        rss_before_bytes=_optional_int(row.get("rss_before_bytes")),
        rss_after_bytes=_optional_int(row.get("rss_after_bytes")),
        boundary_observed_peak_rss_bytes=_optional_int(row.get("boundary_observed_peak_rss_bytes")),
        process_lifetime_peak_rss_bytes=_optional_int(row.get("process_lifetime_peak_rss_bytes")),
        measurement_source=measurement_source,
        measurement_scope=measurement_scope,
        measurement_quality=measurement_quality,
        last_outcome_code=last_outcome,
        first_observed_at=first_observed,
        last_observed_at=last_observed,
    )


def _run_stage_trace(
    value: object, has_execution_evidence: bool
) -> tuple[IngestionRunStageEvidence, ...]:
    rows = _stage_metric_rows(value)
    by_stage = {str(row["stage"]): row for row in rows}
    has_stage_evidence = bool(rows) or has_execution_evidence
    trace: list[IngestionRunStageEvidence] = []
    for stage, label, note_code in _RUN_STAGE_PLAN:
        metric = by_stage.get(stage)
        if metric is not None:
            trace.append(
                IngestionRunStageEvidence(
                    stage=stage,
                    label=label,
                    evidence_status="measured",
                    observation_count=_required_int(metric.get("observation_count")),
                    duration_ms=_required_int(metric.get("duration_ms")),
                    last_outcome_code=_optional_str(metric.get("last_outcome_code")),
                    first_observed_at=_optional_stage_datetime(metric.get("first_observed_at")),
                    last_observed_at=_optional_stage_datetime(metric.get("last_observed_at")),
                    note_code=note_code,
                )
            )
            continue
        inseparable = stage in {"extract_enrich", "normalize_dedupe"}
        trace.append(
            IngestionRunStageEvidence(
                stage=stage,
                label=label,
                evidence_status=(
                    "not_separately_instrumented"
                    if has_stage_evidence and inseparable
                    else ("not_observed" if has_stage_evidence else "legacy_unavailable")
                ),
                observation_count=0,
                duration_ms=None,
                last_outcome_code=None,
                first_observed_at=None,
                last_observed_at=None,
                note_code=(note_code if has_stage_evidence else None),
            )
        )
    return tuple(trace)


def _run_timeline(
    *,
    status: str,
    started_at: datetime,
    completed_at: datetime | None,
    duration_ms: int | None,
    command: IngestionRunCommandLink | None,
    resources: IngestionRunResourceEvidence | None,
    stage_trace: tuple[IngestionRunStageEvidence, ...],
) -> IngestionRunTimeline:
    """Compose a bounded chronology from durable transitions and aggregate observations.

    Stage and execution evidence tables aggregate retries by run and stage. Their
    ``last_observed_at`` values therefore support one typed summary point, not reconstruction of
    individual log lines or exact stage start/end spans. The run ``started_at`` is the latest
    successful claim time because retries replace it; it is therefore an attempt transition, not
    the run's original creation time. Missing evidence is omitted rather than represented by
    synthetic timeline events.
    """
    entries: list[IngestionRunTimelineEntry] = []
    if command is not None:
        entries.append(
            IngestionRunTimelineEntry(
                observed_at=command.requested_at,
                event_code="command_requested",
                timestamp_basis="durable_transition",
            )
        )
        if command.started_at is not None:
            entries.append(
                IngestionRunTimelineEntry(
                    observed_at=command.started_at,
                    event_code="command_started",
                    timestamp_basis="durable_transition",
                )
            )

    entries.append(
        IngestionRunTimelineEntry(
            observed_at=started_at,
            event_code="run_attempt_started",
            timestamp_basis="durable_transition",
        )
    )

    has_aggregate_observations = False
    for stage in stage_trace:
        if stage.evidence_status != "measured" or stage.last_observed_at is None:
            continue
        has_aggregate_observations = True
        entries.append(
            IngestionRunTimelineEntry(
                observed_at=stage.last_observed_at,
                event_code="stage_observed",
                timestamp_basis="evidence_recorded",
                stage=stage.stage,
                outcome_code=stage.last_outcome_code,
                duration_ms=stage.duration_ms,
                observation_count=stage.observation_count,
            )
        )

    if resources is not None:
        has_aggregate_observations = True
        entries.append(
            IngestionRunTimelineEntry(
                observed_at=resources.last_observed_at,
                event_code="execution_observed",
                timestamp_basis="evidence_recorded",
                outcome_code=resources.last_outcome_code,
                duration_ms=resources.wall_time_ms,
                observation_count=resources.execution_count,
            )
        )

    if completed_at is not None:
        entries.append(
            IngestionRunTimelineEntry(
                observed_at=completed_at,
                event_code="run_status_observed",
                timestamp_basis="run_projection",
                outcome_code=status,
                duration_ms=duration_ms,
            )
        )
    if command is not None and command.completed_at is not None:
        entries.append(
            IngestionRunTimelineEntry(
                observed_at=command.completed_at,
                event_code="command_completed",
                timestamp_basis="durable_transition",
            )
        )

    entries.sort(
        key=lambda entry: (
            entry.observed_at,
            _RUN_TIMELINE_EVENT_ORDER[entry.event_code],
            _RUN_TIMELINE_STAGE_ORDER.get(entry.stage or "", len(_RUN_STAGE_PLAN)),
        )
    )
    if len(entries) > _MAX_RUN_TIMELINE_ENTRIES:
        raise RuntimeError("ingestion run timeline exceeds its fixed bound")
    return IngestionRunTimeline(
        evidence_scope=(
            "lifecycle_and_aggregate_observations"
            if has_aggregate_observations
            else "lifecycle_only"
        ),
        complete=False,
        entries=tuple(entries),
    )


def _stage_metric_rows(value: object) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise RuntimeError("ingestion run stage evidence is invalid") from error
    if not isinstance(parsed, Sequence) or isinstance(parsed, str | bytes):
        raise RuntimeError("ingestion run stage evidence is not a list")
    rows: list[Mapping[str, object]] = []
    for item in parsed:
        if not isinstance(item, Mapping) or "stage" not in item:
            raise RuntimeError("ingestion run stage evidence item is invalid")
        rows.append(item)
    if len(rows) > len(_RUN_STAGE_PLAN):
        raise RuntimeError("ingestion run stage evidence exceeds its bound")
    return tuple(rows)


def _command_from_row(row: RowMapping) -> IngestionCommand:
    return IngestionCommand(
        command_id=row["command_id"],
        action=IngestionCommandAction(str(row["action"])),
        source_key=_optional_str(row["source_key"]),
        status=IngestionCommandStatus(str(row["status"])),
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        result=_safe_result_from_db(row["result"]),
        error_code=_optional_str(row["error_code"]),
        source_revision=_optional_int(row.get("source_revision")),
        release_revision=_optional_str(row.get("release_revision")),
        image_digest=_optional_str(row.get("image_digest")),
        executor_source_revision=_optional_int(row.get("executor_source_revision")),
        executor_release_revision=_optional_str(row.get("executor_release_revision")),
        executor_image_digest=_optional_str(row.get("executor_image_digest")),
        requested_by=_optional_str(row.get("requested_by")),
        attempt_count=_optional_int(row.get("attempt_count")) or 0,
        available_at=_optional_datetime(row.get("available_at")),
        lease_expires_at=_optional_datetime(row.get("lease_expires_at")),
        worker_state=_optional_str(row.get("worker_state")),
    )


def _command_run_from_row(row: RowMapping) -> IngestionCommandRun:
    status = str(row["status"])
    phase = str(row["phase"])
    if status not in {"pending", "running", "paused", "succeeded", "failed"}:
        raise RuntimeError("ingestion command child status is invalid")
    if phase not in {
        "awaiting_dispatch",
        "collecting",
        "deferred",
        "completed",
        "failed",
    }:
        raise RuntimeError("ingestion command child phase is invalid")
    position = _required_int(row["run_position"])
    if not 0 <= position < _MAX_CADENCE_LIMIT:
        raise RuntimeError("ingestion command child position is invalid")
    return IngestionCommandRun(
        position=position,
        source_key=str(row["source_key"]),
        display_name=str(row["display_name"]),
        run_key=str(row["run_key"]),
        status=status,
        phase=phase,
        linked_at=_required_datetime(row["linked_at"]),
        started_at=_optional_datetime(row["started_at"]),
        completed_at=_optional_datetime(row["completed_at"]),
        candidate_count=_optional_int(row["candidate_count"]),
        canonical_count=_optional_int(row["canonical_count"]),
        error_code=_optional_str(row["error_code"]),
        attempt_count=_optional_int(row["attempt_count"]),
        duration_ms=_optional_int(row["duration_ms"]),
        updated_at=_required_datetime(row["updated_at"]),
    )


def _command_progress(
    command: IngestionCommand,
    runs: tuple[IngestionCommandRun, ...],
    generated_at: datetime,
) -> IngestionCommandProgress:
    counts = {
        status: sum(run.status == status for run in runs)
        for status in ("pending", "running", "succeeded", "failed", "paused")
    }
    active = next(
        (run for run in runs if run.status == "running"),
        next((run for run in runs if run.status == "pending"), None),
    )
    total = len(runs)
    if not runs and command.result is not None:
        # Commands created before durable child links remain honestly aggregate-only.  Preserve
        # their bounded terminal result without manufacturing source/run associations.
        total = _safe_result_count(command.result, "due_sources")
        if command.action is IngestionCommandAction.REFRESH_SOURCE:
            total = 1
            outcome = str(command.result.get("outcome", ""))
            counts["succeeded"] = int(outcome in {"succeeded", "already_succeeded"})
            counts["failed"] = int(command.status is IngestionCommandStatus.FAILED)
        else:
            counts["succeeded"] = _safe_result_count(command.result, "succeeded")
            counts["failed"] = _safe_result_count(command.result, "failed")
            counts["paused"] = _safe_result_count(command.result, "deferred")
            accounted = counts["succeeded"] + counts["failed"] + counts["paused"]
            counts["pending"] = max(0, total - accounted)
    updated_values = [run.updated_at for run in runs]
    updated_values.extend(
        value
        for value in (command.completed_at, command.started_at, command.requested_at)
        if value is not None
    )
    updated_at = max(updated_values, default=generated_at)
    return IngestionCommandProgress(
        total=total,
        pending=counts["pending"],
        running=counts["running"],
        succeeded=counts["succeeded"],
        failed=counts["failed"],
        paused=counts["paused"],
        completed=counts["succeeded"] + counts["failed"] + counts["paused"],
        active_source_key=active.source_key if active is not None else None,
        active_display_name=active.display_name if active is not None else None,
        active_phase=active.phase if active is not None else None,
        updated_at=updated_at,
    )


def _safe_result_count(result: SafeCommandResult, key: str) -> int:
    value = result.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _source_detail_from_rows(
    source_row: RowMapping,
    summary_row: RowMapping,
    history_rows: Sequence[RowMapping],
    recent_rows: Sequence[RowMapping],
    *,
    window_hours: int,
    bucket_hours: int,
    execution_descriptors: IngestionExecutionDescriptorRegistry | None = None,
) -> IngestionSourceDetail:
    latest_run = None
    if source_row["latest_run_key"] is not None:
        latest_run = _run_status_from_row(_detail_latest_run_mapping(source_row))
    seed_url = str(source_row["seed_url"])
    seed_host = urlsplit(seed_url).hostname
    if seed_host is None:
        raise RuntimeError("ingestion source seed host is invalid")
    generated_at = summary_row["generated_at"]
    return IngestionSourceDetail(
        generated_at=generated_at,
        source=IngestionSourceConfiguration(
            source_key=str(source_row["source_key"]),
            display_name=str(source_row["display_name"]),
            publisher=str(source_row["publisher"]),
            mode=str(source_row["mode"]),
            region=str(source_row["region"]),
            seed_url=seed_url,
            seed_host=seed_host,
            enabled=bool(source_row["enabled"]),
            handoff_only=bool(source_row["handoff_only"]),
            approved_origins=tuple(str(item) for item in source_row["approved_origins"]),
            reviewed_at=source_row["reviewed_at"],
            review_expires_at=source_row["review_expires_at"],
            refresh_interval_minutes=int(source_row["refresh_interval_minutes"]),
            min_interval_ms=int(source_row["min_interval_ms"]),
            page_limit=int(source_row["page_limit"]),
            source_revision=int(source_row["source_revision"]),
            review_status=IngestionReviewStatus(str(source_row["review_status"])),
            effective_status=IngestionEffectiveStatus(str(source_row["effective_status"])),
            policy_blocked=bool(source_row["policy_blocked"]),
            due=bool(source_row["due"]),
            last_succeeded_at=source_row["last_succeeded_at"],
            next_due_at=source_row["next_due_at"],
            event_count=int(source_row["event_count"]),
            latest_run=latest_run,
            retired_at=source_row["retired_at"],
            retired_reason=_optional_str(source_row["retired_reason"]),
            superseded_by_source_key=_optional_str(source_row["superseded_by_source_key"]),
        ),
        window=IngestionHistoryWindow(
            hours=window_hours,
            bucket_hours=bucket_hours,
            starts_at=summary_row["window_starts_at"],
            ends_at=generated_at,
        ),
        summary=IngestionHistorySummary(
            total_runs=int(summary_row["total_runs"]),
            succeeded_runs=int(summary_row["succeeded_runs"]),
            failed_runs=int(summary_row["failed_runs"]),
            running_runs=int(summary_row["running_runs"]),
            success_rate=_optional_float(summary_row["success_rate"]),
            candidate_count=int(summary_row["candidate_count"]),
            canonical_count=int(summary_row["canonical_count"]),
            yield_rate=_optional_float(summary_row["yield_rate"]),
            average_duration_ms=_optional_int(summary_row["average_duration_ms"]),
            p95_duration_ms=_optional_int(summary_row["p95_duration_ms"]),
            latest_success_at=summary_row["latest_success_at"],
            latest_failure_at=summary_row["latest_failure_at"],
        ),
        history=tuple(
            IngestionHistoryBucket(
                bucket_start=row["bucket_start"],
                total_runs=int(row["total_runs"]),
                succeeded_runs=int(row["succeeded_runs"]),
                failed_runs=int(row["failed_runs"]),
                candidate_count=int(row["candidate_count"]),
                canonical_count=int(row["canonical_count"]),
                average_duration_ms=_optional_int(row["average_duration_ms"]),
            )
            for row in history_rows
        ),
        recent_runs=tuple(_run_status_from_row(row, execution_descriptors) for row in recent_rows),
        # The application service replaces this database-independent placeholder with Settings.
        current_build=IngestionBuildIdentity("unavailable", None),
    )


def _detail_latest_run_mapping(row: RowMapping) -> Mapping[str, object]:
    return {
        "source_key": row["source_key"],
        "display_name": row["display_name"],
        "run_key": row["latest_run_key"],
        "status": row["latest_run_status"],
        "started_at": row["latest_run_started_at"],
        "completed_at": row["latest_run_completed_at"],
        "candidate_count": row["latest_run_candidate_count"],
        "canonical_count": row["latest_run_canonical_count"],
        "error": row["latest_run_error"],
        "attempt_count": row["latest_run_attempt_count"],
        "duration_ms": row["latest_run_duration_ms"],
        "source_revision": row["latest_run_source_revision"],
        "release_revision": row["latest_run_release_revision"],
        "image_digest": row["latest_run_image_digest"],
        "provenance_status": row["latest_run_provenance_status"],
        "trigger": row["latest_run_trigger"],
        "is_latest_for_source": True,
        "resolved_by_newer_success": False,
    }


def _catalog_source_from_row(row: RowMapping) -> CatalogSource:
    return CatalogSource(
        source_key=str(row["source_key"]),
        display_name=str(row["display_name"]),
        publisher=str(row["publisher"]),
        seed_url=str(row["seed_url"]),
        approved_origins=tuple(str(origin) for origin in row["approved_origins"]),
        region=str(row["region"]),
        mode=CatalogSourceMode(str(row["mode"])),
        handoff_only=bool(row["handoff_only"]),
        enabled=bool(row["enabled"]),
        reviewed_at=row["reviewed_at"],
        review_expires_at=row["review_expires_at"],
        refresh_interval_minutes=int(row["refresh_interval_minutes"]),
        min_interval_ms=int(row["min_interval_ms"]),
        page_limit=int(row["page_limit"]),
        source_revision=int(row["source_revision"]),
    )


def _raise_for_enqueue_outcome(outcome: str) -> None:
    if outcome in {"enqueued", "replayed"}:
        return
    if outcome == "not_found":
        raise IngestionSourceNotFoundError("source_not_found")
    if outcome == "unavailable":
        raise IngestionCommandUnavailableError("source_unavailable")
    if outcome == "policy_blocked":
        raise IngestionCommandUnavailableError("policy_blocked")
    if outcome == "conflict":
        raise IngestionCommandConflictError("command_conflict")
    if outcome == "invalid":
        raise IngestionCommandRejectedError("invalid_command")
    raise RuntimeError("ingestion command enqueue returned an unknown outcome")


def _validate_page(limit: int, offset: int) -> None:
    _validate_limit(limit)
    if not 0 <= offset <= _MAX_OFFSET:
        raise ValueError("ingestion admin offset is invalid")


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= _MAX_PAGE_LIMIT:
        raise ValueError("ingestion admin limit must be between 1 and 100")


def _validate_due_limit(limit: int) -> None:
    if not 1 <= limit <= _MAX_CADENCE_LIMIT:
        raise ValueError("ingestion admin cadence limit must be between 1 and 500")


def _validate_optional_source_key(source_key: str | None) -> None:
    if source_key is not None and _SOURCE_KEY.fullmatch(source_key) is None:
        raise ValueError("ingestion source key is invalid")


def _validate_optional_facet(value: str | None, name: str) -> None:
    if value is not None and (not value or len(value) > _MAX_FACET_LENGTH):
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


def _safe_result_payload(result: SafeCommandResult) -> str:
    payload = json.dumps(result, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(payload.encode()) > _MAX_RESULT_BYTES:
        raise ValueError("ingestion command result is too large")
    _safe_result_from_db(result)
    return payload


def _safe_result_from_db(value: object) -> SafeCommandResult | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RuntimeError("ingestion command result is not an object")
    result: SafeCommandResult = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RuntimeError("ingestion command result key is invalid")
        if item is not None and not isinstance(item, str | int | float | bool):
            raise RuntimeError("ingestion command result value is not scalar")
        result[key] = item
    return result


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise RuntimeError("ingestion projection count is invalid")
    return value


def _required_int(value: object) -> int:
    result = _optional_int(value)
    if result is None:
        raise RuntimeError("ingestion projection count is missing")
    return result


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise RuntimeError("ingestion projection timestamp is invalid")
    return value


def _optional_stage_datetime(value: object) -> datetime | None:
    """Decode a timestamp embedded in PostgreSQL JSONB stage evidence.

    Top-level ``timestamptz`` columns are returned by psycopg as ``datetime`` objects, while the
    same values nested by ``jsonb_build_object`` are ISO-8601 strings.  Keep that representation
    exception local to the stage projection and reject unbounded, malformed, or timezone-naive
    values rather than broadening the normal row timestamp decoder.
    """
    if value is None or isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and len(value) <= _MAX_STAGE_TIMESTAMP_LENGTH:
        try:
            result = datetime.fromisoformat(value)
        except ValueError as error:
            raise RuntimeError("ingestion run stage timestamp is invalid") from error
    else:
        raise RuntimeError("ingestion run stage timestamp is invalid")
    if result is not None and result.tzinfo is None:
        raise RuntimeError("ingestion run stage timestamp is timezone-naive")
    return result


def _required_datetime(value: object) -> datetime:
    result = _optional_datetime(value)
    if result is None:
        raise RuntimeError("ingestion projection timestamp is missing")
    return result


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        # PostgreSQL numeric values may arrive as Decimal; conversion remains bounded by the SQL
        # projection and is safe as long as the result is finite.
        try:
            converted = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError) as error:
            raise RuntimeError("ingestion projection rate is invalid") from error
        if not math.isfinite(converted):
            raise RuntimeError("ingestion projection rate is invalid")
        return converted
    return float(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)
