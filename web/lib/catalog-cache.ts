import { catalogFilterKey } from "./catalog-filters.ts";
import { addDays, localDateKey, parseLocalDate } from "./date.ts";
import type { CatalogDay, CatalogDaySummary, CatalogFilters, EventItem } from "./types.ts";

/**
 * A short-lived, per-tab cache for calendar day counts.
 *
 * The calendar re-derives the same counts on every mount, so a reload used to
 * repeat the entire range read even though nothing about the filter changed.
 * Caching the counts makes a reload paint immediately.
 *
 * The cache is keyed by the filter *without* its date window, and it records
 * which day ranges it has actually read. A day's count is decided by that day
 * alone, so the same filters asked over a wider window answer with the identical
 * count for every day a narrower window held: one six-month read therefore
 * already contains the month and week grids inside it, and switching between
 * those modes is a slice of memory rather than a round trip. Keying by the exact
 * window instead — as this cache first did — made every mode switch and every
 * step of the range arrows a guaranteed miss.
 *
 * Deliberate boundaries:
 *
 * - `sessionStorage`, not `localStorage`: the cache dies with the tab, so a
 *   shared machine cannot surface one person's catalog view to the next.
 * - keyed by tenant: a different account never reads a cached range, and
 *   {@link clearCatalogCache} runs on sign-out and account erasure.
 * - short TTL: the catalog refreshes continuously, so an entry older than the
 *   TTL is still painted, but only ahead of a revalidation that replaces it.
 * - counts only. Event records, which carry titles, venues, and links, are never
 *   written to storage. The agenda cache below holds them in memory alone, for
 *   the life of the page.
 *
 * One day of every answer is not window-independent. A range read drops events
 * that start exactly at the instant it opens — an all-day event on the range's
 * own first day — so that first day comes back short, and only for that day.
 * (Asked as a month, 2 August counts 145; asked as the first day of a week, 138.)
 * The first day is therefore kept apart from the rest as an "anchor" and reused
 * only for a range that opens on the same day, where a fresh read would answer
 * identically. Without that separation, visiting a few weeks and then switching
 * to the month would paint each week's short Sunday as the month's own count.
 */

const CACHE_PREFIX = "events-concierge.catalog-summary.v3";
const CACHE_TTL_MS = 5 * 60 * 1000;
/** Bounded so a long browsing session cannot fill the origin's storage quota. */
const MAX_CACHE_ENTRIES = 12;
/**
 * A single entry stops accumulating coverage past this many days with events.
 * Reached only by paging the arrows across years; the newest read then starts a
 * fresh entry rather than growing one without limit.
 */
const MAX_CACHE_DAYS = 800;
/** One anchor per range the reader has opened; bounded for the same reason. */
const MAX_CACHE_ANCHORS = 64;

/**
 * An inclusive span of local calendar days, as `YYYY-MM-DD`, and when it was
 * read.
 *
 * The age belongs to the span rather than to the entry: paging the arrows keeps
 * writing, and one entry-wide timestamp would let a range read an hour ago look
 * as fresh as the one just fetched beside it.
 */
export interface CachedDayRange {
  start: string;
  end: string;
  at: number;
}

/** A range's own first day, which only another range opening that day may reuse. */
interface CachedAnchor {
  at: number;
  /** Null when that day held no events. */
  day: CatalogDay | null;
}

interface CachedDays {
  at: number;
  timeZone: string;
  /** Merged, sorted spans read as INTERIOR days, so never a range's first day. */
  ranges: CachedDayRange[];
  /** Only days that hold events. A day inside `ranges` but absent here holds none. */
  days: Record<string, CatalogDay>;
  /** First-day readings, keyed by the day the range opened on. */
  anchors: Record<string, CachedAnchor>;
}

export interface CachedCatalogSummary {
  /** The cached counts, sliced to the requested span. */
  summary: CatalogDaySummary;
  /** True when every day of the requested span has actually been read. */
  covered: boolean;
  /** True once the entry is older than the TTL; callers may paint it but must revalidate. */
  stale: boolean;
}

/**
 * Every filter that changes which events a day contains takes part in the key.
 * The date window does not: it selects which days are read, never how many
 * events a day holds. Presentation-only state (sort, the expanded card) is
 * excluded for the same reason ordering cannot change a count.
 */
