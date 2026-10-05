import type { CatalogEntityKind, EventEntityRole } from "./types.ts";

/**
 * The ego-first bipartite entity graph: types, node-id grammar, and the pure
 * derivation that turns one `/graph` payload into an indexed scene model.
 *
 * The spine, restated so nothing downstream reinvents it: the payload draws
 * `entity -> event -> entity` and never `entity <-> entity`. Every edge on
 * screen is a literal `catalog_entity_event_mentions` row carrying a role and a
 * source, so a peer is always reached *through* the evidence that connects it.
 * Nothing here synthesises a relation, compares display names, or persists a
 * derived link.
 *
 * These types deliberately live in their own module rather than in
 * `web/lib/types.ts`: the graph payload is a self-contained capability and the
 * shared type barrel is edited by several tracks at once. Re-export from there
 * if a barrel import is wanted; nothing in this file depends on that happening.
 *
 * There is no `profile_confidence` anywhere in this file, and there must never
 * be one. The server emits no such field: the only way to decide whether a
 * profile URL "belongs to this name" is to compare the slug to the display
 * name, and a display name is presentation data, never an identity judgment.
 * A short-slug heuristic measured 264 flags over 666 person profiles at
 * ~100% false positives, so no replacement heuristic ships either. The honest
 * surface is the URL, the kind the source asserted, and which source said so.
 */

/* ------------------------------------------------------------------ *
 * Wire types — mirror the jsonb built by
 * `public.fn_get_catalog_entity_graph_v1` and
 * `public.fn_get_catalog_entity_directory_v1` (migration 0156).
 * ------------------------------------------------------------------ */

export type CatalogEntityGraphNodeKind = "entity" | "event" | "topic";

export type CatalogEntityIdentityStatus = "profile_verified" | "source_scoped";

/**
 * One drawn node.
 *
 * The three node kinds share one flat record because the canvas and inspector
 * walk a single list; the fields a kind does not
 * carry are null (entity nodes have no `start_at`, event nodes have no
 * `entity_id`). {@link normalizeGraphNode} is what guarantees that, because the
 * server omits absent keys entirely rather than emitting nulls — see
 * {@link RawCatalogEntityGraphNode}.
 */
export interface CatalogEntityGraphNode {
  node_id: string;
  node_kind: CatalogEntityGraphNodeKind;
  ring: number;
  label: string;
  degree: number;
  entity_id: string | null;
  entity_kind: CatalogEntityKind | null;
  identity_status: CatalogEntityIdentityStatus | null;
  profile_url: string | null;
  profile_key: string | null;
  canonical_event_id: string | null;
  start_at: string | null;
  end_at: string | null;
  is_past: boolean | null;
  venue_name: string | null;
  city: string | null;
  price_status: string | null;
  topics: string[];
  ego_roles: EventEntityRole[];
  registration_url: string | null;
  shared_event_count: number | null;
  roles: EventEntityRole[];
}

/**
 * The node exactly as it arrives on the wire.
 *
 * `jsonb_build_object` is only called with the keys a node kind actually has,
 * so a topic node is four keys wide and an entity node carries no `topics`
 * array at all. Typing the wire shape honestly — every field optional, every
 * field nullable — is what stops `node.topics.length` from throwing on the ego.
 * {@link CatalogEntityGraphNode} is assignable to this, so a normalized node
 * can be handed back to any function accepting the raw form.
 */
export type RawCatalogEntityGraphNode = {
  [K in keyof CatalogEntityGraphNode]?: CatalogEntityGraphNode[K] | null;
} & {
  node_id: string;
  node_kind: CatalogEntityGraphNodeKind;
  ring: number;
  label: string;
};

export interface CatalogEntityGraphEdge {
  a: string;
  b: string;
  kind: "mention" | "topic";
  roles: EventEntityRole[];
  source_labels: string[];
  observed_at: string | null;
}

/**
 * What was drawn, against what matched.
 *
 * `edges` counts the emitted `edges` array — mention edges plus one topic edge
 * per topic node — so it always equals `graph.edges.length`. `mention_edges` is
 * the capped mention subset, and it is the one `edges_total` and
 * `truncated.edges` compare against; topic edges derive from the already-capped
 * event set and are never truncated. Reading `edges` against `edges_total`
 * would therefore compute a false truncation state on any graph with a topic.
 */
export interface CatalogEntityGraphCounts {
  events: number;
  events_total: number;
  peers: number;
  peers_total: number;
  topics: number;
  edges: number;
  mention_edges: number;
  edges_total: number;
}

