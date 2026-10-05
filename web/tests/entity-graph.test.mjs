import assert from "node:assert/strict";
import test from "node:test";

import {
  BoundedLruCache,
  clearEntityGraphCache,
  entityDirectoryCacheKey,
  entityGraphCacheKey,
  entityGraphCacheSizes,
  entityOverviewCacheKey,
  readEntityDetail,
  readEntityGraph,
  readGraphEvent,
  readEntityOverview,
  writeEntityDetail,
  writeEntityGraph,
  writeGraphEvent,
  writeEntityOverview,
} from "../lib/entity-graph-cache.ts";
import {
  deriveEntityAppearances,
  deriveEntityGraphScene,
  formatGraphNodeId,
  graphNodeEntityId,
  graphNodeEventId,
  normalizeGraphNode,
  parseGraphNodeId,
  deriveEntityGraphDetails,
} from "../lib/entity-graph.ts";
import {
  layoutCatalogEntityGraph,
  layoutCatalogEntityOverview,
  layoutEgoRings,
  nodeRadius,
} from "../lib/entity-graph-layout.ts";
import {
  MIN_SCREEN_RADIUS,
  chooseRestingLabels,
  screenRadius,
} from "../lib/entity-graph-labels.ts";
import {
  appearanceDateLabel,
  appearanceProvenance,
  entityFrameActivity,
  entityOverviewLines,
  entitySourcePresentation,
  groupEntityAppearances,
  groupEntitySources,
} from "../lib/entity-inspector-model.ts";
import { readableGraphError } from "../lib/entity-graph-errors.ts";
import { identityLinks } from "../lib/entity-identity-links.ts";

const VIEWPORT = { width: 1200, height: 800 };
const TAU = Math.PI * 2;

/** Pad a counter into the uuid shape the payload uses, so ids sort predictably. */
function uuid(prefix, index) {
  const tail = String(index).padStart(12, "0");
  return `${prefix}0000-0000-4000-8000-${tail}`;
}

/**
 * Build a payload in the exact shape `fn_get_catalog_entity_graph_v1` emits,
 * including its habit of omitting the keys a node kind does not carry.
 */
function buildGraph({ events = 1, peers = 0, topics = 0, upcoming = 0, peerShare = 1 } = {}) {
  const egoId = uuid("aaaaaaaa", 1);
  const nodes = [
    {
      node_id: `entity:${egoId}`,
      node_kind: "entity",
      ring: 0,
      label: "Ego Collective",
      entity_id: egoId,
      entity_kind: "organization",
      identity_status: "source_scoped",
      profile_url: null,
      profile_key: null,
      degree: events,
      shared_event_count: null,
      roles: ["organizer"],
    },
  ];
  const edges = [];
  const eventIds = [];
  for (let index = 0; index < events; index += 1) {
    const eventId = uuid("bbbbbbbb", index + 1);
    eventIds.push(eventId);
    const isPast = index >= upcoming;
    nodes.push({
      node_id: `event:${eventId}`,
      node_kind: "event",
      ring: 1,
      label: `Event ${index + 1}`,
      canonical_event_id: eventId,
      // Upcoming events sort ahead of past ones and each block is monotonic, so
      // the derived ring order is checkable without re-deriving the SQL.
      start_at: `2026-${String((index % 12) + 1).padStart(2, "0")}-01T18:00:00+00:00`,
      end_at: null,
      is_past: isPast,
      venue_name: "Somewhere",
      city: "sanfrancisco",
      price_status: "unknown",
      topics: ["ai"],
      ego_roles: ["organizer"],
      registration_url: `https://example.test/${index}`,
      degree: 2,
    });
    edges.push({
      a: `entity:${egoId}`,
      b: `event:${eventId}`,
      kind: "mention",
      roles: ["organizer"],
      source_labels: ["Meetup San Francisco"],
      observed_at: "2026-08-01T00:00:00+00:00",
    });
  }
  for (let index = 0; index < peers; index += 1) {
    const peerId = uuid("cccccccc", index + 1);
    const shared = [];
    for (let offset = 0; offset < peerShare && eventIds.length > 0; offset += 1) {
      shared.push(eventIds[(index + offset) % eventIds.length]);
    }
    nodes.push({
      node_id: `entity:${peerId}`,
      node_kind: "entity",
      ring: 2,
      label: `Peer ${String(index + 1).padStart(2, "0")}`,
      entity_id: peerId,
      entity_kind: "person",
      identity_status: "profile_verified",
      profile_url: `https://linkedin.com/in/peer-${index + 1}`,
      profile_key: `https://linkedin.com/in/peer-${index + 1}`,
      degree: 3,
      shared_event_count: shared.length,
    });
    for (const eventId of shared) {
      edges.push({
        a: `entity:${peerId}`,
        b: `event:${eventId}`,
        kind: "mention",
        roles: ["host"],
        source_labels: ["Meetup San Francisco"],
        observed_at: "2026-08-01T00:00:00+00:00",
      });
    }
  }
  for (let index = 0; index < topics; index += 1) {
    const label = `topic-${String(index + 1).padStart(2, "0")}`;
    nodes.push({
      node_id: `topic:${label}`,
      node_kind: "topic",
      ring: 3,
      label,
      degree: topics - index,
    });
    edges.push({
      a: `entity:${egoId}`,
      b: `topic:${label}`,
      kind: "topic",
      roles: [],
      source_labels: [],
      observed_at: null,
    });
  }
  return {
    focus_id: `entity:${egoId}`,
    generated_at: "2026-08-26T23:20:34.742258+00:00",
    counts: {
      events,
      events_total: events,
      peers,
      peers_total: peers,
      topics,
      // `edges` counts the emitted array, topic edges included, so it always equals
      // `graph.edges.length`; `mention_edges` is the capped subset `edges_total` compares
      // against.  Migration 0165 is what made the database agree with this.
      edges: edges.length,
      mention_edges: edges.length - topics,
      edges_total: edges.length - topics,
    },
    truncated: { events: false, peers: false, edges: false },
    // `jsonb_agg(node ORDER BY node ->> 'node_id')` sorts the array by node id,
    // so the fixture arrives in that order too and the derivation has to sort.
    nodes: [...nodes].sort((left, right) => (left.node_id < right.node_id ? -1 : 1)),
    edges,
    same_name_candidates: [],
  };
}

function placementOf(scene, nodeId) {
  const found = scene.placements.find((placement) => placement.node_id === nodeId);
  assert.ok(found, `no placement for ${nodeId}`);
  return found;
}

/** The layout's own angle convention: 0 at 12 o'clock, increasing clockwise. */
function angleOf(placement) {
  const angle = Math.atan2(placement.x, -placement.y);
  return angle < 0 ? angle + TAU : angle;
}

/* ------------------------------------------------------------------ *
 * Node-id grammar.
 * ------------------------------------------------------------------ */

test("node ids round-trip through parse and format", () => {
  const ids = [
    `entity:${uuid("aaaaaaaa", 1)}`,
    `event:${uuid("bbbbbbbb", 2)}`,
    "topic:ai",
    // A topic is free text and may itself contain a colon, so parsing splits on
    // the FIRST colon only; anything else would lose part of the topic.
    "topic:art: performance",
    "topic:  spaced  ",
  ];
  for (const id of ids) {
    const parsed = parseGraphNodeId(id);
    assert.ok(parsed, id);
    assert.equal(formatGraphNodeId(parsed), id);
  }
});

test("malformed node ids parse to null rather than to a wrong node", () => {
  for (const id of ["", ":", "entity:", ":abc", "venue:soma", "entity", "topic"]) {
    assert.equal(parseGraphNodeId(id), null, id);
  }
});

test("node id accessors answer only for their own kind", () => {
  const entityId = uuid("aaaaaaaa", 1);
  const eventId = uuid("bbbbbbbb", 1);
  assert.equal(graphNodeEntityId(`entity:${entityId}`), entityId);
  assert.equal(graphNodeEntityId(`event:${eventId}`), null);
  assert.equal(graphNodeEventId(`event:${eventId}`), eventId);
  assert.equal(graphNodeEventId(`topic:ai`), null);
});

/* ------------------------------------------------------------------ *
 * Normalization and derivation.
 * ------------------------------------------------------------------ */

test("nodes normalize the keys the server omits", () => {
  const topic = normalizeGraphNode({
    node_id: "topic:ai",
    node_kind: "topic",
    ring: 3,
    label: "ai",
    degree: 4,
  });
  // The server emits four keys for a topic node; every other field must arrive
  // as null or as an empty array, never as undefined, so no renderer guards it.
  assert.deepEqual(topic.topics, []);
  assert.deepEqual(topic.roles, []);
  assert.deepEqual(topic.ego_roles, []);
  assert.equal(topic.entity_id, null);
  assert.equal(topic.is_past, null);
  assert.equal(topic.shared_event_count, null);
  assert.equal(topic.degree, 4);
});

test("no node, edge or scene field is named profile_confidence", () => {
  // The database emits no such field and no replacement heuristic ships: a
  // short-slug rule flagged 264 of 666 person profiles at ~100% false
  // positives, and deciding it any other way would compare a slug to a display
  // name, which is a name-derived identity judgment.
  const graph = buildGraph({ events: 4, peers: 3, topics: 2, upcoming: 2 });
  const scene = deriveEntityGraphScene(graph);
  const serialized = JSON.stringify([
    scene.graph,
    deriveEntityGraphDetails(scene),
    layoutEgoRings(scene, VIEWPORT),
  ]);
  assert.equal(serialized.includes("profile_confidence"), false);
  assert.equal(serialized.includes("confidence"), false);
});

