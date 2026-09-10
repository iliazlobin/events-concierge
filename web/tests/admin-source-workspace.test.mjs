import assert from "node:assert/strict";
import test from "node:test";
import {
  filterSourceRegistry, parseSourceRegistryLens, sourceRegistryHealth,
  sourceRegistryLensLabel, summarizeSourceRegistry,
} from "../lib/admin-source-workspace.ts";

function source(key, overrides = {}) {
  return {
    source_key: key, display_name: key, publisher: "Publisher", region: "Region", mode: "public_jsonld",
    source_revision: 1, seed_url: "https://example.org/events", enabled: true, effective_status: "active",
    due: false, review_status: "reviewed", retired_at: null, retired_reason: null, superseded_by_source_key: null,
    event_count: 999, last_succeeded_at: "2026-09-08T00:00:00Z", next_due_at: null,
    latest_run: { status: "succeeded", error: null, canonical_count: 10 }, ...overrides,
  };
}
const failed = { status: "failed", error: "source_timeout", canonical_count: null };

test("current health is an exclusive partition with lifecycle and review precedence", () => {
  const sources = [
    source("healthy"), source("due", { due: true }), source("failed", { latest_run: failed }),
    source("blocked", { review_status: "expired", latest_run: failed }),
    source("paused", { enabled: false, latest_run: failed }),
    source("retired", { retired_at: "2026-01-01T00:00:00Z", latest_run: failed }),
    source("running", { effective_status: "running", latest_run: { status: "running", error: null, canonical_count: null } }),
    source("deferred", { latest_run: { status: "paused", error: null, canonical_count: null } }),
    source("no_runs", { latest_run: null }),
  ];
  assert.deepEqual(sources.map(sourceRegistryHealth), sources.map((item) => item.source_key));
  const summary = summarizeSourceRegistry(sources);
  assert.equal(summary.health.reduce((sum, bucket) => sum + bucket.count, 0), sources.length);
  for (const bucket of summary.health) {
    assert.equal(filterSourceRegistry(sources, bucket.lens).length, bucket.count);
  }
  assert.equal(filterSourceRegistry(sources, "failed").length, 1);
  assert.equal(filterSourceRegistry(sources, "latest_failed").length, 4);
  assert.equal(filterSourceRegistry(sources, "error:source_timeout").length, 4);
});

test("legacy pacer deferral remains deferred rather than a latest failed error", () => {
  const sources = [source("paced", { latest_run: { status: "failed", error: "pacer_deferred", canonical_count: null } })];
  assert.equal(sourceRegistryHealth(sources[0]), "deferred");
  assert.equal(filterSourceRegistry(sources, "latest_deferred").length, 1);
  assert.equal(filterSourceRegistry(sources, "latest_failed").length, 0);
  assert.deepEqual(summarizeSourceRegistry(sources).errors, []);
});

test("latest measured zero, unknown output and absent run stay distinct from catalog size", () => {
  const sources = [
    source("zero", { latest_run: { status: "succeeded", error: null, canonical_count: 0 } }),
    source("two", { latest_run: { status: "succeeded", error: null, canonical_count: 2 } }),
    source("unknown", { latest_run: { status: "running", error: null, canonical_count: null } }),
    source("absent", { latest_run: null }),
  ];
  const summary = summarizeSourceRegistry(sources);
  assert.equal(summary.latestPublishedTotal, 2);
  assert.equal(summary.measuredOutputSources, 2);
  assert.equal(summary.zeroOutputSources, 1);
  assert.equal(summary.unknownOutputSources, 1);
  assert.equal(summary.noRunSources, 1);
  assert.deepEqual(summary.volume.map((item) => [item.sourceKey, item.published]), [["two", 2], ["zero", 0]]);
  assert.equal(summarizeSourceRegistry([sources[2], sources[3]]).latestPublishedTotal, null);
  assert.equal(summarizeSourceRegistry([sources[0]]).latestPublishedTotal, 0);
  assert.equal(summary.outcomes.reduce((sum, bucket) => sum + bucket.count, 0), sources.length);
});

test("volume and error ranks are bounded and deterministic without mutating the source list", () => {
  const sources = Array.from({ length: 8 }, (_, index) => source(`source-${index}`, {
    latest_run: { status: "failed", error: `source_error_${index}`, canonical_count: index },
  }));
  const before = sources.map((item) => item.source_key);
  const summary = summarizeSourceRegistry(sources);
  assert.equal(summary.volume.length, 5);
  assert.deepEqual(summary.volume.map((item) => item.published), [7, 6, 5, 4, 3]);
  assert.equal(summary.errors.length, 4);
  assert.equal(summary.errorGroups, 8);
  assert.deepEqual(sources.map((item) => item.source_key), before);
  for (const bucket of summary.errors) assert.equal(filterSourceRegistry(sources, bucket.lens).length, bucket.count);
});

test("URL lenses accept the fixed vocabulary and bounded safe error codes only", () => {
  for (const lens of ["all", "healthy", "paused", "latest_no_runs", "error:source_timeout"]) {
    assert.equal(parseSourceRegistryLens(lens), lens);
  }
  for (const invalid of [null, "", "toString", "constructor", "latest_arbitrary", "error:", "error:bad\ncode", `error:${"x".repeat(129)}`]) {
    assert.equal(parseSourceRegistryLens(invalid), "all");
  }
  assert.equal(sourceRegistryLensLabel("latest_failed"), "Latest failed");
  assert.equal(sourceRegistryLensLabel("error:source_timeout"), "Latest error: source timeout");
});

test("failed run without an error code is explicit and can be filtered", () => {
  const sources = [source("missing-code", { latest_run: { ...failed, error: null } })];
  const summary = summarizeSourceRegistry(sources);
  assert.equal(summary.errors[0].lens, "error:not_recorded");
  assert.equal(filterSourceRegistry(sources, "error:not_recorded").length, 1);
});
