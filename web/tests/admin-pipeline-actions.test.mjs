import assert from "node:assert/strict";
import test from "node:test";
import { pipelineAttentionSources, pipelineBucketScope, pipelineRunScope } from "../lib/admin-pipeline-workspace.ts";
import { sourceNeedsAttention } from "../lib/system-operations.ts";

const filters = {
  sourceKey: "", windowHours: 24, includeFixtures: true, status: "running",
  stage: "collect", stageOutcome: "failed",
  startedAfter: "2026-09-01T00:00:00Z", startedBefore: "2026-09-02T00:00:00Z",
};

test("fleet bucket intervals retain the SQL grid boundary and exclude fixtures", () => {
  const next = pipelineBucketScope(filters, "2026-09-07T10:00:00Z", 1, "2026-09-08T10:15:00Z", false);
  assert.deepEqual(next, {
    ...pipelineRunScope(filters), status: "", includeFixtures: false,
    startedAfter: "2026-09-07T10:00:00.000Z", startedBefore: "2026-09-07T11:00:00.000Z",
  });
  // The first grid bucket starts before generated_at - windowHours and contains real counts.
  assert.ok(Date.parse(next.startedAfter) < Date.parse("2026-09-07T10:15:00Z"));
});

test("current source bucket ends at the snapshot and retains source plus fixture scope", () => {
  const next = pipelineBucketScope({ ...filters, sourceKey: "library" }, "2026-09-08T10:00:00Z", 1, "2026-09-08T10:15:00Z", true, true);
  assert.equal(next.sourceKey, "library");
  assert.equal(next.includeFixtures, true);
  assert.equal(next.status, "failed");
  assert.equal(next.startedBefore, "2026-09-08T10:15:00.000Z");
  assert.equal(next.stage, undefined);
  assert.equal(next.stageOutcome, undefined);
});

test("invalid or future intervals never emit a misleading Runs link", () => {
  for (const [start, hours, at] of [
    ["invalid", 1, "2026-09-08T10:15:00Z"],
    ["2026-09-08T11:00:00Z", 1, "2026-09-08T10:15:00Z"],
    ["2026-09-08T10:00:00Z", 0, "2026-09-08T10:15:00Z"],
    ["2026-09-08T10:00:00Z", 1, "invalid"],
  ]) assert.equal(pipelineBucketScope(filters, start, hours, at, false), null);
});

test("attention prioritizes current failures without treating paused retained coverage as a fault", () => {
  const source = (source_key, overrides = {}) => ({
    source_key, display_name: source_key, enabled: true, retired_at: null,
    run_state: "ok", freshness_state: "ok", retry_state: "ok", yield_state: "ok", upcoming_events: 0,
    ...overrides,
  });
  const input = [source("paused", { enabled: false, run_state: "failed", upcoming_events: 500 }), source("late", { freshness_state: "late", upcoming_events: 100 }), source("failed", { run_state: "failed" }), source("healthy")];
  const ordered = pipelineAttentionSources(input, "");
  assert.deepEqual(ordered.filter(sourceNeedsAttention).map((row) => row.source_key), ["failed", "late"]);
  assert.deepEqual(input.map((row) => row.source_key), ["paused", "late", "failed", "healthy"]);
  assert.deepEqual(pipelineAttentionSources(input, "healthy").map((row) => row.source_key), ["healthy"]);
});