test("rings are assigned by node kind, not by the payload's ring number", () => {
  const graph = buildGraph({ events: 5, peers: 4, topics: 3, upcoming: 2 });
  const scene = deriveEntityGraphScene(graph);

  assert.equal(scene.ego?.node_id, graph.focus_id);
  assert.equal(scene.events.length, 5);
  assert.equal(scene.peers.length, 4);
  assert.equal(scene.topics.length, 3);
  assert.ok(scene.events.every((node) => node.node_kind === "event"));
  assert.ok(scene.peers.every((node) => node.node_kind === "entity"));
  assert.ok(scene.topics.every((node) => node.node_kind === "topic"));
  // The ego is an entity node too and must never be counted as its own peer.
  assert.equal(scene.peers.some((node) => node.node_id === scene.ego?.node_id), false);

  // Ring 1 order: upcoming ascending, then past descending.
  const pastFlags = scene.events.map((node) => node.is_past);
  assert.deepEqual(pastFlags, [false, false, true, true, true]);
  assert.deepEqual(
    scene.events.filter((node) => !node.is_past).map((node) => node.label),
    ["Event 1", "Event 2"],
  );
  assert.deepEqual(
    scene.events.filter((node) => node.is_past).map((node) => node.label),
    ["Event 5", "Event 4", "Event 3"],
  );

  // Every node in the payload gets exactly one placement, in reading order.
  const placed = layoutEgoRings(scene, VIEWPORT);
  assert.equal(placed.placements.length, graph.nodes.length);
  assert.deepEqual(placed.placements.map((placement) => placement.node_id), scene.order);
});

test("a peer's shared events are the drawn events it is joined to", () => {
  const graph = buildGraph({ events: 4, peers: 2, topics: 0, upcoming: 2, peerShare: 2 });
  const scene = deriveEntityGraphScene(graph);
  for (const peer of scene.peers) {
    const shared = scene.peerEvents.get(peer.node_id);
    assert.equal(shared.length, 2);
    assert.ok(shared.every((nodeId) => nodeId.startsWith("event:")));
  }
  // Inspector details derive from the same model, so they list the same
  // evidence and cannot drift from the canvas.
  const details = deriveEntityGraphDetails(scene);
  assert.equal(details.upcoming.length + details.past.length, scene.events.length);
  assert.equal(details.peers.length, scene.peers.length);
  assert.deepEqual(details.peers[0].shared_event_titles.length, 2);
  assert.deepEqual(details.upcoming[0].source_labels, ["Meetup San Francisco"]);
});

/* ------------------------------------------------------------------ *
 * Layout — determinism.
 * ------------------------------------------------------------------ */

test("the layout is byte-identical across runs and across parsed copies", () => {
  const graph = buildGraph({ events: 9, peers: 12, topics: 4, upcoming: 4, peerShare: 3 });
  const first = layoutCatalogEntityGraph(graph, VIEWPORT);
  const second = layoutCatalogEntityGraph(graph, VIEWPORT);
  assert.equal(JSON.stringify(first.placements), JSON.stringify(second.placements));
  assert.equal(JSON.stringify(first.bounds), JSON.stringify(second.bounds));

  // A second, independently parsed copy of the same payload — the shape a page
  // reload actually produces — must lay out to the same coordinates, or a
  // deep link and a screenshot describe two different pictures.
  const reparsed = JSON.parse(JSON.stringify(graph));
  const third = layoutCatalogEntityGraph(reparsed, VIEWPORT);
  assert.equal(JSON.stringify(first.placements), JSON.stringify(third.placements));

  for (let run = 0; run < 50; run += 1) {
    const again = layoutCatalogEntityGraph(graph, VIEWPORT);
    assert.equal(JSON.stringify(again.placements), JSON.stringify(first.placements));
  }
});

test("shuffling the payload's node and edge arrays cannot move a node", () => {
  const graph = buildGraph({ events: 6, peers: 8, topics: 3, upcoming: 3, peerShare: 2 });
  const shuffled = {
    ...graph,
    nodes: [...graph.nodes].reverse(),
    edges: [...graph.edges].reverse(),
  };
  assert.equal(
    JSON.stringify(layoutCatalogEntityGraph(shuffled, VIEWPORT).placements),
    JSON.stringify(layoutCatalogEntityGraph(graph, VIEWPORT).placements),
  );
});

test("a different viewport spreads the rings but never reorders or shrinks them", () => {
  const graph = buildGraph({ events: 6, peers: 6, topics: 2, upcoming: 3 });
  const small = layoutCatalogEntityGraph(graph, { width: 320, height: 320 });
  const large = layoutCatalogEntityGraph(graph, { width: 2400, height: 1600 });
  assert.deepEqual(
    small.placements.map((placement) => placement.node_id),
    large.placements.map((placement) => placement.node_id),
  );
  const smallEvent = placementOf(small, `event:${uuid("bbbbbbbb", 1)}`);
  const largeEvent = placementOf(large, `event:${uuid("bbbbbbbb", 1)}`);
  assert.ok(Math.hypot(largeEvent.x, largeEvent.y) >= Math.hypot(smallEvent.x, smallEvent.y));
  // Angles are viewport-independent: only the radius moves.
  assert.ok(Math.abs(angleOf(largeEvent) - angleOf(smallEvent)) < 1e-9);
  // A degenerate viewport must not produce NaN coordinates.
  const zero = layoutCatalogEntityGraph(graph, { width: 0, height: 0 });
  assert.ok(zero.placements.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y)));
  const broken = layoutCatalogEntityGraph(graph, { width: Number.NaN, height: Number.NaN });
  assert.ok(broken.placements.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y)));
});

/* ------------------------------------------------------------------ *
 * Layout — geometry.
 * ------------------------------------------------------------------ */

test("the ego sits at the origin and rings step strictly outward", () => {
  const graph = buildGraph({ events: 8, peers: 10, topics: 4, upcoming: 4, peerShare: 2 });
  const scene = deriveEntityGraphScene(graph);
  const placed = layoutEgoRings(scene, VIEWPORT);
  const ego = placementOf(placed, scene.ego.node_id);
  assert.equal(ego.x, 0);
  assert.equal(ego.y, 0);

  const radiusOf = (nodes) => nodes.map((node) => {
    const placement = placementOf(placed, node.node_id);
    return Math.hypot(placement.x, placement.y);
  });
  const eventRadii = radiusOf(scene.events);
  const peerRadii = radiusOf(scene.peers);
  const topicRadii = radiusOf(scene.topics);
  // Every member of a ring is at the same radius, and each ring clears the one
  // inside it by at least the label band.
  assert.ok(Math.max(...eventRadii) - Math.min(...eventRadii) < 1e-9);
  assert.ok(Math.max(...peerRadii) - Math.min(...peerRadii) < 1e-9);
  assert.ok(Math.max(...topicRadii) - Math.min(...topicRadii) < 1e-9);
  assert.ok(eventRadii[0] > ego.radius);
  assert.ok(peerRadii[0] > eventRadii[0]);
  assert.ok(topicRadii[0] > peerRadii[0]);
});

test("a single event sits at 12 o'clock instead of at an arbitrary angle", () => {
  const graph = buildGraph({ events: 1, peers: 0, topics: 0 });
  const placed = layoutCatalogEntityGraph(graph, VIEWPORT);
  const event = placementOf(placed, `event:${uuid("bbbbbbbb", 1)}`);
  assert.ok(Math.abs(event.x) < 1e-9, `x should be 0, was ${event.x}`);
  assert.ok(event.y < 0, "the lone event should be directly above the ego");
});

test("under four events the ring spreads over the whole circle", () => {
  const graph = buildGraph({ events: 3, peers: 0, topics: 0, upcoming: 1 });
  const scene = deriveEntityGraphScene(graph);
  const placed = layoutEgoRings(scene, VIEWPORT);
  const angles = scene.events.map((node) => angleOf(placementOf(placed, node.node_id)));
  assert.equal(angles.length, 3);
  for (let index = 0; index < angles.length; index += 1) {
    assert.ok(Math.abs(angles[index] - (index * TAU) / 3) < 1e-9);
  }
});

test("four or more events draw a clock: upcoming above, past below", () => {
  const graph = buildGraph({ events: 8, peers: 0, topics: 0, upcoming: 3 });
  const scene = deriveEntityGraphScene(graph);
  const placed = layoutEgoRings(scene, VIEWPORT);
  for (const node of scene.events) {
    const angle = angleOf(placementOf(placed, node.node_id));
    if (node.is_past) assert.ok(angle >= Math.PI && angle < TAU, `past at ${angle}`);
    else assert.ok(angle >= 0 && angle < Math.PI, `upcoming at ${angle}`);
  }
});

test("the radius a node is drawn at is the radius the layout reserved for it", () => {
  // The canvas reads `placement.radius` and computes no curve of its own.  This asserts the
  // property that makes that safe — and that the overlap proof below is a proof about what is
  // actually drawn, not about a second, differently shaped curve that merely stays under it.
  const scene = deriveEntityGraphScene(
    buildGraph({ events: 24, peers: 48, topics: 6, upcoming: 12, peerShare: 3 }),
  );
  const placed = layoutEgoRings(scene, VIEWPORT);
  for (const ring of [scene.events, scene.peers, scene.topics]) {
    for (const node of ring) {
      assert.equal(placementOf(placed, node.node_id).radius, nodeRadius(node.degree));
    }
  }
  // The ego alone carries the 1.15 reserve, and it carries it in the placement too.
  assert.equal(
    placementOf(placed, scene.ego.node_id).radius,
    nodeRadius(scene.ego.degree) * 1.15,
  );
});

