import type {
  CatalogFilters,
  DatePreset,
  DateRangeFilter,
  DateWindow,
} from "./types.ts";

const DAY_MS = 24 * 60 * 60 * 1000;

export function startOfLocalDay(value = new Date()): Date {
  const next = new Date(value);
  next.setHours(0, 0, 0, 0);
  return next;
}

export function addDays(value: Date, days: number): Date {
  const next = new Date(value);
  next.setDate(next.getDate() + days);
  return next;
}

function endExclusive(value: Date): Date {
  return addDays(startOfLocalDay(value), 1);
}

export function localDateKey(value: Date | string): string {
  const date = typeof value === "string" ? new Date(value) : value;
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function parseLocalDate(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  return localDateKey(date) === value ? date : null;
}

export function dateRangeFilterKey(start: string, end: string): string {
  return `${start}..${end}`;
}

/**
 * Canonicalize additive date ranges at every persistence boundary.
 *
 * Invalid ranges are ignored, reversed ranges are ordered, duplicate calendar
 * windows collapse to the first occurrence, and ids remain stable when valid.
 */
export function normalizeDateRangeFilters(
  ranges: readonly DateRangeFilter[] | null | undefined,
): DateRangeFilter[] {
  const normalized: DateRangeFilter[] = [];
  const seenRanges = new Set<string>();
  const seenIds = new Set<string>();

  for (const range of ranges ?? []) {
    if (!range || typeof range !== "object") continue;
    const parsedStart = parseLocalDate(range.start);
    const parsedEnd = parseLocalDate(range.end);
    if (!parsedStart || !parsedEnd) continue;

    const start = localDateKey(parsedStart <= parsedEnd ? parsedStart : parsedEnd);
    const end = localDateKey(parsedStart <= parsedEnd ? parsedEnd : parsedStart);
    const key = dateRangeFilterKey(start, end);
    if (seenRanges.has(key)) continue;

    const requestedId = typeof range.id === "string" ? range.id.trim() : "";
    const id = requestedId && !seenIds.has(requestedId) ? requestedId : key;
    const requestedLabel = typeof range.label === "string" ? range.label.trim() : "";
    normalized.push({
      id,
      start,
      end,
      ...(requestedLabel ? { label: requestedLabel } : {}),
    });
    seenRanges.add(key);
    seenIds.add(id);
  }

  return normalized;
}

export function serializeDateRangeFilter(range: DateRangeFilter): string {
  return dateRangeFilterKey(range.start, range.end);
}

/**
 * Serialize a calendar range for catalog transport using this browser's local
 * midnight boundaries. The persisted URL stays date-only, but the API needs
 * absolute instants so UTC midnight cannot pull the prior local evening into
 * a selected day.
 */
export function serializeDateRangeFilterForApi(range: DateRangeFilter): string {
  const normalized = normalizeDateRangeFilters([range])[0];
  if (!normalized) throw new Error("catalog date range is invalid");
  const start = parseLocalDate(normalized.start);
  const inclusiveEnd = parseLocalDate(normalized.end);
  if (!start || !inclusiveEnd) throw new Error("catalog date range is invalid");
  return `${start.toISOString()}..${endExclusive(inclusiveEnd).toISOString()}`;
}

export function parseDateRangeParameter(value: string): DateRangeFilter | null {
  const separator = value.indexOf("..");
  if (separator < 0 || value.indexOf("..", separator + 2) >= 0) return null;
  const start = value.slice(0, separator);
  const end = value.slice(separator + 2);
  return normalizeDateRangeFilters([{
    id: dateRangeFilterKey(start, end),
    start,
    end,
  }])[0] ?? null;
}

/** Migrate the old singular custom range without changing preset semantics. */
export function legacyCustomDateRange(filters: CatalogFilters): DateRangeFilter[] {
  if (filters.datePreset !== "custom") return [];
  return normalizeDateRangeFilters([{
    id: dateRangeFilterKey(filters.customStart, filters.customEnd),
    start: filters.customStart,
    end: filters.customEnd,
  }]);
}

function isWorkday(day: Date): boolean {
  const index = day.getDay();
  return index >= 1 && index <= 5;
}

/**
 * Days from `day` to the Saturday of the weekend it belongs to.
 *
 * A weekend already under way is the one you are in, not the one after it: asked on a Sunday,
 * "this weekend" still means the Saturday that has just gone.
 */
function saturdayOffset(day: Date): number {
  const index = day.getDay();
  return index === 0 ? -1 : (6 - index + 7) % 7;
}

/**
 * Days from `day` to the Monday of the working week it belongs to.
 *
 * Backwards within a working week, forwards from a weekend: on a Saturday the working week worth
 * asking about is the one that has not started yet, not the one that just ended.
 */
function mondayOffset(day: Date): number {
  const index = day.getDay();
  if (index === 0) return 1;
  if (index === 6) return 2;
  return -(index - 1);
}

export function dateWindow(
  preset: DatePreset,
  customStart = "",
  customEnd = "",
  now = new Date(),
): DateWindow {
  const today = startOfLocalDay(now);
  const currentMoment = new Date(now);

  if (preset === "all") {
    const first = new Date(today.getFullYear() - 9, today.getMonth(), 1);
    return {
      start: first,
      end: new Date(today.getFullYear() + 9, today.getMonth() + 1, 1),
      label: "All dates",
    };
  }

  if (preset === "today") {
    return { start: currentMoment, end: addDays(today, 1), label: "Today" };
  }

  if (preset === "week") {
    const mondayOffset = (today.getDay() + 6) % 7;
    const monday = addDays(today, -mondayOffset);
    return {
      start: currentMoment,
      end: addDays(monday, 7),
      label: "This week",
    };
  }

  if (preset === "nextweek") {
    const monday = addDays(today, -((today.getDay() + 6) % 7) + 7);
    return { start: monday, end: addDays(monday, 7), label: "Next week" };
  }

  if (preset === "workweek") {
    // Asked on a weekend, the working week that matters is the one about to start.
    const monday = addDays(today, mondayOffset(today));
    return {
      start: isWorkday(today) ? currentMoment : monday,
      end: addDays(monday, 5),
      label: "Work week",
    };
  }

  if (preset === "nextworkweek") {
    // The working week after the one "Work week" means, so the two never name the same days.
    const monday = addDays(today, mondayOffset(today) + 7);
    return { start: monday, end: addDays(monday, 5), label: "Next work week" };
  }

  if (preset === "nextweekend") {
    const saturday = addDays(today, saturdayOffset(today) + 7);
    return { start: saturday, end: addDays(saturday, 2), label: "Next weekend" };
  }

  if (preset === "weekend") {
    const day = today.getDay();
    const saturday = addDays(today, day === 0 ? -1 : (6 - day + 7) % 7);
    const monday = addDays(saturday, 2);
    return {
      start: day === 0 || day === 6 ? currentMoment : saturday,
      end: monday,
      label: "This weekend",
    };
  }

  if (preset === "month") {
    const first = new Date(today.getFullYear(), today.getMonth(), 1);
    return {
      start: currentMoment,
      end: new Date(today.getFullYear(), today.getMonth() + 1, 1),
      label: new Intl.DateTimeFormat(undefined, { month: "long" }).format(first),
    };
  }

  if (preset === "source") {
    const first = new Date(today.getFullYear() - 9, today.getMonth(), 1);
    return {
      start: first,
      end: new Date(today.getFullYear() + 9, today.getMonth() + 1, 1),
      label: "All retained source events",
    };
  }

  const parsedStart = parseLocalDate(customStart) ?? today;
  const requestedEnd = parseLocalDate(customEnd) ?? parsedStart;
  const parsedEnd = requestedEnd < parsedStart ? parsedStart : requestedEnd;
  const formatter = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
  });
  return {
    start: parsedStart,
    end: endExclusive(parsedEnd),
    label: `${formatter.format(parsedStart)} – ${formatter.format(parsedEnd)}`,
  };
}

