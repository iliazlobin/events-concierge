import { normalizeCatalogEntityGraph } from "./entity-graph.ts";
import type { CatalogEntityGraph } from "./entity-graph.ts";
import type { EventItem } from "./types.ts";

/** Topic membership from admitted catalog records; no inferred person identities or relations. */
export function topicEventGraph(topic: string, events: EventItem[], hasMore: boolean): CatalogEntityGraph {
  const matching = events.filter(event => event.topics?.includes(topic));
  const focus = `topic:${topic}`;
  return normalizeCatalogEntityGraph({
    focus_id: focus, generated_at: new Date().toISOString(),
    counts: {events: matching.length, events_total:matching.length, peers:0, peers_total:0,topics:1,edges:matching.length,edges_total:matching.length,mention_edges:0},
    truncated: {events:hasMore,peers:false,edges:false},
    same_name_candidates:[],
    nodes: [{node_id:focus,node_kind:"topic",ring:0,label:topic,degree:matching.length},
      ...matching.map(event=>({node_id:`event:${event.canonical_event_id}`,node_kind:"event" as const,ring:1,label:event.title,canonical_event_id:event.canonical_event_id,start_at:event.start_at,end_at:event.end_at,venue_name:event.venue_name,city:event.city,price_status:event.price_status,topics:event.topics ?? [],registration_url:event.registration_urls[0] ?? null,is_past:event.discovery_state === "past",degree:1}))],
    edges:matching.map(event=>({a:focus,b:`event:${event.canonical_event_id}`,kind:"topic" as const,roles:[],source_labels:event.calendar_labels ?? [],observed_at:null})),
  });
}