test("no two members of a ring overlap, at any size", () => {
  const cases = [
    { events: 1, peers: 1, topics: 0 },
    { events: 3, peers: 5, topics: 2, upcoming: 1 },
    { events: 17, peers: 6, topics: 5, upcoming: 0 },
    { events: 24, peers: 48, topics: 6, upcoming: 12, peerShare: 1 },
    { events: 24, peers: 48, topics: 6, upcoming: 0, peerShare: 3 },
  ];
  for (const shape of cases) {
    const scene = deriveEntityGraphScene(buildGraph(shape));
    const placed = layoutEgoRings(scene, VIEWPORT);
    for (const ring of [scene.events, scene.peers, scene.topics]) {
      for (let i = 0; i < ring.length; i += 1) {
        for (let j = i + 1; j < ring.length; j += 1) {
          const a = placementOf(placed, ring[i].node_id);
          const b = placementOf(placed, ring[j].node_id);
          const distance = Math.hypot(a.x - b.x, a.y - b.y);
          assert.ok(
            distance >= a.radius + b.radius - 1e-6,
            `${JSON.stringify(shape)}: ${a.node_id} and ${b.node_id} overlap (${distance})`,
          );
        }
      }
    }
  }
});

/* ------------------------------------------------------------------ *
 * Layout — the boundary cases.
 * ------------------------------------------------------------------ */

test("a lone node lays out at the origin with its own box as the bounds", () => {
  const graph = buildGraph({ events: 0, peers: 0, topics: 0 });
  const placed = layoutCatalogEntityGraph(graph, VIEWPORT);
  assert.equal(placed.placements.length, 1);
  const [ego] = placed.placements;
  assert.equal(ego.x, 0);
  assert.equal(ego.y, 0);
  assert.ok(ego.radius > 0);
  assert.deepEqual(placed.bounds, {
    minX: -ego.radius,
    minY: -ego.radius,
    maxX: ego.radius,
    maxY: ego.radius,
  });
});

test("the isolated entity draws its event ring and no peer ring", () => {
  // 11.5% of the catalog has no co-mention peer at all. The event ring is never
  // empty by construction, so this is a real figure, not an empty state.
  const graph = buildGraph({ events: 6, peers: 0, topics: 3, upcoming: 2 });
  const scene = deriveEntityGraphScene(graph);
  assert.equal(scene.peers.length, 0);
  const placed = layoutEgoRings(scene, VIEWPORT);
  assert.equal(placed.placements.length, 1 + 6 + 3);

  const eventRadius = Math.hypot(
    placementOf(placed, scene.events[0].node_id).x,
    placementOf(placed, scene.events[0].node_id).y,
  );
  const topicRadius = Math.hypot(
    placementOf(placed, scene.topics[0].node_id).x,
    placementOf(placed, scene.topics[0].node_id).y,
  );
  // With no peers, the topic ring moves inward to sit just outside the events
  // rather than leaving an empty annulus where ring 2 would have been.
  assert.ok(topicRadius > eventRadius);
  assert.ok(topicRadius < eventRadius * 2.2, `topic ring marooned at ${topicRadius}`);

  const details = deriveEntityGraphDetails(scene);
  assert.equal(details.peers.length, 0);
  assert.equal(details.upcoming.length, 2);
  assert.equal(details.past.length, 4);
});

test("a one-event, one-peer ego draws a tight figure rather than a void", () => {
  const graph = buildGraph({ events: 1, peers: 1, topics: 0 });
  const placed = layoutCatalogEntityGraph(graph, { width: 900, height: 700 });
  const width = placed.bounds.maxX - placed.bounds.minX;
  // Fixed 170/310/400 px rings would have drawn this inside an ~800 px void.
  assert.ok(width < 320, `bounds width was ${width}`);
});

test("the 79-node maximum lays out completely and stays finite", () => {
  const graph = buildGraph({ events: 24, peers: 48, topics: 6, upcoming: 11, peerShare: 2 });
  const scene = deriveEntityGraphScene(graph);
  assert.equal(1 + scene.events.length + scene.peers.length + scene.topics.length, 79);

  const placed = layoutEgoRings(scene, VIEWPORT);
  assert.equal(placed.placements.length, 79);
  assert.equal(new Set(placed.placements.map((p) => p.node_id)).size, 79);
  assert.ok(placed.placements.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y)));
  assert.ok(placed.placements.every((p) => p.radius >= 11 && p.radius <= 34 * 1.15));
  assert.ok(Number.isFinite(placed.bounds.minX) && Number.isFinite(placed.bounds.maxY));

  // Deterministic at the ceiling too, which is where a force layout diverges.
  assert.equal(
    JSON.stringify(layoutEgoRings(deriveEntityGraphScene(graph), VIEWPORT).placements),
    JSON.stringify(placed.placements),
  );
});

test("node radius is a bounded, monotonic function of degree alone", () => {
  assert.ok(nodeRadius(0) >= 11);
  assert.ok(nodeRadius(1) > nodeRadius(0));
  assert.ok(nodeRadius(17) > nodeRadius(5));
  assert.equal(nodeRadius(100_000), 34);
  assert.equal(nodeRadius(-5), nodeRadius(0));
  assert.equal(nodeRadius(Number.NaN), nodeRadius(0));
});

/* ------------------------------------------------------------------ *
 * Cache.
 * ------------------------------------------------------------------ */

test("cache keys separate every parameter that changes the payload", () => {
  const base = { tenantId: "t1", entityId: "e1", events: 18, peers: 32, topics: 4 };
  assert.notEqual(entityGraphCacheKey(base), entityGraphCacheKey({ ...base, tenantId: "t2" }));
  assert.notEqual(entityGraphCacheKey(base), entityGraphCacheKey({ ...base, entityId: "e2" }));
  assert.notEqual(entityGraphCacheKey(base), entityGraphCacheKey({ ...base, peers: 8 }));
  assert.equal(entityGraphCacheKey(base), entityGraphCacheKey({ ...base }));
  // An anonymous reader must never collide with a tenant literally named so.
  assert.notEqual(
    entityGraphCacheKey({ ...base, tenantId: null }),
    entityGraphCacheKey({ ...base, tenantId: "anonymous", entityId: "" }),
  );

  const directory = {
    tenantId: "t1",
    query: " noise ",
    kinds: ["person", "organization"],
    cities: ["oakland", "sanfrancisco"],
    limit: 48,
    minEvents: 1,
  };
  // Chip order is not a distinction; the query's surrounding space is not either.
  assert.equal(
    entityDirectoryCacheKey(directory),
    entityDirectoryCacheKey({
      ...directory,
      query: "noise",
      kinds: ["organization", "person"],
      cities: ["sanfrancisco", "oakland"],
    }),
  );
  assert.notEqual(
    entityDirectoryCacheKey(directory),
    entityDirectoryCacheKey({ ...directory, minEvents: 2 }),
  );
});

test("the LRU evicts the least recently USED entry, not the oldest write", () => {
  const cache = new BoundedLruCache(3, 60_000);
  cache.set("a", 1, 0);
  cache.set("b", 2, 1);
  cache.set("c", 3, 2);
  // Reading "a" makes "b" the least recently used one.
  assert.equal(cache.get("a", 3), 1);
  cache.set("d", 4, 4);
  assert.equal(cache.size, 3);
  assert.equal(cache.get("b", 5), null);
  assert.deepEqual(cache.keys(), ["c", "a", "d"]);

  // Re-writing an existing key refreshes its place instead of growing the map.
  cache.set("c", 30, 6);
  assert.equal(cache.size, 3);
  assert.deepEqual(cache.keys(), ["a", "d", "c"]);
});

test("the LRU never grows past its bound, whatever the traffic", () => {
  const cache = new BoundedLruCache(4, 60_000);
  for (let index = 0; index < 500; index += 1) cache.set(`k${index}`, index, index);
  assert.equal(cache.size, 4);
  assert.deepEqual(cache.keys(), ["k496", "k497", "k498", "k499"]);
});

test("an entry past its TTL is dropped on read rather than served", () => {
  const cache = new BoundedLruCache(4, 1_000);
  cache.set("a", 1, 0);
  assert.equal(cache.get("a", 999), 1);
  assert.equal(cache.get("a", 1_001), null);
  assert.equal(cache.size, 0);
});

test("the graph cache round-trips a bundle and clears on demand", () => {
  clearEntityGraphCache();
  const request = { tenantId: "t1", entityId: uuid("aaaaaaaa", 1), events: 18, peers: 32, topics: 4 };
  assert.equal(readEntityGraph(request, 0), null);
  const graph = buildGraph({ events: 2, peers: 1, topics: 1, upcoming: 1 });
  writeEntityGraph(request, graph, 0);
  assert.equal(readEntityGraph(request, 10), graph);
  assert.equal(readEntityGraph({ ...request, peers: 8 }, 10), null);
  assert.equal(entityGraphCacheSizes().graphs, 1);
  // The detail payload the inspector fetches lives in the same module for exactly this reason:
  // one `clear` on sign-out, with nothing reachable only through a component left behind.
  writeEntityDetail("t1", uuid("aaaaaaaa", 1), { entity: { display_name: "held" } });
  assert.equal(entityGraphCacheSizes().details, 1);
  assert.notEqual(readEntityDetail("t1", uuid("aaaaaaaa", 1)), null);
  assert.equal(readEntityDetail("t2", uuid("aaaaaaaa", 1)), null);
  clearEntityGraphCache();
  assert.equal(readEntityGraph(request, 10), null);
  assert.equal(readEntityDetail("t1", uuid("aaaaaaaa", 1)), null);
  assert.deepEqual(entityGraphCacheSizes(), {
    graphs: 0,
    directories: 0,
    overviews: 0,
    details: 0,
    events: 0,
  });
});

