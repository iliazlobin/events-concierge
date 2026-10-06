"""Safe operational projections and durable ingestion commands.

The consumer app installs these routes only in explicit local/mock mode behind loopback checks.
The separate hosted operator app resolves signed edge identity and capabilities. Neither edge
invokes a catalog runner during an HTTP request.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ..domain.ingestion_admin import IngestionCommandAction, IngestionSourceRevisionTarget
from ..domain.ingestion_run_query import run_query_options, validate_run_key
from ..ports.ingestion_admin import (
    IngestionAdminError,
    IngestionCommandConflictError,
    IngestionCommandRejectedError,
    IngestionCommandUnavailableError,
    IngestionSourceConfigurationConflictError,
    IngestionSourceConfigurationUnavailableError,
    IngestionSourceNotFoundError,
)
from .operator_auth import (
    OperatorPrincipal,
    authorize_operator,
    verify_operator_mutation_origin,
)

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SOURCE_KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{1,79}$"
_SAFE_CODE_PATTERN = r"^[a-z0-9][a-z0-9._:-]{0,127}$"
_MAX_OFFSET = 100_000
_MAX_HISTORY_BUCKETS = 120
_MAX_SUMMARY_WINDOW_HOURS = 2_160
_DEFAULT_SUMMARY_WINDOW_HOURS = 168
_LOCAL_OPERATOR = "local-admin"
_NO_STORE_HEADERS = {"Cache-Control": "no-store, max-age=0"}
_MIN_VISIBLE_CODEPOINT = 0x21
_DELETE_CODEPOINT = 0x7F
_PUBLIC_ERROR_CODES = frozenset(
    {
        "command_conflict",
        "invalid_command",
        "policy_blocked",
        "source_revision_conflict",
        "source_unavailable",
    }
)


class _IngestionAdminService(Protocol):
    """Duck-typed application service consumed by the local HTTP edge."""

    async def get_overview(self) -> object: ...

    async def fleet_summary(
        self,
        *,
        window_hours: int,
        include_fixtures: bool,
    ) -> object: ...

    async def stage_summary(
        self,
        *,
        window_hours: int,
        include_fixtures: bool,
    ) -> object: ...

    async def catalog_freshness(self) -> object: ...

    async def source_health(self, *, include_fixtures: bool) -> object: ...

    async def source_registration_history(
        self, *, window_days: int, include_fixtures: bool
    ) -> object: ...

    async def fleet_shape(self) -> object: ...

    async def throughput(self, *, window_hours: int, bucket_hours: int) -> object: ...

    async def catalog_concentration(self, *, limit: int) -> object: ...

    async def list_sources(
        self,
        *,
        query: str | None,
        state: str,
        mode: str | None,
        publisher: str | None,
        region: str | None,
        include_fixtures: bool,
        sort_by: str,
        sort_direction: str,
        limit: int,
        offset: int,
    ) -> object: ...

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
        query: str | None = None,
        sort_by: str = "started",
        sort_direction: str = "desc",
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        stage: str | None = None,
        stage_outcome: str | None = None,
        offset: int,
    ) -> object: ...

    async def lookup_run(
        self, source_key: str, run_key: str, *, include_fixtures: bool = False
    ) -> object | None: ...

    async def list_commands(self, limit: int) -> object: ...

    async def get_command_detail(self, command_id: UUID) -> object | None: ...

    async def get_filter_metadata(
        self,
        *,
        query: str | None,
        state: str,
        mode: str | None,
        publisher: str | None,
        region: str | None,
        include_fixtures: bool,
    ) -> object: ...

    async def get_source_detail(
        self,
        source_key: str,
        *,
        window_hours: int,
        bucket_hours: int,
        include_fixtures: bool,
    ) -> object | None: ...

    async def list_catalog_records(
        self,
        *,
        source_key: str | None,
        run_key: str | None,
        query: str | None,
        date_scope: str,
        price_status: str,
        after_start_at: datetime | None,
        after_canonical_event_id: UUID | None,
        limit: int,
    ) -> object: ...

    async def list_source_events(
        self,
        source_key: str,
        *,
        query: str | None,
        after_start_at: datetime | None,
        after_canonical_event_id: UUID | None,
        limit: int,
        run_key: str | None = None,
    ) -> object: ...

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
        collection_horizon_days: int | None = None,
    ) -> object: ...

    async def set_sources_enabled(
        self,
        targets: tuple[IngestionSourceRevisionTarget, ...],
        *,
        enabled: bool,
        requested_by: str,
    ) -> object: ...

    async def enqueue_command(
        self,
        command_id: UUID,
        action: IngestionCommandAction | str,
        source_key: str | None,
        requested_by: str = _LOCAL_OPERATOR,
    ) -> object: ...


class _FromAttributesModel(BaseModel):
    """Base projection that ignores capabilities absent from the public admin contract."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class IngestionPolicyOut(_FromAttributesModel):
    allowed: bool
    reason: str = Field(min_length=1, max_length=500)
    code: str = Field(min_length=1, max_length=128, pattern=_SAFE_CODE_PATTERN)


class IngestionOverviewSummaryOut(_FromAttributesModel):
    sources: int = Field(ge=0)
    active_sources: int = Field(ge=0)
    due_sources: int = Field(ge=0)
    running_runs: int = Field(ge=0)
    failed_runs_24h: int = Field(ge=0)
    catalog_events: int = Field(ge=0)
    pending_commands: int = Field(ge=0)
    fixture_sources: int = Field(ge=0)


class IngestionOverviewOut(_FromAttributesModel):
    generated_at: datetime
    policy: IngestionPolicyOut
    summary: IngestionOverviewSummaryOut
    latest_success_at: datetime | None


class IngestionFleetSummaryOut(_FromAttributesModel):
    """One database-computed fleet rollup, replacing the client's ledger reduction."""

    generated_at: datetime
    window_start: datetime
    window_hours: int = Field(ge=1, le=_MAX_SUMMARY_WINDOW_HOURS)
    runs: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    running: int = Field(ge=0)
    paused: int = Field(ge=0)
    sources_run: int = Field(ge=0)
    sources_failed: int = Field(ge=0)
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=0)
    retrying_runs: int = Field(ge=0)
    candidates: int = Field(ge=0)
    canonicals: int = Field(ge=0)
    zero_yield_runs: int = Field(ge=0)
    wall_ms: int = Field(ge=0)
    duration_p50_ms: int | None = Field(default=None, ge=0)
    duration_p95_ms: int | None = Field(default=None, ge=0)
    duration_p99_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def counts_are_bounded_by_runs(self) -> IngestionFleetSummaryOut:
        """Refuse to publish a rollup whose parts exceed its whole."""
        if self.succeeded + self.failed + self.running + self.paused > self.runs:
            raise ValueError("ingestion fleet summary status counts exceed its run total")
        if self.retrying_runs > self.runs or self.zero_yield_runs > self.runs:
            raise ValueError("ingestion fleet summary run subsets exceed its run total")
        return self


class IngestionSourceRegistrationBucketOut(_FromAttributesModel):
    bucket_start: AwareDatetime
    bucket_end: AwareDatetime
    registered_sources: int = Field(ge=0)
    added_sources: int = Field(ge=0)


class IngestionSourceRegistrationHistoryOut(_FromAttributesModel):
    generated_at: AwareDatetime
    window_start: AwareDatetime
    window_days: Literal[7, 30, 90]
    bucket_hours: Literal[24]
    baseline_sources: int = Field(ge=0)
    total_sources: int = Field(ge=0)
    added_sources: int = Field(ge=0)
    items: list[IngestionSourceRegistrationBucketOut] = Field(min_length=7, max_length=90)
    include_fixtures: bool
    history_scope: Literal["retained_registry"]

    @model_validator(mode="after")
    def counts_reconcile_with_interval(self) -> IngestionSourceRegistrationHistoryOut:
        if len(self.items) != self.window_days or (
            self.generated_at - self.window_start != timedelta(days=self.window_days)
        ):
            raise ValueError("source registration history interval is inconsistent")
        count = self.baseline_sources
        start = self.window_start
        for bucket in self.items:
            count += bucket.added_sources
            if (
                bucket.bucket_start != start
                or bucket.bucket_end - start != timedelta(hours=24)
                or bucket.registered_sources != count
            ):
                raise ValueError("source registration history buckets are inconsistent")
            start = bucket.bucket_end
        if (
            start != self.generated_at
            or count != self.total_sources
            or self.total_sources - self.baseline_sources != self.added_sources
        ):
            raise ValueError("source registration history totals are inconsistent")
        return self


