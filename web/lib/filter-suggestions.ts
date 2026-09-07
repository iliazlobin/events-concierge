import {
  addDays,
  localDateKey,
  startOfLocalDay,
} from "./date.ts";
import { CATALOG_LOCATION_SCOPES } from "./location-scopes.ts";
import { formatCity } from "./presentation.ts";
import type {
  CatalogProvider,
  CatalogTopic,
  DatePreset,
  LocationScope,
  PriceFilter,
} from "./types.ts";

export type AtomicSmartFilterSuggestion =
  | {
    kind: "city";
    value: string;
    label: string;
    score: number;
  }
  | {
    kind: "provider";
    value: string;
    label: string;
    score: number;
  }
  | {
    kind: "topic";
    value: string;
    label: string;
    score: number;
  }
  | {
    kind: "scope";
    value: LocationScope;
    label: string;
    scopeKind: "Area" | "Neighborhood";
    score: number;
  }
  | {
    kind: "date";
    value: DatePreset;
    label: string;
    score: number;
    customStart?: string;
    customEnd?: string;
  }
  | {
    kind: "price";
    value: PriceFilter;
    label: string;
    score: number;
    maximumDollars?: string;
  };

/**
 * A whole selection the tenant already saved, offered by name.
 *
 * It is not an atomic filter: applying it replaces the entire selection rather than composing with
 * what is active, so it sits alongside the combination kind rather than inside the atomic union.
 */
export interface SavedFilterSuggestion {
  kind: "saved";
  value: string;
  label: string;
  description: string;
  score: number;
}

export type SmartFilterSuggestion = AtomicSmartFilterSuggestion | SavedFilterSuggestion | {
  kind: "combination";
  value: string;
  label: string;
  description: string;
  score: number;
  filters: AtomicSmartFilterSuggestion[];
};

export type SmartFilterContext = "place" | "source" | "topic" | "date" | "price";

export interface SmartFilterComposerQuery {
  context: SmartFilterContext;
  term: string;
}

const FILTER_CONTEXT_ALIASES: Array<{
  context: SmartFilterContext;
  aliases: string[];
}> = [
  {
    context: "place",
    aliases: [
      "place", "places", "city", "cities", "location", "locations",
      "area", "areas", "neighborhood", "neighborhoods",
    ],
  },
  {
    context: "source",
    aliases: [
      "source", "sources", "provider", "providers", "vendor", "vendors",
      "calendar", "calendars",
    ],
  },
  {
    context: "topic",
    aliases: ["topic", "topics", "category", "categories"],
  },
  {
    context: "date",
    aliases: ["date", "dates", "when", "day", "days"],
  },
  {
    context: "price",
    aliases: ["price", "prices", "cost", "costs", "budget"],
  },
];

export function parseSmartFilterComposerQuery(
  query: string,
): SmartFilterComposerQuery | null {
  const trimmed = query.trim();
  if (!trimmed) return null;
  for (const group of FILTER_CONTEXT_ALIASES) {
    for (const alias of group.aliases) {
      const escaped = alias.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
      const match = trimmed.match(new RegExp(`^${escaped}(?:\\s*:\\s*|\\s+)(.*)$`, "i"));
      if (match) return { context: group.context, term: match[1]?.trim() ?? "" };
      if (trimmed.toLocaleLowerCase() === alias) {
        return { context: group.context, term: "" };
      }
    }
  }
  return null;
}

export function filterSmartFilterSuggestionsByContext(
  suggestions: SmartFilterSuggestion[],
  context: SmartFilterContext | null,
): SmartFilterSuggestion[] {
  if (!context) return suggestions;
  return suggestions.filter((suggestion) => {
    // A saved selection answers every context at once, so no single context owns it.
    if (suggestion.kind === "combination" || suggestion.kind === "saved") return false;
    if (context === "place") {
      return suggestion.kind === "city" || suggestion.kind === "scope";
    }
    if (context === "source") return suggestion.kind === "provider";
    return suggestion.kind === context;
  });
}

const POPULAR_CITY_KEYS = [
  "sanfrancisco",
  "oakland",
  "sanjose",
  "newyork",
  "berkeley",
  "losangeles",
] as const;