test("the graph cache holds 24 bundles and no more", () => {
  clearEntityGraphCache();
  const graph = buildGraph({ events: 1 });
  for (let index = 0; index < 40; index += 1) {
    writeEntityGraph(
      { tenantId: "t1", entityId: uuid("aaaaaaaa", index), events: 18, peers: 32, topics: 4 },
      graph,
      index,
    );
  }
  assert.equal(entityGraphCacheSizes().graphs, 24);
  clearEntityGraphCache();
});

/* ------------------------------------------------------------------ *
 * The overview layout: many hubs, no ego.
 * ------------------------------------------------------------------ */

/**
 * Build a payload in the shape the overview endpoint emits: `focus_id` is the
 * literal string "overview", which names no node, entities sit at ring 0 and
 * one representative bridge event per connected pair sits at ring 1.
 *
 * `pairs` is a list of index pairs into the connected pool; each one produces
 * exactly one bridge event and the two mention edges that make it a bridge.
 * `isolated` adds top hubs that share nothing with another hub, which the
 * endpoint returns because they are still hubs.
 */
function buildOverview({ entities = 0, pairs = [], isolated = 0 } = {}) {
  const nodes = [];
  const edges = [];
  const entityNodeId = (index) => `entity:${uuid("dddddddd", index + 1)}`;

  for (let index = 0; index < entities; index += 1) {
    nodes.push({
      node_id: entityNodeId(index),
      node_kind: "entity",
      ring: 0,
      label: `Hub ${String(index + 1).padStart(2, "0")}`,
      entity_id: uuid("dddddddd", index + 1),
      entity_kind: index % 2 === 0 ? "organization" : "person",
      identity_status: index % 3 === 0 ? "profile_verified" : "source_scoped",
      degree: entities - index,
      roles: ["organizer"],
    });
  }
  for (let index = 0; index < isolated; index += 1) {
    nodes.push({
      node_id: `entity:${uuid("eeeeeeee", index + 1)}`,
      node_kind: "entity",
      ring: 0,
      label: `Alone ${String(index + 1).padStart(2, "0")}`,
      entity_id: uuid("eeeeeeee", index + 1),
      entity_kind: "organization",
      identity_status: "source_scoped",
      degree: 4,
      roles: ["host"],
    });
  }
  pairs.forEach(([left, right], index) => {
    const eventId = uuid("ffffffff", index + 1);
    nodes.push({
      node_id: `event:${eventId}`,
      node_kind: "event",
      ring: 1,
      label: `Bridge ${index + 1}`,
      canonical_event_id: eventId,
      start_at: `2026-0${(index % 9) + 1}-01T18:00:00+00:00`,
      is_past: false,
      venue_name: "Somewhere",
      city: "sanfrancisco",
      degree: 2,
      // The pair's true shared total, which the representative stands in for.
      shared_event_count: 4 + index,
      topics: [],
    });
    for (const endpoint of [left, right]) {
      edges.push({
        a: entityNodeId(endpoint),
        b: `event:${eventId}`,
        kind: "mention",
        roles: ["organizer"],
        source_labels: ["Meetup San Francisco"],
        observed_at: "2026-08-01T00:00:00+00:00",
      });
    }
  });

  return {
    focus_id: "overview",
    generated_at: "2026-08-26T23:20:34.742258+00:00",
    counts: {
      events: pairs.length,
      events_total: pairs.length,
      peers: entities + isolated,
      peers_total: entities + isolated,
      topics: 0,
      edges: edges.length,
      mention_edges: edges.length,
      edges_total: edges.length,
    },
    truncated: { events: false, peers: false, edges: false },
    nodes: [...nodes].sort((left, right) => (left.node_id < right.node_id ? -1 : 1)),
    edges,
    same_name_candidates: [],
  };
}

test("the overview has no ego and retains every hub's appearance adjacency", () => {
  const graph = buildOverview({ entities: 3, pairs: [[0, 1], [1, 2]], isolated: 1 });
  const scene = deriveEntityGraphScene(graph);
  const firstHub = `entity:${uuid("dddddddd", 1)}`;
  const secondHub = `entity:${uuid("dddddddd", 2)}`;
  const thirdHub = `entity:${uuid("dddddddd", 3)}`;
  const isolatedHub = `entity:${uuid("eeeeeeee", 1)}`;
  const firstEvent = `event:${uuid("ffffffff", 1)}`;
  const secondEvent = `event:${uuid("ffffffff", 2)}`;

  assert.equal(scene.ego, null, "the first ring-0 hub is not an implicit focus");
  assert.equal(deriveEntityGraphDetails(scene).ego, null);
  assert.deepEqual(new Set(scene.peers.map((node) => node.node_id)),
    new Set([firstHub, secondHub, thirdHub, isolatedHub]));
  assert.deepEqual(scene.peerEvents.get(firstHub), [firstEvent]);
  assert.deepEqual(scene.peerEvents.get(secondHub), [firstEvent, secondEvent]);
  assert.deepEqual(scene.peerEvents.get(thirdHub), [secondEvent]);
  assert.deepEqual(scene.peerEvents.get(isolatedHub), []);
  assert.deepEqual(deriveEntityAppearances(scene, isolatedHub), [],
    "no drawn connection does not create a synthetic appearance");
});

test("overview appearances retain the selected hub's own roles and source evidence", () => {
  const graph = buildOverview({ entities: 3, pairs: [[0, 1], [0, 2], [1, 2]] });
  const selectedHub = `entity:${uuid("dddddddd", 1)}`;
  for (const edge of graph.edges) {
    const selected = edge.a === selectedHub;
    edge.roles = selected ? ["host"] : ["organizer"];
    edge.source_labels = selected ? ["Host calendar"] : ["Organizer calendar"];
    edge.observed_at = selected ? "2026-08-02T00:00:00Z" : "2026-08-03T00:00:00Z";
  }
  const firstEvent = `event:${uuid("ffffffff", 1)}`;
  graph.nodes.find((node) => node.node_id === firstEvent).is_past = true;
  for (const node of graph.nodes.filter((node) => node.node_kind === "event")) {
    node.ego_roles = ["organizer", "host"];
  }
  graph.edges.reverse();
  const scene = deriveEntityGraphScene(graph);
  const appearances = deriveEntityAppearances(scene, selectedHub);

  assert.deepEqual(appearances.map((row) => row.node_id),
    [`event:${uuid("ffffffff", 2)}`, firstEvent],
    "only incident events are shown, in upcoming-then-past scene order");
  assert.deepEqual(appearances.map(({ roles, source_labels, observed_at }) =>
    ({ roles, source_labels, observed_at })), [
    { roles: ["host"], source_labels: ["Host calendar"], observed_at: "2026-08-02T00:00:00Z" },
    { roles: ["host"], source_labels: ["Host calendar"], observed_at: "2026-08-02T00:00:00Z" },
  ]);
  assert.equal(appearances[0].title, "Bridge 2");
  assert.equal(appearances[0].start_at, "2026-02-01T18:00:00+00:00");
  assert.equal(appearances[0].venue_name, "Somewhere");
});

test("focused peer appearances do not inherit the focus's assertions or unrelated events", () => {
  const graph = buildGraph({ events: 3, peers: 1, peerShare: 2, upcoming: 2 });
  const peer = `entity:${uuid("cccccccc", 1)}`;
  for (const edge of graph.edges) {
    const isPeer = edge.a === peer;
    edge.roles = isPeer ? ["speaker"] : ["organizer"];
    edge.source_labels = isPeer ? ["Speaker calendar"] : ["Focus calendar"];
    edge.observed_at = isPeer ? "2026-08-04T00:00:00Z" : "2026-08-05T00:00:00Z";
  }
  const scene = deriveEntityGraphScene(graph);
  const appearances = deriveEntityAppearances(scene, peer);
  const focusAppearances = deriveEntityAppearances(scene, graph.focus_id);

  assert.deepEqual(appearances.map((row) => row.node_id),
    [`event:${uuid("bbbbbbbb", 1)}`, `event:${uuid("bbbbbbbb", 2)}`]);
  for (const row of appearances) {
    assert.deepEqual(row.roles, ["speaker"]);
    assert.deepEqual(row.source_labels, ["Speaker calendar"]);
    assert.equal(row.observed_at, "2026-08-04T00:00:00Z");
  }
  assert.equal(focusAppearances.length, 3);
  for (const row of focusAppearances) {
    assert.deepEqual(row.roles, ["organizer"]);
    assert.deepEqual(row.source_labels, ["Focus calendar"]);
    assert.equal(row.observed_at, "2026-08-05T00:00:00Z");
  }
});

function overlapping(scene) {
  const clashes = [];
  for (let left = 0; left < scene.placements.length; left += 1) {
    for (let right = left + 1; right < scene.placements.length; right += 1) {
      const a = scene.placements[left];
      const b = scene.placements[right];
      const gap = Math.hypot(a.x - b.x, a.y - b.y) - a.radius - b.radius;
      if (gap < -0.001) clashes.push([a.node_id, b.node_id, gap]);
    }
  }
  return clashes;
}

function centroidOf(scene, nodeIds) {
  let x = 0;
  let y = 0;
  for (const nodeId of nodeIds) {
    const placement = placementOf(scene, nodeId);
    x += placement.x;
    y += placement.y;
  }
  return { x: x / nodeIds.length, y: y / nodeIds.length };
}