export function catalogSummarySignature(
  filters: CatalogFilters,
  timeZone: string,
): string {
  // One definition of "which events does this describe", shared with the request
  // key, so the cache and the fetch can never disagree about what changed.
  return JSON.stringify([
    timeZone,
    catalogFilterKey(filters, { includeSort: false, includeDateWindow: false }),
  ]);
}

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    // Private modes and storage-partitioned embeds can refuse access outright.
    return null;
  }
}

function cacheKey(tenantId: string | null, signature: string): string {
  return `${CACHE_PREFIX}:${tenantId ?? "anonymous"}:${signature}`;
}

function isCatalogDay(value: unknown): value is CatalogDay {
  if (!value || typeof value !== "object") return false;
  const day = value as Partial<CatalogDay>;
  return typeof day.start_day === "string"
    && typeof day.event_count === "number"
    && Array.isArray(day.topics);
}

function isCachedDays(value: unknown): value is CachedDays {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<CachedDays>;
  if (typeof candidate.at !== "number" || !Number.isFinite(candidate.at)) return false;
  if (typeof candidate.timeZone !== "string") return false;
  if (!Array.isArray(candidate.ranges)) return false;
  if (!candidate.ranges.every((range) => (
    range
    && typeof range.start === "string"
    && typeof range.end === "string"
    && typeof range.at === "number"
    && Number.isFinite(range.at)
  ))) return false;
  const days = candidate.days;
  if (!days || typeof days !== "object") return false;
  if (!Object.values(days).every(isCatalogDay)) return false;
  const anchors = candidate.anchors;
  if (!anchors || typeof anchors !== "object") return false;
  return Object.values(anchors).every((anchor) => (
    anchor
    && typeof anchor.at === "number"
    && Number.isFinite(anchor.at)
    && (anchor.day === null || isCatalogDay(anchor.day))
  ));
}

/** The day after `key`, in the same `YYYY-MM-DD` form. */
function nextDayKey(key: string): string {
  const parsed = parseLocalDate(key);
  return parsed ? localDateKey(addDays(parsed, 1)) : key;
}

/**
 * Merge spans that overlap or merely touch. Two spans that meet end-to-start
 * describe one continuous read, so they must fuse: otherwise stepping the arrows
 * a month at a time would leave a seam the grid reads as unknown.
 *
 * A fused span keeps the *older* reading time, so merging can never make a
 * range look fresher than its oldest part actually is.
 */
function mergeRanges(ranges: CachedDayRange[]): CachedDayRange[] {
  const sorted = [...ranges]
    .filter((range) => range.start <= range.end)
    .sort((left, right) => (left.start < right.start ? -1 : left.start > right.start ? 1 : 0));
  const merged: CachedDayRange[] = [];
  for (const range of sorted) {
    const last = merged.at(-1);
    if (last && range.start <= nextDayKey(last.end)) {
      if (range.end > last.end) last.end = range.end;
      last.at = Math.min(last.at, range.at);
      continue;
    }
    merged.push({ ...range });
  }
  return merged;
}

/** The span that holds the whole request, if one has been read. */
function coveringRange(
  ranges: CachedDayRange[],
  start: string,
  end: string,
): CachedDayRange | undefined {
  return ranges.find((range) => range.start <= start && range.end >= end);
}

function coveredDayCount(ranges: CachedDayRange[]): number {
  let total = 0;
  for (const range of ranges) {
    const start = parseLocalDate(range.start);
    const end = parseLocalDate(range.end);
    if (!start || !end) continue;
    total += Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1;
  }
  return total;
}

/**
 * An in-memory mirror of what storage holds, so painting a grid does not parse
 * a hundred kilobytes of JSON on the render path.
 */
const memoryDays = new Map<string, CachedDays>();

function readEntry(key: string): CachedDays | null {
  const cached = memoryDays.get(key);
  if (cached) return cached;
  const store = storage();
  if (!store) return null;
  try {
    const raw = store.getItem(key);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!isCachedDays(parsed)) return null;
    memoryDays.set(key, parsed);
    return parsed;
  } catch {
    return null;
  }
}

/**
 * Read the counts for one span.
 *
 * Returns whatever is known even when the span is only partly read, so the grid
 * can paint the days it has; `covered` reports whether the rest is genuinely
 * empty or merely unread, which is the difference between rendering "no events"
 * and rendering nothing yet.
 */
