import type { EventItem } from "./types.ts";

const normalized = (value: string | null | undefined) => (value ?? "").trim().toLocaleLowerCase().replace(/\s+/g, " ");

/** Presentation only: never merge IDs, prices, locations or registration actions. */
export function groupEventSessions(events: EventItem[]): EventItem[][] {
  const groups = new Map<string, EventItem[]>();
  for (const event of events) {
    const anchors = [event.title, event.organizer_name, event.venue_name, event.city].map(normalized);
    const sources = [...new Set(event.source_keys ?? event.sources.map((source) => source.source))].sort();
    const key = anchors.every(Boolean) && sources.length
      ? JSON.stringify([...anchors, normalized(event.description), event.price_status, ...sources])
      : event.canonical_event_id;
    const existing = groups.get(key);
    if (existing) existing.push(event);
    else groups.set(key, [event]);
  }
  return [...groups.values()];
}

export function discoveryLabels(event: EventItem): string[] {
  const labels: string[] = [];
  if (event.discovery_state === "past") labels.push("Past event");
  if (event.discovery_state === "ongoing") labels.push("Ongoing");
  if (event.discovery_state === "uncertain") labels.push("Date needs confirmation");
  if (event.event_status === "cancelled") labels.push("Cancelled");
  if (event.discovery_state !== "past") {
    if (event.source_freshness === "stale") labels.push("Source not checked in 7 days");
    if (event.source_freshness === "unknown") labels.push("Source check date unknown");
  }
  return labels;
}
