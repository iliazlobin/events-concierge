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
