import { eventTopicLabel } from "./event-topics.ts";
import type {
  CalendarMode,
  CatalogDay,
  CatalogDaySummary,
  CatalogTopic,
  EventItem,
} from "./types.ts";

export interface CalendarDayCategory {
  topic: string;
  label: string;
  eventCount: number;
}

export function calendarRangeLabel(
  startDate: Date,
  endDate: Date,
  mode: CalendarMode,
  locale?: Intl.LocalesArgument,
): string {
  const monthLabel = (value: Date) => new Intl.DateTimeFormat(locale, {
    month: "long",
    year: "numeric",
  }).format(value);
  if (mode === "month") return monthLabel(startDate);
  if (mode === "six-months") {
    if (startDate.getFullYear() !== endDate.getFullYear()) {
      return `${monthLabel(startDate)} – ${monthLabel(endDate)}`;
    }
    const start = new Intl.DateTimeFormat(locale, { month: "long" }).format(startDate);
    const end = new Intl.DateTimeFormat(locale, { month: "long" }).format(endDate);
    return `${start} – ${end} ${endDate.getFullYear()}`;
  }
  const sameYear = startDate.getFullYear() === endDate.getFullYear();
  const sameMonth = sameYear && startDate.getMonth() === endDate.getMonth();
  const start = new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    year: sameYear ? undefined : "numeric",
  }).format(startDate);
  const end = new Intl.DateTimeFormat(locale, {
    month: sameMonth ? undefined : "short",
    day: "numeric",
  }).format(endDate);
  return `${start} – ${end}, ${endDate.getFullYear()}`;
}

/**
 * Name a week by the dates it actually covers.
 *
 * The month grid's rail is a way into that week, so it is labelled with the span
 * it leads to rather than an ordinal: "week 3" means nothing without counting,
 * while "Aug 16 – 22" is the thing being clicked. A week that crosses a month
 * names both months.
 */
export function calendarWeekSpanLabel(
  start: Date,
  locale?: Intl.LocalesArgument,
): string {
  const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 6);
  const sameMonth = start.getMonth() === end.getMonth()
    && start.getFullYear() === end.getFullYear();
  const from = new Intl.DateTimeFormat(locale, { month: "short", day: "numeric" }).format(start);
  const to = new Intl.DateTimeFormat(
    locale,
    sameMonth ? { day: "numeric" } : { month: "short", day: "numeric" },
  ).format(end);
  return `${from} – ${to}`;
}

export function calendarDayCategories(
  events: EventItem[],
  limit?: number,
): CalendarDayCategory[] {
  if (limit !== undefined && limit < 1) return [];
  const counts = new Map<string, number>();
  for (const event of events) {
    const eventTopics = [...new Set(
      (event.topics ?? []).map((topic) => topic.trim()).filter(Boolean),
    )];
    if (!eventTopics.length) {
      counts.set("other", (counts.get("other") ?? 0) + 1);
      continue;
    }
    for (const topic of eventTopics) {
      counts.set(topic, (counts.get(topic) ?? 0) + 1);
    }
  }
  const categories = [...counts.entries()]
    .map(([topic, eventCount]) => ({
      topic,
      label: topic === "other" ? "Other" : eventTopicLabel(topic),
      eventCount,
    }))
    .sort((left, right) => (
      right.eventCount - left.eventCount
      || left.label.localeCompare(right.label)
    ));
  return limit === undefined ? categories : categories.slice(0, limit);
}

/**
 * Build the calendar color key from the events that survived every active
 * filter. The API facet inventory intentionally ignores topic selections so
 * it can power broad filter suggestions; using those counts in the calendar
 * made the key disagree with the visible agenda after selecting multiple
 * topics.
 */
