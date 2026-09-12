import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  consumerHistorySnapshotFromUrl,
  consumerHistoryUrl,
  createConsumerHistorySnapshot,
  createConsumerHistoryState,
  readConsumerHistorySnapshot,
} from "../lib/consumer-history.ts";

const app = readFileSync(
  new URL("../components/concierge-app.tsx", import.meta.url),
  "utf8",
);

const FILTERS = {
  query: "Anna-Alexia Basile",
  sort: "latest",
  datePreset: "weekend",
  customStart: "2026-08-01",
  customEnd: "2026-08-02",
  dateRanges: [
    { id: "weekend-one", start: "2026-08-01", end: "2026-08-02", label: "This weekend" },
    { id: "weekend-two", start: "2026-08-08", end: "2026-08-09" },
  ],
  sourceKeys: ["luma"],
  city: "sanfrancisco",
  cities: ["sanfrancisco", "oakland"],
  locationScopes: ["bay_area"],
  price: "free",
  priceComparison: "at-most",
  priceMinDollars: "",
  priceMaxDollars: "25",
  availability: "available",
  topics: ["sports", "community"],
};

test("consumer navigation state preserves framework history and round-trips the full UI", () => {
  const snapshot = createConsumerHistorySnapshot("events", FILTERS, "event-123");
  const historyState = createConsumerHistoryState(snapshot, { __NA: true, keep: "next" });

  assert.equal(historyState.__NA, true);
  assert.equal(historyState.keep, "next");
  assert.deepEqual(readConsumerHistorySnapshot(historyState), snapshot);

  const url = consumerHistoryUrl(
    snapshot,
    "https://events.test/?campaign=summer#results",
  );
  const parsedUrl = new URL(url, "https://events.test");
  assert.equal(parsedUrl.searchParams.get("campaign"), "summer");
  assert.equal(parsedUrl.searchParams.get("q"), "Anna-Alexia Basile");
  assert.equal(parsedUrl.searchParams.get("sort"), "latest");
  assert.deepEqual(parsedUrl.searchParams.getAll("city"), ["sanfrancisco", "oakland"]);
  assert.deepEqual(parsedUrl.searchParams.getAll("topic"), ["sports", "community"]);
  assert.equal(parsedUrl.searchParams.get("availability"), "available");
  assert.deepEqual(parsedUrl.searchParams.getAll("date_range"), [
    "2026-08-01..2026-08-02",
    "2026-08-08..2026-08-09",
  ]);
  assert.equal(parsedUrl.searchParams.get("start"), null);
  assert.equal(parsedUrl.searchParams.get("end"), null);
  assert.equal(parsedUrl.searchParams.get("event"), "event-123");

  const restored = consumerHistorySnapshotFromUrl(
    `https://events.test${url}`,
    { ...FILTERS, query: "", cities: ["sanfrancisco"], topics: [] },
  );
  assert.deepEqual(
    restored?.filters.dateRanges?.map(({ start, end }) => ({ start, end })),
    snapshot.filters.dateRanges?.map(({ start, end }) => ({ start, end })),
  );
  assert.deepEqual(
    { ...restored, filters: { ...restored?.filters, dateRanges: undefined } },
    { ...snapshot, filters: { ...snapshot.filters, dateRanges: undefined } },
  );
});

test("version 1 history and start/end links migrate without breaking back navigation", () => {
  const legacyFilters = {
    ...FILTERS,
    datePreset: "custom",
    dateRanges: undefined,
  };
  const restoredState = readConsumerHistorySnapshot({
    eventsConciergeConsumer: {
      version: 1,
      view: "calendar",
      filters: legacyFilters,
      expandedId: null,
    },
  });
  assert.equal(restoredState?.version, 6);
  assert.equal(restoredState?.calendarMode, "month");
  assert.equal(restoredState?.filters.sort, "soonest");
  assert.deepEqual(restoredState?.filters.dateRanges, [{
    id: "2026-08-01..2026-08-02",
    start: "2026-08-01",
    end: "2026-08-02",
  }]);

  const restoredUrl = consumerHistorySnapshotFromUrl(
    "https://events.test/?view=events&when=custom&start=2026-08-15&end=2026-08-16",
    { ...FILTERS, dateRanges: [] },
  );
  assert.equal(restoredUrl?.filters.customStart, "2026-08-15");
  assert.equal(restoredUrl?.filters.customEnd, "2026-08-16");
  assert.deepEqual(restoredUrl?.filters.dateRanges, []);
});

