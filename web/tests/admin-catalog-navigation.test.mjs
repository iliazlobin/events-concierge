import assert from "node:assert/strict";
import test from "node:test";

import { catalogLocationFromUrl, catalogLocationUrl } from "../lib/admin-catalog-navigation.ts";
import { adminHistoryLocationFromUrl, adminHistoryUrl } from "../lib/admin-history.ts";

const origin = "https://events.test";
const href = `${origin}/admin`;
const absolute = (path) => new URL(path, origin).href;
const defaults = {
  sourceKey: "", sourceQuery: "", sourcePage: 1, dateScope: "all", priceScope: "all", query: "", eventId: null, afterStart: null, afterId: null,
};
const eventId = "019a7137-8b68-7bf4-b75c-f3a04c98123f";
const cursorId = "019a7137-8b68-7bf4-b75c-f3a04c981230";

test("exact publishing run, search and cursor survive copying and browser history", () => {
  const state = { ...defaults, sourceKey: "bay-arts-01", runKey: "manual:bay-arts-01:20260908",
    dateScope: "upcoming", priceScope: "free", query: "music", eventId, afterStart: "2026-09-30T09:00:00.000001Z", afterId: cursorId };
  const recordUrl = absolute(catalogLocationUrl(state, href));
  assert.deepEqual(catalogLocationFromUrl(recordUrl), state);
  assert.equal(new URL(recordUrl).searchParams.get("store_run"), state.runKey);
  const ledgerUrl = absolute(adminHistoryUrl({ tab: "runs", sourceKey: null }, recordUrl));
  assert.deepEqual(catalogLocationFromUrl(ledgerUrl), state);
  const cleared = { ...state, runKey: undefined, query: "", eventId: null, afterStart: null, afterId: null };
  const currentSourceUrl = absolute(catalogLocationUrl(cleared, recordUrl));
  assert.equal(new URL(currentSourceUrl).searchParams.has("store_run"), false);
  assert.equal(catalogLocationFromUrl(currentSourceUrl).sourceKey, state.sourceKey);
  assert.deepEqual(catalogLocationFromUrl(recordUrl), state);
});

test("run attribution is bounded and cannot exist without a valid source", () => {
  for (const runKey of ["", "a".repeat(257), "../private", "run\nkey", " run", "run/extra"]) {
    assert.equal(catalogLocationFromUrl(`${href}?${new URLSearchParams({ store_source: "bay-arts-01", store_run: runKey })}`).runKey, undefined);
  }
  assert.equal(catalogLocationFromUrl(`${href}?store_run=manual:one`).runKey, undefined);
  assert.equal(catalogLocationFromUrl(`${href}?store_source=../private&store_run=manual:one`).runKey, undefined);
});

test("Catalog reads legacy record links and copies canonical links without retired diagram state", () => {
  assert.deepEqual(catalogLocationFromUrl(href), defaults);
  const state = {
    ...defaults, sourceKey: "bay-arts-01", query: "SF / music café",
    eventId, afterStart: "2026-12-31T09:00:00.123456-07:00", afterId: cursorId,
  };
  const old = new URL(`${href}?store=catalog&tab=system&component=catalog&path=catalog&usecase=ingestion&focus=collection&node=catalog&edge=publish:catalog:write&inspect=data&level=systems&view=structure&source_lens=all&source_query=retired&source_page=2&ops_queue=request_start&campaign=ops#records`);
  for (const [key, value] of Object.entries({
    store_source: state.sourceKey, store_source_query: "Bay & arts", store_source_page: "3",
    store_query: state.query, store_event: state.eventId, store_after_start: state.afterStart, store_after_id: state.afterId,
  })) old.searchParams.set(key, value);
  assert.deepEqual(catalogLocationFromUrl(old.href), state);
  const copied = new URL(absolute(catalogLocationUrl(state, old.href)));
  assert.equal(copied.searchParams.get("tab"), "catalog");
  assert.equal(adminHistoryLocationFromUrl(copied.href).tab, "catalog");
  assert.deepEqual(catalogLocationFromUrl(copied.href), state);
  for (const key of ["store", "component", "path", "usecase", "focus", "node", "edge", "inspect", "level", "view", "source_lens", "source_query", "source_page", "store_source_query", "store_source_page"]) {
    assert.equal(copied.searchParams.has(key), false, key);
  }
  assert.equal(copied.searchParams.get("ops_queue"), "request_start");
  assert.equal(copied.searchParams.get("campaign"), "ops");
  assert.equal(copied.hash, "#records");
});

