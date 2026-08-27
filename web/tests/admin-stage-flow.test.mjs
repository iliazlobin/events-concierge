import assert from "node:assert/strict";
import test from "node:test";

import {
  buildStageFlow,
  stageFlowEmptyReason,
} from "../lib/admin-stage-flow.ts";

const STAGES = {
  generated_at: "2026-08-27T00:00:00Z",
  window_hours: 168,
  stages: [
    {
      stage: "catalog_publish", stage_position: 5, evidence_status: "measured",
      folded_into: null, runs_with_evidence: 699, observations: 811,
      total_ms: 1_993_849, avg_ms: 2848, p95_ms: 9932, failed_count: 3, pct_of_wall: 0.5,
    },
    {
      stage: "admission", stage_position: 1, evidence_status: "measured",
      folded_into: null, runs_with_evidence: 779, observations: 18_335,
      total_ms: 194_126, avg_ms: 249, p95_ms: 615, failed_count: 0, pct_of_wall: 0.1,
    },
    {
      stage: "collect", stage_position: 2, evidence_status: "measured",
      folded_into: null, runs_with_evidence: 716, observations: 3312,
      total_ms: 375_635_194, avg_ms: 524_346, p95_ms: 245_813, failed_count: 17,
      pct_of_wall: 99.4,
    },
    {
      stage: "extract_enrich", stage_position: 3,
      evidence_status: "not_separately_instrumented",
      folded_into: "adapter_boundary_includes_extract_enrich",
      runs_with_evidence: 0, observations: 0, total_ms: 0,
      avg_ms: null, p95_ms: null, failed_count: 0, pct_of_wall: 0,
    },
    {
      stage: "normalize_dedupe", stage_position: 4,
      evidence_status: "not_separately_instrumented",
      folded_into: "commit_boundary_includes_normalize_dedupe",
      runs_with_evidence: 0, observations: 0, total_ms: 0,
      avg_ms: null, p95_ms: null, failed_count: 0, pct_of_wall: 0,
    },
  ],
};

const FLEET = {
  generated_at: "2026-08-27T00:00:00Z",
  window_start: "2026-08-20T00:00:00Z",
  window_hours: 168,
  runs: 779, succeeded: 696, failed: 19, running: 3, paused: 61,
  sources_run: 90, sources_failed: 13,
  attempts: 17_986, max_attempts: 5167, retrying_runs: 589,
  candidates: 187_068, canonicals: 183_125, zero_yield_runs: 18,
  wall_ms: 43_319_512,
  duration_p50_ms: 10_948, duration_p95_ms: 189_216, duration_p99_ms: 571_416,
};

test("steps are ordered by declared pipeline position, not response order", () => {
  const flow = buildStageFlow(STAGES, FLEET);
  assert.deepEqual(
    flow.steps.map((step) => step.stage),
    ["admission", "collect", "extract_enrich", "normalize_dedupe", "catalog_publish"],
  );
});

test("a folded stage reports null duration, never zero", () => {
  const flow = buildStageFlow(STAGES, FLEET);
  const folded = flow.steps.filter(
    (step) => step.evidenceStatus === "not_separately_instrumented",
  );

  assert.equal(folded.length, 2);
  for (const step of folded) {
    // Zero would assert the stage ran instantly; null says it was never separately timed.
    assert.equal(step.totalMs, null);
    assert.equal(step.pctOfWall, null);
    assert.equal(step.avgMs, null);
    assert.ok(step.foldedInto, "a folded stage must name its owning boundary");
  }
  assert.equal(flow.hasFoldedStages, true);
});

test("record counts appear only at the two real waypoints", () => {
  const flow = buildStageFlow(STAGES, FLEET);
  const counted = flow.steps.filter((step) => step.recordCount !== null);

  // This is the defect being fixed: the old panel showed four stage cards carrying only two
  // distinct numbers, duplicating each one.
  assert.deepEqual(counted.map((step) => step.stage), ["collect", "catalog_publish"]);
  assert.equal(counted[0].recordCount, 187_068);
  assert.equal(counted[1].recordCount, 183_125);
  assert.notEqual(counted[0].recordCount, counted[1].recordCount);
});

test("the candidate/canonical delta is reported as dedupe, not loss", () => {
  const flow = buildStageFlow(STAGES, FLEET);

  assert.equal(flow.candidates, 187_068);
  assert.equal(flow.canonicals, 183_125);
  assert.equal(flow.mergedByDedupe, 3943);
  assert.ok(flow.yieldRate !== null && flow.yieldRate > 0.97);
});

test("collect is identified as the dominant wall-clock owner", () => {
  const flow = buildStageFlow(STAGES, FLEET);
  const collect = flow.steps.find((step) => step.stage === "collect");

  assert.equal(collect.pctOfWall, 99.4);
});

test("a missing fleet summary yields no fabricated record counts", () => {
  const flow = buildStageFlow(STAGES, null);

  assert.equal(flow.candidates, 0);
  for (const step of flow.steps) {
    assert.equal(step.recordCount, null);
  }
});

test("an empty window and a failed load are distinguishable", () => {
  // The whole point: a broken load must not present itself as "no runs match".
  assert.equal(stageFlowEmptyReason({ ...FLEET, runs: 0 }, false), "no_runs");
  assert.equal(stageFlowEmptyReason(null, true), "unavailable");
  assert.equal(stageFlowEmptyReason(FLEET, false), null);
});
