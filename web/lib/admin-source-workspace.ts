import type { AdminSource, RunStatus } from "@/lib/admin-types";
import { displayedRunStatus } from "./admin-presentation.ts";
import { adminSourceIsRetired } from "./admin-source-lifecycle.ts";

export type SourceRegistryHealthLens =
  | "healthy" | "due" | "blocked" | "failed" | "paused" | "retired"
  | "running" | "deferred" | "no_runs";
export type SourceRegistryLens = "all" | SourceRegistryHealthLens
  | "latest_succeeded" | "latest_failed" | "latest_deferred" | "latest_running" | "latest_no_runs"
  | `error:${string}`;

const LABELS: Record<Exclude<SourceRegistryLens, `error:${string}`>, string> = {
  all: "All sources", healthy: "Healthy", due: "Due", blocked: "Blocked", failed: "Failed",
  paused: "Paused", retired: "Retired", running: "Running", deferred: "Deferred", no_runs: "No runs",
  latest_succeeded: "Latest succeeded", latest_failed: "Latest failed", latest_deferred: "Latest deferred",
  latest_running: "Latest running", latest_no_runs: "No recorded run",
};
const HEALTH_ORDER: SourceRegistryHealthLens[] = ["healthy", "due", "running", "failed", "blocked", "deferred", "no_runs", "paused", "retired"];
const OUTCOME_ORDER: SourceRegistryLens[] = ["latest_succeeded", "latest_failed", "latest_running", "latest_deferred", "latest_no_runs"];
const OUTCOME_LENS: Record<RunStatus, SourceRegistryLens> = {
  succeeded: "latest_succeeded", failed: "latest_failed", running: "latest_running", paused: "latest_deferred",
};
const SAFE_ERROR = /^[a-z0-9][a-z0-9._:-]{0,127}$/;
const MAX_RANKED_SOURCES = 5;
const MAX_ERROR_GROUPS = 4;

export function parseSourceRegistryLens(value: string | null): SourceRegistryLens {
  if (value !== null && Object.hasOwn(LABELS, value)) return value as SourceRegistryLens;
  if (value?.startsWith("error:") && SAFE_ERROR.test(value.slice(6))) return value as SourceRegistryLens;
  return "all";
}

export function sourceRegistryLensLabel(lens: SourceRegistryLens): string {
  return lens.startsWith("error:")
    ? `Latest error: ${lens.slice(6).replaceAll("_", " ")}`
    : LABELS[lens as keyof typeof LABELS];
}

/** Exclusive current-state lens; a historical failed run cannot turn a retired source red. */
export function sourceRegistryHealth(source: AdminSource): SourceRegistryHealthLens {
  if (adminSourceIsRetired(source)) return "retired";
  if (!source.enabled || source.effective_status === "disabled") return "paused";
  if (source.review_status !== "reviewed" || ["unreviewed", "review_expired", "policy_blocked"].includes(source.effective_status)) return "blocked";
  const latest = source.latest_run ? displayedRunStatus(source.latest_run) : null;
  if (source.effective_status === "running" || latest === "running") return "running";
  if (latest === "failed") return "failed";
  if (source.due || source.effective_status === "due") return "due";
  if (latest === "paused") return "deferred";
  if (latest === null) return "no_runs";
  return "healthy";
}

function latestLens(source: AdminSource): SourceRegistryLens {
  return source.latest_run ? OUTCOME_LENS[displayedRunStatus(source.latest_run)] : "latest_no_runs";
}

function latestError(source: AdminSource): string | null {
  if (!source.latest_run || displayedRunStatus(source.latest_run) !== "failed") return null;
  const code = source.latest_run.error;
  return code && SAFE_ERROR.test(code) ? code : "not_recorded";
}

export function filterSourceRegistry(sources: readonly AdminSource[], lens: SourceRegistryLens): AdminSource[] {
  if (lens === "all") return [...sources];
  if (lens.startsWith("error:")) return sources.filter((source) => latestError(source) === lens.slice(6));
  if (lens.startsWith("latest_")) return sources.filter((source) => latestLens(source) === lens);
  return sources.filter((source) => sourceRegistryHealth(source) === lens);
}

export interface SourceRegistryBucket {
  lens: SourceRegistryLens;
  label: string;
  count: number;
}
export interface SourceRegistryVolume {
  sourceKey: string;
  label: string;
  published: number;
  outcome: RunStatus;
}
export interface SourceRegistrySummaryData {
  count: number;
  health: SourceRegistryBucket[];
  outcomes: SourceRegistryBucket[];
  errors: SourceRegistryBucket[];
  errorGroups: number;
  volume: SourceRegistryVolume[];
  latestPublishedTotal: number | null;
  measuredOutputSources: number;
  zeroOutputSources: number;
  unknownOutputSources: number;
  noRunSources: number;
}

/** Latest snapshots only: no rates, historical throughput, or inferred missing output. */
export function summarizeSourceRegistry(sources: readonly AdminSource[]): SourceRegistrySummaryData {
  const bucket = (lens: SourceRegistryLens): SourceRegistryBucket => ({
    lens, label: sourceRegistryLensLabel(lens), count: filterSourceRegistry(sources, lens).length,
  });
  const output: SourceRegistryVolume[] = [];
  const errors = new Map<string, number>();
  let unknownOutputSources = 0;
  let noRunSources = 0;
  for (const source of sources) {
    const latest = source.latest_run;
    if (!latest) { noRunSources += 1; continue; }
    const count = latest.canonical_count;
    if (count === null || !Number.isFinite(count) || count < 0) unknownOutputSources += 1;
    else output.push({ sourceKey: source.source_key, label: source.display_name, published: count, outcome: displayedRunStatus(latest) });
    const error = latestError(source);
    if (error !== null) errors.set(error, (errors.get(error) ?? 0) + 1);
  }
  const errorBuckets = [...errors].map(([code, count]): SourceRegistryBucket => ({
    lens: `error:${code}`, label: sourceRegistryLensLabel(`error:${code}`), count,
  })).sort((left, right) => right.count - left.count || left.lens.localeCompare(right.lens));
  return {
    count: sources.length,
    health: HEALTH_ORDER.map(bucket), outcomes: OUTCOME_ORDER.map(bucket),
    errors: errorBuckets.slice(0, MAX_ERROR_GROUPS), errorGroups: errorBuckets.length,
    volume: [...output].sort((left, right) => right.published - left.published || left.sourceKey.localeCompare(right.sourceKey)).slice(0, MAX_RANKED_SOURCES),
    latestPublishedTotal: output.length ? output.reduce((sum, source) => sum + source.published, 0) : null,
    measuredOutputSources: output.length,
    zeroOutputSources: output.filter((source) => source.published === 0).length,
    unknownOutputSources, noRunSources,
  };
}