export function getPopularSmartFilterSuggestions(
  context: SmartFilterContext,
  cities: string[],
  providers: CatalogProvider[],
  topics: CatalogTopic[] = [],
  limit = 6,
  now = new Date(),
): AtomicSmartFilterSuggestion[] {
  if (context === "place") {
    const cityByKey = new Map(cities.map((city) => [comparisonKey(city), city]));
    const popularCities = POPULAR_CITY_KEYS
      .map((key) => cityByKey.get(comparisonKey(key)))
      .filter((city): city is string => Boolean(city))
      .map((city, index) => ({
        kind: "city" as const,
        value: city,
        label: formatCity(city),
        score: 200 - index,
      }));
    const scopes = CATALOG_LOCATION_SCOPES.map((scope, index) => ({
      kind: "scope" as const,
      value: scope.value,
      label: scope.label,
      scopeKind: scope.kind,
      score: 150 - index,
    }));
    return [...popularCities, ...scopes].slice(0, limit);
  }
  if (context === "source") {
    return [...providers]
      .sort((left, right) => (
        right.event_count - left.event_count
        || left.display_name.localeCompare(right.display_name)
      ))
      .slice(0, limit)
      .map((provider, index) => ({
        kind: "provider" as const,
        value: provider.source_key,
        label: provider.display_name,
        score: 200 - index,
      }));
  }
  if (context === "topic") {
    return [...topics]
      .sort((left, right) => (
        right.event_count - left.event_count || left.label.localeCompare(right.label)
      ))
      .slice(0, limit)
      .map((topic, index) => ({
        kind: "topic" as const,
        value: topic.topic,
        label: topic.label,
        score: 200 - index,
      }));
  }
  if (context === "date") {
    return [...DATE_SUGGESTIONS, ...relativeDateSuggestions(now)]
      .slice(0, limit)
      .map((date, index) => ({
        kind: "date" as const,
        value: date.value,
        label: date.label,
        score: 200 - index,
        customStart: date.customStart,
        customEnd: date.customEnd,
      }));
  }
  // Opening the price composer has to show that a number is an option at all.
  // Listing only Free/Paid/Any/Unlisted made the ceiling invisible: a reader had
  // to already know the phrasing to reach it.
  const categories = PRICE_SUGGESTIONS.map((price, index) => ({
    kind: "price" as const,
    value: price.value,
    label: price.label,
    score: 200 - index,
  }));
  const ceilings = COMMON_PRICE_CEILINGS.map((amount, index) => ({
    kind: "price" as const,
    value: "any" as PriceFilter,
    maximumDollars: String(amount),
    label: `Up to $${amount}`,
    score: 190 - index,
  }));
  return [...categories, ...ceilings]
    .sort((left, right) => right.score - left.score)
    .slice(0, limit);
}

const CITY_ALIASES: Array<{
  value: string;
  label: string;
  aliases: string[];
}> = [
  {
    value: "sanfrancisco",
    label: "San Francisco",
    aliases: ["san francisco", "san fran", "sf"],
  },
  {
    value: "newyork",
    label: "New York",
    aliases: ["new york", "new york city", "nyc"],
  },
  {
    value: "sanjose",
    label: "San José",
    aliases: ["san jose", "sj"],
  },
];

interface DateSuggestionCandidate {
  value: DatePreset;
  label: string;
  aliases: string[];
  customStart?: string;
  customEnd?: string;
}

const DATE_SUGGESTIONS: DateSuggestionCandidate[] = [
  {
    value: "today",
    label: "Today",
    aliases: ["today", "tonight", "right now", "now", "happening today"],
  },
  {
    value: "week",
    label: "This week",
    aliases: ["this week", "week", "current week"],
  },
  {
    value: "nextweek",
    label: "Next week",
    aliases: ["next week", "following week", "the week after"],
  },
  {
    value: "workweek",
    label: "Work week",
    aliases: [
      "work week", "workweek", "working week", "weekdays", "weekday",
      "monday to friday", "mon-fri", "mon to fri",
    ],
  },
  {
    value: "nextworkweek",
    label: "Next work week",
    aliases: ["next work week", "next workweek", "next working week", "next weekdays"],
  },
  {
    value: "weekend",
    label: "This weekend",
    aliases: ["this weekend", "weekend", "saturday and sunday", "sat and sun"],
  },
  {
    value: "nextweekend",
    label: "Next weekend",
    aliases: ["next weekend", "the weekend after", "following weekend"],
  },
  {
    value: "month",
    label: "This month",
    aliases: ["this month", "month", "current month"],
  },
];

