import { filterDateKeys, parseLocalDate } from "./date.ts";
import { locationScope } from "./location-scopes.ts";
import { formatCity } from "./presentation.ts";
import type { CatalogFilters, CatalogProvider, CatalogTopic } from "./types.ts";

/**
 * The label the Save control offers before the reader edits it.
 *
 * A saved filter is recognised by what it selects, so the name is built from the selection in the
 * order the chips read: place, topic, source, price, then dates. It is a starting point, not a key
 * — the reader can replace it entirely, and two selections may legitimately want the same name
 * until the server refuses the duplicate.
 */
const MAX_GENERATED_NAME_LENGTH = 80;

function placeLabel(filters: CatalogFilters): string | null {
  const scopes = filters.locationScopes.map((scope) => locationScope(scope)?.label ?? scope);
  const cities = filters.cities.map(formatCity).filter(Boolean);
  const places = [...scopes, ...cities];
  if (!places.length) return null;
  if (places.length <= 2) return places.join(" & ");
  return `${places[0]} +${places.length - 1}`;
}

function topicLabel(filters: CatalogFilters, topics: CatalogTopic[]): string | null {
  if (!filters.topics.length) return null;
  const labels = new Map(topics.map((topic) => [topic.topic, topic.label]));
  const named = filters.topics.map((topic) => labels.get(topic) ?? topic);
  if (named.length <= 2) return named.join(" & ");
  return `${named[0]} +${named.length - 1}`;
}

function sourceLabel(filters: CatalogFilters, providers: CatalogProvider[]): string | null {
  if (!filters.sourceKeys.length) return null;
  const labels = new Map(providers.map((provider) => [provider.source_key, provider.display_name]));
  const named = filters.sourceKeys.map((key) => labels.get(key) ?? key);
  if (named.length === 1) return named[0];
  return `${named[0]} +${named.length - 1}`;
}

function priceLabel(filters: CatalogFilters): string | null {
  const minimum = filters.priceMinDollars.trim();
  const maximum = filters.priceMaxDollars.trim();
  if (filters.price === "free") return "free";
  if (filters.price === "unknown") return "price unlisted";
  if (filters.priceComparison === "at-most" && maximum) return `under $${maximum}`;
  if (filters.priceComparison === "at-least" && minimum) return `over $${minimum}`;
  if (filters.priceComparison === "exactly" && minimum) return `at $${minimum}`;
  if (filters.priceComparison === "between" && minimum && maximum) {
    return `$${minimum}-$${maximum}`;
  }
  return filters.price === "paid" ? "paid" : null;
}

function availabilityLabel(filters: CatalogFilters): string | null {
  if (filters.availability === "available") return "registration open";
  if (filters.availability === "sold_out") return "sold out";
  return null;
}

function dateLabel(filters: CatalogFilters, now: Date): string | null {
  const ranges = filters.dateRanges ?? [];
  if (ranges.length > 1) return `${ranges.length} date ranges`;
  // A preset is named by its own word; only an arbitrary window needs its dates spelled out.
  if (!ranges.length) {
    if (filters.datePreset === "all") return null;
    if (filters.datePreset === "today") return "today";
    if (filters.datePreset === "week") return "this week";
    if (filters.datePreset === "nextweek") return "next week";
    if (filters.datePreset === "workweek") return "work week";
    if (filters.datePreset === "nextworkweek") return "next work week";
    if (filters.datePreset === "weekend") return "this weekend";
    if (filters.datePreset === "nextweekend") return "next weekend";
    if (filters.datePreset === "month") return "this month";
    if (filters.datePreset === "source") return "source history";
  }
  const range = ranges[0] ?? filterDateKeys(filters, now);
  const start = parseLocalDate(range.start);
  const end = parseLocalDate(range.end);
  if (!start || !end) return null;
  const semantic = filterDateKeys({ ...filters, datePreset: "week", dateRanges: [] }, now);
  if (range.start === semantic.start && range.end === semantic.end) return "this week";
  const formatter = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  if (range.start === range.end) return formatter.format(start);
  return `${formatter.format(start)}–${formatter.format(end)}`;
}

/**
 * What the selection actually selects, one readable part per filter.
 *
 * The offered name is these parts joined, so a reader who renames a saved filter to something of
 * their own can still be shown what it holds without the two descriptions drifting apart.
 */
export function describeCatalogSelection(
  filters: CatalogFilters,
  providers: CatalogProvider[] = [],
  topics: CatalogTopic[] = [],
  now: Date = new Date(),
): string[] {
  const parts = [
    placeLabel(filters),
    topicLabel(filters, topics),
    sourceLabel(filters, providers),
    priceLabel(filters),
    availabilityLabel(filters),
    dateLabel(filters, now),
  ].filter((part): part is string => Boolean(part));
  const query = filters.query.trim();
  if (query) parts.unshift(query);
  return parts;
}

export function suggestSavedFilterName(
  filters: CatalogFilters,
  providers: CatalogProvider[] = [],
  topics: CatalogTopic[] = [],
  now: Date = new Date(),
): string {
  const name = describeCatalogSelection(filters, providers, topics, now).join(" · ");
  if (!name) return "All events";
  return name.length > MAX_GENERATED_NAME_LENGTH
    ? `${name.slice(0, MAX_GENERATED_NAME_LENGTH - 1).trimEnd()}…`
    : name;
}

/** Make a generated name unique against what the tenant already saved. */
export function uniqueSavedFilterName(name: string, taken: Iterable<string>): string {
  const used = new Set([...taken].map((value) => value.trim().toLocaleLowerCase()));
  if (!used.has(name.trim().toLocaleLowerCase())) return name;
  for (let suffix = 2; suffix < 100; suffix += 1) {
    const candidate = `${name} (${suffix})`;
    if (!used.has(candidate.toLocaleLowerCase())) return candidate;
  }
  return name;
}