class IngestionFleetShapeEntryOut(_FromAttributesModel):
    """One adapter mode's share of sources against its share of the served catalog."""

    mode: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    sources: int = Field(ge=0)
    scheduled: int = Field(ge=0)
    paused: int = Field(ge=0)
    retired: int = Field(ge=0)
    upcoming_events: int = Field(ge=0)
    events_per_source: float | None = Field(default=None, ge=0.0)
    pct_of_sources: float | None = Field(default=None, ge=0.0, le=100.0)
    pct_of_events: float | None = Field(default=None, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def lifecycle_partitions_the_mode(self) -> IngestionFleetShapeEntryOut:
        """Every source in a mode is scheduled, paused or retired -- never double counted."""
        if self.scheduled + self.paused + self.retired != self.sources:
            raise ValueError("ingestion fleet shape lifecycle counts do not partition its sources")
        return self


class IngestionFleetShapeOut(_FromAttributesModel):
    generated_at: datetime
    total_sources: int = Field(ge=0)
    total_events: int = Field(ge=0)
    modes: tuple[IngestionFleetShapeEntryOut, ...]


class IngestionThroughputBucketOut(_FromAttributesModel):
    bucket_start: datetime
    runs: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    deferred: int = Field(ge=0)
    collected: int = Field(ge=0)
    published: int = Field(ge=0)
    yield_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    median_duration_ms: int | None = Field(default=None, ge=0)


class IngestionThroughputOut(_FromAttributesModel):
    generated_at: datetime
    window_hours: int = Field(ge=1, le=_MAX_SUMMARY_WINDOW_HOURS)
    bucket_hours: int = Field(ge=1)
    buckets: tuple[IngestionThroughputBucketOut, ...]


class CatalogConcentrationEntryOut(_FromAttributesModel):
    rank: int = Field(ge=1)
    source_key: str = Field(min_length=1, max_length=80, pattern=_SOURCE_KEY_PATTERN)
    display_name: str = Field(min_length=1, max_length=300)
    mode: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    publisher: str = Field(min_length=1, max_length=300)
    upcoming_events: int = Field(ge=0)
    pct: float | None = Field(default=None, ge=0.0, le=100.0)
    cumulative_pct: float | None = Field(default=None, ge=0.0, le=100.0)


class CatalogConcentrationOut(_FromAttributesModel):
    generated_at: datetime
    total_events: int = Field(ge=0)
    total_sources: int = Field(ge=0)
    shown: int = Field(ge=0)
    sources: tuple[CatalogConcentrationEntryOut, ...]


class IngestionSourceHealthOut(_FromAttributesModel):
    """One graded registry row. Paused and retired sources are present, never filtered away."""

    source_key: str = Field(min_length=1, max_length=80, pattern=_SOURCE_KEY_PATTERN)
    display_name: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=300)
    mode: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    region: str = Field(min_length=1, max_length=120)
    enabled: bool
    retired_at: datetime | None
    refresh_interval_minutes: int = Field(ge=1)
    page_limit: int = Field(ge=1)
    health: Literal["down", "never_succeeded", "late", "warn", "paused", "retired", "healthy"]
    run_state: Literal["never_run", "failed", "running", "deferred", "ok"]
    freshness_state: Literal["never", "down", "late", "warn", "ok", "not_scheduled"]
    retry_state: Literal["severe", "elevated", "ok"]
    yield_state: Literal["zero_yield", "unknown", "ok"]
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_catalog_change_at: datetime | None
    latest_run_status: str | None = Field(default=None, max_length=32)
    latest_run_error: str | None = Field(default=None, max_length=128, pattern=_SAFE_CODE_PATTERN)
    latest_attempt_count: int | None = Field(default=None, ge=0)
    upcoming_events: int = Field(ge=0)
    hours_since_success: float | None = Field(default=None, ge=0.0)


class IngestionSourceHealthListOut(_FromAttributesModel):
    generated_at: datetime
    total: int = Field(ge=0)
    sources: tuple[IngestionSourceHealthOut, ...]


class IngestionStageSummaryEntryOut(_FromAttributesModel):
    """One declared pipeline stage and the grade of evidence that exists for it."""

    stage: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    stage_position: int = Field(ge=1, le=32)
    evidence_status: Literal["measured", "not_separately_instrumented", "not_observed"]
    folded_into: str | None = Field(default=None, max_length=128, pattern=_SAFE_CODE_PATTERN)
    runs_with_evidence: int = Field(ge=0)
    observations: int = Field(ge=0)
    total_ms: int = Field(ge=0)
    avg_ms: int | None = Field(default=None, ge=0)
    p95_ms: int | None = Field(default=None, ge=0)
    failed_count: int = Field(ge=0)
    pct_of_wall: float | None = Field(default=None, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def folded_stages_carry_their_boundary(self) -> IngestionStageSummaryEntryOut:
        """A stage that cannot be timed must name the boundary that absorbed it."""
        folded = self.evidence_status == "not_separately_instrumented"
        if folded and self.folded_into is None:
            raise ValueError("a folded ingestion stage must name its owning boundary")
        if not folded and self.folded_into is not None:
            raise ValueError("only a folded ingestion stage may name an owning boundary")
        if folded and self.total_ms:
            raise ValueError("a folded ingestion stage cannot report its own duration")
        return self


class IngestionStageSummaryOut(_FromAttributesModel):
    generated_at: datetime
    window_hours: int = Field(ge=1, le=_MAX_SUMMARY_WINDOW_HOURS)
    stages: tuple[IngestionStageSummaryEntryOut, ...]


class CatalogFreshnessBucketOut(_FromAttributesModel):
    bucket: Literal["fresh", "aging", "stale", "dead", "never"]
    bucket_position: int = Field(ge=1, le=16)
    sources: int = Field(ge=0)
    events: int = Field(ge=0)
    pct: float | None = Field(default=None, ge=0.0, le=100.0)


class CatalogFreshnessOut(_FromAttributesModel):
    generated_at: datetime
    total_events: int = Field(ge=0)
    buckets: tuple[CatalogFreshnessBucketOut, ...]


class IngestionRunSourceConfigurationOut(_FromAttributesModel):
    mode: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    reviewed_at: datetime | None
    review_expires_at: datetime | None
    refresh_interval_minutes: int = Field(ge=1, le=10_080)
    min_interval_ms: int = Field(ge=1, le=60_000)
    page_limit: int = Field(ge=1, le=500)
    collection_horizon_days: int | None = Field(default=None, ge=1, le=90)
    source_revision: int | None = Field(default=None, ge=1)


class IngestionRunCommandLinkOut(_FromAttributesModel):
    command_id: UUID
    action: Literal["refresh_source", "refresh_due"]
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class IngestionRunCollectionWindowOut(_FromAttributesModel):
    window_source_revision: int = Field(ge=1)
    horizon_days: int = Field(ge=1, le=90)
    start_at: datetime
    end_at: datetime


class IngestionRunExecutionDescriptorOut(_FromAttributesModel):
    execution_path: Literal["guarded_direct", "temporal_single_get", "temporal_paged"]
    worker_service: Literal[
        "ingestion-command-worker", "catalog-refresh-worker", "temporal-activity-worker"
    ]
    task_queue: str | None = Field(default=None, min_length=1, max_length=200)
    adapter_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    adapter_module: str = Field(
        min_length=1,
        max_length=300,
        pattern=r"^src/events_concierge/[a-z0-9_/]+\.py$",
    )
    adapter_symbol: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z_][A-Za-z0-9_.]+$")
    orchestration_module: str = Field(
        min_length=1,
        max_length=300,
        pattern=r"^src/events_concierge/[a-z0-9_/]+\.py$",
    )
    orchestration_symbol: str = Field(
        min_length=1, max_length=160, pattern=r"^[A-Za-z_][A-Za-z0-9_.]+$"
    )
    worker_module: str = Field(
        min_length=1,
        max_length=300,
        pattern=r"^src/events_concierge/[a-z0-9_/]+\.py$",
    )
    worker_symbol: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z_][A-Za-z0-9_.]+$")


