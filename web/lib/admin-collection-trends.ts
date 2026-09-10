import type { AdminRunFilters, AdminSourceDetail, AdminThroughput } from "./admin-types.ts";
import { pipelineBucketScope } from "./admin-pipeline-workspace.ts";

export type CollectionTrendWindow = 24 | 168 | 720;
export type CollectionTrendMetric = "runs" | "records";
export interface CollectionTrendLocation {
  windowHours: CollectionTrendWindow;
  sourceKey: string;
  metric: CollectionTrendMetric;
}
export interface CollectionRunExpansion {
  startedAfter: string;
  startedBefore: string;
  kind: "interval" | "range";
  status: AdminRunFilters["status"];
}
const RUN_EXPANSION_KEYS = ["run_selection", "run_inspector", "run_page", "run_query", "run_sort", "run_direction", "run_evidence_stage", "run_timeline"];
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

/** An expanded row keeps its exact interval when the rolling chart is refreshed. */
export function collectionRunExpansion(href: string): CollectionRunExpansion | null {
  const params = new URL(href, "http://localhost").searchParams;
  const startedAfter = params.get("trend_after") ?? "";
  const startedBefore = params.get("trend_before") ?? "";
  const width = Date.parse(startedBefore) - Date.parse(startedAfter);
  if (!INSTANT.test(startedAfter) || !INSTANT.test(startedBefore) || !Number.isFinite(width) || width <= 0 || width > 31 * 86_400_000) return null;
  const status = params.get("trend_outcome") ?? "";
  return { startedAfter, startedBefore, kind: params.get("trend_scope") === "range" ? "range" : "interval",
    status: status === "succeeded" || status === "failed" || status === "running" || status === "paused" ? status : "" };
}

