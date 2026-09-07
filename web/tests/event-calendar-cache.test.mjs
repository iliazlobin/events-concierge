import assert from "node:assert/strict";
import test from "node:test";

import {
  calendarSummaryByDate,
  calendarSummaryDayCategories,
  calendarSummaryLegendTopics,
  clipCalendarSummary,
} from "../lib/calendar.ts";

class MemoryStorage {
  #entries = new Map();

  get length() {
    return this.#entries.size;
  }

  key(index) {
    return [...this.#entries.keys()][index] ?? null;
  }

  getItem(key) {
    return this.#entries.has(key) ? this.#entries.get(key) : null;
  }

  setItem(key, value) {
    this.#entries.set(key, String(value));
  }

  removeItem(key) {
    this.#entries.delete(key);
  }

  clear() {
    this.#entries.clear();
  }
}

globalThis.window = { sessionStorage: new MemoryStorage() };

const {
  catalogSummarySignature,
  clearCatalogCache,
  readCatalogDayEvents,
  readCatalogSummary,
  writeCatalogDayEvents,
  writeCatalogSummary,
} = await import("../lib/catalog-cache.ts");

function filters(overrides = {}) {
  return {
    datePreset: "custom",
    customStart: "2026-08-01",
    customEnd: "2026-08-31",
    dateRanges: [],
    sourceKeys: [],
    query: "",
    city: "",
    cities: [],
    locationScopes: [],
    topics: [],
    price: "any",
    priceComparison: "any",
    priceMinDollars: "",
    priceMaxDollars: "",
    availability: "any",
    sort: "soonest",
    ...overrides,
  };
}

function summary(days) {
  return {
    days,
    total_event_count: days.reduce((total, day) => total + day.event_count, 0),
    time_zone: "America/Los_Angeles",
  };
}

test("the cache key covers every filter that changes which events a range holds", () => {
  const base = catalogSummarySignature(filters(), "America/Los_Angeles");

  assert.equal(base, catalogSummarySignature(filters(), "America/Los_Angeles"));
  // Presentation-only state cannot change a count.
  assert.equal(base, catalogSummarySignature(filters({ sort: "latest" }), "America/Los_Angeles"));

  // The window selects which days are read, never how many events a day holds,
  // so it is deliberately absent from the key: that is what lets one six-month
  // read serve the month and week grids inside it.
  assert.equal(
    base,
    catalogSummarySignature(
      filters({ customStart: "2026-08-01", customEnd: "2027-01-31" }),
      "America/Los_Angeles",
    ),
  );

  for (const overrides of [
    { sourceKeys: ["luma-sf"] },
    { query: "chess" },
    { cities: ["Oakland"] },
    { locationScopes: ["bay_area"] },
    { topics: ["music"] },
    { price: "free" },
    { priceComparison: "at-most", priceMaxDollars: "25" },
    { priceComparison: "at-least", priceMinDollars: "40" },
    { availability: "available" },
  ]) {
    assert.notEqual(
      base,
      catalogSummarySignature(filters(overrides), "America/Los_Angeles"),
      `${JSON.stringify(overrides)} must change the cache key`,
    );
  }
  // A day boundary is local, so the zone is part of the identity.
  assert.notEqual(base, catalogSummarySignature(filters(), "America/New_York"));
});

test("filter ordering does not fragment the cache", () => {
  assert.equal(
    catalogSummarySignature(filters({ topics: ["music", "arts"] }), "UTC"),
    catalogSummarySignature(filters({ topics: ["arts", "music"] }), "UTC"),
  );
});

test("a cached summary round-trips and reports its own staleness", () => {
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "America/Los_Angeles");
  const cached = summary([
    { start_day: "2026-08-25", event_count: 279, topics: [] },
  ]);
  writeCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", cached, 1_000);

  const fresh = readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", 2_000);
  assert.equal(fresh?.stale, false);
  assert.equal(fresh?.covered, true);
  assert.equal(fresh?.summary.days[0].event_count, 279);

  const stale = readCatalogSummary(
    "tenant-a", signature, "2026-08-01", "2026-08-31", 1_000 + 6 * 60 * 1000,
  );
  assert.equal(stale?.stale, true, "an expired entry may still paint, but must revalidate");
});