export interface CatalogEntityGraphTruncation {
  events: boolean;
  peers: boolean;
  edges: boolean;
}

/**
 * A same-name candidate is surfaced for a human to look at and is never merged,
 * never joined, and never used as an identity. Matching is exact on
 * `normalized_name` (pure case and whitespace folding) — no token, prefix, or
 * fuzzy match is offered, because a near-name match would turn a name into an
 * identity judgment.
 */
export interface CatalogEntitySameNameCandidate {
  entity_id: string;
  display_name: string;
  kind: CatalogEntityKind;
  identity_status: CatalogEntityIdentityStatus;
  event_count: number;
}

export interface CatalogEntityGraph {
  focus_id: string;
  generated_at: string;
  counts: CatalogEntityGraphCounts;
  truncated: CatalogEntityGraphTruncation;
  nodes: CatalogEntityGraphNode[];
  edges: CatalogEntityGraphEdge[];
  same_name_candidates: CatalogEntitySameNameCandidate[];
}

/** The graph as it arrives, before {@link normalizeCatalogEntityGraph}. */
export interface RawCatalogEntityGraph {
  focus_id: string;
  generated_at: string;
  counts: CatalogEntityGraphCounts;
  truncated: CatalogEntityGraphTruncation;
  nodes: RawCatalogEntityGraphNode[];
  edges: CatalogEntityGraphEdge[];
  same_name_candidates?: CatalogEntitySameNameCandidate[] | null;
}

export interface CatalogEntityHub {
  entity_id: string;
  display_name: string;
  kind: CatalogEntityKind;
  identity_status: CatalogEntityIdentityStatus;
  profile_url: string | null;
  profile_key: string | null;
  event_count: number;
  upcoming_count: number;
  peer_count: number;
  source_count: number;
  roles: EventEntityRole[];
  top_city: string | null;
  last_event_at: string | null;
}

export interface CatalogEntityDirectoryTotals {
  entity_count: number;
  person_count: number;
  organization_count: number;
  unknown_count: number;
  verified_count: number;
  scoped_count: number;
}

/**
 * The honesty contract, computed server-side so it cannot drift from the
 * ranking it qualifies: only a small share of catalog events name anybody, and
 * every ranking on the directory covers only those events.
 */
export interface CatalogEntityDirectoryCoverage {
  events_with_entities: number;
  events_total: number;
  mention_count: number;
}

export interface CatalogEntityDirectory {
  generated_at: string;
  totals: CatalogEntityDirectoryTotals;
  coverage: CatalogEntityDirectoryCoverage;
  matched: number;
  hubs: CatalogEntityHub[];
}

/* ------------------------------------------------------------------ *
 * Geometry types, shared with the layout.
 * ------------------------------------------------------------------ */

/** Screen-independent geometry produced by the layout. World units are CSS px at k = 1. */
export interface EntityGraphPlacement {
  node_id: string;
  x: number;
  y: number;
  radius: number;
}

export interface EntityGraphBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export interface EntityGraphScene {
  graph: CatalogEntityGraph;
  placements: EntityGraphPlacement[];
  bounds: EntityGraphBounds;
}

/* ------------------------------------------------------------------ *
 * Node-id grammar.
 * ------------------------------------------------------------------ */

/**
 * A parsed node id.
 *
 * Node ids are `"<kind>:<value>"`. For entities and events the value is a uuid;
 * for topics it is the topic text itself, which is why parsing splits on the
 * FIRST colon only — a topic containing a colon must survive the round trip
 * {@link formatGraphNodeId}({@link parseGraphNodeId}(id)) === id.
 */
export interface EntityGraphNodeRef {
  kind: CatalogEntityGraphNodeKind;
  value: string;
}

const NODE_KINDS: readonly CatalogEntityGraphNodeKind[] = ["entity", "event", "topic"];

function isNodeKind(value: string): value is CatalogEntityGraphNodeKind {
  return (NODE_KINDS as readonly string[]).includes(value);
}

/** Parse `"entity:<uuid>"`, `"event:<uuid>"`, or `"topic:<text>"`. Null when malformed. */
export function parseGraphNodeId(nodeId: string): EntityGraphNodeRef | null {
  const separator = nodeId.indexOf(":");
  if (separator <= 0) return null;
  const kind = nodeId.slice(0, separator);
  if (!isNodeKind(kind)) return null;
  const value = nodeId.slice(separator + 1);
  // An empty value is not a node: `"topic:"` would collide with every other
  // empty topic and there is no such topic in the catalog.
  if (value.length === 0) return null;
  return { kind, value };
}

