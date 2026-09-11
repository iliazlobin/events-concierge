"""PII-free operational projections for the catalog-ingestion admin surface.

The admin plane deliberately exposes only reviewed public-source configuration, aggregate catalog
counts, normalized failure codes, and opaque command identities. Raw provider responses, lease
tokens, catalog payloads, and tenant data never cross this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from .events import EventEntityProfile


class IngestionCommandAction(StrEnum):
    """The only two network-capable commands admitted by the local admin plane."""

    REFRESH_SOURCE = "refresh_source"
    REFRESH_DUE = "refresh_due"


class IngestionCommandStatus(StrEnum):
    """Durable lifecycle of one admin command."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class IngestionReviewStatus(StrEnum):
    """Owner-review posture of one public source."""

    REVIEWED = "reviewed"
    UNREVIEWED = "unreviewed"
    EXPIRED = "expired"


class IngestionEffectiveStatus(StrEnum):
    """Closed, operator-facing explanation of whether a source can currently run."""

    ACTIVE = "active"
    DUE = "due"
    RUNNING = "running"
    DISABLED = "disabled"
    UNREVIEWED = "unreviewed"
    REVIEW_EXPIRED = "review_expired"
    POLICY_BLOCKED = "policy_blocked"
    RETIRED = "retired"


class IngestionCatalogQualityIssue(StrEnum):
    """Closed set of missing normalized fields surfaced to local operators."""

    DESCRIPTION = "missing_description"
    END_TIME = "missing_end_time"
    VENUE = "missing_venue"
    CITY = "missing_city"
    GEO = "missing_geo"
    REGISTRATION_URL = "missing_registration_url"


type JsonScalar = str | int | float | bool | None
type SafeCommandResult = dict[str, JsonScalar]


@dataclass(frozen=True, slots=True)
class IngestionPolicyStatus:
    """Current fleet-wide public-catalog browser-discovery policy."""

    allowed: bool
    reason: str
    code: str


@dataclass(frozen=True, slots=True)
class IngestionOverviewSummary:
    """Bounded aggregate counts rendered by the ingestion overview."""

    sources: int
    active_sources: int
    due_sources: int
    running_runs: int
    failed_runs_24h: int
    catalog_events: int
    pending_commands: int
    fixture_sources: int


@dataclass(frozen=True, slots=True)
class IngestionOverview:
    """One database-clock snapshot of the ingestion control plane."""

    generated_at: datetime
    policy: IngestionPolicyStatus
    summary: IngestionOverviewSummary
    latest_success_at: datetime | None


@dataclass(frozen=True, slots=True)
class IngestionFleetSummary:
    """One window-scoped rollup of the whole fleet, computed by the database.

    ``retrying_runs`` counts slots claimed more than once. A healthy slot is claimed exactly
    once, so this is the only field that distinguishes "one run failed" from "one run has been
    re-claimed five thousand times" -- the distinction the run-status counts cannot express.
    """

    generated_at: datetime
    window_start: datetime
    window_hours: int
    runs: int
    succeeded: int
    failed: int
    running: int
    paused: int
    sources_run: int
    sources_failed: int
    attempts: int
    max_attempts: int
    retrying_runs: int
    candidates: int
    canonicals: int
    zero_yield_runs: int
    wall_ms: int
    duration_p50_ms: int | None
    duration_p95_ms: int | None
    duration_p99_ms: int | None


@dataclass(frozen=True, slots=True)
class IngestionFleetShapeEntry:
    """One adapter mode's share of the fleet against its share of the catalog.

    These two distributions are not the same shape, and the difference is the most consequential
    fact about how the fleet is built: a mode can be most of the source roster and almost none of
    what people can see, while costing the same scheduler slot time to poll.
    """

    mode: str
    sources: int
    scheduled: int
    paused: int
    retired: int
    upcoming_events: int
    events_per_source: float | None
    pct_of_sources: float | None
    pct_of_events: float | None


@dataclass(frozen=True, slots=True)
class IngestionThroughputBucket:
    """One gap-filled interval of pipeline volume.

    A bucket with no runs is a fact about the fleet -- the scheduler was not dispatching -- so it
    is returned with zeros rather than omitted from the series.
    """

    bucket_start: datetime
    runs: int
    succeeded: int
    failed: int
    deferred: int
    collected: int
    published: int
    yield_pct: float | None
    median_duration_ms: int | None


