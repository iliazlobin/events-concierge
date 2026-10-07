import assert from "node:assert/strict";
import test from "node:test";

import {
  getCatalogDayPage,
  getCatalogFacets,
  getCatalogPage,
  getCatalogSummary,
} from "../lib/api.ts";
import { catalogFilterKey, emptyCatalogFilters } from "../lib/catalog-filters.ts";

const FILTERS = {
  ...emptyCatalogFilters("latest"),
  query: "  Alex Example  ",
  datePreset: "custom",
  dateRanges: [
    { id: "a", start: "2026-10-07", end: "2026-10-11" },
    { id: "b", start: "2026-10-17", end: "2026-10-18" },
  ],
  sourceKeys: ["tech-week-sf-2026", "luma-sf", "luma-sf"],
  city: "sanfrancisco",
  cities: ["sanfrancisco", "oakland", "oakland"],
  locationScopes: ["bay_area", "manhattan", "bay_area"],
  topics: ["ai", "networking", "ai"],
  price: "paid",
  priceComparison: "between",
  priceMinDollars: "10.25",
  priceMaxDollars: "25.50",
  availability: "available",
};

async function captureRequests(run) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (input) => {
    requests.push(new URL(String(input), "https://events.example.test"));
    return Response.json({ items: [], next_cursor: "next", providers: [], topic_facets: [] });
  };
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
  return requests;
}

test("a rolling preset uses the first page's exact window on every continuation", async () => {
  const firstRead = new Date(2026, 9, 7, 9, 12, 34, 567);
  const laterRead = new Date(2026, 9, 7, 10, 15);
  for (const preset of ["today", "week", "workweek", "month"]) {
    const filters = { ...FILTERS, datePreset: preset, dateRanges: [] };
    const requests = await captureRequests(async () => {
      await getCatalogPage(null, filters, null, firstRead);
      await getCatalogPage(null, filters, "next", firstRead);
      await getCatalogPage(null, filters, null, laterRead);
    });
    const queries = requests.map(request => request.searchParams);
    assert.equal(queries[0].get("starts_after"), firstRead.toISOString(), preset);
    assert.equal(queries[1].get("starts_after"), queries[0].get("starts_after"), preset);
    assert.equal(queries[1].get("starts_before"), queries[0].get("starts_before"), preset);
    assert.equal(queries[1].get("cursor"), "next");
    assert.equal(queries[2].get("starts_after"), laterRead.toISOString(), "new scope refreshes the anchor");
    assert.equal(queries[2].get("cursor"), null);
  }
});

test("events, facets, calendar summary, and day agenda share every non-date filter", async () => {
  const requests = await captureRequests(async () => {
    await getCatalogPage(null, FILTERS);
    await getCatalogFacets(null, FILTERS);
    await getCatalogSummary(null, FILTERS, "America/Los_Angeles");
    await getCatalogDayPage(null, FILTERS, "2026-10-08", 72, "day-next");
  });
  for (const request of requests) {
    const query = request.searchParams;
    assert.deepEqual(query.getAll("source_key"), ["tech-week-sf-2026", "luma-sf"]);
    assert.deepEqual(query.getAll("city"), ["sanfrancisco", "oakland"]);
    assert.deepEqual(query.getAll("location_scope"), ["bay_area", "manhattan"]);
    assert.deepEqual(query.getAll("topic"), ["ai", "networking"]);
    assert.equal(query.get("q"), "Alex Example");
    assert.equal(query.get("price"), "paid");
    assert.equal(query.get("price_min_cents"), "1025");
    assert.equal(query.get("price_max_cents"), "2550");
    assert.equal(query.get("availability"), "available");
  }
  const dateRanges = requests[0].searchParams.getAll("date_range");
  assert.equal(dateRanges.length, 2);
  for (const request of requests.slice(0, 3)) {
    assert.deepEqual(request.searchParams.getAll("date_range"), dateRanges);
    assert.equal(request.searchParams.get("starts_after"), null);
    assert.equal(request.searchParams.get("starts_before"), null);
  }
  const dayRanges = requests[3].searchParams.getAll("date_range");
  assert.equal(dayRanges.length, 1);
  const [start, end] = dayRanges[0].split("..").map(value => new Date(value));
  assert.deepEqual([start.getDate(), start.getHours(), end.getDate(), end.getHours()], [8, 0, 9, 0]);
  assert.equal(requests[3].searchParams.get("include_facets"), "false");
  assert.equal(requests[3].searchParams.get("cursor"), "day-next");
});

test("each eligibility filter changes the request and calendar cache identity", () => {
  const base = emptyCatalogFilters();
  const baseKey = catalogFilterKey(base);
  const cacheKey = catalogFilterKey(base, { includeSort: false, includeDateWindow: false });
  for (const patch of [
    { query: "Alex" }, { sourceKeys: ["luma-sf"] }, { cities: ["oakland"] },
    { locationScopes: ["bay_area"] }, { topics: ["ai"] }, { price: "free" },
    { priceComparison: "exactly", priceMinDollars: "12" }, { availability: "available" },
  ]) {
    assert.notEqual(catalogFilterKey({ ...base, ...patch }), baseKey);
    assert.notEqual(
      catalogFilterKey({ ...base, ...patch }, { includeSort: false, includeDateWindow: false }),
      cacheKey,
    );
  }
  assert.notEqual(catalogFilterKey({ ...base, datePreset: "week" }), baseKey);
  assert.notEqual(catalogFilterKey({ ...base, dateRanges: FILTERS.dateRanges }), baseKey);
});
