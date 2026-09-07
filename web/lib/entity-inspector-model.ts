/**
 * The reading logic behind the entity inspector's three data tabs.
 *
 * It lives beside the component rather than inside it for one reason: everything here is a pure
 * function of a payload, and a `.tsx` module cannot be imported by the node test runner. Grouping,
 * de-duplication, cadence claims and provider presentation are the parts that can be wrong in a way
 * a screenshot will not show, so they are the parts that are tested.
 *
 * Two rules are structural here, not stylistic:
 *
 *  - Nothing in this module builds a URL. Every link the inspector renders is a URL a source already
 *    published against this entity; a display name is never turned into a query, a slug, or a
 *    profile guess. {@link entitySourcePresentation} maps a provider key to a *label*, and that is
 *    the whole of its authority.
 *  - Every derived claim degrades to nothing rather than to a zero. A missing insights payload
 *    produces no lines at all, and a single observed event never earns a line phrased as a pattern.
 */

import type { EntityGraphTextAppearance } from "./entity-graph.ts";
import { formatCity } from "./presentation.ts";
import type { CatalogEntityInsights, EventEntityRole } from "./types.ts";

/* ------------------------------------------------------------------ *
 * Overview
 * ------------------------------------------------------------------ */

/** One `<dt>/<dd>` pair. `value` is finished copy; the component never recomputes it. */
export interface EntityOverviewLine {
  key: string;
  label: string;
  value: string;
}

/**
 * What the drawn frame alone can say about an entity.
 *
 * The frame is the fallback for the ego before the detail route has ever been opened, so it may
 * only be trusted when it is the *complete* event set: a truncated frame knows how many events it
 * drew, not how many exist, and "3 upcoming" derived from a capped list is a false total.
 */
export interface EntityFrameActivity {
  upcoming: number;
  past: number;
  venues: string[];
  cities: string[];
}

const MAX_PLACES = 3;

/** Most frequent first, ties broken by first appearance, capped — never the whole tail. */
function rankByFrequency(values: Iterable<string | null | undefined>): string[] {
  const seen = new Map<string, { value: string; count: number; order: number }>();
  let order = 0;
  for (const raw of values) {
    const value = raw?.trim();
    if (!value) continue;
    const key = value.toLocaleLowerCase();
    const existing = seen.get(key);
    if (existing) existing.count += 1;
    else seen.set(key, { value, count: 1, order: order++ });
  }
  return [...seen.values()]
    .sort((a, b) => b.count - a.count || a.order - b.order)
    .slice(0, MAX_PLACES)
    .map((entry) => entry.value);
}

function monthYear(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { month: "short", year: "numeric" }).format(parsed);
}

/**
 * Frame-derived activity, or `null` when the frame is not entitled to speak for the record.
 *
 * `eventsTruncated` is the capability's own flag, so this cannot drift from what was drawn.
 */
export function entityFrameActivity(
  appearances: readonly EntityGraphTextAppearance[],
  eventsTruncated: boolean,
): EntityFrameActivity | null {
  if (eventsTruncated || appearances.length === 0) return null;
  let upcoming = 0;
  let past = 0;
  for (const item of appearances) {
    if (item.is_past) past += 1;
    else upcoming += 1;
  }
  return {
    upcoming,
    past,
    venues: rankByFrequency(appearances.map((item) => item.venue_name)),
    cities: rankByFrequency(appearances.map((item) => item.city)),
  };
}

/**
 * The overview's factual lines, in reading order.
 *
 * Deliberately absent: a bare total. The panel header already states the event count two lines
 * above, and restating it was the single largest thing on the old overview.
 *
 * Cadence is claimed only when the recent window holds more than one event, and place lines only
 * once more than one event has been observed — a one-event entity has no "usual" anything, and a
 * line that implied otherwise would be a claim the data cannot carry.
 */
