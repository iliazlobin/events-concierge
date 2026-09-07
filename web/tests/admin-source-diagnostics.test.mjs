import assert from "node:assert/strict";
import test from "node:test";

import {
  sourceAttentionScore,
  sourceDiagnostics,
} from "../lib/admin-presentation.ts";

function source(overrides = {}) {
  return {
    source_key: "public-meetings",
    display_name: "Public Meetings",
    publisher: "City",
    mode: "oakland_legistar",
    region: "Oakland",
    seed_url: "https://webapi.legistar.com/v1/Oakland/Events",
    enabled: true,
    review_status: "reviewed",
    effective_status: "active",
    due: false,
    last_succeeded_at: "2026-07-29T15:00:00Z",
    next_due_at: "2026-07-30T15:00:00Z",
    event_count: 0,
    latest_run: {
      run_key: "run-1",
      status: "succeeded",
      started_at: "2026-07-29T15:00:00Z",
      completed_at: "2026-07-29T15:01:00Z",
      candidate_count: 0,
      canonical_count: 0,
      error: null,
      attempt_count: 1,
      duration_ms: 60_000,
      source_revision: 1,
      release_revision: "development",
      image_digest: null,
      provenance_status: "claim_recorded",
      trigger: "cadence_or_manual",
    },
    ...overrides,
  };
}

test("a successful authoritative empty window is informational, not a crawler failure", () => {
  const [diagnostic] = sourceDiagnostics(source());

  assert.equal(diagnostic.tone, "neutral");
  assert.equal(diagnostic.title, "No publishable events in this window");
  assert.match(diagnostic.detail, /not a failed run/i);
  assert.equal(diagnostic.target, "catalog");
});

test("candidate loss before persistence remains actionable", () => {
  const value = source({
    latest_run: {
      ...source().latest_run,
      candidate_count: 12,
      canonical_count: 0,
    },
  });
  const [diagnostic] = sourceDiagnostics(value);

  assert.equal(diagnostic.tone, "warning");
  assert.equal(diagnostic.title, "Candidates were dropped before persistence");
  assert.equal(diagnostic.target, "normalize");
});

test("pacing deferrals stay out of failure attention and diagnostics", () => {
  const pacerDeferred = {
    ...source().latest_run,
    status: "failed",
    error: "pacer_deferred",
    candidate_count: 999,
    canonical_count: 999,
  };
  const value = source({
    due: true,
    last_succeeded_at: null,
    latest_run: pacerDeferred,
  });
  const diagnostics = sourceDiagnostics(value, {
    summary: {
      success_rate: 0.5,
      total_runs: 4,
      failed_runs: 2,
      yield_rate: null,
      candidate_count: 0,
    },
    recent_runs: [pacerDeferred],
  });

  assert.equal(sourceAttentionScore(value), 0);
  assert.equal(diagnostics[0].tone, "neutral");
  assert.equal(diagnostics[0].title, "Collection deferred by the pacing gate");
  assert.equal(diagnostics[0].target, "admission");
  assert.equal(
    diagnostics.some((diagnostic) => /failed|success in this window/i.test(diagnostic.title)),
    false,
  );
  assert.equal(
    diagnostics.some((diagnostic) => diagnostic.target === "runs"),
    false,
  );
});
