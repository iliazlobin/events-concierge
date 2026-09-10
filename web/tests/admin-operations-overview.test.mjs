import assert from "node:assert/strict";
import test from "node:test";
import { operationQueues, operationQueueDefinition, operationsQueueFromUrl, operationsQueueUrl } from "../lib/admin-operations-overview.ts";
import { sourceNeedsAttention } from "../lib/system-operations.ts";

const queue = (name, overrides = {}) => ({ queue: name, pending: 0, ready: 0, leased: 0, failed: 0, oldest_pending_at: null, last_progress_at: null, ...overrides });

test("retained command and notification failures never become current pending-error alerts", () => {
  for (const name of ["ingestion_commands", "notifications"]) {
    const [view] = operationQueues([queue(name, { pending: 3, failed: 25 })]);
    assert.equal(view.needsAttention, false);
    assert.equal(view.attention, null);
    assert.equal(view.definition.failureScope, "history");
    assert.equal(view.evidence.failureLabel, "Recorded failures");
    assert.match(view.evidence.failureMeaning, /separate from pending work/);
  }
});

test("pending errors and entity inventory errors retain different meanings", () => {
  const [start, entities] = operationQueues([queue("request_start", { pending: 41, failed: 41 }), queue("entity_refresh", { pending: 0, failed: 25 })]);
  assert.equal(start.needsAttention, true);
  assert.equal(start.attention, "41 pending with recorded errors");
  assert.equal(entities.needsAttention, true);
  assert.match(entities.attention, /may not be due now/);
  assert.equal(entities.work, "0 due for refresh");
  assert.equal(entities.definition.inventory, true);
  assert.equal(entities.evidence.progressLabel, "Last refresh evidence");
});

test("unknown returned queues stay visible and neutral even with failure signals", () => {
  const input = [queue("request_start"), queue("future_worker_v2", { pending: 5, failed: 900 })];
  const before = JSON.stringify(input);
  const result = operationQueues(input);
  assert.deepEqual(result.map(row => row.queue.queue), ["request_start", "future_worker_v2"]);
  assert.equal(result[1].definition.name, "future_worker_v2");
  assert.equal(result[1].evidence.tone, "neutral");
  assert.equal(result[1].needsAttention, false);
  assert.match(result[1].evidence.failureMeaning, /unknown/);
  assert.equal(JSON.stringify(input), before);
  assert.equal(operationQueueDefinition("other").failureScope, "unknown");
  for (const name of ["constructor", "__proto__"]) {
    const [row] = operationQueues([queue(name, { failed: 1 })]);
    assert.equal(row.definition.name, name);
    assert.equal(row.evidence.tone, "neutral");
    assert.equal(row.evidence.progressLabel, "Reported progress");
  }
});

test("empty recorded queues do not assert worker health and cleanup progress is stage-specific", () => {
  const [empty, cleanup] = operationQueues([queue("watch_projection"), queue("account_erasure")]);
  assert.equal(empty.evidence.tone, "neutral");
  assert.match(empty.evidence.summary, /liveness is not measured/);
  assert.equal(cleanup.evidence.progressLabel, "Last completed stage");
});

test("paused and retired sources are excluded from active-source attention", () => {
  const source = { enabled: true, retired_at: null, run_state: "failed", freshness_state: "late", retry_state: "ok", yield_state: "unknown" };
  assert.equal(sourceNeedsAttention(source), true);
  assert.equal(sourceNeedsAttention({ ...source, enabled: false }), false);
  assert.equal(sourceNeedsAttention({ ...source, retired_at: "2026-09-01" }), false);
});

test("queue inspection links preserve workspace state and allow future names", () => {
  const original = "http://localhost/admin?tab=overview&store_query=music#work";
  const linked = operationsQueueUrl(original, "future_worker/v2");
  assert.equal(operationsQueueFromUrl(linked), "future_worker/v2");
  assert.ok(linked.includes("store_query=music"));
  assert.ok(linked.endsWith("#work"));
  assert.equal(operationsQueueFromUrl(operationsQueueUrl(linked, null)), null);
  assert.equal(operationsQueueFromUrl("/admin?ops_queue=%00bad"), null);
  assert.equal(operationsQueueFromUrl(`/admin?ops_queue=${"x".repeat(161)}`), null);
});
