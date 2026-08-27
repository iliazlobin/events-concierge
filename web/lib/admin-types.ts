export type AdminTab = "overview" | "pipeline" | "sources" | "runs" | "commands";
export type SourceState = "all" | "active" | "due" | "blocked" | "failed";
export type SourceStatus =
  | "active"
  | "due"
  | "running"
  | "disabled"
  | "retired"
  | "unreviewed"
  | "review_expired"
  | "policy_blocked";
export type RunStatus = "running" | "paused" | "succeeded" | "failed";
export type CommandStatus = "queued" | "running" | "completed" | "failed";
export type AdminSourceSort =
  | "source"
  | "health"
  | "catalog"
  | "last_success"
  | "latest_run"
  | "output";
export type AdminSortDirection = "asc" | "desc";

export interface AdminPolicy {
  allowed: boolean;
  reason: string;
  code: string;
}

export interface AdminRunSourceConfiguration {
  mode: string;
  reviewed_at: string | null;
  review_expires_at: string | null;
  refresh_interval_minutes: number;
  min_interval_ms: number;
  page_limit: number;
}

export interface AdminRunCommandLink {
  command_id: string;
  action: "refresh_source";
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface AdminRunExecutionDescriptor {
  execution_path: "guarded_direct" | "temporal_single_get" | "temporal_paged";
  worker_service:
    | "ingestion-command-worker"
    | "catalog-refresh-worker"
    | "temporal-activity-worker";
  task_queue: string | null;
  adapter_id: string;
  adapter_module: string;
  adapter_symbol: string;
  orchestration_module: string;
  orchestration_symbol: string;
  worker_module: string;
  worker_symbol: string;
}

export interface AdminRunResourceEvidence {
  execution_count: number;
  wall_time_ms: number;
  process_cpu_time_ms: number | null;
  cpu_utilization_percent: number | null;
  rss_before_bytes: number | null;
  rss_after_bytes: number | null;
  boundary_observed_peak_rss_bytes: number | null;
  process_lifetime_peak_rss_bytes: number | null;
  measurement_source: string;
  measurement_scope: "activity_wall_clock_only" | "worker_process_boundary_samples";
  measurement_quality:
    | "process_metrics_omitted_shared_worker"
    | "best_effort_process_delta_sequential_worker";
  last_outcome_code: string;
  first_observed_at: string;
  last_observed_at: string;
}

export interface AdminRunStageEvidence {
  stage:
    | "admission"
    | "collect"
    | "extract_enrich"
    | "normalize_dedupe"
    | "catalog_publish";
  label: string;
  evidence_status:
    | "measured"
    | "not_separately_instrumented"
    | "not_observed"
    | "legacy_unavailable";
  observation_count: number;
  duration_ms: number | null;
  last_outcome_code: string | null;
  first_observed_at: string | null;
  last_observed_at: string | null;
  note_code: string | null;
}

export interface AdminRunTimelineEntry {
  observed_at: string;
  event_code:
    | "command_requested"
    | "command_started"
    | "run_attempt_started"
    | "stage_observed"
    | "execution_observed"
    | "run_status_observed"
    | "command_completed";
  timestamp_basis: "durable_transition" | "evidence_recorded" | "run_projection";
  stage:
    | "admission"
    | "collect"
    | "extract_enrich"
    | "normalize_dedupe"
    | "catalog_publish"
    | null;
  outcome_code: string | null;
  duration_ms: number | null;
  observation_count: number | null;
}

export interface AdminRunTimeline {
  evidence_scope: "lifecycle_only" | "lifecycle_and_aggregate_observations";
  complete: false;
  entries: AdminRunTimelineEntry[];
}

export interface AdminOverview {
  generated_at: string;
  policy: AdminPolicy;
  summary: {
    sources: number;
    active_sources: number;
    due_sources: number;
    running_runs: number;
    failed_runs_24h: number;
    catalog_events: number;
    pending_commands: number;
    fixture_sources: number;
  };
  latest_success_at: string | null;
}

/**
 * Mirrors `IngestionFleetSummaryOut`. Computed by `fn_get_ingestion_admin_fleet_summary_v1`,
 * never by reducing rows in the browser.
 *
 * `retrying_runs` and `max_attempts` are the fields the old console had no equivalent of: a slot
 * claimed once is healthy, and a slot claimed thousands of times still counts as a single run.
 */
export interface AdminFleetSummary {
  generated_at: string;
  window_start: string;
  window_hours: number;
  runs: number;
  succeeded: number;
  failed: number;
  running: number;
  paused: number;
  sources_run: number;
  sources_failed: number;
  attempts: number;
  max_attempts: number;
  retrying_runs: number;
  candidates: number;
  canonicals: number;
  zero_yield_runs: number;
  wall_ms: number;
  duration_p50_ms: number | null;
  duration_p95_ms: number | null;
  duration_p99_ms: number | null;
}

/**
 * `not_separately_instrumented` means the stage is folded into the boundary named by
 * `folded_into` and can never report a duration. It must not render as a stage that measured
 * zero — that substitution is what made the pipeline view state a false fact.
 */
export type AdminStageEvidenceStatus =
  | "measured"
  | "not_separately_instrumented"
  | "not_observed";

export interface AdminStageSummaryEntry {
  stage: string;
  stage_position: number;
  evidence_status: AdminStageEvidenceStatus;
  folded_into: string | null;
  runs_with_evidence: number;
  observations: number;
  total_ms: number;
  avg_ms: number | null;
  p95_ms: number | null;
  failed_count: number;
  pct_of_wall: number | null;
}

export interface AdminStageSummary {
  generated_at: string;
  window_hours: number;
  stages: AdminStageSummaryEntry[];
}

export type AdminFreshnessBucketName =
  | "fresh"
  | "aging"
  | "stale"
  | "dead"
  | "never";

export interface AdminCatalogFreshnessBucket {
  bucket: AdminFreshnessBucketName;
  bucket_position: number;
  sources: number;
  events: number;
  pct: number | null;
}

export interface AdminCatalogFreshness {
  generated_at: string;
  total_events: number;
  buckets: AdminCatalogFreshnessBucket[];
}

export type AdminSourceHealthToken =
  | "down" | "never_succeeded" | "late" | "warn" | "paused" | "retired" | "healthy";

export interface AdminSourceHealth {
  source_key: string;
  display_name: string;
  publisher: string;
  mode: string;
  region: string;
  enabled: boolean;
  retired_at: string | null;
  refresh_interval_minutes: number;
  page_limit: number;
  health: AdminSourceHealthToken;
  run_state: "never_run" | "failed" | "running" | "deferred" | "ok";
  freshness_state: "never" | "down" | "late" | "warn" | "ok";
  retry_state: "severe" | "elevated" | "ok";
  yield_state: "zero_yield" | "unknown" | "ok";
  last_attempt_at: string | null;
  last_success_at: string | null;
  last_catalog_change_at: string | null;
  latest_run_status: string | null;
  latest_run_error: string | null;
  latest_attempt_count: number | null;
  upcoming_events: number;
  hours_since_success: number | null;
}

export interface AdminSourceHealthList {
  generated_at: string;
  total: number;
  sources: AdminSourceHealth[];
}

export interface AdminRunCore {
  run_key: string;
  status: RunStatus;
  started_at: string | null;
  completed_at: string | null;
  candidate_count: number | null;
  canonical_count: number | null;
  error: string | null;
  attempt_count: number;
  duration_ms: number | null;
  source_revision: number | null;
  release_revision: string | null;
  image_digest: string | null;
  provenance_status:
    | "claim_recorded"
    | "source_revision_only"
    | "legacy_unavailable";
  trigger: "admin_source" | "cadence_or_manual";
  source_configuration: AdminRunSourceConfiguration | null;
  command: AdminRunCommandLink | null;
  execution: AdminRunExecutionDescriptor | null;
  resources: AdminRunResourceEvidence | null;
  stage_trace: AdminRunStageEvidence[];
  timeline: AdminRunTimeline;
}

export interface AdminRun extends AdminRunCore {
  source_key: string;
  display_name: string | null;
  is_latest_for_source: boolean;
  resolved_by_newer_success: boolean;
}

export interface AdminSource {
  source_key: string;
  source_revision: number;
  display_name: string;
  publisher: string;
  mode: string;
  region: string;
  seed_url: string;
  enabled: boolean;
  retired_at: string | null;
  retired_reason: string | null;
  superseded_by_source_key: string | null;
  review_status: "reviewed" | "unreviewed" | "expired";
  effective_status: SourceStatus;
  due: boolean;
  last_succeeded_at: string | null;
  next_due_at: string | null;
  event_count: number;
  latest_run: AdminRunCore | null;
}

export interface AdminSourcePage {
  items: AdminSource[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminRunPage {
  items: AdminRun[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminFacet {
  value: string;
  count: number;
}

export interface AdminFixedFilter {
  value: string;
  label: string;
}

export interface AdminFilterMetadata {
  modes: AdminFacet[];
  publishers: AdminFacet[];
  regions: AdminFacet[];
  source_states: AdminFixedFilter[];
  run_statuses: AdminFixedFilter[];
  window_hours: number[];
}

export interface AdminSourceConfiguration extends AdminSource {
  seed_host: string;
  handoff_only: boolean;
  approved_origins: string[];
  reviewed_at: string | null;
  review_expires_at: string | null;
  refresh_interval_minutes: number;
  min_interval_ms: number;
  page_limit: number;
  source_revision: number;
  policy_blocked: boolean;
}

export interface AdminSourceConfigurationInput {
  expected_revision: number;
  seed_url: string;
  approved_origins: string[];
  mode: string;
  enabled: boolean;
  handoff_only: true;
  review_expires_at: string | null;
  refresh_interval_minutes: number;
  min_interval_ms: number;
  page_limit: number;
  review_acknowledged: true;
}

export interface AdminSourceConfigurationUpdate {
  source_key: string;
  source_revision: number;
  reviewed_at: string;
  updated_at: string;
}

export interface AdminSourceEnabledTarget {
  source_key: string;
  expected_revision: number;
}

export interface AdminSourceBulkEnabledUpdate {
  enabled: boolean;
  requested: number;
  updated: number;
  unchanged: number;
  items: AdminSourceConfigurationUpdate[];
}

export interface AdminHistoryBucket {
  bucket_start: string;
  total_runs: number;
  succeeded_runs: number;
  failed_runs: number;
  candidate_count: number;
  canonical_count: number;
  average_duration_ms: number | null;
}

export interface AdminSourceDetail {
  generated_at: string;
  source: AdminSourceConfiguration;
  window: {
    hours: number;
    bucket_hours: number;
    starts_at: string;
    ends_at: string;
  };
  summary: {
    total_runs: number;
    succeeded_runs: number;
    failed_runs: number;
    running_runs: number;
    success_rate: number | null;
    candidate_count: number;
    canonical_count: number;
    yield_rate: number | null;
    average_duration_ms: number | null;
    p95_duration_ms: number | null;
    latest_success_at: string | null;
    latest_failure_at: string | null;
  };
  history: AdminHistoryBucket[];
  recent_runs: AdminRun[];
  current_build: {
    release_revision: string;
    image_digest: string | null;
  };
}

export type AdminCatalogQualityIssue =
  | "missing_description"
  | "missing_end_time"
  | "missing_venue"
  | "missing_city"
  | "missing_geo"
  | "missing_registration_url";

export interface AdminCatalogEntityProfile {
  name: string;
  role: "host" | "organizer" | "speaker" | "partner";
  kind: "person" | "organization";
  profile_url: string;
}

export interface AdminCatalogEvent {
  canonical_event_id: string;
  title: string;
  start_at: string;
  end_at: string | null;
  venue_name: string | null;
  city: string | null;
  latitude: number | null;
  longitude: number | null;
  description: string;
  description_length: number;
  price_status: "free" | "paid" | "unknown";
  price_min_cents: number | null;
  price_max_cents: number | null;
  price_currency: string | null;
  event_status: "scheduled" | "cancelled" | "rescheduled";
  normalizer_version: number;
  merge_version: number;
  source_event_id: string;
  registration_url: string | null;
  last_seen_at: string;
  refresh_run_key: string;
  quality_issues: AdminCatalogQualityIssue[];
  organizer_name: string | null;
  host_names: string[];
  speaker_names: string[];
  partner_names: string[];
  entity_profiles: AdminCatalogEntityProfile[];
  attendance_count: number | null;
  registration_status: "open" | "waitlist" | "sold_out" | "unknown";
}

export interface AdminCatalogEventCursor {
  startAt: string;
  canonicalEventId: string;
}

export interface AdminCatalogEventPage {
  items: AdminCatalogEvent[];
  source_total: number;
  limit: number;
  has_more: boolean;
  next_start_at: string | null;
  next_canonical_event_id: string | null;
  query: string | null;
}

export interface AdminCommand {
  command_id: string;
  action: "refresh_source" | "refresh_due";
  source_key: string | null;
  status: CommandStatus;
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  result: Record<string, string | number | boolean | null> | null;
  error_code: string | null;
  source_revision: number | null;
  release_revision: string | null;
  image_digest: string | null;
  executor_release_revision: string | null;
  executor_source_revision: number | null;
  executor_image_digest: string | null;
}

export interface AdminCommandList {
  items: AdminCommand[];
}

export type AdminCommandWorkerState =
  | "awaiting_claim"
  | "retry_scheduled"
  | "heartbeat_live"
  | "lease_expired"
  | "finished"
  | "failed";

export interface AdminCommandDetailCommand extends AdminCommand {
  requested_by: string | null;
  attempt_count: number;
  available_at: string | null;
  lease_expires_at: string | null;
  worker_state: AdminCommandWorkerState | null;
}

export interface AdminCommandProgress {
  total: number;
  pending: number;
  running: number;
  succeeded: number;
  failed: number;
  paused: number;
  completed: number;
  active_source_key: string | null;
  active_display_name: string | null;
  active_phase: AdminCommandRunPhase | null;
  updated_at: string;
}

export type AdminCommandRunStatus =
  | "pending"
  | "running"
  | "paused"
  | "succeeded"
  | "failed";

export type AdminCommandRunPhase =
  | "awaiting_dispatch"
  | "collecting"
  | "deferred"
  | "completed"
  | "failed";

export interface AdminCommandLinkedRun {
  position: number;
  source_key: string;
  display_name: string;
  run_key: string;
  status: AdminCommandRunStatus;
  phase: AdminCommandRunPhase;
  linked_at: string;
  started_at: string | null;
  completed_at: string | null;
  candidate_count: number | null;
  canonical_count: number | null;
  error_code: string | null;
  attempt_count: number | null;
  duration_ms: number | null;
  updated_at: string;
}

export interface AdminCommandDetail {
  generated_at: string;
  command: AdminCommandDetailCommand;
  progress: AdminCommandProgress;
  runs: AdminCommandLinkedRun[];
}

export interface AdminSourceFilters {
  query: string;
  state: SourceState;
  mode: string;
  publisher: string;
  region: string;
  includeFixtures: boolean;
  sortBy: AdminSourceSort;
  sortDirection: AdminSortDirection;
}

export interface AdminRunFilters {
  status: RunStatus | "";
  sourceKey: string;
  windowHours: number;
  includeFixtures: boolean;
}
