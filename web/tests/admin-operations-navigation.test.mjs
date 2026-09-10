import assert from "node:assert/strict";
import test from "node:test";
import { operationsNavigation, operationsNavigationUrl } from "../lib/admin-operations-navigation.ts";

const params = href => new URL(href, "http://localhost").searchParams;

test("Work-only and retired panel bookmarks return to Operations", () => {
  for (const href of ["/admin", "/admin?ops_work=1", "/admin?ops_area=requests&ops_lens=reliability&ops_issues=1&ops_source_page=3"]) {
    assert.deepEqual(operationsNavigation(href), { queue: null, workPage: 1 });
  }
});

test("copied queue bookmarks and unknown future queue names remain addressable", () => {
  for (const queue of ["request_start", "notifications", "entity_refresh", "future_worker_v3"]) {
    assert.deepEqual(operationsNavigation(`/admin?ops_work=1&ops_queue=${queue}`), { queue, workPage: 1 });
  }
  for (const queue of ["", " invalid", "bad\nqueue", "a".repeat(161)]) {
    assert.deepEqual(operationsNavigation(`/admin?ops_queue=${encodeURIComponent(queue)}`), { queue: null, workPage: 1 });
    assert.equal(params(operationsNavigationUrl("/admin", { queue })).has("ops_queue"), false);
  }
});

test("direct queue navigation removes retired state and preserves other investigations", () => {
  const original = "/admin?ops_work=1&ops_area=requests&ops_lens=reliability&ops_issues=1&ops_source_page=3&store_source=music&registry_query=arts&run_query=old&future=value#records";
  const href = operationsNavigationUrl(original, { queue: "request_start", workPage: 1 });
  assert.deepEqual(operationsNavigation(href), { queue: "request_start", workPage: 1 });
  for (const key of ["ops_work", "ops_area", "ops_lens", "ops_issues", "ops_source_page"]) assert.equal(params(href).has(key), false);
  for (const key of ["store_source", "registry_query", "run_query", "future"]) assert.equal(params(href).get(key), params(original).get(key));
  assert.ok(href.endsWith("#records"));
  assert.equal(operationsNavigationUrl(href, { queue: "request_start" }), href);
});

test("returning to Operations clears queue, record, and pagination", () => {
  const original = "/admin?ops_work=1&ops_queue=request_start&ops_record=record&ops_offset=20&ops_scope=errors&store_query=music";
  const href = operationsNavigationUrl(original, { queue: null, workPage: 1 });
  for (const key of ["ops_work", "ops_queue", "ops_record", "ops_offset", "ops_scope"]) assert.equal(params(href).has(key), false);
  assert.equal(href, "/admin?store_query=music");
  assert.deepEqual(operationsNavigation(original), { queue: "request_start", workPage: 1 });
});

test("same-queue writes preserve a record; changing queue cannot leak its selection", () => {
  const original = "/admin?ops_queue=request_start&ops_record=record&ops_offset=20&ops_scope=errors";
  const same = operationsNavigationUrl(original, { queue: "request_start", workPage: 1 });
  assert.equal(same, original);
  const changed = operationsNavigationUrl(original, { queue: "notifications", workPage: 1 });
  assert.deepEqual(operationsNavigation(changed), { queue: "notifications", workPage: 1 });
  for (const key of ["ops_record", "ops_offset", "ops_scope"]) assert.equal(params(changed).has(key), false);
  const dangling = operationsNavigationUrl("/admin?ops_record=record&ops_offset=20", { queue: null, workPage: 1 });
  assert.equal(dangling, "/admin");
});


test("background pagination survives queue drilldown, copied links and browser return", () => {
  const original = "/admin?registry_query=arts&trend_window=168#work";
  const second = operationsNavigationUrl(original, { queue: null, workPage: 2 });
  assert.deepEqual(operationsNavigation(second), { queue: null, workPage: 2 });
  const records = operationsNavigationUrl(second, { queue: "entity_refresh" });
  assert.deepEqual(operationsNavigation(records), { queue: "entity_refresh", workPage: 2 });
  const selected = `${records.replace("#work", "")}&ops_record=record&ops_offset=10&ops_scope=errors#work`;
  const returned = operationsNavigationUrl(selected, { queue: null });
  assert.equal(returned, second);
  assert.deepEqual(operationsNavigation(records), { queue: "entity_refresh", workPage: 2 });
  assert.equal(operationsNavigationUrl(returned, { queue: null, workPage: 1 }), original);
});

test("background page is bounded, one-based, and canonicalized without disturbing a selected record", () => {
  for (const value of ["0", "-1", "1.5", "Infinity", "NaN", "1e3", "9007199254740992", "", "abc"]) {
    assert.equal(operationsNavigation(`/admin?ops_work_page=${value}`).workPage, 1, value);
  }
  assert.equal(operationsNavigation("/admin?ops_work_page=0002").workPage, 2);
  assert.equal(operationsNavigation("/admin?ops_work_page=100001").workPage, 100000);
  for (const workPage of [0, -1, 1.5, Infinity, NaN, Number.MAX_SAFE_INTEGER + 1]) {
    const url = operationsNavigationUrl("/admin?ops_queue=notifications&ops_record=-9223372036854775808&ops_scope=failed", { queue: "notifications", workPage });
    assert.equal(params(url).has("ops_work_page"), false);
    assert.equal(params(url).get("ops_record"), "-9223372036854775808");
    assert.equal(params(url).get("ops_scope"), "failed");
  }
});