export function entityOverviewLines(
  insights: CatalogEntityInsights | null | undefined,
  frame: EntityFrameActivity | null,
  now: Date = new Date(),
): EntityOverviewLine[] {
  const lines: EntityOverviewLine[] = [];
  if (!insights && !frame) return lines;

  const schedule = insights
    ? { upcoming: insights.upcoming_count ?? 0, past: insights.past_count ?? 0 }
    : frame
      ? { upcoming: frame.upcoming, past: frame.past }
      : null;
  const observed = insights?.event_count
    ?? (schedule ? schedule.upcoming + schedule.past : 0);

  if (schedule && schedule.upcoming + schedule.past > 0) {
    const { upcoming, past } = schedule;
    const held = `${past} already held`;
    const ahead = `${upcoming} upcoming`;
    lines.push({
      key: "schedule",
      label: "Schedule",
      value: upcoming > 0 && past > 0
        ? `${ahead} · ${held}`
        : upcoming > 0
          ? ahead
          : `Nothing upcoming · ${held}`,
    });
  }

  /*
   * Cadence is the one line here that would be a projection rather than a reading of the record,
   * so it is gated on an observation span that can carry a rate.
   *
   * `active_months` is not months-with-activity: `fn_get_catalog_entity_insights_v2` computes it as
   * `greatest(1, ceil((max(start_at) - min(start_at)) / 1 month))`, an elapsed span. A span of 1
   * therefore means every observed event fell inside a single window — often a single evening.
   * Measured live, 801 of the 974 entities that clear `recent_event_count > 1` sit at a span of 1,
   * including one whose 16 events are nine hours apart and one with 17 events over 24 days; each
   * would have rendered "~16 a month" with nothing on screen to disclose the window.
   *
   * So the rate is dropped rather than weakened when the span cannot support it, and where it is
   * claimed it names the span as the span it is.
   */
  if (
    insights
    && insights.recent_event_count > 1
    && insights.events_per_month
    && insights.active_months > 1
  ) {
    lines.push({
      key: "cadence",
      label: "Cadence",
      value: `~${insights.events_per_month} a month over ~${insights.active_months} months`,
    });
  }

  if (insights?.first_event_at && observed > 0) {
    // Gated on `observed` as well as on the date: a stale `first_event_at` left behind by events
    // that have since dropped out of the catalog would otherwise print a history for a record that
    // currently holds nothing.
    //
    // "since <month>" only earns its place once the record reaches back past the current month;
    // otherwise it dresses a few days of observation up as history.
    const first = new Date(insights.first_event_at);
    const earlier = !Number.isNaN(first.getTime())
      && (first.getFullYear() < now.getFullYear()
        || (first.getFullYear() === now.getFullYear() && first.getMonth() < now.getMonth()));
    const label = monthYear(insights.first_event_at);
    if (earlier && label) lines.push({ key: "since", label: "First seen", value: label });
  }

  if (insights?.typical_attendance) {
    lines.push({
      key: "attendance",
      // "Typical" is a claim about a distribution; one event has no distribution.
      label: observed > 1 ? "Typical size" : "Attendance",
      value: `${insights.typical_attendance.toLocaleString()} going`,
    });
  }

  if (insights) {
    const free = insights.free_count ?? 0;
    const paid = insights.paid_count ?? 0;
    const priced = free + paid;
    if (priced === 1) {
      lines.push({ key: "admission", label: "Admission", value: free ? "Free" : "Paid" });
    } else if (priced > 1) {
      lines.push({
        key: "admission",
        label: "Admission",
        value: paid === 0
          ? `${free} free, none paid`
          : free === 0
            ? `${paid} paid, none free`
            : `${free} free · ${paid} paid`,
      });
    }
  }

  const venues = insights?.top_venues?.length
    ? insights.top_venues.slice(0, MAX_PLACES)
    : frame?.venues ?? [];
  if (observed > 1 && venues.length) {
    lines.push({
      key: "venues",
      label: venues.length === 1 ? "Venue" : "Venues",
      value: venues.join(" · "),
    });
  }

  const cities = (insights?.top_cities?.length
    ? insights.top_cities.slice(0, MAX_PLACES)
    : frame?.cities ?? []).map((city) => formatCity(city)).filter(Boolean);
  if (observed > 1 && cities.length) {
    lines.push({
      key: "cities",
      label: cities.length === 1 ? "City" : "Cities",
      value: cities.join(" · "),
    });
  }

  if (insights?.source_labels?.length) {
    // Provenance stated once for the whole record, instead of repeated on every appearance row.
    lines.push({
      key: "sources",
      label: "Named by",
      value: insights.source_labels.join(" · "),
    });
  }

  return lines;
}