export function readCatalogSummary(
  tenantId: string | null,
  signature: string,
  start: string,
  end: string,
  now = Date.now(),
): CachedCatalogSummary | null {
  const entry = readEntry(cacheKey(tenantId, signature));
  if (!entry) return null;
  // The first day is only ever reused for a range that opens on it, because that
  // is the only range a fresh read would answer identically for.
  const anchor = entry.anchors[start];
  const afterFirst = nextDayKey(start);
  const interior = afterFirst > end ? undefined : coveringRange(entry.ranges, afterFirst, end);
  const interiorCovered = afterFirst > end || interior !== undefined;
  const days = [
    ...(anchor?.day ? [anchor.day] : []),
    ...Object.values(entry.days)
      .filter((day) => day.start_day > start && day.start_day <= end),
  ].sort((left, right) => (left.start_day < right.start_day ? -1 : 1));
  const readAt = Math.min(anchor?.at ?? entry.at, interior?.at ?? entry.at);
  return {
    summary: {
      days,
      // The API totals a range by summing its days, so a slice totals the same
      // way and a re-sliced cache cannot disagree with a fresh read.
      total_event_count: days.reduce((total, day) => total + day.event_count, 0),
      time_zone: entry.timeZone,
    },
    covered: anchor !== undefined && interiorCovered,
    // The age of the oldest part actually being served, not of the entry holding it.
    stale: now - readAt > CACHE_TTL_MS,
  };
}

/**
 * Record one answered span.
 *
 * The response is authoritative for the whole span, so days it omits inside the
 * span are dropped: an event that disappeared must not survive as a stale count.
 */
export function writeCatalogSummary(
  tenantId: string | null,
  signature: string,
  start: string,
  end: string,
  summary: CatalogDaySummary,
  now = Date.now(),
): void {
  const key = cacheKey(tenantId, signature);
  const previous = readEntry(key);
  const afterFirst = nextDayKey(start);
  const days: Record<string, CatalogDay> = {};
  for (const [dayKey, day] of Object.entries(previous?.days ?? {})) {
    // Outside the answered span the older reading still stands.
    if (dayKey < afterFirst || dayKey > end) days[dayKey] = day;
  }
  for (const day of summary.days) {
    // The range's own first day is short by construction; it is kept as an
    // anchor instead, never as an interior reading other ranges may reuse.
    if (day.start_day > start && day.event_count > 0) days[day.start_day] = day;
  }
  const anchors: Record<string, CachedAnchor> = { ...(previous?.anchors ?? {}) };
  anchors[start] = {
    at: now,
    day: summary.days.find((day) => day.start_day === start) ?? null,
  };
  let ranges = mergeRanges([
    ...(previous?.ranges ?? []),
    ...(afterFirst <= end ? [{ start: afterFirst, end, at: now }] : []),
  ]);
  let entry: CachedDays = { at: now, timeZone: summary.time_zone, ranges, days, anchors };
  if (coveredDayCount(ranges) > MAX_CACHE_DAYS) {
    // Past the bound the newest read starts over rather than growing an entry
    // that would eventually be refused by the storage quota.
    ranges = afterFirst <= end ? [{ start: afterFirst, end, at: now }] : [];
    entry = {
      at: now,
      timeZone: summary.time_zone,
      ranges,
      days: Object.fromEntries(
        summary.days
          .filter((day) => day.start_day > start && day.event_count > 0)
          .map((day) => [day.start_day, day]),
      ),
      anchors: { [start]: anchors[start] },
    };
  }
  entry.anchors = pruneAnchors(entry.anchors);
  memoryDays.set(key, entry);
  const store = storage();
  if (!store) return;
  try {
    pruneCatalogCache(store, MAX_CACHE_ENTRIES - 1, key);
    store.setItem(key, JSON.stringify(entry));
  } catch {
    // A quota rejection must never break browsing; drop the cache and continue.
    // The in-memory entry is kept: this tab can still slice what it just read.
    clearCatalogStorage();
  }
}

/** Keep the most recently read anchors so paging cannot grow one entry forever. */
function pruneAnchors(anchors: Record<string, CachedAnchor>): Record<string, CachedAnchor> {
  const entries = Object.entries(anchors);
  if (entries.length <= MAX_CACHE_ANCHORS) return anchors;
  entries.sort((left, right) => right[1].at - left[1].at);
  return Object.fromEntries(entries.slice(0, MAX_CACHE_ANCHORS));
}