test("global date and price filters preserve their exact record cursor across navigation", () => {
  const before = {
    ...defaults, dateScope: "past", priceScope: "paid", query: "event filter",
    eventId, afterStart: "2026-09-30T09:00:00.000001Z", afterId: cursorId,
  };
  const beforeUrl = absolute(catalogLocationUrl(before, href));
  assert.deepEqual(catalogLocationFromUrl(beforeUrl), before);
  assert.equal(new URL(beforeUrl).searchParams.has("store_source"), false);
  const operationsUrl = absolute(adminHistoryUrl({ tab: "overview", sourceKey: null }, beforeUrl));
  const returnedUrl = absolute(adminHistoryUrl({ tab: "catalog", sourceKey: null }, operationsUrl));
  assert.deepEqual(catalogLocationFromUrl(returnedUrl), before);
  const changed = { ...before, dateScope: "upcoming", priceScope: "unknown", eventId: null, afterStart: null, afterId: null };
  const afterUrl = absolute(catalogLocationUrl(changed, returnedUrl));
  assert.deepEqual(catalogLocationFromUrl(afterUrl), changed);
  assert.deepEqual(catalogLocationFromUrl(beforeUrl), before);
});

test("date and price scopes use exact allowlists on reads and canonical writes", () => {
  for (const dateScope of ["all", "upcoming", "past"]) {
    for (const priceScope of ["all", "free", "paid", "unknown"]) {
      const expected = { ...defaults, dateScope, priceScope };
      const url = absolute(catalogLocationUrl(expected, href));
      assert.deepEqual(catalogLocationFromUrl(url), expected);
      assert.equal(new URL(url).searchParams.has("store_dates"), dateScope !== "all");
      assert.equal(new URL(url).searchParams.has("store_price"), priceScope !== "all");
    }
  }
  for (const invalid of ["", "ALL", "Past", "upcoming ", "free/paid", "0", "a".repeat(200)]) {
    const params = new URLSearchParams({ store_dates: invalid, store_price: invalid });
    assert.deepEqual(catalogLocationFromUrl(`${href}?${params}`), defaults);
    assert.deepEqual(catalogLocationFromUrl(absolute(catalogLocationUrl({ ...defaults, dateScope: invalid, priceScope: invalid }, href))), defaults);
  }
  const { dateScope: _date, priceScope: _price, sourceQuery: _pickerQuery, sourcePage: _pickerPage, ...legacy } = defaults;
  assert.deepEqual(catalogLocationFromUrl(absolute(catalogLocationUrl(legacy, href))), defaults);
});

test("Catalog serialization retires configuration state while preserving its record browser", () => {
  const selected = { ...defaults, configurationKey: "luma-sf", sourceKey: "bay-arts-01", query: "music", eventId };
  const url = catalogLocationUrl(selected, `${href}?campaign=ops#records`);
  const { configurationKey: _retired, ...browsing } = selected;
  assert.deepEqual(catalogLocationFromUrl(absolute(url)), browsing);
  assert.equal(new URL(absolute(url)).searchParams.has("catalog_config"), false);
  const closed = catalogLocationUrl({ ...selected, configurationKey: undefined }, absolute(url));
  assert.equal(new URL(absolute(closed)).searchParams.has("catalog_config"), false);
  assert.equal(catalogLocationFromUrl(absolute(closed)).sourcePage, 1);
  assert.equal(catalogLocationFromUrl(absolute(closed)).query, "music");
  for (const bad of ["../private", "a", "Luma-SF", "a".repeat(81)]) {
    assert.equal(catalogLocationFromUrl(`${href}?${new URLSearchParams({ catalog_config: bad })}`).configurationKey, undefined);
  }
});

test("Catalog source selectors match the operator route and UUIDs require their complete shape", () => {
  for (const sourceKey of ["ab", "bay-arts-01", "0a", "s".repeat(80)]) {
    const parsed = catalogLocationFromUrl(`${href}?${new URLSearchParams({ store_source: sourceKey })}`);
    assert.equal(parsed.sourceKey, sourceKey);
    assert.equal(catalogLocationFromUrl(absolute(catalogLocationUrl({ ...defaults, sourceKey }, href))).sourceKey, sourceKey);
  }
  for (const sourceKey of ["a", "Bay-arts", "bay_arts", "source/extra", " source-key", "source-key ", "s".repeat(81), "-source"]) {
    const params = new URLSearchParams({ store_source: sourceKey });
    assert.equal(catalogLocationFromUrl(`${href}?${params}`).sourceKey, "", sourceKey);
    assert.equal(new URL(absolute(catalogLocationUrl({ ...defaults, sourceKey }, href))).searchParams.has("store_source"), false, sourceKey);
  }
  assert.equal(catalogLocationFromUrl(`${href}?store_event=${eventId.toUpperCase()}`).eventId, eventId);
  for (const invalid of ["", "event-123", eventId.replaceAll("-", ""), `{${eventId}}`, `${eventId}/extra`, ` ${eventId}`, "019g7137-8b68-7bf4-b75c-f3a04c98123f"]) {
    const params = new URLSearchParams({ store_event: invalid });
    assert.equal(catalogLocationFromUrl(`${href}?${params}`).eventId, null, invalid);
    assert.equal(new URL(absolute(catalogLocationUrl({ ...defaults, eventId: invalid }, href))).searchParams.has("store_event"), false, invalid);
  }
});

