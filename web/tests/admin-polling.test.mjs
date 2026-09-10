import assert from "node:assert/strict";
import test from "node:test";
import { startAdminPolling } from "../lib/admin-polling.ts";
import { shouldPollAdminCommandDetail } from "../lib/admin-command-polling.ts";

const flush = () => new Promise((resolve) => setImmediate(resolve));
function clock() {
  let callback;
  return {
    schedule: (next) => { assert.equal(callback, undefined); callback = next; return 1; },
    cancel: () => { callback = undefined; },
    tick: () => { const next = callback; callback = undefined; next?.(); },
    pending: () => Boolean(callback),
  };
}

test("a slow request cannot overlap a later poll, and stopping aborts the request", async () => {
  const timer = clock();
  let resolve;
  let signal;
  let calls = 0;
  const values = [];
  const stop = startAdminPolling({
    ...timer,
    load: (nextSignal) => { calls++; signal = nextSignal; return new Promise((next) => { resolve = next; }); },
    onValue: (value) => values.push(value), onError: assert.fail, shouldContinue: () => true,
  });
  assert.equal(calls, 1);
  assert.equal(timer.pending(), false);
  timer.tick();
  assert.equal(calls, 1);
  stop();
  assert.equal(signal.aborted, true);
  resolve("late snapshot");
  await flush();
  assert.deepEqual(values, []);
  assert.equal(timer.pending(), false);
});

test("terminal dispatch continues tracking its children and stops at their terminal result", async () => {
  const timer = clock();
  const values = [
    { command: { status: "completed" }, progress: { running: 1, pending: 0 }, runs: [{ status: "running" }] },
    { command: { status: "completed" }, progress: { running: 0, pending: 0 }, runs: [{ status: "succeeded" }] },
  ];
  let calls = 0;
  const stop = startAdminPolling({ ...timer,
    load: async () => values[calls++], onValue: () => {}, onError: assert.fail,
    shouldContinue: (detail) => shouldPollAdminCommandDetail(detail, "completed"),
  });
  await flush();
  assert.equal(timer.pending(), true);
  timer.tick();
  await flush();
  assert.equal(calls, 2);
  assert.equal(timer.pending(), false);
  stop();
});

test("hidden views issue no requests and resume with fresh evidence when visible", async () => {
  const timer = clock();
  let visible = false;
  let calls = 0;
  const stop = startAdminPolling({ ...timer,
    isVisible: () => visible, load: async () => ++calls, onValue: () => {}, onError: assert.fail,
    shouldContinue: () => false,
  });
  assert.equal(calls, 0);
  timer.tick();
  assert.equal(calls, 0);
  visible = true;
  timer.tick();
  await flush();
  assert.equal(calls, 1);
  assert.equal(timer.pending(), false);
  stop();
});

test("transient read failure preserves the polling owner and retries only after a delay", async () => {
  const timer = clock();
  let calls = 0;
  let errors = 0;
  const stop = startAdminPolling({ ...timer,
    load: async () => { if (++calls === 1) throw new Error("unavailable"); return "recovered"; },
    onValue: (value) => assert.equal(value, "recovered"), onError: () => errors++, shouldContinue: () => false,
  });
  await flush();
  assert.equal(errors, 1);
  assert.equal(calls, 1);
  timer.tick();
  await flush();
  assert.equal(calls, 2);
  assert.equal(timer.pending(), false);
  stop();
});

test("permanent authorization failures do not create a retry loop", async () => {
  const timer = clock();
  let errors = 0;
  const stop = startAdminPolling({ ...timer,
    load: async () => { throw new Error("forbidden"); },
    onValue: assert.fail, onError: () => errors++, shouldContinue: () => true,
    retryError: () => false,
  });
  await flush();
  assert.equal(errors, 1);
  assert.equal(timer.pending(), false);
  stop();
});