test("calendar modes round-trip through URLs and history snapshots", () => {
  const snapshot = createConsumerHistorySnapshot("calendar", {
    ...FILTERS,
    datePreset: "custom",
    customStart: "2026-08-01",
    customEnd: "2026-10-31",
    dateRanges: [],
  }, null, "six-months");
  const url = consumerHistoryUrl(snapshot, "https://events.test/");
  assert.equal(new URL(url, "https://events.test").searchParams.get("calendar"), "six-months");
  assert.equal(
    consumerHistorySnapshotFromUrl(`https://events.test${url}`, FILTERS)?.calendarMode,
    "six-months",
  );
  assert.equal(
    readConsumerHistorySnapshot(createConsumerHistoryState(snapshot, {}))?.calendarMode,
    "six-months",
  );
});

test("legacy three-month links and history migrate to the six-month view", () => {
  const legacyState = createConsumerHistoryState(
    createConsumerHistorySnapshot("calendar", FILTERS, null, "six-months"),
    {},
  );
  legacyState.eventsConciergeConsumer.calendarMode = "three-months";
  assert.equal(readConsumerHistorySnapshot(legacyState)?.calendarMode, "six-months");
  assert.equal(
    consumerHistorySnapshotFromUrl(
      "https://events.test/?view=calendar&calendar=three-months",
      FILTERS,
    )?.calendarMode,
    "six-months",
  );
});

test("preset URLs omit stale custom bounds and ignore legacy bounds on reload", () => {
  const snapshot = createConsumerHistorySnapshot("events", {
    ...FILTERS,
    datePreset: "week",
    customStart: "2026-08-02",
    customEnd: "2026-08-02",
    dateRanges: [],
  }, null);
  const url = new URL(
    consumerHistoryUrl(snapshot, "https://events.test/?start=old&end=old"),
    "https://events.test",
  );

  assert.equal(url.searchParams.get("when"), "week");
  assert.equal(url.searchParams.get("start"), null);
  assert.equal(url.searchParams.get("end"), null);

  const restored = consumerHistorySnapshotFromUrl(
    "https://events.test/?view=events&when=week"
      + "&start=2026-08-02&end=2026-08-02",
    { ...FILTERS, customStart: "", customEnd: "", dateRanges: [] },
  );
  assert.equal(restored?.filters.datePreset, "week");
  assert.equal(restored?.filters.customStart, "");
  assert.equal(restored?.filters.customEnd, "");
});

test("cleared city filters survive link reload with the initial city default", () => {
  const snapshot = createConsumerHistorySnapshot("events", {
    ...FILTERS,
    query: "",
    datePreset: "all",
    customStart: "",
    customEnd: "",
    dateRanges: [],
    sourceKeys: [],
    city: "",
    cities: [],
    locationScopes: [],
    price: "any",
    priceComparison: "any",
    priceMinDollars: "",
    priceMaxDollars: "",
    availability: "any",
    topics: [],
  }, null);
  const url = new URL(
    consumerHistoryUrl(snapshot, "https://events.test/?city=legacy"),
    "https://events.test",
  );

  const restored = consumerHistorySnapshotFromUrl(url.href, {
    ...FILTERS,
    datePreset: "week",
    city: "sanfrancisco",
    cities: ["sanfrancisco"],
    dateRanges: [],
  });
  assert.equal(restored?.filters.city, "");
  assert.deepEqual(restored?.filters.cities, []);
  assert.equal(restored?.filters.datePreset, "all");
});

