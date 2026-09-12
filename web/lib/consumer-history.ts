import type {
  AvailabilityFilter,
  CalendarMode,
  CatalogFilters,
  CatalogSort,
  DatePreset,
  DateRangeFilter,
  LocationScope,
  PriceComparison,
  PriceFilter,
  ViewName,
} from "@/lib/types";
import {
  legacyCustomDateRange,
  normalizeDateRangeFilters,
  parseDateRangeParameter,
  serializeDateRangeFilter,
} from "./date.ts";

const HISTORY_KEY = "eventsConciergeConsumer";
const HISTORY_VERSION = 6;
const PREVIOUS_HISTORY_VERSION = 5;
const SECOND_PREVIOUS_HISTORY_VERSION = 4;
const THIRD_PREVIOUS_HISTORY_VERSION = 3;
const FOURTH_PREVIOUS_HISTORY_VERSION = 2;
const LEGACY_HISTORY_VERSION = 1;

const VIEWS = new Set<ViewName>(["chat", "events", "map", "calendar", "entities"]);
const CALENDAR_MODES = new Set<CalendarMode>(["week", "month", "six-months"]);
const LEGACY_THREE_MONTH_MODE = "three-months";
const DATE_PRESETS = new Set<DatePreset>([
  "all",
  "today",
  "week",
  "nextweek",
  "workweek",
  "nextworkweek",
  "weekend",
  "nextweekend",
  "month",
  "source",
  "custom",
]);
const PRICES = new Set<PriceFilter>(["any", "free", "paid", "unknown"]);
const AVAILABILITIES = new Set<AvailabilityFilter>(["any", "available", "sold_out"]);
const PRICE_COMPARISONS = new Set<PriceComparison>([
  "any",
  "at-most",
  "at-least",
  "exactly",
  "between",
]);
const SORTS = new Set<CatalogSort>(["soonest", "latest"]);
const LOCATION_SCOPES = new Set<LocationScope>([
  "bay_area",
  "manhattan",
  "los_angeles_area",
]);
const OWNED_QUERY_PARAMETERS = [
  "view",
  "calendar",
  "q",
  "sort",
  "when",
  "start",
  "end",
  "date_range",
  "source",
  "city",
  "area",
  "price",
  "cmp",
  "min",
  "max",
  "availability",
  "topic",
  "event",
  "entity",
] as const;

