import assert from "node:assert/strict";
import test from "node:test";
import { workPreviewInsight } from "../lib/admin-work-preview.ts";
import { getAdminWorkRecords } from "../lib/admin-work-errors.ts";

const queue = (name, values = {}) => ({ queue: name, pending: 41, ready: 0, leased: 0, failed: 0, oldest_pending_at: null, last_progress_at: null, ...values });

test("future pending work is not called stuck or processing", () => {
  assert.match(workPreviewInsight(queue("request_start"), "pending"), /Nothing is ready to claim/);
  assert.match(workPreviewInsight(queue("request_start", { ready: 2 }), "pending"), /does not confirm/);
  assert.match(workPreviewInsight(queue("notifications", { leased: 3 }), "pending"), /does not establish worker progress/);
});

test("failure history, pending errors, and entity inventory keep their scopes", () => {
  assert.match(workPreviewInsight(queue("notifications", { failed: 12 }), "failed"), /separate from pending/);
  assert.match(workPreviewInsight(queue("request_start", { failed: 12 }), "errors"), /pending requests/);
  assert.match(workPreviewInsight(queue("entity_refresh", { failed: 21, pending: 0 }), "errors"), /0 profiles are due/);
  assert.match(workPreviewInsight(queue("entity_refresh", { failed: 21, pending: 5 }), "errors"), /groups can overlap/);
  assert.match(workPreviewInsight(undefined, "pending"), /unavailable/);
});

test("preview reads are bounded on the server and full investigations retain ten records", async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async href => {
    const url = new URL(href, "http://localhost"); calls.push(url);
    return Response.json({ queue: url.searchParams.get("queue"), scope: url.searchParams.get("scope"), items: [] });
  };
  try {
    for (const name of ["request_start", "notifications", "entity_refresh"]) {
      await getAdminWorkRecords(name, name === "entity_refresh" ? "errors" : "pending", 0, null, new AbortController().signal, 3);
      assert.equal(calls.at(-1).searchParams.get("limit"), "3");
      assert.equal(calls.at(-1).searchParams.get("offset"), "0");
    }
    await getAdminWorkRecords("notifications", "pending", 0, null, new AbortController().signal);
    assert.equal(calls.at(-1).searchParams.get("limit"), "10");
    for (const limit of [0, -1, 1.5, NaN, Infinity, 51]) {
      await getAdminWorkRecords("request_start", "pending", 0, null, new AbortController().signal, limit);
      assert.equal(calls.at(-1).searchParams.get("limit"), "10");
    }
  } finally { globalThis.fetch = original; }
});