test("event search strips control characters and stays bounded", () => {
  const raw = ` \u0000Bay\tArts\n\u007f ${"é".repeat(200)}`;
  const clean = ` BayArts ${"é".repeat(200)}`.slice(0, 160);
  const params = new URLSearchParams({ store_query: raw });
  assert.equal(catalogLocationFromUrl(`${href}?${params}`).query, clean);
  const written = catalogLocationUrl({ ...defaults, query: raw }, href);
  assert.deepEqual(catalogLocationFromUrl(absolute(written)), { ...defaults, query: clean });
});

test("retired source-picker filters and pages are ignored and removed on new navigation", () => {
  const old = `${href}?store_source_query=library&store_source_page=4&store_source=bay-arts-01&store_query=music&store_dates=past&store_price=free`;
  const expected = { ...defaults, sourceKey: "bay-arts-01", query: "music", dateScope: "past", priceScope: "free" };
  assert.deepEqual(catalogLocationFromUrl(old), expected);
  const written = absolute(catalogLocationUrl({ ...expected, sourceQuery: "retired", sourcePage: 900 }, old));
  assert.deepEqual(catalogLocationFromUrl(written), expected);
  assert.equal(new URL(written).searchParams.has("store_source_query"), false);
  assert.equal(new URL(written).searchParams.has("store_source_page"), false);
});

test("event pagination requires an aware complete cursor and preserves calendar-valid microseconds", () => {
  const valid = [
    "2026-09-30T09:00:00.123456-07:00", "2026-10-31T09:00:00.123456Z", "2026-11-30T09:00:00Z",
    "2026-12-31T23:59:59.999999+05:30", "2024-02-29T09:00:00Z", "2000-02-29T09:00:00.000001Z",
  ];
  for (const afterStart of valid) {
    const params = new URLSearchParams({ store_after_start: afterStart, store_after_id: cursorId.toUpperCase() });
    const state = catalogLocationFromUrl(`${href}?${params}`);
    assert.equal(state.afterStart, afterStart, afterStart);
    assert.equal(state.afterId, cursorId);
    assert.deepEqual(catalogLocationFromUrl(absolute(catalogLocationUrl(state, href))), state);
  }
  const invalid = [
    "2026-09-31T09:00:00Z", "2026-11-31T09:00:00Z", "2026-02-29T09:00:00Z", "2026-02-30T09:00:00Z",
    "1900-02-29T09:00:00Z", "2026-04-31T09:00:00Z", "2026-12-32T09:00:00Z", "2026-00-01T09:00:00Z",
    "2026-13-01T09:00:00Z", "0000-09-08T09:00:00Z", "2026-09-08", "2026-09-08T09:00:00",
    "2026-09-08 09:00:00", "2026-09-08T24:00:00Z", "2026-09-08T09:00:00+24:00", "not-a-date",
    `${valid[0]}${" ".repeat(65)}`,
  ];
  for (const [afterStart, afterId] of [
    ...invalid.map(value => [value, cursorId]), [valid[0], null], [valid[0], ""], [valid[0], "not-a-uuid"], [null, cursorId], ["", cursorId],
  ]) {
    const params = new URLSearchParams();
    if (afterStart !== null) params.set("store_after_start", afterStart);
    if (afterId !== null) params.set("store_after_id", afterId);
    const state = catalogLocationFromUrl(`${href}?${params}`);
    assert.equal(state.afterStart, null, String(afterStart));
    assert.equal(state.afterId, null, String(afterId));
    const written = new URL(absolute(catalogLocationUrl({ ...defaults, afterStart, afterId }, href)));
    assert.equal(written.searchParams.has("store_after_start"), false);
    assert.equal(written.searchParams.has("store_after_id"), false);
  }
});

test("Catalog copies clear record defaults and normalize the workspace even when opened from a source", () => {
  const old = `${href}?tab=sources&source=luma-sf&store_source=luma-sf&store_source_query=arts&store_source_page=4&store_query=music&store_event=${eventId}&store_after_start=2026-09-30T09:00:00Z&store_after_id=${cursorId}&store_dates=past&store_price=free&registry_query=library#records`;
  const cleared = new URL(absolute(catalogLocationUrl(defaults, old)));
  assert.equal(cleared.searchParams.get("tab"), "catalog");
  assert.equal(cleared.searchParams.has("source"), false);
  for (const key of ["store_source", "store_source_query", "store_source_page", "store_query", "store_event", "store_after_start", "store_after_id", "store_dates", "store_price"]) {
    assert.equal(cleared.searchParams.has(key), false, key);
  }
  assert.equal(cleared.searchParams.get("registry_query"), "library");
  assert.equal(cleared.hash, "#records");
  assert.deepEqual(catalogLocationFromUrl(cleared.href), defaults);
});

test("legacy System catalog links migrate while explicit supported tabs and source details still win", () => {
  for (const [query, tab] of [
    ["store=catalog", "catalog"], ["tab=system&store=catalog", "catalog"], ["tab=system&focus=collection", "overview"],
    ["tab=overview&store=catalog", "overview"], ["tab=runs&store=catalog", "runs"],
    ["tab=invalid&store=catalog", "overview"], ["tab=system&store=catalog&source=luma-sf", "sources"],
  ]) assert.equal(adminHistoryLocationFromUrl(`${href}?${query}`).tab, tab, query);
});