export interface ConsumerHistorySnapshot {
  version: typeof HISTORY_VERSION;
  view: ViewName;
  calendarMode: CalendarMode;
  filters: CatalogFilters;
  expandedId: string | null;
  selectedEntityId: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

function normalizeCalendarMode(value: unknown): CalendarMode | null {
  if (value === LEGACY_THREE_MONTH_MODE) return "six-months";
  return typeof value === "string" && CALENDAR_MODES.has(value as CalendarMode)
    ? value as CalendarMode
    : null;
}

function copySnapshot(snapshot: ConsumerHistorySnapshot): ConsumerHistorySnapshot {
  return {
    version: HISTORY_VERSION,
    view: snapshot.view,
    calendarMode: snapshot.calendarMode,
    filters: {
      ...snapshot.filters,
      sort: snapshot.filters.sort ?? "soonest",
      dateRanges: normalizeDateRangeFilters(snapshot.filters.dateRanges),
      availability: snapshot.filters.availability ?? "any",
      cities: [...snapshot.filters.cities],
      locationScopes: [...snapshot.filters.locationScopes],
      topics: [...snapshot.filters.topics],
    },
    expandedId: snapshot.expandedId,
    selectedEntityId: snapshot.selectedEntityId,
  };
}

export function createConsumerHistorySnapshot(
  view: ViewName,
  filters: CatalogFilters,
  expandedId: string | null,
  calendarMode: CalendarMode = "month",
  selectedEntityId: string | null = null,
): ConsumerHistorySnapshot {
  return copySnapshot({
    version: HISTORY_VERSION,
    view,
    calendarMode,
    filters,
    expandedId,
    selectedEntityId,
  });
}

export function createConsumerHistoryState(
  snapshot: ConsumerHistorySnapshot,
  currentState: unknown,
): Record<string, unknown> {
  return {
    ...(isRecord(currentState) ? currentState : {}),
    [HISTORY_KEY]: copySnapshot(snapshot),
  };
}

export function readConsumerHistorySnapshot(
  historyState: unknown,
): ConsumerHistorySnapshot | null {
  if (!isRecord(historyState)) return null;
  const candidate = historyState[HISTORY_KEY];
  if (
    !isRecord(candidate)
    || (
      candidate.version !== HISTORY_VERSION
      && candidate.version !== PREVIOUS_HISTORY_VERSION
      && candidate.version !== SECOND_PREVIOUS_HISTORY_VERSION
      && candidate.version !== THIRD_PREVIOUS_HISTORY_VERSION
      && candidate.version !== FOURTH_PREVIOUS_HISTORY_VERSION
      && candidate.version !== LEGACY_HISTORY_VERSION
    )
  ) return null;
  if (typeof candidate.view !== "string" || !VIEWS.has(candidate.view as ViewName)) {
    return null;
  }
  if (
    (
      candidate.version === HISTORY_VERSION
      || candidate.version === PREVIOUS_HISTORY_VERSION
      || candidate.version === SECOND_PREVIOUS_HISTORY_VERSION
    )
    && normalizeCalendarMode(candidate.calendarMode) === null
  ) return null;
  if (candidate.expandedId !== null && typeof candidate.expandedId !== "string") {
    return null;
  }
  if (
    (candidate.version === HISTORY_VERSION || candidate.version === PREVIOUS_HISTORY_VERSION)
    && candidate.selectedEntityId !== null
    && typeof candidate.selectedEntityId !== "string"
  ) return null;
  const filters = candidate.filters;
  if (!isRecord(filters)) return null;
  if (
    typeof filters.query !== "string"
    || (
      (
        candidate.version === HISTORY_VERSION
        || candidate.version === PREVIOUS_HISTORY_VERSION
        || candidate.version === SECOND_PREVIOUS_HISTORY_VERSION
        || candidate.version === THIRD_PREVIOUS_HISTORY_VERSION
      )
      && (typeof filters.sort !== "string" || !SORTS.has(filters.sort as CatalogSort))
    )
    || typeof filters.datePreset !== "string"
    || !DATE_PRESETS.has(filters.datePreset as DatePreset)
    || typeof filters.customStart !== "string"
    || typeof filters.customEnd !== "string"
    || typeof filters.city !== "string"
    || !isStringArray(filters.cities)
    || !isStringArray(filters.locationScopes)
    || !filters.locationScopes.every((scope) => LOCATION_SCOPES.has(scope as LocationScope))
    || typeof filters.price !== "string"
    || !PRICES.has(filters.price as PriceFilter)
    || (
      candidate.version === HISTORY_VERSION
      && (
        typeof filters.availability !== "string"
        || !AVAILABILITIES.has(filters.availability as AvailabilityFilter)
      )
    )
    || typeof filters.priceMaxDollars !== "string"
    || !isStringArray(filters.topics)
  ) {
    return null;
  }
  let dateRanges: DateRangeFilter[] = [];
  if (candidate.version !== LEGACY_HISTORY_VERSION && filters.dateRanges !== undefined) {
    if (!Array.isArray(filters.dateRanges)) return null;
    const rawRanges = filters.dateRanges.filter(isRecord);
    if (rawRanges.length !== filters.dateRanges.length) return null;
    if (!rawRanges.every((range) => (
      typeof range.id === "string"
      && typeof range.start === "string"
      && typeof range.end === "string"
      && (range.label === undefined || typeof range.label === "string")
    ))) return null;
    dateRanges = normalizeDateRangeFilters(rawRanges as unknown as DateRangeFilter[]);
  }
  const migratedFilters = {
    ...(filters as unknown as CatalogFilters),
    sort: (
      candidate.version === HISTORY_VERSION
      || candidate.version === PREVIOUS_HISTORY_VERSION
      || candidate.version === SECOND_PREVIOUS_HISTORY_VERSION
      || candidate.version === THIRD_PREVIOUS_HISTORY_VERSION
    )
      ? filters.sort as CatalogSort
      : "soonest",
    dateRanges,
    availability: candidate.version === HISTORY_VERSION
      ? filters.availability as AvailabilityFilter
      : "any",
  };
  if (candidate.version === LEGACY_HISTORY_VERSION && !dateRanges.length) {
    migratedFilters.dateRanges = legacyCustomDateRange(migratedFilters);
  }
  return createConsumerHistorySnapshot(
    candidate.view as ViewName,
    migratedFilters,
    candidate.expandedId as string | null,
    (
      candidate.version === HISTORY_VERSION
      || candidate.version === PREVIOUS_HISTORY_VERSION
      || candidate.version === SECOND_PREVIOUS_HISTORY_VERSION
    )
      ? normalizeCalendarMode(candidate.calendarMode) ?? "month"
      : "month",
    (candidate.version === HISTORY_VERSION || candidate.version === PREVIOUS_HISTORY_VERSION)
      ? candidate.selectedEntityId as string | null
      : null,
  );
}

export function consumerHistoryUrl(
  snapshot: ConsumerHistorySnapshot,
  currentHref: string,
): string {
  const url = new URL(currentHref);
  for (const name of OWNED_QUERY_PARAMETERS) url.searchParams.delete(name);

  url.searchParams.set("view", snapshot.view);
  if (snapshot.view === "calendar") url.searchParams.set("calendar", snapshot.calendarMode);
  url.searchParams.set("sort", snapshot.filters.sort);
  url.searchParams.set("when", snapshot.filters.datePreset);
  url.searchParams.set("price", snapshot.filters.price);
  if (snapshot.filters.query) url.searchParams.set("q", snapshot.filters.query);
  const dateRanges = normalizeDateRangeFilters(snapshot.filters.dateRanges);
  if (dateRanges.length) {
    for (const range of dateRanges) {
      url.searchParams.append("date_range", serializeDateRangeFilter(range));
    }
  } else if (snapshot.filters.datePreset === "custom") {
    if (snapshot.filters.customStart) url.searchParams.set("start", snapshot.filters.customStart);
    if (snapshot.filters.customEnd) url.searchParams.set("end", snapshot.filters.customEnd);
  }
  for (const sourceKey of snapshot.filters.sourceKeys ?? []) {
    url.searchParams.append("source", sourceKey);
  }
  if ((snapshot.filters.priceComparison ?? "any") !== "any") {
    url.searchParams.set("cmp", snapshot.filters.priceComparison);
  }
  if (snapshot.filters.priceMinDollars) {
    url.searchParams.set("min", snapshot.filters.priceMinDollars);
  }
  if (snapshot.filters.priceMaxDollars) {
    url.searchParams.set("max", snapshot.filters.priceMaxDollars);
  }
  if (snapshot.filters.availability !== "any") {
    url.searchParams.set("availability", snapshot.filters.availability);
  }
  if (snapshot.filters.cities.length) {
    for (const city of snapshot.filters.cities) url.searchParams.append("city", city);
  } else {
    // Absence means the initial city default; an empty value preserves an explicit reset.
    url.searchParams.set("city", "");
  }
  for (const scope of snapshot.filters.locationScopes) {
    url.searchParams.append("area", scope);
  }
  for (const topic of snapshot.filters.topics) url.searchParams.append("topic", topic);
  if (snapshot.expandedId) url.searchParams.set("event", snapshot.expandedId);
  if (snapshot.view === "entities" && snapshot.selectedEntityId) {
    url.searchParams.set("entity", snapshot.selectedEntityId);
  }

  return `${url.pathname}${url.search}${url.hash}`;
}

export function consumerHistorySnapshotFromUrl(
  currentHref: string,
  defaults: CatalogFilters,
): ConsumerHistorySnapshot | null {
  const url = new URL(currentHref);
  if (!OWNED_QUERY_PARAMETERS.some((name) => url.searchParams.has(name))) return null;

  const rawView = url.searchParams.get("view");
  const view = rawView && VIEWS.has(rawView as ViewName)
    ? rawView as ViewName
    : "chat";
  const rawCalendarMode = url.searchParams.get("calendar");
  const calendarMode = normalizeCalendarMode(rawCalendarMode) ?? "month";
  const rawDatePreset = url.searchParams.get("when");
  const datePreset = rawDatePreset && DATE_PRESETS.has(rawDatePreset as DatePreset)
    ? rawDatePreset as DatePreset
    : defaults.datePreset;
  const rawPrice = url.searchParams.get("price");
  const price = rawPrice && PRICES.has(rawPrice as PriceFilter)
    ? rawPrice as PriceFilter
    : defaults.price;
  const rawAvailability = url.searchParams.get("availability");
  const availability = rawAvailability
    && AVAILABILITIES.has(rawAvailability as AvailabilityFilter)
    ? rawAvailability as AvailabilityFilter
    : defaults.availability ?? "any";
  const rawComparison = url.searchParams.get("cmp");
  const priceComparison = rawComparison && PRICE_COMPARISONS.has(rawComparison as PriceComparison)
    ? rawComparison as PriceComparison
    // A link that carries only "max" predates the comparison and still means a ceiling.
    : url.searchParams.get("max")
    ? "at-most"
    : defaults.priceComparison ?? "any";
  const rawSort = url.searchParams.get("sort");
  const sort = rawSort && SORTS.has(rawSort as CatalogSort)
    ? rawSort as CatalogSort
    : defaults.sort ?? "soonest";
  const cities = url.searchParams.has("city")
    ? uniqueStrings(url.searchParams.getAll("city"))
    : [...defaults.cities];
  const locationScopes = uniqueStrings(url.searchParams.getAll("area"))
    .filter((scope): scope is LocationScope => LOCATION_SCOPES.has(scope as LocationScope));
  const dateRanges = url.searchParams.getAll("date_range")
    .map(parseDateRangeParameter)
    .filter((range): range is DateRangeFilter => range !== null);
  const legacyCustomStart = datePreset === "custom" ? url.searchParams.get("start") : null;
  const legacyCustomEnd = datePreset === "custom" ? url.searchParams.get("end") : null;
  const customStart = legacyCustomStart ?? dateRanges[0]?.start ?? "";
  const customEnd = legacyCustomEnd ?? dateRanges[0]?.end ?? "";

  return createConsumerHistorySnapshot(
    view,
    {
      ...defaults,
      query: (url.searchParams.get("q") ?? "").slice(0, 160),
      sort,
      datePreset,
      customStart,
      customEnd,
      dateRanges,
      sourceKeys: uniqueStrings(url.searchParams.getAll("source")),
      city: cities[0] ?? "",
      cities,
      locationScopes,
      price,
      priceComparison,
      priceMinDollars: url.searchParams.get("min") ?? "",
      priceMaxDollars: url.searchParams.get("max") ?? "",
      availability,
      topics: uniqueStrings(url.searchParams.getAll("topic")),
    },
    url.searchParams.get("event"),
    calendarMode,
    view === "entities" ? url.searchParams.get("entity") : null,
  );
}