test("the overview layout is deterministic and independent of payload order", () => {
  const graph = buildOverview({
    entities: 8,
    pairs: [[0, 1], [1, 2], [3, 4], [5, 6]],
    isolated: 3,
  });
  const first = layoutCatalogEntityOverview(graph, VIEWPORT);
  const second = layoutCatalogEntityOverview(
    // A structurally identical but distinct object, so nothing can be keyed on
    // object identity or on a cached derivation.
    JSON.parse(JSON.stringify(graph)),
    VIEWPORT,
  );
  assert.equal(JSON.stringify(second.placements), JSON.stringify(first.placements));
  assert.deepEqual(second.bounds, first.bounds);

  // The server aggregates its arrays in whatever order jsonb_agg hands back, so
  // the layout must not inherit an ordering from them.
  const shuffled = {
    ...graph,
    nodes: [...graph.nodes].reverse(),
    edges: [...graph.edges].reverse(),
  };
  const third = layoutCatalogEntityOverview(shuffled, VIEWPORT);
  assert.equal(JSON.stringify(third.placements), JSON.stringify(first.placements));
});

test("the overview layout places every node exactly once, and none overlap", () => {
  const graph = buildOverview({
    entities: 10,
    pairs: [[0, 1], [1, 2], [2, 3], [4, 5], [6, 7]],
    isolated: 4,
  });
  const scene = layoutCatalogEntityOverview(graph, VIEWPORT);
  assert.equal(scene.placements.length, graph.nodes.length);
  const ids = new Set(scene.placements.map((placement) => placement.node_id));
  assert.equal(ids.size, graph.nodes.length);
  for (const placement of scene.placements) {
    assert.ok(Number.isFinite(placement.x), `${placement.node_id} x is not finite`);
    assert.ok(Number.isFinite(placement.y), `${placement.node_id} y is not finite`);
    assert.ok(placement.radius > 0);
  }
  assert.deepEqual(overlapping(scene), []);
});

test("the overview layout packs each connected component into its own cell", () => {
  // Two components that share nothing: 0-1-2 and 5-6.
  const graph = buildOverview({ entities: 7, pairs: [[0, 1], [1, 2], [5, 6]] });
  const scene = layoutCatalogEntityOverview(graph, VIEWPORT);

  const bigMembers = [
    `entity:${uuid("dddddddd", 1)}`,
    `entity:${uuid("dddddddd", 2)}`,
    `entity:${uuid("dddddddd", 3)}`,
    `event:${uuid("ffffffff", 1)}`,
    `event:${uuid("ffffffff", 2)}`,
  ];
  const smallMembers = [
    `entity:${uuid("dddddddd", 6)}`,
    `entity:${uuid("dddddddd", 7)}`,
    `event:${uuid("ffffffff", 3)}`,
  ];
  const bigCentre = centroidOf(scene, bigMembers);
  const smallCentre = centroidOf(scene, smallMembers);

  // Every node is nearer its own component's centre than the other's, which is
  // what "packed into cells" has to mean for a reader looking at the picture.
  for (const nodeId of bigMembers) {
    const placement = placementOf(scene, nodeId);
    assert.ok(
      Math.hypot(placement.x - bigCentre.x, placement.y - bigCentre.y)
        < Math.hypot(placement.x - smallCentre.x, placement.y - smallCentre.y),
      `${nodeId} drifted toward the other component`,
    );
  }
  for (const nodeId of smallMembers) {
    const placement = placementOf(scene, nodeId);
    assert.ok(
      Math.hypot(placement.x - smallCentre.x, placement.y - smallCentre.y)
        < Math.hypot(placement.x - bigCentre.x, placement.y - bigCentre.y),
      `${nodeId} drifted toward the other component`,
    );
  }

  // A bridge sits between the two hubs it joins, not off to one side: it is
  // within the circle its endpoints sit on, and nearer their midpoint than
  // either endpoint is to the other.
  const bridge = placementOf(scene, `event:${uuid("ffffffff", 3)}`);
  const left = placementOf(scene, `entity:${uuid("dddddddd", 6)}`);
  const right = placementOf(scene, `entity:${uuid("dddddddd", 7)}`);
  const midX = (left.x + right.x) / 2;
  const midY = (left.y + right.y) / 2;
  const span = Math.hypot(left.x - right.x, left.y - right.y);
  assert.ok(Math.hypot(bridge.x - midX, bridge.y - midY) < span / 2, "bridge is not between its hubs");
});

test("the overview layout sets the isolated block apart from the clusters", () => {
  const graph = buildOverview({ entities: 4, pairs: [[0, 1], [2, 3]], isolated: 5 });
  const scene = layoutCatalogEntityOverview(graph, VIEWPORT);

  const isolatedIds = [];
  for (let index = 1; index <= 5; index += 1) isolatedIds.push(`entity:${uuid("eeeeeeee", index)}`);
  const clustered = scene.placements.filter(
    (placement) => !isolatedIds.includes(placement.node_id),
  );
  const alone = scene.placements.filter((placement) => isolatedIds.includes(placement.node_id));
  assert.equal(alone.length, 5);

  const clusterFloor = Math.max(...clustered.map((placement) => placement.y));
  const isolatedCeiling = Math.min(...alone.map((placement) => placement.y));
  // The block reads as "no drawn connection" only because of this gap; without
  // it the isolated hubs read as a component that failed to draw its edges.
  assert.ok(
    isolatedCeiling - clusterFloor > 100,
    `isolated block is only ${isolatedCeiling - clusterFloor}px below the clusters`,
  );

  // And it is a compact grid, not a line: five entities do not sit on one row.
  const rows = new Set(alone.map((placement) => Math.round(placement.y)));
  assert.ok(rows.size > 1, "the isolated block did not wrap");
});

test("the overview layout survives its degenerate cases", () => {
  const empty = layoutCatalogEntityOverview(buildOverview({}), VIEWPORT);
  assert.deepEqual(empty.placements, []);
  assert.deepEqual(empty.bounds, { minX: 0, minY: 0, maxX: 0, maxY: 0 });

  const lone = layoutCatalogEntityOverview(buildOverview({ entities: 1 }), VIEWPORT);
  assert.equal(lone.placements.length, 1);
  assert.equal(lone.placements[0].x, 0);
  assert.equal(lone.placements[0].y, 0);

  // Every hub isolated: the block is the whole figure and nothing is dropped.
  const scattered = layoutCatalogEntityOverview(buildOverview({ isolated: 9 }), VIEWPORT);
  assert.equal(scattered.placements.length, 9);
  assert.deepEqual(overlapping(scattered), []);

  // A viewport that has not been measured yet must not produce NaN.
  const unmeasured = layoutCatalogEntityOverview(
    buildOverview({ entities: 4, pairs: [[0, 1]], isolated: 2 }),
    { width: 0, height: 0 },
  );
  for (const placement of unmeasured.placements) {
    assert.ok(Number.isFinite(placement.x) && Number.isFinite(placement.y));
  }
});

test("the overview layout holds the widest graph the endpoint can return", () => {
  // 60 hubs and 60 representative bridges, the caps the capability allows: a
  // long chain, then a fan, then hubs that share nothing.
  const pairs = [];
  for (let index = 0; index + 1 < 40; index += 1) pairs.push([index, index + 1]);
  for (let index = 0; index < 21; index += 1) pairs.push([40, 41 + (index % 8)]);
  const graph = buildOverview({ entities: 50, pairs: pairs.slice(0, 60), isolated: 10 });
  const scene = layoutCatalogEntityOverview(graph, VIEWPORT);

  assert.equal(scene.placements.length, graph.nodes.length);
  assert.equal(scene.placements.length, 120);
  for (const placement of scene.placements) {
    assert.ok(Number.isFinite(placement.x) && Number.isFinite(placement.y));
  }
  assert.ok(scene.bounds.maxX > scene.bounds.minX);
  assert.ok(scene.bounds.maxY > scene.bounds.minY);

  // Determinism has to hold at capacity too, which is where a tie-break that is
  // not total would first show up.
  const again = layoutCatalogEntityOverview(JSON.parse(JSON.stringify(graph)), VIEWPORT);
  assert.equal(JSON.stringify(again.placements), JSON.stringify(scene.placements));
});

test("the overview layout leaves layoutEgoRings alone", () => {
  // The ego view still uses the ring layout; the two must not have merged.
  const ego = buildGraph({ events: 6, peers: 4, topics: 2, upcoming: 3 });
  const before = layoutCatalogEntityGraph(ego, VIEWPORT);
  layoutCatalogEntityOverview(buildOverview({ entities: 6, pairs: [[0, 1]] }), VIEWPORT);
  const after = layoutCatalogEntityGraph(ego, VIEWPORT);
  assert.equal(JSON.stringify(after.placements), JSON.stringify(before.placements));
  // The ego is still alone at the origin, which is the ring layout's whole claim.
  assert.equal(before.placements[0].x, 0);
  assert.equal(before.placements[0].y, 0);
});

test("the overview cache keys on every filter that changes the payload", () => {
  clearEntityGraphCache();
  const request = {
    tenantId: "t1",
    query: " ai ",
    kinds: ["person", "organization"],
    identity: "all",
    entities: 40,
    pairs: 40,
  };
  // The query is trimmed and the kinds are sorted into the key, so two chips
  // pressed in either order are one entry rather than two.
  assert.equal(
    entityOverviewCacheKey(request),
    entityOverviewCacheKey({ ...request, query: "ai", kinds: ["organization", "person"] }),
  );
  assert.notEqual(entityOverviewCacheKey(request), entityOverviewCacheKey({
    ...request,
    identity: "profile_verified",
  }));
  assert.notEqual(entityOverviewCacheKey(request), entityOverviewCacheKey({
    ...request,
    tenantId: "t2",
  }));

  const graph = buildOverview({ entities: 4, pairs: [[0, 1]] });
  assert.equal(readEntityOverview(request, 0), null);
  writeEntityOverview(request, graph, 0);
  assert.equal(readEntityOverview(request, 10), graph);
  assert.equal(readEntityOverview({ ...request, entities: 20 }, 10), null);
  assert.equal(entityGraphCacheSizes().overviews, 1);
  clearEntityGraphCache();
  assert.equal(entityGraphCacheSizes().overviews, 0);
});

