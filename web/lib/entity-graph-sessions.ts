import { normalizeCatalogEntityGraph } from "./entity-graph.ts";
import type { CatalogEntityGraph, CatalogEntityGraphNode } from "./entity-graph.ts";

const fold = (value: string | null) => (value ?? "").trim().toLowerCase().replace(/\s+/g, " ");

/** Collapse visual sessions only. The original graph remains the per-date evidence model. */
export function aggregateGraphSessions(input: CatalogEntityGraph) {
  const graph = normalizeCatalogEntityGraph(input);
  const buckets = new Map<string, CatalogEntityGraphNode[]>();
  for (const node of graph.nodes.filter(n => n.node_kind === "event")) {
    const mentions = graph.edges.filter(e => e.kind === "mention" && e.b === node.node_id);
    const identity = mentions.map(e => JSON.stringify([e.a, [...e.roles].sort(), [...e.source_labels].sort()])).sort();
    // Matching names alone are insufficient: require venue, city and the same named entities,
    // roles and source assertions. Missing anchors stay separate.
    const anchors = [fold(node.label), fold(node.venue_name), fold(node.city)];
    const key = anchors.every(Boolean) && identity.length && node.start_at
      ? JSON.stringify([anchors, identity, [...node.topics].sort(), node.price_status])
      : node.node_id;
    const members = buckets.get(key) ?? [];
    members.push(node);
    buckets.set(key, members);
  }
  const groups = new Map<string, CatalogEntityGraphNode[]>();
  const representative = new Map<string, string>();
  const nodes = graph.nodes.filter(n => n.node_kind !== "event");
  for (const members of buckets.values()) {
    members.sort((a,b) => Number(a.is_past === true)-Number(b.is_past === true)
      || (a.is_past ? (b.start_at ?? "").localeCompare(a.start_at ?? "") : (a.start_at ?? "").localeCompare(b.start_at ?? ""))
      || a.node_id.localeCompare(b.node_id));
    const head = members[0];
    groups.set(head.node_id, members);
    for (const member of members) representative.set(member.node_id,head.node_id);
    nodes.push({...head,label:members.length>1 ? `${head.label} · ${members.length} dates` : head.label});
  }
  const edges = new Map<string, CatalogEntityGraph["edges"][number]>();
  for (const edge of graph.edges) {
    const a=representative.get(edge.a) ?? edge.a;
    const b=representative.get(edge.b) ?? edge.b;
    const key=JSON.stringify([a,b,edge.kind]);
    if (!edges.has(key)) edges.set(key,{...edge,a,b});
  }
  return {graph:{...graph,nodes,edges:[...edges.values()]},groups,representative};
}