class IngestionRunResourceEvidenceOut(_FromAttributesModel):
    execution_count: int = Field(ge=1, le=10_000)
    wall_time_ms: int = Field(ge=0)
    process_cpu_time_ms: int | None = Field(default=None, ge=0)
    cpu_utilization_percent: float | None = Field(default=None, ge=0)
    rss_before_bytes: int | None = Field(default=None, ge=0)
    rss_after_bytes: int | None = Field(default=None, ge=0)
    boundary_observed_peak_rss_bytes: int | None = Field(default=None, ge=0)
    process_lifetime_peak_rss_bytes: int | None = Field(default=None, ge=0)
    measurement_source: Literal[
        "python_monotonic",
        "python_monotonic+process_time",
        "python_monotonic+process_time+linux_procfs",
        "python_monotonic+process_time+getrusage",
        "python_monotonic+process_time+linux_procfs+getrusage",
    ]
    measurement_scope: Literal["activity_wall_clock_only", "worker_process_boundary_samples"]
    measurement_quality: Literal[
        "process_metrics_omitted_shared_worker",
        "best_effort_process_delta_sequential_worker",
    ]
    last_outcome_code: Literal[
        "succeeded",
        "queued",
        "skipped",
        "busy",
        "already_succeeded",
        "deferred",
        "progressed",
        "failed",
    ]
    first_observed_at: datetime
    last_observed_at: datetime


class IngestionRunStageEvidenceOut(_FromAttributesModel):
    stage: Literal["admission", "collect", "extract_enrich", "normalize_dedupe", "catalog_publish"]
    label: Literal[
        "Admission", "Collect", "Extract / enrich", "Normalize / dedupe", "Catalog / publish"
    ]
    evidence_status: Literal[
        "measured", "not_separately_instrumented", "not_observed", "legacy_unavailable"
    ]
    observation_count: int = Field(ge=0, le=10_000)
    duration_ms: int | None = Field(default=None, ge=0)
    last_outcome_code: (
        Literal[
            "succeeded",
            "queued",
            "skipped",
            "busy",
            "already_succeeded",
            "deferred",
            "progressed",
            "failed",
        ]
        | None
    ) = None
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    note_code: (
        Literal[
            "adapter_boundary_includes_extract_enrich",
            "included_in_collect_adapter_boundary",
            "included_in_catalog_commit_boundary",
            "commit_boundary_includes_normalize_dedupe",
        ]
        | None
    ) = None


class IngestionRunTimelineEntryOut(_FromAttributesModel):
    """One closed, payload-free lifecycle or aggregate-observation timestamp."""

    observed_at: datetime
    event_code: Literal[
        "command_requested",
        "command_started",
        "run_attempt_started",
        "stage_observed",
        "execution_observed",
        "run_status_observed",
        "command_completed",
    ]
    timestamp_basis: Literal["durable_transition", "evidence_recorded", "run_projection"]
    stage: (
        Literal["admission", "collect", "extract_enrich", "normalize_dedupe", "catalog_publish"]
        | None
    ) = None
    outcome_code: (
        Literal[
            "succeeded",
            "paused",
            "queued",
            "skipped",
            "busy",
            "already_succeeded",
            "deferred",
            "progressed",
            "failed",
        ]
        | None
    ) = None
    duration_ms: int | None = Field(default=None, ge=0)
    observation_count: int | None = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_closed_shape(self) -> IngestionRunTimelineEntryOut:
        """Prevent typed fields from being combined into a misleading pseudo-log entry."""
        if self.event_code == "stage_observed":
            valid = (
                self.timestamp_basis == "evidence_recorded"
                and self.stage is not None
                and self.outcome_code is not None
                and self.duration_ms is not None
                and self.observation_count is not None
            )
        elif self.event_code == "execution_observed":
            valid = (
                self.timestamp_basis == "evidence_recorded"
                and self.stage is None
                and self.outcome_code is not None
                and self.duration_ms is not None
                and self.observation_count is not None
            )
        elif self.event_code == "run_status_observed":
            valid = (
                self.timestamp_basis == "run_projection"
                and self.stage is None
                and self.outcome_code in {"succeeded", "failed"}
                and self.observation_count is None
            )
        else:
            valid = (
                self.timestamp_basis == "durable_transition"
                and self.stage is None
                and self.outcome_code is None
                and self.duration_ms is None
                and self.observation_count is None
            )
        if not valid:
            raise ValueError("ingestion run timeline entry shape is invalid")
        return self


class IngestionRunTimelineOut(_FromAttributesModel):
    """An explicitly incomplete chronology derived from retained aggregate evidence."""

    evidence_scope: Literal["lifecycle_only", "lifecycle_and_aggregate_observations"] = (
        "lifecycle_only"
    )
    complete: Literal[False] = False
    entries: list[IngestionRunTimelineEntryOut] = Field(default_factory=list, max_length=11)

    @model_validator(mode="after")
    def validate_evidence_scope(self) -> IngestionRunTimelineOut:
        """Require chronological, unique entries and an honest aggregate-evidence scope."""
        if any(
            left.observed_at > right.observed_at
            for left, right in zip(self.entries, self.entries[1:], strict=False)
        ):
            raise ValueError("ingestion run timeline is not chronological")
        identities = [(entry.event_code, entry.stage) for entry in self.entries]
        if len(set(identities)) != len(identities):
            raise ValueError("ingestion run timeline contains duplicate entries")
        has_aggregates = any(entry.timestamp_basis == "evidence_recorded" for entry in self.entries)
        expected_scope = (
            "lifecycle_and_aggregate_observations" if has_aggregates else "lifecycle_only"
        )
        if self.evidence_scope != expected_scope:
            raise ValueError("ingestion run timeline evidence scope is invalid")
        return self


class IngestionRunCoreOut(_FromAttributesModel):
    run_key: str = Field(min_length=1, max_length=256)
    execution_configuration: IngestionRunSourceConfigurationOut | None = None
    collection_window: IngestionRunCollectionWindowOut | None = None
    status: Literal["running", "paused", "succeeded", "failed"]
    started_at: datetime | None
    completed_at: datetime | None
    candidate_count: int | None = Field(default=None, ge=0)
    canonical_count: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=128, pattern=_SAFE_CODE_PATTERN)
    attempt_count: int = Field(ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    source_revision: int | None = Field(default=None, ge=1)
    release_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    provenance_status: Literal["claim_recorded", "source_revision_only", "legacy_unavailable"] = (
        "legacy_unavailable"
    )
    trigger: Literal["admin_source", "cadence_or_manual"] = "cadence_or_manual"
    source_configuration: IngestionRunSourceConfigurationOut | None = None
    command: IngestionRunCommandLinkOut | None = None
    execution: IngestionRunExecutionDescriptorOut | None = None
    resources: IngestionRunResourceEvidenceOut | None = None
    stage_trace: list[IngestionRunStageEvidenceOut] = Field(default_factory=list, max_length=5)
    timeline: IngestionRunTimelineOut = Field(default_factory=IngestionRunTimelineOut)


class IngestionRunOut(IngestionRunCoreOut):
    source_key: str = Field(pattern=_SOURCE_KEY_PATTERN)
    display_name: str | None = Field(default=None, min_length=1, max_length=300)
    is_latest_for_source: bool
    resolved_by_newer_success: bool


class IngestionSourceOut(_FromAttributesModel):
    source_key: str = Field(pattern=_SOURCE_KEY_PATTERN)
    display_name: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=300)
    mode: str = Field(min_length=1, max_length=80)
    region: str = Field(min_length=1, max_length=120)
    seed_url: str = Field(min_length=1, max_length=2048)
    enabled: bool
    source_revision: int = Field(ge=1)
    retired_at: datetime | None = None
    retired_reason: str | None = Field(default=None, min_length=1, max_length=160)
    superseded_by_source_key: str | None = Field(default=None, pattern=_SOURCE_KEY_PATTERN)
    review_status: Literal["reviewed", "unreviewed", "expired"]
    effective_status: Literal[
        "active",
        "due",
        "running",
        "disabled",
        "unreviewed",
        "review_expired",
        "policy_blocked",
        "retired",
    ]
    due: bool
    last_succeeded_at: datetime | None
    next_due_at: datetime | None
    event_count: int = Field(ge=0)
    latest_run: IngestionRunCoreOut | None
    total_event_count: int | None = Field(default=None, ge=0)
    upcoming_event_count: int | None = Field(default=None, ge=0)


