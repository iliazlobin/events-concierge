import assert from "node:assert/strict";
import test from "node:test";
import {
  COMMAND_EVENT_TAIL_LIMIT, mergeCommandEvents, mergeCommandInvestigation,
  commandWaitExplanation, codeRevisionLabel,
} from "../lib/admin-command-investigation.ts";

const event = (id, code = "started") => ({ event_id: String(id), event_code: code, observed_at: "2026-09-08T00:00:00Z" });
const investigation = (id, events, cursor = null) => ({ command_id: id, events, next_event_id: cursor, plan: { tasks: [] }, attempts: [] });

test("event replay is deduplicated without losing bigint cursor order", () => {
  const merged = mergeCommandEvents([event("9007199254740994"), event("9007199254740992")],
    [event("9007199254740993"), event("9007199254740992", "updated")]);
  assert.deepEqual(merged.map((item) => item.event_id), ["9007199254740992", "9007199254740993", "9007199254740994"]);
  assert.equal(merged[0].event_code, "updated");
});

test("following replaces current plan and attempts while retaining a bounded event tail", () => {
  const first = investigation("command", Array.from({ length: 500 }, (_, i) => event(i)), "499");
  const next = { ...investigation("command", Array.from({ length: 20 }, (_, i) => event(i + 500)), "519"),
    plan: { tasks: [{ status: "succeeded" }] }, attempts: [{ attempt_count: 7 }] };
  const merged = mergeCommandInvestigation(first, next);
  assert.equal(merged.events.length, COMMAND_EVENT_TAIL_LIMIT);
  assert.equal(merged.events[0].event_id, "20");
  assert.deepEqual(merged.plan, next.plan);
  assert.deepEqual(merged.attempts, next.attempts);
  assert.equal(merged.next_event_id, "519");
});

test("empty live response preserves cursor; changing commands never mixes activity", () => {
  const first = investigation("first", [event(10)], "10");
  assert.equal(mergeCommandInvestigation(first, investigation("first", [])).next_event_id, "10");
  const changed = mergeCommandInvestigation(first, investigation("second", [event(1)], "1"));
  assert.deepEqual(changed.events.map((item) => item.event_id), ["1"]);
});

test("queued wait evidence distinguishes future eligibility without inventing a blocker", () => {
  const now = Date.parse("2026-09-08T00:00:00Z");
  assert.match(commandWaitExplanation("queued", "2026-09-08T00:01:00Z", now), /not eligible/);
  assert.match(commandWaitExplanation("queued", "2026-09-07T23:59:00Z", now), /awaiting a dispatch claim/);
  assert.match(commandWaitExplanation("running", null, now), /lease evidence separately/);
});

test("dirty and unpinned worker revisions cannot be presented as published source proof", () => {
  assert.match(codeRevisionLabel("5377b52-dirty", "sha256:abc"), /local or unpublished/);
  assert.match(codeRevisionLabel("5377b52", null), /source is not pinned/);
  assert.equal(codeRevisionLabel(null, null), "Execution revision not recorded");
});

test("progress counters do not sum cumulative snapshots or invent unmeasured zeroes", async () => {
  const { commandActivityMetrics } = await import("../lib/admin-command-investigation.ts");
  const scope = { command_attempt: 7, source_key: "bay-arts", run_key: "fixed", task_attempt: 2, worker_id: "one" };
  assert.deepEqual(commandActivityMetrics([
    { ...scope, request_count: 2, page_count: 1 }, { ...scope, request_count: 4, page_count: 3 },
    { ...scope, request_count: null, page_count: null },
  ]), { requests: 4, pages: 3 });
  assert.deepEqual(commandActivityMetrics([{ ...scope, request_count: null, page_count: null }]), { requests: null, pages: null });
});

test("current stage belongs to the exact running task claim, not an old attempt or pending source", async () => {
  const { currentTaskObservation } = await import("../lib/admin-command-investigation.ts");
  const current = { ...event(2), source_key: "one", run_key: "fixed", task_attempt: 2, stage: "collect" };
  const tasks = [{ source_key: "one", run_key: "fixed", attempt_count: 2, status: "running" }];
  assert.equal(currentTaskObservation(tasks, [current, { ...current, event_id: "3", task_attempt: 1 }]), current);
  assert.equal(currentTaskObservation([{ ...tasks[0], status: "pending" }], [current]), undefined);
  assert.equal(currentTaskObservation([{ ...tasks[0], lease_state: "expired" }], [current]), undefined);
});

test("code copy references resolve repository modules without inventing published commit links", async () => {
  const { codeSourcePath } = await import("../lib/admin-command-investigation.ts");
  assert.equal(codeSourcePath("events_concierge.adapters.bibliocommons.source"), "src/events_concierge/adapters/bibliocommons/source.py");
  assert.equal(codeSourcePath("unrecognized/module"), "unrecognized/module");
});

test("a fresh collection stage does not retain counters from an earlier activity retry", async () => {
  const { commandActivityMetrics } = await import("../lib/admin-command-investigation.ts");
  const scope = { command_attempt: 7, source_key: "bay-arts", run_key: "fixed", task_attempt: 2, worker_id: "one" };
  assert.deepEqual(commandActivityMetrics([
    { ...scope, request_count: 4, page_count: 3 },
    { ...scope, event_code: "stage_started", stage: "collect", request_count: null, page_count: null },
  ]), { requests: null, pages: null });
});

test("operational lease and exhausted-retry failures remain visible without exception metadata", async () => {
  const { isCommandError } = await import("../lib/admin-command-investigation.ts");
  for (const event_code of ["error", "lease_lost", "lease_renewal_error", "execution_error"]) {
    assert.equal(isCommandError({ event_code, error_type: null, outcome_code: null }), true);
  }
  for (const outcome_code of ["failed", "retry_exhausted"]) {
    assert.equal(isCommandError({ event_code: "task_completed", error_type: null, outcome_code }), true);
  }
  assert.equal(isCommandError({ event_code: "progress", error_type: null, outcome_code: "progressed" }), false);
});

test("investigation tabs support roving keyboard focus without changing evidence selection", async () => {
  const { commandPanelForKey } = await import("../lib/admin-command-investigation.ts");
  assert.equal(commandPanelForKey("overview", "ArrowRight"), "activity");
  assert.equal(commandPanelForKey("overview", "ArrowLeft"), "execution");
  assert.equal(commandPanelForKey("execution", "ArrowRight"), "overview");
  assert.equal(commandPanelForKey("activity", "Home"), "overview");
  assert.equal(commandPanelForKey("activity", "End"), "execution");
  assert.equal(commandPanelForKey("activity", "Escape"), undefined);
});