function cacheKeys(store: Storage): string[] {
  const keys: string[] = [];
  for (let index = 0; index < store.length; index += 1) {
    const key = store.key(index);
    if (key?.startsWith(`${CACHE_PREFIX}:`)) keys.push(key);
  }
  return keys;
}

/** Drop the oldest entries until at most `keep` remain, never dropping `retain`. */
function pruneCatalogCache(store: Storage, keep: number, retain: string): void {
  const entries: Array<{ key: string; at: number }> = [];
  for (const key of cacheKeys(store)) {
    if (key === retain) continue;
    try {
      const parsed: unknown = JSON.parse(store.getItem(key) ?? "");
      entries.push({ key, at: isCachedDays(parsed) ? parsed.at : 0 });
    } catch {
      entries.push({ key, at: 0 });
    }
  }
  entries.sort((left, right) => right.at - left.at);
  for (const entry of entries.slice(Math.max(keep, 0))) {
    store.removeItem(entry.key);
    memoryDays.delete(entry.key);
  }
}

function clearCatalogStorage(): void {
  const store = storage();
  if (!store) return;
  try {
    for (const key of cacheKeys(store)) store.removeItem(key);
  } catch {
    // Nothing further is available if the storage itself is refusing access.
  }
}

/**
 * A day's agenda, held for the life of the page and never written to storage.
 *
 * Event records carry titles, venues, and links, so the counts cache refuses
 * them. Keeping them in memory honours that boundary — nothing outlives the
 * document — while making a day already looked at re-open instantly, which is
 * what re-selecting a day after a mode switch does.
 */
const MAX_AGENDA_ENTRIES = 60;
const AGENDA_TTL_MS = 2 * 60 * 1000;

interface CachedAgenda {
  at: number;
  events: EventItem[];
  cursor: string | null;
}

const memoryAgendas = new Map<string, CachedAgenda>();

/**
 * Which read produced an entry.
 *
 * A week column reads one page; the agenda keeps reading until the day's own
 * events appear, because a day window also matches events that began earlier and
 * those sort first. A column's page can therefore be entirely leftovers and hold
 * nothing for that day — true for the column, which shows the day's count beside
 * it, and wrong for the agenda, which would call a day of hundreds quiet. The two
 * never share an entry.
 */
export type CatalogDayScope = "agenda" | "preview";

function agendaKey(
  tenantId: string | null,
  signature: string,
  dayKey: string,
  scope: CatalogDayScope,
): string {
  return `${scope}:${tenantId ?? "anonymous"}:${signature}:${dayKey}`;
}

export function readCatalogDayEvents(
  tenantId: string | null,
  signature: string,
  dayKey: string,
  scope: CatalogDayScope = "agenda",
  now = Date.now(),
): { events: EventItem[]; cursor: string | null } | null {
  const key = agendaKey(tenantId, signature, dayKey, scope);
  const entry = memoryAgendas.get(key);
  if (!entry) return null;
  if (now - entry.at > AGENDA_TTL_MS) {
    memoryAgendas.delete(key);
    return null;
  }
  // Re-insert so the bound evicts by least-recent use rather than by insertion.
  memoryAgendas.delete(key);
  memoryAgendas.set(key, entry);
  return { events: entry.events, cursor: entry.cursor };
}

export function writeCatalogDayEvents(
  tenantId: string | null,
  signature: string,
  dayKey: string,
  events: EventItem[],
  cursor: string | null,
  scope: CatalogDayScope = "agenda",
  now = Date.now(),
): void {
  const key = agendaKey(tenantId, signature, dayKey, scope);
  memoryAgendas.delete(key);
  memoryAgendas.set(key, { at: now, events, cursor });
  while (memoryAgendas.size > MAX_AGENDA_ENTRIES) {
    const oldest = memoryAgendas.keys().next();
    if (oldest.done) break;
    memoryAgendas.delete(oldest.value);
  }
}

/** Remove every cached range and agenda. Sign-out and account erasure must call this. */
export function clearCatalogCache(): void {
  memoryDays.clear();
  memoryAgendas.clear();
  clearCatalogStorage();
}
