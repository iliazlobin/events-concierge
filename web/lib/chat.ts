import {
  addDays,
  filterDateKeys,
  localDateKey,
  semanticDateRangeKeys,
  startOfLocalDay,
} from "./date.ts";
import { formatCity } from "./presentation.ts";
import type {
  CatalogFilters,
  CatalogProvider,
  CatalogTopic,
  PriceFilter,
} from "./types.ts";

interface TopicIntent {
  pattern: RegExp;
  value: string;
  label: string;
  phrase?: string;
}

interface PhraseMatch {
  phrase: string;
}

interface CityIntent extends PhraseMatch {
  value: string;
  label: string;
}

interface ProviderIntent extends PhraseMatch {
  value: string;
  label: string;
}

interface DateIntent extends PhraseMatch {
  datePreset: CatalogFilters["datePreset"];
  customStart: string;
  customEnd: string;
  label: string;
  explicit: boolean;
}

export interface ChatInterpretation {
  filters: CatalogFilters;
  intent: {
    dateLabel: string;
    explicitDate: boolean;
    cityLabel: string | null;
    providerLabel: string | null;
    topicLabel: string | null;
    priceLabel: string | null;
  };
}

export interface ChatInterpretationContext {
  topics?: CatalogTopic[];
  baseFilters?: CatalogFilters;
  now?: Date;
}

export type CatalogFilterExpressionKind =
  | "place"
  | "source"
  | "topic"
  | "date"
  | "price";

export interface CatalogFilterExpression {
  filters: CatalogFilters;
  appliedKinds: CatalogFilterExpressionKind[];
}

const TOPICS: TopicIntent[] = [
  { pattern: /\b(?:artificial intelligence|ai|agents?)\b/i, value: "ai", label: "AI" },
  { pattern: /\b(?:chess|bughouse)\b/i, value: "chess", label: "Chess" },
  { pattern: /\b(?:board[ -]?games?|tabletop(?: games?| gaming)?|mah[ -]?jongg?|cribbage|backgammon)\b/i, value: "board-games", label: "Board games" },
  { pattern: /\b(?:volleyball|beach volleyball)\b/i, value: "volleyball", label: "Volleyball" },
  { pattern: /\b(?:technology|tech|developers?|coding|hackathons?)\b/i, value: "technology", label: "Technology" },
  { pattern: /\b(?:jazz|live music|music|concerts?)\b/i, value: "music", label: "Music" },
  { pattern: /\b(?:founders?|startups?|entrepreneurs?)\b/i, value: "founders", label: "Founders" },
  { pattern: /\b(?:workshops?|hands-on|bootcamps?|training)\b/i, value: "workshop", label: "Workshop" },
  { pattern: /\b(?:arts?|creative|creativity|gallery|museum|theatre|theater|plays?|dance|dancing|films?|movies?|cinema)\b/i, value: "arts", label: "Arts" },
  { pattern: /\b(?:food|dinner|brunch|cooking)\b/i, value: "food-drink", label: "Food & drink" },
  { pattern: /\b(?:family|kids?|children)\b/i, value: "family", label: "Family" },
  { pattern: /\b(?:networking|network|mixer)\b/i, value: "networking", label: "Networking" },
  { pattern: /\b(?:sports?|running|cycling|basketball|soccer|tennis|pickleball)\b/i, value: "sports", label: "Sports" },
  { pattern: /\b(?:wellness|yoga|meditation|fitness)\b/i, value: "wellness", label: "Wellness" },
  { pattern: /\b(?:classes?|lecture|seminar|storytime|tutoring)\b/i, value: "education", label: "Education" },
  { pattern: /\b(?:gaming|video games?|esports?)\b/i, value: "gaming", label: "Gaming" },
  { pattern: /\b(?:outdoors?|hiking|nature walk|gardening)\b/i, value: "outdoors", label: "Outdoors" },
];

const FALLBACK_STOP_WORDS = new Set([
  "about",
  "any",
  "around",
  "at",
  "best",
  "city",
  "cool",
  "event",
  "events",
  "find",
  "for",
  "free",
  "friday",
  "fun",
  "going",
  "good",
  "happening",
  "interesting",
  "in",
  "local",
  "looking",
  "me",
  "monday",
  "month",
  "near",
  "nearby",
  "next",
  "night",
  "now",
  "of",
  "on",
  "paid",
  "please",
  "popular",
  "price",
  "right",
  "saturday",
  "show",
  "smart",
  "some",
  "something",
  "sunday",
  "the",
  "this",
  "thursday",
  "ticketed",
  "today",
  "tomorrow",
  "tonight",
  "tuesday",
  "unlisted",
  "want",
  "wednesday",
  "week",
  "weekend",
  "what",
  "whats",
  "worth",
]);