export function calendarLegendTopics(
  events: EventItem[],
  topicInventory: CatalogTopic[],
  activeTopics: string[],
): CatalogTopic[] {
  const labels = new Map(topicInventory.map((topic) => [topic.topic, topic.label]));
  const counts = new Map<string, number>();

  for (const event of events) {
    const eventTopics = new Set(
      (event.topics ?? []).map((topic) => topic.trim()).filter(Boolean),
    );
    for (const topic of eventTopics) counts.set(topic, (counts.get(topic) ?? 0) + 1);
  }

  // Keep zero-result selections visible so each active filter can still be
  // removed directly from the key.
  for (const topic of activeTopics) {
    const normalized = topic.trim();
    if (normalized && !counts.has(normalized)) counts.set(normalized, 0);
  }

  const active = new Set(activeTopics);
  return [...counts.entries()]
    .map(([topic, eventCount]) => ({
      topic,
      label: labels.get(topic) ?? eventTopicLabel(topic),
      event_count: eventCount,
    }))
    .sort((left, right) => (
      Number(active.has(right.topic)) - Number(active.has(left.topic))
      || right.event_count - left.event_count
      || left.label.localeCompare(right.label)
    ));
}

/**
 * Keep only the days the range actually asked for, and re-total what is left.
 *
 * A range read matches events by interval overlap, so an event that began before
 * the range and runs into it matches too — and it is counted on the day it
 * started, which can be outside the range. Those leaked days are not what they
 * appear: the count is only the part that overlaps the window, not that day's
 * real total. Keeping one would state a wrong number for a day the reader did
 * not ask about, and caching one would state it again the next time that day is
 * genuinely in range.
 *
 * Clipping also makes a cached range and a freshly read one agree exactly, so
 * the total does not shift by a few events when an entry expires.
 */
export function clipCalendarSummary(
  summary: CatalogDaySummary,
  start: string,
  end: string,
): CatalogDaySummary {
  const days = summary.days.filter((day) => day.start_day >= start && day.start_day <= end);
  if (days.length === summary.days.length) return summary;
  return {
    days,
    total_event_count: days.reduce((total, day) => total + day.event_count, 0),
    time_zone: summary.time_zone,
  };
}

/**
 * Index a day summary by its local date key so the grid can look a cell up
 * without holding the events that produced it.
 */
export function calendarSummaryByDate(
  summary: CatalogDaySummary | null,
): Map<string, CatalogDay> {
  const byDate = new Map<string, CatalogDay>();
  for (const day of summary?.days ?? []) byDate.set(day.start_day, day);
  return byDate;
}

/**
 * Present one summarized day in the same shape {@link calendarDayCategories}
 * produces from events, so a cell renders identically whichever side counted.
 * The server already folds untopiced events into `other`.
 */
export function calendarSummaryDayCategories(
  day: CatalogDay | undefined,
  limit?: number,
): CalendarDayCategory[] {
  if (!day || (limit !== undefined && limit < 1)) return [];
  const categories = day.topics
    .filter((topic) => topic.event_count > 0)
    .map((topic) => ({
      topic: topic.topic,
      label: topic.topic === "other" ? "Other" : topic.label || eventTopicLabel(topic.topic),
      eventCount: topic.event_count,
    }))
    .sort((left, right) => (
      right.eventCount - left.eventCount
      || left.label.localeCompare(right.label)
    ));
  return limit === undefined ? categories : categories.slice(0, limit);
}

/**
 * Build the color key from the summarized range rather than from the events a
 * client happened to have fetched. The contract of
 * {@link calendarLegendTopics} is preserved: counts reflect every active
 * filter, and a selection with no results stays visible so it can be removed.
 *
 * The synthetic `other` bucket is excluded: it is not a selectable topic.
 */
export function calendarSummaryLegendTopics(
  summary: CatalogDaySummary | null,
  topicInventory: CatalogTopic[],
  activeTopics: string[],
): CatalogTopic[] {
  const labels = new Map(topicInventory.map((topic) => [topic.topic, topic.label]));
  const counts = new Map<string, number>();

  for (const day of summary?.days ?? []) {
    for (const topic of day.topics) {
      if (topic.topic === "other") continue;
      counts.set(topic.topic, (counts.get(topic.topic) ?? 0) + topic.event_count);
    }
  }

  for (const topic of activeTopics) {
    const normalized = topic.trim();
    if (normalized && !counts.has(normalized)) counts.set(normalized, 0);
  }

  const active = new Set(activeTopics);
  return [...counts.entries()]
    .map(([topic, eventCount]) => ({
      topic,
      label: labels.get(topic) ?? eventTopicLabel(topic),
      event_count: eventCount,
    }))
    .sort((left, right) => (
      Number(active.has(right.topic)) - Number(active.has(left.topic))
      || right.event_count - left.event_count
      || left.label.localeCompare(right.label)
    ));
}
