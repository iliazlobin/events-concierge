import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  describeChatIntent,
  interpretCatalogFilterExpression,
  interpretChatRequest,
} from "../lib/chat.ts";
import { filterDateKeys } from "../lib/date.ts";

const CONCIERGE_APP = readFileSync(
  new URL("../components/concierge-app.tsx", import.meta.url),
  "utf8",
);

const NOW = new Date(2026, 6, 28, 12, 0, 0);
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
    source_key: "luma-nyc",
    label: "Luma New York",
    display_name: "Luma New York",
    publisher: "Luma Discover",
    provider: "luma",
    seed_url: "https://api.luma.com/discover/nyc",
    event_count: 76,
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
  { topic: "board-games", label: "Board games", event_count: 12 },
  { topic: "chess", label: "Chess", event_count: 5 },
  { topic: "music", label: "Music", event_count: 31 },
  { topic: "technology", label: "Technology", event_count: 29 },
  { topic: "founders", label: "Founders", event_count: 18 },
];
const BASE_FILTERS = {
  query: "",
  datePreset: "week",
  customStart: "",
  customEnd: "",
  dateRanges: [],
  sourceKeys: [],
  city: "sanfrancisco",
  cities: ["sanfrancisco"],
  locationScopes: [],
  topics: [],
  price: "any",
  priceMaxDollars: "",
  availability: "any",
};

function context(now = NOW, baseFilters) {
  return { topics: TOPICS, now, ...(baseFilters ? { baseFilters } : {}) };
}

test("SF shorthand becomes a city filter rather than a search term", () => {
  const result = interpretChatRequest("sf events today", CITIES, PROVIDERS, context());

  assert.deepEqual(result.filters, {
    query: "",
    sort: "soonest",
    datePreset: "today",
    customStart: "2026-07-28",
    customEnd: "2026-07-28",
    sourceKeys: [],
    city: "sanfrancisco",
    cities: ["sanfrancisco"],
    locationScopes: [],
    topics: [],
    price: "any",
    priceComparison: "any",
    priceMinDollars: "",
    priceMaxDollars: "",
    availability: "any",
  });
  assert.equal(describeChatIntent(result), "today in San Francisco");
});

test("full multi-word cities win over nested city names", () => {
  const result = interpretChatRequest(
    "events in South San Francisco today",
    CITIES,
    PROVIDERS,
    context(),
  );

  assert.equal(result.filters.city, "southsanfrancisco");
  assert.equal(result.filters.query, "");
  assert.equal(result.intent.cityLabel, "South San Francisco");
});

test("topic, city, price, and weekday compose without leaking intent words into query", () => {
  const result = interpretChatRequest(
    "free jazz in Oakland Friday night",
    CITIES,
    PROVIDERS,
    context(),
  );

  assert.equal(result.filters.query, "");
  assert.deepEqual(result.filters.topics, ["music"]);
  assert.equal(result.filters.city, "oakland");
  assert.equal(result.filters.price, "free");
  assert.equal(result.filters.datePreset, "custom");
  assert.equal(result.filters.customStart, "2026-07-31");
  assert.equal(result.filters.customEnd, "2026-07-31");
  assert.equal(
    describeChatIntent(result),
    "friday in Oakland · matching “Music” · free only",
  );
});

test("NYC and relative future windows are normalized", () => {
  const result = interpretChatRequest(
    "nyc tech next weekend",
    CITIES,
    PROVIDERS,
    context(),
  );

  assert.equal(result.filters.city, "newyork");
  assert.equal(result.filters.query, "");
  assert.deepEqual(result.filters.topics, ["technology"]);
  assert.equal(result.filters.customStart, "2026-08-08");
  assert.equal(result.filters.customEnd, "2026-08-09");
});

test("tomorrow and next week become exact inclusive date filters", () => {
  const tomorrow = interpretChatRequest(
    "events in Palo Alto tomorrow",
    CITIES,
    PROVIDERS,
    context(),
  );
  const nextWeek = interpretChatRequest(
    "founder events next week",
    CITIES,
    PROVIDERS,
    context(),
  );

  assert.deepEqual(filterDateKeys(tomorrow.filters, NOW), {
    start: "2026-07-29",
    end: "2026-07-29",
  });
  assert.deepEqual(filterDateKeys(nextWeek.filters, NOW), {
    start: "2026-08-03",
    end: "2026-08-09",
  });
});

