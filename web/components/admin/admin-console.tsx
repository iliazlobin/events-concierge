"use client";

import {
  Activity,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Command as CommandIcon,
  Copy,
  ChevronsUpDown,
  FileClock,
  Filter,
  Layers3,
  ListFilter,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  Power,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  Workflow,
  X,
} from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import { FleetOverview } from "@/components/admin/fleet-overview";
import { RunExecutionEvidence } from "@/components/admin/run-execution-evidence";
import { SourceDetail } from "@/components/admin/source-detail";
import {
  enqueueAdminCommand,
  getAllAdminRuns,
  getAdminCommandDetail,
  getAdminCommands,
  getAdminFilters,
  getAdminOverview,
  getAdminSourceDetail,
  getAdminSources,
  setAdminSourceEnabled,
  setAdminSourcesEnabled,
} from "@/lib/admin-api";
import {
  adminHistoryLocationFromUrl,
  adminHistoryUrl,
} from "@/lib/admin-history";
import {
  commandEvidence,
  commandExecutionPresentation,
  commandNeedsReview,
  commandOutcomePresentation,
  commandReviewDisposition,
  commandResultSummary,
  displayedRunStatus,
  formatDateTime,
  formatDuration,
  formatFullDate,
  formatNumber,
  formatPercent,
  formatRelativeTime,
  humanize,
  isFailedRun,
  isPacerDeferredRun,
  presentAdminRuns,
  runStatusLabel,
  shortRevision,
  sourceAttentionScore,
  sourceDiagnostics,
  statusLabel,
} from "@/lib/admin-presentation";
import {
  bucketAverageDuration,
  bucketFailureTotal,
  bucketSuccessRate,
  bucketTotal,
  bucketYieldRate,
  buildRunBuckets,
  RUN_WINDOW_CONFIG,
} from "@/lib/admin-run-chart";
import { buildAdminPipelineSummary } from "@/lib/admin-pipeline";
import {
  ADMIN_COMMAND_DETAIL_POLL_INTERVAL_MS,
  isAdminCommandPollAbort,
  shouldPollAdminCommandDetail,
} from "@/lib/admin-command-polling";
import {
  adminSourceEnabledTargets,
  selectableAdminSources,
} from "@/lib/admin-source-bulk";
import {
  adminSourceIsRetired,
  adminSourceLifecycleLabel,
} from "@/lib/admin-source-lifecycle";
import type {
  AdminRunDisposition,
  AdminRunView,
} from "@/lib/admin-presentation";
import type {
  OverviewRunWindow,
  RunBucket,
} from "@/lib/admin-run-chart";
import type {
  AdminCommand,
  AdminCommandDetail,
  AdminCommandLinkedRun,
  AdminFilterMetadata,
  AdminOverview,
  AdminRun,
  AdminRunFilters,
  AdminRunPage,
  AdminSource,
  AdminSourceDetail,
  AdminSourceFilters,
  AdminSourcePage,
  AdminSourceSort,
  AdminSortDirection,
  AdminTab,
  CommandStatus,
} from "@/lib/admin-types";
import { ApiError } from "@/lib/api";

const DEFAULT_SOURCE_FILTERS: AdminSourceFilters = {
  query: "",
  state: "all",
  mode: "",
  publisher: "",
  region: "",
  includeFixtures: false,
  sortBy: "source",
  sortDirection: "asc",
};

const SOURCE_SORT_DEFAULT_DIRECTION: Record<AdminSourceSort, AdminSortDirection> = {
  source: "asc",
  health: "asc",
  catalog: "desc",
  last_success: "desc",
  latest_run: "asc",
  output: "desc",
};

const DEFAULT_RUN_FILTERS: AdminRunFilters = {
  status: "",
  sourceKey: "",
  windowHours: 168,
  includeFixtures: false,
};

const RUNS_PAGE_SIZE = 50;

const TABS: Array<{
  value: AdminTab;
  label: string;
  icon: typeof Activity;
}> = [
  { value: "overview", label: "Overview", icon: Activity },
  { value: "pipeline", label: "Pipeline", icon: Workflow },
  { value: "sources", label: "Sources", icon: Layers3 },
  { value: "runs", label: "Runs", icon: FileClock },
  { value: "commands", label: "Commands", icon: CommandIcon },
];

function sourceIsCommandEligible(source: AdminSource | null): source is AdminSource {
  return Boolean(
    source
    && !adminSourceIsRetired(source)
    && source.enabled
    && source.review_status === "reviewed"
    && source.effective_status !== "policy_blocked",
  );
}

function readableError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "The ingestion control plane did not respond.";
}

function sourceStatusTone(status: string): string {
  if (status === "failed" || status === "policy_blocked" || status === "review_expired") {
    return "critical";
  }
  if (
    status === "due"
    || status === "paused"
    || status === "unreviewed"
    || status === "needs_review"
  ) {
    return "warning";
  }
  if (status === "succeeded" || status === "active" || status === "running") return "healthy";
  return "neutral";
}

function sourceOutput(source: AdminSource): string {
  const run = source.latest_run;
  if (!run || run.candidate_count === null || run.canonical_count === null) return "No run output";
  return `${formatNumber(run.candidate_count)} → ${formatNumber(run.canonical_count)}`;
}

function AdminStatus({
  status,
  label,
}: {
  status: string;
  label?: string;
}) {
  return (
    <span className={`admin-status admin-status--${sourceStatusTone(status)}`}>
      <i />
      {label ?? humanize(status)}
    </span>
  );
}

function SourceSortHeader({
  field,
  label,
  filters,
  onChange,
}: {
  field: AdminSourceSort;
  label: string;
  filters: AdminSourceFilters;
  onChange: (field: AdminSourceSort) => void;
}) {
  const active = filters.sortBy === field;
  const ariaSort = active
    ? filters.sortDirection === "asc" ? "ascending" : "descending"
    : "none";
  return (
    <th aria-sort={ariaSort}>
      <button
        className={`admin-source-sort ${active ? "is-active" : ""}`}
        type="button"
        onClick={() => onChange(field)}
        title={`Sort by ${label.toLowerCase()}${active ? `, currently ${ariaSort}` : ""}`}
      >
        <span>{label}</span>
        {active
          ? filters.sortDirection === "asc"
            ? <ArrowUp aria-hidden="true" />
            : <ArrowDown aria-hidden="true" />
          : <ChevronsUpDown aria-hidden="true" />}
      </button>
    </th>
  );
}

interface OverviewPanelProps {
  overview: AdminOverview | null;
  sources: AdminSource[];
  runs: AdminRun[];
  runTotal: number;
  runWindow: OverviewRunWindow;
  runsLoading: boolean;
  loading: boolean;
  onRunWindowChange: (windowHours: OverviewRunWindow) => void;
  onOpenSource: (sourceKey: string) => void;
  onOpenSources: () => void;
  onOpenCommands: () => void;
  onReviewFailures: () => void;
  onRefreshDue: () => void;
  refreshSubmitting: boolean;
  refreshDuePending: boolean;
  pendingCommands: number;
}

type OverviewRunMetric = "volume" | "reliability" | "yield" | "output" | "duration";

const RUN_WINDOWS: Array<{ value: OverviewRunWindow; label: string }> = [
  { value: 24, label: "24h" },
  { value: 168, label: "7d" },
  { value: 720, label: "30d" },
];

const RUN_METRICS: Array<{
  value: OverviewRunMetric;
  label: string;
}> = [
  { value: "volume", label: "Runs" },
  { value: "reliability", label: "Success" },
  { value: "yield", label: "Publish yield" },
  { value: "output", label: "Stage flow" },
  { value: "duration", label: "Latency" },
];


function RunLegend({ metric }: { metric: OverviewRunMetric }) {
  const items = metric === "volume"
      ? [
        ["success", "Succeeded"],
        ["failed", "Unresolved failure"],
        ["resolved", "Resolved failure"],
        ["running", "Running"],
        ["paused", "Deferred / paused"],
      ]
    : metric === "output"
      ? [["candidate", "Collected"], ["canonical", "Published"]]
      : metric === "duration"
        ? [["duration", "Average duration"]]
        : metric === "yield"
          ? [["yield", "Publish yield"]]
          : [["success", "Success rate"]];
  return (
    <div className="admin-inline-legend" aria-label={`${humanize(metric)} legend`}>
      {items.map(([tone, label]) => (
        <span key={tone}><i className={`is-${tone}`} />{label}</span>
      ))}
    </div>
  );
}

