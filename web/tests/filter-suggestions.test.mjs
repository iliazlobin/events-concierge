import assert from "node:assert/strict";
import test from "node:test";

import { semanticDateRangeKeys } from "../lib/date.ts";
import {
  filterSmartFilterSuggestionsByContext,
  getCatalogNameFilterSuggestions,
  getPopularSmartFilterSuggestions,
  getSmartFilterSuggestions,
  getStarterFilterSuggestions,
  parseSmartFilterComposerQuery,
  REGISTRATION_FILTER_OPTIONS,
} from "../lib/filter-suggestions.ts";

const CITIES = [
  "oakland",
  "paloalto",
  "sanfrancisco",
  "southsanfrancisco",
  "newyork",
];

const PROVIDERS = [
  {
    source_key: "luma-sf",
    label: "Luma Bay Area",
    display_name: "Luma Bay Area",
    publisher: "Luma Discover",
    provider: "luma",
    seed_url: "https://api.luma.com/discover/sf",
    event_count: 67,
  },
  {
    source_key: "palo-alto-library",
    label: "Palo Alto City Library Events",
    display_name: "Palo Alto City Library Events",
    publisher: "Palo Alto City Library",
    provider: "bibliocommons",
    seed_url: "https://paloalto.bibliocommons.com/events",
    event_count: 21,
  },
  {
    source_key: "contra-costa-library",
    label: "Contra Costa County Library Events",
    display_name: "Contra Costa County Library Events",
    publisher: "Contra Costa County Library",
    provider: "bibliocommons",
    seed_url: "https://ccclib.bibliocommons.com/events",
    event_count: 1167,
  },
];
const TOPICS = [
  { topic: "ai", label: "AI", event_count: 42 },
  { topic: "sports", label: "Sports & Fitness", event_count: 31 },
  { topic: "volleyball", label: "Volleyball", event_count: 8 },
];
const NOW = new Date(2026, 6, 28, 12, 0, 0);

test("catalog names retain their known types and are separate from facet composition", () => {
  const names = getCatalogNameFilterSuggestions("nebius", [
    { name: "Nebius", kinds: ["host", "organization"], event_count: 3 },
    { name: "Nebius Studio", kinds: ["venue"], event_count: 1 },
    { name: "Unrelated", kinds: ["speaker"], event_count: 4 },
  ]);
  assert.deepEqual(names.map(({ label, nameKind, description }) => ({ label, nameKind, description })), [
    { label: "Nebius", nameKind: "Organization", description: "Host · 3 events" },
    { label: "Nebius Studio", nameKind: "Venue", description: "1 event" },
  ]);
  assert.ok(names[0].score > names[1].score);
  for (const context of ["place", "source", "topic", "date", "price", "availability"]) {
    assert.deepEqual(filterSmartFilterSuggestionsByContext(names, context), []);
  }
});

test("catalog name matching preserves non-Latin names and meaningful punctuation", () => {
  for (const [query, name] of [["東京", "東京 Community"], ["++", "C++ Builders"], ["%_", "A%_B Collective"]]) {
    assert.equal(getCatalogNameFilterSuggestions(query, [
      { name, kinds: ["organizer"], event_count: 1 },
    ])[0].value, name);
  }
});

test("category-first expressions separate the filter kind from its term", () => {
  assert.deepEqual(
    parseSmartFilterComposerQuery("place"),
    { context: "place", term: "" },
  );
  assert.deepEqual(
    parseSmartFilterComposerQuery("place san francisco"),
    { context: "place", term: "san francisco" },
  );
  assert.deepEqual(
    parseSmartFilterComposerQuery("city: south san francisco"),
    { context: "place", term: "south san francisco" },
  );
  assert.deepEqual(
    parseSmartFilterComposerQuery("vendor luma"),
    { context: "source", term: "luma" },
  );
  assert.deepEqual(
    parseSmartFilterComposerQuery("when next weekend"),
    { context: "date", term: "next weekend" },
  );
  assert.equal(parseSmartFilterComposerQuery("Beyond Prompts"), null);
});

