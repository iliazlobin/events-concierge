import assert from "node:assert/strict";
import test from "node:test";

import {
  commandDetailHasActiveRuns,
  shouldPollAdminCommandDetail,
} from "../lib/admin-command-polling.ts";

function detail({
  commandStatus = "completed",
  pending = 0,
  running = 0,
  runStatuses = [],
} = {}) {
  return {
    command: { status: commandStatus },
    progress: { pending, running },
    runs: runStatuses.map((status) => ({ status })),
  };
}

test("terminal dispatch commands keep polling while a linked source run is active", () => {
  assert.equal(
    shouldPollAdminCommandDetail(
      detail({
        commandStatus: "completed",
        running: 1,
        runStatuses: ["running"],
      }),
      "completed",
    ),
    true,
  );
  assert.equal(
    shouldPollAdminCommandDetail(
      detail({
        commandStatus: "failed",
        pending: 1,
        runStatuses: ["pending"],
      }),
      "failed",
    ),
    true,
  );
});

test("linked run rows keep polling resilient to a stale aggregate", () => {
  const value = detail({
    commandStatus: "completed",
    pending: 0,
    running: 0,
    runStatuses: ["succeeded", "running"],
  });

  assert.equal(commandDetailHasActiveRuns(value), true);
  assert.equal(shouldPollAdminCommandDetail(value, "completed"), true);
});

test("polling stops only after the command and every linked run are terminal", () => {
  assert.equal(
    shouldPollAdminCommandDetail(
      detail({
        commandStatus: "completed",
        runStatuses: ["succeeded", "failed", "paused"],
      }),
      "completed",
    ),
    false,
  );
});

test("a terminal legacy receipt without linked runs does not poll forever", () => {
  assert.equal(
    shouldPollAdminCommandDetail(
      detail({ commandStatus: "completed", pending: 4 }),
      "completed",
    ),
    false,
  );
});

test("the list-row status is used before the first detail response", () => {
  assert.equal(shouldPollAdminCommandDetail(null, "queued"), true);
  assert.equal(shouldPollAdminCommandDetail(null, "running"), true);
  assert.equal(shouldPollAdminCommandDetail(null, "completed"), false);
});

// Exercise the polling owner's network shape and cross-screen invalidation signal.
const { readAdminCommandProgress } = await import("../lib/admin-command-polling.ts");

test("selected investigation owns its detail polling without duplicate background reads", async () => {
  const reads = [];
  await readAdminCommandProgress({
    selectedCommandId: "selected", watched: new Set(), signal: new AbortController().signal,
    loadList: async () => ({ items: [{ command_id: "selected", status: "running" }, { command_id: "other", status: "running" }] }),
    loadDetail: async (id) => { reads.push(id); return detail({ running: 1, runStatuses: ["running"] }); },
  });
  assert.deepEqual(reads, ["other"]);
});

test("command completion invalidates views only after its linked child work is followed", async () => {
  const watched = new Set(["command-a"]);
  const page = { items: [{ command_id: "command-a", status: "completed" }] };
  const first = await readAdminCommandProgress({
    watched, signal: new AbortController().signal, loadList: async () => page,
    loadDetail: async () => detail({ running: 1, runStatuses: ["running"] }),
  });
  assert.equal(first.continuePolling, true);
  assert.equal(first.childrenSettled, false);
  const next = await readAdminCommandProgress({
    watched, signal: new AbortController().signal, loadList: async () => page,
    loadDetail: async () => detail({ runStatuses: ["succeeded"] }),
  });
  assert.equal(next.continuePolling, false);
  assert.equal(next.childrenSettled, true);
  assert.equal(watched.size, 0);
});

test("detail reads rotate across active work with a fixed per-cycle ceiling", async () => {
  const watched = new Set();
  const page = { items: Array.from({ length: 10 }, (_, i) => ({ command_id: `c-${i}`, status: "running" })) };
  const calls = [];
  const read = () => readAdminCommandProgress({
    watched, signal: new AbortController().signal, loadList: async () => page,
    loadDetail: async (id) => { calls.push(id); return detail({ commandStatus: "running" }); },
  });
  await read();
  assert.deepEqual(calls, ["c-0", "c-1", "c-2", "c-3"]);
  await read();
  assert.deepEqual(calls.slice(4), ["c-4", "c-5", "c-6", "c-7"]);
});

test("terminal historical receipts are not expanded just to render a command list", async () => {
  const snapshot = await readAdminCommandProgress({
    watched: new Set(), signal: new AbortController().signal,
    loadList: async () => ({ items: [{ command_id: "historical", status: "completed" }] }),
    loadDetail: async () => assert.fail("unrelated historical detail read"),
  });
  assert.equal(snapshot.continuePolling, false);
  assert.equal(snapshot.childrenSettled, false);
});