class IngestionSourcePageOut(_FromAttributesModel):
    items: list[IngestionSourceOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0, le=_MAX_OFFSET)


class IngestionRunPageOut(_FromAttributesModel):
    items: list[IngestionRunOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0, le=_MAX_OFFSET)


class IngestionCommandOut(_FromAttributesModel):
    command_id: UUID
    action: Literal["refresh_source", "refresh_due"]
    source_key: str | None = Field(default=None, pattern=_SOURCE_KEY_PATTERN)
    status: Literal["queued", "running", "completed", "failed"]
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: dict[str, str | int | float | bool | None] | None
    error_code: str | None = Field(default=None, max_length=128, pattern=_SAFE_CODE_PATTERN)
    source_revision: int | None = Field(default=None, ge=1)
    release_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    executor_release_revision: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    executor_source_revision: int | None = Field(default=None, ge=1)
    executor_image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class IngestionCommandDetailCommandOut(IngestionCommandOut):
    """Expanded worker state reserved for the single-command detail capability."""

    requested_by: str | None = Field(default=None, min_length=1, max_length=256)
    attempt_count: int = Field(default=0, ge=0)
    available_at: datetime | None = None
    lease_expires_at: datetime | None = None
    worker_state: (
        Literal[
            "awaiting_claim",
            "retry_scheduled",
            "heartbeat_live",
            "lease_expired",
            "finished",
            "failed",
        ]
        | None
    ) = None


class IngestionCommandRunOut(_FromAttributesModel):
    position: int = Field(ge=0, lt=500)
    source_key: str = Field(pattern=_SOURCE_KEY_PATTERN)
    display_name: str = Field(min_length=1, max_length=300)
    run_key: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]*$")
    status: Literal["pending", "running", "paused", "succeeded", "failed"]
    phase: Literal["awaiting_dispatch", "collecting", "deferred", "completed", "failed"]
    linked_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    candidate_count: int | None = Field(default=None, ge=0)
    canonical_count: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=128, pattern=_SAFE_CODE_PATTERN)
    attempt_count: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    updated_at: datetime


class IngestionCommandProgressOut(_FromAttributesModel):
    total: int = Field(ge=0, le=500)
    pending: int = Field(ge=0, le=500)
    running: int = Field(ge=0, le=500)
    succeeded: int = Field(ge=0, le=500)
    failed: int = Field(ge=0, le=500)
    paused: int = Field(ge=0, le=500)
    completed: int = Field(ge=0, le=500)
    active_source_key: str | None = Field(default=None, pattern=_SOURCE_KEY_PATTERN)
    active_display_name: str | None = Field(default=None, min_length=1, max_length=300)
    active_phase: (
        Literal["awaiting_dispatch", "collecting", "deferred", "completed", "failed"] | None
    ) = None
    updated_at: datetime


class IngestionCommandDetailOut(_FromAttributesModel):
    generated_at: datetime
    command: IngestionCommandDetailCommandOut
    progress: IngestionCommandProgressOut
    runs: list[IngestionCommandRunOut] = Field(max_length=500)


class IngestionFacetValueOut(_FromAttributesModel):
    value: str = Field(min_length=1, max_length=300)
    count: int = Field(ge=0)


class IngestionFixedFilterOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)


class IngestionFilterMetadataOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modes: list[IngestionFacetValueOut]
    publishers: list[IngestionFacetValueOut]
    regions: list[IngestionFacetValueOut]
    source_states: list[IngestionFixedFilterOut]
    run_statuses: list[IngestionFixedFilterOut]
    window_hours: list[int]


class IngestionSourceConfigurationOut(_FromAttributesModel):
    source_key: str = Field(pattern=_SOURCE_KEY_PATTERN)
    display_name: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=300)
    mode: str = Field(min_length=1, max_length=80)
    region: str = Field(min_length=1, max_length=120)
    seed_url: str = Field(min_length=1, max_length=2048)
    seed_host: str = Field(min_length=1, max_length=253)
    enabled: bool
    handoff_only: bool
    approved_origins: list[str] = Field(max_length=100)
    reviewed_at: datetime | None
    review_expires_at: datetime | None
    refresh_interval_minutes: int = Field(ge=1)
    min_interval_ms: int = Field(ge=1)
    page_limit: int = Field(ge=1)
    source_revision: int = Field(ge=1)
    collection_horizon_days: int = Field(default=90, ge=1, le=90)
    retired_at: datetime | None = None
    retired_reason: str | None = Field(default=None, min_length=1, max_length=160)
    superseded_by_source_key: str | None = Field(default=None, pattern=_SOURCE_KEY_PATTERN)
    review_status: Literal["reviewed", "unreviewed", "expired"]
    effective_status: Literal[
        "active",
        "due",
        "running",
        "disabled",
        "unreviewed",
        "review_expired",
        "policy_blocked",
        "retired",
    ]
    policy_blocked: bool
    due: bool
    last_succeeded_at: datetime | None
    next_due_at: datetime | None
    event_count: int = Field(ge=0)
    latest_run: IngestionRunCoreOut | None
    total_event_count: int | None = Field(default=None, ge=0)
    upcoming_event_count: int | None = Field(default=None, ge=0)


class IngestionHistoryWindowOut(_FromAttributesModel):
    hours: int = Field(ge=1, le=2160)
    bucket_hours: int = Field(ge=1, le=168)
    starts_at: datetime
    ends_at: datetime


class IngestionHistorySummaryOut(_FromAttributesModel):
    total_runs: int = Field(ge=0)
    succeeded_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    running_runs: int = Field(ge=0)
    success_rate: float | None = Field(default=None, ge=0, le=1)
    candidate_count: int = Field(ge=0)
    canonical_count: int = Field(ge=0)
    yield_rate: float | None = Field(default=None, ge=0)
    average_duration_ms: int | None = Field(default=None, ge=0)
    p95_duration_ms: int | None = Field(default=None, ge=0)
    latest_success_at: datetime | None
    latest_failure_at: datetime | None


class IngestionHistoryBucketOut(_FromAttributesModel):
    bucket_start: datetime
    total_runs: int = Field(ge=0)
    succeeded_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    canonical_count: int = Field(ge=0)
    average_duration_ms: int | None = Field(default=None, ge=0)


class IngestionBuildIdentityOut(_FromAttributesModel):
    release_revision: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class IngestionSourceDetailOut(_FromAttributesModel):
    generated_at: datetime
    source: IngestionSourceConfigurationOut
    window: IngestionHistoryWindowOut
    summary: IngestionHistorySummaryOut
    history: list[IngestionHistoryBucketOut] = Field(max_length=120)
    recent_runs: list[IngestionRunOut] = Field(max_length=20)
    current_build: IngestionBuildIdentityOut


class IngestionCatalogEntityProfileOut(_FromAttributesModel):
    """One direct public profile retained on the merged canonical event."""

    name: str = Field(min_length=1, max_length=160)
    role: Literal["host", "organizer", "speaker", "partner"]
    kind: Literal["person", "organization"]
    profile_url: str = Field(min_length=9, max_length=2_048, pattern=r"^https://")