test("a range already read serves a narrower range that opens with it", () => {
  // The point of the cache: a six-month read already holds the month and the
  // week that begin on the same day, so those switches touch no network. A range
  // opening on a DIFFERENT day is not served, because its own first day would be
  // read short and the cached interior reading would not match it.
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "America/Los_Angeles");
  writeCatalogSummary(
    "tenant-a",
    signature,
    "2026-08-01",
    "2027-01-31",
    summary([
      { start_day: "2026-08-01", event_count: 6, topics: [] },
      { start_day: "2026-08-23", event_count: 14, topics: [] },
      { start_day: "2026-08-26", event_count: 9, topics: [] },
      { start_day: "2026-11-04", event_count: 3, topics: [] },
    ]),
    1_000,
  );

  const month = readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", 1_000);
  assert.equal(month?.covered, true, "the month opens on the day the six months did");
  assert.deepEqual(month?.summary.days.map((day) => day.start_day),
    ["2026-08-01", "2026-08-23", "2026-08-26"]);
  // The API totals a range by summing its days, so a slice must total the same.
  assert.equal(month?.summary.total_event_count, 29);

  const week = readCatalogSummary("tenant-a", signature, "2026-08-23", "2026-08-29", 1_000);
  assert.equal(week?.covered, false, "that week has never been read on its own terms");

  // A range reaching past what was read is painted, but not called complete.
  const beyond = readCatalogSummary("tenant-a", signature, "2026-08-01", "2027-06-30", 1_000);
  assert.equal(beyond?.covered, false, "an unread tail is unknown, not empty");
});

test("a day inside a read range with no entry holds no events", () => {
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "UTC");
  writeCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", summary([]), 1_000);

  const read = readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-12", 1_000);
  assert.equal(read?.covered, true, "the range was read; its silence is an answer");
  assert.equal(read?.summary.total_event_count, 0);
});

test("adjacent reads fuse so stepping the arrows leaves no seam", () => {
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "UTC");
  // The second read opens on the last day of the first, so between them every
  // day of September is known as an interior reading.
  writeCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", summary([]), 1_000);
  writeCatalogSummary("tenant-a", signature, "2026-08-31", "2026-09-30", summary([]), 1_100);

  const across = readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-09-30", 1_100);
  assert.equal(across?.covered, true, "two reads meeting end to start leave no unread day");
});

test("a re-read range drops days whose events disappeared", () => {
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "UTC");
  writeCatalogSummary(
    "tenant-a",
    signature,
    "2026-08-01",
    "2026-08-31",
    summary([
      { start_day: "2026-08-05", event_count: 4, topics: [] },
      { start_day: "2026-08-06", event_count: 2, topics: [] },
    ]),
    1_000,
  );
  writeCatalogSummary(
    "tenant-a",
    signature,
    "2026-08-01",
    "2026-08-31",
    summary([{ start_day: "2026-08-05", event_count: 4, topics: [] }]),
    1_100,
  );

  const read = readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", 1_100);
  assert.deepEqual(read?.summary.days.map((day) => day.start_day), ["2026-08-05"]);
  assert.equal(read?.summary.total_event_count, 4);
});

test("one account never reads another account's cached range", () => {
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "UTC");
  writeCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", summary([]), 1_000);

  assert.equal(
    readCatalogSummary("tenant-b", signature, "2026-08-01", "2026-08-31", 1_000),
    null,
  );
  assert.notEqual(
    readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", 1_000),
    null,
  );
});

test("clearing the cache removes every range", () => {
  writeCatalogSummary("tenant-a", "one", "2026-08-01", "2026-08-31", summary([]), 1_000);
  writeCatalogSummary("tenant-a", "two", "2026-08-01", "2026-08-31", summary([]), 1_000);
  clearCatalogCache();

  assert.equal(readCatalogSummary("tenant-a", "one", "2026-08-01", "2026-08-31", 1_000), null);
  assert.equal(readCatalogSummary("tenant-a", "two", "2026-08-01", "2026-08-31", 1_000), null);
});

test("a corrupt entry is ignored rather than thrown", () => {
  clearCatalogCache();
  window.sessionStorage.setItem(
    "events-concierge.catalog-summary.v3:tenant-a:broken",
    "{not json",
  );
  assert.equal(readCatalogSummary("tenant-a", "broken", "2026-08-01", "2026-08-31", 1_000), null);
});

test("the cache is bounded so a long session cannot exhaust storage", () => {
  clearCatalogCache();
  for (let index = 0; index < 40; index += 1) {
    writeCatalogSummary(
      "tenant-a", `range-${index}`, "2026-08-01", "2026-08-31", summary([]), 1_000 + index,
    );
  }
  assert.ok(window.sessionStorage.length <= 12, "the cache must prune its oldest entries");
  // The most recent write always survives the prune.
  assert.notEqual(
    readCatalogSummary("tenant-a", "range-39", "2026-08-01", "2026-08-31", 1_000),
    null,
  );
});