test("a unique source phrase becomes a provider filter", () => {
  const result = interpretChatRequest(
    "Contra Costa County Library events this month",
    CITIES,
    PROVIDERS,
    context(),
  );

  assert.deepEqual(result.filters.sourceKeys, ["contra-costa-library"]);
  assert.equal(result.filters.query, "");
  assert.deepEqual(filterDateKeys(result.filters, NOW), {
    start: "2026-07-28",
    end: "2026-07-31",
  });
});

test("a generic provider shared by multiple sources stays a catalog query", () => {
  const result = interpretChatRequest("Luma events this week", CITIES, PROVIDERS, context());

  assert.deepEqual(result.filters.sourceKeys, []);
  assert.equal(result.filters.query, "luma");
});

test("preset windows expose the exact dates used by the API", () => {
  const week = interpretChatRequest("events this week", CITIES, PROVIDERS, context());
  const weekend = interpretChatRequest("events this weekend", CITIES, PROVIDERS, context());

  assert.deepEqual(filterDateKeys(week.filters, NOW), {
    start: "2026-07-28",
    end: "2026-08-02",
  });
  assert.deepEqual(filterDateKeys(weekend.filters, NOW), {
    start: "2026-08-01",
    end: "2026-08-02",
  });
});

test("AI chat intent uses a structured topic, remaining week, and active city", () => {
  const sundayBoundary = new Date(2026, 7, 2, 12, 0, 0);
  const result = interpretChatRequest(
    "An AI meetup this week",
    CITIES,
    PROVIDERS,
    context(sundayBoundary, BASE_FILTERS),
  );

  assert.equal(result.filters.query, "");
  assert.deepEqual(result.filters.topics, ["ai"]);
  assert.equal(result.filters.datePreset, "week");
  assert.deepEqual(filterDateKeys(result.filters, sundayBoundary), {
    start: "2026-08-02",
    end: "2026-08-02",
  });
  assert.equal(result.filters.city, "sanfrancisco");
  assert.deepEqual(result.filters.cities, ["sanfrancisco"]);
  assert.equal(
    describeChatIntent(result),
    "this week in San Francisco · matching “AI”",
  );
});

test("catalog composer commits topic and date clauses as independent filters", () => {
  const result = interpretCatalogFilterExpression(
    "music this week",
    CITIES,
    PROVIDERS,
    TOPICS,
    BASE_FILTERS,
    NOW,
  );

  assert.deepEqual(result.appliedKinds, ["topic", "date"]);
  assert.equal(result.filters.query, "");
  assert.deepEqual(result.filters.topics, ["music"]);
  assert.deepEqual(result.filters.cities, ["sanfrancisco"]);
  assert.equal(result.filters.datePreset, "week");
  assert.equal(result.filters.customStart, "2026-07-28");
  assert.equal(result.filters.customEnd, "2026-08-02");
  assert.deepEqual(result.filters.dateRanges, []);
});

test("chess and board games map to specific predetermined topic facets", () => {
  const chess = interpretChatRequest("chess this week", CITIES, PROVIDERS, context());
  const boardGames = interpretChatRequest("board games this week", CITIES, PROVIDERS, context());

  assert.deepEqual(chess.filters.topics, ["chess"]);
  assert.deepEqual(boardGames.filters.topics, ["board-games"]);
  assert.equal(chess.filters.query, "");
  assert.equal(boardGames.filters.query, "");
});

test("chat submission leaves explicit catalog filters unchanged", () => {
  const start = CONCIERGE_APP.indexOf("const handleChat = async");
  const end = CONCIERGE_APP.indexOf("const handleSignOut =", start);
  assert.ok(start >= 0 && end > start);
  const submission = CONCIERGE_APP.slice(start, end);
  assert.match(submission, /streamChatTurn\(text/);
  assert.doesNotMatch(submission, /applyFilters\(|setFilters\(|pushConsumerSnapshot\(/);
});
