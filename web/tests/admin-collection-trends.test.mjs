import assert from "node:assert/strict";
import test from "node:test";
import {
  collectionTrendLocation, collectionTrendUrl, collectionTrendTotals, collectionBucketRuns, collectionRangeRuns, collectionRunExpansion, collectionRunExpansionUrl,
  fleetCollectionSeries, sourceCollectionSeries, collectionTrendTimestamp,
} from "../lib/admin-collection-trends.ts";

const defaults = { windowHours: 24, sourceKey: "", metric: "runs" };
const row = (at, values = {}) => ({ at, runs: 0, succeeded: 0, failed: 0, collected: 0, published: 0, ...values });
const base = { generatedAt: "2026-09-08T16:25:13.123456Z", bucketHours: 1, sourceKey: "", sourceName: "All sources", windowHours: 24 };

test("inline runs preserve exact bounds and reject incomplete or unbounded bookmarks", () => {
  const scope = { startedAfter: "2026-09-08T12:25:13.123456Z", startedBefore: "2026-09-08T16:25:13.123456Z", kind: "interval", status: "failed" };
  const href = collectionRunExpansionUrl("/admin?tab=run-stats&trend_source=bay-arts-01", scope);
  assert.deepEqual(collectionRunExpansion(href), scope);
  for (const [start, end] of [["", scope.startedBefore], [scope.startedAfter, ""], [scope.startedBefore, scope.startedAfter], ["2026-08-01T00:00:00Z", scope.startedBefore], ["2026-09-08", scope.startedBefore], ["bad", "worse"]]) {
    assert.equal(collectionRunExpansion(`/admin?${new URLSearchParams({ trend_after: start, trend_before: end })}`), null);
  }
});

test("switching or collapsing intervals clears run selection while preserving other workspaces", () => {
  const scope = { startedAfter: "2026-09-08T12:25:13.123456Z", startedBefore: "2026-09-08T16:25:13.123456Z", kind: "range", status: "" };
  const original = "/admin?tab=run-stats&registry_query=arts&store_query=music&trend_source=bay-arts-01&run_selection=old&run_page=2&run_query=stale&run_inspector=stages&run_evidence_stage=collect#details";
  const next = collectionRunExpansionUrl(original, scope);
  const params = new URL(next, "http://localhost").searchParams;
  for (const key of ["run_selection", "run_page", "run_query", "run_inspector", "run_evidence_stage"]) assert.equal(params.has(key), false);
  assert.equal(params.get("registry_query"), "arts");
  assert.equal(params.get("store_query"), "music");
  assert.equal(params.get("tab"), "run-stats");
  assert.equal(collectionRunExpansion(next).kind, "range");
  assert.equal(collectionRunExpansion(collectionRunExpansionUrl(next, null)), null);
  assert.equal(collectionRunExpansionUrl(next, null).endsWith("#details"), true);
});

test("outcome changes within one interval retain the run investigation", () => {
  const scope = { startedAfter: "2026-09-08T12:25:13.123456Z", startedBefore: "2026-09-08T16:25:13.123456Z", kind: "interval", status: "" };
  const original = collectionRunExpansionUrl("/admin?tab=run-stats", scope) + "&run_selection=exact&run_query=arts&run_inspector=execution";
  const next = new URL(collectionRunExpansionUrl(original, { ...scope, status: "failed" }), "http://localhost");
  assert.equal(next.searchParams.get("run_selection"), "exact");
  assert.equal(next.searchParams.get("run_query"), "arts");
  assert.equal(next.searchParams.get("run_inspector"), "execution");
  assert.equal(next.searchParams.get("trend_outcome"), "failed");
});

test("URL controls normalize invalid input and preserve separate investigations", () => {
  assert.deepEqual(collectionTrendLocation("/admin"), defaults);
  for (const window of ["30", "-1", "0168", "168.0", "1.68e2", "Infinity"]) assert.equal(collectionTrendLocation(`/admin?trend_window=${window}`).windowHours, 24);
  for (const source of ["", "a", "BAD", "../secret", "x".repeat(81)]) assert.equal(collectionTrendLocation(`/admin?trend_source=${encodeURIComponent(source)}`).sourceKey, "");
  assert.deepEqual(collectionTrendLocation("/admin?trend_source=bay-arts-01&trend_window=168&trend_metric=records"), { windowHours: 168, sourceKey: "bay-arts-01", metric: "records" });
  assert.equal(collectionTrendLocation("/admin?trend_metric=duration").metric, "runs");
  const original = "/admin?store_query=music&run_query=old&ops_queue=notifications&ops_record=-9223372036854775808&future=keep#timeline";
  const next = collectionTrendUrl(original, { windowHours: 720, sourceKey: "bay-arts-01", metric: "records" });
  assert.deepEqual(collectionTrendLocation(next), { windowHours: 720, sourceKey: "bay-arts-01", metric: "records" });
  assert.equal(collectionTrendUrl(next, defaults), original);
  assert.equal(collectionTrendUrl(next, collectionTrendLocation(next)), next);
  assert.deepEqual(collectionTrendLocation(original), defaults);
});