/** One-tap ceilings offered when the price composer opens, and a hint that any amount works. */
const COMMON_PRICE_CEILINGS = [10, 25, 50, 100] as const;

const PRICE_SUGGESTIONS: Array<{
  value: PriceFilter;
  label: string;
  aliases: string[];
}> = [
  {
    value: "any",
    label: "Any price",
    aliases: ["any price", "all prices", "regardless of price"],
  },
  {
    value: "free",
    label: "Free",
    aliases: ["free", "no cost", "free admission", "complimentary", "zero cost"],
  },
  {
    value: "paid",
    label: "Paid",
    aliases: ["paid", "ticketed", "tickets required", "paid admission"],
  },
  {
    value: "unknown",
    label: "Price unlisted",
    aliases: ["unlisted", "unknown price", "price unknown", "price tbd"],
  },
];

/**
 * The price categories every filter surface offers, in reading order. The chip
 * editor and the search composer must name a category the same way, so the
 * labels live here rather than being restated per surface.
 */
export const PRICE_FILTER_OPTIONS: ReadonlyArray<{
  value: PriceFilter;
  label: string;
}> = Object.freeze(
  PRICE_SUGGESTIONS.map(({ value, label }) => Object.freeze({ value, label })),
);

const FILTER_QUERY_FILLERS = new Set([
  "event",
  "events",
  "find",
  "for",
  "from",
  "give",
  "happening",
  "in",
  "me",
  "near",
  "please",
  "show",
  "showing",
  "source",
  "sources",
]);

/**
 * High-value starting points shown before somebody types. Each item is a
 * complete, atomic filter transaction rather than a link disguised as a
 * search result. The UI may contextualize the first preset with the active
 * city, but never claims to know the visitor's physical location.
 */
export function getStarterFilterSuggestions(
  activeCity = "sanfrancisco",
): SmartFilterSuggestion[] {
  const city = activeCity || "sanfrancisco";
  const cityLabel = formatCity(city);
  return [
    {
      kind: "combination",
      value: "free-this-week",
      label: `Free in ${cityLabel} this week`,
      description: "No-cost events in the active city",
      score: 100,
      filters: [
        { kind: "city", value: city, label: cityLabel, score: 100 },
        { kind: "price", value: "free", label: "Free", score: 100 },
        { kind: "date", value: "week", label: "This week", score: 100 },
      ],
    },
    {
      kind: "combination",
      value: "bay-area-weekend",
      label: "Bay Area this weekend",
      description: "Saturday and Sunday across the region",
      score: 90,
      filters: [
        {
          kind: "scope",
          value: "bay_area",
          label: "Bay Area",
          scopeKind: "Area",
          score: 100,
        },
        { kind: "date", value: "weekend", label: "This weekend", score: 100 },
      ],
    },
    {
      kind: "combination",
      value: "city-this-month",
      label: `${cityLabel} this month`,
      description: "A broader date window in the active city",
      score: 80,
      filters: [
        { kind: "city", value: city, label: cityLabel, score: 100 },
        { kind: "date", value: "month", label: "This month", score: 100 },
      ],
    },
    {
      kind: "combination",
      value: "city-today",
      label: `Today in ${cityLabel}`,
      description: "Events happening today in the active city",
      score: 70,
      filters: [
        { kind: "city", value: city, label: cityLabel, score: 100 },
        { kind: "date", value: "today", label: "Today", score: 100 },
      ],
    },
    {
      kind: "combination",
      value: "ai-this-week",
      label: `AI in ${cityLabel} this week`,
      description: "Artificial intelligence events nearby",
      score: 60,
      filters: [
        { kind: "city", value: city, label: cityLabel, score: 100 },
        { kind: "topic", value: "ai", label: "AI", score: 100 },
        { kind: "date", value: "week", label: "This week", score: 100 },
      ],
    },
    {
      kind: "combination",
      value: "sports-weekend",
      label: `Sports in ${cityLabel} this weekend`,
      description: "Sports and fitness events this weekend",
      score: 50,
      filters: [
        { kind: "city", value: city, label: cityLabel, score: 100 },
        {
          kind: "topic",
          value: "sports",
          label: "Sports & Fitness",
          score: 100,
        },
        { kind: "date", value: "weekend", label: "This weekend", score: 100 },
      ],
    },
  ];
}

