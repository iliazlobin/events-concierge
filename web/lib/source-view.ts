import { eventTopicLabel } from "./event-topics.ts";
import { formatCity } from "./presentation.ts";
import type { CatalogProvider, EventItem } from "./types.ts";

/** Source metadata comes from the public catalog, never the operator source API. */
export function sourceWebsite(source: CatalogProvider | null): string | null {
  const value = source?.seed_url;
  if (!value || /[\u0000-\u0020\u007f\\]/.test(value)) return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password
      ? url.href : null;
  } catch {
    return null;
  }
}

export function sourceTitle(source: CatalogProvider | null, sourceKey: string, events: EventItem[]): string {
  const sourceName = source?.display_name.trim() || source?.label.trim();
  if (sourceName) return sourceName;
  for (const event of events) {
    const observation = event.sources.find((item) => item.source_key === sourceKey);
    if (observation?.label?.trim()) return observation.label.trim();
  }
  return "Event source";
}

/** Facts summarize loaded event dates, not the source's complete offering or coverage. */
export function sourceEventSummary(events: EventItem[]) {
  const unique = [...new Map(events.map((event) => [event.canonical_event_id, event])).values()];
  const dates = unique.map((event) => new Date(event.start_at))
    .filter((date) => Number.isFinite(date.valueOf()))
    .sort((left, right) => left.valueOf() - right.valueOf());
  const formatter = new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric", year: "numeric",
  });
  const cities = [...new Set(unique.map((event) => formatCity(event.city)).filter(Boolean))];
  const topicCounts = new Map<string, number>();
  for (const event of unique) {
    for (const topic of new Set(event.topics ?? [])) {
      if (topic.trim()) topicCounts.set(topic, (topicCounts.get(topic) ?? 0) + 1);
    }
  }
  const topics = [...topicCounts].map(([key, count]) => ({ key, label: eventTopicLabel(key), count }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
  return {
    loadedCount: unique.length,
    dateLabel: dates.length ? formatter.formatRange(dates[0], dates[dates.length - 1]) : null,
    cities,
    topics,
  };
}
