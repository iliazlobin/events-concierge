import type {
  AdminOverview,
  AdminRun,
  AdminSource,
} from "@/lib/admin-types";
import { isPacerDeferredRun } from "./admin-presentation.ts";

export interface AdminPipelineSummary {
  admittedSources: number;
  totalSources: number;
  loadedRuns: number;
  totalRuns: number;
  partial: boolean;
  succeededRuns: number;
  failedRuns: number;
  runningRuns: number;
  pausedRuns: number;
  completedRuns: number;
  successRate: number | null;
  outputRuns: number;
  candidateCount: number;
  canonicalCount: number;
  yieldRate: number | null;
  durationSamples: number;
  averageDurationMs: number | null;
  slowestDurationMs: number | null;
  currentCatalogEvents: number;
  catalogScope: "fleet" | "source";
}

function sourceIsAdmitted(source: AdminSource): boolean {
  return source.enabled
    && source.review_status === "reviewed"
    && source.effective_status !== "policy_blocked";
}

/**
 * Summarize only bounded fields returned by the operator-safe admin projections.
 *
 * Candidate/canonical throughput is counted only for successful runs with both counts recorded.
 * The fleet catalog uses the overview's globally deduplicated count instead of summing per-source
 * projections, which may legitimately observe the same canonical event.
 */
export function buildAdminPipelineSummary(
  runs: AdminRun[],
  runTotal: number,
  sources: AdminSource[],
  overview: AdminOverview | null,
  selectedSourceKey: string,
): AdminPipelineSummary {
  const selectedSource = selectedSourceKey
    ? sources.find((source) => source.source_key === selectedSourceKey) ?? null
    : null;
  const scopedSources = selectedSourceKey
    ? selectedSource ? [selectedSource] : []
    : sources;
  const fallbackAdmitted = scopedSources.filter(sourceIsAdmitted).length;
  const totalSources = selectedSourceKey
    ? scopedSources.length
    : overview?.summary.sources ?? scopedSources.length;
  const admittedSources = selectedSourceKey
    ? fallbackAdmitted
    : overview?.summary.active_sources ?? fallbackAdmitted;
  const outputRuns = runs.filter((run) => (
    run.status === "succeeded"
    && run.candidate_count !== null
    && run.canonical_count !== null
  ));
  const candidateCount = outputRuns.reduce(
    (total, run) => total + (run.candidate_count ?? 0),
    0,
  );
  const canonicalCount = outputRuns.reduce(
    (total, run) => total + (run.canonical_count ?? 0),
    0,
  );
  const durationRuns = runs.filter((run) => (
    run.duration_ms !== null && !isPacerDeferredRun(run)
  ));
  const durationTotal = durationRuns.reduce(
    (total, run) => total + (run.duration_ms ?? 0),
    0,
  );
  const succeededRuns = runs.filter((run) => run.status === "succeeded").length;
  const failedRuns = runs.filter((run) => (
    run.status === "failed" && !isPacerDeferredRun(run)
  )).length;
  const runningRuns = runs.filter((run) => run.status === "running").length;
  const pausedRuns = runs.filter((run) => (
    run.status === "paused" || isPacerDeferredRun(run)
  )).length;
  const completedRuns = succeededRuns + failedRuns;

  return {
    admittedSources,
    totalSources,
    loadedRuns: runs.length,
    totalRuns: runTotal,
    partial: runs.length < runTotal,
    succeededRuns,
    failedRuns,
    runningRuns,
    pausedRuns,
    completedRuns,
    successRate: completedRuns ? succeededRuns / completedRuns : null,
    outputRuns: outputRuns.length,
    candidateCount,
    canonicalCount,
    yieldRate: candidateCount ? canonicalCount / candidateCount : null,
    durationSamples: durationRuns.length,
    averageDurationMs: durationRuns.length
      ? Math.round(durationTotal / durationRuns.length)
      : null,
    slowestDurationMs: durationRuns.length
      ? Math.max(...durationRuns.map((run) => run.duration_ms ?? 0))
      : null,
    currentCatalogEvents: selectedSource
      ? selectedSource.event_count
      : overview?.summary.catalog_events
        ?? scopedSources.reduce((total, source) => total + source.event_count, 0),
    catalogScope: selectedSourceKey ? "source" : "fleet",
  };
}
