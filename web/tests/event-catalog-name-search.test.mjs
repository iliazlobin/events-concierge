import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, getCatalogNameSuggestions } from "../lib/api.ts";
import { emptyCatalogFilters } from "../lib/catalog-filters.ts";

const scoped = () => ({
  ...emptyCatalogFilters(), datePreset: "custom", customStart: "2030-10-09",
  customEnd: "2030-10-11", sourceKeys: ["tech-week-sf-2026"], city: "sanfrancisco",
  cities: ["sanfrancisco"], locationScopes: ["bay_area"], topics: ["ai"], price: "paid",
  priceComparison: "between", priceMinDollars: "5", priceMaxDollars: "20", availability: "available",
});
const entry = { name: "Nebius", kinds: ["organization"], event_count: 7 };
const response = (items, status = 200) => new Response(JSON.stringify(items), { status });

test("matching scoped names retain their counts and all selected restrictions", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (path) => {
    calls.push(new URL(path, "https://example.test"));
    return response([entry]);
  });
  assert.deepEqual(await getCatalogNameSuggestions(scoped(), "nebi"), [entry]);
  assert.equal(calls.length, 1);
  const query = calls[0].searchParams;
  for (const [key, value] of Object.entries({
    source_key: "tech-week-sf-2026", city: "sanfrancisco", location_scope: "bay_area",
    topic: "ai", price: "paid", price_min_cents: "500", price_max_cents: "2000",
    availability: "available", q: "nebi", limit: "8",
  })) assert.equal(query.get(key), value, key);
  assert.ok(query.has("starts_after"));
  assert.ok(query.has("starts_before"));
});

test("empty scoped names stay empty without fetching vendors outside the filters", async (t) => {
  const calls = [];
  const filters = scoped();
  const before = structuredClone(filters);
  t.mock.method(globalThis, "fetch", async (path, options) => {
    const url = new URL(path, "https://example.test");
    calls.push(url);
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.method, undefined);
    // A broader request would expose this known vendor despite its lack of scoped events.
    return response(url.searchParams.has("city") ? [] : [{ ...entry, name: "C++ Builders" }]);
  });
  assert.deepEqual(await getCatalogNameSuggestions(filters, "C++"), []);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].searchParams.get("q"), "C++");
  for (const key of ["source_key", "city", "location_scope", "topic", "price",
    "price_min_cents", "price_max_cents", "availability", "starts_after", "starts_before"]) {
    assert.ok(calls[0].searchParams.has(key), key);
  }
  assert.deepEqual(filters, before);
});

test("an empty unfiltered read is not repeated", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => { calls++; return response([]); });
  assert.deepEqual(await getCatalogNameSuggestions(emptyCatalogFilters(), "unknown"), []);
  assert.equal(calls, 1);
});

test("one-character text sends no names request", async (t) => {
  t.mock.method(globalThis, "fetch", async () => assert.fail("unexpected request"));
  assert.deepEqual(await getCatalogNameSuggestions(scoped(), "n"), []);
});

test("failed name reads remain errors and are not retried", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls++;
    return response({ detail: "temporarily unavailable" }, 503);
  });
  await assert.rejects(getCatalogNameSuggestions(scoped(), "nebi"), (error) => (
    error instanceof ApiError && error.status === 503
  ));
  assert.equal(calls, 1);
});

test("already-cancelled lookup sends no request", async (t) => {
  const controller = new AbortController();
  const reason = new Error("query changed");
  controller.abort(reason);
  t.mock.method(globalThis, "fetch", async () => assert.fail("unexpected request"));
  await assert.rejects(getCatalogNameSuggestions(scoped(), "nebi", controller.signal), reason);
});

test("cancelling a lookup discards its old response", async (t) => {
  const controller = new AbortController();
  const reason = new Error("query changed");
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (_, options) => {
    assert.equal(options.signal, controller.signal);
    calls++;
    controller.abort(reason);
    return response([entry]);
  });
  await assert.rejects(getCatalogNameSuggestions(scoped(), "nebi", controller.signal), reason);
  assert.equal(calls, 1);
});