test("the overview layout clears entities and bridges in one pass, not two", () => {
  /*
   * The regression this exists for, found against the live 53-node payload: the
   * first implementation cleared entities, then cleared bridges in a second
   * pass, and the second pass pushed a bridge off another bridge and straight
   * back onto an entity — three nodes ended up 26px inside each other.
   *
   * Both shapes below force the search: a triangle seats its hubs close enough
   * that every chord midpoint lands near the rim, and three bridges declared on
   * one pair share a midpoint exactly.
   */
  const triangle = layoutCatalogEntityOverview(
    buildOverview({ entities: 3, pairs: [[0, 1], [1, 2], [0, 2]] }),
    VIEWPORT,
  );
  assert.equal(triangle.placements.length, 6);
  assert.deepEqual(overlapping(triangle), []);

  const stacked = layoutCatalogEntityOverview(
    buildOverview({ entities: 2, pairs: [[0, 1], [0, 1], [0, 1]] }),
    VIEWPORT,
  );
  assert.equal(stacked.placements.length, 5);
  assert.deepEqual(overlapping(stacked), []);

  // A dense cluster is still packed, not exploded: nothing is flung outside a
  // radius the fit camera can frame.
  for (const placement of stacked.placements) {
    assert.ok(Math.hypot(placement.x, placement.y) < 600);
  }
});

/* ---------- resting labels ---------- */

const disc = (label, cx, cy, radius = 22) => ({ label, cx, cy, radius });

test("a node's disc is scaled by the camera so the layout's clearance is what the reader sees", () => {
  assert.equal(screenRadius(30, 1), 30);
  assert.equal(screenRadius(30, 0.5), 15);
  // The floor only binds where a disc is a position marker rather than a target.
  assert.equal(screenRadius(12, 0.25), MIN_SCREEN_RADIUS);
});

test("labels that would print over one another are held back, and the first one wins", () => {
  const legible = chooseRestingLabels([
    disc("Brooklyn Grange Events", 200, 200),
    disc("Local Economy", 210, 200),
  ]);

  assert.deepEqual(legible, [true, false]);
});

test("labels far enough apart both rest visible", () => {
  const legible = chooseRestingLabels([
    disc("Brooklyn Grange Events", 200, 200),
    disc("Local Economy", 600, 200),
  ]);

  assert.deepEqual(legible, [true, true]);
});

test("a label is held back when it would land across another node's disc", () => {
  // Directly below the first node, close enough that the first node's label crosses it.
  const legible = chooseRestingLabels([
    disc("Reading Rhythms California", 300, 300),
    disc("Accent Accent", 300, 345),
  ]);

  assert.equal(legible[0], false, "the label would be printed over the node beneath it");
});

test("the choice depends only on order, so a camera move cannot make labels flicker between nodes", () => {
  const placed = [
    disc("The New York Philosophical Society", 100, 100),
    disc("The Commons", 108, 100),
    disc("J.H. Seow", 460, 100),
  ];

  assert.deepEqual(chooseRestingLabels(placed), chooseRestingLabels(placed));
  assert.deepEqual(chooseRestingLabels(placed), [true, false, true]);
});

test("an empty graph asks nothing of the label pass", () => {
  assert.deepEqual(chooseRestingLabels([]), []);
});

/* ---------- failed graph loads ---------- */

test("a network failure is reported as one, not as the browser's word for it", () => {
  // Exactly what fetch rejects with when the request never reaches a server.
  assert.equal(
    readableGraphError(new TypeError("Failed to fetch")),
    "Could not reach the server.",
  );
});

test("an error the API chose to report is passed through", () => {
  assert.equal(
    readableGraphError(new Error("Entity graph is unavailable (503)")),
    "Entity graph is unavailable (503)",
  );
});

test("anything else still says something a reader can read", () => {
  for (const caught of [undefined, null, "boom", 42, new Error("   ")]) {
    assert.equal(readableGraphError(caught), "This entity graph could not be loaded.");
  }
});


/* ================================================================== *
 * Inspector reading model
 *
 * The three tabs' logic, tested where a screenshot cannot reach: what a claim is allowed to say,
 * what collapses into what, and what happens when the payload has nothing in it.
 * ================================================================== */

/** An appearance in the exact shape `deriveEntityGraphDetails` emits. */
function appearance(overrides = {}) {
  return {
    node_id: `event:${uuid("bbbbbbbb", 1)}`,
    title: "Junto Founder Dinner",
    start_at: "2026-09-04T16:00:00Z",
    is_past: false,
    venue_name: "Astoria",
    city: "newyork",
    roles: ["host"],
    source_labels: ["Andrew's Yeung's Tech Events"],
    observed_at: "2026-08-27T00:00:00Z",
    registration_url: "https://luma.com/junto",
    entity_count: 4,
    ...overrides,
  };
}

/** The Andrew Yeung figures measured against the live database. */
function insights(overrides = {}) {
  return {
    event_count: 14,
    upcoming_count: 8,
    past_count: 6,
    first_event_at: "2026-06-02T00:00:00Z",
    last_event_at: "2026-10-01T00:00:00Z",
    recent_event_count: 14,
    active_months: 3,
    events_per_month: 4.7,
    typical_attendance: 39,
    free_count: 13,
    paid_count: 1,
    top_topics: ["tech"],
    top_venues: ["Astoria", "Manhattan", "Midtown Manhattan"],
    top_cities: ["newyork", "brooklyn", "queens"],
    source_labels: ["Andrew's Yeung's Tech Events", "Luma Bay Area", "Luma New York"],
    collaborators: [],
  };
}

const AUGUST_2026 = new Date("2026-08-27T12:00:00Z");

function lineFor(lines, key) {
  return lines.find((line) => line.key === key) ?? null;
}

/* ---------- overview: insights ---------- */

test("a null insights payload with no frame produces no overview lines at all", () => {
  // Not a row of zeroes, not "0 events": the panel must be able to say nothing.
  assert.deepEqual(entityOverviewLines(null, null, AUGUST_2026), []);
  assert.deepEqual(entityOverviewLines(undefined, null, AUGUST_2026), []);
});

test("an entity with insights but no observations still emits nothing rather than zeroes", () => {
  const empty = {
    ...insights(),
    event_count: 0,
    upcoming_count: 0,
    past_count: 0,
    recent_event_count: 0,
    active_months: 0,
    events_per_month: null,
    typical_attendance: null,
    free_count: 0,
    paid_count: 0,
    top_topics: [],
    top_venues: [],
    top_cities: [],
    source_labels: [],
  };
  assert.deepEqual(entityOverviewLines(empty, null, AUGUST_2026), []);
});

test("the overview never restates the header's bare event count", () => {
  const lines = entityOverviewLines(insights(), null, AUGUST_2026);
  assert.ok(lines.length > 0);
  for (const line of lines) {
    assert.ok(
      !/^\d+ events?$/.test(line.value),
      `"${line.label}: ${line.value}" repeats the header's count`,
    );
    assert.ok(!/in the catalog/i.test(line.value));
  }
});

test("the measured Andrew Yeung payload reads as a host's record", () => {
  const lines = entityOverviewLines(insights(), null, AUGUST_2026);
  assert.equal(lines[0].key, "schedule", "what is coming leads");
  assert.equal(lineFor(lines, "schedule").value, "8 upcoming · 6 already held");
  assert.equal(lineFor(lines, "cadence").value, "~4.7 a month over ~3 months");
  assert.equal(lineFor(lines, "attendance").label, "Typical size");
  assert.equal(lineFor(lines, "attendance").value, "39 going");
  assert.equal(lineFor(lines, "admission").value, "13 free · 1 paid");
  assert.equal(lineFor(lines, "venues").value, "Astoria · Manhattan · Midtown Manhattan");
  assert.equal(lineFor(lines, "cities").value, "New York · Brooklyn · Queens");
  assert.equal(
    lineFor(lines, "sources").value,
    "Andrew's Yeung's Tech Events · Luma Bay Area · Luma New York",
  );
});

test("cadence is claimed only when the recent window holds more than one event", () => {
  const once = { ...insights(), recent_event_count: 1, events_per_month: 4.7 };
  assert.equal(lineFor(entityOverviewLines(once, null, AUGUST_2026), "cadence"), null);

  const twice = { ...insights(), recent_event_count: 2 };
  assert.ok(lineFor(entityOverviewLines(twice, null, AUGUST_2026), "cadence"));
});

test("a rate is not extrapolated from an observation span of one month", () => {
  // Measured live: 801 of the 974 entities that clear `recent_event_count > 1` sit at
  // active_months = 1, and active_months is an elapsed span, not months-with-activity.  The worst
  // real case is 16 events whose first and last start_at are nine hours apart, which used to
  // render "Cadence — ~16 a month" with nothing on screen disclosing the window.
  const oneEvening = {
    ...insights(),
    recent_event_count: 16,
    active_months: 1,
    events_per_month: 16,
  };
  assert.equal(
    lineFor(entityOverviewLines(oneEvening, null, AUGUST_2026), "cadence"),
    null,
    "a claim degrades to nothing, never to a weaker claim",
  );
});