export function filterWindow(filters: CatalogFilters, now = new Date()): DateWindow {
  return dateWindow(
    filters.datePreset,
    filters.customStart,
    filters.customEnd,
    now,
  );
}

export function filterDateKeys(
  filters: CatalogFilters,
  now = new Date(),
): { start: string; end: string } {
  const window = filterWindow(filters, now);
  return {
    start: localDateKey(window.start),
    end: localDateKey(addDays(window.end, -1)),
  };
}

/**
 * Materialize a named calendar choice as the same explicit inclusive range a
 * person would select in the date picker.  Query presets remain future-facing
 * (they start at the current moment), while composed filter chips need stable
 * calendar boundaries that do not collapse late in a week or weekend.
 */
export function semanticDateRangeKeys(
  preset: DatePreset,
  customStart = "",
  customEnd = "",
  now = new Date(),
): { start: string; end: string } {
  const today = startOfLocalDay(now);

  if (preset === "today") {
    const key = localDateKey(today);
    return { start: key, end: key };
  }

  if (preset === "week") {
    const monday = addDays(today, -((today.getDay() + 6) % 7));
    return { start: localDateKey(monday), end: localDateKey(addDays(monday, 6)) };
  }

  if (preset === "nextweek") {
    const monday = addDays(today, -((today.getDay() + 6) % 7) + 7);
    return { start: localDateKey(monday), end: localDateKey(addDays(monday, 6)) };
  }

  if (preset === "workweek") {
    const monday = addDays(today, mondayOffset(today));
    return { start: localDateKey(monday), end: localDateKey(addDays(monday, 4)) };
  }

  if (preset === "nextworkweek") {
    const monday = addDays(today, mondayOffset(today) + 7);
    return { start: localDateKey(monday), end: localDateKey(addDays(monday, 4)) };
  }

  if (preset === "nextweekend") {
    const saturday = addDays(today, saturdayOffset(today) + 7);
    return { start: localDateKey(saturday), end: localDateKey(addDays(saturday, 1)) };
  }

  if (preset === "weekend") {
    const saturday = addDays(today, today.getDay() === 0 ? -1 : (6 - today.getDay() + 7) % 7);
    return { start: localDateKey(saturday), end: localDateKey(addDays(saturday, 1)) };
  }

  if (preset === "month") {
    const first = new Date(today.getFullYear(), today.getMonth(), 1);
    const last = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: localDateKey(first), end: localDateKey(last) };
  }

  const filters: CatalogFilters = {
    query: "",
    sort: "soonest",
    datePreset: preset,
    customStart,
    customEnd,
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
  };
  return filterDateKeys(filters, now);
}

export function daysBetween(start: Date, end: Date): number {
  return Math.max(1, Math.round((end.valueOf() - start.valueOf()) / DAY_MS));
}

export function formatEventDate(value: string): {
  month: string;
  day: string;
  weekday: string;
} {
  const date = new Date(value);
  return {
    month: new Intl.DateTimeFormat(undefined, { month: "short" })
      .format(date)
      .toUpperCase(),
    day: new Intl.DateTimeFormat(undefined, { day: "2-digit" }).format(date),
    weekday: new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(date),
  };
}

export function formatEventTime(startValue: string, endValue: string | null): string {
  const start = new Date(startValue);
  const time = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
  const startCopy = time.format(start);
  if (!endValue) return startCopy;
  const end = new Date(endValue);
  if (Number.isNaN(end.valueOf())) return startCopy;
  return `${startCopy} – ${time.format(end)}`;
}

export function monthKey(value: Date): string {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}`;
}