/** The inverse of {@link parseGraphNodeId}. */
export function formatGraphNodeId(ref: EntityGraphNodeRef): string {
  return `${ref.kind}:${ref.value}`;
}

/** The entity uuid a node id names, or null when it names an event or a topic. */
export function graphNodeEntityId(nodeId: string): string | null {
  const ref = parseGraphNodeId(nodeId);
  return ref?.kind === "entity" ? ref.value : null;
}

/** The canonical event uuid a node id names, or null otherwise. */
export function graphNodeEventId(nodeId: string): string | null {
  const ref = parseGraphNodeId(nodeId);
  return ref?.kind === "event" ? ref.value : null;
}

/* ------------------------------------------------------------------ *
 * Normalization.
 * ------------------------------------------------------------------ */

function asArray<T>(value: readonly T[] | null | undefined): T[] {
  return Array.isArray(value) ? [...value] : [];
}

function asNumber(value: number | null | undefined): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** Fill every key the server omitted, so downstream code never guards a shape. */
export function normalizeGraphNode(node: RawCatalogEntityGraphNode): CatalogEntityGraphNode {
  return {
    node_id: node.node_id,
    node_kind: node.node_kind,
    ring: asNumber(node.ring),
    label: node.label,
    degree: asNumber(node.degree),
    entity_id: node.entity_id ?? null,
    entity_kind: node.entity_kind ?? null,
    identity_status: node.identity_status ?? null,
    profile_url: node.profile_url ?? null,
    profile_key: node.profile_key ?? null,
    canonical_event_id: node.canonical_event_id ?? null,
    start_at: node.start_at ?? null,
    end_at: node.end_at ?? null,
    is_past: node.is_past ?? null,
    venue_name: node.venue_name ?? null,
    city: node.city ?? null,
    price_status: node.price_status ?? null,
    topics: asArray(node.topics),
    ego_roles: asArray(node.ego_roles),
    registration_url: node.registration_url ?? null,
    shared_event_count: node.shared_event_count ?? null,
    roles: asArray(node.roles),
  };
}

export function normalizeCatalogEntityGraph(graph: RawCatalogEntityGraph): CatalogEntityGraph {
  return {
    focus_id: graph.focus_id,
    generated_at: graph.generated_at,
    counts: graph.counts,
    truncated: graph.truncated,
    nodes: graph.nodes.map(normalizeGraphNode),
    edges: graph.edges,
    same_name_candidates: asArray(graph.same_name_candidates),
  };
}

/* ------------------------------------------------------------------ *
 * Scene derivation.
 * ------------------------------------------------------------------ */

/** Ring 1 holds events, ring 2 peers, ring 3 topics; ring 0 is the ego alone. */
export const RING_EGO = 0;
export const RING_EVENTS = 1;
export const RING_PEERS = 2;
export const RING_TOPICS = 3;

/**
 * The indexed, ordered form of one graph payload.
 *
 * Everything the canvas and inspector need is derived
 * once, here, so their renderings cannot disagree about what the frame
 * contains. Ordering is fixed and total, which is what lets the layout be
 * byte-identical across runs and screenshot evidence be meaningful at all.
 */
export interface EntityGraphSceneModel {
  graph: CatalogEntityGraph;
  focusId: string;
  /** Null only when the payload is malformed; every real payload has an ego. */
  ego: CatalogEntityGraphNode | null;
  /** Ring 1, ordered upcoming-ascending then past-descending, as the server ranked them. */
  events: CatalogEntityGraphNode[];
  /** Ring 2, ordered by shared events desc, then label, then node id. */
  peers: CatalogEntityGraphNode[];
  /** Ring 3, ordered by event count desc, then label. */
  topics: CatalogEntityGraphNode[];
  byId: Map<string, CatalogEntityGraphNode>;
  edges: CatalogEntityGraphEdge[];
  /** node id -> the node ids it is joined to, sorted; both directions recorded. */
  neighbors: Map<string, string[]>;
  /** peer node id -> the event node ids it shares with the ego, in ring-1 order. */
  peerEvents: Map<string, string[]>;
  /** Tab and reading order: ego, then events clockwise, then peers, then topics. */
  order: string[];
}

function isPastEvent(node: CatalogEntityGraphNode): boolean {
  return node.is_past === true;
}