function normalized(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function compact(value: string): string {
  return normalized(value).replace(/\s+/g, "");
}

function matchScore(query: string, candidate: string): number {
  const needle = normalized(query);
  const haystack = normalized(candidate);
  if (!needle || !haystack) return 0;
  if (needle === haystack) return 140;
  if (haystack.startsWith(needle)) return 120 - Math.min(20, haystack.length - needle.length);
  if (haystack.includes(needle)) return 90 - Math.min(20, haystack.indexOf(needle));

  const needleWords = needle.split(" ");
  const candidateWords = haystack.split(" ");
  if (needleWords.every((word) => candidateWords.some((part) => part.startsWith(word)))) {
    return 82;
  }

  const compactNeedle = compact(needle);
  const compactCandidate = compact(haystack);
  if (compactCandidate.startsWith(compactNeedle)) return 74;
  if (compactCandidate.includes(compactNeedle)) return 62;
  return 0;
}

function queryVariants(query: string): string[] {
  const full = normalized(query);
  const concise = full
    .split(" ")
    .filter((word) => !FILTER_QUERY_FILLERS.has(word))
    .join(" ");
  return [...new Set([full, concise].filter(Boolean))];
}

function bestScore(query: string, candidates: string[]): number {
  return Math.max(
    0,
    ...queryVariants(query).flatMap((variant) => (
      candidates.map((candidate) => matchScore(variant, candidate))
    )),
  );
}

function comparisonKey(value: string): string {
  return compact(value);
}

/**
 * Read a maximum ticket price out of a query.
 *
 * Outside the price composer an amount must announce itself — `$25`, `under 25`,
 * `25 dollars` — because a bare number is far more often a year, a street
 * number, or part of an event title. Inside the price composer the reader has
 * already said what they are filtering, so `price 25` is unambiguous and is the
 * most natural way to ask for a ceiling.
 */
function maximumPriceSuggestion(
  query: string,
  { allowBareAmount = false }: { allowBareAmount?: boolean } = {},
): Extract<AtomicSmartFilterSuggestion, { kind: "price" }> | null {
  const bareAmount = allowBareAmount
    ? query.trim().match(/^(\d+(?:\.\d{1,2})?)$/)
    : null;
  const currencyAmount = query.match(/\$\s*(\d+(?:\.\d{1,2})?)/i);
  const qualifiedAmount = query.match(
    /\b(?:under|below|up\s+to|at\s+most|less\s+than|no\s+more\s+than|max(?:imum)?(?:\s+of)?)\s*\$?\s*(\d+(?:\.\d{1,2})?)\s*(?:dollars?|usd)?\b/i,
  );
  const namedCurrencyAmount = query.match(
    /\b(\d+(?:\.\d{1,2})?)\s*(?:dollars?|usd|bucks?)\b/i,
  );
  const rawAmount = qualifiedAmount?.[1]
    ?? currencyAmount?.[1]
    ?? namedCurrencyAmount?.[1]
    ?? bareAmount?.[1];
  if (!rawAmount) return null;

  const amount = Number(rawAmount);
  if (!Number.isFinite(amount) || amount < 0 || amount > 1_000_000) return null;
  const maximumDollars = amount.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
  return {
    kind: "price",
    value: "any",
    maximumDollars,
    label: `Up to $${maximumDollars}`,
    score: 180,
  };
}

function startOfWeek(value: Date): Date {
  const today = startOfLocalDay(value);
  return addDays(today, -((today.getDay() + 6) % 7));
}

/**
 * One-off windows named by phrase, pinned to the dates they mean today.
 *
 * "Next week" and "next weekend" used to live here as pinned windows too. They are rolling
 * presets now: the same days, but a selection kept with one still means next week in December.
 */
function relativeDateSuggestions(now: Date): DateSuggestionCandidate[] {
  const today = startOfLocalDay(now);
  const tomorrow = addDays(today, 1);
  const nextMonthStart = new Date(today.getFullYear(), today.getMonth() + 1, 1);
  const nextMonthEnd = new Date(today.getFullYear(), today.getMonth() + 2, 0);
  const weekdayNames = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
  ];
  const weekdays = weekdayNames.map((label, day) => {
    const date = addDays(today, (day - today.getDay() + 7) % 7);
    return {
      value: "custom" as const,
      label,
      aliases: [label.toLowerCase(), `this ${label.toLowerCase()}`],
      customStart: localDateKey(date),
      customEnd: localDateKey(date),
    };
  });
  return [
    {
      value: "custom",
      label: "Tomorrow",
      aliases: ["tomorrow"],
      customStart: localDateKey(tomorrow),
      customEnd: localDateKey(tomorrow),
    },
    {
      value: "custom",
      label: "Next month",
      aliases: ["next month"],
      customStart: localDateKey(nextMonthStart),
      customEnd: localDateKey(nextMonthEnd),
    },
    {
      value: "custom",
      label: "Next 7 days",
      aliases: ["next 7 days", "next seven days"],
      customStart: localDateKey(today),
      customEnd: localDateKey(addDays(today, 6)),
    },
    {
      value: "custom",
      label: "Next 30 days",
      aliases: ["next 30 days", "next thirty days"],
      customStart: localDateKey(today),
      customEnd: localDateKey(addDays(today, 29)),
    },
    ...weekdays,
  ];
}