test("a single-event entity gets no line phrased as a pattern", () => {
  const single = {
    ...insights(),
    event_count: 1,
    upcoming_count: 1,
    past_count: 0,
    recent_event_count: 1,
    active_months: 1,
    events_per_month: 1,
    typical_attendance: 39,
    free_count: 1,
    paid_count: 0,
    top_venues: ["Astoria"],
    top_cities: ["newyork"],
  };
  const lines = entityOverviewLines(single, null, AUGUST_2026);
  assert.equal(lineFor(lines, "cadence"), null, "one event is not a cadence");
  assert.equal(lineFor(lines, "venues"), null, "one event has no usual venue");
  assert.equal(lineFor(lines, "cities"), null, "one event has no usual city");
  assert.equal(lineFor(lines, "attendance").label, "Attendance", "one event has no 'typical' size");
  assert.equal(lineFor(lines, "admission").value, "Free");
  assert.equal(lineFor(lines, "schedule").value, "1 upcoming");
});

test("an entity with nothing ahead says so instead of dropping the line", () => {
  const behind = { ...insights(), upcoming_count: 0, past_count: 6 };
  assert.equal(
    lineFor(entityOverviewLines(behind, null, AUGUST_2026), "schedule").value,
    "Nothing upcoming · 6 already held",
  );
});

test("'first seen' appears only once the record reaches back past this month", () => {
  const thisMonth = { ...insights(), first_event_at: "2026-08-02T00:00:00Z" };
  assert.equal(lineFor(entityOverviewLines(thisMonth, null, AUGUST_2026), "since"), null);

  const lastYearSameMonth = { ...insights(), first_event_at: "2025-08-02T00:00:00Z" };
  assert.ok(
    lineFor(entityOverviewLines(lastYearSameMonth, null, AUGUST_2026), "since"),
    "an earlier year is history even when the month number matches",
  );
});

/* ---------- overview: the drawn frame as a fallback ---------- */

test("a truncated frame is never allowed to state a total", () => {
  const drawn = [appearance(), appearance({ node_id: "event:b", is_past: true })];
  assert.equal(entityFrameActivity(drawn, true), null);
  assert.deepEqual(entityOverviewLines(null, entityFrameActivity(drawn, true), AUGUST_2026), []);
});

test("a complete frame carries the schedule and places before the detail route is ever opened", () => {
  const drawn = [
    appearance({ node_id: "event:a", venue_name: "Astoria", city: "newyork" }),
    appearance({ node_id: "event:b", venue_name: "Astoria", city: "newyork", is_past: true }),
    appearance({ node_id: "event:c", venue_name: "Manhattan", city: "brooklyn", is_past: true }),
  ];
  const frame = entityFrameActivity(drawn, false);
  assert.deepEqual(frame, {
    upcoming: 1,
    past: 2,
    venues: ["Astoria", "Manhattan"],
    cities: ["newyork", "brooklyn"],
  });

  const lines = entityOverviewLines(null, frame, AUGUST_2026);
  assert.equal(lineFor(lines, "schedule").value, "1 upcoming · 2 already held");
  assert.equal(lineFor(lines, "venues").value, "Astoria · Manhattan");
  assert.equal(lineFor(lines, "cities").value, "New York · Brooklyn");
  assert.equal(lineFor(lines, "cadence"), null, "the frame cannot know a cadence");
  assert.equal(lineFor(lines, "attendance"), null, "the frame cannot know attendance");
});

test("insights outrank the frame wherever both can speak", () => {
  const frame = entityFrameActivity([appearance(), appearance({ node_id: "event:b" })], false);
  const lines = entityOverviewLines(insights(), frame, AUGUST_2026);
  assert.equal(lineFor(lines, "schedule").value, "8 upcoming · 6 already held");
});

/* ---------- appearances ---------- */

test("appearances separate what is coming from what has happened, most relevant first", () => {
  const groups = groupEntityAppearances([
    appearance({ node_id: "event:p1", title: "Old One", start_at: "2026-07-01T16:00:00Z", is_past: true }),
    appearance({ node_id: "event:u2", title: "Later", start_at: "2026-10-01T16:00:00Z" }),
    appearance({ node_id: "event:u1", title: "Sooner", start_at: "2026-09-01T16:00:00Z" }),
    appearance({ node_id: "event:p2", title: "Recent One", start_at: "2026-08-01T16:00:00Z", is_past: true }),
  ]);

  assert.deepEqual(groups.map((group) => group.key), ["upcoming", "past"]);
  assert.equal(groups[0].label, "Upcoming");
  assert.equal(groups[1].label, "Already held");
  assert.deepEqual(groups[0].rows.map((row) => row.title), ["Sooner", "Later"]);
  assert.deepEqual(groups[1].rows.map((row) => row.title), ["Recent One", "Old One"]);
});

test("an empty side is omitted rather than headed over nothing", () => {
  const groups = groupEntityAppearances([appearance()]);
  assert.deepEqual(groups.map((group) => group.key), ["upcoming"]);
  assert.deepEqual(groupEntityAppearances([]), []);
});

test("the two identical 'Junto Founder Dinner' rows collapse into one that says so", () => {
  // The exact defect in the owner's screenshot: one event reaching the frame twice.
  const groups = groupEntityAppearances([
    appearance({
      node_id: "event:dup-a",
      title: "Junto Founder Dinner (Hosted by Andrew & Friends)",
      roles: ["host"],
      source_labels: ["Andrew's Yeung's Tech Events"],
    }),
    appearance({
      node_id: "event:dup-b",
      title: "Junto Founder Dinner (Hosted by Andrew & Friends)",
      roles: ["organizer"],
      source_labels: ["Luma New York"],
    }),
  ]);

  assert.equal(groups[0].rows.length, 1, "one event, one row");
  const [row] = groups[0].rows;
  assert.equal(row.occurrence_count, 2);
  assert.equal(row.series_count, 1, "one date is not a series");
  assert.deepEqual(row.roles, ["host", "organizer"], "the union of what each record asserted");
  assert.deepEqual(row.source_labels, ["Andrew's Yeung's Tech Events", "Luma New York"]);
  assert.match(appearanceProvenance(row), /2 identical records/);
});

test("a title recurring on other dates is annotated, never hidden", () => {
  const groups = groupEntityAppearances([
    appearance({ node_id: "event:s1", title: "Junto Founder Dinner", start_at: "2026-09-04T16:00:00Z" }),
    appearance({ node_id: "event:s2", title: "Junto Founder Dinner", start_at: "2026-10-02T16:00:00Z" }),
    appearance({ node_id: "event:s3", title: "junto founder dinner", start_at: "2026-11-06T16:00:00Z" }),
  ]);

  assert.equal(groups[0].rows.length, 3, "three dates are three rows");
  for (const row of groups[0].rows) {
    assert.equal(row.series_count, 3);
    assert.equal(row.occurrence_count, 1);
    assert.match(appearanceProvenance(row), /3 dates under this title/);
  }
});

test("a title recurring across the now boundary is counted once, and annotated on both sides", () => {
  // The Andrew Yeung payload's real shape: "Junto Founder Dinner" on three dates, one already held
  // and two ahead.  Counting per group made the two upcoming rows say "2 dates under this title"
  // while the panel visibly showed three, and left the past row saying nothing at all.
  const groups = groupEntityAppearances([
    appearance({
      node_id: "event:p1",
      title: "Junto Founder Dinner",
      start_at: "2026-08-19T16:00:00Z",
      is_past: true,
    }),
    appearance({ node_id: "event:u1", title: "Junto Founder Dinner", start_at: "2026-09-14T16:00:00Z" }),
    appearance({ node_id: "event:u2", title: "Junto Founder Dinner", start_at: "2026-09-30T16:00:00Z" }),
  ]);

  const rows = groups.flatMap((group) => group.rows);
  assert.equal(rows.length, 3, "three dates are three rows");
  for (const row of rows) {
    assert.equal(row.series_count, 3, "the series is a property of the record, not of the group");
    assert.match(appearanceProvenance(row), /3 dates under this title/);
  }
});

test("a title with one date on each side of the boundary is still annotated", () => {
  const groups = groupEntityAppearances([
    appearance({
      node_id: "event:p1",
      title: "AI & Marketing Salon",
      start_at: "2026-08-20T16:00:00Z",
      is_past: true,
    }),
    appearance({ node_id: "event:u1", title: "AI & Marketing Salon", start_at: "2026-09-10T16:00:00Z" }),
  ]);

  for (const row of groups.flatMap((group) => group.rows)) {
    assert.equal(row.series_count, 2);
    assert.match(appearanceProvenance(row), /2 dates under this title/);
  }
});

test("an undated row sinks instead of claiming a place in the chronology", () => {
  const groups = groupEntityAppearances([
    appearance({ node_id: "event:x", title: "Undated", start_at: null }),
    appearance({ node_id: "event:y", title: "Dated", start_at: "2026-09-01T16:00:00Z" }),
  ]);
  assert.deepEqual(groups[0].rows.map((row) => row.title), ["Dated", "Undated"]);
});

test("every appearance row renders a date, not just a clock time", () => {
  // The defect in the owner's screenshot: "9:00 AM" with nothing to place it in the year.
  const label = appearanceDateLabel("2026-09-04T16:00:00Z", AUGUST_2026);
  assert.match(label, /Sep/, "the month is present");
  assert.match(label, /\b4\b/, "the day of the month is present");
  assert.match(label, /\d{1,2}:\d{2}/, "the clock time is still there, after the date");
  assert.ok(!label.includes("2026"), "the current year is left off as noise");

  const otherYear = appearanceDateLabel("2027-01-04T16:00:00Z", AUGUST_2026);
  assert.match(otherYear, /2027/, "a year that is not this one is stated");

  assert.equal(appearanceDateLabel(null, AUGUST_2026), "Date not published");
  assert.equal(appearanceDateLabel("not a date", AUGUST_2026), "Date not published");
});

