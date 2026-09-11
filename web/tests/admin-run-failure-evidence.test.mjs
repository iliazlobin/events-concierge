import assert from "node:assert/strict";
import test from "node:test";
import { runLogContext, runWorkerEvidence } from "../lib/admin-run-failure-evidence.ts";

const run = { source_key: "source-a", run_key: "run-a", started_at: "2026-09-08T16:00:00Z", command: { command_id: "command-a" }, attempt_count: 7 };
const event = { event_id: "1", observed_at: "2026-09-08T16:01:00Z", source_key: "source-a", run_key: "run-a", command_attempt: 1, task_attempt: 1, stage: "collect", event_code: "error", error_type: "validation" };
const snapshot = (events) => ({ command_id: "command-a", events });

test("worker evidence requires command, source and run identity including unfiltered command events", () => {
  const events = [event, { ...event, source_key: "source-b", error_type: "network" }, { ...event, run_key: "run-b", error_type: "internal" }, { ...event, source_key: null, run_key: null }];
  assert.deepEqual(runWorkerEvidence(run, snapshot(events)).events, [event]);
  assert.equal(runWorkerEvidence(run, { ...snapshot(events), command_id: "other" }).error, null);
  assert.equal(runWorkerEvidence({ ...run, command: null }, snapshot(events)).error, null);
  assert.equal(runWorkerEvidence(run, null).error, null);
});

test("last error uses the most specific stage in its task and distinguishes older attempts", () => {
  const generic = { ...event, event_id: "2", stage: null, observed_at: "2026-09-08T16:01:01Z" };
  const old = { ...event, event_id: "0", task_attempt: 2, observed_at: "2026-09-07T16:01:00Z", error_type: "timeout" };
  const result = runWorkerEvidence(run, snapshot([old, generic, event]));
  assert.equal(result.error, event);
  assert.equal(result.earlierAttempt, false);
  assert.equal(result.error.task_attempt, 1); // Never compare a command task attempt to source claims (7).
  assert.equal(runWorkerEvidence(run, snapshot([old])).earlierAttempt, true);
  assert.equal(runWorkerEvidence({ ...run, started_at: null }, snapshot([old])).unknownAttempt, true);
  assert.equal(runWorkerEvidence({ ...run, started_at: "invalid" }, snapshot([event])).unknownAttempt, true);
  assert.equal(runWorkerEvidence({ ...run, completed_at: "2026-09-08T16:00:59Z" }, snapshot([event])).error, event);
});

test("a newer error does not borrow a stage from an older command or task attempt", () => {
  const next = { ...event, event_id: "2", observed_at: "2026-09-08T16:02:00Z", task_attempt: 2, stage: null, error_type: "internal" };
  assert.equal(runWorkerEvidence(run, snapshot([event, next])).error, next);
  assert.equal(runWorkerEvidence(run, snapshot([{ ...event, command_attempt: 2 }, next])).error, next);
});

test("log context preserves opaque identifiers and actual attempt types", () => {
  const context = runLogContext(run, event);
  assert.match(context, /source_key=source-a\nrun_key=run-a\ncommand_id=command-a/);
  assert.match(context, /command_attempt=1\ntask_attempt=1/);
  assert.match(context, /error_type=validation/);
  assert.doesNotMatch(context, /undefined|null/);
});