test("a week column's page never answers for the agenda", () => {
  // A column reads one page; a day window also matches events that began earlier
  // and those sort first, so that page can hold none of the day's own events.
  // True for the column, which shows the day's count beside it — and wrong for
  // the agenda, which would call a day of hundreds quiet.
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "UTC");
  writeCatalogDayEvents("tenant-a", signature, "2026-08-26", [], "cursor-1", "preview", 1_000);

  assert.equal(
    readCatalogDayEvents("tenant-a", signature, "2026-08-26", "agenda", 1_000),
    null,
    "the agenda must read the day itself, not a column's leftovers",
  );
  assert.deepEqual(
    readCatalogDayEvents("tenant-a", signature, "2026-08-26", "preview", 1_000)?.events,
    [],
  );
});

test("an agenda is held in memory and never written to storage", () => {
  // Event records carry titles, venues, and links. The counts cache refuses
  // them, so the agenda cache keeps them for the life of the page instead.
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "UTC");
  const events = [{ canonical_event_id: "one", title: "Chess night", start_at: "2026-08-26" }];
  writeCatalogDayEvents("tenant-a", signature, "2026-08-26", events, "cursor-1", "agenda", 1_000);

  const read = readCatalogDayEvents("tenant-a", signature, "2026-08-26", "agenda", 1_000);
  assert.deepEqual(read?.events, events);
  assert.equal(read?.cursor, "cursor-1");
  for (let index = 0; index < window.sessionStorage.length; index += 1) {
    const key = window.sessionStorage.key(index);
    assert.doesNotMatch(window.sessionStorage.getItem(key) ?? "", /Chess night/);
  }

  // Another account never reads it, and it expires on its own.
  assert.equal(readCatalogDayEvents("tenant-b", signature, "2026-08-26", "agenda", 1_000), null);
  assert.equal(
    readCatalogDayEvents("tenant-a", signature, "2026-08-26", "agenda", 1_000 + 3 * 60 * 1000),
    null,
  );
});

test("clearing the cache drops held agendas too", () => {
  const signature = catalogSummarySignature(filters(), "UTC");
  writeCatalogDayEvents("tenant-a", signature, "2026-08-26", [], null, "agenda", 1_000);
  clearCatalogCache();
  assert.equal(readCatalogDayEvents("tenant-a", signature, "2026-08-26", "agenda", 1_000), null);
});

test("a summarized day renders the same categories events would have produced", () => {
  const day = {
    start_day: "2026-08-25",
    event_count: 279,
    topics: [
      { topic: "education", label: "Education", event_count: 65 },
      { topic: "other", label: "Other", event_count: 72 },
      { topic: "family", label: "Family", event_count: 66 },
    ],
  };

  assert.deepEqual(calendarSummaryDayCategories(day), [
    { topic: "other", label: "Other", eventCount: 72 },
    { topic: "family", label: "Family", eventCount: 66 },
    { topic: "education", label: "Education", eventCount: 65 },
  ]);
  assert.equal(calendarSummaryDayCategories(day, 2).length, 2);
  assert.deepEqual(calendarSummaryDayCategories(undefined), []);
});

test("the color key totals the summarized range and keeps empty selections visible", () => {
  const cached = summary([
    {
      start_day: "2026-08-25",
      event_count: 10,
      topics: [
        { topic: "music", label: "Music", event_count: 4 },
        { topic: "other", label: "Other", event_count: 6 },
      ],
    },
    {
      start_day: "2026-08-26",
      event_count: 3,
      topics: [{ topic: "music", label: "Music", event_count: 3 }],
    },
  ]);

  assert.deepEqual(
    calendarSummaryLegendTopics(cached, [{ topic: "music", label: "Music", event_count: 99 }], []),
    // `other` is not a selectable topic, so it never appears as a filter.
    [{ topic: "music", label: "Music", event_count: 7 }],
  );
  assert.deepEqual(
    calendarSummaryLegendTopics(cached, [], ["sports"]),
    [
      { topic: "sports", label: "Sports", event_count: 0 },
      { topic: "music", label: "Music", event_count: 7 },
    ],
  );
});

test("day lookup is keyed by the summary's own local date", () => {
  const byDate = calendarSummaryByDate(
    summary([{ start_day: "2026-08-25", event_count: 279, topics: [] }]),
  );
  assert.equal(byDate.get("2026-08-25")?.event_count, 279);
  assert.equal(byDate.get("2026-08-26"), undefined);
  assert.equal(calendarSummaryByDate(null).size, 0);
});