test("summaries use displayed buckets and only completed outcomes in success rate", () => {
  const result = collectionTrendTotals([
    row("2026-09-08T14:00:00Z", { runs: 9, succeeded: 2, failed: 1, collected: 80, published: 100 }),
    row("2026-09-08T15:00:00Z", { runs: 1, succeeded: 1, collected: 20, published: 10 }),
    row("2026-09-08T16:00:00Z"),
  ]);
  assert.deepEqual(result, { runs: 10, succeeded: 3, failed: 1, collected: 100, published: 110, completed: 4, completedSuccessRate: .75 });
  assert.equal(collectionTrendTotals([row("2026-09-08T16:00:00Z", { runs: 7 })]).completedSuccessRate, null);
  assert.equal(collectionTrendTotals([]).runs, 0);
});

test("fleet projection retains empty grid intervals without inventing measurements", () => {
  const data = { generated_at: base.generatedAt, window_hours: 24, bucket_hours: 1, buckets: [
    { bucket_start: "2026-09-08T16:00:00Z", runs: 0, succeeded: 0, failed: 0, collected: 0, published: 0, deferred: 0, yield_pct: null, median_duration_ms: null },
    { bucket_start: "2026-09-08T15:00:00Z", runs: 3, succeeded: 1, failed: 1, collected: 50, published: 40, deferred: 1, yield_pct: 80, median_duration_ms: 900 },
  ] };
  const result = fleetCollectionSeries(data, 24);
  assert.deepEqual(result.buckets, [row("2026-09-08T15:00:00Z", { runs: 3, succeeded: 1, failed: 1, collected: 50, published: 40 }), row("2026-09-08T16:00:00Z")]);
  assert.equal(result.sourceKey, "");
  assert.equal(result.bucketHours, 1);
});

test("source history keeps its own interval width and bucket totals", () => {
  const result = sourceCollectionSeries({ generated_at: base.generatedAt, source: { source_key: "bay-arts-01", display_name: "Bay Arts" }, window: { bucket_hours: 4 }, summary: { total_runs: 900 }, history: [
    { bucket_start: "2026-09-08T12:25:13.123456Z", total_runs: 7, succeeded_runs: 5, failed_runs: 1, candidate_count: 19, canonical_count: 16, average_duration_ms: 55 },
  ] }, 24);
  assert.equal(result.bucketHours, 4);
  assert.equal(result.sourceName, "Bay Arts");
  assert.equal(collectionTrendTotals(result.buckets).runs, 7);
  assert.equal(collectionTrendTotals(result.buckets).completedSuccessRate, 5 / 6);
  assert.equal(collectionBucketRuns(result, result.buckets[0]).startedBefore, base.generatedAt);
});

test("drilldowns use exact grid bounds, fixture exclusion, and snapshot clipping", () => {
  const buckets = [row("2026-09-07T16:00:00Z"), row("2026-09-08T16:00:00Z")];
  const series = { ...base, buckets };
  assert.deepEqual(collectionBucketRuns(series, buckets[0]), { windowHours: 24, sourceKey: "", status: "", includeFixtures: false, startedAfter: buckets[0].at, startedBefore: "2026-09-07T17:00:00.000Z", stage: undefined, stageOutcome: undefined });
  assert.equal(collectionBucketRuns(series, buckets[1]).startedBefore, base.generatedAt);
  assert.equal(collectionBucketRuns(series, row("2026-09-08T17:00:00Z")), null);
  assert.equal(collectionBucketRuns(series, row("invalid")), null);
});