class IngestionCatalogEventOut(_FromAttributesModel):
    canonical_event_id: UUID
    title: str = Field(min_length=1, max_length=500)
    start_at: datetime
    end_at: datetime | None
    venue_name: str | None = Field(default=None, max_length=500)
    city: str | None = Field(default=None, max_length=300)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    description: str = Field(max_length=2_000)
    description_length: int = Field(ge=0)
    price_status: Literal["free", "paid", "unknown"]
    price_min_cents: int | None = Field(default=None, ge=1, le=100_000_000)
    price_max_cents: int | None = Field(default=None, ge=1, le=100_000_000)
    price_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    event_status: Literal["scheduled", "cancelled", "rescheduled"]
    normalizer_version: int = Field(ge=1)
    merge_version: int = Field(ge=1)
    source_event_id: str = Field(min_length=1, max_length=500)
    registration_url: str | None = Field(default=None, max_length=2_048)
    last_seen_at: datetime
    refresh_run_key: str = Field(min_length=1, max_length=256)
    quality_issues: list[
        Literal[
            "missing_description",
            "missing_end_time",
            "missing_venue",
            "missing_city",
            "missing_geo",
            "missing_registration_url",
        ]
    ] = Field(max_length=6)
    organizer_name: str | None = Field(default=None, max_length=160)
    host_names: list[str] = Field(default_factory=list, max_length=32)
    speaker_names: list[str] = Field(default_factory=list, max_length=32)
    partner_names: list[str] = Field(default_factory=list, max_length=32)
    entity_profiles: list[IngestionCatalogEntityProfileOut] = Field(
        default_factory=list,
        max_length=64,
    )
    attendance_count: int | None = Field(default=None, ge=0, le=10_000_000)
    registration_status: Literal["open", "waitlist", "sold_out", "unknown"] = "unknown"


class IngestionCatalogEventPageOut(_FromAttributesModel):
    items: list[IngestionCatalogEventOut] = Field(max_length=100)
    source_total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    has_more: bool
    next_start_at: datetime | None
    next_canonical_event_id: UUID | None
    query: str | None = Field(default=None, max_length=160)
    run_key: str | None = Field(default=None, min_length=1, max_length=256)


class IngestionCatalogRecordOut(IngestionCatalogEventOut):
    source_key: str = Field(pattern=_SOURCE_KEY_PATTERN)
    source_display_name: str = Field(min_length=1, max_length=500)


class IngestionCatalogSourceCountOut(_FromAttributesModel):
    source_key: str = Field(pattern=_SOURCE_KEY_PATTERN)
    source_display_name: str = Field(min_length=1, max_length=500)
    events: int = Field(ge=0)


class IngestionCatalogRecordPageOut(_FromAttributesModel):
    generated_at: datetime
    items: list[IngestionCatalogRecordOut] = Field(max_length=100)
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    has_more: bool
    next_start_at: datetime | None
    next_canonical_event_id: UUID | None
    query: str | None = Field(default=None, max_length=160)
    source_key: str | None = Field(default=None, pattern=_SOURCE_KEY_PATTERN)
    run_key: str | None = Field(default=None, min_length=1, max_length=256)
    date_scope: Literal["all", "upcoming", "past"]
    price_status: Literal["all", "free", "paid", "unknown"]
    upcoming_total: int = Field(ge=0)
    source_counts: list[IngestionCatalogSourceCountOut] = Field(max_length=500)
    source_count: int = Field(ge=0)
    source_counts_truncated: bool


class IngestionSourceConfigurationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    seed_url: str = Field(min_length=1, max_length=2_048)
    approved_origins: list[str] = Field(min_length=1, max_length=20)
    mode: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    enabled: bool
    handoff_only: Literal[True]
    review_expires_at: datetime | None
    refresh_interval_minutes: int = Field(ge=5, le=1_440)
    min_interval_ms: int = Field(ge=250, le=60_000)
    page_limit: int = Field(ge=1, le=500)
    collection_horizon_days: int | None = Field(default=None, ge=1, le=90, strict=True)
    review_acknowledged: Literal[True]


class IngestionSourceConfigurationUpdateOut(_FromAttributesModel):
    source_key: str = Field(pattern=_SOURCE_KEY_PATTERN)
    source_revision: int = Field(ge=1)
    reviewed_at: datetime
    updated_at: datetime


class IngestionSourceRevisionTargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(pattern=_SOURCE_KEY_PATTERN)
    expected_revision: int = Field(ge=1, le=2_147_483_647, strict=True)


class IngestionSourcesEnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[IngestionSourceRevisionTargetBody] = Field(min_length=1, max_length=100)
    enabled: bool = Field(strict=True)
    review_acknowledged: Literal[True]

    @model_validator(mode="after")
    def targets_are_unique(self) -> IngestionSourcesEnabledBody:
        """Prevent ambiguous repeated revisions for one source."""
        source_keys = [target.source_key for target in self.targets]
        if len(set(source_keys)) != len(source_keys):
            raise ValueError("source targets must be unique")
        return self


class IngestionSourcesEnabledUpdateOut(_FromAttributesModel):
    enabled: bool
    requested: int = Field(ge=1, le=100)
    updated: int = Field(ge=0, le=100)
    unchanged: int = Field(ge=0, le=100)
    items: list[IngestionSourceConfigurationUpdateOut] = Field(max_length=100)

    @model_validator(mode="after")
    def counts_are_consistent(self) -> IngestionSourcesEnabledUpdateOut:
        """Refuse internally inconsistent capability output at the HTTP boundary."""
        if self.requested != self.updated + self.unchanged or self.updated != len(self.items):
            raise ValueError("source enabled update counts are inconsistent")
        return self


class IngestionCommandListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[IngestionCommandOut]


class IngestionCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    action: IngestionCommandAction
    source_key: str | None = Field(default=None, pattern=_SOURCE_KEY_PATTERN)

    @model_validator(mode="after")
    def source_matches_action(self) -> IngestionCommandBody:
        """Require one exact source only for a source-specific refresh."""
        if self.action is IngestionCommandAction.REFRESH_SOURCE and self.source_key is None:
            raise ValueError("source_key is required for refresh_source")
        if self.action is IngestionCommandAction.REFRESH_DUE and self.source_key is not None:
            raise ValueError("source_key is not allowed for refresh_due")
        return self