const CITY_ALIASES: Array<{
  key: string;
  label: string;
  aliases: string[];
}> = [
  {
    key: "sanfrancisco",
    label: "San Francisco",
    aliases: ["san francisco", "san fran", "sf"],
  },
  {
    key: "newyork",
    label: "New York",
    aliases: ["new york city", "new york", "nyc"],
  },
  {
    key: "sanjose",
    label: "San José",
    aliases: ["san jose", "sj"],
  },
];

function comparisonKey(value: string): string {
  return value.normalize("NFKD").replace(/[^a-z0-9]/gi, "").toLowerCase();
}

function phraseKey(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function containsPhrase(prompt: string, phrase: string): boolean {
  const normalized = phraseKey(phrase);
  return Boolean(normalized) && ` ${prompt} `.includes(` ${normalized} `);
}

function startOfWeek(value: Date): Date {
  const today = startOfLocalDay(value);
  return addDays(today, -((today.getDay() + 6) % 7));
}

function customDateIntent(
  start: Date,
  end: Date,
  label: string,
  phrase: string,
): DateIntent {
  return {
    datePreset: "custom",
    customStart: localDateKey(start),
    customEnd: localDateKey(end),
    label,
    phrase,
    explicit: true,
  };
}

function interpretDate(text: string, now: Date): DateIntent {
  const today = startOfLocalDay(now);
  const weekday = [
    { pattern: /\bsunday\b/i, day: 0, label: "Sunday" },
    { pattern: /\bmonday\b/i, day: 1, label: "Monday" },
    { pattern: /\btuesday\b/i, day: 2, label: "Tuesday" },
    { pattern: /\bwednesday\b/i, day: 3, label: "Wednesday" },
    { pattern: /\bthursday\b/i, day: 4, label: "Thursday" },
    { pattern: /\bfriday\b/i, day: 5, label: "Friday" },
    { pattern: /\bsaturday\b/i, day: 6, label: "Saturday" },
  ].find((item) => item.pattern.test(text));

  if (/\bnext weekend\b/i.test(text)) {
    const upcomingSaturday = addDays(today, (6 - today.getDay() + 7) % 7);
    const nextSaturday = addDays(upcomingSaturday, 7);
    return customDateIntent(
      nextSaturday,
      addDays(nextSaturday, 1),
      "Next weekend",
      "next weekend",
    );
  }
  if (/\bnext week\b/i.test(text)) {
    const nextMonday = addDays(startOfWeek(today), 7);
    return customDateIntent(
      nextMonday,
      addDays(nextMonday, 6),
      "Next week",
      "next week",
    );
  }
  if (/\bnext month\b/i.test(text)) {
    const first = new Date(today.getFullYear(), today.getMonth() + 1, 1);
    const last = new Date(today.getFullYear(), today.getMonth() + 2, 0);
    return customDateIntent(first, last, "Next month", "next month");
  }
  if (/\btomorrow\b/i.test(text)) {
    const tomorrow = addDays(today, 1);
    return customDateIntent(tomorrow, tomorrow, "Tomorrow", "tomorrow");
  }
  if (/\b(?:today|tonight)\b/i.test(text)) {
    return {
      datePreset: "today",
      customStart: "",
      customEnd: "",
      label: /\btonight\b/i.test(text) ? "Tonight" : "Today",
      phrase: /\btonight\b/i.test(text) ? "tonight" : "today",
      explicit: true,
    };
  }
  if (/\bweekend\b/i.test(text)) {
    return {
      datePreset: "weekend",
      customStart: "",
      customEnd: "",
      label: "This weekend",
      phrase: "weekend",
      explicit: true,
    };
  }
  if (/\b(?:this month|month)\b/i.test(text)) {
    return {
      datePreset: "month",
      customStart: "",
      customEnd: "",
      label: "This month",
      phrase: "month",
      explicit: true,
    };
  }
  if (weekday) {
    const date = addDays(today, (weekday.day - today.getDay() + 7) % 7);
    return customDateIntent(date, date, weekday.label, weekday.label);
  }
  if (/\b(?:this week|week)\b/i.test(text)) {
    const week = semanticDateRangeKeys("week", "", "", now);
    return {
      datePreset: "custom",
      customStart: week.start,
      customEnd: week.end,
      label: "This week",
      phrase: "week",
      explicit: true,
    };
  }
  return {
    datePreset: "week",
    customStart: "",
    customEnd: "",
    label: "This week",
    phrase: /\b(?:this week|week)\b/i.test(text) ? "week" : "",
    explicit: /\b(?:this week|week)\b/i.test(text),
  };
}

function interpretTopic(text: string, topics: CatalogTopic[]): TopicIntent | null {
  const prompt = phraseKey(text);
  const facetMatches = topics
    .flatMap((topic) => {
      const phrases = new Set([
        topic.topic.replace(/[_-]+/g, " "),
        topic.label,
      ]);
      return [...phrases]
        .filter((phrase) => containsPhrase(prompt, phrase))
        .map((phrase) => ({
          pattern: /(?:)/,
          value: topic.topic,
          label: topic.label,
          phrase,
        }));
    })
    .sort((left, right) => phraseKey(right.phrase ?? "").length - phraseKey(left.phrase ?? "").length);
  if (facetMatches[0]) return facetMatches[0];

  for (const topic of TOPICS) {
    const match = text.match(topic.pattern);
    if (match?.[0]) return { ...topic, phrase: match[0] };
  }
  return null;
}

function interpretCity(text: string, cities: string[]): CityIntent | null {
  const prompt = phraseKey(text);
  const candidates: CityIntent[] = [];
  for (const city of cities) {
    const label = formatCity(city);
    for (const alias of new Set([label, city.replace(/[_-]+/g, " ")])) {
      if (containsPhrase(prompt, alias)) candidates.push({ value: city, label, phrase: alias });
    }
  }
  for (const group of CITY_ALIASES) {
    const knownValue = cities.find((city) => comparisonKey(city) === group.key) ?? group.key;
    for (const alias of group.aliases) {
      if (containsPhrase(prompt, alias)) {
        candidates.push({ value: knownValue, label: group.label, phrase: alias });
      }
    }
  }
  return candidates.sort((left, right) => (
    phraseKey(right.phrase).length - phraseKey(left.phrase).length
  ))[0] ?? null;
}

function interpretProvider(
  text: string,
  providers: CatalogProvider[],
): ProviderIntent | null {
  const prompt = phraseKey(text);
  const matches: Array<ProviderIntent & { score: number }> = [];
  for (const provider of providers) {
    const candidates = [
      { name: provider.source_key.replace(/-/g, " "), weight: 5 },
      { name: provider.display_name, weight: 4 },
      { name: provider.label, weight: 4 },
      { name: provider.publisher, weight: 3 },
      { name: provider.provider, weight: 1 },
    ];
    for (const candidate of candidates) {
      const phrase = phraseKey(candidate.name);
      if (!phrase || !containsPhrase(prompt, phrase)) continue;
      matches.push({
        value: provider.source_key,
        label: provider.display_name,
        phrase,
        score: phrase.length * 10 + candidate.weight,
      });
    }
  }
  matches.sort((left, right) => right.score - left.score);
  const best = matches[0];
  if (!best) return null;
  const competingSources = new Set(
    matches
      .filter((match) => match.score === best.score)
      .map((match) => match.value),
  );
  return competingSources.size === 1
    ? { value: best.value, label: best.label, phrase: best.phrase }
    : null;
}

function interpretPrice(text: string): {
  value: PriceFilter;
  label: string | null;
  phrase: string;
} {
  const free = /\bfree\b/i.test(text);
  const paid = /\b(?:paid|ticketed)\b/i.test(text);
  const unknown = /\b(?:unlisted|unknown price|price unknown)\b/i.test(text);
  if ([free, paid, unknown].filter(Boolean).length !== 1) {
    return { value: "any", label: null, phrase: "" };
  }
  if (free) return { value: "free", label: "Free only", phrase: "free" };
  if (paid) return { value: "paid", label: "Paid only", phrase: "paid" };
  return { value: "unknown", label: "Price unlisted", phrase: "unlisted" };
}

function fallbackQuery(text: string, consumedPhrases: string[]): string {
  const consumedWords = new Set(
    consumedPhrases.flatMap((phrase) => phraseKey(phrase).split(" ").filter(Boolean)),
  );
  return (
    phraseKey(text)
      .split(" ")
      .find((word) => (
        word.length > 2
        && !FALLBACK_STOP_WORDS.has(word)
        && !consumedWords.has(word)
      ))
    ?? ""
  );
}

export function interpretChatRequest(
  text: string,
  cities: string[],
  providers: CatalogProvider[],
  context: ChatInterpretationContext = {},
): ChatInterpretation {
  const now = context.now ?? new Date();
  const date = interpretDate(text, now);
  const city = interpretCity(text, cities);
  const provider = interpretProvider(text, providers);
  const price = interpretPrice(text);
  const topic = interpretTopic(text, context.topics ?? []);
  const query = topic ? "" : fallbackQuery(text, [
    date.phrase,
    city?.phrase ?? "",
    provider?.phrase ?? "",
    price.phrase,
  ]);
  const baseCities = context.baseFilters?.cities?.length
    ? context.baseFilters.cities
    : context.baseFilters?.city
      ? [context.baseFilters.city]
      : [];
  const defaultCity = context.baseFilters
    ? cities.find((candidate) => comparisonKey(candidate) === "sanfrancisco")
      ?? "sanfrancisco"
    : "";
  const inheritedCities = city
    ? [city.value]
    : baseCities.length
      ? [...baseCities]
      : context.baseFilters?.locationScopes?.length
        ? []
        : defaultCity
          ? [defaultCity]
          : [];
  const inheritedScopes = city
    ? []
    : context.baseFilters?.locationScopes?.length
      ? [...context.baseFilters.locationScopes]
      : [];
  const filters: CatalogFilters = {
    query,
    sort: context.baseFilters?.sort ?? "soonest",
    datePreset: date.datePreset,
    customStart: date.customStart,
    customEnd: date.customEnd,
    sourceKeys: provider ? [provider.value] : [],
    city: inheritedCities[0] ?? "",
    cities: inheritedCities,
    locationScopes: inheritedScopes,
    price: price.value,
    priceComparison: "any",
    priceMinDollars: "",
    priceMaxDollars: "",
    availability: context.baseFilters?.availability ?? "any",
    topics: topic ? [topic.value] : [],
  };
  const resolvedDates = filterDateKeys(filters, now);
  if (filters.datePreset !== "custom") {
    filters.customStart = resolvedDates.start;
    filters.customEnd = resolvedDates.end;
  }

  return {
    filters,
    intent: {
      dateLabel: date.label,
      explicitDate: date.explicit,
      cityLabel: city?.label
        ?? (inheritedCities.length === 1 ? formatCity(inheritedCities[0]) : null),
      providerLabel: provider?.label ?? null,
      topicLabel: topic?.label ?? (query || null),
      priceLabel: price.label,
    },
  };
}

/**
 * Interpret committed text from the catalog's smart filter composer. Unlike a
 * chat request, this keeps unrelated active filters and applies only the
 * dimensions that were explicitly present in the expression.
 */
export function interpretCatalogFilterExpression(
  text: string,
  cities: string[],
  providers: CatalogProvider[],
  topics: CatalogTopic[],
  baseFilters: CatalogFilters,
  now = new Date(),
): CatalogFilterExpression {
  const interpreted = interpretChatRequest(text, cities, providers, { topics, now });
  const parsed = interpreted.filters;
  const appliedKinds: CatalogFilterExpressionKind[] = [];
  const filters: CatalogFilters = {
    ...baseFilters,
    cities: [...baseFilters.cities],
    sourceKeys: [...baseFilters.sourceKeys],
    locationScopes: [...baseFilters.locationScopes],
    topics: [...baseFilters.topics],
    dateRanges: [...(baseFilters.dateRanges ?? [])],
  };

  const parsedCity = interpreted.intent.cityLabel ? parsed.cities[0] : null;
  if (parsedCity) {
    if (!filters.cities.includes(parsedCity)) filters.cities.push(parsedCity);
    filters.city = filters.cities[0] ?? "";
    appliedKinds.push("place");
  }
  const parsedSource = parsed.sourceKeys[0];
  if (parsedSource) {
    if (!filters.sourceKeys.includes(parsedSource)) filters.sourceKeys.push(parsedSource);
    appliedKinds.push("source");
  }
  const parsedTopic = parsed.topics[0];
  if (parsedTopic) {
    if (!filters.topics.includes(parsedTopic)) filters.topics.push(parsedTopic);
    appliedKinds.push("topic");
  }
  if (interpreted.intent.explicitDate) {
    filters.datePreset = parsed.datePreset;
    filters.customStart = parsed.customStart;
    filters.customEnd = parsed.customEnd;
    filters.dateRanges = [];
    appliedKinds.push("date");
  }
  if (interpreted.intent.priceLabel) {
    filters.price = parsed.price;
    filters.priceComparison = parsed.priceComparison;
    filters.priceMinDollars = parsed.priceMinDollars;
    filters.priceMaxDollars = parsed.priceMaxDollars;
    appliedKinds.push("price");
  }

  filters.query = appliedKinds.length ? parsed.query : text.trim();
  return { filters, appliedKinds };
}

export function describeChatIntent(
  interpretation: ChatInterpretation,
  { includeTopic = true }: { includeTopic?: boolean } = {},
): string {
  const { intent } = interpretation;
  let description = intent.dateLabel.toLowerCase();
  if (intent.cityLabel) description += ` in ${intent.cityLabel}`;
  if (intent.providerLabel) description += ` from ${intent.providerLabel}`;
  const qualifiers = [
    includeTopic && intent.topicLabel ? `matching “${intent.topicLabel}”` : null,
    intent.priceLabel?.toLowerCase() ?? null,
  ].filter((value): value is string => Boolean(value));
  return qualifiers.length ? `${description} · ${qualifiers.join(" · ")}` : description;
}