test("adjacent source boundaries retain microseconds in Runs scopes", () => {
  const buckets = [row("2026-09-08T08:25:13.123456Z"), row("2026-09-08T12:25:13.123456Z")];
  const series = { ...base, bucketHours: 4, sourceKey: "bay-arts-01", buckets };
  const scope = collectionBucketRuns(series, buckets[0]);
  assert.equal(scope.sourceKey, "bay-arts-01");
  assert.equal(scope.includeFixtures, false);
  assert.equal(scope.startedAfter, buckets[0].at);
  assert.equal(scope.startedBefore, buckets[1].at);
  assert.equal(collectionTrendTimestamp(buckets[0].at), "2026-09-08 08:25 UTC");
  assert.equal(collectionTrendTimestamp("invalid"), "Unavailable");
});


test("source interval drilldowns end at history time, not its older summary timestamp", () => {
  const result = sourceCollectionSeries({
    generated_at: "2026-09-08T16:25:12.111111Z",
    source: { source_key: "bay-arts-01", display_name: "Bay Arts" },
    window: { bucket_hours: 4 },
    history: [{ bucket_start: "2026-09-08T14:25:13.987654+02:00", total_runs: 2, succeeded_runs: 2, failed_runs: 0, candidate_count: 10, canonical_count: 9 }],
  }, 24);
  assert.equal(result.generatedAt, "2026-09-08T16:25:13.987654Z");
  const scope = collectionBucketRuns(result, result.buckets[0]);
  assert.equal(scope.startedAfter, "2026-09-08T14:25:13.987654+02:00");
  assert.equal(scope.startedBefore, "2026-09-08T16:25:13.987654Z");
  assert.equal(collectionTrendTotals(result.buckets).runs, 2);
});


test("Browse runs spans the displayed grid and clipped last bucket without changing inputs", () => {
  const buckets = [row("2026-08-09T00:00:00Z"), row("2026-09-08T00:00:00Z")];
  const series = { ...base, windowHours: 720, bucketHours: 24, buckets };
  const before = structuredClone(series);
  buckets.forEach(Object.freeze); Object.freeze(buckets); Object.freeze(series);
  const scope = collectionRangeRuns(series);
  assert.equal(scope.startedAfter, collectionBucketRuns(series, buckets[0]).startedAfter);
  assert.equal(scope.startedBefore, collectionBucketRuns(series, buckets.at(-1)).startedBefore);
  assert.equal(scope.startedAfter, "2026-08-09T00:00:00Z", "include the grid-aligned first interval, before the rolling boundary");
  assert.equal(scope.startedBefore, "2026-09-08T16:25:13.123456Z");
  assert.equal(scope.sourceKey, "");
  assert.equal(scope.status, "");
  assert.equal(scope.includeFixtures, false);
  assert.equal(scope.windowHours, 720);
  assert.deepEqual(series, before);
});

test("Browse runs retains source and exact history timestamps across the whole range", () => {
  const series = sourceCollectionSeries({
    generated_at: "2026-09-08T16:25:12.111111Z",
    source: { source_key: "bay-arts-01", display_name: "Bay Arts" },
    window: { bucket_hours: 4 },
    history: [
      { bucket_start: "2026-09-08T08:25:13.987654Z", total_runs: 3, succeeded_runs: 2, failed_runs: 1, candidate_count: 20, canonical_count: 19 },
      { bucket_start: "2026-09-08T12:25:13.987654Z", total_runs: 2, succeeded_runs: 2, failed_runs: 0, candidate_count: 10, canonical_count: 9 },
    ],
  }, 24);
  const scope = collectionRangeRuns(series);
  assert.equal(scope.sourceKey, "bay-arts-01");
  assert.equal(scope.startedAfter, "2026-09-08T08:25:13.987654Z");
  assert.equal(scope.startedBefore, "2026-09-08T16:25:13.987654Z");
  assert.equal(scope.includeFixtures, false);
  assert.equal(scope.status, "");
});

test("Browse runs has no destination without valid displayed intervals", () => {
  assert.equal(collectionRangeRuns({ ...base, buckets: [] }), null);
  assert.equal(collectionRangeRuns({ ...base, buckets: [row("invalid")] }), null);
  assert.equal(collectionRangeRuns({ ...base, buckets: [row("2026-09-08T17:00:00Z")] }), null);
  const bucket = row("2026-09-08T16:00:00Z");
  const series = { ...base, buckets: [bucket] };
  assert.deepEqual(collectionRangeRuns(series), collectionBucketRuns(series, bucket));
});