@dataclass(frozen=True, slots=True)
class CatalogConcentrationEntry:
    """One source's share of served upcoming events, with a running cumulative."""

    rank: int
    source_key: str
    display_name: str
    mode: str
    publisher: str
    upcoming_events: int
    pct: float | None
    cumulative_pct: float | None
    total_events: int
    total_sources: int


@dataclass(frozen=True, slots=True)
class IngestionSourceRegistrationBucket:
    """One daily registration interval and its cumulative retained source count."""

    bucket_start: datetime
    bucket_end: datetime
    registered_sources: int
    added_sources: int


@dataclass(frozen=True, slots=True)
class IngestionSourceRegistrationHistory:
    """Registration dates for retained sources, including paused and retired rows."""

    generated_at: datetime
    window_start: datetime
    window_days: int
    bucket_hours: int
    baseline_sources: int
    total_sources: int
    added_sources: int
    items: tuple[IngestionSourceRegistrationBucket, ...]
    include_fixtures: bool
    history_scope: str = "retained_registry"


@dataclass(frozen=True, slots=True)
class IngestionSourceHealth:
    """One registry row graded by four independently inspectable components.

    ``health`` is the most elevated component, never a composite score, so a chip can always be
    explained by naming the component that produced it. Paused and retired sources are graded and
    returned rather than skipped: the write path never deletes, so a disabled source keeps serving
    its last known events and must stay visible.
    """

    source_key: str
    display_name: str
    publisher: str
    mode: str
    region: str
    enabled: bool
    retired_at: datetime | None
    refresh_interval_minutes: int
    page_limit: int
    health: str
    run_state: str
    freshness_state: str
    retry_state: str
    yield_state: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_catalog_change_at: datetime | None
    latest_run_status: str | None
    latest_run_error: str | None
    latest_attempt_count: int | None
    upcoming_events: int
    hours_since_success: float | None


@dataclass(frozen=True, slots=True)
class IngestionStageSummaryEntry:
    """One declared pipeline stage and the evidence that does or does not exist for it.

    ``evidence_status`` is load-bearing: ``not_separately_instrumented`` means the stage is folded
    into the boundary named by ``folded_into`` and can never produce a duration, which is a
    different fact from a stage that ran and measured zero.
    """

    stage: str
    stage_position: int
    evidence_status: str
    folded_into: str | None
    runs_with_evidence: int
    observations: int
    total_ms: int
    avg_ms: int | None
    p95_ms: int | None
    failed_count: int
    pct_of_wall: float | None


@dataclass(frozen=True, slots=True)
class CatalogFreshnessBucket:
    """Upcoming events being served, graded by the age of their source's last successful fetch."""

    bucket: str
    bucket_position: int
    sources: int
    events: int
    pct: float | None


@dataclass(frozen=True, slots=True)
class IngestionRunTimelineEntry:
    """One typed timestamp in a bounded run timeline.

    ``evidence_recorded`` timestamps identify when an aggregate observation was persisted; they do
    not claim to be the start time of a stage or an individual retry log entry. ``run_projection``
    also covers lease-expiry failures synthesized by the safe admin read model. Free-form messages
    are deliberately absent so raw provider data and worker exception text cannot cross this
    projection.
    """

    observed_at: datetime
    event_code: str
    timestamp_basis: str
    stage: str | None = None
    outcome_code: str | None = None
    duration_ms: int | None = None
    observation_count: int | None = None


