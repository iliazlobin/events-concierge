import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, getCatalogNameMatches } from "../lib/api.ts";
import { emptyCatalogFilters } from "../lib/catalog-filters.ts";

const scoped = () => ({
  ...emptyCatalogFilters(), datePreset: "custom", customStart: "2030-10-09",
  customEnd: "2030-10-11", sourceKeys: ["tech-week-sf-2026"], city: "sanfrancisco",
  cities: ["sanfrancisco"], locationScopes: ["bay_area"], topics: ["ai"], price: "paid",
  priceComparison: "between", priceMinDollars: "5", priceMaxDollars: "20", availability: "available",
});
const entry = { name: "Nebius", kinds: ["organization"], event_count: 7 };
const response = (items, status = 200) => new Response(JSON.stringify(items), { status });

test("matching scoped names keep their counts and issue no broader request", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (path) => {
    calls.push(path);
    return response([entry]);
  });
  assert.deepEqual(await getCatalogNameMatches(scoped(), "nebi"), {
    items: [entry], outsideFilters: false,
  });
  assert.equal(calls.length, 1);
  assert.equal(new URL(calls[0], "https://example.test").searchParams.get("city"), "sanfrancisco");
});

test("empty scoped names fall back once without changing event filters or literal text", async (t) => {
  const calls = [];
  const filters = scoped();
  const before = structuredClone(filters);
  t.mock.method(globalThis, "fetch", async (path, options) => {
    calls.push(new URL(path, "https://example.test"));
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.method, undefined);
    return response(calls.length === 1 ? [] : [{ ...entry, name: "C++ Builders" }]);
  });
  const result = await getCatalogNameMatches(filters, "C++");
  assert.equal(result.outsideFilters, true);
  assert.equal(result.items[0].event_count, 7);
  assert.equal(calls.length, 2);
  for (const key of ["source_key", "city", "location_scope", "topic", "price",
    "price_min_cents", "price_max_cents", "availability", "starts_after", "starts_before"]) {
    assert.ok(calls[0].searchParams.has(key), key);
    assert.equal(calls[1].searchParams.has(key), false, key);
  }
  assert.deepEqual([...calls[1].searchParams], [["q", "C++"], ["limit", "8"]]);
  assert.deepEqual(filters, before);
});

test("an empty unfiltered read is not repeated", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => { calls++; return response([]); });
  assert.deepEqual(await getCatalogNameMatches(emptyCatalogFilters(), "unknown"), {
    items: [], outsideFilters: false,
  });
  assert.equal(calls, 1);
});

test("one-character text sends no names request", async (t) => {
  t.mock.method(globalThis, "fetch", async () => assert.fail("unexpected request"));
  assert.deepEqual(await getCatalogNameMatches(scoped(), "n"), { items: [], outsideFilters: false });
});

test("a failed scoped read never masquerades as empty or triggers a broader read", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls++;
    return response({ detail: "temporarily unavailable" }, 503);
  });
  await assert.rejects(getCatalogNameMatches(scoped(), "nebi"), (error) => (
    error instanceof ApiError && error.status === 503
  ));
  assert.equal(calls, 1);
});

test("cancelling after the scoped response prevents the broader request", async (t) => {
  const controller = new AbortController();
  const reason = new Error("query changed");
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (_, options) => {
    assert.equal(options.signal, controller.signal);
    calls++;
    controller.abort(reason);
    return response([]);
  });
  await assert.rejects(getCatalogNameMatches(scoped(), "nebi", controller.signal), reason);
  assert.equal(calls, 1);
});

test("a failed broader read remains an error and is not retried", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => (
    ++calls === 1 ? response([]) : response({ detail: "unavailable" }, 503)
  ));
  await assert.rejects(getCatalogNameMatches(scoped(), "nebi"), ApiError);
  assert.equal(calls, 2);
});