test("bare categories expose popular values before a term is typed", () => {
  const places = getPopularSmartFilterSuggestions(
    "place",
    CITIES,
    PROVIDERS,
    TOPICS,
  );
  const sources = getPopularSmartFilterSuggestions(
    "source",
    CITIES,
    PROVIDERS,
    TOPICS,
  );
  const topics = getPopularSmartFilterSuggestions(
    "topic",
    CITIES,
    PROVIDERS,
    TOPICS,
  );
  const dates = getPopularSmartFilterSuggestions(
    "date",
    CITIES,
    PROVIDERS,
    TOPICS,
    6,
    NOW,
  );

  assert.deepEqual(
    places.slice(0, 2).map(({ kind, value }) => ({ kind, value })),
    [
      { kind: "city", value: "sanfrancisco" },
      { kind: "city", value: "oakland" },
    ],
  );
  assert.equal(sources[0]?.kind, "provider");
  assert.equal(sources[0]?.value, "contra-costa-library");
  assert.equal(topics[0]?.kind, "topic");
  assert.equal(topics[0]?.value, "ai");
  assert.equal(dates[0]?.kind, "date");
  assert.equal(dates[0]?.value, "today");
});

test("empty search puts both Tech Week calendars first without losing existing starting points", () => {
  const suggestions = getStarterFilterSuggestions("oakland");

  assert.equal(suggestions.length, 8);
  assert.ok(
    suggestions.filter((suggestion) => suggestion.kind === "combination").length >= 3,
  );

  assert.deepEqual(suggestions.slice(0, 2).map(({ label, filters }) => ({
    label,
    filters: filters.map(({ kind, value }) => ({ kind, value })),
  })), [
    { label: "SF Tech Week 2026", filters: [{ kind: "provider", value: "tech-week-sf-2026" }] },
    { label: "LA Tech Week 2026", filters: [{ kind: "provider", value: "tech-week-la-2026" }] },
  ]);

  const freeThisWeek = suggestions[2];
  assert.equal(freeThisWeek?.kind, "combination");
  assert.equal(freeThisWeek?.label, "Free in Oakland this week");
  assert.deepEqual(
    freeThisWeek?.kind === "combination"
      ? freeThisWeek.filters.map(({ kind, value }) => ({ kind, value }))
      : [],
    [
      { kind: "city", value: "oakland" },
      { kind: "price", value: "free" },
      { kind: "date", value: "week" },
    ],
  );

  const regionalWeekend = suggestions[3];
  assert.deepEqual(
    regionalWeekend?.kind === "combination"
      ? regionalWeekend.filters.map(({ kind, value }) => ({ kind, value }))
      : [],
    [
      { kind: "scope", value: "bay_area" },
      { kind: "date", value: "weekend" },
    ],
  );
});

test("registration-open phrases select the existing confirmed-open API value", () => {
  for (const query of ["registration open", "registration still open", "still open", "open for registration", "available"]) {
    const suggestion = getSmartFilterSuggestions(query, CITIES, PROVIDERS)[0];
    assert.equal(suggestion?.kind, "availability", query);
    assert.equal(suggestion?.value, "available", query);
    assert.equal(suggestion?.label, "Registration open", query);
  }
  assert.deepEqual(REGISTRATION_FILTER_OPTIONS, [
    { value: "available", label: "Registration open" },
    { value: "sold_out", label: "Sold out" },
  ]);
  assert.equal(getSmartFilterSuggestions("waitlist", CITIES, PROVIDERS).some(
    (suggestion) => suggestion.kind === "availability" && suggestion.value === "available",
  ), false);
});

test("the registration composer exposes and scopes registration statuses", () => {
  assert.deepEqual(parseSmartFilterComposerQuery("registration"), { context: "availability", term: "" });
  assert.deepEqual(parseSmartFilterComposerQuery("registration still open"), { context: "availability", term: "still open" });
  assert.deepEqual(parseSmartFilterComposerQuery("registration status: sold out"), { context: "availability", term: "sold out" });
  const popular = getPopularSmartFilterSuggestions("availability", CITIES, PROVIDERS, TOPICS);
  assert.deepEqual(popular.map(({ kind, value }) => ({ kind, value })), [
    { kind: "availability", value: "available" },
    { kind: "availability", value: "sold_out" },
  ]);
  const suggestions = getSmartFilterSuggestions("open", CITIES, PROVIDERS, [{ topic: "open-source", label: "Open source", event_count: 20 }]);
  const scoped = filterSmartFilterSuggestionsByContext(suggestions, "availability");
  assert.equal(scoped.length, 1);
  assert.equal(scoped[0].value, "available");
});