/**
 * The words that name the category rather than one entry inside it.
 *
 * The other filter kinds already answer their own name -- typing "place" opens the place composer.
 * Saved selections had no such word, so someone asking for them by the only name they have got
 * nothing back unless a selection happened to be *called* "filter".
 */
const SAVED_FILTER_CATEGORY_TERMS = new Set([
  "saved", "save", "saves", "filter", "filters", "savedfilter", "savedfilters",
  "myfilters", "mysavedfilters", "selection", "selections",
  "savedselection", "savedselections", "savedsearch", "savedsearches",
]);

function isSavedFilterCategoryTerm(query: string): boolean {
  return SAVED_FILTER_CATEGORY_TERMS.has(compact(query));
}

/**
 * Match saved selections by name so the search box can reach them by typing.
 *
 * These rank above ordinary filter suggestions on an equal match: a reader who typed the name of
 * something they saved wants that thing, not a coincidentally similar city.
 *
 * A term that names the category asks for the list itself rather than for one entry, so it matches
 * every kept selection and takes more of the list than the three slots a name match is allowed --
 * there is nothing else such a term could have meant.
 */
export function getSavedFilterSuggestions(
  query: string,
  saved: ReadonlyArray<{ saved_filter_id: string; name: string }>,
  limit = 3,
): SavedFilterSuggestion[] {
  const trimmed = query.trim();
  const wholeCategory = isSavedFilterCategoryTerm(trimmed);
  const matches = saved.flatMap((entry) => {
    const score = trimmed && !wholeCategory ? bestScore(trimmed, [entry.name]) : 60;
    return score > 0
      ? [{
        kind: "saved" as const,
        value: entry.saved_filter_id,
        label: entry.name,
        description: "Saved filter",
        // Outrank an equally good ordinary match; a named thing beats a guess.
        score: score + 40,
      }]
      : [];
  });
  // Equal scores keep the order they arrived in, which is the server's recency order.
  return matches
    .sort((left, right) => right.score - left.score)
    .slice(0, wholeCategory ? Math.max(limit, 6) : limit);
}