function RunBarChart({
  buckets,
  metric,
  intervalLabel,
  partial,
  selectedKey,
  onSelect,
}: {
  buckets: RunBucket[];
  metric: OverviewRunMetric;
  intervalLabel: string;
  partial: boolean;
  selectedKey: string | null;
  onSelect: (key: string) => void;
}) {
  const maxVolume = Math.max(1, ...buckets.map(bucketTotal));
  const maxOutput = Math.max(
    1,
    ...buckets.flatMap((bucket) => [bucket.candidates, bucket.canonical]),
  );
  const maxDuration = Math.max(
    1,
    ...buckets.map((bucket) => bucketAverageDuration(bucket) ?? 0),
  );
  const ariaSummary = metric === "volume"
    ? `${buckets.reduce((total, bucket) => total + bucket.succeeded, 0)} succeeded, `
      + `${buckets.reduce((total, bucket) => total + bucket.failed, 0)} failed, `
      + `${buckets.reduce((total, bucket) => total + bucket.resolved, 0)} resolved failures, `
      + `${buckets.reduce((total, bucket) => total + bucket.running, 0)} running, and `
      + `${buckets.reduce((total, bucket) => total + bucket.paused, 0)} deferred or paused runs`
    : metric === "output"
      ? `${formatNumber(buckets.reduce((total, bucket) => total + bucket.candidates, 0))} collected and `
        + `${formatNumber(buckets.reduce((total, bucket) => total + bucket.canonical, 0))} published records`
      : metric === "duration"
        ? `Average run duration by ${intervalLabel}`
        : metric === "yield"
          ? `Publish yield by ${intervalLabel}`
          : `Success rate by ${intervalLabel}`;
  return (
    <div
      className={`admin-metric-chart admin-metric-chart--${metric}`}
      aria-label={`${partial ? "Partial loaded sample. " : ""}${ariaSummary}`}
    >
      <div
        className="admin-metric-chart__plot"
        style={{ gridTemplateColumns: `repeat(${buckets.length}, minmax(0, 1fr))` }}
      >
        {buckets.map((bucket) => {
          const total = bucketTotal(bucket);
          const successRate = bucketSuccessRate(bucket);
          const yieldRate = bucketYieldRate(bucket);
          const averageDuration = bucketAverageDuration(bucket);
          const title = metric === "volume"
            ? `${bucket.dateLabel}: ${bucket.succeeded} succeeded, ${bucket.failed} unresolved failures, ${bucket.resolved} resolved failures, ${bucket.running} running, ${bucket.paused} deferred or paused`
            : metric === "output"
              ? `${bucket.dateLabel}: ${formatNumber(bucket.candidates)} collected, ${formatNumber(bucket.canonical)} published`
              : metric === "duration"
                ? `${bucket.dateLabel}: ${formatDuration(averageDuration)} average, ${formatDuration(bucket.slowestDurationMs || null)} slowest`
                : metric === "yield"
                  ? `${bucket.dateLabel}: ${formatPercent(yieldRate)} publish yield`
                  : `${bucket.dateLabel}: ${formatPercent(successRate)} success`;
          return (
            <div
              className={`admin-metric-bucket${selectedKey === bucket.key ? " is-selected" : ""}`}
              key={bucket.key}
            >
              <button
                className="admin-metric-bucket__track"
                type="button"
                title={title}
                aria-label={`${title}. Inspect this interval.`}
                aria-pressed={selectedKey === bucket.key}
                onClick={() => onSelect(bucket.key)}
              >
                {metric === "volume" ? (
                  total ? (
                    <div
                      className="admin-metric-stack"
                      style={{ height: `${Math.max(4, (total / maxVolume) * 100)}%` }}
                    >
                      {bucket.paused ? <i className="is-paused" style={{ flex: bucket.paused }} /> : null}
                      {bucket.running ? <i className="is-running" style={{ flex: bucket.running }} /> : null}
                      {bucket.failed ? <i className="is-failed" style={{ flex: bucket.failed }} /> : null}
                      {bucket.resolved ? <i className="is-resolved" style={{ flex: bucket.resolved }} /> : null}
                      {bucket.succeeded ? <i className="is-success" style={{ flex: bucket.succeeded }} /> : null}
                    </div>
                  ) : <i className="admin-run-day__empty" />
                ) : null}
                {metric === "output" ? (
                  bucket.candidates || bucket.canonical ? (
                    <div className="admin-metric-pair">
                      <i
                        className="is-candidate"
                        style={{ height: `${Math.max(3, (bucket.candidates / maxOutput) * 100)}%` }}
                      />
                      <i
                        className="is-canonical"
                        style={{ height: `${Math.max(3, (bucket.canonical / maxOutput) * 100)}%` }}
                      />
                    </div>
                  ) : <i className="admin-run-day__empty" />
                ) : null}
                {metric === "reliability" ? (
                  successRate !== null ? (
                    <div className="admin-metric-rate">
                      <span>{formatPercent(successRate)}</span>
                      <i
                        className={
                          successRate >= 0.9
                            ? "is-healthy"
                            : successRate >= 0.75 ? "is-warning" : "is-critical"
                        }
                        style={{ height: `${Math.max(3, successRate * 100)}%` }}
                      />
                    </div>
                  ) : <span className="admin-metric-rate__empty">—</span>
                ) : null}
                {metric === "yield" ? (
                  yieldRate !== null ? (
                    <div className="admin-metric-rate">
                      <span>{formatPercent(yieldRate)}</span>
                      <i
                        className="is-yield"
                        style={{ height: `${Math.max(3, Math.min(1, yieldRate) * 100)}%` }}
                      />
                    </div>
                  ) : <span className="admin-metric-rate__empty">—</span>
                ) : null}
                {metric === "duration" ? (
                  averageDuration !== null ? (
                    <div className="admin-metric-rate">
                      <span>{formatDuration(averageDuration)}</span>
                      <i
                        className="is-duration"
                        style={{ height: `${Math.max(3, (averageDuration / maxDuration) * 100)}%` }}
                      />
                    </div>
                  ) : <span className="admin-metric-rate__empty">—</span>
                ) : null}
              </button>
              <span>{bucket.label}</span>
              <small>{bucket.dateLabel}</small>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RunBucketInspector({
  bucket,
  partial,
  onOpenSource,
  onReviewFailures,
}: {
  bucket: RunBucket;
  partial: boolean;
  onOpenSource: (sourceKey: string) => void;
  onReviewFailures: () => void;
}) {
  const failures = bucketFailureTotal(bucket);
  return (
    <section
      className="admin-run-bucket-inspector"
      aria-label={`Selected interval ${bucket.dateLabel}`}
      aria-live="polite"
    >
      <div className="admin-run-bucket-inspector__heading">
        <div>
          <span>Selected interval</span>
          <strong>{bucket.dateLabel}</strong>
        </div>
        <b>{formatNumber(bucketTotal(bucket))} {partial ? "loaded " : ""}runs</b>
      </div>
      <dl>
        <div><dt>{partial ? "Sample success" : "Success"}</dt><dd>{formatPercent(bucketSuccessRate(bucket))}</dd></div>
        <div><dt>Failures</dt><dd>{formatNumber(bucket.failed)} open · {formatNumber(bucket.resolved)} resolved</dd></div>
        <div><dt>Stage flow</dt><dd>{formatNumber(bucket.candidates)} collected → {formatNumber(bucket.canonical)} published</dd></div>
        <div><dt>{partial ? "Sample yield" : "Yield"}</dt><dd>{formatPercent(bucketYieldRate(bucket))}</dd></div>
        <div><dt>Average</dt><dd>{formatDuration(bucketAverageDuration(bucket))}</dd></div>
        <div><dt>Slowest</dt><dd>{formatDuration(bucket.slowestDurationMs || null)}</dd></div>
      </dl>
      <div className="admin-run-bucket-inspector__sources">
        <span>
          {formatNumber(bucket.sourceKeys.length)} source{bucket.sourceKeys.length === 1 ? "" : "s"}
          {partial ? " in loaded sample" : " observed"}
        </span>
        {bucket.failedSourceKeys.slice(0, 4).map((sourceKey) => (
          <button type="button" key={sourceKey} onClick={() => onOpenSource(sourceKey)}>
            {humanize(sourceKey)}
            <ChevronRight aria-hidden="true" />
          </button>
        ))}
        {failures ? (
          <button type="button" onClick={onReviewFailures}>
            Inspect all window failures
            <ArrowRight aria-hidden="true" />
          </button>
        ) : null}
      </div>
    </section>
  );
}

function RunDataTable({
  buckets,
  windowLabel,
  partial,
  loadedCount,
  totalCount,
}: {
  buckets: RunBucket[];
  windowLabel: string;
  partial: boolean;
  loadedCount: number;
  totalCount: number;
}) {
  const total = buckets.reduce((sum, bucket) => sum + bucketTotal(bucket), 0);
  const succeeded = buckets.reduce((sum, bucket) => sum + bucket.succeeded, 0);
  const failed = buckets.reduce((sum, bucket) => sum + bucket.failed, 0);
  const resolved = buckets.reduce((sum, bucket) => sum + bucket.resolved, 0);
  const running = buckets.reduce((sum, bucket) => sum + bucket.running, 0);
  const paused = buckets.reduce((sum, bucket) => sum + bucket.paused, 0);
  const candidates = buckets.reduce((sum, bucket) => sum + bucket.candidates, 0);
  const canonical = buckets.reduce((sum, bucket) => sum + bucket.canonical, 0);
  return (
    <div className="admin-run-data-table">
      {partial ? (
        <p className="admin-run-data-table__partial" role="note">
          <CircleAlert aria-hidden="true" />
          <span>
            <strong>Partial loaded sample.</strong> This table covers the newest{" "}
            {formatNumber(loadedCount)} of {formatNumber(totalCount)} records; its counts and rates
            are not full-window totals.
          </span>
        </p>
      ) : null}
      <div className="admin-overview-table-scroll">
        <table className="admin-overview-table">
          <caption className="sr-only">
            {partial
              ? `Partial ingestion run sample for the ${windowLabel}: newest ${formatNumber(loadedCount)} of ${formatNumber(totalCount)} records`
              : `Ingestion run data for the ${windowLabel}`}
          </caption>
          <thead>
            <tr>
              {[
                "Period",
                "Total",
                "Succeeded",
                "Open failed",
                "Resolved",
                "Running",
                "Deferred / paused",
                partial ? "Sample success" : "Success",
                "Collected",
                "Published",
                partial ? "Sample publish yield" : "Publish yield",
                "Average",
                "Slowest",
              ].map((label) => (
                <th scope="col" key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {buckets.map((bucket) => (
              <tr key={bucket.key}>
                <td><strong>{bucket.label}</strong><small>{bucket.dateLabel}</small></td>
                <td>{formatNumber(bucketTotal(bucket))}</td>
                <td>{formatNumber(bucket.succeeded)}</td>
                <td>{formatNumber(bucket.failed)}</td>
                <td>{formatNumber(bucket.resolved)}</td>
                <td>{formatNumber(bucket.running)}</td>
                <td>{formatNumber(bucket.paused)}</td>
                <td>{formatPercent(bucketSuccessRate(bucket))}</td>
                <td>{formatNumber(bucket.candidates)}</td>
                <td>{formatNumber(bucket.canonical)}</td>
                <td>{formatPercent(bucketYieldRate(bucket))}</td>
                <td>{formatDuration(bucketAverageDuration(bucket))}</td>
                <td>{formatDuration(bucket.slowestDurationMs || null)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">{partial ? "Loaded sample" : "Window total"}</th>
              <td>{formatNumber(total)}</td>
              <td>{formatNumber(succeeded)}</td>
              <td>{formatNumber(failed)}</td>
              <td>{formatNumber(resolved)}</td>
              <td>{formatNumber(running)}</td>
              <td>{formatNumber(paused)}</td>
              <td>{formatPercent(
                succeeded + failed + resolved
                  ? succeeded / (succeeded + failed + resolved)
                  : null,
              )}</td>
              <td>{formatNumber(candidates)}</td>
              <td>{formatNumber(canonical)}</td>
              <td>{formatPercent(candidates ? canonical / candidates : null)}</td>
              <td>
                {formatDuration(
                  buckets.reduce((sum, bucket) => sum + bucket.durationSamples, 0)
                    ? Math.round(
                      buckets.reduce((sum, bucket) => sum + bucket.durationTotalMs, 0)
                        / buckets.reduce((sum, bucket) => sum + bucket.durationSamples, 0),
                    )
                    : null,
                )}
              </td>
              <td>
                {formatDuration(Math.max(...buckets.map((bucket) => bucket.slowestDurationMs)) || null)}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}

function OverviewPanel({
  overview,
  sources,
  runs,
  runTotal,
  runWindow,
  runsLoading,
  loading,
  onRunWindowChange,
  onOpenSource,
  onOpenSources,
  onOpenCommands,
  onReviewFailures,
  onRefreshDue,
  refreshSubmitting,
  refreshDuePending,
  pendingCommands,
}: OverviewPanelProps) {
  const [runMetric, setRunMetric] = useState<OverviewRunMetric>("volume");
  const [showRunData, setShowRunData] = useState(false);
  const [selectedBucketKey, setSelectedBucketKey] = useState<string | null>(null);
  const summary = overview?.summary;
  const runWindowConfig = RUN_WINDOW_CONFIG[runWindow];
  const attentionTotal = useMemo(
    () => sources.filter((source) => (
      sourceAttentionScore(source) > 0
      && (overview?.policy.allowed || source.effective_status !== "policy_blocked")
    )).length,
    [overview?.policy.allowed, sources],
  );
  const attention = useMemo(
    () => [...sources]
      .filter((source) => (
        sourceAttentionScore(source) > 0
        && (overview?.policy.allowed || source.effective_status !== "policy_blocked")
      ))
      .sort((a, b) => sourceAttentionScore(b) - sourceAttentionScore(a))
      .slice(0, 5),
    [overview?.policy.allowed, sources],
  );
  const neverCollected = sources.filter(
    (source) => source.enabled && !source.last_succeeded_at,
  ).length;
  const runDataPartial = runs.length < runTotal;
  const runBuckets = useMemo(
    () => overview?.generated_at
      ? buildRunBuckets(runs, overview.generated_at, runWindow)
      : [],
    [overview?.generated_at, runWindow, runs],
  );
  const succeededRuns = runBuckets.reduce((total, bucket) => total + bucket.succeeded, 0);
  const failedRuns = runBuckets.reduce(
    (total, bucket) => total + bucketFailureTotal(bucket),
    0,
  );
  const unresolvedRuns = runBuckets.reduce((total, bucket) => total + bucket.failed, 0);
  const resolvedRuns = runBuckets.reduce((total, bucket) => total + bucket.resolved, 0);
  const runningRuns = runBuckets.reduce(
    (total, bucket) => total + bucket.running,
    0,
  );
  const deferredOrPausedRuns = runBuckets.reduce(
    (total, bucket) => total + bucket.paused,
    0,
  );
  const openRuns = runningRuns + deferredOrPausedRuns;
  const successRate = succeededRuns + failedRuns
    ? succeededRuns / (succeededRuns + failedRuns)
    : null;
  const candidateTotal = runBuckets.reduce(
    (total, bucket) => total + bucket.candidates,
    0,
  );
  const canonicalTotal = runBuckets.reduce(
    (total, bucket) => total + bucket.canonical,
    0,
  );
  const yieldRate = candidateTotal ? canonicalTotal / candidateTotal : null;
  const totalRuns = succeededRuns + failedRuns + openRuns;
  const durationSamples = runBuckets.reduce(
    (total, bucket) => total + bucket.durationSamples,
    0,
  );
  const averageDuration = durationSamples
    ? Math.round(
      runBuckets.reduce((total, bucket) => total + bucket.durationTotalMs, 0)
        / durationSamples,
    )
    : null;
  const healthySources = Math.max(
    0,
    (summary?.active_sources ?? 0) - (summary?.due_sources ?? 0),
  );
  const metricValue = runMetric === "volume"
    ? `${formatNumber(totalRuns)} ${runDataPartial ? "loaded " : ""}runs`
    : runMetric === "reliability"
      ? `${formatPercent(successRate)}${runDataPartial ? " sample rate" : ""}`
      : runMetric === "yield"
        ? `${formatPercent(yieldRate)}${runDataPartial ? " sample yield" : ""}`
        : runMetric === "duration"
          ? `${formatDuration(averageDuration)}${runDataPartial ? " sample average" : ""}`
          : `${formatNumber(candidateTotal)} → ${formatNumber(canonicalTotal)}${
            runDataPartial ? " in loaded sample" : ""
          }`;
  const metricDetailBase = runMetric === "volume"
    ? `${formatNumber(succeededRuns)} succeeded · ${formatNumber(unresolvedRuns)} open failed · ${formatNumber(resolvedRuns)} resolved${
      runningRuns ? ` · ${formatNumber(runningRuns)} running` : ""
    }${
      deferredOrPausedRuns
        ? ` · ${formatNumber(deferredOrPausedRuns)} deferred / paused`
        : ""
    }`
    : runMetric === "reliability"
      ? `${formatNumber(succeededRuns)} of ${formatNumber(succeededRuns + failedRuns)} completed successfully`
      : runMetric === "yield"
        ? `${formatNumber(canonicalTotal)} published from ${formatNumber(candidateTotal)} collected`
        : runMetric === "duration"
          ? `${formatNumber(durationSamples)} timed run${durationSamples === 1 ? "" : "s"}`
          : `${formatPercent(yieldRate)} publish yield`;
  const metricDetail = runDataPartial
    ? `Partial · newest ${formatNumber(runs.length)} of ${formatNumber(runTotal)} records · ${metricDetailBase}`
    : metricDetailBase;
  const selectedBucket = runBuckets.find((bucket) => bucket.key === selectedBucketKey)
    ?? [...runBuckets].reverse().find((bucket) => bucketTotal(bucket) > 0)
    ?? runBuckets.at(-1)
    ?? null;

  useEffect(() => {
    if (!selectedBucket || selectedBucket.key === selectedBucketKey) return;
    setSelectedBucketKey(selectedBucket.key);
  }, [selectedBucket, selectedBucketKey]);

  return (
    <div className={`admin-panel admin-panel--overview ${
      loading ? "is-loading" : ""
    } ${
      runsLoading ? "is-runs-loading" : ""
    }`}>
      <section className="admin-hero">
        <div className="admin-hero__copy">
          <div className="admin-hero__title-row">
            <h1>Ingestion console</h1>
            <span className="admin-snapshot">
              snapshot <time title={formatFullDate(overview?.generated_at ?? null)}>{formatDateTime(overview?.generated_at ?? null)}</time>
            </span>
          </div>
          <div className="admin-engineering-meta" aria-label="Control-plane metadata">
            <span className={overview ? "is-live" : loading ? "is-syncing" : "is-error"}>
              <i />
              {overview ? "api / ready" : loading ? "api / syncing" : "api / unavailable"}
            </span>
            <span>
              refresh <code>{overview ? overview.policy.allowed ? "allowed" : "blocked" : "loading"}</code>
            </span>
            <span>last success <time>{formatRelativeTime(overview?.latest_success_at ?? null)}</time></span>
          </div>
        </div>
        <div className="admin-hero__action">
          <div>
            <span>Needs refresh</span>
            <strong>{formatNumber(summary?.due_sources ?? 0)} sources</strong>
            <small>{neverCollected} never collected</small>
          </div>
          <button
            className="admin-primary-button"
            type="button"
            disabled={
              refreshSubmitting
              || refreshDuePending
              || !overview?.policy.allowed
              || !summary?.due_sources
            }
            onClick={onRefreshDue}
          >
            {refreshDuePending
              ? <CommandIcon aria-hidden="true" />
              : refreshSubmitting
              ? <LoaderCircle className="spin" aria-hidden="true" />
              : <RefreshCw aria-hidden="true" />}
            {refreshDuePending
              ? "Refresh queued"
              : refreshSubmitting ? "Queueing…" : "Refresh due sources"}
          </button>
        </div>
      </section>

      <section className="admin-overview-posture" aria-label="Fleet posture">
        <div>
          <strong>{formatNumber(summary?.active_sources ?? 0)} / {formatNumber(summary?.sources ?? sources.length)}</strong>
          <span>enabled</span>
        </div>
        <div>
          <strong>{formatNumber(healthySources)}</strong>
          <span>current</span>
        </div>
        <div>
          <strong>{formatNumber(attentionTotal)}</strong>
          <span>need review</span>
        </div>
        <div>
          <strong>{formatNumber(summary?.catalog_events ?? 0)}</strong>
          <span>catalog events</span>
        </div>
        <button type="button" onClick={onOpenSources}>
          View sources <ArrowRight aria-hidden="true" />
        </button>
      </section>

      {pendingCommands ? (
        <button className="admin-overview-pending" type="button" onClick={onOpenCommands}>
          <CommandIcon aria-hidden="true" />
          <span>
            <strong>{formatNumber(pendingCommands)} refresh command{pendingCommands === 1 ? "" : "s"} active</strong>
            <small>Open the durable command queue</small>
          </span>
          <ArrowRight aria-hidden="true" />
        </button>
      ) : null}

      {!overview?.policy.allowed && overview ? (
        <div className="admin-overview-policy-alert" role="alert">
          <ShieldAlert aria-hidden="true" />
          <span><strong>Refreshes are blocked.</strong> {overview.policy.reason}</span>
        </div>
      ) : null}

      <section
        className="admin-management-grid admin-management-grid--attention"
        data-overview-section="attention"
      >
        <article className="admin-surface admin-attention-panel admin-attention-panel--compact">
          <div className="admin-card-heading">
            <div>
              <span>Action queue</span>
              <h2>Needs attention</h2>
            </div>
            <div className="admin-card-actions">
              <span
                className="admin-count"
                title={`Showing the ${attention.length} highest-priority sources`}
              >
                {attentionTotal}
              </span>
              <button className="admin-text-button" type="button" onClick={onOpenSources}>
                View sources <ArrowRight aria-hidden="true" />
              </button>
            </div>
          </div>
          {loading && !sources.length ? (
            <div className="admin-empty-state admin-empty-state--compact">
              <LoaderCircle className="spin" aria-hidden="true" />
              <p>Loading source signals…</p>
            </div>
          ) : attention.length ? (
            <div className="admin-overview-table-scroll">
              <table className="admin-overview-table admin-overview-table--attention">
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Issue</th>
                    <th>Last success</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {attention.map((source) => {
                    const diagnostic = sourceDiagnostics(source)[0];
                    return (
                      <tr key={source.source_key}>
                        <td>
                          <button type="button" onClick={() => onOpenSource(source.source_key)}>
                            <strong>{source.display_name}</strong>
                            <small>{source.source_key}</small>
                          </button>
                        </td>
                        <td>
                          <span className={`admin-priority admin-priority--${diagnostic.tone}`}>
                            <i />{diagnostic.title}
                          </span>
                        </td>
                        <td>{formatRelativeTime(source.last_succeeded_at)}</td>
                        <td>
                          <button
                            className="admin-table-action"
                            type="button"
                            onClick={() => onOpenSource(source.source_key)}
                            aria-label={`Troubleshoot ${source.display_name}`}
                          >
                            Review <ChevronRight aria-hidden="true" />
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="admin-empty-state admin-empty-state--compact">
              <CheckCircle2 aria-hidden="true" />
              <p>No source currently needs immediate attention.</p>
            </div>
          )}
        </article>
      </section>

      <section
        className="admin-management-grid admin-management-grid--primary"
        data-overview-section="runs"
      >
        <article className="admin-surface admin-run-reliability admin-run-reliability--simple">
          <div className="admin-card-heading">
            <div>
              <span>{runWindowConfig.eyebrow}</span>
              <h2>Operating trend</h2>
            </div>
            {failedRuns ? (
              <button className="admin-text-button" type="button" onClick={onReviewFailures}>
                {runDataPartial
                  ? "Inspect failed-run history"
                  : `Inspect ${formatNumber(failedRuns)} failed runs`}
                <ArrowRight aria-hidden="true" />
              </button>
            ) : null}
          </div>
          {runDataPartial ? (
            <div className="admin-run-partial-note" role="note">
              <CircleAlert aria-hidden="true" />
              <span>
                <strong>Partial run sample.</strong> The newest {formatNumber(runs.length)} of{" "}
                {formatNumber(runTotal)} records are loaded. Charts, counts, rates, and averages
                below describe only that loaded sample.
              </span>
            </div>
          ) : null}
          <div
            className="admin-run-readout"
            data-overview-readout
            aria-label={`${runWindowConfig.eyebrow} ${RUN_METRICS.find((item) => item.value === runMetric)?.label}`}
          >
            <strong>{metricValue}</strong>
            <span>{metricDetail}</span>
          </div>
          <div className="admin-chart-controls">
            <div className="admin-chart-control">
              <span>Window</span>
              <div className="admin-segmented" role="group" aria-label="Run chart time window">
                {RUN_WINDOWS.map(({ value, label }) => (
                  <button
                    type="button"
                    key={value}
                    className={runWindow === value ? "is-active" : ""}
                    aria-pressed={runWindow === value}
                    onClick={() => onRunWindowChange(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <div className="admin-chart-control">
              <span>Chart</span>
              <div className="admin-segmented" role="group" aria-label="Run chart metric">
                {RUN_METRICS.map(({ value, label }) => (
                  <button
                    type="button"
                    key={value}
                    className={runMetric === value ? "is-active" : ""}
                    aria-pressed={runMetric === value}
                    onClick={() => setRunMetric(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <button
              className={`admin-chart-data-toggle ${showRunData ? "is-active" : ""}`}
              type="button"
              aria-expanded={showRunData}
              aria-controls="admin-run-data"
              onClick={() => setShowRunData((current) => !current)}
            >
              {showRunData ? "Hide data" : "View data"}
            </button>
            {runsLoading ? (
              <span className="admin-chart-updating">
                <LoaderCircle className="spin" aria-hidden="true" />
                Updating
              </span>
            ) : null}
          </div>
          <div
            className="admin-run-visual"
            aria-busy={runsLoading}
          >
            <div className="admin-chart-title">
              <span>
                {runMetric === "volume"
                  ? `Run volume by ${runWindowConfig.intervalLabel}`
                  : runMetric === "reliability"
                    ? `Completed-run success by ${runWindowConfig.intervalLabel}`
                    : runMetric === "yield"
                      ? `Publish yield by ${runWindowConfig.intervalLabel}`
                      : runMetric === "duration"
                        ? `Average latency by ${runWindowConfig.intervalLabel}`
                        : `Collected and published by ${runWindowConfig.intervalLabel}`}
                {runDataPartial
                  ? ` · partial sample: newest ${formatNumber(runs.length)} of ${formatNumber(runTotal)}`
                  : ""}
              </span>
              <RunLegend metric={runMetric} />
            </div>
            {runBuckets.length ? (
              <>
                <RunBarChart
                  buckets={runBuckets}
                  metric={runMetric}
                  intervalLabel={runWindowConfig.intervalLabel}
                  partial={runDataPartial}
                  selectedKey={selectedBucket?.key ?? null}
                  onSelect={setSelectedBucketKey}
                />
                {selectedBucket ? (
                  <RunBucketInspector
                    bucket={selectedBucket}
                    partial={runDataPartial}
                    onOpenSource={onOpenSource}
                    onReviewFailures={onReviewFailures}
                  />
                ) : null}
              </>
            ) : (
              <div className="admin-empty-state admin-empty-state--compact">
                <LoaderCircle className="spin" aria-hidden="true" />
                <p>Loading run history…</p>
              </div>
            )}
            <span className="sr-only" aria-live="polite">
              {runWindowConfig.eyebrow}; {RUN_METRICS.find((item) => item.value === runMetric)?.label};
              {runDataPartial
                ? ` partial loaded sample of ${formatNumber(runs.length)} out of ${formatNumber(runTotal)} records;`
                : ""}
              {showRunData ? " data table expanded" : " chart view"}
            </span>
          </div>
          {showRunData ? (
            <div id="admin-run-data">
              <RunDataTable
                buckets={runBuckets}
                windowLabel={runWindowConfig.eyebrow.toLowerCase()}
                partial={runDataPartial}
                loadedCount={runs.length}
                totalCount={runTotal}
              />
            </div>
          ) : null}
        </article>

      </section>

    </div>
  );
}

interface SourcesPanelProps {
  page: AdminSourcePage | null;
  metadata: AdminFilterMetadata | null;
  filters: AdminSourceFilters;
  loading: boolean;
  error: string | null;
  fixtureCount: number;
  busySourceKey: string | null;
  bulkMutation: { enabled: boolean; total: number } | null;
  onFiltersChange: (filters: AdminSourceFilters) => void;
  onOpenSource: (sourceKey: string) => void;
  onReviewLogs: (source: AdminSource) => void;
  onSetEnabled: (source: AdminSource, enabled: boolean) => void;
  onSetSelectedEnabled: (sources: AdminSource[], enabled: boolean) => Promise<boolean>;
}

function SourcesPanel({
  page,
  metadata,
  filters,
  loading,
  error,
  fixtureCount,
  busySourceKey,
  bulkMutation,
  onFiltersChange,
  onOpenSource,
  onReviewLogs,
  onSetEnabled,
  onSetSelectedEnabled,
}: SourcesPanelProps) {
  const [selectedSourceKeys, setSelectedSourceKeys] = useState<Set<string>>(() => new Set());
  const activeFilterCount = [
    filters.state !== "all",
    Boolean(filters.mode),
    Boolean(filters.publisher),
    Boolean(filters.region),
    filters.includeFixtures,
  ].filter(Boolean).length;
  const facetTotal = (facets: AdminFilterMetadata["modes"] | undefined): number | null => (
    facets ? facets.reduce((total, facet) => total + facet.count, 0) : null
  );
  const modeTotal = facetTotal(metadata?.modes);
  const publisherTotal = facetTotal(metadata?.publishers);
  const regionTotal = facetTotal(metadata?.regions);
  const allOptionLabel = (label: string, count: number | null): string => (
    count === null
      ? label
      : `${label} · ${formatNumber(count)} ${count === 1 ? "source" : "sources"}`
  );
  const shownSources = page?.items ?? [];
  const eligibleSources = selectableAdminSources(shownSources);
  const selectedSources = eligibleSources.filter((source) => selectedSourceKeys.has(source.source_key));
  const allShownSelected = eligibleSources.length > 0
    && selectedSources.length === eligibleSources.length;
  const someShownSelected = selectedSources.length > 0 && !allShownSelected;
  const selectedEnabled = selectedSources.filter((source) => source.enabled).length;
  const selectedPaused = selectedSources.length - selectedEnabled;
  const shownRetired = shownSources.filter(adminSourceIsRetired).length;
  const shownEnabled = shownSources.filter(
    (source) => !adminSourceIsRetired(source) && source.enabled,
  ).length;
  const shownPaused = shownSources.length - shownEnabled - shownRetired;
  const shownLifecycleSummary = [
    `${formatNumber(shownEnabled)} enabled`,
    `${formatNumber(shownPaused)} paused`,
    shownRetired ? `${formatNumber(shownRetired)} retired` : null,
  ].filter(Boolean).join(" · ");
  const truncated = Boolean(page && page.total > shownSources.length);
  const scopeLabel = truncated
    ? `${formatNumber(shownSources.length)} shown of ${formatNumber(page?.total ?? 0)} matching`
    : `${formatNumber(shownSources.length)} matching`;
  const selectScopeLabel = truncated
    ? `Select ${formatNumber(eligibleSources.length)} shown`
    : eligibleSources.length === shownSources.length
      ? `Select all ${formatNumber(eligibleSources.length)} matching`
      : `Select ${formatNumber(eligibleSources.length)} reviewed`;
  const updateFilters = (next: AdminSourceFilters) => {
    setSelectedSourceKeys(new Set());
    onFiltersChange(next);
  };
  const changeSort = (field: AdminSourceSort) => {
    updateFilters({
      ...filters,
      sortBy: field,
      sortDirection: filters.sortBy === field
        ? filters.sortDirection === "asc" ? "desc" : "asc"
        : SOURCE_SORT_DEFAULT_DIRECTION[field],
    });
  };
  const toggleSourceSelection = (sourceKey: string) => {
    setSelectedSourceKeys((current) => {
      const next = new Set(current);
      if (next.has(sourceKey)) next.delete(sourceKey);
      else next.add(sourceKey);
      return next;
    });
  };
  const toggleAllShown = () => {
    setSelectedSourceKeys(
      allShownSelected
        ? new Set()
        : new Set(eligibleSources.map((source) => source.source_key)),
    );
  };
  const applySelectedState = async (enabled: boolean) => {
    const targets = selectedSources.filter((source) => source.enabled !== enabled);
    if (await onSetSelectedEnabled(targets, enabled)) {
      setSelectedSourceKeys(new Set());
    }
  };
  return (
    <div className="admin-panel">
      <section className="admin-page-heading">
        <div>
          <span className="admin-eyebrow">registry / reviewed-sources</span>
          <h1>Source registry</h1>
          <p>Server-filtered admission state, current projection counts, and latest durable run evidence.</p>
        </div>
        <div className="admin-page-heading__stat">
          <strong>{page ? formatNumber(page.total) : "—"}</strong>
          <span>matching sources</span>
        </div>
      </section>

      <section className="admin-filter-surface">
        <label className="admin-search">
          <Search aria-hidden="true" />
          <span className="sr-only">Search sources</span>
          <input
            value={filters.query}
            onChange={(event) => updateFilters({ ...filters, query: event.target.value })}
            placeholder="Search source, publisher, mode, region, or URL"
          />
          {filters.query ? (
            <button type="button" onClick={() => updateFilters({ ...filters, query: "" })} aria-label="Clear search">
              <X aria-hidden="true" />
            </button>
          ) : null}
        </label>
        <div className="admin-source-state-tabs" aria-label="Source state">
          {(metadata?.source_states ?? [
            { value: "all", label: "All states" },
            { value: "active", label: "Active" },
            { value: "due", label: "Due" },
            { value: "blocked", label: "Blocked" },
            { value: "failed", label: "Failed" },
          ]).map((state) => (
            <button
              type="button"
              key={state.value}
              className={filters.state === state.value ? "is-active" : ""}
              onClick={() => updateFilters({
                ...filters,
                state: state.value as AdminSourceFilters["state"],
              })}
            >
              {state.label}
            </button>
          ))}
        </div>
        <div className="admin-filter-row">
          <div className="admin-filter-label"><Filter aria-hidden="true" /><span>Filters</span>{activeFilterCount ? <b>{activeFilterCount}</b> : null}</div>
          <label>
            <span className="sr-only">Adapter mode</span>
            <select value={filters.mode} onChange={(event) => updateFilters({ ...filters, mode: event.target.value })}>
              <option value="">{allOptionLabel("All adapters", modeTotal)}</option>
              {filters.mode && !metadata?.modes.some((mode) => mode.value === filters.mode) ? (
                <option value={filters.mode}>{humanize(filters.mode)} · 0 sources</option>
              ) : null}
              {metadata?.modes.map((mode) => <option key={mode.value} value={mode.value}>{humanize(mode.value)} · {formatNumber(mode.count)} {mode.count === 1 ? "source" : "sources"}</option>)}
            </select>
          </label>
          <label>
            <span className="sr-only">Publisher</span>
            <select value={filters.publisher} onChange={(event) => updateFilters({ ...filters, publisher: event.target.value })}>
              <option value="">{allOptionLabel("All publishers", publisherTotal)}</option>
              {filters.publisher && !metadata?.publishers.some((publisher) => publisher.value === filters.publisher) ? (
                <option value={filters.publisher}>{filters.publisher} · 0 sources</option>
              ) : null}
              {metadata?.publishers.map((publisher) => <option key={publisher.value} value={publisher.value}>{publisher.value} · {formatNumber(publisher.count)} {publisher.count === 1 ? "source" : "sources"}</option>)}
            </select>
          </label>
          <label>
            <span className="sr-only">Region</span>
            <select value={filters.region} onChange={(event) => updateFilters({ ...filters, region: event.target.value })}>
              <option value="">{allOptionLabel("All regions", regionTotal)}</option>
              {filters.region && !metadata?.regions.some((region) => region.value === filters.region) ? (
                <option value={filters.region}>{humanize(filters.region)} · 0 sources</option>
              ) : null}
              {metadata?.regions.map((region) => <option key={region.value} value={region.value}>{humanize(region.value)} · {formatNumber(region.count)} {region.count === 1 ? "source" : "sources"}</option>)}
            </select>
          </label>
          <label className="admin-checkbox">
            <input
              type="checkbox"
              checked={filters.includeFixtures}
              onChange={(event) => updateFilters({ ...filters, includeFixtures: event.target.checked })}
            />
            <span>Include {formatNumber(fixtureCount)} fixtures</span>
          </label>
          {activeFilterCount ? (
            <button className="admin-text-button" type="button" onClick={() => updateFilters({ ...DEFAULT_SOURCE_FILTERS, query: filters.query })}>
              Clear filters
            </button>
          ) : null}
        </div>
      </section>

      {error ? <div className="admin-inline-error"><CircleAlert aria-hidden="true" />{error}</div> : null}

      <section
        className={`admin-source-bulk-bar ${selectedSources.length ? "is-active" : ""}`}
        aria-label="Bulk source controls"
      >
        <label className="admin-source-select-all">
          <input
            type="checkbox"
            checked={allShownSelected}
            disabled={!eligibleSources.length || loading || Boolean(bulkMutation)}
            ref={(node) => {
              if (node) node.indeterminate = someShownSelected;
            }}
            onChange={toggleAllShown}
          />
          <span>
            <strong>
              {selectedSources.length
                ? `${formatNumber(selectedSources.length)} selected`
                : shownLifecycleSummary}
            </strong>
            <small>
              {selectedSources.length
                ? `${formatNumber(selectedEnabled)} enabled · ${formatNumber(selectedPaused)} paused`
                : scopeLabel}
            </small>
          </span>
        </label>
        <div className="admin-source-selection-tools">
          {selectedSources.length ? (
            <button type="button" onClick={() => setSelectedSourceKeys(new Set())} disabled={Boolean(bulkMutation)}>
              Clear selection
            </button>
          ) : (
            <button type="button" onClick={toggleAllShown} disabled={!eligibleSources.length || loading || Boolean(bulkMutation)}>
              {selectScopeLabel}
            </button>
          )}
          {truncated ? <span>Bulk actions affect this page only.</span> : null}
        </div>
        <div className="admin-source-bulk-actions">
          <button
            className="admin-table-action"
            type="button"
            disabled={!selectedPaused || Boolean(bulkMutation) || Boolean(busySourceKey)}
            title="Writes one audited revision per selected source; refresh remains a separate action"
            onClick={() => void applySelectedState(true)}
          >
            {bulkMutation?.enabled
              ? <LoaderCircle className="spin" aria-hidden="true" />
              : <Power aria-hidden="true" />}
            {bulkMutation?.enabled
              ? `Resuming ${formatNumber(bulkMutation.total)}`
              : `Resume ${formatNumber(selectedPaused)}`}
          </button>
          <button
            className="admin-table-action admin-table-action--pause"
            type="button"
            disabled={!selectedEnabled || Boolean(bulkMutation) || Boolean(busySourceKey)}
            title="Fences future refreshes and retains catalog events; an in-flight fetch may still finish"
            onClick={() => void applySelectedState(false)}
          >
            {bulkMutation && !bulkMutation.enabled
              ? <LoaderCircle className="spin" aria-hidden="true" />
              : <Pause aria-hidden="true" />}
            {bulkMutation && !bulkMutation.enabled
              ? `Pausing ${formatNumber(bulkMutation.total)}`
              : `Pause ${formatNumber(selectedEnabled)}`}
          </button>
        </div>
      </section>

      <section className={`admin-source-table-shell ${loading ? "is-loading" : ""}`}>
        <div className="admin-table-scroll">
          <table className="admin-table admin-table--sources">
            <thead>
              <tr>
                <th className="admin-source-select-column">
                  <span className="sr-only">Select</span>
                </th>
                <SourceSortHeader field="source" label="Source" filters={filters} onChange={changeSort} />
                <SourceSortHeader field="health" label="Health" filters={filters} onChange={changeSort} />
                <SourceSortHeader field="catalog" label="Catalog" filters={filters} onChange={changeSort} />
                <SourceSortHeader field="last_success" label="Last success" filters={filters} onChange={changeSort} />
                <SourceSortHeader field="latest_run" label="Latest run" filters={filters} onChange={changeSort} />
                <SourceSortHeader field="output" label="Output" filters={filters} onChange={changeSort} />
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && !page ? Array.from({ length: 8 }, (_, index) => (
                <tr key={index} className="admin-table-skeleton">
                  <td colSpan={8}><i /></td>
                </tr>
              )) : null}
              {page?.items.map((source) => {
                const retired = adminSourceIsRetired(source);
                const replacementKey = source.superseded_by_source_key;
                return (
                <tr key={source.source_key} className={selectedSourceKeys.has(source.source_key) ? "is-selected" : ""}>
                  <td className="admin-source-select-column">
                    <input
                      type="checkbox"
                      checked={selectedSourceKeys.has(source.source_key)}
                      disabled={retired || source.review_status !== "reviewed" || Boolean(bulkMutation)}
                      title={retired
                        ? "Retired sources cannot be changed"
                        : source.review_status === "reviewed"
                          ? "Select source"
                          : "Review this source before changing its state"}
                      aria-label={`Select ${source.display_name}`}
                      onChange={() => toggleSourceSelection(source.source_key)}
                    />
                  </td>
                  <td>
                    <div className="admin-source-name-cell">
                      <button className="admin-source-name" type="button" onClick={() => onOpenSource(source.source_key)}>
                        <strong>{source.display_name}</strong>
                        <span>{source.publisher} · <code>{source.source_key}</code></span>
                      </button>
                      {replacementKey ? (
                        <button
                          className="admin-source-replacement"
                          type="button"
                          onClick={() => onOpenSource(replacementKey)}
                        >
                          Superseded by <code>{replacementKey}</code>
                          <ChevronRight aria-hidden="true" />
                        </button>
                      ) : null}
                    </div>
                  </td>
                  <td>
                    <AdminStatus
                      status={source.effective_status}
                      label={retired
                        ? adminSourceLifecycleLabel(source)
                        : statusLabel(source.effective_status)}
                    />
                  </td>
                  <td><strong className="admin-table-number">{formatNumber(source.event_count)}</strong></td>
                  <td title={formatFullDate(source.last_succeeded_at)}>
                    <span>{formatRelativeTime(source.last_succeeded_at)}</span>
                    <small>{formatDateTime(source.last_succeeded_at)}</small>
                  </td>
                  <td>
                    {source.latest_run ? (
                      <>
                        <AdminStatus
                          status={displayedRunStatus(source.latest_run)}
                          label={runStatusLabel(source.latest_run)}
                        />
                        <small>{formatDuration(source.latest_run.duration_ms)} · {source.latest_run.attempt_count} attempt{source.latest_run.attempt_count === 1 ? "" : "s"}</small>
                      </>
                    ) : <span className="admin-muted">No run</span>}
                  </td>
                  <td>
                    <strong>{sourceOutput(source)}</strong>
                    <small>{humanize(source.mode)}</small>
                  </td>
                  <td>
                    <div className="admin-source-actions">
                      <button
                        className="admin-table-action"
                        type="button"
                        onClick={() => onReviewLogs(source)}
                        aria-label={`Review run evidence for ${source.display_name}`}
                      >
                        <FileClock aria-hidden="true" />
                        Troubleshoot
                      </button>
                      {!retired ? (
                        <button
                          className="admin-table-action"
                          type="button"
                          disabled={busySourceKey === source.source_key || Boolean(bulkMutation)}
                          title={
                            source.review_status === "reviewed"
                              ? `${source.enabled ? "Pause" : "Resume"} collection and write a new reviewed registry revision`
                              : "Open the reviewed configuration before changing collection state"
                          }
                          onClick={() => {
                            if (source.review_status !== "reviewed") {
                              onOpenSource(source.source_key);
                              return;
                            }
                            onSetEnabled(source, !source.enabled);
                          }}
                          aria-label={
                            source.review_status === "reviewed"
                              ? `${source.enabled ? "Pause" : "Resume"} collection for ${source.display_name}`
                              : `Review configuration for ${source.display_name}`
                          }
                        >
                          {busySourceKey === source.source_key
                            ? <LoaderCircle className="spin" aria-hidden="true" />
                            : <Power aria-hidden="true" />}
                          {source.review_status === "reviewed"
                            ? source.enabled ? "Pause" : "Resume"
                            : "Review"}
                        </button>
                      ) : null}
                      <button
                        className="admin-row-action"
                        type="button"
                        onClick={() => onOpenSource(source.source_key)}
                        aria-label={`Inspect ${source.display_name}`}
                      >
                        <ChevronRight aria-hidden="true" />
                      </button>
                    </div>
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {!loading && !page?.items.length ? (
          <div className="admin-empty-state">
            <ListFilter aria-hidden="true" />
            <h2>No sources match</h2>
            <p>Clear a filter or search a different publisher, mode, source key, or region.</p>
          </div>
        ) : null}
      </section>
    </div>
  );
}

function presentedRunStatus(
  run: AdminRun,
  disposition: AdminRunDisposition,
): {
  status: string;
  label?: string;
  detail: string | null;
} {
  if (isPacerDeferredRun(run)) {
    return {
      status: displayedRunStatus(run),
      label: runStatusLabel(run),
      detail: "Coordination deferred this attempt at the pacing gate",
    };
  }
  if (isFailedRun(run) && disposition === "resolved") {
    return {
      status: "resolved",
      label: "Resolved",
      detail: `${run.error ? `${humanize(run.error)} · ` : ""}newer run succeeded`,
    };
  }
  if (isFailedRun(run) && disposition === "historical") {
    return {
      status: "historical",
      label: "Historical",
      detail: `${run.error ? `${humanize(run.error)} · ` : ""}older attempt`,
    };
  }
  return {
    status: displayedRunStatus(run),
    label: runStatusLabel(run),
    detail: run.error ? humanize(run.error) : null,
  };
}

function RunEvidence({
  run,
  disposition,
  onOpenSource,
}: {
  run: AdminRun;
  disposition: AdminRunDisposition;
  onOpenSource: (sourceKey: string) => void;
}) {
  const status = presentedRunStatus(run, disposition);
  const failed = isFailedRun(run);
  const pacerDeferred = isPacerDeferredRun(run);
  const historicalFailure = failed && disposition !== "current";
  const outcome = pacerDeferred
    ? "Coordination deferred this attempt"
    : disposition === "resolved" && failed
      ? "Historical failure recovered by a newer run"
      : disposition === "historical" && failed
        ? "Historical failure retained for diagnosis"
        : failed
          ? run.error ? humanize(run.error) : "Unclassified failure"
          : run.status === "succeeded"
            ? "Run completed successfully"
            : `Run is ${humanize(run.status).toLowerCase()}`;
  const note = pacerDeferred
    ? "The pacing gate delayed this attempt before provider collection. It is a coordination outcome, not evidence of a provider or parser failure."
    : disposition === "resolved" && failed
      ? "This failed attempt is retained for diagnostics, but a newer run for this source succeeded. It is not the source's current state."
      : disposition === "historical" && failed
        ? "This is an older failed attempt, not the source's current state. No newer successful run is recorded for this attempt in the selected evidence scope."
        : "This is the operator-safe troubleshooting record. Raw provider payloads, credentials, stack traces, and lease tokens are intentionally excluded.";
  return (
    <RunExecutionEvidence
      run={run}
      outcome={outcome}
      note={note}
      status={<AdminStatus status={status.status} label={status.label} />}
      historical={historicalFailure}
      onOpenSource={onOpenSource}
    />
  );
}

interface PipelinePanelProps {
  overview: AdminOverview | null;
  page: AdminRunPage | null;
  sources: AdminSource[];
  metadata: AdminFilterMetadata | null;
  filters: AdminRunFilters;
  loading: boolean;
  error: string | null;
  onFiltersChange: (filters: AdminRunFilters) => void;
  onOpenSource: (sourceKey: string) => void;
  onOpenSources: () => void;
  onReviewFailures: () => void;
}

function PipelinePanel({
  overview,
  page,
  sources,
  metadata,
  filters,
  loading,
  error,
  onFiltersChange,
  onOpenSource,
  onOpenSources,
  onReviewFailures,
}: PipelinePanelProps) {
  const [metric, setMetric] = useState<OverviewRunMetric>("output");
  const [selectedBucketKey, setSelectedBucketKey] = useState<string | null>(null);
  const runWindow: OverviewRunWindow = filters.windowHours === 24
    || filters.windowHours === 720
    ? filters.windowHours
    : 168;
  const runWindowConfig = RUN_WINDOW_CONFIG[runWindow];
  const presentedRuns = useMemo(
    () => presentAdminRuns(page?.items ?? [], "history", ""),
    [page?.items],
  );
  const runs = useMemo(
    () => presentedRuns.map(({ run }) => run),
    [presentedRuns],
  );
  const summary = useMemo(
    () => buildAdminPipelineSummary(
      runs,
      page?.total ?? runs.length,
      sources,
      overview,
      filters.sourceKey,
    ),
    [filters.sourceKey, overview, page?.total, runs, sources],
  );
  const runBuckets = useMemo(
    () => overview?.generated_at
      ? buildRunBuckets(runs, overview.generated_at, runWindow)
      : [],
    [overview?.generated_at, runWindow, runs],
  );
  const selectedBucket = runBuckets.find((bucket) => bucket.key === selectedBucketKey)
    ?? [...runBuckets].reverse().find((bucket) => bucketTotal(bucket) > 0)
    ?? null;
  const sourceByKey = useMemo(
    () => new Map(sources.map((source) => [source.source_key, source])),
    [sources],
  );
  const sourceScope = filters.sourceKey
    ? sourceByKey.get(filters.sourceKey)?.display_name ?? humanize(filters.sourceKey)
    : "All admitted sources";

  useEffect(() => {
    if (!selectedBucket || selectedBucket.key === selectedBucketKey) return;
    setSelectedBucketKey(selectedBucket.key);
  }, [selectedBucket, selectedBucketKey]);

  return (
    <div className={`admin-panel admin-panel--pipeline ${loading ? "is-loading" : ""}`}>
      <section className="admin-page-heading admin-pipeline-heading">
        <div>
          <span className="admin-eyebrow">operations / bounded-pipeline-evidence</span>
          <h1>Collection pipeline</h1>
          <p>
            Follow admission, collection, enrichment, normalization, and publication for the
            selected run window. The retained searchable catalog is shown separately as a live snapshot.
          </p>
        </div>
        <div className="admin-page-heading__stat">
          <strong>{page ? formatNumber(page.total) : "—"}</strong>
          <span>runs in scope</span>
        </div>
      </section>

      <section className="admin-filter-surface admin-filter-surface--pipeline">
        <div className="admin-filter-row">
          <div className="admin-filter-label">
            <ListFilter aria-hidden="true" />
            <span>Pipeline slice</span>
          </div>
          <label>
            <span className="sr-only">Pipeline source</span>
            <select
              value={filters.sourceKey}
              onChange={(event) => onFiltersChange({
                ...filters,
                sourceKey: event.target.value,
              })}
            >
              <option value="">All sources</option>
              {sources.map((source) => (
                <option key={source.source_key} value={source.source_key}>
                  {source.display_name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">Pipeline run status</span>
            <select
              value={filters.status}
              onChange={(event) => onFiltersChange({
                ...filters,
                status: event.target.value as AdminRunFilters["status"],
              })}
            >
              <option value="">All statuses</option>
              {(metadata?.run_statuses ?? [
                { value: "running", label: "Running" },
                { value: "paused", label: "Paused" },
                { value: "succeeded", label: "Succeeded" },
                { value: "failed", label: "Failed" },
              ]).map((status) => (
                <option key={status.value} value={status.value}>{status.label}</option>
              ))}
            </select>
          </label>
          <div className="admin-segmented" role="group" aria-label="Pipeline run window">
            {RUN_WINDOWS.map(({ value, label }) => (
              <button
                type="button"
                key={value}
                className={runWindow === value ? "is-active" : ""}
                aria-pressed={runWindow === value}
                onClick={() => onFiltersChange({ ...filters, windowHours: value })}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </section>

      {error ? (
        <div className="admin-inline-error"><CircleAlert aria-hidden="true" />{error}</div>
      ) : null}

      {summary.partial ? (
        <div className="admin-run-partial-note admin-pipeline-partial" role="note">
          <CircleAlert aria-hidden="true" />
          <span>
            <strong>Partial loaded sample.</strong> Aggregates and charts cover the newest{" "}
            {formatNumber(summary.loadedRuns)} of {formatNumber(summary.totalRuns)} matching runs.
          </span>
        </div>
      ) : null}

      <section className="admin-pipeline-stage-surface" aria-label="Pipeline stage summary">
        <div className="admin-card-heading">
          <div>
            <span>{sourceScope}</span>
            <h2>Selected-window flow</h2>
          </div>
          <button className="admin-text-button" type="button" onClick={onOpenSources}>
            View sources <ArrowRight aria-hidden="true" />
          </button>
        </div>
        <div className="admin-pipeline-stages">
          <article data-stage="admission">
            <span>01 · Admission</span>
            <strong>
              {formatNumber(summary.admittedSources)} / {formatNumber(summary.totalSources)}
            </strong>
            <small>reviewed, enabled, and policy-admitted sources</small>
          </article>
          <article data-stage="collect">
            <span>02 · Collect</span>
            <strong>{formatNumber(summary.candidateCount)}</strong>
            <small>
              event records collected by {formatNumber(summary.outputRuns)} successful run
              {summary.outputRuns === 1 ? "" : "s"} with recorded output
            </small>
          </article>
          <article data-stage="extract">
            <span>03 · Extract + enrich</span>
            <strong>{formatNumber(summary.candidateCount)}</strong>
            <small>
              records evaluated for normalized facts and topics; reviewed source modes may
              add bounded detail evidence
            </small>
          </article>
          <article data-stage="normalize">
            <span>04 · Normalize + dedupe</span>
            <strong>{formatNumber(summary.canonicalCount)}</strong>
            <small>
              normalized and deduplicated · {formatPercent(summary.yieldRate)} publish yield
            </small>
          </article>
          <article data-stage="catalog">
            <span>05 · Catalog + publish</span>
            <strong>{formatNumber(summary.canonicalCount)}</strong>
            <small>
              records published by {formatNumber(summary.outputRuns)} recorded successful run
              {summary.outputRuns === 1 ? "" : "s"}
            </small>
          </article>
        </div>
        <aside className="admin-pipeline-catalog-snapshot" aria-label="Current catalog live snapshot">
          <div>
            <span>Live snapshot</span>
            <strong>Current catalog</strong>
            <small>
              Searchable {summary.catalogScope === "fleet" ? "deduplicated fleet" : "source"}
              {" "}events retained now—not a sum of the selected run window.
            </small>
          </div>
          <b>{formatNumber(summary.currentCatalogEvents)} <small>events</small></b>
        </aside>
      </section>

      <section className="admin-pipeline-trend">
        <div className="admin-card-heading">
          <div>
            <span>{runWindowConfig.eyebrow}</span>
            <h2>Throughput and outcomes</h2>
          </div>
          {summary.failedRuns ? (
            <button className="admin-text-button" type="button" onClick={onReviewFailures}>
              Inspect failed runs <ArrowRight aria-hidden="true" />
            </button>
          ) : null}
        </div>
        <dl className="admin-pipeline-rollup">
          <div>
            <dt>Completed success</dt>
            <dd>{formatPercent(summary.successRate)}</dd>
            <small>
              {formatNumber(summary.succeededRuns)} succeeded · {formatNumber(summary.failedRuns)} failed
            </small>
          </div>
          <div>
            <dt>Average duration</dt>
            <dd>{formatDuration(summary.averageDurationMs)}</dd>
            <small>{formatNumber(summary.durationSamples)} timed records</small>
          </div>
          <div>
            <dt>Slowest run</dt>
            <dd>{formatDuration(summary.slowestDurationMs)}</dd>
            <small>pacing deferrals excluded</small>
          </div>
          <div>
            <dt>Open coordination</dt>
            <dd>{formatNumber(summary.runningRuns + summary.pausedRuns)}</dd>
            <small>
              {formatNumber(summary.runningRuns)} running · {formatNumber(summary.pausedRuns)} deferred / paused
            </small>
          </div>
        </dl>
        <div className="admin-chart-controls admin-pipeline-chart-controls">
          <div className="admin-chart-control">
            <span>View</span>
            <div className="admin-segmented" role="group" aria-label="Pipeline chart metric">
              {RUN_METRICS.map(({ value, label }) => (
                <button
                  type="button"
                  key={value}
                  className={metric === value ? "is-active" : ""}
                  aria-pressed={metric === value}
                  onClick={() => setMetric(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {loading ? (
            <span className="admin-chart-updating">
              <LoaderCircle className="spin" aria-hidden="true" /> Updating
            </span>
          ) : null}
        </div>
        <div className="admin-run-visual" aria-busy={loading}>
          <div className="admin-chart-title">
            <span>
              {metric === "output"
                ? `Collected and published by ${runWindowConfig.intervalLabel}`
                : metric === "volume"
                  ? `Run outcomes by ${runWindowConfig.intervalLabel}`
                  : metric === "reliability"
                    ? `Completed-run success by ${runWindowConfig.intervalLabel}`
                    : metric === "yield"
                      ? `Recorded publish yield by ${runWindowConfig.intervalLabel}`
                      : `Average latency by ${runWindowConfig.intervalLabel}`}
            </span>
            <RunLegend metric={metric} />
          </div>
          {runs.length && runBuckets.length ? (
            <>
              <RunBarChart
                buckets={runBuckets}
                metric={metric}
                intervalLabel={runWindowConfig.intervalLabel}
                partial={summary.partial}
                selectedKey={selectedBucket?.key ?? null}
                onSelect={setSelectedBucketKey}
              />
              {selectedBucket ? (
                <RunBucketInspector
                  bucket={selectedBucket}
                  partial={summary.partial}
                  onOpenSource={onOpenSource}
                  onReviewFailures={onReviewFailures}
                />
              ) : null}
            </>
          ) : (
            <div className="admin-empty-state admin-empty-state--compact">
              <Workflow aria-hidden="true" />
              <p>No recorded runs match this pipeline slice.</p>
            </div>
          )}
        </div>
      </section>

    </div>
  );
}

interface RunsPanelProps {
  page: AdminRunPage | null;
  sources: AdminSource[];
  metadata: AdminFilterMetadata | null;
  filters: AdminRunFilters;
  view: AdminRunView;
  loading: boolean;
  error: string | null;
  onFiltersChange: (filters: AdminRunFilters) => void;
  onViewChange: (view: AdminRunView) => void;
  onOpenSource: (sourceKey: string) => void;
}

function RunsPanel({
  page,
  sources,
  metadata,
  filters,
  view,
  loading,
  error,
  onFiltersChange,
  onViewChange,
  onOpenSource,
}: RunsPanelProps) {
  const [expandedRunKey, setExpandedRunKey] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(0);
  const presentedRuns = useMemo(
    () => presentAdminRuns(page?.items ?? [], view, filters.status),
    [filters.status, page?.items, view],
  );
  const pageCount = Math.max(1, Math.ceil(presentedRuns.length / RUNS_PAGE_SIZE));
  const pageIndex = Math.min(currentPage, pageCount - 1);
  const pageStart = pageIndex * RUNS_PAGE_SIZE;
  const visibleRuns = useMemo(
    () => presentedRuns.slice(pageStart, pageStart + RUNS_PAGE_SIZE),
    [pageStart, presentedRuns],
  );
  const firstVisibleRun = presentedRuns.length ? pageStart + 1 : 0;
  const lastVisibleRun = Math.min(pageStart + RUNS_PAGE_SIZE, presentedRuns.length);
  const resolvedCount = presentedRuns.filter(
    ({ run, disposition }) => isFailedRun(run) && disposition === "resolved",
  ).length;
  const historicalFailureCount = presentedRuns.filter(
    ({ run, disposition }) => isFailedRun(run) && disposition === "historical",
  ).length;
  const historyFailureSummary = [
    resolvedCount ? `${formatNumber(resolvedCount)} resolved` : null,
    historicalFailureCount ? `${formatNumber(historicalFailureCount)} historical` : null,
  ].filter((value): value is string => value !== null).join(" · ");
  const changeRunView = (nextView: AdminRunView) => {
    onViewChange(nextView);
    setExpandedRunKey(null);
    setCurrentPage(0);
  };
  const changePage = (nextPage: number) => {
    setCurrentPage(Math.max(0, Math.min(nextPage, pageCount - 1)));
    setExpandedRunKey(null);
  };

  useEffect(() => {
    setCurrentPage(0);
    setExpandedRunKey(null);
  }, [
    filters.includeFixtures,
    filters.sourceKey,
    filters.status,
    filters.windowHours,
    view,
  ]);

  useEffect(() => {
    setCurrentPage((current) => Math.min(current, pageCount - 1));
  }, [pageCount]);

  return (
    <div className="admin-panel">
      <section className="admin-page-heading">
        <div>
          <span className="admin-eyebrow">operations / durable-run-evidence</span>
          <h1>Runs</h1>
          <p>
            Inspect source attempts, outcomes, timing, output, and a chronological structured execution log.
          </p>
        </div>
        <div className="admin-page-heading__stat">
          <strong>{page ? formatNumber(presentedRuns.length) : "—"}</strong>
          <span>{view === "current" ? "current source states" : "matching attempts"}</span>
        </div>
      </section>

      <section className="admin-filter-surface admin-filter-surface--runs">
        <div className="admin-run-view-bar">
          <div className="admin-run-view-bar__copy">
            <span>Evidence lens</span>
            <strong>
              {view === "current" ? "Latest state by source" : "Full attempt history"}
            </strong>
            <small>
              {view === "current"
                ? `One newest run per source; ${page ? formatNumber(page.total) : "—"} attempts remain available.`
                : historyFailureSummary
                  ? `Archived failures: ${historyFailureSummary}.`
                  : "Older failures are separated from current source state."}
            </small>
          </div>
          <div className="admin-segmented admin-run-view-toggle" role="group" aria-label="Run evidence view">
            <button
              type="button"
              className={view === "current" ? "is-active" : ""}
              aria-pressed={view === "current"}
              onClick={() => changeRunView("current")}
            >
              Current by source
            </button>
            <button
              type="button"
              className={view === "history" ? "is-active" : ""}
              aria-pressed={view === "history"}
              onClick={() => changeRunView("history")}
            >
              Full history
            </button>
          </div>
        </div>
        <div className="admin-filter-row">
          <div className="admin-filter-label"><ListFilter aria-hidden="true" /><span>Run slice</span></div>
          <label>
            <span className="sr-only">Source</span>
            <select value={filters.sourceKey} onChange={(event) => onFiltersChange({ ...filters, sourceKey: event.target.value })}>
              <option value="">All sources</option>
              {sources.map((source) => <option key={source.source_key} value={source.source_key}>{source.display_name}</option>)}
            </select>
          </label>
          <label>
            <span className="sr-only">Run status</span>
            <select value={filters.status} onChange={(event) => onFiltersChange({ ...filters, status: event.target.value as AdminRunFilters["status"] })}>
              <option value="">All statuses</option>
              {metadata?.run_statuses.map((status) => <option key={status.value} value={status.value}>{status.label}</option>)}
            </select>
          </label>
          <div className="admin-segmented" aria-label="Run window">
            {(metadata?.window_hours ?? [24, 168, 720]).map((hours) => (
              <button
                type="button"
                key={hours}
                className={filters.windowHours === hours ? "is-active" : ""}
                onClick={() => onFiltersChange({ ...filters, windowHours: hours })}
              >
                {hours === 24 ? "24h" : hours === 168 ? "7d" : "30d"}
              </button>
            ))}
          </div>
          <label className="admin-checkbox">
            <input
              type="checkbox"
              checked={filters.includeFixtures}
              onChange={(event) => onFiltersChange({ ...filters, includeFixtures: event.target.checked })}
            />
            <span>Fixtures</span>
          </label>
        </div>
      </section>

      {error ? <div className="admin-inline-error"><CircleAlert aria-hidden="true" />{error}</div> : null}

      <section className={`admin-source-table-shell ${loading ? "is-loading" : ""}`}>
        <div className="admin-table-scroll">
          <table className="admin-table admin-table--ledger admin-table--runs">
            <thead>
              <tr>
                <th>Source</th>
                <th>{view === "current" ? "Current status" : "Attempt status"}</th>
                <th>Started</th>
                <th>Output</th>
                <th>Duration</th>
              </tr>
            </thead>
            <tbody>
              {loading && !page ? Array.from({ length: 8 }, (_, index) => (
                <tr key={index} className="admin-table-skeleton"><td colSpan={5}><i /></td></tr>
              )) : null}
              {visibleRuns.map(({ run, disposition }) => {
                const rowKey = `${run.source_key}:${run.run_key}`;
                const expanded = expandedRunKey === rowKey;
                const evidenceId = `admin-run-evidence-${encodeURIComponent(rowKey).replaceAll("%", "-")}`;
                const triggerId = `admin-run-trigger-${encodeURIComponent(rowKey).replaceAll("%", "-")}`;
                const status = presentedRunStatus(run, disposition);
                const historicalFailure = isFailedRun(run) && disposition !== "current";
                const nonFailureDetail = historicalFailure || isPacerDeferredRun(run);
                return (
                  <Fragment key={rowKey}>
                    <tr
                      className={[
                        "admin-run-summary-row",
                        expanded ? "is-expanded" : "",
                        historicalFailure ? "admin-run-row--historical" : "",
                      ].filter(Boolean).join(" ")}
                    >
                      <td>
                        <button
                          className="admin-run-row__trigger"
                          id={triggerId}
                          type="button"
                          aria-expanded={expanded}
                          aria-controls={evidenceId}
                          onClick={() => setExpandedRunKey(expanded ? null : rowKey)}
                        >
                          <span className="sr-only">
                            {expanded ? "Collapse" : "Expand"} run evidence for {run.display_name ?? humanize(run.source_key)}. {status.label}.
                          </span>
                        </button>
                        <button className="admin-source-name" type="button" onClick={() => onOpenSource(run.source_key)}>
                          <strong>{run.display_name ?? humanize(run.source_key)}</strong>
                          <span><code>{run.source_key}</code></span>
                        </button>
                      </td>
                      <td>
                        <AdminStatus status={status.status} label={status.label} />
                        {status.detail ? (
                          <small className={nonFailureDetail ? "admin-run-history-note" : "admin-run-error"}>
                            {status.detail}
                          </small>
                        ) : null}
                      </td>
                      <td title={formatFullDate(run.started_at)}>
                        <span>{formatRelativeTime(run.started_at)}</span>
                        <small>{formatDateTime(run.started_at)}</small>
                      </td>
                      <td>
                        <strong>{run.candidate_count ?? "—"} <span className="admin-output-arrow">→</span> {run.canonical_count ?? "—"}</strong>
                        <small>
                          {run.candidate_count
                            ? formatPercent((run.canonical_count ?? 0) / run.candidate_count)
                            : "No candidate yield"}
                        </small>
                      </td>
                      <td>
                        <span className="admin-run-row__duration">
                          <span>{formatDuration(run.duration_ms)}</span>
                          <ChevronDown aria-hidden="true" />
                        </span>
                      </td>
                    </tr>
                    {expanded ? (
                      <tr className="admin-run-evidence-row">
                        <td colSpan={5}>
                          <div id={evidenceId} role="region" aria-labelledby={triggerId}>
                            <RunEvidence
                              run={run}
                              disposition={disposition}
                              onOpenSource={onOpenSource}
                            />
                          </div>
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        {presentedRuns.length ? (
          <nav className="admin-run-pager" aria-label="Runs pagination">
            <p aria-live="polite">
              <strong>
                {formatNumber(firstVisibleRun)}–{formatNumber(lastVisibleRun)}
              </strong>{" "}
              of {formatNumber(presentedRuns.length)} matching {view === "current" ? "states" : "attempts"}
              <span> · {formatNumber(RUNS_PAGE_SIZE)} per page</span>
            </p>
            <div>
              <button
                type="button"
                disabled={pageIndex === 0 || loading}
                onClick={() => changePage(pageIndex - 1)}
                aria-label="Previous runs page"
              >
                <ChevronLeft aria-hidden="true" />
                Previous
              </button>
              <span>Page {pageIndex + 1} of {pageCount}</span>
              <button
                type="button"
                disabled={pageIndex >= pageCount - 1 || loading}
                onClick={() => changePage(pageIndex + 1)}
                aria-label="Next runs page"
              >
                Next
                <ChevronRight aria-hidden="true" />
              </button>
            </div>
          </nav>
        ) : null}
        {!loading && !presentedRuns.length ? (
          <div className="admin-empty-state">
            <FileClock aria-hidden="true" />
            <h2>
              {view === "current"
                ? "No current source states match"
                : "No run history matches"}
            </h2>
            <p>
              {view === "current"
                ? "The status filter is applied to each source’s latest run. Try another status, source, or window."
                : "Try a wider window, another status, or all sources."}
            </p>
          </div>
        ) : null}
      </section>
    </div>
  );
}

function commandDetailElapsed(detail: AdminCommandDetail): number | null {
  const started = detail.command.started_at ?? detail.command.requested_at;
  const finished = detail.command.completed_at ?? detail.generated_at;
  const startedAt = new Date(started).getTime();
  const finishedAt = new Date(finished).getTime();
  if (!Number.isFinite(startedAt) || !Number.isFinite(finishedAt)) return null;
  return Math.max(0, finishedAt - startedAt);
}

function commandRunOutput(run: AdminCommandLinkedRun): string {
  if (run.candidate_count === null && run.canonical_count === null) return "No output yet";
  return `${run.candidate_count === null ? "—" : formatNumber(run.candidate_count)} → ${
    run.canonical_count === null ? "—" : formatNumber(run.canonical_count)
  }`;
}

function CommandLiveExecution({ detail }: { detail: AdminCommandDetail }) {
  const { command, progress } = detail;
  const activeRun = detail.runs.find((run) => run.status === "running")
    ?? detail.runs.find((run) => run.source_key === progress.active_source_key)
    ?? null;
  const orderedRuns = [...detail.runs].sort((left, right) => {
    if (left === activeRun) return -1;
    if (right === activeRun) return 1;
    return right.position - left.position;
  });
  const visibleRuns = orderedRuns.slice(0, 8);
  const elapsed = commandDetailElapsed(detail);
  const progressPercent = progress.total > 0
    ? Math.min(100, Math.round((progress.completed / progress.total) * 100))
    : null;
  const isTerminal = command.status === "completed" || command.status === "failed";
  const activeName = progress.active_display_name
    ?? activeRun?.display_name
    ?? null;
  const headline = activeName
    ? `${humanize(progress.active_phase ?? activeRun?.phase ?? "collecting")}: ${activeName}`
    : command.worker_state === "awaiting_claim"
      ? "Waiting for a worker claim"
      : command.worker_state === "retry_scheduled"
        ? "Retry is scheduled"
        : isTerminal
          ? `${formatNumber(progress.completed)} source${progress.completed === 1 ? "" : "s"} processed`
          : progress.total > 0
            ? "Preparing the next eligible source"
            : "Calculating the due-source scope";
  const detailCopy = command.action === "refresh_due"
    ? "The worker resolved reviewed sources that were due when execution began, then dispatched each eligible source through guarded collection."
    : "The worker is executing this source through guarded admission, collection, normalization, and catalog publication.";

  return (
    <section
      className="admin-command-live"
      aria-label="Authoritative command progress"
      aria-live="polite"
    >
      <header className="admin-command-live__heading">
        <div>
          <span>{isTerminal ? "Execution receipt" : "Live execution"}</span>
          <strong>{headline}</strong>
          <p>{detailCopy}</p>
        </div>
        {progressPercent !== null ? (
          <div>
            <strong>{progressPercent}%</strong>
            <span>{formatNumber(progress.completed)} / {formatNumber(progress.total)}</span>
          </div>
        ) : null}
      </header>

      {progress.total > 0 ? (
        <div
          className="admin-command-live__progress"
          role="img"
          aria-label={[
            `Succeeded: ${progress.succeeded}`,
            `Failed: ${progress.failed}`,
            `Paused: ${progress.paused}`,
            `Running: ${progress.running}`,
            `Pending: ${progress.pending}`,
          ].join(", ")}
        >
          {progress.succeeded ? <span data-state="succeeded" style={{ flexGrow: progress.succeeded }} /> : null}
          {progress.failed ? <span data-state="failed" style={{ flexGrow: progress.failed }} /> : null}
          {progress.paused ? <span data-state="paused" style={{ flexGrow: progress.paused }} /> : null}
          {progress.running ? <span data-state="running" style={{ flexGrow: progress.running }} /> : null}
          {progress.pending ? <span data-state="pending" style={{ flexGrow: progress.pending }} /> : null}
        </div>
      ) : null}

      <dl className="admin-command-live__facts">
        <div>
          <dt>Scope</dt>
          <dd>{progress.total > 0 ? `${formatNumber(progress.total)} due source${progress.total === 1 ? "" : "s"}` : "Resolving due sources"}</dd>
        </div>
        <div>
          <dt>Current source</dt>
          <dd>{activeName ?? (isTerminal ? "None active" : "Awaiting dispatch")}</dd>
        </div>
        <div>
          <dt>Stage</dt>
          <dd>{progress.active_phase ? humanize(progress.active_phase) : humanize(command.worker_state ?? command.status)}</dd>
        </div>
        <div>
          <dt>Elapsed</dt>
          <dd>{formatDuration(elapsed)}</dd>
        </div>
        <div>
          <dt>Last state change</dt>
          <dd title={formatFullDate(progress.updated_at)}>{formatRelativeTime(progress.updated_at)}</dd>
        </div>
        <div>
          <dt>Command attempt</dt>
          <dd>{formatNumber(command.attempt_count)}</dd>
        </div>
      </dl>

      {visibleRuns.length ? (
        <div className="admin-command-children">
          <div className="admin-command-children__heading">
            <strong>Linked source runs</strong>
            <span>{formatNumber(detail.runs.length)} recorded</span>
          </div>
          <ol>
            {visibleRuns.map((run) => (
              <li data-state={run.status} key={`${run.position}:${run.source_key}`}>
                <span className="admin-command-children__position">
                  {String(run.position).padStart(2, "0")}
                </span>
                <div className="admin-command-children__source">
                  <strong>{run.display_name}</strong>
                  <span>
                    {humanize(run.phase)}
                    {run.attempt_count === null ? "" : ` · attempt ${formatNumber(run.attempt_count)}`}
                  </span>
                </div>
                <div className="admin-command-children__output">
                  <strong>{commandRunOutput(run)}</strong>
                  <span>{run.duration_ms === null ? humanize(run.status) : formatDuration(run.duration_ms)}</span>
                </div>
                <AdminStatus status={run.status} />
                {run.error_code ? <small>{humanize(run.error_code)}</small> : null}
              </li>
            ))}
          </ol>
          {detail.runs.length > visibleRuns.length ? (
            <p>
              Showing the active and {formatNumber(visibleRuns.length - (activeRun ? 1 : 0))} most recent runs.
              Open Runs for the full source-run ledger.
            </p>
          ) : null}
        </div>
      ) : (
        <p className="admin-command-live__empty">
          No source run has been linked yet. The command may still be resolving its due-source scope or waiting for a worker.
        </p>
      )}
    </section>
  );
}

function CommandsPanel({
  commands,
  sources,
  sourceProjectionsReady,
  loading,
  error,
  policyAllowed,
  submitting,
  onOpenSource,
  onOpenRuns,
  onLaunch,
}: {
  commands: AdminCommand[];
  sources: AdminSource[];
  sourceProjectionsReady: boolean;
  loading: boolean;
  error: string | null;
  policyAllowed: boolean;
  submitting: boolean;
  onOpenSource: (sourceKey: string) => void;
  onOpenRuns: (command: AdminCommand) => void;
  onLaunch: (
    action: "refresh_source" | "refresh_due",
    source: AdminSource | null,
  ) => void;
}) {
  const [status, setStatus] = useState<CommandStatus | "all">("all");
  const [query, setQuery] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [draftAction, setDraftAction] = useState<"refresh_source" | "refresh_due">(
    "refresh_source",
  );
  const [draftSourceKey, setDraftSourceKey] = useState("");
  const [expandedCommandId, setExpandedCommandId] = useState<string | null>(null);
  const [commandDetails, setCommandDetails] = useState<Record<string, AdminCommandDetail>>({});
  const [commandDetailLoadingId, setCommandDetailLoadingId] = useState<string | null>(null);
  const [commandDetailErrors, setCommandDetailErrors] = useState<Record<string, string>>({});
  const [copiedCommandId, setCopiedCommandId] = useState<string | null>(null);
  const runnableSources = useMemo(
    () => sources.filter((source) => sourceIsCommandEligible(source)),
    [sources],
  );
  const selectedDraftSource = runnableSources.find(
    (source) => source.source_key === draftSourceKey,
  ) ?? null;

  useEffect(() => {
    if (selectedDraftSource || !runnableSources.length) return;
    setDraftSourceKey(runnableSources[0].source_key);
  }, [runnableSources, selectedDraftSource]);

  const visible = useMemo(() => commands.filter((command) => {
    if (status !== "all" && command.status !== status) return false;
    if (!query.trim()) return true;
    const search = query.toLowerCase();
    return command.command_id.toLowerCase().includes(search)
      || (command.source_key ?? "all due sources").toLowerCase().includes(search)
      || command.action.toLowerCase().includes(search);
  }), [commands, query, status]);

  useEffect(() => {
    if (
      expandedCommandId
      && !visible.some((command) => command.command_id === expandedCommandId)
    ) {
      setExpandedCommandId(null);
    }
  }, [expandedCommandId, visible]);
  const expandedCommandStatus = expandedCommandId
    ? commands.find((command) => command.command_id === expandedCommandId)?.status ?? null
    : null;

  useEffect(() => {
    if (!expandedCommandId) return;
    const commandId = expandedCommandId;
    let cancelled = false;
    let timer: number | null = null;
    let activeRequest: AbortController | null = null;
    let continuePolling = shouldPollAdminCommandDetail(
      commandDetails[commandId] ?? null,
      expandedCommandStatus,
    );

    const scheduleNext = () => {
      if (cancelled || !continuePolling) return;
      timer = window.setTimeout(
        () => {
          timer = null;
          void loadDetail();
        },
        ADMIN_COMMAND_DETAIL_POLL_INTERVAL_MS,
      );
    };

    const loadDetail = async () => {
      const request = new AbortController();
      activeRequest = request;
      setCommandDetailLoadingId(commandId);
      try {
        const next = await getAdminCommandDetail(commandId, request.signal);
        if (cancelled || request.signal.aborted) return;
        continuePolling = shouldPollAdminCommandDetail(
          next,
          expandedCommandStatus,
        );
        setCommandDetails((current) => ({ ...current, [commandId]: next }));
        setCommandDetailErrors((current) => {
          if (!current[commandId]) return current;
          const nextErrors = { ...current };
          delete nextErrors[commandId];
          return nextErrors;
        });
      } catch (detailError) {
        if (!cancelled && !isAdminCommandPollAbort(detailError)) {
          setCommandDetailErrors((current) => ({
            ...current,
            [commandId]: readableError(detailError),
          }));
        }
      } finally {
        if (activeRequest === request) activeRequest = null;
        if (!cancelled && !request.signal.aborted) {
          setCommandDetailLoadingId((current) => current === commandId ? null : current);
          scheduleNext();
        }
      }
    };

    void loadDetail();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      activeRequest?.abort();
    };
  }, [expandedCommandId, expandedCommandStatus]);
  const pending = commands.filter((command) => command.status === "queued" || command.status === "running").length;
  const launchDraft = () => {
    if (!policyAllowed || submitting) return;
    if (draftAction === "refresh_source") {
      if (!selectedDraftSource) return;
      onLaunch(draftAction, selectedDraftSource);
    } else {
      onLaunch(draftAction, null);
    }
    setComposerOpen(false);
  };
  const copyCommandId = async (commandId: string) => {
    try {
      await navigator.clipboard.writeText(commandId);
      setCopiedCommandId(commandId);
      window.setTimeout(() => setCopiedCommandId(null), 1_500);
    } catch {
      setCopiedCommandId(null);
    }
  };

  return (
    <div className="admin-panel">
      <section className="admin-page-heading">
        <div>
          <span className="admin-eyebrow">control-plane / durable-queue</span>
          <h1>Command queue</h1>
          <p>Launch bounded collection work, follow worker execution, and inspect safe terminal evidence.</p>
        </div>
        <div className="admin-command-heading-actions">
          <div className="admin-page-heading__stat">
            <strong>{formatNumber(pending)}</strong>
            <span>pending commands</span>
          </div>
          <button
            className="admin-primary-button"
            type="button"
            aria-expanded={composerOpen}
            aria-controls="admin-command-composer"
            onClick={() => setComposerOpen((current) => !current)}
          >
            <Plus aria-hidden="true" />
            New command
          </button>
        </div>
      </section>

      {composerOpen ? (
        <section className="admin-command-composer" id="admin-command-composer">
          <div>
            <span>Command builder</span>
            <strong>Launch guarded ingestion work</strong>
            <p>
              Commands are immutable receipts. Edit a source in its workbench, then launch a new
              bounded run here.
            </p>
          </div>
          <div className="admin-command-composer__fields">
            <label>
              <span>Operation</span>
              <select
                value={draftAction}
                onChange={(event) => setDraftAction(
                  event.target.value as "refresh_source" | "refresh_due",
                )}
              >
                <option value="refresh_source">Test one source</option>
                <option value="refresh_due">Refresh all due sources</option>
              </select>
            </label>
            <label>
              <span>Source</span>
              <select
                value={draftSourceKey}
                disabled={draftAction === "refresh_due"}
                onChange={(event) => setDraftSourceKey(event.target.value)}
              >
                {runnableSources.map((source) => (
                  <option value={source.source_key} key={source.source_key}>
                    {source.display_name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="admin-command-composer__launch">
            <span>
              {draftAction === "refresh_source"
                ? "Runs the selected adapter and applies successful output to the current catalog."
                : "Dispatches only sources whose reviewed cadence is due."}
            </span>
            <button
              className="admin-primary-button"
              type="button"
              disabled={
                submitting
                || !policyAllowed
                || (draftAction === "refresh_source" && !selectedDraftSource)
              }
              onClick={launchDraft}
            >
              {submitting
                ? <LoaderCircle className="spin" aria-hidden="true" />
                : <Play aria-hidden="true" />}
              {submitting ? "Launching…" : "Launch command"}
            </button>
          </div>
        </section>
      ) : null}

      <section className="admin-command-explainer">
        <CommandIcon aria-hidden="true" />
        <div>
          <strong>HTTP accepts work; the worker performs it later.</strong>
          <p>
            Queued commands survive restarts. Each actual source refresh still rechecks source
            review, policy, pacing, approved origins, and its durable lease.
          </p>
        </div>
        <div className="admin-command-flow" aria-hidden="true">
          <span>Accepted</span><ChevronRight /><span>Worker claim</span><ChevronRight /><span>Source run</span>
        </div>
      </section>

      <section className="admin-filter-surface admin-filter-surface--commands">
        <label className="admin-search">
          <Search aria-hidden="true" />
          <span className="sr-only">Search commands</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search command ID, source, or action" />
          {query ? <button type="button" onClick={() => setQuery("")} aria-label="Clear search"><X aria-hidden="true" /></button> : null}
        </label>
        <div
          className="admin-source-state-tabs"
          role="group"
          aria-label="Filter commands by status"
        >
          {(["all", "queued", "running", "completed", "failed"] as const).map((value) => (
            <button
              key={value}
              type="button"
              className={status === value ? "is-active" : ""}
              aria-pressed={status === value}
              onClick={() => setStatus(value)}
            >
              {humanize(value)}
            </button>
          ))}
        </div>
      </section>

      {error ? <div className="admin-inline-error"><CircleAlert aria-hidden="true" />{error}</div> : null}

      <section className="admin-command-ledger">
        {loading && !commands.length ? Array.from({ length: 5 }, (_, index) => <i className="admin-command-skeleton" key={index} />) : null}
        {visible.map((command) => {
          const expanded = expandedCommandId === command.command_id;
          const commandDetail = commandDetails[command.command_id] ?? null;
          const commandDetailLoading = commandDetailLoadingId === command.command_id;
          const commandDetailError = commandDetailErrors[command.command_id] ?? null;
          const reviewDisposition = sourceProjectionsReady
            ? commandReviewDisposition(command, sources)
            : commandNeedsReview(command) ? "current" : "none";
          const needsReview = reviewDisposition === "current";
          const containedHistorical = reviewDisposition === "contained";
          const evidence = commandEvidence(command);
          const outcome = commandOutcomePresentation(command, { containedHistorical });
          const execution = commandExecutionPresentation(command);
          const evidenceMap = new Map(evidence);
          const runKeyValue = evidenceMap.get("run_key");
          const runKey = typeof runKeyValue === "string" ? runKeyValue : null;
          const acceptedBuild = command.release_revision
            ? shortRevision(command.release_revision)
            : null;
          const workerBuild = command.executor_release_revision
            ? shortRevision(command.executor_release_revision)
            : null;
          const sameBuild = acceptedBuild && workerBuild && acceptedBuild === workerBuild;
          const source = command.source_key
            ? sources.find((item) => item.source_key === command.source_key) ?? null
            : null;
          const canOfferRelaunch = !containedHistorical
            && (command.status === "failed" || needsReview);
          const sourceEligible = command.action === "refresh_due"
            || sourceIsCommandEligible(source);
          const relaunchDisabled = submitting || !policyAllowed || !sourceEligible;
          const relaunchLabel = command.action === "refresh_due"
            ? "Refresh currently due"
            : "Launch again";
          const relaunchExplanation = !policyAllowed
            ? "New commands are blocked by the current refresh policy."
            : command.action === "refresh_source" && !sourceEligible
              ? "This source is no longer enabled with a current owner review."
              : command.action === "refresh_due"
                ? "Refreshing now creates a new command ID and recalculates which sources are currently due."
                : "Launching again creates a new command ID and uses the source’s current reviewed configuration.";
          const evidenceId = `admin-command-evidence-${command.command_id}`;
          const triggerId = `admin-command-trigger-${command.command_id}`;
          const sourceLabel = command.source_key
            ? source?.display_name ?? humanize(command.source_key)
            : "All currently due sources";
          const commandStatus = containedHistorical
            ? "Contained / historical"
            : needsReview ? "Needs review" : humanize(command.status);
          return (
            <article
              className={`${expanded ? "is-expanded" : ""} ${containedHistorical ? "is-contained" : ""}`.trim()}
              data-review-disposition={reviewDisposition}
              key={command.command_id}
            >
              <div className="admin-command-ledger__summary">
                <button
                  className="admin-command-ledger__trigger"
                  id={triggerId}
                  type="button"
                  aria-expanded={expanded}
                  aria-controls={evidenceId}
                  onClick={() => setExpandedCommandId(expanded ? null : command.command_id)}
                >
                  <span className="sr-only">
                    {expanded ? "Collapse" : "Expand"} {humanize(command.action)} command for{" "}
                    {sourceLabel}. {commandStatus}.
                  </span>
                </button>
                <div className="admin-command-ledger__status">
                  <AdminStatus
                    status={containedHistorical
                      ? "contained"
                      : needsReview ? "needs_review" : command.status}
                    label={containedHistorical
                      ? "Contained / historical"
                      : needsReview ? "Needs review" : undefined}
                  />
                  <span>{formatRelativeTime(command.requested_at)}</span>
                </div>
                <div className="admin-command-ledger__main">
                  <div>
                    <span>{humanize(command.action)}</span>
                    <strong>{sourceLabel}</strong>
                  </div>
                  <p>{commandResultSummary(command, { containedHistorical })}</p>
                </div>
                <dl className="admin-command-ledger__summary-meta">
                  <div>
                    <dt>Command</dt>
                    <dd title={command.command_id}>{command.command_id.slice(0, 8)}</dd>
                  </div>
                  <div>
                    <dt>Requested</dt>
                    <dd title={formatFullDate(command.requested_at)}>
                      {formatDateTime(command.requested_at)}
                    </dd>
                  </div>
                  <div>
                    <dt>Completed</dt>
                    <dd title={formatFullDate(command.completed_at)}>
                      {formatDateTime(command.completed_at)}
                    </dd>
                  </div>
                </dl>
                <span className="admin-command-ledger__disclosure-cue" aria-hidden="true">
                  <span>{expanded ? "Hide" : "Details"}</span>
                  <ChevronDown />
                </span>
              </div>
              {expanded ? (
                <div
                  className="admin-command-evidence"
                  id={evidenceId}
                  role="region"
                  aria-labelledby={triggerId}
                >
                  <section
                    className="admin-command-outcome"
                    data-tone={outcome.tone}
                    aria-label="Command outcome"
                  >
                    <div className="admin-command-outcome__copy">
                      <span>Outcome</span>
                      <strong>{outcome.headline}</strong>
                      <p>{outcome.detail}</p>
                    </div>
                    {outcome.progressPercent !== null ? (
                      <div className="admin-command-outcome__score">
                        <strong>{outcome.progressPercent}%</strong>
                        <span>
                          {command.action === "refresh_due"
                            ? "completed"
                            : "publish yield"}
                        </span>
                      </div>
                    ) : null}
                    {outcome.segments.length ? (
                      <div
                        className="admin-command-outcome__bar"
                        role="img"
                        aria-label={outcome.segments
                          .map((segment) => `${segment.label}: ${segment.value}`)
                          .join(", ")}
                      >
                        {outcome.segments.map((segment) => (
                          <span
                            data-tone={segment.tone}
                            key={segment.key}
                            style={{ flexGrow: segment.value }}
                            title={`${segment.label}: ${formatNumber(segment.value)}`}
                          />
                        ))}
                      </div>
                    ) : null}
                    {outcome.metrics.length ? (
                      <dl className="admin-command-outcome__metrics">
                        {outcome.metrics.map((item) => (
                          <div data-tone={item.tone} key={item.key}>
                            <dt>{item.label}</dt>
                            <dd>{item.value}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : null}
                  </section>

                  {commandDetail ? (
                    <CommandLiveExecution detail={commandDetail} />
                  ) : execution ? (
                    <section
                      className="admin-command-execution"
                      aria-label="Current command execution"
                      data-detail-state={commandDetailLoading ? "loading" : "legacy"}
                    >
                      <div className="admin-command-execution__copy">
                        <span>Current execution</span>
                        <strong>{execution.headline}</strong>
                        <p>{execution.detail}</p>
                      </div>
                      <dl>
                        {execution.facts.map((fact) => (
                          <div key={fact.key}>
                            <dt>{fact.label}</dt>
                            <dd>{fact.value}</dd>
                          </div>
                        ))}
                      </dl>
                      <p>
                        {commandDetailLoading
                          ? "Loading authoritative linked source-run progress…"
                          : "Live child progress is unavailable for this receipt. Source-level execution evidence remains available under Runs."}
                      </p>
                    </section>
                  ) : null}
                  {commandDetailError && !commandDetail ? (
                    <p className="admin-command-detail-error">
                      Live command detail is unavailable ({commandDetailError}). Showing the durable command receipt instead.
                    </p>
                  ) : null}

                  <ol className="admin-command-timeline" aria-label="Execution progress">
                    <li data-state="complete">
                      <span aria-hidden="true" />
                      <div>
                        <strong>Accepted</strong>
                        <small>{formatDateTime(command.requested_at)}</small>
                      </div>
                    </li>
                    <li data-state={command.started_at ? "complete" : "pending"}>
                      <span aria-hidden="true" />
                      <div>
                        <strong>Worker claimed</strong>
                        <small>
                          {command.started_at
                            ? formatDateTime(command.started_at)
                            : "Waiting"}
                        </small>
                      </div>
                    </li>
                    <li
                      data-state={command.completed_at
                        ? "complete"
                        : command.started_at ? "active" : "pending"}
                    >
                      <span aria-hidden="true" />
                      <div>
                        <strong>Finished</strong>
                        <small>
                          {command.completed_at
                            ? formatDateTime(command.completed_at)
                            : command.started_at ? "In progress" : "Pending"}
                        </small>
                      </div>
                    </li>
                  </ol>

                  <details className="admin-command-technical">
                    <summary>
                      <span>Technical details</span>
                      <ChevronDown aria-hidden="true" />
                    </summary>
                    <dl>
                      {sameBuild || (!acceptedBuild && workerBuild) || (acceptedBuild && !workerBuild) ? (
                        <div>
                          <dt>Build</dt>
                          <dd>{workerBuild ?? acceptedBuild}</dd>
                        </div>
                      ) : (
                        <>
                          {acceptedBuild ? (
                            <div>
                              <dt>Accepted build</dt>
                              <dd>{acceptedBuild}</dd>
                            </div>
                          ) : null}
                          {workerBuild ? (
                            <div>
                              <dt>Worker build</dt>
                              <dd>{workerBuild}</dd>
                            </div>
                          ) : null}
                        </>
                      )}
                      {command.source_revision !== null
                      || command.executor_source_revision !== null ? (
                        <div>
                          <dt>Source revision</dt>
                          <dd>
                            {command.source_revision ?? "—"} →{" "}
                            {command.executor_source_revision ?? "—"}
                          </dd>
                        </div>
                      ) : null}
                      <div>
                        <dt>Requested</dt>
                        <dd>{formatFullDate(command.requested_at)}</dd>
                      </div>
                      {command.started_at ? (
                        <div>
                          <dt>Worker claimed</dt>
                          <dd>{formatFullDate(command.started_at)}</dd>
                        </div>
                      ) : null}
                      {command.completed_at ? (
                        <div>
                          <dt>Completed</dt>
                          <dd>{formatFullDate(command.completed_at)}</dd>
                        </div>
                      ) : null}
                      {commandDetail ? (
                        <>
                          <div>
                            <dt>Worker state</dt>
                            <dd>{humanize(commandDetail.command.worker_state ?? commandDetail.command.status)}</dd>
                          </div>
                          <div>
                            <dt>Command attempts</dt>
                            <dd>{formatNumber(commandDetail.command.attempt_count)}</dd>
                          </div>
                          {commandDetail.command.requested_by ? (
                            <div>
                              <dt>Requested by</dt>
                              <dd>{commandDetail.command.requested_by}</dd>
                            </div>
                          ) : null}
                          {commandDetail.command.available_at ? (
                            <div>
                              <dt>Available at</dt>
                              <dd>{formatFullDate(commandDetail.command.available_at)}</dd>
                            </div>
                          ) : null}
                          {commandDetail.command.lease_expires_at ? (
                            <div>
                              <dt>Lease expires</dt>
                              <dd>{formatFullDate(commandDetail.command.lease_expires_at)}</dd>
                            </div>
                          ) : null}
                          <div>
                            <dt>Detail snapshot</dt>
                            <dd>{formatFullDate(commandDetail.generated_at)}</dd>
                          </div>
                        </>
                      ) : null}
                      {runKey ? (
                        <div>
                          <dt>Source run</dt>
                          <dd>{runKey}</dd>
                        </div>
                      ) : null}
                      <div>
                        <dt>Command ID</dt>
                        <dd>{command.command_id}</dd>
                      </div>
                    </dl>
                  </details>
                  {canOfferRelaunch ? (
                    <p className="admin-command-relaunch-note">{relaunchExplanation}</p>
                  ) : null}
                  <div className="admin-command-evidence__actions">
                    <div>
                      {command.started_at ? (
                        <button type="button" onClick={() => onOpenRuns(command)}>
                          <FileClock aria-hidden="true" />
                          {command.source_key ? "Open source runs" : "Open recent source runs"}
                        </button>
                      ) : null}
                      {command.source_key ? (
                        <button type="button" onClick={() => onOpenSource(command.source_key!)}>
                          Open source
                          <ChevronRight aria-hidden="true" />
                        </button>
                      ) : null}
                      {canOfferRelaunch ? (
                        <button
                          type="button"
                          disabled={relaunchDisabled}
                          title={relaunchExplanation}
                          aria-label={`${relaunchLabel}. ${relaunchExplanation}`}
                          onClick={() => onLaunch(command.action, source)}
                        >
                          <RefreshCw aria-hidden="true" />
                          {relaunchLabel}
                        </button>
                      ) : null}
                      <button
                        type="button"
                        onClick={() => void copyCommandId(command.command_id)}
                      >
                        <Copy aria-hidden="true" />
                        {copiedCommandId === command.command_id ? "Copied" : "Copy ID"}
                      </button>
                    </div>
                  </div>
                </div>
              ) : null}
            </article>
          );
        })}
        {!loading && !visible.length ? (
          <div className="admin-empty-state">
            <CommandIcon aria-hidden="true" />
            <h2>No commands match</h2>
            <p>Clear the search or choose another command state.</p>
          </div>
        ) : null}
      </section>
    </div>
  );
}

export function AdminConsole() {
  const [tab, setTab] = useState<AdminTab>("overview");
  const [urlReady, setUrlReady] = useState(false);
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [fleetSources, setFleetSources] = useState<AdminSource[]>([]);
  const [fleetSourcesReady, setFleetSourcesReady] = useState(false);
  const [fleetRuns, setFleetRuns] = useState<AdminRun[]>([]);
  const [fleetRunTotal, setFleetRunTotal] = useState(0);
  const [overviewRunWindow, setOverviewRunWindow] = useState<OverviewRunWindow>(168);
  const [metadata, setMetadata] = useState<AdminFilterMetadata | null>(null);
  const [sourceFilters, setSourceFilters] = useState<AdminSourceFilters>(DEFAULT_SOURCE_FILTERS);
  const [sourcePage, setSourcePage] = useState<AdminSourcePage | null>(null);
  const [pipelineFilters, setPipelineFilters] = useState<AdminRunFilters>(DEFAULT_RUN_FILTERS);
  const [pipelinePage, setPipelinePage] = useState<AdminRunPage | null>(null);
  const [runFilters, setRunFilters] = useState<AdminRunFilters>(DEFAULT_RUN_FILTERS);
  const [runView, setRunView] = useState<AdminRunView>("current");
  const [runPage, setRunPage] = useState<AdminRunPage | null>(null);
  const [commands, setCommands] = useState<AdminCommand[]>([]);
  const [selectedSourceKey, setSelectedSourceKey] = useState<string | null>(null);
  const [sourceDetail, setSourceDetail] = useState<AdminSourceDetail | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [fleetRunsLoading, setFleetRunsLoading] = useState(true);
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [pipelineLoading, setPipelineLoading] = useState(true);
  const [runsLoading, setRunsLoading] = useState(true);
  const [commandsLoading, setCommandsLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [pipelineError, setPipelineError] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [sourceMutatingKey, setSourceMutatingKey] = useState<string | null>(null);
  const [sourceBulkMutation, setSourceBulkMutation] = useState<{
    enabled: boolean;
    total: number;
  } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [toastTracksCommand, setToastTracksCommand] = useState(false);

  useEffect(() => {
    const location = adminHistoryLocationFromUrl(window.location.href);
    setTab(location.tab);
    setSelectedSourceKey(location.sourceKey);
    window.history.replaceState(
      window.history.state,
      "",
      adminHistoryUrl(location, window.location.href),
    );
    setUrlReady(true);
  }, []);

  useEffect(() => {
    if (!urlReady) return;
    const handlePopState = () => {
      const location = adminHistoryLocationFromUrl(window.location.href);
      setTab(location.tab);
      setSelectedSourceKey(location.sourceKey);
      setSourceDetail(null);
      setDetailError(null);
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [urlReady]);

  useEffect(() => {
    let cancelled = false;
    setOverviewLoading(true);
    void Promise.all([
      getAdminOverview(),
      getAdminSources(DEFAULT_SOURCE_FILTERS),
    ]).then(([nextOverview, sourceFleet]) => {
      if (cancelled) return;
      setOverview(nextOverview);
      setFleetSources(sourceFleet.items);
      setFleetSourcesReady(true);
      setGlobalError(null);
    }).catch((error) => {
      if (!cancelled) setGlobalError(readableError(error));
    }).finally(() => {
      if (!cancelled) {
        setOverviewLoading(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  useEffect(() => {
    let cancelled = false;
    setFleetRunsLoading(true);
    setFleetRuns([]);
    setFleetRunTotal(0);
    void getAllAdminRuns({
      ...DEFAULT_RUN_FILTERS,
      windowHours: overviewRunWindow,
    }).then((runFleet) => {
      if (cancelled) return;
      setFleetRuns(runFleet.items);
      setFleetRunTotal(runFleet.total);
    }).catch((error) => {
      if (!cancelled) setGlobalError(readableError(error));
    }).finally(() => {
      if (!cancelled) setFleetRunsLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [overviewRunWindow, reloadNonce]);

  useEffect(() => {
    if (!globalError) return;
    const timer = window.setTimeout(() => {
      setReloadNonce((current) => current + 1);
    }, 5_000);
    return () => window.clearTimeout(timer);
  }, [globalError, reloadNonce]);

  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void getAdminFilters(sourceFilters)
        .then((next) => {
          if (!cancelled) setMetadata(next);
        })
        .catch((error) => {
          if (!cancelled) setGlobalError(readableError(error));
        });
    }, sourceFilters.query ? 220 : 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [reloadNonce, sourceFilters]);

  useEffect(() => {
    let cancelled = false;
    setSourcesLoading(true);
    const timer = window.setTimeout(() => {
      void getAdminSources(sourceFilters)
        .then((next) => {
          if (cancelled) return;
          setSourcePage(next);
          setSourceError(null);
        })
        .catch((error) => {
          if (!cancelled) setSourceError(readableError(error));
        })
        .finally(() => {
          if (!cancelled) setSourcesLoading(false);
        });
    }, sourceFilters.query ? 220 : 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [reloadNonce, sourceFilters]);

  useEffect(() => {
    let cancelled = false;
    setRunsLoading(true);
    void getAllAdminRuns({
      ...runFilters,
      status: "",
    })
      .then((next) => {
        if (cancelled) return;
        setRunPage(next);
        setRunError(null);
      })
      .catch((error) => {
        if (!cancelled) setRunError(readableError(error));
      })
      .finally(() => {
        if (!cancelled) setRunsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    reloadNonce,
    runFilters.includeFixtures,
    runFilters.sourceKey,
    runFilters.windowHours,
  ]);

  useEffect(() => {
    if (tab !== "pipeline") return;
    let cancelled = false;
    setPipelineLoading(true);
    setPipelinePage(null);
    void getAllAdminRuns({
      ...pipelineFilters,
      includeFixtures: false,
    })
      .then((next) => {
        if (cancelled) return;
        setPipelinePage(next);
        setPipelineError(null);
      })
      .catch((error) => {
        if (!cancelled) setPipelineError(readableError(error));
      })
      .finally(() => {
        if (!cancelled) setPipelineLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    pipelineFilters.sourceKey,
    pipelineFilters.status,
    pipelineFilters.windowHours,
    reloadNonce,
    tab,
  ]);

  const loadCommands = useCallback(async () => {
    try {
      const next = await getAdminCommands();
      setCommands(next.items);
      setCommandError(null);
      return next.items;
    } catch (error) {
      setCommandError(readableError(error));
      return null;
    } finally {
      setCommandsLoading(false);
    }
  }, []);

  useEffect(() => {
    setCommandsLoading(true);
    void loadCommands();
  }, [loadCommands, reloadNonce]);

  useEffect(() => {
    if (!selectedSourceKey) {
      setSourceDetail(null);
      setDetailError(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    void getAdminSourceDetail(
      selectedSourceKey,
      DEFAULT_RUN_FILTERS.windowHours,
      sourceFilters.includeFixtures,
    ).then((next) => {
      if (cancelled) return;
      setSourceDetail(next);
      setDetailError(null);
    }).catch((error) => {
      if (!cancelled) setDetailError(readableError(error));
    }).finally(() => {
      if (!cancelled) setDetailLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [reloadNonce, selectedSourceKey, sourceFilters.includeFixtures]);

  const pendingCommandCount = commands.filter(
    (command) => command.status === "queued" || command.status === "running",
  ).length;
  const hasPendingCommands = pendingCommandCount > 0;
  const refreshDuePending = commands.some(
    (command) => (
      command.action === "refresh_due"
      && (command.status === "queued" || command.status === "running")
    ),
  );
  useEffect(() => {
    if (!hasPendingCommands) return;
    const timer = window.setInterval(() => {
      void loadCommands().then((next) => {
        if (next && !next.some((command) => command.status === "queued" || command.status === "running")) {
          setReloadNonce((current) => current + 1);
        }
      });
    }, 2_500);
    return () => window.clearInterval(timer);
  }, [hasPendingCommands, loadCommands]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 4_000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const selectedSource = useMemo(
    () => sourcePage?.items.find((source) => source.source_key === selectedSourceKey)
      ?? fleetSources.find((source) => source.source_key === selectedSourceKey)
      ?? null,
    [fleetSources, selectedSourceKey, sourcePage],
  );

  const navigateAdmin = useCallback((nextTab: AdminTab, sourceKey: string | null = null) => {
    const nextLocation = { tab: sourceKey ? "sources" as const : nextTab, sourceKey };
    const currentLocation = adminHistoryLocationFromUrl(window.location.href);
    if (
      currentLocation.tab !== nextLocation.tab
      || currentLocation.sourceKey !== nextLocation.sourceKey
    ) {
      window.history.pushState(
        window.history.state,
        "",
        adminHistoryUrl(nextLocation, window.location.href),
      );
    }
    setSelectedSourceKey(nextLocation.sourceKey);
    setSourceDetail(null);
    setDetailError(null);
    setTab(nextLocation.tab);
  }, []);

  const openSource = (sourceKey: string) => {
    navigateAdmin("sources", sourceKey);
  };

  const reviewSourceLogs = (source: AdminSource) => {
    setRunView("current");
    setRunFilters({
      status: source.latest_run && isFailedRun(source.latest_run) ? "failed" : "",
      sourceKey: source.source_key,
      windowHours: 720,
      includeFixtures: sourceFilters.includeFixtures,
    });
    navigateAdmin("runs");
  };

  const openSourcePipeline = (sourceKey: string) => {
    setPipelineFilters({
      ...DEFAULT_RUN_FILTERS,
      sourceKey,
    });
    navigateAdmin("pipeline");
  };

  const openSourceRuns = (sourceKey: string, failedOnly = false) => {
    setRunView("history");
    setRunFilters({
      status: failedOnly ? "failed" : "",
      sourceKey,
      windowHours: 720,
      includeFixtures: sourceFilters.includeFixtures,
    });
    navigateAdmin("runs");
  };

  const reviewFailedRuns = () => {
    setRunView("history");
    setRunFilters({
      ...DEFAULT_RUN_FILTERS,
      status: "failed",
      windowHours: overviewRunWindow,
    });
    navigateAdmin("runs");
  };

  const reviewPipelineFailures = () => {
    setRunView("history");
    setRunFilters({
      status: "failed",
      sourceKey: pipelineFilters.sourceKey,
      windowHours: pipelineFilters.windowHours,
      includeFixtures: false,
    });
    navigateAdmin("runs");
  };

  const selectTab = (nextTab: AdminTab) => {
    if (nextTab === "runs") setRunView("current");
    navigateAdmin(nextTab);
  };

  const trackLatestCommand = () => {
    navigateAdmin("commands");
    setToast(null);
    setToastTracksCommand(false);
  };

  const submitCommand = async (
    action: "refresh_source" | "refresh_due",
    source: AdminSource | null,
  ) => {
    if (submitting) return;
    if (action === "refresh_source" && !sourceIsCommandEligible(source)) {
      setToast(source && adminSourceIsRetired(source)
        ? "Retired sources cannot launch new collection commands."
        : "This source is not currently eligible to run.");
      setToastTracksCommand(false);
      return;
    }
    const duplicatePending = commands.some((command) => (
      command.action === action
      && command.source_key === (source?.source_key ?? null)
      && (command.status === "queued" || command.status === "running")
    ));
    if (duplicatePending) {
      setToast(
        action === "refresh_due"
          ? "A due-source refresh is already queued."
          : `${source?.display_name ?? "Source"} already has a refresh queued.`,
      );
      setToastTracksCommand(true);
      return;
    }
    setSubmitting(true);
    try {
      const command = await enqueueAdminCommand(
        action,
        source?.source_key ?? null,
      );
      setCommands((current) => [command, ...current.filter((item) => item.command_id !== command.command_id)]);
      setToast(
        action === "refresh_due"
          ? "Due-source refresh accepted into the durable queue."
          : `${source?.display_name ?? "Source"} refresh accepted into the durable queue.`,
      );
      setToastTracksCommand(true);
      setCommandError(null);
      setReloadNonce((current) => current + 1);
    } catch (error) {
      setCommandError(readableError(error));
      navigateAdmin("commands");
    } finally {
      setSubmitting(false);
    }
  };

  const setSourceEnabled = async (source: AdminSource, enabled: boolean) => {
    if (sourceMutatingKey || sourceBulkMutation) return;
    if (adminSourceIsRetired(source)) {
      setToast("Retired sources are immutable and cannot be resumed or paused.");
      setToastTracksCommand(false);
      return;
    }
    setSourceMutatingKey(source.source_key);
    setSourceError(null);
    try {
      const result = await setAdminSourceEnabled(
        source.source_key,
        enabled,
        sourceFilters.includeFixtures,
      );
      setToast(
        `${source.display_name} collection ${enabled ? "resumed" : "paused"} · registry revision ${result.source_revision}.`,
      );
      setToastTracksCommand(false);
      setReloadNonce((current) => current + 1);
    } catch (error) {
      const message = error instanceof ApiError && error.status === 409
        ? "This source changed before the update completed. Reloaded the latest revision; try again."
        : readableError(error);
      setSourceError(message);
      if (selectedSourceKey === source.source_key) setDetailError(message);
      setReloadNonce((current) => current + 1);
    } finally {
      setSourceMutatingKey(null);
    }
  };

  const setSelectedSourcesEnabled = async (
    sources: AdminSource[],
    enabled: boolean,
  ): Promise<boolean> => {
    if (sourceMutatingKey || sourceBulkMutation) return false;
    const targets = adminSourceEnabledTargets(sources, enabled);
    if (!targets.length) {
      setToast(`Every selected source is already ${enabled ? "enabled" : "paused"}.`);
      setToastTracksCommand(false);
      return true;
    }
    setSourceBulkMutation({ enabled, total: targets.length });
    setSourceError(null);
    try {
      const result = await setAdminSourcesEnabled(targets, enabled);
      setToast(
        `${enabled ? "Resumed" : "Paused"} ${formatNumber(result.updated)} source${result.updated === 1 ? "" : "s"}`
        + (result.unchanged ? ` · ${formatNumber(result.unchanged)} already ${enabled ? "enabled" : "paused"}` : "")
        + ".",
      );
      setToastTracksCommand(false);
      setReloadNonce((current) => current + 1);
      return true;
    } catch (error) {
      const message = error instanceof ApiError && error.status === 409
        ? "The selected fleet changed before the update completed. Nothing was changed; the latest revisions are loading."
        : readableError(error);
      setSourceError(message);
      setReloadNonce((current) => current + 1);
      return false;
    } finally {
      setSourceBulkMutation(null);
    }
  };

  return (
    <div className="admin-shell">
      <aside className="admin-sidebar">
        <div className="admin-sidebar__top">
          <a className="admin-brand" href="/admin">
            <span className="brand-symbol" aria-hidden="true"><i /><i /></span>
            <span>
              <strong>Events Concierge</strong>
              <small>Ingestion admin</small>
            </span>
          </a>
          <span className="admin-sidebar__label">Workspace</span>
          <nav className="admin-nav" aria-label="Administration navigation">
            {TABS.map(({ value, label, icon: Icon }) => (
              <button
                type="button"
                key={value}
                className={tab === value ? "is-active" : ""}
                aria-current={tab === value ? "page" : undefined}
                onClick={() => selectTab(value)}
              >
                <Icon aria-hidden="true" />
                <span>{label}</span>
                {value === "commands" && pendingCommandCount ? (
                  <b>{pendingCommandCount}</b>
                ) : null}
              </button>
            ))}
          </nav>
        </div>
        <div className="admin-sidebar__footer">
          <details className="admin-operator-menu">
            <summary>
              <span className="admin-operator-menu__avatar" aria-hidden="true">LO</span>
              <span>
                <strong>Local operator</strong>
                <small><i />local / mock</small>
              </span>
              <ChevronDown aria-hidden="true" />
            </summary>
            <div className="admin-operator-menu__panel">
              <span>Operator &amp; access</span>
              <dl>
                <div>
                  <dt>Session</dt>
                  <dd>Local mock</dd>
                </div>
                <div>
                  <dt>Refresh access</dt>
                  <dd className={overview?.policy.allowed ? "is-allowed" : "is-blocked"}>
                    {overview
                      ? overview.policy.allowed ? "Admitted" : "Blocked"
                      : overviewLoading ? "Checking" : "Unavailable"}
                  </dd>
                </div>
                <div>
                  <dt>Authentication</dt>
                  <dd>Not configured</dd>
                </div>
              </dl>
              <p title={overview?.policy.reason}>
                {overview?.policy.code ?? "Local development session"}
              </p>
            </div>
          </details>
        </div>
      </aside>

      <header className="admin-mobile-header">
        <a className="admin-brand" href="/admin">
          <span className="brand-symbol" aria-hidden="true"><i /><i /></span>
          <span>
            <strong>Events Concierge</strong>
            <small>Ingestion admin</small>
          </span>
        </a>
        <div>
          <span className="admin-local-badge"><i />local operator</span>
        </div>
      </header>

      {globalError ? (
        <div className="admin-global-error">
          <CircleAlert aria-hidden="true" />
          <span>
            <strong>Admin data is unavailable.</strong> {globalError}
            <small>Retrying automatically…</small>
          </span>
          <button type="button" onClick={() => setGlobalError(null)} aria-label="Dismiss"><X aria-hidden="true" /></button>
        </div>
      ) : null}

      <main className={`admin-main${selectedSourceKey ? " admin-main--detail" : ""}`}>
        {selectedSourceKey ? (
          <SourceDetail
            source={selectedSource}
            detail={sourceDetail}
            loading={detailLoading}
            error={detailError}
            policyAllowed={overview?.policy.allowed ?? false}
            onClose={() => {
              navigateAdmin("sources");
            }}
            onOpenPipeline={openSourcePipeline}
            onOpenRuns={openSourceRuns}
            onOpenSource={openSource}
            onRefresh={(source) => void submitCommand("refresh_source", source)}
            refreshSubmitting={submitting}
            stateChanging={sourceMutatingKey === selectedSourceKey}
            onSetEnabled={(source, enabled) => void setSourceEnabled(source, enabled)}
            modeOptions={metadata?.modes.map((mode) => mode.value) ?? []}
            onConfigurationSaved={(result) => {
              setToast(`Reviewed configuration saved at registry revision ${result.source_revision}.`);
              setToastTracksCommand(false);
              setReloadNonce((current) => current + 1);
            }}
          />
        ) : (
          <>
            {tab === "overview" ? (
              <FleetOverview
                onOpenSource={openSource}
                onOpenRuns={() => navigateAdmin("runs")}
                onRefreshDue={() => void submitCommand("refresh_due", null)}
                refreshSubmitting={submitting}
                refreshDuePending={refreshDuePending}
              />
            ) : null}
            {tab === "pipeline" ? (
              <PipelinePanel
                overview={overview}
                page={pipelinePage}
                sources={fleetSources}
                metadata={metadata}
                filters={pipelineFilters}
                loading={pipelineLoading}
                error={pipelineError}
                onFiltersChange={setPipelineFilters}
                onOpenSource={openSource}
                onOpenSources={() => navigateAdmin("sources")}
                onReviewFailures={reviewPipelineFailures}
              />
            ) : null}
            {tab === "sources" ? (
              <SourcesPanel
                page={sourcePage}
                metadata={metadata}
                filters={sourceFilters}
                loading={sourcesLoading}
                error={sourceError}
                fixtureCount={overview?.summary.fixture_sources ?? 0}
                busySourceKey={sourceMutatingKey}
                bulkMutation={sourceBulkMutation}
                onFiltersChange={setSourceFilters}
                onOpenSource={openSource}
                onReviewLogs={reviewSourceLogs}
                onSetEnabled={(source, enabled) => void setSourceEnabled(source, enabled)}
                onSetSelectedEnabled={setSelectedSourcesEnabled}
              />
            ) : null}
            {tab === "runs" ? (
              <RunsPanel
                page={runPage}
                sources={fleetSources}
                metadata={metadata}
                filters={runFilters}
                view={runView}
                loading={runsLoading}
                error={runError}
                onFiltersChange={setRunFilters}
                onViewChange={setRunView}
                onOpenSource={openSource}
              />
            ) : null}
            {tab === "commands" ? (
              <CommandsPanel
                commands={commands}
                sources={fleetSources}
                sourceProjectionsReady={fleetSourcesReady}
                loading={commandsLoading}
                error={commandError}
                policyAllowed={overview?.policy.allowed ?? false}
                submitting={submitting}
                onOpenSource={openSource}
                onOpenRuns={(command) => {
                  setRunView("history");
                  setRunFilters({
                    ...DEFAULT_RUN_FILTERS,
                    sourceKey: command.source_key ?? "",
                    windowHours: 24,
                  });
                  navigateAdmin("runs");
                  setReloadNonce((current) => current + 1);
                }}
                onLaunch={(action, source) => void submitCommand(action, source)}
              />
            ) : null}
          </>
        )}
      </main>

      <nav className="admin-mobile-nav" aria-label="Administration navigation">
        {TABS.map(({ value, label, icon: Icon }) => (
          <button
            type="button"
            key={value}
            className={tab === value ? "is-active" : ""}
            onClick={() => selectTab(value)}
          >
            <Icon aria-hidden="true" />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      {toast ? (
        <div className="admin-toast" role="status">
          <CheckCircle2 aria-hidden="true" />
          <span>{toast}</span>
          {toastTracksCommand ? (
            <button type="button" onClick={trackLatestCommand}>Track command</button>
          ) : (
            <button type="button" onClick={() => setToast(null)}>Dismiss</button>
          )}
        </div>
      ) : null}

    </div>
  );
}
