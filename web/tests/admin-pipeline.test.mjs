import assert from "node:assert/strict";
import test from "node:test";

import { buildAdminPipelineSummary } from "../lib/admin-pipeline.ts";

const source = (overrides = {}) => ({
  source_key: "source-a",
  source_revision: 1,
  display_name: "Source A",
  publisher: "Publisher",
  mode: "public_jsonld",
  region: "bay_area_9_county",
  seed_url: "https://example.com/events",
  enabled: true,
  review_status: "reviewed",
  effective_status: "active",
  due: false,
  last_succeeded_at: "2026-07-31T12:00:00Z",
  next_due_at: "2026-07-31T13:00:00Z",
  event_count: 6,
  latest_run: null,
  ...overrides,
});

const run = (overrides = {}) => ({
  run_key: crypto.randomUUID(),
  source_key: "source-a",
  display_name: "Source A",
  status: "succeeded",
  started_at: "2026-07-31T12:00:00Z",
  completed_at: "2026-07-31T12:00:01Z",
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
});

const overview = {
  generated_at: "2026-07-31T12:30:00Z",
  policy: { allowed: true, reason: "Allowed", code: "allowed" },
  summary: {
    sources: 2,
    active_sources: 1,
    due_sources: 0,
    running_runs: 0,
    failed_runs_24h: 0,
    catalog_events: 9,
    pending_commands: 0,
    fixture_sources: 0,
  },
  latest_success_at: "2026-07-31T12:00:01Z",
};

test("pipeline aggregation uses only recorded successful output and honest pacing semantics", () => {
  const summary = buildAdminPipelineSummary(
    [
      run(),
      run({
        run_key: "failed",
        source_key: "source-b",
        status: "failed",
        candidate_count: 999,
        canonical_count: 999,
        duration_ms: 5_000,
      }),
      run({
        run_key: "deferred",
        status: "failed",
        error: "pacer_deferred",
        candidate_count: 999,
        canonical_count: 999,
        duration_ms: 30_000,
      }),
    ],
    7,
    [
      source(),
      source({
        source_key: "source-b",
        enabled: false,
        effective_status: "disabled",
        event_count: 6,
      }),
    ],
    overview,
    "",
  );

  assert.equal(summary.partial, true);
  assert.equal(summary.loadedRuns, 3);
  assert.equal(summary.totalRuns, 7);
  assert.equal(summary.succeededRuns, 1);
  assert.equal(summary.failedRuns, 1);
  assert.equal(summary.pausedRuns, 1);
  assert.equal(summary.candidateCount, 10);
  assert.equal(summary.canonicalCount, 8);
  assert.equal(summary.outputRuns, 1);
  assert.equal(summary.yieldRate, 0.8);
  assert.equal(summary.averageDurationMs, 3_000);
  assert.equal(summary.slowestDurationMs, 5_000);
});

test("fleet catalog stays globally deduplicated while a source scope uses its projection", () => {
  const sources = [source(), source({ source_key: "source-b", event_count: 6 })];
  const fleet = buildAdminPipelineSummary([], 0, sources, overview, "");
  const selected = buildAdminPipelineSummary([], 0, sources, overview, "source-b");

  assert.equal(fleet.currentCatalogEvents, 9);
  assert.equal(fleet.catalogScope, "fleet");
  assert.equal(fleet.admittedSources, 1);
  assert.equal(fleet.totalSources, 2);
  assert.equal(selected.currentCatalogEvents, 6);
  assert.equal(selected.catalogScope, "source");
  assert.equal(selected.admittedSources, 1);
  assert.equal(selected.totalSources, 1);
});