export function getSmartFilterSuggestions(
  query: string,
  cities: string[],
  providers: CatalogProvider[],
  topics: CatalogTopic[] = [],
  limit = 6,
  now = new Date(),
  { context = null }: { context?: SmartFilterContext | null } = {},
): SmartFilterSuggestion[] {
  // A one-character term is noise everywhere except a price ceiling, where "5"
  // is a complete answer.
  const minimumTermLength = context === "price" ? 1 : 2;
  if (normalized(query).length < minimumTermLength) return [];

  const suggestions: SmartFilterSuggestion[] = [];
  const cityCandidates = new Map<string, {
    value: string;
    label: string;
    aliases: Set<string>;
  }>();

  for (const city of cities) {
    const label = formatCity(city);
    cityCandidates.set(comparisonKey(city), {
      value: city,
      label,
      aliases: new Set([city.replace(/[_-]+/g, " "), label]),
    });
  }

  for (const city of CITY_ALIASES) {
    const existing = cityCandidates.get(comparisonKey(city.value));
    const candidate = existing ?? {
      value: city.value,
      label: city.label,
      aliases: new Set<string>(),
    };
    candidate.aliases.add(city.label);
    for (const alias of city.aliases) candidate.aliases.add(alias);
    cityCandidates.set(comparisonKey(city.value), candidate);
  }

  for (const city of cityCandidates.values()) {
    const score = bestScore(query, [city.label, ...city.aliases]);
    const popularityIndex = POPULAR_CITY_KEYS.indexOf(
      comparisonKey(city.value) as (typeof POPULAR_CITY_KEYS)[number],
    );
    const popularityBoost = popularityIndex >= 0 ? 30 - (popularityIndex * 3) : 0;
    if (score) suggestions.push({
      kind: "city",
      value: city.value,
      label: city.label,
      score: score + 4 + popularityBoost,
    });
  }

  const everywhereScore = bestScore(query, [
    "Everywhere",
    "All cities",
    "Any city",
    "Anywhere",
  ]);
  if (everywhereScore) suggestions.push({
    kind: "city",
    value: "",
    label: "Everywhere",
    score: everywhereScore + 4,
  });

  for (const scope of CATALOG_LOCATION_SCOPES) {
    const score = bestScore(query, [scope.label, ...scope.aliases]);
    if (score) suggestions.push({
      kind: "scope",
      value: scope.value,
      label: scope.label,
      scopeKind: scope.kind,
      score: score + 5,
    });
  }

  for (const provider of providers) {
    const score = bestScore(query, [
      provider.display_name,
      provider.label,
      provider.publisher,
      provider.source_key.replace(/-/g, " "),
    ]);
    if (score) suggestions.push({
      kind: "provider",
      value: provider.source_key,
      label: provider.display_name,
      score: score + 2,
    });
  }

  const allSourcesScore = bestScore(query, ["All sources", "Any source", "Every source"]);
  if (allSourcesScore) suggestions.push({
    kind: "provider",
    value: "",
    label: "All sources",
    score: allSourcesScore + 2,
  });

  const seenTopics = new Set<string>();
  for (const topic of topics) {
    const value = topic.topic.trim();
    if (!value || seenTopics.has(comparisonKey(value))) continue;
    seenTopics.add(comparisonKey(value));
    const label = topic.label.trim() || value;
    const score = bestScore(query, [label, value.replace(/[_-]+/g, " ")]);
    if (score) suggestions.push({
      kind: "topic",
      value,
      label,
      score: score + 3,
    });
  }

  for (const date of [...DATE_SUGGESTIONS, ...relativeDateSuggestions(now)]) {
    const score = bestScore(query, [date.label, ...date.aliases]);
    if (score) suggestions.push({
      kind: "date",
      value: date.value,
      label: date.label,
      score,
      customStart: date.customStart,
      customEnd: date.customEnd,
    });
  }

  for (const price of PRICE_SUGGESTIONS) {
    const score = bestScore(query, [price.label, ...price.aliases]);
    if (score) suggestions.push({
      kind: "price",
      value: price.value,
      label: price.label,
      score,
    });
  }

  const maximumPrice = maximumPriceSuggestion(query, {
    allowBareAmount: context === "price",
  });
  if (maximumPrice) suggestions.push(maximumPrice);

  return suggestions
    .sort((left, right) => right.score - left.score || left.label.localeCompare(right.label))
    .slice(0, limit);
}