/* ------------------------------------------------------------------ *
 * Appearances
 * ------------------------------------------------------------------ */

export interface EntityAppearanceRow {
  node_id: string;
  title: string;
  start_at: string | null;
  is_past: boolean;
  venue_name: string | null;
  city: string | null;
  roles: EventEntityRole[];
  source_labels: string[];
  registration_url: string | null;
  /**
   * Records folded into this row because title and start were identical, this one included.
   *
   * Defence, not the fix for the duplication a reader actually sees. The component hands this
   * module one appearance per node id, so a single event cannot enter the fold twice, and across
   * the whole live catalog no two canonical events share a title and an exact start. The
   * duplication a reader does see — one title on several dates — is {@link series_count}.
   */
  occurrence_count: number;
  /**
   * Distinct dated rows anywhere in this record sharing this title — i.e. a recurring series.
   *
   * Counted over the whole appearance set, never per group: a title that recurs across the now
   * boundary would otherwise be undercounted in the upcoming group and unannotated in the past one.
   */
  series_count: number;
}

export interface EntityAppearanceGroup {
  key: "upcoming" | "past";
  label: string;
  rows: EntityAppearanceRow[];
}

function mergeInto(target: string[], incoming: readonly string[]): void {
  for (const value of incoming) if (!target.includes(value)) target.push(value);
}

function byStart(direction: 1 | -1) {
  return (a: EntityAppearanceRow, b: EntityAppearanceRow): number => {
    const left = a.start_at ? Date.parse(a.start_at) : Number.NaN;
    const right = b.start_at ? Date.parse(b.start_at) : Number.NaN;
    const leftMissing = Number.isNaN(left);
    const rightMissing = Number.isNaN(right);
    // An undated row cannot be placed in a chronology, so it sinks rather than claiming a slot.
    if (leftMissing && rightMissing) return a.title.localeCompare(b.title);
    if (leftMissing) return 1;
    if (rightMissing) return -1;
    if (left !== right) return (left - right) * direction;
    return a.title.localeCompare(b.title);
  };
}

/** Title, folded the way {@link foldGroup} folds it, so the two cannot key differently. */
function titleKey(item: EntityGraphTextAppearance): string {
  return (item.title.trim() || item.title).toLocaleLowerCase();
}

/**
 * Distinct dated rows per title, over the WHOLE record rather than per group.
 *
 * A series is a property of the record, not of the half of it a row happens to land in. Counting
 * inside a group made "3 dates under this title" read as 2 on a title with an upcoming pair and a
 * past date, and left a title with one date on each side of the now boundary annotated nowhere at
 * all. Distinct starts, not raw appearances, so the exact-duplicate fold cannot inflate a series.
 */
function seriesCounts(
  appearances: readonly EntityGraphTextAppearance[],
): Map<string, number> {
  const starts = new Map<string, Set<string>>();
  for (const item of appearances) {
    const key = titleKey(item);
    const existing = starts.get(key);
    if (existing) existing.add(item.start_at ?? "");
    else starts.set(key, new Set([item.start_at ?? ""]));
  }
  return new Map([...starts].map(([key, dates]) => [key, dates.size]));
}

