import assert from "node:assert/strict";
import test from "node:test";

import {
  bucketAverageDuration,
  bucketFailureTotal,
  bucketSuccessRate,
  bucketTotal,
  bucketYieldRate,
  buildRunBuckets,
  RUN_WINDOW_CONFIG,
} from "../lib/admin-run-chart.ts";

const NOW = new Date("2026-07-29T16:00:00.000Z");

function run(overrides = {}) {
  return {
    run_key: crypto.randomUUID(),
    source_key: "source-a",
    display_name: "Source A",
    status: "succeeded",
    started_at: "2026-07-29T15:30:00.000Z",
    completed_at: "2026-07-29T15:30:01.000Z",
    candidate_count: 10,
    canonical_count: 8,
    error: null,
    attempt_count: 1,
    duration_ms: 1_000,
    source_revision: 1,
    release_revision: "development",
    image_digest: null,
    provenance_status: "claim_recorded",
    trigger: "cadence_or_manual",
    is_latest_for_source: true,
    resolved_by_newer_success: false,
    ...overrides,
  };
}

test("run windows expose useful temporal resolution", () => {
  assert.deepEqual(
    Object.fromEntries(
      Object.entries(RUN_WINDOW_CONFIG).map(([window, value]) => [
        window,
        [value.bucketCount, value.bucketHours],
      ]),
    ),
    {
      24: [24, 1],
      168: [28, 6],
      720: [30, 24],
    },
  );
});

test("buckets separate unresolved and later-resolved failures", () => {
  const buckets = buildRunBuckets(
    [
      run(),
      run({
        run_key: "failed-current",
        source_key: "source-b",
        status: "failed",
        candidate_count: null,
        canonical_count: null,
        duration_ms: 2_000,
      }),
      run({
        run_key: "failed-resolved",
        source_key: "source-c",
        status: "failed",
        resolved_by_newer_success: true,
        candidate_count: null,
        canonical_count: null,
        duration_ms: 3_000,
      }),
    ],
    NOW.toISOString(),
    24,
  );
  const bucket = buckets.at(-1);

  assert.equal(bucket.succeeded, 1);
  assert.equal(bucket.failed, 1);
  assert.equal(bucket.resolved, 1);
  assert.equal(bucketFailureTotal(bucket), 2);
  assert.equal(bucketTotal(bucket), 3);
  assert.equal(bucketSuccessRate(bucket), 1 / 3);
  assert.deepEqual(bucket.failedSourceKeys, ["source-b", "source-c"]);
  assert.deepEqual(bucket.sourceKeys, ["source-a", "source-b", "source-c"]);
});

test("output, yield, and latency use bounded truthful inputs", () => {
  const buckets = buildRunBuckets(
    [
      run({ duration_ms: 1_000 }),
      run({
        run_key: "second-success",
        source_key: "source-b",
        candidate_count: 30,
        canonical_count: 27,
        duration_ms: 5_000,
      }),
      run({
        run_key: "failure",
        source_key: "source-c",
        status: "failed",
        candidate_count: 999,
        canonical_count: 999,
        duration_ms: 9_000,
      }),
    ],
    NOW.toISOString(),
    24,
  );
  const bucket = buckets.at(-1);

  assert.equal(bucket.candidates, 40);
  assert.equal(bucket.canonical, 35);
  assert.equal(bucketYieldRate(bucket), 0.875);
  assert.equal(bucketAverageDuration(bucket), 5_000);
  assert.equal(bucket.slowestDurationMs, 9_000);
});

test("pacing deferrals are paused coordination, not failure or performance data", () => {
  const buckets = buildRunBuckets(
    [
      run({ duration_ms: 1_000 }),
      run({
        run_key: "pacer-deferred",
        source_key: "source-b",
        status: "failed",
        error: "pacer_deferred",
        resolved_by_newer_success: true,
        candidate_count: 999,
        canonical_count: 999,
        duration_ms: 9_000,
      }),
    ],
    NOW.toISOString(),
    24,
  );
  const bucket = buckets.at(-1);

  assert.equal(bucket.succeeded, 1);
  assert.equal(bucket.paused, 1);
  assert.equal(bucket.failed, 0);
  assert.equal(bucket.resolved, 0);
  assert.equal(bucketFailureTotal(bucket), 0);
  assert.equal(bucketSuccessRate(bucket), 1);
  assert.equal(bucketTotal(bucket), 2);
  assert.deepEqual(bucket.failedSourceKeys, []);
  assert.deepEqual(bucket.sourceKeys, ["source-a", "source-b"]);
  assert.equal(bucket.candidates, 10);
  assert.equal(bucket.canonical, 8);
  assert.equal(bucketYieldRate(bucket), 0.8);
  assert.equal(bucketAverageDuration(bucket), 1_000);
  assert.equal(bucket.slowestDurationMs, 1_000);
});

test("runs outside the selected window are excluded", () => {
  const buckets = buildRunBuckets(
    [run({ started_at: "2026-07-27T15:30:00.000Z" })],
    NOW.toISOString(),
    24,
  );
  assert.equal(buckets.reduce((sum, bucket) => sum + bucketTotal(bucket), 0), 0);
});

test("bucket construction requires a valid explicit snapshot timestamp", () => {
  assert.deepEqual(buildRunBuckets([run()], "not-a-timestamp", 24), []);
});
