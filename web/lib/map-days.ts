import { localDateKey, parseLocalDate } from "./date.ts";
import type { EventItem } from "./types.ts";

const DAY_MS = 24 * 60 * 60 * 1000;

/** One day of the map result set. Geometry comes from the filtered set, counts from the viewport. */
export interface MapDayCell {
  key: string;
  /** Short weekday for the cell's identity line. */
  weekday: string;
  /** Two-digit day of month, matching the rail card's date badge. */
  dayOfMonth: string;
  /** Set only when this cell starts a new month, so a long track labels its own boundaries. */
  monthLabel: string | null;
  /** Whole days with no mapped events between the previous cell and this one. */
  gapBefore: number;
  /** Mapped events on this day across the whole result set. */
  total: number;
  /** The subset currently inside the map viewport. */
  inView: number;
}

export interface MapDayModel {
  cells: MapDayCell[];
  keys: string[];
  maxTotal: number;
  totalInView: number;
  activeInView: number;
  activeTotal: number;
}

export interface MapDayGroup {
  key: string;
  events: EventItem[];
}

/**
 * A day belongs to the calendar day its start falls on in this browser's zone — the
 * same rule `CalendarMonthGrid` applies, so the track, the rail sections and the
 * calendar tab never disagree about which day an 11:50pm event is on.
 */
export function eventDayKey(event: Pick<EventItem, "start_at">): string | null {
  const start = new Date(event.start_at);
  if (Number.isNaN(start.valueOf())) return null;
  return localDateKey(start);
}

function formatDayPart(
  key: string,
  options: Intl.DateTimeFormatOptions,
  locale?: Intl.LocalesArgument,
): string {
  const date = parseLocalDate(key);
  if (!date) return key;
  return new Intl.DateTimeFormat(locale, options).format(date);
}

/** "WED · AUG 26" — the rail's day header. */
export function dayTrackLabel(key: string, locale?: Intl.LocalesArgument): string {
  const date = parseLocalDate(key);
  if (!date) return key;
  const weekday = formatDayPart(key, { weekday: "short" }, locale).toUpperCase();
  const month = formatDayPart(key, { month: "short" }, locale).toUpperCase();
  const day = new Intl.DateTimeFormat(locale, { day: "2-digit" }).format(date);
  return `${weekday} · ${month} ${day}`;
}

/** "Wed, Aug 26" — inline prose and marker labels. */
export function dayShortLabel(key: string, locale?: Intl.LocalesArgument): string {
  return formatDayPart(
    key,
    { weekday: "short", month: "short", day: "numeric" },
    locale,
  );
}

/** "Wednesday, August 26" — screen-reader sentences. */
export function dayFullLabel(key: string, locale?: Intl.LocalesArgument): string {
  return formatDayPart(
    key,
    { weekday: "long", month: "long", day: "numeric" },
    locale,
  );
}

/** "Aug 26" — compact enough for a stub line or a fit button. */
export function dayCompactLabel(key: string, locale?: Intl.LocalesArgument): string {
  return formatDayPart(key, { month: "short", day: "numeric" }, locale);
}

/**
 * Build the day track's geometry from `mapped` and its counts from `visibleIds`.
 *
 * Cells derive from the filtered result set and NEVER from the viewport: a control
 * whose own shape reflows under the cursor mid-pan cannot be aimed at. Only
 * `inView` — and therefore the numeral and the coverage bar — re-reads on moveend.
 */
export function mapDayModel(
  mapped: EventItem[],
  visibleIds: Iterable<string>,
  activeDay: string | null,
  locale?: Intl.LocalesArgument,
): MapDayModel {
  const visible = visibleIds instanceof Set
    ? visibleIds as Set<string>
    : new Set(visibleIds);
  const totals = new Map<string, { total: number; inView: number }>();

  for (const event of mapped) {
    // An unparsable start_at would otherwise mint a "NaN-NaN-NaN" day.
    const key = eventDayKey(event);
    if (!key) continue;
    const bucket = totals.get(key) ?? { total: 0, inView: 0 };
    bucket.total += 1;
    if (visible.has(event.canonical_event_id)) bucket.inView += 1;
    totals.set(key, bucket);
  }

  const keys = [...totals.keys()].sort();
  const cells: MapDayCell[] = [];
  let previousDate: Date | null = null;
  let previousMonth: number | null = null;

  for (const key of keys) {
    const date = parseLocalDate(key);
    const bucket = totals.get(key);
    if (!date || !bucket) continue;
    const month = date.getMonth();
    cells.push({
      key,
      weekday: formatDayPart(key, { weekday: "short" }, locale).toUpperCase(),
      dayOfMonth: new Intl.DateTimeFormat(locale, { day: "2-digit" }).format(date),
      monthLabel: previousMonth !== null && previousMonth !== month
        ? formatDayPart(key, { month: "short" }, locale).toUpperCase()
        : null,
      gapBefore: previousDate
        ? Math.max(0, Math.round((date.valueOf() - previousDate.valueOf()) / DAY_MS) - 1)
        : 0,
      total: bucket.total,
      inView: bucket.inView,
    });
    previousDate = date;
    previousMonth = month;
  }

  const active = activeDay ? totals.get(activeDay) : undefined;
  return {
    cells,
    keys: cells.map((cell) => cell.key),
    maxTotal: cells.reduce((peak, cell) => Math.max(peak, cell.total), 0),
    totalInView: cells.reduce((sum, cell) => sum + cell.inView, 0),
    activeInView: active?.inView ?? 0,
    activeTotal: active?.total ?? 0,
  };
}

/**
 * Sections for the rail, chronological ascending regardless of the catalog sort —
 * left-to-right on the track has to mean earlier-to-later in the list.
 */
export function groupVisibleEventsByDay(visibleEvents: EventItem[]): MapDayGroup[] {
  const buckets = new Map<string, EventItem[]>();
  for (const event of visibleEvents) {
    const key = eventDayKey(event);
    if (!key) continue;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(event);
    else buckets.set(key, [event]);
  }
  return [...buckets.keys()].sort().map((key) => ({
    key,
    events: (buckets.get(key) ?? []).slice().sort(
      (left, right) => new Date(left.start_at).valueOf() - new Date(right.start_at).valueOf(),
    ),
  }));
}

/**
 * Walk to the next day that actually has a cell, clamping at both ends. Wrapping
 * would lose the reader's place; clamping makes a held `]` deterministic.
 */
export function stepDay(
  keys: string[],
  current: string | null,
  direction: 1 | -1,
): string | null {
  if (!keys.length) return null;
  if (!current) return direction === 1 ? keys[0] : keys[keys.length - 1];
  const index = keys.indexOf(current);
  if (index < 0) return direction === 1 ? keys[0] : keys[keys.length - 1];
  const next = index + direction;
  if (next < 0 || next >= keys.length) return current;
  return keys[next];
}