function foldGroup(
  items: readonly EntityGraphTextAppearance[],
  isPast: boolean,
  direction: 1 | -1,
  series: Map<string, number>,
): EntityAppearanceRow[] {
  const byKey = new Map<string, EntityAppearanceRow>();
  for (const item of items) {
    const title = item.title.trim() || item.title;
    const key = `${title.toLocaleLowerCase()} ${item.start_at ?? ""}`;
    const existing = byKey.get(key);
    if (existing) {
      // The same event reaching the frame twice is a duplication in the record, not two events.
      existing.occurrence_count += 1;
      mergeInto(existing.roles, item.roles);
      mergeInto(existing.source_labels, item.source_labels);
      existing.venue_name = existing.venue_name ?? item.venue_name;
      existing.city = existing.city ?? item.city;
      existing.registration_url = existing.registration_url ?? item.registration_url;
      continue;
    }
    byKey.set(key, {
      node_id: item.node_id,
      title,
      start_at: item.start_at,
      is_past: isPast,
      venue_name: item.venue_name,
      city: item.city,
      roles: [...item.roles],
      source_labels: [...item.source_labels],
      registration_url: item.registration_url,
      occurrence_count: 1,
      series_count: 1,
    });
  }

  const rows = [...byKey.values()];
  for (const row of rows) row.series_count = series.get(row.title.toLocaleLowerCase()) ?? 1;
  return rows.sort(byStart(direction));
}

/**
 * Appearances as a reader can use them: what is coming, then what has happened.
 *
 * Exact duplicates (same title, same start) collapse into one row carrying the union of their roles
 * and asserting sources — defence rather than a live path; see {@link EntityAppearanceRow}.
 * Recurrences under one title are NOT collapsed — they are separate dates, and hiding them would
 * silently shrink the record — but each row states how many dates share its title so a series reads
 * as a series. That count is taken over the whole record before the split, so a title straddling
 * the now boundary is counted once and annotated on both sides.
 *
 * Empty groups are omitted rather than rendered as a heading over nothing.
 */
export function groupEntityAppearances(
  appearances: readonly EntityGraphTextAppearance[],
): EntityAppearanceGroup[] {
  const series = seriesCounts(appearances);
  const upcoming = foldGroup(appearances.filter((item) => !item.is_past), false, 1, series);
  const past = foldGroup(appearances.filter((item) => item.is_past), true, -1, series);
  const groups: EntityAppearanceGroup[] = [];
  if (upcoming.length) groups.push({ key: "upcoming", label: "Upcoming", rows: upcoming });
  if (past.length) groups.push({ key: "past", label: "Already held", rows: past });
  return groups;
}

/**
 * A date a reader can place, with the clock time after it.
 *
 * The old row showed only a clock time, which made a list spanning months unreadable. The year is
 * added only when it is not the current one, so the common case stays short.
 */
export function appearanceDateLabel(startAt: string | null, now: Date = new Date()): string {
  if (!startAt) return "Date not published";
  const parsed = new Date(startAt);
  if (Number.isNaN(parsed.getTime())) return "Date not published";
  const date = new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    ...(parsed.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  }).format(parsed);
  const time = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed);
  return `${date} · ${time}`;
}

/** The demoted provenance line: role and asserting source, and never the observation date. */
export function appearanceProvenance(row: EntityAppearanceRow): string {
  const parts: string[] = [];
  parts.push(row.roles.length ? row.roles.map(roleLabel).join(" · ") : "Named");
  if (row.series_count > 1) parts.push(`${row.series_count} dates under this title`);
  if (row.occurrence_count > 1) parts.push(`${row.occurrence_count} identical records`);
  if (row.source_labels.length) parts.push(`via ${row.source_labels.join(", ")}`);
  return parts.join(" · ");
}

export function roleLabel(role: string): string {
  return role.replace(/^./, (value) => value.toUpperCase());
}

/* ------------------------------------------------------------------ *
 * Profile & sources
 * ------------------------------------------------------------------ */