function compareStrings(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

/**
 * Ring-1 order, mirroring `fn_get_catalog_entity_graph_v1`'s own ORDER BY:
 * upcoming ascending then past descending, so history can never crowd out an
 * upcoming appearance. Re-derived rather than trusted because `jsonb_agg` sorts
 * the node array by `node_id` on the way out.
 */
function compareEvents(left: CatalogEntityGraphNode, right: CatalogEntityGraphNode): number {
  const leftPast = isPastEvent(left);
  const rightPast = isPastEvent(right);
  if (leftPast !== rightPast) return leftPast ? 1 : -1;
  const leftStart = left.start_at ?? "";
  const rightStart = right.start_at ?? "";
  if (leftStart !== rightStart) {
    return leftPast
      ? compareStrings(rightStart, leftStart)
      : compareStrings(leftStart, rightStart);
  }
  return compareStrings(left.node_id, right.node_id);
}

function comparePeers(left: CatalogEntityGraphNode, right: CatalogEntityGraphNode): number {
  const leftShared = left.shared_event_count ?? 0;
  const rightShared = right.shared_event_count ?? 0;
  if (leftShared !== rightShared) return rightShared - leftShared;
  const byLabel = compareStrings(left.label, right.label);
  return byLabel !== 0 ? byLabel : compareStrings(left.node_id, right.node_id);
}

function compareTopics(left: CatalogEntityGraphNode, right: CatalogEntityGraphNode): number {
  if (left.degree !== right.degree) return right.degree - left.degree;
  return compareStrings(left.label, right.label);
}

/**
 * Turn one payload into the ordered, indexed scene model.
 *
 * Pure: no DOM, no clock, no randomness. Complexity O(N log N + E) for N <= 79
 * nodes and E <= 600 edges, dominated by the three ring sorts.
 */
export function deriveEntityGraphScene(
  graph: CatalogEntityGraph | RawCatalogEntityGraph,
): EntityGraphSceneModel {
  // Normalization is idempotent and costs one pass over <= 79 nodes, so it runs
  // unconditionally rather than sniffing whether the caller already did it.
  const normalized = normalizeCatalogEntityGraph(graph);

  const byId = new Map<string, CatalogEntityGraphNode>();
  for (const node of normalized.nodes) byId.set(node.node_id, node);

  const focusId = normalized.focus_id;
  const ego = byId.get(focusId) ?? null;

  const events = normalized.nodes
    .filter((node) => node.node_kind === "event")
    .sort(compareEvents);
  const peers = normalized.nodes
    .filter((node) => node.node_kind === "entity" && node.node_id !== ego?.node_id)
    .sort(comparePeers);
  const topics = normalized.nodes
    .filter((node) => node.node_kind === "topic" && node.node_id !== ego?.node_id)
    .sort(compareTopics);

  const neighbors = new Map<string, string[]>();
  const link = (from: string, to: string): void => {
    const existing = neighbors.get(from);
    if (existing) existing.push(to);
    else neighbors.set(from, [to]);
  };
  for (const edge of normalized.edges) {
    link(edge.a, edge.b);
    link(edge.b, edge.a);
  }
  for (const [key, value] of neighbors) {
    neighbors.set(key, [...new Set(value)].sort(compareStrings));
  }

  // A peer's shared events are the ring-1 events it is itself joined to. Kept in
  // ring-1 order so the layout can anchor a peer beside its evidence without
  // re-sorting, and so the inspector follows the same order as the ring.
  const eventRank = new Map<string, number>();
  events.forEach((node, index) => eventRank.set(node.node_id, index));
  const peerEvents = new Map<string, string[]>();
  for (const peer of peers) {
    const shared = (neighbors.get(peer.node_id) ?? [])
      .filter((nodeId) => eventRank.has(nodeId))
      .sort((left, right) => (eventRank.get(left) ?? 0) - (eventRank.get(right) ?? 0));
    peerEvents.set(peer.node_id, shared);
  }

  const order: string[] = [];
  if (ego) order.push(ego.node_id);
  for (const node of events) order.push(node.node_id);
  for (const node of peers) order.push(node.node_id);
  for (const node of topics) order.push(node.node_id);

  return {
    graph: normalized,
    focusId,
    ego,
    events,
    peers,
    topics,
    byId,
    edges: normalized.edges,
    neighbors,
    peerEvents,
    order,
  };
}

/* ------------------------------------------------------------------ *
 * Inspector details.
 * ------------------------------------------------------------------ */

export interface EntityGraphAppearance {
  node_id: string;
  title: string;
  start_at: string | null;
  is_past: boolean;
  venue_name: string | null;
  city: string | null;
  roles: EventEntityRole[];
  /** Which source asserted the inspected entity's role on this event, and when. */
  source_labels: string[];
  observed_at: string | null;
  registration_url: string | null;
  entity_count: number;
}

export interface EntityGraphPeer {
  node_id: string;
  entity_id: string | null;
  display_name: string;
  kind: CatalogEntityKind | null;
  identity_status: CatalogEntityIdentityStatus | null;
  shared_event_count: number;
  /** Titles of the shared events for the inspector's connection list. */
  shared_event_titles: string[];
  degree: number;
}

export interface EntityGraphDetailModel {
  focus_id: string;
  generated_at: string;
  ego: {
    node_id: string;
    display_name: string;
    kind: CatalogEntityKind | null;
    identity_status: CatalogEntityIdentityStatus | null;
    profile_url: string | null;
    roles: EventEntityRole[];
    degree: number;
  } | null;
  upcoming: EntityGraphAppearance[];
  past: EntityGraphAppearance[];
  peers: EntityGraphPeer[];
  topics: Array<{ node_id: string; label: string; event_count: number }>;
  same_name_candidates: CatalogEntitySameNameCandidate[];
  truncated: CatalogEntityGraphTruncation;
  counts: CatalogEntityGraphCounts;
}

function graphAppearance(
  node: CatalogEntityGraphNode,
  evidence?: CatalogEntityGraphEdge,
): EntityGraphAppearance {
  return {
    node_id: node.node_id,
    title: node.label,
    start_at: node.start_at,
    is_past: isPastEvent(node),
    venue_name: node.venue_name,
    city: node.city,
    roles: evidence?.kind === "mention" ? evidence.roles : [],
    source_labels: evidence?.source_labels ?? [],
    observed_at: evidence?.observed_at ?? null,
    registration_url: node.registration_url,
    entity_count: node.degree,
  };
}

/** Appearance evidence belongs to the inspected entity, even in a sampled catalog graph. */
export function deriveEntityAppearances(
  scene: EntityGraphSceneModel,
  entityNodeId: string,
): EntityGraphAppearance[] {
  const mentions = new Map<string, CatalogEntityGraphEdge>();
  for (const edge of scene.edges) {
    if (edge.kind === "mention" && edge.a === entityNodeId) mentions.set(edge.b, edge);
  }
  return scene.events.flatMap((node) => {
    const edge = mentions.get(node.node_id);
    if (!edge) return [];
    return [graphAppearance(node, edge)];
  });
}

export function deriveEntityGraphDetails(scene: EntityGraphSceneModel): EntityGraphDetailModel {
  // Topic/catalog frames retain every occurrence, without borrowing entity assertions.
  const appearances = scene.ego?.node_kind === "entity"
    ? deriveEntityAppearances(scene, scene.focusId)
    : scene.events.map((node) => graphAppearance(node, scene.edges.find((edge) =>
      edge.kind === "topic" && edge.a === scene.focusId && edge.b === node.node_id)));

  return {
    focus_id: scene.focusId,
    generated_at: scene.graph.generated_at,
    ego: scene.ego
      ? {
        node_id: scene.ego.node_id,
        display_name: scene.ego.label,
        kind: scene.ego.entity_kind,
        identity_status: scene.ego.identity_status,
        profile_url: scene.ego.profile_url,
        roles: scene.ego.roles,
        degree: scene.ego.degree,
      }
      : null,
    upcoming: appearances.filter((entry) => !entry.is_past),
    past: appearances.filter((entry) => entry.is_past),
    peers: scene.peers.map((peer) => {
      const shared = scene.peerEvents.get(peer.node_id) ?? [];
      return {
        node_id: peer.node_id,
        entity_id: peer.entity_id,
        display_name: peer.label,
        kind: peer.entity_kind,
        identity_status: peer.identity_status,
        shared_event_count: peer.shared_event_count ?? shared.length,
        shared_event_titles: shared.map((nodeId) => scene.byId.get(nodeId)?.label ?? nodeId),
        degree: peer.degree,
      };
    }),
    topics: scene.topics.map((topic) => ({
      node_id: topic.node_id,
      label: topic.label,
      event_count: topic.degree,
    })),
    same_name_candidates: scene.graph.same_name_candidates,
    truncated: scene.graph.truncated,
    counts: scene.graph.counts,
  };
}