test("a range read is clipped to the range it asked for", () => {
  // A range matches events by interval overlap, so an event that began earlier
  // and runs into the range is returned too, counted on its own start day —
  // outside the range. That count covers only the overlap, so it is neither a
  // true total for that day nor part of this range's total.
  const fetched = summary([
    { start_day: "2026-07-31", event_count: 1, topics: [] },
    { start_day: "2026-08-01", event_count: 15, topics: [] },
    { start_day: "2026-08-31", event_count: 2, topics: [] },
  ]);
  assert.equal(fetched.total_event_count, 18);

  const clipped = clipCalendarSummary(fetched, "2026-08-01", "2026-08-31");
  assert.deepEqual(clipped.days.map((day) => day.start_day), ["2026-08-01", "2026-08-31"]);
  assert.equal(clipped.total_event_count, 17, "the leaked day is not part of this range");
  // Nothing to drop means the same object, so a clip cannot churn React state.
  assert.equal(clipCalendarSummary(clipped, "2026-08-01", "2026-08-31"), clipped);
});

test("a clipped read and a cached slice of a wider read agree exactly", () => {
  // The whole reason to clip: a total must not shift when a cache entry expires.
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "America/Los_Angeles");
  const wide = clipCalendarSummary(
    summary([
      { start_day: "2026-07-31", event_count: 1, topics: [] },
      { start_day: "2026-08-15", event_count: 4, topics: [] },
      { start_day: "2026-11-02", event_count: 6, topics: [] },
    ]),
    "2026-08-01",
    "2027-01-31",
  );
  writeCatalogSummary("tenant-a", signature, "2026-08-01", "2027-01-31", wide, 1_000);

  const sliced = readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", 1_000);
  const freshMonth = clipCalendarSummary(
    summary([
      { start_day: "2026-07-31", event_count: 1, topics: [] },
      { start_day: "2026-08-15", event_count: 4, topics: [] },
    ]),
    "2026-08-01",
    "2026-08-31",
  );
  assert.equal(sliced?.summary.total_event_count, freshMonth.total_event_count);
  assert.deepEqual(
    sliced?.summary.days.map((day) => day.start_day),
    freshMonth.days.map((day) => day.start_day),
  );
});

test("a week's own first day never becomes the month's count for that day", () => {
  // A range read drops events starting exactly at the instant it opens, so its
  // FIRST day comes back short. Reusing that short reading as an interior day of
  // some other range would paint a wrong count and call it authoritative.
  // Reproduces the real numbers: 2 August is 145 inside a month, 138 as the
  // first day of the week beginning 2 August.
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "America/Los_Angeles");
  writeCatalogSummary(
    "tenant-a", signature, "2026-08-02", "2026-08-08",
    summary([
      { start_day: "2026-08-02", event_count: 138, topics: [] },
      { start_day: "2026-08-03", event_count: 217, topics: [] },
    ]),
    1_000,
  );

  // The month opens on 1 August and has no reading of its own yet.
  const month = readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", 1_000);
  assert.equal(month?.covered, false, "the month must read its own first day, not the week's");

  // The same week asked again is served: a fresh read would answer identically.
  const week = readCatalogSummary("tenant-a", signature, "2026-08-02", "2026-08-08", 1_000);
  assert.equal(week?.covered, true);
  assert.equal(week?.summary.total_event_count, 355);

  // Once the month has been read, its own first-day reading is what is served.
  writeCatalogSummary(
    "tenant-a", signature, "2026-08-01", "2026-08-31",
    summary([
      { start_day: "2026-08-01", event_count: 265, topics: [] },
      { start_day: "2026-08-02", event_count: 145, topics: [] },
      { start_day: "2026-08-03", event_count: 217, topics: [] },
    ]),
    1_100,
  );
  const after = readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", 1_100);
  assert.equal(after?.covered, true);
  assert.deepEqual(
    after?.summary.days.map((day) => [day.start_day, day.event_count]),
    [["2026-08-01", 265], ["2026-08-02", 145], ["2026-08-03", 217]],
    "the month serves the month's own reading of 2 August, not the week's short one",
  );
  // ...and the week still serves the week's own reading of its first day.
  const weekAgain = readCatalogSummary("tenant-a", signature, "2026-08-02", "2026-08-08", 1_100);
  assert.equal(weekAgain?.summary.days[0].event_count, 138);
});

test("a six-month read still serves the month that opens with it", () => {
  // The win this cache exists for: same opening day, so a fresh read agrees.
  clearCatalogCache();
  const signature = catalogSummarySignature(filters(), "America/Los_Angeles");
  writeCatalogSummary(
    "tenant-a", signature, "2026-08-01", "2027-01-31",
    summary([
      { start_day: "2026-08-01", event_count: 265, topics: [] },
      { start_day: "2026-08-15", event_count: 4, topics: [] },
      { start_day: "2026-11-02", event_count: 6, topics: [] },
    ]),
    1_000,
  );
  const month = readCatalogSummary("tenant-a", signature, "2026-08-01", "2026-08-31", 1_000);
  assert.equal(month?.covered, true, "the month opens on the same day the six months did");
  assert.equal(month?.summary.total_event_count, 269);
});