/**
 * A neutral glyph vocabulary, not a brand one.
 *
 * lucide-react carries no brand marks, and inlining one would both add an unlicensed asset and
 * imply an endorsement or a verification the record does not hold. These name the *kind* of thing
 * a link is.
 */
export type EntitySourceGlyph =
  | "website"
  | "code"
  | "reference"
  | "identity"
  | "handle"
  | "photo"
  | "music"
  | "video"
  | "generic";

export interface EntitySourcePresentation {
  label: string;
  glyph: EntitySourceGlyph;
  /**
   * `profile` is identity-bearing — a page a person or organization publishes *as themselves*.
   * `public` is everything else: a site, a repository host, a reference work.
   */
  group: "profile" | "public";
  /** Reading order within the group. */
  order: number;
}

/**
 * The provider vocabulary as it is known today.
 *
 * `x_profile`, `instagram_profile`, `tiktok_profile` and `youtube_profile` are listed ahead of the
 * database CHECK that will admit them, so the panel lights up the moment a row exists rather than
 * needing a second deploy. Any key not listed still renders — see {@link entitySourcePresentation}.
 */
const SOURCE_PRESENTATION: Record<string, EntitySourcePresentation> = {
  linkedin_profile: { label: "LinkedIn", glyph: "identity", group: "profile", order: 0 },
  x_profile: { label: "X", glyph: "handle", group: "profile", order: 1 },
  instagram_profile: { label: "Instagram", glyph: "photo", group: "profile", order: 2 },
  youtube_profile: { label: "YouTube", glyph: "video", group: "profile", order: 3 },
  tiktok_profile: { label: "TikTok", glyph: "music", group: "profile", order: 4 },
  official_website: { label: "Official website", glyph: "website", group: "public", order: 0 },
  github_public: { label: "GitHub", glyph: "code", group: "public", order: 1 },
  wikidata_public: { label: "Wikidata", glyph: "reference", group: "public", order: 2 },
};

function titleCase(value: string): string {
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toLocaleUpperCase() + part.slice(1))
    .join(" ");
}

/**
 * How to render one external source.
 *
 * An unrecognized key is presented from its own suffix rather than swallowed into "Public source":
 * the vocabulary is a database CHECK that another agent extends, and a panel that silently
 * mislabels a new provider is worse than one that reads its key honestly.
 */
export function entitySourcePresentation(providerKey: string): EntitySourcePresentation {
  const known = SOURCE_PRESENTATION[providerKey];
  if (known) return known;
  if (providerKey.endsWith("_profile")) {
    return {
      label: titleCase(providerKey.slice(0, -"_profile".length)) || "Profile",
      glyph: "handle",
      group: "profile",
      order: 90,
    };
  }
  if (providerKey.endsWith("_public")) {
    return {
      label: titleCase(providerKey.slice(0, -"_public".length)) || "Public source",
      glyph: "reference",
      group: "public",
      order: 90,
    };
  }
  return { label: "Public source", glyph: "generic", group: "public", order: 99 };
}

export interface EntitySourceGroups<T> {
  /** Identity-bearing profiles, read together and caveated together. */
  profiles: T[];
  /** Sites, repositories and reference works. */
  public: T[];
}

export function groupEntitySources<T extends { provider_key: string }>(
  sources: readonly T[],
): EntitySourceGroups<T> {
  const profiles: Array<{ item: T; order: number; index: number }> = [];
  const rest: Array<{ item: T; order: number; index: number }> = [];
  sources.forEach((item, index) => {
    const presentation = entitySourcePresentation(item.provider_key);
    const entry = { item, order: presentation.order, index };
    if (presentation.group === "profile") profiles.push(entry);
    else rest.push(entry);
  });
  const sort = (entries: Array<{ item: T; order: number; index: number }>): T[] =>
    entries.sort((a, b) => a.order - b.order || a.index - b.index).map((entry) => entry.item);
  return { profiles: sort(profiles), public: sort(rest) };
}