test("San Francisco shorthand produces a city filter suggestion", () => {
  const [suggestion] = getSmartFilterSuggestions("san fran", CITIES, PROVIDERS);

  assert.deepEqual(
    { kind: suggestion?.kind, value: suggestion?.value, label: suggestion?.label },
    { kind: "city", value: "sanfrancisco", label: "San Francisco" },
  );
});

test("a category term ranks only matching values from that category", () => {
  const expression = parseSmartFilterComposerQuery("place san");
  assert.ok(expression);
  const matches = filterSmartFilterSuggestionsByContext(
    getSmartFilterSuggestions(
      expression.term,
      [...CITIES, "sanbruno", "sanmateo", "sanramon", "sanjose"],
      PROVIDERS,
      TOPICS,
      100,
    ),
    expression.context,
  );

  assert.equal(matches[0]?.kind, "city");
  assert.equal(matches[0]?.value, "sanfrancisco");
  assert.ok(matches.every(({ kind }) => kind === "city" || kind === "scope"));
});

test("every category prefix isolates its own facet vocabulary", () => {
  const cases = [
    ["city palo", "city", "paloalto"],
    ["source palo", "provider", "palo-alto-library"],
    ["category ai", "topic", "ai"],
    ["date next week", "date", "nextweek"],
    ["price free", "price", "free"],
  ];

  for (const [query, expectedKind, expectedValue] of cases) {
    const expression = parseSmartFilterComposerQuery(query);
    assert.ok(expression, query);
    const matches = filterSmartFilterSuggestionsByContext(
      getSmartFilterSuggestions(
        expression.term,
        CITIES,
        PROVIDERS,
        TOPICS,
        100,
        NOW,
      ),
      expression.context,
    );
    assert.equal(matches[0]?.kind, expectedKind, query);
    assert.equal(matches[0]?.value, expectedValue, query);
    assert.ok(
      matches.every(({ kind }) => (
        expression.context === "place"
          ? kind === "city" || kind === "scope"
          : kind === expectedKind
      )),
      query,
    );
  }
});

test("partial provider names and filter concepts are suggested", () => {
  const provider = getSmartFilterSuggestions("luma bay", CITIES, PROVIDERS)[0];
  const weekend = getSmartFilterSuggestions("weekend", CITIES, PROVIDERS)[0];
  const free = getSmartFilterSuggestions("free", CITIES, PROVIDERS)[0];

  assert.equal(provider?.kind, "provider");
  assert.equal(provider?.value, "luma-sf");
  assert.equal(weekend?.kind, "date");
  assert.equal(weekend?.value, "weekend");
  assert.equal(free?.kind, "price");
  assert.equal(free?.value, "free");
});

test("catalog topics are available through the smart search", () => {
  const ai = getSmartFilterSuggestions("show me ai events", CITIES, PROVIDERS, TOPICS)[0];
  const volleyball = getSmartFilterSuggestions("volleyball", CITIES, PROVIDERS, TOPICS)[0];

  assert.deepEqual(
    { kind: ai?.kind, value: ai?.value, label: ai?.label },
    { kind: "topic", value: "ai", label: "AI" },
  );
  assert.deepEqual(
    { kind: volleyball?.kind, value: volleyball?.value, label: volleyball?.label },
    { kind: "topic", value: "volleyball", label: "Volleyball" },
  );
});

test("currency and natural ceilings produce maximum-price suggestions", () => {
  for (const [query, expected] of [
    ["$10", "10"],
    ["under $25", "25"],
    ["up to 50 dollars", "50"],
    ["at most 12.50 usd", "12.5"],
  ]) {
    const suggestion = getSmartFilterSuggestions(
      query,
      CITIES,
      PROVIDERS,
      TOPICS,
    )[0];
    assert.deepEqual(
      {
        kind: suggestion?.kind,
        value: suggestion?.value,
        maximumDollars: suggestion?.kind === "price"
          ? suggestion.maximumDollars
          : undefined,
        label: suggestion?.label,
      },
      {
        kind: "price",
        value: "any",
        maximumDollars: expected,
        label: `Up to $${expected}`,
      },
      query,
    );
  }
});