def _loopback_host(request: Request) -> tuple[str, int | None] | None:
    """Return one canonical loopback Host authority, rejecting ambiguous syntax."""
    host_values = request.headers.getlist("host")
    if len(host_values) != 1:
        return None
    raw_host = host_values[0]
    if (
        not raw_host
        or any(
            ord(character) < _MIN_VISIBLE_CODEPOINT or ord(character) == _DELETE_CODEPOINT
            for character in raw_host
        )
        or "," in raw_host
    ):
        return None
    try:
        parsed = urlsplit(f"//{raw_host}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        hostname not in _LOCAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return hostname, port


def _origin_authority(value: str) -> tuple[str, str, int] | None:
    """Parse a web Origin into its canonical scheme/host/effective-port tuple."""
    if (
        not value
        or any(
            ord(character) < _MIN_VISIBLE_CODEPOINT or ord(character) == _DELETE_CODEPOINT
            for character in value
        )
        or "," in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    effective_port = port if port is not None else (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, hostname, effective_port


async def _local_ingestion_admin(request: Request) -> _IngestionAdminService:
    """Resolve the service only inside the explicitly enabled local mock boundary."""
    settings = request.app.state.settings
    if getattr(request.app.state, "operator_boundary", False):
        await authorize_operator(request)
        service = request.app.state.ingestion_admin
        if service is None:
            raise HTTPException(503, "operator service unavailable", headers=_NO_STORE_HEADERS)
        return cast("_IngestionAdminService", service)
    if not bool(getattr(settings, "admin_ingestion_enabled", False)) or not bool(
        getattr(settings, "mock_cloud", False)
    ):
        raise HTTPException(
            status_code=403,
            detail="local ingestion admin is unavailable",
            headers=_NO_STORE_HEADERS,
        )
    authority = _loopback_host(request)
    if authority is None:
        raise HTTPException(
            status_code=403,
            detail="local ingestion admin requires a loopback Host",
            headers=_NO_STORE_HEADERS,
        )
    try:
        service = request.app.state.ingestion_admin
    except AttributeError as error:
        raise HTTPException(
            status_code=503,
            detail="ingestion administration is unavailable",
            headers=_NO_STORE_HEADERS,
        ) from error
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="ingestion administration is unavailable",
            headers=_NO_STORE_HEADERS,
        )
    return cast("_IngestionAdminService", service)


LocalIngestionAdmin = Annotated[_IngestionAdminService, Depends(_local_ingestion_admin)]


async def _same_origin_if_present(request: Request) -> None:
    """Reject a present Origin unless it exactly matches this loopback request origin."""
    if getattr(request.app.state, "operator_boundary", False):
        verify_operator_mutation_origin(request)
        return
    origins = request.headers.getlist("origin")
    if not origins:
        return
    if len(origins) != 1:
        raise HTTPException(
            status_code=403,
            detail="cross-origin admin command rejected",
            headers=_NO_STORE_HEADERS,
        )
    host = _loopback_host(request)
    supplied = _origin_authority(origins[0])
    if host is None or supplied is None:
        raise HTTPException(
            status_code=403,
            detail="cross-origin admin command rejected",
            headers=_NO_STORE_HEADERS,
        )
    hostname, explicit_port = host
    scheme = request.url.scheme
    request_port = (
        explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    )
    if supplied != (scheme, hostname, request_port):
        raise HTTPException(
            status_code=403,
            detail="cross-origin admin command rejected",
            headers=_NO_STORE_HEADERS,
        )


SameOriginAdminCommand = Annotated[None, Depends(_same_origin_if_present)]


def _request_operator_actor(request: Request) -> str:
    principal = getattr(request.state, "operator_principal", None)
    if isinstance(principal, OperatorPrincipal):
        return principal.actor
    if getattr(request.app.state, "operator_boundary", False):
        raise HTTPException(401, "verified operator identity required", headers=_NO_STORE_HEADERS)
    return _LOCAL_OPERATOR


def _safe_http_error(error: Exception) -> HTTPException:
    """Translate only fixed control-plane errors; never reflect exception text."""
    code = getattr(error, "code", None)
    safe_code = code if isinstance(code, str) and code in _PUBLIC_ERROR_CODES else None
    if isinstance(error, IngestionSourceNotFoundError):
        return HTTPException(
            status_code=404,
            detail="ingestion source not found",
            headers=_NO_STORE_HEADERS,
        )
    if isinstance(
        error,
        (
            IngestionCommandConflictError,
            IngestionCommandUnavailableError,
            IngestionSourceConfigurationConflictError,
            IngestionSourceConfigurationUnavailableError,
        ),
    ):
        return HTTPException(
            status_code=409,
            detail=safe_code or "ingestion command unavailable",
            headers=_NO_STORE_HEADERS,
        )
    if isinstance(error, IngestionCommandRejectedError):
        return HTTPException(
            status_code=422,
            detail=safe_code or "invalid ingestion command",
            headers=_NO_STORE_HEADERS,
        )
    if isinstance(error, IngestionAdminError):
        return HTTPException(
            status_code=409,
            detail="ingestion command unavailable",
            headers=_NO_STORE_HEADERS,
        )
    return HTTPException(
        status_code=503,
        detail="ingestion administration is unavailable",
        headers=_NO_STORE_HEADERS,
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"


def install_ingestion_admin_routes(app: FastAPI) -> None:  # noqa: PLR0915
    """Install the local ingestion API only when the startup setting explicitly enables it."""
    settings = app.state.settings
    if not bool(getattr(settings, "admin_ingestion_enabled", False)) and not getattr(
        app.state, "operator_boundary", False
    ):
        return

    @app.get(
        "/admin/v1/ingestion/overview",
        response_model=IngestionOverviewOut,
        include_in_schema=False,
    )
    async def ingestion_overview(
        response: Response,
        admin: LocalIngestionAdmin,
    ) -> IngestionOverviewOut:
        _no_store(response)
        try:
            return IngestionOverviewOut.model_validate(await admin.get_overview())
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/summary",
        response_model=IngestionFleetSummaryOut,
        include_in_schema=False,
    )
    async def ingestion_fleet_summary(
        response: Response,
        admin: LocalIngestionAdmin,
        window_hours: Annotated[
            int, Query(ge=1, le=_MAX_SUMMARY_WINDOW_HOURS)
        ] = _DEFAULT_SUMMARY_WINDOW_HOURS,
        include_fixtures: bool = False,
    ) -> IngestionFleetSummaryOut:
        _no_store(response)
        try:
            return IngestionFleetSummaryOut.model_validate(
                await admin.fleet_summary(
                    window_hours=window_hours,
                    include_fixtures=include_fixtures,
                )
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/stages",
        response_model=IngestionStageSummaryOut,
        include_in_schema=False,
    )
    async def ingestion_stage_summary(
        response: Response,
        admin: LocalIngestionAdmin,
        window_hours: Annotated[
            int, Query(ge=1, le=_MAX_SUMMARY_WINDOW_HOURS)
        ] = _DEFAULT_SUMMARY_WINDOW_HOURS,
        include_fixtures: bool = False,
    ) -> IngestionStageSummaryOut:
        _no_store(response)
        try:
            stages = await admin.stage_summary(
                window_hours=window_hours,
                include_fixtures=include_fixtures,
            )
            return IngestionStageSummaryOut.model_validate(
                {
                    "generated_at": datetime.now(UTC),
                    "window_hours": window_hours,
                    "stages": tuple(cast(Sequence[object], stages)),
                }
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/catalog-freshness",
        response_model=CatalogFreshnessOut,
        include_in_schema=False,
    )
    async def ingestion_catalog_freshness(
        response: Response,
        admin: LocalIngestionAdmin,
    ) -> CatalogFreshnessOut:
        _no_store(response)
        try:
            buckets = cast(Sequence[object], await admin.catalog_freshness())
            projected = tuple(
                CatalogFreshnessBucketOut.model_validate(bucket) for bucket in buckets
            )
            return CatalogFreshnessOut(
                generated_at=datetime.now(UTC),
                total_events=sum(bucket.events for bucket in projected),
                buckets=projected,
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/source-registration-history",
        response_model=IngestionSourceRegistrationHistoryOut,
        include_in_schema=False,
    )
    async def ingestion_source_registration_history(
        response: Response,
        admin: LocalIngestionAdmin,
        window_days: Annotated[int, Query(ge=7, le=90)] = 90,
        include_fixtures: bool = False,
    ) -> IngestionSourceRegistrationHistoryOut:
        _no_store(response)
        if window_days not in {7, 30, 90}:
            raise HTTPException(
                status_code=422,
                detail="invalid source registration history window",
                headers=_NO_STORE_HEADERS,
            )
        try:
            history = await admin.source_registration_history(
                window_days=window_days, include_fixtures=include_fixtures
            )
            return IngestionSourceRegistrationHistoryOut.model_validate(history)
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/source-health",
        response_model=IngestionSourceHealthListOut,
        include_in_schema=False,
    )
    async def ingestion_source_health(
        response: Response,
        admin: LocalIngestionAdmin,
        include_fixtures: bool = False,
    ) -> IngestionSourceHealthListOut:
        _no_store(response)
        try:
            rows = cast(
                Sequence[object],
                await admin.source_health(include_fixtures=include_fixtures),
            )
            graded = tuple(IngestionSourceHealthOut.model_validate(row) for row in rows)
            return IngestionSourceHealthListOut(
                generated_at=datetime.now(UTC),
                total=len(graded),
                sources=graded,
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/shape",
        response_model=IngestionFleetShapeOut,
        include_in_schema=False,
    )
    async def ingestion_fleet_shape(
        response: Response,
        admin: LocalIngestionAdmin,
    ) -> IngestionFleetShapeOut:
        _no_store(response)
        try:
            rows = cast(Sequence[object], await admin.fleet_shape())
            modes = tuple(IngestionFleetShapeEntryOut.model_validate(row) for row in rows)
            return IngestionFleetShapeOut(
                generated_at=datetime.now(UTC),
                total_sources=sum(mode.sources for mode in modes),
                total_events=sum(mode.upcoming_events for mode in modes),
                modes=modes,
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/throughput",
        response_model=IngestionThroughputOut,
        include_in_schema=False,
    )
    async def ingestion_throughput(
        response: Response,
        admin: LocalIngestionAdmin,
        window_hours: Annotated[
            int, Query(ge=1, le=_MAX_SUMMARY_WINDOW_HOURS)
        ] = _DEFAULT_SUMMARY_WINDOW_HOURS,
        bucket_hours: Annotated[int, Query(ge=1, le=168)] = 24,
    ) -> IngestionThroughputOut:
        _no_store(response)
        try:
            rows = cast(
                Sequence[object],
                await admin.throughput(window_hours=window_hours, bucket_hours=bucket_hours),
            )
            return IngestionThroughputOut(
                generated_at=datetime.now(UTC),
                window_hours=window_hours,
                bucket_hours=bucket_hours,
                buckets=tuple(IngestionThroughputBucketOut.model_validate(row) for row in rows),
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/concentration",
        response_model=CatalogConcentrationOut,
        include_in_schema=False,
    )
    async def catalog_concentration(
        response: Response,
        admin: LocalIngestionAdmin,
        limit: Annotated[int, Query(ge=1, le=200)] = 15,
    ) -> CatalogConcentrationOut:
        _no_store(response)
        try:
            raw = list(cast(Sequence[Any], await admin.catalog_concentration(limit=limit)))
            entries = tuple(CatalogConcentrationEntryOut.model_validate(row) for row in raw)
            # The totals describe the whole population, not the truncated page, so the share
            # column stays honest when only the top N rows are shown.
            return CatalogConcentrationOut(
                generated_at=datetime.now(UTC),
                total_events=int(getattr(raw[0], "total_events", 0)) if raw else 0,
                total_sources=int(getattr(raw[0], "total_sources", 0)) if raw else 0,
                shown=len(entries),
                sources=entries,
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/sources",
        response_model=IngestionSourcePageOut,
        include_in_schema=False,
    )
    async def ingestion_sources(
        response: Response,
        admin: LocalIngestionAdmin,
        query: Annotated[str | None, Query(max_length=200)] = None,
        state: Annotated[
            Literal["all", "active", "due", "blocked", "failed"],
            Query(),
        ] = "all",
        mode: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        publisher: Annotated[str | None, Query(min_length=1, max_length=300)] = None,
        region: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
        include_fixtures: bool = False,
        sort_by: Annotated[
            Literal[
                "source",
                "health",
                "catalog",
                "catalog_total",
                "last_success",
                "latest_run",
                "output",
            ],
            Query(),
        ] = "source",
        sort_direction: Annotated[Literal["asc", "desc"], Query()] = "asc",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=_MAX_OFFSET)] = 0,
    ) -> IngestionSourcePageOut:
        _no_store(response)
        normalized_query = query.strip() if query is not None else None
        if normalized_query == "":
            normalized_query = None
        try:
            page = await admin.list_sources(
                query=normalized_query,
                state=state,
                mode=mode,
                publisher=publisher,
                region=region,
                include_fixtures=include_fixtures,
                sort_by=sort_by,
                sort_direction=sort_direction,
                limit=limit,
                offset=offset,
            )
            return IngestionSourcePageOut.model_validate(page)
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/filters",
        response_model=IngestionFilterMetadataOut,
        include_in_schema=False,
    )
    async def ingestion_filters(
        response: Response,
        admin: LocalIngestionAdmin,
        query: Annotated[str | None, Query(max_length=200)] = None,
        state: Annotated[
            Literal["all", "active", "due", "blocked", "failed"],
            Query(),
        ] = "all",
        mode: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        publisher: Annotated[str | None, Query(min_length=1, max_length=300)] = None,
        region: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
        include_fixtures: bool = False,
    ) -> IngestionFilterMetadataOut:
        _no_store(response)
        normalized_query = query.strip() if query is not None else None
        if normalized_query == "":
            normalized_query = None
        try:
            metadata = cast(
                "Any",
                await admin.get_filter_metadata(
                    query=normalized_query,
                    state=state,
                    mode=mode,
                    publisher=publisher,
                    region=region,
                    include_fixtures=include_fixtures,
                ),
            )
            return IngestionFilterMetadataOut(
                modes=[IngestionFacetValueOut.model_validate(item) for item in metadata.modes],
                publishers=[
                    IngestionFacetValueOut.model_validate(item) for item in metadata.publishers
                ],
                regions=[IngestionFacetValueOut.model_validate(item) for item in metadata.regions],
                source_states=[
                    IngestionFixedFilterOut(value="all", label="All states"),
                    IngestionFixedFilterOut(value="active", label="Active"),
                    IngestionFixedFilterOut(value="due", label="Due"),
                    IngestionFixedFilterOut(value="blocked", label="Blocked"),
                    IngestionFixedFilterOut(value="failed", label="Failed"),
                ],
                run_statuses=[
                    IngestionFixedFilterOut(value="running", label="Running"),
                    IngestionFixedFilterOut(value="paused", label="Paused"),
                    IngestionFixedFilterOut(value="succeeded", label="Succeeded"),
                    IngestionFixedFilterOut(value="failed", label="Failed"),
                ],
                window_hours=[24, 168, 720],
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/sources/{source_key}",
        response_model=IngestionSourceDetailOut,
        include_in_schema=False,
    )
    async def ingestion_source_detail(
        source_key: Annotated[str, Path(pattern=_SOURCE_KEY_PATTERN)],
        response: Response,
        admin: LocalIngestionAdmin,
        window_hours: Annotated[int, Query(ge=1, le=2160)] = 168,
        bucket_hours: Annotated[int, Query(ge=1, le=168)] = 24,
        include_fixtures: bool = False,
    ) -> IngestionSourceDetailOut:
        _no_store(response)
        if window_hours % bucket_hours != 0 or window_hours // bucket_hours > _MAX_HISTORY_BUCKETS:
            raise HTTPException(
                status_code=422,
                detail="invalid ingestion history bucket",
                headers=_NO_STORE_HEADERS,
            )
        try:
            detail = await admin.get_source_detail(
                source_key,
                window_hours=window_hours,
                bucket_hours=bucket_hours,
                include_fixtures=include_fixtures,
            )
            if detail is None:
                raise HTTPException(
                    status_code=404,
                    detail="ingestion source not found",
                    headers=_NO_STORE_HEADERS,
                )
            return IngestionSourceDetailOut.model_validate(detail)
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.patch(
        "/admin/v1/ingestion/sources/bulk/enabled",
        response_model=IngestionSourcesEnabledUpdateOut,
        include_in_schema=False,
    )
    async def update_ingestion_sources_enabled(
        request: Request,
        body: IngestionSourcesEnabledBody,
        response: Response,
        admin: LocalIngestionAdmin,
        same_origin: SameOriginAdminCommand,
    ) -> IngestionSourcesEnabledUpdateOut:
        del same_origin
        _no_store(response)
        try:
            updated = await admin.set_sources_enabled(
                tuple(
                    IngestionSourceRevisionTarget(
                        source_key=target.source_key,
                        expected_revision=target.expected_revision,
                    )
                    for target in body.targets
                ),
                enabled=body.enabled,
                requested_by=_request_operator_actor(request),
            )
            return IngestionSourcesEnabledUpdateOut.model_validate(updated)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="invalid source enabled update",
                headers=_NO_STORE_HEADERS,
            ) from error
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.patch(
        "/admin/v1/ingestion/sources/{source_key}",
        response_model=IngestionSourceConfigurationUpdateOut,
        include_in_schema=False,
    )
    async def update_ingestion_source_configuration(
        request: Request,
        source_key: Annotated[str, Path(pattern=_SOURCE_KEY_PATTERN)],
        body: IngestionSourceConfigurationBody,
        response: Response,
        admin: LocalIngestionAdmin,
        same_origin: SameOriginAdminCommand,
    ) -> IngestionSourceConfigurationUpdateOut:
        del same_origin
        _no_store(response)
        try:
            updated = await admin.update_source_configuration(
                source_key,
                **(
                    {"collection_horizon_days": body.collection_horizon_days}
                    if body.collection_horizon_days is not None
                    else {}
                ),
                expected_revision=body.expected_revision,
                seed_url=body.seed_url,
                approved_origins=tuple(body.approved_origins),
                mode=body.mode,
                enabled=body.enabled,
                handoff_only=body.handoff_only,
                review_expires_at=body.review_expires_at,
                refresh_interval_minutes=body.refresh_interval_minutes,
                min_interval_ms=body.min_interval_ms,
                page_limit=body.page_limit,
                requested_by=_request_operator_actor(request),
            )
            return IngestionSourceConfigurationUpdateOut.model_validate(updated)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="invalid source configuration",
                headers=_NO_STORE_HEADERS,
            ) from error
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/events",
        response_model=IngestionCatalogRecordPageOut,
        include_in_schema=False,
    )
    async def ingestion_catalog_records(
        response: Response,
        admin: LocalIngestionAdmin,
        source_key: Annotated[str | None, Query(pattern=_SOURCE_KEY_PATTERN)] = None,
        run_key: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
        query: Annotated[str | None, Query(alias="q", max_length=160)] = None,
        date_scope: Annotated[Literal["all", "upcoming", "past"], Query()] = "all",
        price_status: Annotated[Literal["all", "free", "paid", "unknown"], Query()] = "all",
        after_start_at: Annotated[datetime | None, Query()] = None,
        after_canonical_event_id: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> IngestionCatalogRecordPageOut:
        _no_store(response)
        try:
            page = await admin.list_catalog_records(
                source_key=source_key,
                run_key=run_key,
                query=query,
                date_scope=date_scope,
                price_status=price_status,
                after_start_at=after_start_at,
                after_canonical_event_id=after_canonical_event_id,
                limit=limit,
            )
            return IngestionCatalogRecordPageOut.model_validate(page)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="invalid catalog query",
                headers=_NO_STORE_HEADERS,
            ) from error
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/sources/{source_key}/events",
        response_model=IngestionCatalogEventPageOut,
        include_in_schema=False,
    )
    async def ingestion_source_events(
        source_key: Annotated[str, Path(pattern=_SOURCE_KEY_PATTERN)],
        response: Response,
        admin: LocalIngestionAdmin,
        query: Annotated[str | None, Query(alias="q", max_length=160)] = None,
        after_start_at: Annotated[datetime | None, Query()] = None,
        after_canonical_event_id: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        run_key: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    ) -> IngestionCatalogEventPageOut:
        _no_store(response)
        if (after_start_at is None) != (after_canonical_event_id is None):
            raise HTTPException(
                status_code=422,
                detail="invalid source event cursor",
                headers=_NO_STORE_HEADERS,
            )
        try:
            page = await admin.list_source_events(
                source_key,
                query=query,
                after_start_at=after_start_at,
                after_canonical_event_id=after_canonical_event_id,
                limit=limit,
                **({"run_key": run_key} if run_key is not None else {}),
            )
            return IngestionCatalogEventPageOut.model_validate(page)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="invalid source event query",
                headers=_NO_STORE_HEADERS,
            ) from error
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/runs",
        response_model=IngestionRunPageOut,
        include_in_schema=False,
    )
    async def ingestion_runs(
        response: Response,
        admin: LocalIngestionAdmin,
        status_filter: Annotated[
            Literal["running", "paused", "succeeded", "failed"] | None,
            Query(alias="status"),
        ] = None,
        source_key: Annotated[
            str | None,
            Query(pattern=_SOURCE_KEY_PATTERN),
        ] = None,
        mode: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        publisher: Annotated[str | None, Query(min_length=1, max_length=300)] = None,
        region: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
        window_hours: Annotated[int | None, Query(ge=1, le=2160)] = None,
        query: Annotated[str | None, Query(max_length=160)] = None,
        sort_by: Literal[
            "source", "status", "started", "duration", "attempts", "output", "stage_duration"
        ] = "started",
        sort_direction: Literal["asc", "desc"] = "desc",
        started_after: Annotated[AwareDatetime | None, Query()] = None,
        started_before: Annotated[AwareDatetime | None, Query()] = None,
        stage: Literal[
            "admission", "collect", "extract_enrich", "normalize_dedupe", "catalog_publish"
        ]
        | None = None,
        stage_outcome: Literal["failed"] | None = None,
        include_fixtures: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=_MAX_OFFSET)] = 0,
    ) -> IngestionRunPageOut:
        _no_store(response)
        try:
            options = run_query_options(
                query=query,
                sort_by=sort_by,
                sort_direction=sort_direction,
                started_after=started_after,
                started_before=started_before,
                stage=stage,
                stage_outcome=stage_outcome,
            )
        except ValueError as error:
            raise HTTPException(
                422, "invalid ingestion run query", headers=_NO_STORE_HEADERS
            ) from error
        try:
            page = await admin.list_runs(
                **options,
                status=status_filter,
                source_key=source_key,
                mode=mode,
                publisher=publisher,
                region=region,
                window_hours=window_hours,
                include_fixtures=include_fixtures,
                limit=limit,
                offset=offset,
            )
            return IngestionRunPageOut.model_validate(page)
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/runs/lookup", response_model=IngestionRunOut, include_in_schema=False
    )
    async def ingestion_run_lookup(
        response: Response,
        admin: LocalIngestionAdmin,
        source_key: Annotated[str, Query(pattern=_SOURCE_KEY_PATTERN)],
        run_key: Annotated[str, Query(min_length=1, max_length=256)],
        include_fixtures: bool = False,
    ) -> IngestionRunOut:
        _no_store(response)
        try:
            validate_run_key(run_key)
        except ValueError as error:
            raise HTTPException(
                422, "invalid ingestion run identity", headers=_NO_STORE_HEADERS
            ) from error
        try:
            run = await admin.lookup_run(source_key, run_key, include_fixtures=include_fixtures)
            if run is None:
                raise HTTPException(404, "ingestion run not found", headers=_NO_STORE_HEADERS)
            return IngestionRunOut.model_validate(run)
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/commands",
        response_model=IngestionCommandListOut,
        include_in_schema=False,
    )
    async def ingestion_commands(
        response: Response,
        admin: LocalIngestionAdmin,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> IngestionCommandListOut:
        _no_store(response)
        try:
            commands = await admin.list_commands(limit)
            return IngestionCommandListOut(
                items=[
                    IngestionCommandOut.model_validate(command) for command in cast("Any", commands)
                ]
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.get(
        "/admin/v1/ingestion/commands/{command_id}",
        response_model=IngestionCommandDetailOut,
        include_in_schema=False,
    )
    async def ingestion_command_detail(
        command_id: Annotated[UUID, Path()],
        response: Response,
        admin: LocalIngestionAdmin,
    ) -> IngestionCommandDetailOut:
        _no_store(response)
        try:
            detail = await admin.get_command_detail(command_id)
            if detail is None:
                raise HTTPException(
                    status_code=404,
                    detail="ingestion command not found",
                    headers=_NO_STORE_HEADERS,
                )
            return IngestionCommandDetailOut.model_validate(detail)
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error

    @app.post(
        "/admin/v1/ingestion/commands",
        response_model=IngestionCommandOut,
        status_code=status.HTTP_202_ACCEPTED,
        include_in_schema=False,
    )
    async def enqueue_ingestion_command(
        request: Request,
        body: IngestionCommandBody,
        response: Response,
        admin: LocalIngestionAdmin,
        same_origin: SameOriginAdminCommand,
    ) -> IngestionCommandOut:
        del same_origin
        _no_store(response)
        try:
            command = await admin.enqueue_command(
                command_id=body.command_id,
                action=body.action,
                source_key=body.source_key,
                requested_by=_request_operator_actor(request),
            )
            return IngestionCommandOut.model_validate(command)
        except HTTPException:
            raise
        except Exception as error:
            raise _safe_http_error(error) from error
