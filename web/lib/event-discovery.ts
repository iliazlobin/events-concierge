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

/** Diversify only broad discovery. Operates on loaded results; date-order views bypass it. */
export function variedEventChoices(events: EventItem[], broad: boolean): EventItem[] {
  if (!broad) return events;
  const pending = groupEventSessions(events).map((group) => [...group]);
  const result: EventItem[] = [];
  const organizers = new Map<string, number>();
  const sources = new Map<string, number>();
  const topics = new Map<string, number>();
  while (pending.length) {
    const cost = (index: number) => {
      const event = pending[index][0];
      const organizer = normalized(event.organizer_name);
      const source = event.source_keys?.[0] ?? "";
      const topicRepeats = event.topics?.length ? Math.min(...event.topics.map(t => topics.get(t) ?? 0)) : 0;
      return index + 1.5 * (organizer ? organizers.get(organizer) ?? 0 : 0)
        + 0.5 * (source ? sources.get(source) ?? 0 : 0) + 0.5 * topicRepeats
        + (event.source_freshness === "stale" ? 1 : 0);
    };
    let best = 0;
    for (let i=1; i<Math.min(5,pending.length);i++) if (cost(i)<cost(best)) best=i;
    const group = pending.splice(best,1)[0];
    result.push(...group);
    const event = group[0];
    const organizer = normalized(event.organizer_name);
    if (organizer) organizers.set(organizer,(organizers.get(organizer) ?? 0)+1);
    const source = event.source_keys?.[0];
    if (source) sources.set(source,(sources.get(source) ?? 0)+1);
    for (const t of event.topics ?? []) topics.set(t,(topics.get(t) ?? 0)+1);
  }
  return result;
}