test("custom URLs retain singular start and end bounds", () => {
  const snapshot = createConsumerHistorySnapshot("events", {
    ...FILTERS,
    datePreset: "custom",
    customStart: "2026-08-15",
    customEnd: "2026-08-16",
    dateRanges: [],
  }, null);
  const url = new URL(
    consumerHistoryUrl(snapshot, "https://events.test/"),
    "https://events.test",
  );

  assert.equal(url.searchParams.get("start"), "2026-08-15");
  assert.equal(url.searchParams.get("end"), "2026-08-16");
});

test("date range URLs deduplicate ranges and derive a useful legacy draft", () => {
  const restored = consumerHistorySnapshotFromUrl(
    "https://events.test/?view=map&when=custom"
      + "&date_range=2026-08-01..2026-08-02"
      + "&date_range=2026-08-08..2026-08-09"
      + "&date_range=2026-08-01..2026-08-02",
    { ...FILTERS, customStart: "", customEnd: "", dateRanges: [] },
  );
  assert.deepEqual(restored?.filters.dateRanges, [
    {
      id: "2026-08-01..2026-08-02",
      start: "2026-08-01",
      end: "2026-08-02",
    },
    {
      id: "2026-08-08..2026-08-09",
      start: "2026-08-08",
      end: "2026-08-09",
    },
  ]);
  assert.equal(restored?.filters.customStart, "2026-08-01");
  assert.equal(restored?.filters.customEnd, "2026-08-02");
});

test("opening an event card does not re-request the list it is part of", () => {
  // Browsing actions rebuild the whole snapshot, so expanding a card handed back
  // a structurally identical filter object with a new identity. That read as a
  // new request and replaced the results the reader had just clicked into with
  // loading skeletons.
  assert.match(app, /const applyFilters = useCallback/);
  assert.match(
    app,
    /catalogFilterKey\(current\) === catalogFilterKey\(nextFilters\) \? current : nextFilters/,
  );
  // No path may set filters straight from a snapshot and bypass the guard.
  assert.doesNotMatch(app, /setFilters\(snapshot\.filters\)/);
  assert.doesNotMatch(app, /setFilters\(nextSnapshot\.filters\)/);
  assert.doesNotMatch(app, /setFilters\(normalized\)/);
});

test("deliberate consumer navigation pushes snapshots and popstate restores them", () => {
  const helperStart = app.indexOf("const pushConsumerSnapshot");
  const replace = app.indexOf("window.history.replaceState(", helperStart);
  const push = app.indexOf("window.history.pushState(", helperStart);
  const stateUpdate = app.indexOf("applyFilters(nextSnapshot.filters)", push);

  assert.ok(helperStart > 0, "consumer navigation has one history transaction helper");
  assert.ok(replace > helperStart, "the current UI is snapshotted before navigation");
  assert.ok(push > replace, "a deliberate navigation creates a browser entry");
  assert.ok(stateUpdate > push, "React state follows the browser history transaction");
  assert.match(app, /const changeView = \(nextView: ViewName\) => \{[\s\S]*?pushConsumerSnapshot/);
  assert.match(app, /const handleFacetSelect = useCallback[\s\S]*?pushConsumerSnapshot\(nextSnapshot\)/);
  assert.match(app, /history === "push"[\s\S]*?pushConsumerSnapshot/);
  assert.match(app, /window\.addEventListener\("popstate", handlePopState, true\)/);
  assert.match(
    app,
    /preserveExpandedOnNextCatalogLoad\.current = true;[\s\S]*setView\(snapshot\.view\);[\s\S]*applyFilters\(snapshot\.filters\);[\s\S]*setExpandedId\(snapshot\.expandedId\);/,
  );
});

test("invalid foreign history is ignored instead of corrupting the UI", () => {
  assert.equal(readConsumerHistorySnapshot(null), null);
  assert.equal(readConsumerHistorySnapshot({ eventsConciergeConsumer: { version: 999 } }), null);
  assert.equal(
    consumerHistorySnapshotFromUrl("https://events.test/?campaign=summer", FILTERS),
    null,
  );
});