export function collectionRunExpansionUrl(href: string, next: CollectionRunExpansion | null): string {
  const url = new URL(href, "http://localhost");
  const previous = collectionRunExpansion(href);
  if (!next || !previous || previous.startedAfter !== next.startedAfter || previous.startedBefore !== next.startedBefore || previous.kind !== next.kind) {
    for (const key of RUN_EXPANSION_KEYS) url.searchParams.delete(key);
  }
  for (const key of ["trend_after", "trend_before", "trend_scope", "trend_outcome"]) url.searchParams.delete(key);
  if (next) {
    url.searchParams.set("trend_after", next.startedAfter);
    url.searchParams.set("trend_before", next.startedBefore);
    if (next.kind === "range") url.searchParams.set("trend_scope", "range");
    if (next.status) url.searchParams.set("trend_outcome", next.status);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
export interface CollectionTrendBucket {
  at: string;
  runs: number;
  succeeded: number;
  failed: number;
  collected: number;
  published: number;
}
export interface CollectionTrendSeries {
  generatedAt: string;
  bucketHours: number;
  sourceKey: string;
  sourceName: string;
  windowHours: number;
  buckets: CollectionTrendBucket[];
}
const SOURCE_KEY = /^[a-z0-9][a-z0-9-]{1,79}$/;
function normalize(value: CollectionTrendLocation): CollectionTrendLocation {
  return {
    windowHours: value.windowHours === 168 || value.windowHours === 720 ? value.windowHours : 24,
    sourceKey: SOURCE_KEY.test(value.sourceKey) ? value.sourceKey : "",
    metric: value.metric === "records" ? "records" : "runs",
  };
}
export function collectionTrendLocation(href: string): CollectionTrendLocation {
  const params = new URL(href, "http://localhost").searchParams;
  return normalize({
    windowHours: params.get("trend_window") === "168" ? 168 : params.get("trend_window") === "720" ? 720 : 24,
    sourceKey: params.get("trend_source") ?? "",
    metric: params.get("trend_metric") as CollectionTrendMetric,
  });
}
export function collectionTrendUrl(href: string, value: CollectionTrendLocation): string {
  const url = new URL(href, "http://localhost");
  const next = normalize(value);
  const fields = {
    trend_window: next.windowHours === 24 ? "" : String(next.windowHours),
    trend_source: next.sourceKey,
    trend_metric: next.metric === "runs" ? "" : next.metric,
  };
  for (const [key, value] of Object.entries(fields)) {
    if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
export function fleetCollectionSeries(data: AdminThroughput, windowHours: CollectionTrendWindow): CollectionTrendSeries {
  return {
    generatedAt: data.generated_at, bucketHours: data.bucket_hours, sourceKey: "", sourceName: "All sources", windowHours,
    buckets: data.buckets.map(bucket => ({ at: bucket.bucket_start, runs: bucket.runs, succeeded: bucket.succeeded, failed: bucket.failed, collected: bucket.collected, published: bucket.published })).sort((a, b) => Date.parse(a.at) - Date.parse(b.at)),
  };
}
/** Adding whole hours preserves the fractional second returned by PostgreSQL. */
function intervalEnd(at: string, hours: number): string {
  const date = new Date(Date.parse(at) + hours * 3_600_000);
  if (!Number.isFinite(date.getTime())) return at;
  const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/i.exec(at)?.[1];
  return fraction ? date.toISOString().replace(/\.\d{3}Z$/, `.${fraction}Z`) : date.toISOString();
}
export function sourceCollectionSeries(data: AdminSourceDetail, windowHours: number): CollectionTrendSeries {
  const history = [...data.history].sort((a, b) => Date.parse(a.bucket_start) - Date.parse(b.bucket_start));
  const last = history.at(-1);
  // Source history has its own statement timestamp, after the independent summary read.
  // Its final full interval ends at that timestamp; the older summary must not clip it.
  const end = last ? intervalEnd(last.bucket_start, data.window.bucket_hours) : data.generated_at;
  return {
    generatedAt: end, bucketHours: data.window.bucket_hours, sourceKey: data.source.source_key, sourceName: data.source.display_name, windowHours,
    buckets: history.map(bucket => ({ at: bucket.bucket_start, runs: bucket.total_runs, succeeded: bucket.succeeded_runs, failed: bucket.failed_runs, collected: bucket.candidate_count, published: bucket.canonical_count })),
  };
}
/** Totals describe exactly the displayed buckets, including the grid-aligned first interval. */
export function collectionTrendTotals(buckets: CollectionTrendBucket[]) {
  const total = buckets.reduce((sum, row) => ({
    runs: sum.runs + row.runs, succeeded: sum.succeeded + row.succeeded, failed: sum.failed + row.failed,
    collected: sum.collected + row.collected, published: sum.published + row.published,
  }), { runs: 0, succeeded: 0, failed: 0, collected: 0, published: 0 });
  const completed = total.succeeded + total.failed;
  return { ...total, completed, completedSuccessRate: completed ? total.succeeded / completed : null };
}
export function collectionBucketRuns(series: CollectionTrendSeries, bucket: CollectionTrendBucket): AdminRunFilters | null {
  const next = pipelineBucketScope({ windowHours: series.windowHours, sourceKey: series.sourceKey, status: "", includeFixtures: false }, bucket.at, series.bucketHours, series.generatedAt, Boolean(series.sourceKey));
  if (!next) return null;
  const following = series.buckets[series.buckets.indexOf(bucket) + 1];
  const nominalEnd = Date.parse(bucket.at) + series.bucketHours * 3_600_000;
  // Preserve exact server timestamps (including PostgreSQL microseconds) at known boundaries.
  const end = Date.parse(series.generatedAt) <= nominalEnd ? series.generatedAt
    : following && Date.parse(following.at) === nominalEnd ? following.at : next.startedBefore;
  return { ...next, startedAfter: bucket.at, startedBefore: end };
}
/** Browse the same complete interval shown by the chart, not a newly rolling window. */
export function collectionRangeRuns(series: CollectionTrendSeries): AdminRunFilters | null {
  const first = series.buckets[0];
  const last = series.buckets.at(-1);
  if (!first || !last) return null;
  const start = collectionBucketRuns(series, first);
  const end = collectionBucketRuns(series, last);
  if (!start?.startedAfter || !end?.startedBefore || Date.parse(end.startedBefore) <= Date.parse(start.startedAfter)) return null;
  return { ...start, startedBefore: end.startedBefore };
}
export function collectionTrendTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? `${date.toISOString().slice(0, 16).replace("T", " ")} UTC` : "Unavailable";
}
