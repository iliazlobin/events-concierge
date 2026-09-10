import assert from "node:assert/strict";
import test from "node:test";
import { collectionBucketRuns, sourceCollectionSeries } from "../lib/admin-collection-trends.ts";
import { DEFAULT_SOURCE_HISTORY_WINDOW, sourceHistoryBucketHours, sourceHistoryPlot, sourceHistoryWindow, sourceHistoryWindowUrl } from "../lib/admin-source-history.ts";

const history = (at, overrides = {}) => ({ bucket_start: at, total_runs: 0, succeeded_runs: 0, failed_runs: 0, candidate_count: 0, canonical_count: 0, average_duration_ms: null, ...overrides });
const detail = (rows, bucketHours = 6) => ({ generated_at: "2026-09-08T23:59:59.100001Z", source: { source_key: "bay-arts-01", display_name: "Bay Arts" }, window: { bucket_hours: bucketHours }, history: rows });

test("longer source history defaults to 30 days and every real resolution satisfies API bounds", () => {
  assert.equal(DEFAULT_SOURCE_HISTORY_WINDOW, 720);
  assert.equal(sourceHistoryWindow("/admin?window=168"), 720);
  for (const [hours, buckets, count] of [[24, 1, 24], [168, 2, 84], [720, 6, 120], [2160, 24, 90]]) {
    assert.equal(sourceHistoryBucketHours(hours), buckets);
    assert.equal(hours / buckets, count);
    assert.equal(hours % buckets, 0);
    assert.ok(count <= 120);
    assert.equal(sourceHistoryWindow(`/admin?source_window=${hours}`), hours);
  }
  for (const invalid of ["1", "-24", "7d", "024", "720.0", "Infinity"]) assert.equal(sourceHistoryWindow(`/admin?source_window=${invalid}`), 720);
});

test("source window history leaves source selection, draft panel and other views untouched", () => {
  const original = "/admin?tab=sources&source_selection=bay-arts-01&source_inspector=configuration&registry_query=arts&run_after=2026-09-01T00%3A00%3A00Z&trend_window=168#source";
  const long = sourceHistoryWindowUrl(original, 2160);
  assert.equal(sourceHistoryWindow(long), 2160);
  assert.equal(sourceHistoryWindowUrl(long, 720), original);
  assert.equal(sourceHistoryWindowUrl(long, 2160), long);
  assert.equal(sourceHistoryWindow(original), 720, "Back restores the original default window");
});

test("line geometry uses elapsed time and retains returned quiet periods without added points", () => {
  const series = sourceCollectionSeries(detail([
    history("2026-09-08T00:00:00.123456Z", { total_runs: 2, succeeded_runs: 2, canonical_count: 10, candidate_count: 20 }),
    history("2026-09-08T06:00:00.123456Z"),
    history("2026-09-08T18:00:00.123456Z", { total_runs: 1, failed_runs: 1, canonical_count: 5, candidate_count: 10 }),
  ]), 720);
  const saved = structuredClone(series);
  const plot = sourceHistoryPlot(series, "records");
  assert.equal(plot.points.length, 3);
  assert.deepEqual(plot.points.map(point => point.x), [0, 250, 750]);
  assert.deepEqual(plot.points.map(point => point.primary), [10, 0, 5]);
  assert.equal(plot.points[1].primaryY, 190);
  assert.equal(plot.maximum, 20);
  assert.equal(plot.primaryPath, "M0.000,100.000 L250.000,190.000 L750.000,145.000");
  assert.equal(plot.secondaryPath, "M0.000,10.000 L250.000,190.000 L750.000,100.000");
  assert.deepEqual(series, saved);
});

test("run metric uses total and failed run counts, and empty history has a finite baseline", () => {
  const series = sourceCollectionSeries(detail([
    history("2026-09-08T18:00:00Z", { total_runs: 5, succeeded_runs: 3, failed_runs: 1, candidate_count: 900, canonical_count: 800 }),
  ]), 720);
  const plot = sourceHistoryPlot(series, "runs");
  assert.equal(plot.maximum, 5);
  assert.equal(plot.points[0].primary, 5);
  assert.equal(plot.points[0].secondary, 1);
  const empty = sourceHistoryPlot(sourceCollectionSeries(detail([history("2026-09-08T18:00:00Z")]), 720), "records");
  assert.equal(empty.points[0].primaryY, 190);
  assert.ok(!empty.primaryPath.includes("NaN"));
  assert.deepEqual(sourceHistoryPlot(sourceCollectionSeries(detail([]), 720), "runs").points, []);
});

test("90 day source intervals retain exact history ends rather than older summary timestamps", () => {
  const series = sourceCollectionSeries(detail([
    history("2026-09-08T00:00:00.654321Z", { total_runs: 2 }),
  ], 24), 2160);
  const scope = collectionBucketRuns(series, series.buckets[0]);
  assert.equal(scope.windowHours, 2160);
  assert.equal(scope.startedAfter, "2026-09-08T00:00:00.654321Z");
  assert.equal(scope.startedBefore, "2026-09-09T00:00:00.654321Z");
  assert.equal(scope.sourceKey, "bay-arts-01");
});