test("the active default phrase still resolves to this week instead of weekend", () => {
  const [suggestion] = getSmartFilterSuggestions("this we", CITIES, PROVIDERS);

  assert.deepEqual(
    { kind: suggestion?.kind, value: suggestion?.value, label: suggestion?.label },
    { kind: "date", value: "week", label: "This week" },
  );
});

test("common natural filter phrases resolve without leaking filler words", () => {
  const free = getSmartFilterSuggestions("show me free events", CITIES, PROVIDERS)[0];
  const city = getSmartFilterSuggestions("events in san fran", CITIES, PROVIDERS)[0];
  const allSources = getSmartFilterSuggestions("all sources", CITIES, PROVIDERS)[0];
  const anyPrice = getSmartFilterSuggestions("any price", CITIES, PROVIDERS)[0];

  assert.deepEqual(
    { kind: free?.kind, value: free?.value },
    { kind: "price", value: "free" },
  );
  assert.deepEqual(
    { kind: city?.kind, value: city?.value },
    { kind: "city", value: "sanfrancisco" },
  );
  assert.deepEqual(
    { kind: allSources?.kind, value: allSources?.value },
    { kind: "provider", value: "" },
  );
  assert.deepEqual(
    { kind: anyPrice?.kind, value: anyPrice?.value },
    { kind: "price", value: "any" },
  );
});

test("a phrase with a rolling window of its own gets the window, not pinned dates", () => {
  // "Next week" and "next weekend" used to resolve to pinned custom windows. They resolve to the
  // same days -- but as presets, so a selection kept with one does not go stale.
  for (const [phrase, value, expected] of [
    ["next week", "nextweek", { start: "2026-08-03", end: "2026-08-09" }],
    ["next weekend", "nextweekend", { start: "2026-08-08", end: "2026-08-09" }],
    ["next work week", "nextworkweek", { start: "2026-08-03", end: "2026-08-07" }],
  ]) {
    const match = getSmartFilterSuggestions(phrase, CITIES, PROVIDERS, TOPICS, 6, NOW)[0];
    assert.equal(match?.kind, "date", phrase);
    assert.equal(match?.value, value, phrase);
    // A preset carries no window of its own; it is resolved against the day it is read.
    assert.equal(match?.kind === "date" ? match.customStart : "set", undefined, phrase);
    assert.deepEqual(semanticDateRangeKeys(value, "", "", NOW), expected, phrase);
  }
});

test("relative dates produce exact custom windows", () => {
  const tomorrow = getSmartFilterSuggestions(
    "tomorrow",
    CITIES,
    PROVIDERS,
    TOPICS,
    6,
    NOW,
  )[0];
  const nextMonth = getSmartFilterSuggestions(
    "next month",
    CITIES,
    PROVIDERS,
    TOPICS,
    6,
    NOW,
  )[0];

  assert.deepEqual(
    {
      kind: tomorrow?.kind,
      label: tomorrow?.label,
      start: tomorrow?.kind === "date" ? tomorrow.customStart : undefined,
      end: tomorrow?.kind === "date" ? tomorrow.customEnd : undefined,
    },
    {
      kind: "date",
      label: "Tomorrow",
      start: "2026-07-29",
      end: "2026-07-29",
    },
  );
  // A phrase with no rolling window of its own is still pinned to the days it means today.
  assert.deepEqual(
    {
      kind: nextMonth?.kind,
      label: nextMonth?.label,
      start: nextMonth?.kind === "date" ? nextMonth.customStart : undefined,
      end: nextMonth?.kind === "date" ? nextMonth.customEnd : undefined,
    },
    {
      kind: "date",
      label: "Next month",
      start: "2026-08-01",
      end: "2026-08-31",
    },
  );
});

test("named areas and neighborhoods are distinct smart location suggestions", () => {
  const bayArea = getSmartFilterSuggestions("bay area", CITIES, PROVIDERS)[0];
  const manhattan = getSmartFilterSuggestions("manhattan", CITIES, PROVIDERS)[0];
  const losAngeles = getSmartFilterSuggestions("la area", CITIES, PROVIDERS)[0];

  assert.deepEqual(
    {
      kind: bayArea?.kind,
      value: bayArea?.value,
      scopeKind: bayArea?.kind === "scope" ? bayArea.scopeKind : undefined,
    },
    { kind: "scope", value: "bay_area", scopeKind: "Area" },
  );
  assert.deepEqual(
    {
      kind: manhattan?.kind,
      value: manhattan?.value,
      scopeKind: manhattan?.kind === "scope" ? manhattan.scopeKind : undefined,
    },
    { kind: "scope", value: "manhattan", scopeKind: "Neighborhood" },
  );
  assert.equal(losAngeles?.kind, "scope");
  assert.equal(losAngeles?.value, "los_angeles_area");
});

