import type { AdminRun, AdminRunStageEvidence, AdminRunTimelineEntry } from "./admin-types.ts";

export const RUN_PAGE_SIZE = 100;
export const RUN_MAX_PAGE = 100_000 / RUN_PAGE_SIZE;
export const RUN_SORT_KEYS = ["source", "status", "started", "duration", "attempts", "output", "stage_duration"] as const;
export type RunSortKey = typeof RUN_SORT_KEYS[number];
export const RUN_STAGES = ["admission", "collect", "extract_enrich", "normalize_dedupe", "catalog_publish"] as const;
export type RunStageKey = typeof RUN_STAGES[number];
export const RUN_TIMELINE_FILTERS = ["all", "stages", "lifecycle"] as const;
export type RunTimelineFilter = typeof RUN_TIMELINE_FILTERS[number];
export interface RunEvidenceLocation { stage: RunStageKey; timeline: RunTimelineFilter }
export interface RunLedgerLocation {
  page: number;
  query: string;
  sort: RunSortKey;
  direction: "asc" | "desc";
}

export function runEvidenceLocationFromUrl(href: string): RunEvidenceLocation {
  const params = new URL(href).searchParams;
  return {
    stage: RUN_STAGES.find((value) => value === params.get("run_evidence_stage")) ?? "collect",
    timeline: RUN_TIMELINE_FILTERS.find((value) => value === params.get("run_timeline")) ?? "all",
  };
}

export function runEvidenceLocationUrl(location: RunEvidenceLocation, href: string): string {
  const url = new URL(href);
  for (const [key, value, defaultValue] of [["run_evidence_stage", location.stage, "collect"], ["run_timeline", location.timeline, "all"]]) {
    if (value === defaultValue) url.searchParams.delete(key);
    else url.searchParams.set(key, value);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Navigation uses the server's current ordering and never wraps into unrelated evidence. */
export function runNeighbors(runs: readonly Pick<AdminRun, "source_key" | "run_key">[], selection: string | null) {
  const index = runs.findIndex((run) => runSelectionKey(run) === selection);
  return {
    index,
    previous: index > 0 ? runSelectionKey(runs[index - 1]) : null,
    next: index >= 0 && index + 1 < runs.length ? runSelectionKey(runs[index + 1]) : null,
  };
}

export function runTimelineEntries(entries: readonly AdminRunTimelineEntry[], filter: RunTimelineFilter) {
  return entries.filter((entry) => filter === "all" || (filter === "stages" ? entry.stage !== null : entry.event_code !== "stage_observed" && entry.event_code !== "execution_observed"));
}

export function runStageDuration(stages: readonly AdminRunStageEvidence[], stage: string | undefined): number | null {
  const entry = stages.find((item) => item.stage === stage);
  return entry?.evidence_status === "measured" ? entry.duration_ms : null;
}

/** Polling is only a bounded read of durable evidence, never a worker-liveness signal. */
export function shouldFollowRun(state: { enabled: boolean; status: string | null; visible: boolean; loading: boolean; failed: boolean; authorizationDenied: boolean }): boolean {
  return state.enabled && state.status === "running" && state.visible && !state.loading && !state.failed && !state.authorizationDenied;
}

const SOURCE_KEY = /^[a-z0-9][a-z0-9-]{1,79}$/;
const RUN_KEY = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$/;

export function runSelectionKey(run: Pick<AdminRun, "source_key" | "run_key">): string {
  return `${run.source_key}|${run.run_key}`;
}

export function parseRunSelection(value: string | null): { sourceKey: string; runKey: string } | null {
  if (!value) return null;
  const parts = value.split("|");
  return parts.length === 2 && SOURCE_KEY.test(parts[0]) && RUN_KEY.test(parts[1])
    ? { sourceKey: parts[0], runKey: parts[1] } : null;
}

export function runLedgerLocationFromUrl(href: string): RunLedgerLocation {
  const params = new URL(href).searchParams;
  const rawPage = params.get("run_page") ?? "0";
  const page = Number(rawPage);
  const sort = params.get("run_sort");
  return {
    page: /^\d+$/.test(rawPage) && Number.isSafeInteger(page) ? Math.min(page, RUN_MAX_PAGE) : 0,
    query: (params.get("run_query") ?? "").slice(0, 160),
    sort: RUN_SORT_KEYS.find((key) => key === sort) ?? "started",
    direction: params.get("run_direction") === "asc" ? "asc" : "desc",
  };
}

/** Preserve server scopes, selected evidence, and other workspaces' navigation parameters. */
export function runLedgerLocationUrl(location: RunLedgerLocation, href: string): string {
  const url = new URL(href);
  const values = {
    run_page: location.page ? String(location.page) : "",
    run_query: location.query.slice(0, 160),
    run_sort: location.sort === "started" ? "" : location.sort,
    run_direction: location.direction === "desc" ? "" : location.direction,
  };
  for (const [key, value] of Object.entries(values)) {
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Counts describe loaded run observations, never unique catalog inventory or the whole window. */
export function runPageEvidence(runs: readonly Pick<AdminRun, "status" | "attempt_count" | "canonical_count">[]) {
  const measured = runs.filter((run) => run.canonical_count !== null);
  return {
    succeeded: runs.filter((run) => run.status === "succeeded").length,
    failed: runs.filter((run) => run.status === "failed").length,
    reclaimed: runs.filter((run) => run.attempt_count > 1).length,
    output: measured.length ? measured.reduce((total, run) => total + run.canonical_count!, 0) : null,
    outputMeasuredRuns: measured.length,
  };
}