@dataclass(frozen=True, slots=True)
class IngestionRunTimeline:
    """Lifecycle transitions plus aggregate observations; never a complete worker log."""

    evidence_scope: str = "lifecycle_only"
    complete: bool = False
    entries: tuple[IngestionRunTimelineEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class IngestionRunStatus:
    """Safe refresh-run projection; ``error`` is normalized, never raw text.

    ``release_revision`` and ``image_digest`` identify the latest worker claim attempt, not
    necessarily the process that performed provider I/O. ``provenance_status`` makes that
    distinction explicit. Paged progress is the strongest source-revision evidence; claim and
    accepted-command snapshots are bounded fallbacks. ``is_latest_for_source`` uses the same
    descending run ordering as the source projection. ``resolved_by_newer_success`` applies only
    to a failed run with a strictly newer normalized success in the same fixture scope.
    """

    source_key: str
    display_name: str
    run_key: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    candidate_count: int | None
    canonical_count: int | None
    error: str | None
    attempt_count: int
    duration_ms: int | None = None
    source_revision: int | None = None
    release_revision: str | None = None
    image_digest: str | None = None
    provenance_status: str = "legacy_unavailable"
    trigger: str = "cadence_or_manual"
    is_latest_for_source: bool = False
    resolved_by_newer_success: bool = False
    source_configuration: IngestionRunSourceConfiguration | None = None
    execution_configuration: IngestionRunSourceConfiguration | None = None
    collection_window: IngestionRunCollectionWindow | None = None
    command: IngestionRunCommandLink | None = None
    execution: IngestionRunExecutionDescriptor | None = None
    resources: IngestionRunResourceEvidence | None = None
    stage_trace: tuple[IngestionRunStageEvidence, ...] = ()
    timeline: IngestionRunTimeline = field(default_factory=IngestionRunTimeline)


@dataclass(frozen=True, slots=True)
class IngestionRunSourceConfiguration:
    """Reviewed source facts that governed the run, when the row is not a fixture."""

    mode: str
    reviewed_at: datetime | None
    review_expires_at: datetime | None
    refresh_interval_minutes: int
    min_interval_ms: int
    page_limit: int
    collection_horizon_days: int | None = None
    source_revision: int | None = None


@dataclass(frozen=True, slots=True)
class IngestionRunCollectionWindow:
    """Frozen bounds of the actual run, not today's registry or a reconstructed legacy window."""

    window_source_revision: int
    horizon_days: int
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionRunCommandLink:
    """Exact durable admin command linked by its deterministic ``admin:<uuid>`` run key."""

    command_id: UUID
    action: str
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class IngestionRunExecutionDescriptor:
    """Reviewed code ownership for a source path, not a runtime stack trace.

    All paths are repository-relative and all symbols are fixed composition metadata.  This gives
    an operator useful places to inspect without exposing host paths, command lines, environment
    values, credentials, or provider payloads.
    """

    execution_path: str
    worker_service: str
    task_queue: str | None
    adapter_id: str
    adapter_module: str
    adapter_symbol: str
    orchestration_module: str
    orchestration_symbol: str
    worker_module: str
    worker_symbol: str


@dataclass(frozen=True, slots=True)
class IngestionRunResourceEvidence:
    """Bounded resource observations with an explicit attribution scope and quality."""

    execution_count: int
    wall_time_ms: int
    process_cpu_time_ms: int | None
    cpu_utilization_percent: float | None
    rss_before_bytes: int | None
    rss_after_bytes: int | None
    boundary_observed_peak_rss_bytes: int | None
    process_lifetime_peak_rss_bytes: int | None
    measurement_source: str
    measurement_scope: str
    measurement_quality: str
    last_outcome_code: str
    first_observed_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionRunStageEvidence:
    """One item in the fixed five-stage, payload-free execution trace."""

    stage: str
    label: str
    evidence_status: str
    observation_count: int
    duration_ms: int | None
    last_outcome_code: str | None
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    note_code: str | None


@dataclass(frozen=True, slots=True)
class IngestionSourceStatus:
    """Reviewed-source status plus its latest durable result and current catalog yield."""

    source_key: str
    display_name: str
    publisher: str
    mode: str
    region: str
    seed_url: str
    enabled: bool
    source_revision: int
    review_status: IngestionReviewStatus
    effective_status: IngestionEffectiveStatus
    policy_blocked: bool
    due: bool
    last_succeeded_at: datetime | None
    next_due_at: datetime | None
    event_count: int
    latest_run: IngestionRunStatus | None
    retired_at: datetime | None = None
    retired_reason: str | None = None
    superseded_by_source_key: str | None = None
    # Retained operator Catalog inventory; distinct from legacy discovery event_count.
    total_event_count: int | None = None
    upcoming_event_count: int | None = None


@dataclass(frozen=True, slots=True)
class IngestionSourcePage:
    """Offset page used by the small operator registry."""

    items: tuple[IngestionSourceStatus, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class IngestionFilterValue:
    """One exact, counted source-registry facet value."""

    value: str
    count: int


@dataclass(frozen=True, slots=True)
class IngestionFilterMetadata:
    """Closed filter vocabulary plus fixture-aware source-registry facets."""

    modes: tuple[IngestionFilterValue, ...]
    publishers: tuple[IngestionFilterValue, ...]
    regions: tuple[IngestionFilterValue, ...]


@dataclass(frozen=True, slots=True)
class IngestionBuildIdentity:
    """Bounded deployment identity; a missing digest is represented honestly."""

    release_revision: str
    image_digest: str | None


@dataclass(frozen=True, slots=True)
class IngestionSourceConfiguration:
    """Reviewed public source configuration safe for the local operator surface."""

    source_key: str
    display_name: str
    publisher: str
    mode: str
    region: str
    seed_url: str
    seed_host: str
    enabled: bool
    handoff_only: bool
    approved_origins: tuple[str, ...]
    reviewed_at: datetime | None
    review_expires_at: datetime | None
    refresh_interval_minutes: int
    min_interval_ms: int
    page_limit: int
    source_revision: int
    review_status: IngestionReviewStatus
    effective_status: IngestionEffectiveStatus
    policy_blocked: bool
    due: bool
    last_succeeded_at: datetime | None
    next_due_at: datetime | None
    event_count: int
    latest_run: IngestionRunStatus | None
    retired_at: datetime | None = None
    retired_reason: str | None = None
    superseded_by_source_key: str | None = None
    collection_horizon_days: int = 90
    total_event_count: int | None = None
    upcoming_event_count: int | None = None


@dataclass(frozen=True, slots=True)
class IngestionHistoryWindow:
    """Explicit database-clock bounds used by a source analysis."""

    hours: int
    bucket_hours: int
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionHistorySummary:
    """Aggregate source yield and reliability over one bounded window."""

    total_runs: int
    succeeded_runs: int
    failed_runs: int
    running_runs: int
    success_rate: float | None
    candidate_count: int
    canonical_count: int
    yield_rate: float | None
    average_duration_ms: int | None
    p95_duration_ms: int | None
    latest_success_at: datetime | None
    latest_failure_at: datetime | None


@dataclass(frozen=True, slots=True)
class IngestionHistoryBucket:
    """One bounded time-series bucket for a source analysis."""

    bucket_start: datetime
    total_runs: int
    succeeded_runs: int
    failed_runs: int
    candidate_count: int
    canonical_count: int
    average_duration_ms: int | None


@dataclass(frozen=True, slots=True)
class IngestionSourceDetail:
    """Source configuration, bounded history, and deployment identity."""

    generated_at: datetime
    source: IngestionSourceConfiguration
    window: IngestionHistoryWindow
    summary: IngestionHistorySummary
    history: tuple[IngestionHistoryBucket, ...]
    recent_runs: tuple[IngestionRunStatus, ...]
    current_build: IngestionBuildIdentity


@dataclass(frozen=True, slots=True)
class IngestionSourceConfigurationUpdate:
    """Audited result of one optimistic reviewed-source configuration change."""

    source_key: str
    source_revision: int
    reviewed_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionSourceRevisionTarget:
    """One exact optimistic-lock target for an atomic registry-state change."""

    source_key: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class IngestionSourceEnabledBulkUpdate:
    """All-or-nothing result for one bounded bulk enabled-state revision."""

    enabled: bool
    requested: int
    updated: int
    unchanged: int
    items: tuple[IngestionSourceConfigurationUpdate, ...]


@dataclass(frozen=True, slots=True)
class IngestionCatalogEvent:
    """One normalized current event with bounded, safe source provenance."""

    canonical_event_id: UUID
    title: str
    start_at: datetime
    end_at: datetime | None
    venue_name: str | None
    city: str | None
    latitude: float | None
    longitude: float | None
    description: str
    description_length: int
    price_status: str
    price_min_cents: int | None
    price_max_cents: int | None
    price_currency: str | None
    event_status: str
    normalizer_version: int
    merge_version: int
    source_event_id: str
    registration_url: str | None
    last_seen_at: datetime
    refresh_run_key: str
    quality_issues: tuple[IngestionCatalogQualityIssue, ...]
    organizer_name: str | None = None
    host_names: tuple[str, ...] = ()
    speaker_names: tuple[str, ...] = ()
    partner_names: tuple[str, ...] = ()
    entity_profiles: tuple[EventEntityProfile, ...] = ()
    attendance_count: int | None = None
    registration_status: str = "unknown"


@dataclass(frozen=True, slots=True)
class IngestionCatalogEventPage:
    """Keyset page of current source output or an exact run's retained attribution."""

    items: tuple[IngestionCatalogEvent, ...]
    source_total: int
    limit: int
    has_more: bool
    next_start_at: datetime | None
    next_canonical_event_id: UUID | None
    query: str | None
    run_key: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestionCatalogRecord(IngestionCatalogEvent):
    """One retained canonical record with its selected matching source observation."""

    source_key: str
    source_display_name: str


@dataclass(frozen=True, slots=True)
class IngestionCatalogSourceCount:
    """Distinct matching canonical records for one source; counts can overlap."""

    source_key: str
    source_display_name: str
    events: int


@dataclass(frozen=True, slots=True)
class IngestionCatalogRecordPage:
    """Exact filtered inventory count and a bounded, deduplicated keyset page."""

    generated_at: datetime
    items: tuple[IngestionCatalogRecord, ...]
    total: int
    limit: int
    has_more: bool
    next_start_at: datetime | None
    next_canonical_event_id: UUID | None
    query: str | None
    source_key: str | None
    run_key: str | None
    date_scope: str
    price_status: str
    upcoming_total: int
    source_counts: tuple[IngestionCatalogSourceCount, ...]
    source_count: int
    source_counts_truncated: bool


@dataclass(frozen=True, slots=True)
class IngestionRunPage:
    """Offset page of normalized refresh-run history."""

    items: tuple[IngestionRunStatus, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class IngestionCommand:
    """Safe durable command projection returned to the API."""

    command_id: UUID
    action: IngestionCommandAction
    source_key: str | None
    status: IngestionCommandStatus
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: SafeCommandResult | None
    error_code: str | None
    source_revision: int | None = None
    # The command fields retain the API build that accepted this immutable request. Run
    # projections use the separate executor fields captured by the claiming worker.
    release_revision: str | None = None
    image_digest: str | None = None
    # These identify the worker claim attempt. A claim can queue durable work or observe prior
    # success, so it is not asserted as causal fetch-execution proof.
    executor_source_revision: int | None = None
    executor_release_revision: str | None = None
    executor_image_digest: str | None = None
    requested_by: str | None = None
    attempt_count: int = 0
    available_at: datetime | None = None
    lease_expires_at: datetime | None = None
    worker_state: str | None = None


@dataclass(frozen=True, slots=True)
class IngestionCommandRun:
    """One authoritative cadence child associated with a fleet command.

    A missing refresh row is represented as ``pending``: the association is committed before
    provider work begins, so operators can see the complete bounded plan without timestamp
    correlation or a command-specific cadence run key.
    """

    position: int
    source_key: str
    display_name: str
    run_key: str
    status: str
    phase: str
    linked_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    candidate_count: int | None
    canonical_count: int | None
    error_code: str | None
    attempt_count: int | None
    duration_ms: int | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionCommandRunTarget:
    """Planned source/slot identity linked before a fleet refresh starts."""

    position: int
    source_key: str
    run_key: str


@dataclass(frozen=True, slots=True)
class IngestionCommandProgress:
    """Bounded live summary derived only from authoritative child-run state."""

    total: int
    pending: int
    running: int
    succeeded: int
    failed: int
    paused: int
    completed: int
    active_source_key: str | None
    active_display_name: str | None
    active_phase: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionCommandDetail:
    """Operator-safe command state plus exact child runs and derived progress."""

    generated_at: datetime
    command: IngestionCommand
    progress: IngestionCommandProgress
    runs: tuple[IngestionCommandRun, ...]


@dataclass(frozen=True, slots=True)
class IngestionCommandLease:
    """Opaque authority for one command execution attempt."""

    command_id: UUID
    action: IngestionCommandAction
    source_key: str | None
    attempt_count: int
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class IngestionProcessReport:
    """Aggregate outcome of one bounded command-worker pass."""

    claimed: int = 0
    completed: int = 0
    deferred: int = 0
    failed: int = 0
    lost_leases: int = 0