test("active filter editing keeps suggestions within the selected filter type", () => {
  const mixed = getSmartFilterSuggestions(
    "San Francisco",
    CITIES,
    [
      ...PROVIDERS,
      {
        source_key: "sf-rec-park",
        label: "San Francisco Recreation & Parks Events",
        display_name: "San Francisco Recreation & Parks Events",
        publisher: "San Francisco Recreation & Parks",
        provider: "rss",
        seed_url: "https://sfrecpark.org/events",
        event_count: 16,
      },
    ],
    TOPICS,
    100,
  );

  const places = filterSmartFilterSuggestionsByContext(mixed, "place");
  assert.ok(places.length > 0);
  assert.ok(places.every(({ kind }) => kind === "city" || kind === "scope"));
  assert.ok(places.some(({ kind, value }) => kind === "city" && value === "sanfrancisco"));
  assert.ok(!places.some(({ kind }) => kind === "provider"));

  assert.ok(
    filterSmartFilterSuggestionsByContext(mixed, "source")
      .every(({ kind }) => kind === "provider"),
  );
});

function priceComposerSuggestions(input) {
  const parsed = parseSmartFilterComposerQuery(input);
  const context = parsed?.context ?? null;
  const term = parsed?.term ?? input;
  const candidates = context && !term.trim()
    ? getPopularSmartFilterSuggestions(context, CITIES, PROVIDERS, [], 6)
    : getSmartFilterSuggestions(
      term,
      CITIES,
      PROVIDERS,
      [],
      context ? 100 : 6,
      undefined,
      { context },
    );
  return filterSmartFilterSuggestionsByContext(candidates, context).slice(0, 6);
}

test("a bare amount inside the price composer sets a ceiling", () => {
  // Typing "price" and then a number used to return nothing at all: the UI
  // confirmed it understood "price", then went blank on the number.
  for (const [input, expected] of [
    ["price 25", "25"],
    ["budget 40", "40"],
    ["cost 15", "15"],
    ["price 5", "5"],
    ["price 12.50", "12.5"],
  ]) {
    const [suggestion] = priceComposerSuggestions(input);
    assert.equal(suggestion?.kind, "price", `${input} should suggest a price`);
    assert.equal(suggestion?.maximumDollars, expected, `${input} should cap at ${expected}`);
    assert.equal(suggestion?.value, "any");
  }
});

test("the price composer still reads amounts that announce themselves", () => {
  for (const input of ["price $25", "price under 25", "price 25 bucks", "cost up to 25"]) {
    const [suggestion] = priceComposerSuggestions(input);
    assert.equal(suggestion?.maximumDollars, "25", `${input} should cap at 25`);
  }
});

test("a bare number outside the price composer is not a price", () => {
  // 2026, a street number, or part of a title is far likelier than a ceiling.
  const labels = getSmartFilterSuggestions("25", CITIES, PROVIDERS).map((s) => s.label);
  assert.deepEqual(labels.filter((label) => label.startsWith("Up to")), []);
  const year = getSmartFilterSuggestions("2026", CITIES, PROVIDERS).map((s) => s.label);
  assert.deepEqual(year.filter((label) => label.startsWith("Up to")), []);
});

test("opening the price composer shows that an amount is an option", () => {
  const labels = priceComposerSuggestions("price").map((s) => s.label);
  assert.ok(labels.includes("Free"), "the categories stay");
  assert.ok(
    labels.some((label) => label.startsWith("Up to $")),
    "a ceiling must be visible without knowing the phrasing",
  );
  const ceiling = priceComposerSuggestions("price").find((s) => s.label.startsWith("Up to $"));
  assert.equal(ceiling?.value, "any");
  assert.ok(Number(ceiling?.maximumDollars) > 0);
});

test("an implausible amount is refused rather than clamped", () => {
  assert.deepEqual(
    priceComposerSuggestions("price 9999999").filter((s) => s.label.startsWith("Up to")),
    [],
  );
});
