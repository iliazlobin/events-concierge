import assert from "node:assert/strict";
import test from "node:test";

import {
  parseRunSelection, RUN_MAX_PAGE, RUN_PAGE_SIZE, runLedgerLocationFromUrl,
  runLedgerLocationUrl, runPageEvidence, runSelectionKey, runEvidenceLocationFromUrl,
  runEvidenceLocationUrl, runNeighbors, runStageDuration, runTimelineEntries, shouldFollowRun,
} from "../lib/admin-run-workspace.ts";

test("selected run identity includes its source and safely round-trips cadence keys", () => {
  const run = { source_key: "luma-sf", run_key: "cadence:luma-sf:2026-09-08T00:00:00Z" };
  assert.deepEqual(parseRunSelection(runSelectionKey(run)), { sourceKey: run.source_key, runKey: run.run_key });
  assert.notEqual(runSelectionKey(run), runSelectionKey({ ...run, source_key: "luma-sf-other" }));
  for (const value of [null, "", "luma-sf", "luma-sf|", "../private|run", "luma-sf|../run", "luma-sf|run|other", `luma-sf|${"r".repeat(257)}`]) {
    assert.equal(parseRunSelection(value), null);
  }
});

test("page, search and sort links preserve selected evidence, server scope and unrelated navigation", () => {
  const base = "https://events.test/admin?tab=runs&run_source=luma-sf&window=720&status=failed&fixtures=true&run_selection=luma-sf%7Crun-1&run_inspector=execution&pipeline_stage=collect#workspace";
  const location = { page: 3, query: "failed slot:α", sort: "duration", direction: "asc" };
  const path = runLedgerLocationUrl(location, base);
  const url = new URL(path, base);
  assert.deepEqual(runLedgerLocationFromUrl(url.href), location);
  for (const key of ["tab", "run_source", "window", "status", "fixtures", "run_selection", "run_inspector", "pipeline_stage"]) {
    assert.equal(url.searchParams.get(key), new URL(base).searchParams.get(key));
  }
  assert.equal(url.hash, "#workspace");
});

test("default ledger settings remove only ledger-owned parameters", () => {
  const base = "https://events.test/admin?tab=runs&run_page=2&run_query=old&run_sort=duration&run_direction=asc&run_selection=luma-sf%7Crun-1&window=336";
  const defaults = { page: 0, query: "", sort: "started", direction: "desc" };
  const url = new URL(runLedgerLocationUrl(defaults, base), base);
  assert.deepEqual(runLedgerLocationFromUrl(url.href), defaults);
  for (const key of ["run_page", "run_query", "run_sort", "run_direction"]) assert.equal(url.searchParams.has(key), false);
  assert.equal(url.searchParams.get("run_selection"), "luma-sf|run-1");
  assert.equal(url.searchParams.get("window"), "336");
});

test("malformed pagination cannot exceed the server offset contract", () => {
  for (const raw of ["-1", "0.5", "1e3", "Infinity", "NaN", "9007199254740992"]) {
    assert.equal(runLedgerLocationFromUrl(`https://events.test/admin?run_page=${raw}`).page, 0);
  }
  const bounded = runLedgerLocationFromUrl(`https://events.test/admin?run_page=900000&run_sort=payload&run_direction=up&run_query=${"a".repeat(200)}`);
  assert.equal(bounded.page, RUN_MAX_PAGE);
  assert.equal(bounded.page * RUN_PAGE_SIZE, 100_000);
  assert.equal(bounded.query.length, 160);
  assert.equal(bounded.sort, "started");
  assert.equal(bounded.direction, "desc");
});

test("unknown run output never becomes an observed zero", () => {
  const unknown = { status: "running", attempt_count: 1, canonical_count: null };
  assert.equal(runPageEvidence([]).output, null);
  assert.equal(runPageEvidence([unknown]).output, null);
  assert.equal(runPageEvidence([unknown]).outputMeasuredRuns, 0);
  const zero = runPageEvidence([{ ...unknown, status: "succeeded", canonical_count: 0 }]);
  assert.equal(zero.output, 0);
  assert.equal(zero.outputMeasuredRuns, 1);
});

