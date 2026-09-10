import assert from "node:assert/strict";
import test from "node:test";
import { getAdminWorkErrors, getAdminWorkRecords, supportsWorkErrors, supportsWorkRecords, workRecordScope, workRecordScopeUrl, workErrorLocation, workErrorState, workErrorTime, workErrorUrl } from "../lib/admin-work-errors.ts";

const record = "d8b004b2-e923-4664-8282-91449983eb0f";

test("record links restore a selected UUID and list page without changing workspace filters", () => {
  const original = "/admin?ops_queue=request_start&store_source=music&tab=overview#work";
  const selected = workErrorUrl(original, record.toUpperCase(), 20);
  assert.deepEqual(workErrorLocation(selected, "request_start"), { record, offset: 20 });
  assert.ok(selected.includes("store_source=music"));
  assert.ok(selected.endsWith("#work"));
  assert.deepEqual(workErrorLocation(selected, "entity_refresh"), { record: null, offset: 0 });
  const cleared = workErrorUrl(selected, null, 0);
  assert.equal(new URL(cleared, "http://localhost").searchParams.has("ops_record"), false);
  assert.equal(new URL(cleared, "http://localhost").searchParams.has("ops_offset"), false);
});

test("malformed references and offsets cannot become record queries", () => {
  for (const invalid of ["not-a-record", `${record}/extra`, "<script>"]) {
    assert.equal(workErrorLocation(`/admin?ops_queue=request_start&ops_record=${encodeURIComponent(invalid)}`, "request_start").record, null);
    assert.equal(new URL(workErrorUrl("/admin", invalid, 0), "http://localhost").searchParams.has("ops_record"), false);
  }
  for (const offset of ["-1", "NaN", "1.5", "10001", "1e2", "99999999999999999999"]) {
    assert.equal(workErrorLocation(`/admin?ops_queue=request_start&ops_offset=${offset}`, "request_start").offset, 0);
  }
  assert.equal(workErrorLocation("/admin?ops_queue=request_start&ops_offset=10000", "request_start").offset, 10000);
});

test("future retry dates retain year and timezone, and missing evidence stays unrecorded", () => {
  assert.equal(workErrorTime("2099-01-02T00:00:00Z"), "2099-01-02 00:00 UTC");
  assert.equal(workErrorTime("2026-09-08T10:30:00-07:00"), "2026-09-08 17:30 UTC");
  assert.equal(workErrorTime(null), "Not recorded");
  assert.equal(workErrorTime("invalid"), "Not recorded");
});

test("scheduler, lease, and entity inventory evidence do not imply worker liveness", () => {
  assert.match(workErrorState("scheduled").detail, /not ready to claim/);
  assert.match(workErrorState("leased").detail, /does not establish that a worker is running/);
  assert.match(workErrorState("ready").detail, /does not establish that a worker is running/);
  assert.match(workErrorState("due", "entity_refresh").detail, /does not record a claim lease/);
  assert.match(workErrorState("scheduled", "entity_refresh").detail, /profile is not due/);
  assert.doesNotMatch(workErrorState("scheduled", "entity_refresh").detail, /claim/);
  assert.equal(supportsWorkErrors("future_worker"), false);
  assert.equal(supportsWorkErrors("notifications"), false);
  assert.equal(supportsWorkErrors("entity_refresh"), true);
});

test("a selected record fetch is independent of its original list page and cancellable", async () => {
  const original = globalThis.fetch;
  const controller = new AbortController();
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return Response.json({ queue: "request_start", total: 1, offset: 0, limit: 10, items: [] });
  };
  try {
    await getAdminWorkErrors("request_start", 20, record, controller.signal);
    const url = new URL(calls[0].url, "http://localhost");
    assert.equal(url.pathname, "/admin/v1/operations/errors");
    assert.equal(url.searchParams.get("offset"), "0");
    assert.equal(url.searchParams.get("record_id"), record);
    assert.equal(url.searchParams.get("limit"), "10");
    assert.equal(calls[0].options.signal, controller.signal);
    assert.equal(calls[0].options.cache, "no-store");
    assert.equal(calls[0].options.credentials, "same-origin");
    await assert.rejects(getAdminWorkErrors("entity_refresh", 0, null, controller.signal), /did not match the selected queue/);
  } finally { globalThis.fetch = original; }
});


test("notification references remain exact decimal strings and reject invalid bigint IDs", () => {
  for (const id of ["0", "1", "9007199254740993", "9223372036854775807", "-9222999999738606380", "-9223372036854775808"]) {
    const href = workErrorUrl("/admin?ops_queue=notifications&ops_scope=failed", id, 10);
    assert.deepEqual(workErrorLocation(href, "notifications"), { record: id, offset: 10 });
  }
  for (const id of ["-0", "+1", "1.1", "01", "-01", "1e2", "9223372036854775808", "-9223372036854775809", record]) {
    assert.equal(workErrorLocation(`/admin?ops_queue=notifications&ops_record=${id}`, "notifications").record, null);
  }
  assert.equal(supportsWorkRecords("notifications"), true);
});

test("scope switches clear record and page while preserving other workspace selections", () => {
  const href = workRecordScopeUrl(`/admin?ops_queue=request_start&ops_record=${record}&ops_offset=10&store_query=music`, "errors");
  assert.equal(workRecordScope(href, "request_start"), "errors");
  assert.deepEqual(workErrorLocation(href, "request_start"), { record: null, offset: 0 });
  assert.equal(new URL(href, "http://localhost").searchParams.get("store_query"), "music");
  assert.equal(workRecordScope("/admin?ops_queue=notifications&ops_scope=errors", "notifications"), "pending");
  assert.equal(workRecordScope("/admin?ops_queue=request_start&ops_scope=failed", "request_start"), "pending");
  assert.equal(workRecordScope("/admin?ops_queue=notifications&ops_scope=failed", "notifications"), "failed");
  assert.equal(workRecordScope(workRecordScopeUrl(href, "pending"), "request_start"), "pending");
});

test("record reader keeps scopes separate and rejects mismatched responses", async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (href, options) => { calls.push({ href, options }); return Response.json({ queue: "notifications", scope: "failed", total: 0, items: [] }); };
  try {
    const signal = new AbortController().signal;
    await getAdminWorkRecords("notifications", "failed", 20, "-9222999999738606380", signal);
    const url = new URL(calls[0].href, "http://localhost");
    assert.equal(url.pathname, "/admin/v1/operations/records");
    assert.equal(url.searchParams.get("record_id"), "-9222999999738606380");
    assert.equal(url.searchParams.get("offset"), "0");
    assert.equal(url.searchParams.get("scope"), "failed");
    assert.equal(calls[0].options.signal, signal);
    await assert.rejects(getAdminWorkRecords("notifications", "pending", 0, null, signal), /did not match/);
    await assert.rejects(getAdminWorkRecords("request_start", "failed", 0, null, signal), /did not match/);
  } finally { globalThis.fetch = original; }
});