test("provenance is demoted to one line and never carries the observation date", () => {
  const [group] = groupEntityAppearances([appearance()]);
  const line = appearanceProvenance(group.rows[0]);
  assert.equal(line, "Host · via Andrew's Yeung's Tech Events");
  // `observed_at` is identical on every row, so it was the least informative text on the card.
  assert.ok(!/seen/i.test(line));
  assert.ok(!line.includes("2026"));
});

/* ---------- profile & sources ---------- */

test("the four provider keys in the CHECK today are labelled and grouped", () => {
  assert.equal(entitySourcePresentation("linkedin_profile").label, "LinkedIn");
  assert.equal(entitySourcePresentation("linkedin_profile").group, "profile");
  assert.equal(entitySourcePresentation("official_website").label, "Official website");
  assert.equal(entitySourcePresentation("official_website").group, "public");
  assert.equal(entitySourcePresentation("github_public").label, "GitHub");
  assert.equal(entitySourcePresentation("github_public").group, "public");
  assert.equal(entitySourcePresentation("wikidata_public").label, "Wikidata");
});

test("the social provider keys light up the moment a row exists", () => {
  for (const [key, label] of [
    ["x_profile", "X"],
    ["instagram_profile", "Instagram"],
    ["tiktok_profile", "TikTok"],
    ["youtube_profile", "YouTube"],
  ]) {
    const presentation = entitySourcePresentation(key);
    assert.equal(presentation.label, label);
    assert.equal(presentation.group, "profile", `${key} is identity-bearing`);
    assert.ok(presentation.glyph, `${key} has a glyph`);
  }
});

test("a provider key nobody taught the panel about still reads honestly", () => {
  assert.deepEqual(
    { ...entitySourcePresentation("mastodon_profile"), order: 0 },
    { label: "Mastodon", glyph: "handle", group: "profile", order: 0 },
  );
  assert.equal(entitySourcePresentation("crunchbase_public").label, "Crunchbase");
  assert.equal(entitySourcePresentation("crunchbase_public").group, "public");
  assert.equal(entitySourcePresentation("something-else").label, "Public source");
  assert.equal(entitySourcePresentation("something-else").group, "public");
});

test("identity-bearing profiles read together, apart from sites and repositories", () => {
  const grouped = groupEntitySources([
    { provider_key: "github_public" },
    { provider_key: "instagram_profile" },
    { provider_key: "official_website" },
    { provider_key: "linkedin_profile" },
    { provider_key: "x_profile" },
  ]);

  assert.deepEqual(
    grouped.profiles.map((item) => item.provider_key),
    ["linkedin_profile", "x_profile", "instagram_profile"],
  );
  assert.deepEqual(
    grouped.public.map((item) => item.provider_key),
    ["official_website", "github_public"],
  );
});

test("an entity with no connected source groups into two empty lists, not one false one", () => {
  assert.deepEqual(groupEntitySources([]), { profiles: [], public: [] });
});

/* ---------- the rule that has no UI ---------- */

test("nothing in the inspector builds a URL from a name", async () => {
  // Binding rule (f): a handle is only ever taken from a structured field a source published, or
  // from a page the entity itself links.  No search, no guessed profile, no name-derived slug.
  const { readFile } = await import("node:fs/promises");
  // Every file in the feature, the two identity-link modules included. Exempting them would have
  // exempted the only files that carry platform host literals at all, leaving the guard reading far
  // stronger than it was: `https://x.com/${handle}` inside entity-identity-links.ts would have
  // shipped green.
  const sources = await Promise.all([
    readFile(new URL("../lib/entity-inspector-model.ts", import.meta.url), "utf8"),
    readFile(new URL("../components/entity-inspector.tsx", import.meta.url), "utf8"),
    readFile(new URL("../lib/entity-identity-links.ts", import.meta.url), "utf8"),
    readFile(new URL("../components/entity-identity-links.tsx", import.meta.url), "utf8"),
  ]);

  const forbidden = [
    /[?&]q=/,
    /[?&]query=/,
    /search\?/,
    /google\.com/i,
    /duckduckgo/i,
    /bing\.com/i,
    /linkedin\.com\/pub\/dir/i,
    /encodeURIComponent/,
    // A *scheme-prefixed* platform host could only be a URL this code assembled itself; every link
    // the panel renders arrives whole, on the payload. The bare suffixes ("linkedin.com") stay
    // legal because the HOSTS table has to name them to recognise a URL somebody else wrote.
    /https:\/\/(www\.)?(twitter|x|instagram|tiktok|youtube|linkedin|github)\.com/i,
  ];
  for (const text of sources) {
    for (const pattern of forbidden) {
      assert.ok(!pattern.test(text), `inspector source matches ${pattern}`);
    }
  }
});

/* ---------- identity links ---------- */

test("the identity a source keyed on leads the row", () => {
  const links = identityLinks("https://www.linkedin.com/in/rick-kempinski-66537243", [], []);

  assert.deepEqual(links.map((l) => l.network), ["linkedin"]);
  assert.equal(links[0].label, "LinkedIn");
});

test("every network we could hold is recognised by host, not by the field it arrived in", () => {
  const links = identityLinks(null, [
    "https://x.com/someone",
    "https://twitter.com/legacy",
    "https://www.instagram.com/someone",
    "https://youtube.com/@someone",
    "https://www.tiktok.com/@someone",
    "https://github.com/someone",
    "https://www.facebook.com/someone",
    "https://someone.substack.com",
    "https://luma.com/thecommons",
    "https://thesfcommons.com",
  ], []);

  assert.deepEqual(links.map((l) => l.network), [
    "x", "x", "instagram", "youtube", "tiktok", "github", "facebook", "substack", "luma", "website",
  ]);
});

test("the same identity reached twice is drawn once", () => {
  const links = identityLinks("https://www.linkedin.com/in/someone", [
    "https://www.linkedin.com/in/someone/",
    "https://www.linkedin.com/in/someone",
  ], ["https://www.linkedin.com/in/someone"]);

  assert.equal(links.length, 1);
});

test("a spelling the storage layer normalises away is not drawn twice", () => {
  // Measured live: 27 entities hold a canonical_profile_url and an official_website differing only
  // by the `www.` prefix, and a `profile` fact written before twitter.com -> x.com normalisation
  // sits beside the normalised source URL for the same account.  Both used to render two adjacent,
  // visually identical marks with the identical accessible name and the identical destination.
  const bareAndWww = identityLinks("https://bravenewspaces.org/", [
    "https://www.bravenewspaces.org/",
  ], []);
  assert.equal(bareAndWww.length, 1, "www. and bare are one site");

  const legacyAndCurrent = identityLinks(null, ["https://twitter.com/jonbng"], [
    "https://x.com/jonbng",
  ]);
  assert.equal(legacyAndCurrent.length, 1, "twitter.com and x.com are one platform");
  assert.equal(legacyAndCurrent[0].network, "x");
});

test("an http link is not rendered at all", () => {
  // Every stored URL passes an `^https://` validator, so an http: value did not arrive through one.
  assert.deepEqual(identityLinks("http://www.linkedin.com/in/someone", [], []), []);
});

test("a platform's own front page is not somebody's profile", () => {
  const links = identityLinks(null, ["https://substack.com", "https://luma.com/"], []);

  assert.deepEqual(links.map((l) => l.network), ["website", "website"]);
});

test("only links a browser will follow safely are drawn", () => {
  const links = identityLinks(null, [
    "javascript:alert(1)",
    "data:text/html,<script>x</script>",
    "not a url",
    null,
    undefined,
    "",
  ], []);

  assert.deepEqual(links, []);
});

test("an unknown host is still offered, labelled by its own name", () => {
  const links = identityLinks(null, ["https://www.andluo.studio/about"], []);

  assert.deepEqual(links.map((l) => [l.network, l.label]), [["website", "andluo.studio"]]);
});



test("compact profile handles come from the stored URL", () => {
  const links = identityLinks("https://www.linkedin.com/in/gussayer", [], [
    "https://x.com/augustinsayer", "https://www.instagram.com/itslauradang",
  ]);
  assert.deepEqual(links.map(({ label, handle }) => [label, handle]), [
    ["LinkedIn", "gussayer"], ["X", "augustinsayer"], ["Instagram", "itslauradang"],
  ]);
  assert.equal(identityLinks(null, ["https://youtube.com/@someone"])[0].handle, "someone");
  const channel = identityLinks(null, ["https://youtube.com/channel/UC123"])[0];
  assert.equal(channel.handle, undefined, "a channel ID is not a handle");
  assert.equal(channel.href, "https://youtube.com/channel/UC123");
});


test("selected event details are bounded, expire and clear across accounts", () => {
  clearEntityGraphCache();
  const event = { canonical_event_id: uuid("bbbbbbbb", 1), title: "First occurrence" };
  writeGraphEvent("tenant-a", event, 0);
  assert.equal(readGraphEvent("tenant-a", event.canonical_event_id, 10), event);
  assert.equal(readGraphEvent("tenant-b", event.canonical_event_id, 10), null);
  assert.equal(readGraphEvent(null, event.canonical_event_id, 10), null);
  assert.equal(readGraphEvent("tenant-a", event.canonical_event_id, 300_001), null);
  for (let index = 0; index < 100; index += 1) {
    writeGraphEvent("tenant-a", { ...event, canonical_event_id: uuid("bbbbbbbb", index) }, index);
  }
  assert.equal(entityGraphCacheSizes().events, 48);
  assert.equal(readGraphEvent("tenant-a", uuid("bbbbbbbb", 0), 110), null);
  assert.notEqual(readGraphEvent("tenant-a", uuid("bbbbbbbb", 99), 110), null);
  clearEntityGraphCache();
  assert.equal(entityGraphCacheSizes().events, 0);
  assert.equal(readGraphEvent("tenant-a", uuid("bbbbbbbb", 99), 110), null);
});