test("loaded-page totals retain measured coverage and count reclaims separately", () => {
  const evidence = runPageEvidence([
    { status: "succeeded", attempt_count: 2, canonical_count: 5 },
    { status: "succeeded", attempt_count: 1, canonical_count: 5 },
    { status: "failed", attempt_count: 3, canonical_count: null },
    { status: "paused", attempt_count: 0, canonical_count: null },
  ]);
  assert.deepEqual(evidence, { succeeded: 2, failed: 1, reclaimed: 2, output: 10, outputMeasuredRuns: 2 });
});

test("stage and timeline perspectives round-trip without changing the server stage filter", () => {
  const base = "https://events.test/admin?tab=runs&run_selection=luma-sf%7Crun-1&run_stage=catalog_publish&run_stage_outcome=failed&run_page=2";
  const next = { stage: "extract_enrich", timeline: "stages" };
  const url = new URL(runEvidenceLocationUrl(next, base), base);
  assert.deepEqual(runEvidenceLocationFromUrl(url.href), next);
  assert.equal(url.searchParams.get("run_stage"), "catalog_publish");
  assert.equal(url.searchParams.get("run_stage_outcome"), "failed");
  assert.equal(url.searchParams.get("run_selection"), "luma-sf|run-1");
  assert.equal(url.searchParams.get("run_page"), "2");
  assert.deepEqual(runEvidenceLocationFromUrl("https://events.test/admin?run_evidence_stage=payload&run_timeline=raw"), { stage: "collect", timeline: "all" });
});

test("run navigation preserves server order and has explicit boundaries for exact lookups outside the page", () => {
  const runs = [
    { source_key: "luma-sf", run_key: "run-2" },
    { source_key: "luma-sf", run_key: "run-1" },
    { source_key: "smcl-events", run_key: "run-1" },
  ];
  assert.deepEqual(runNeighbors(runs, "luma-sf|run-1"), { index: 1, previous: "luma-sf|run-2", next: "smcl-events|run-1" });
  assert.equal(runNeighbors(runs, "luma-sf|run-2").previous, null);
  assert.equal(runNeighbors(runs, "smcl-events|run-1").next, null);
  assert.deepEqual(runNeighbors(runs, "luma-sf|old-run"), { index: -1, previous: null, next: null });
  assert.deepEqual(runNeighbors([], null), { index: -1, previous: null, next: null });
});

test("stage-duration display uses only measured evidence and keeps real zero", () => {
  const stages = [
    { stage: "collect", evidence_status: "measured", duration_ms: 0 },
    { stage: "catalog_publish", evidence_status: "measured", duration_ms: 63 },
    { stage: "extract_enrich", evidence_status: "not_separately_instrumented", duration_ms: 0 },
  ];
  assert.equal(runStageDuration(stages, "collect"), 0);
  assert.equal(runStageDuration(stages, "catalog_publish"), 63);
  assert.equal(runStageDuration(stages, "extract_enrich"), null);
  assert.equal(runStageDuration(stages, "admission"), null);
});

test("timeline filtering preserves original ordering and separates stage observations from lifecycle projections", () => {
  const entries = [
    { event_code: "command_requested", timestamp_basis: "durable_transition", stage: null },
    { event_code: "stage_observed", timestamp_basis: "evidence_recorded", stage: "collect" },
    { event_code: "execution_observed", timestamp_basis: "evidence_recorded", stage: null },
    { event_code: "run_status_observed", timestamp_basis: "run_projection", stage: null },
  ];
  assert.deepEqual(runTimelineEntries(entries, "all"), entries);
  assert.deepEqual(runTimelineEntries(entries, "stages"), [entries[1]]);
  assert.deepEqual(runTimelineEntries(entries, "lifecycle"), [entries[0], entries[3]]);
  assert.deepEqual(runTimelineEntries([], "stages"), []);
});

test("follow reads require a visible active selection and stop on failures or overlapping reads", () => {
  const enabled = { enabled: true, status: "running", visible: true, loading: false, failed: false, authorizationDenied: false };
  assert.equal(shouldFollowRun(enabled), true);
  for (const override of [
    { enabled: false }, { status: null }, { status: "succeeded" }, { status: "paused" },
    { status: "failed" }, { visible: false }, { loading: true }, { failed: true }, { authorizationDenied: true },
  ]) assert.equal(shouldFollowRun({ ...enabled, ...override }), false);
});
