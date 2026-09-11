import assert from "node:assert/strict";
import test from "node:test";

import { queueEvidence, sourceNeedsAttention, sourceSignal } from "../lib/system-operations.ts";

const queue = (overrides = {}) => ({
  queue: "request_start", pending: 0, ready: 0, leased: 0, failed: 0,
  oldest_pending_at: null, last_progress_at: null, ...overrides,
});

const source = (overrides = {}) => ({
  source_key: "example", display_name: "Example", publisher: "Example",
  mode: "public_jsonld", region: "bay_area_9_county", enabled: true, retired_at: null,
  refresh_interval_minutes: 60, page_limit: 1, health: "healthy", run_state: "ok",
  freshness_state: "ok", retry_state: "ok", yield_state: "ok",
  last_attempt_at: "2026-09-08T09:00:00Z", last_success_at: "2026-09-08T09:00:00Z",
  last_catalog_change_at: "2026-09-08T09:00:00Z", latest_run_status: "succeeded",
  latest_run_error: null, latest_attempt_count: 1, upcoming_events: 10, hours_since_success: 0,
  ...overrides,
});

test("missing and unavailable snapshots remain unknown, including a cached zero snapshot", () => {
  for (const evidence of [queueEvidence(undefined), queueEvidence(queue(), true)]) {
    assert.equal(evidence.label, "Unknown");
    assert.equal(evidence.tone, "neutral");
    assert.match(evidence.summary, /unavailable/);
  }
  assert.equal(queueEvidence(queue()).label, "No pending work");
  assert.equal(queueEvidence(queue()).tone, "neutral");
});

test("ready and leased work are durable evidence, never worker health", () => {
  const ready = queueEvidence(queue({ pending: 2, ready: 1, leased: 1 }));
  assert.equal(ready.label, "Ready work");
  assert.match(ready.summary, /2 pending · 1 ready · 1 leased/);
  assert.match(ready.summary, /Worker liveness is not measured/);
  assert.equal(queueEvidence(queue({ pending: 1, leased: 1 })).label, "Leased work");
  assert.equal(queueEvidence(queue({ pending: 1 })).label, "Pending work");
});

test("pending error predicates are explained as overlapping work", () => {
  for (const name of ["request_start", "account_erasure", "change_delivery", "calendar_repair", "handoff_expiry", "watch_projection"]) {
    const evidence = queueEvidence(queue({ queue: name, pending: 41, failed: 41 }));
    assert.equal(evidence.tone, "warn", name);
    assert.equal(evidence.failureLabel, "Pending with errors", name);
    assert.match(evidence.failureMeaning, /overlaps the pending count/);
    assert.match(evidence.summary, /41 pending with errors/);
  }
  assert.equal(queueEvidence(queue({ queue: "account_erasure" })).progressLabel, "Last completed stage");
});

test("historical command and notification failures do not become current incidents", () => {
  for (const name of ["notifications", "ingestion_commands"]) {
    const evidence = queueEvidence(queue({ queue: name, failed: 169 }));
    assert.equal(evidence.tone, "neutral");
    assert.equal(evidence.label, "Failure history");
    assert.match(evidence.failureMeaning, /retained history/);
    assert.match(evidence.failureMeaning, /separate from pending/);
    const pending = queueEvidence(queue({ queue: name, pending: 2, ready: 1, failed: 15 }));
    assert.equal(pending.tone, "info");
    assert.equal(pending.label, "Ready work");
  }
});

test("entity error evidence can exist without due work and does not measure a claim lease", () => {
  const evidence = queueEvidence(queue({ queue: "entity_refresh", failed: 25 }));
  assert.equal(evidence.tone, "warn");
  assert.match(evidence.summary, /0 entities due · 25 with failed or blocked source evidence/);
  assert.match(evidence.summary, /No claim lease is measured/);
  assert.match(evidence.failureMeaning, /may not currently be due/);
  assert.equal(evidence.progressLabel, "Last refresh evidence");
  assert.equal(queueEvidence(queue({ queue: "entity_refresh" })).label, "No refresh due");
});

test("an unrecognized queue does not inherit current or historical failure semantics", () => {
  const evidence = queueEvidence(queue({ queue: "future_queue", failed: 1 }));
  assert.equal(evidence.failureLabel, "Failure signals");
  assert.match(evidence.failureMeaning, /relationship to pending work is unknown/);
});

test("paused and retired sources are not active exceptions even when they still serve stale events", () => {
  const paused = source({ enabled: false, freshness_state: "down", run_state: "failed", upcoming_events: 100 });
  const retired = source({ retired_at: "2026-09-01T00:00:00Z", freshness_state: "down", retry_state: "severe" });
  assert.equal(sourceNeedsAttention(paused), false);
  assert.equal(sourceNeedsAttention(retired), false);
  assert.equal(sourceSignal(paused), "Paused");
  assert.equal(sourceSignal(retired), "Retired");
});

test("source attention follows independent evidence axes rather than only the aggregate token", () => {
  for (const overrides of [
    { run_state: "failed" }, { run_state: "never_run" }, { freshness_state: "never" },
    { freshness_state: "down" }, { freshness_state: "late" }, { freshness_state: "warn" },
    { retry_state: "elevated" }, { retry_state: "severe" }, { yield_state: "zero_yield" },
  ]) assert.equal(sourceNeedsAttention(source(overrides)), true, JSON.stringify(overrides));
  assert.equal(sourceNeedsAttention(source()), false);
  assert.equal(sourceNeedsAttention(source({ run_state: "running" })), false);
});

test("zero inventory and unknown yield do not establish failed ingestion; recorded zero yield invites inspection", () => {
  assert.equal(sourceNeedsAttention(source({ upcoming_events: 0, yield_state: "unknown" })), false);
  const empty = source({ upcoming_events: 0, yield_state: "zero_yield" });
  assert.equal(sourceNeedsAttention(empty), true);
  assert.equal(sourceSignal(empty), "No catalog output on latest success");
  assert.equal(sourceSignal(source({ freshness_state: "never", last_success_at: null })), "No successful refresh recorded");
});

test("old retries on a succeeded run do not claim a currently retrying worker", () => {
  const retried = source({ retry_state: "severe", latest_attempt_count: 25, latest_run_status: "succeeded" });
  assert.equal(sourceSignal(retried), "25 attempts on latest run");
  assert.equal(sourceSignal(source({ run_state: "running" })), "Run recorded as running");
});
