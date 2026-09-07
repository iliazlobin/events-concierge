import { deriveEntityGraphScene } from "./entity-graph.ts";
import type {
  CatalogEntityGraph,
  CatalogEntityGraphNode,
  EntityGraphBounds,
  EntityGraphPlacement,
  EntityGraphScene,
  EntityGraphSceneModel,
  RawCatalogEntityGraph,
} from "./entity-graph.ts";

/**
 * `layoutEgoRings` — a deterministic concentric bipartite ring layout.
 *
 * A pure function of `(scene, width, height)`. No DOM, no `Math.random`, no
 * `Date.now`, no iteration to convergence, and no force simulation of any kind.
 * The same scene and the same viewport produce byte-identical coordinates on
 * every call and in every process, which is the property that makes a deep link
 * land on the same picture twice and makes a screenshot usable as evidence.
 *
 * A force layout would give up all of that: with ~808 connected components and
 * a long tail at degree 1 it settles differently on every reload, needs an
 * animation loop that `prefers-reduced-motion` readers must be excluded from,
 * and puts a synchronous settle in front of first paint.
 *
 * ## Complexity
 *
 * O(N log N) for N nodes, dominated by the ring sorts inside
 * {@link deriveEntityGraphScene} and by the one angular sort on the peer ring.
 * Every other step is a single linear pass: ring sizing, arc placement, the
 * one-shot angular relaxation, and the bounds sweep. N is hard-capped at 79
 * (1 ego + 24 events + 48 peers + 6 topics) and E at 600 by the SQL that
 * produces the payload, so the measured envelope is well under 0.5 ms.
 *
 * ## The rings
 *
 * - Ring 0: the ego, alone, at the world origin. "Where am I" never depends on
 *   position.
 * - Ring 1: events. This is the bipartite spine — a peer is never joined to the
 *   ego directly, only through an event node that carries the role and source.
 * - Ring 2: peers, each anchored at the circular mean of the events it shares
 *   with the ego, so it sits physically beside the evidence connecting it.
 * - Ring 3: topics, the ego's own top-N, placed in the widest angular gap the
 *   peer ring leaves.
 *
 * ## Degenerate cases, all handled explicitly
 *
 * - **One node.** An ego with no events at all lays out as a single placement
 *   at the origin; `bounds` is that node's own box, never an empty rect.
 * - **The isolated entity** (11.5% of the catalog: no co-mention peers at all).
 *   The event ring is never empty — every entity has at least one mention by
 *   construction — so this draws a real figure, not an empty state. Ring 2 is
 *   skipped and ring 3 moves inward to take its place rather than leaving a
 *   blank annulus.
 * - **A single event.** Placed at 12 o'clock, directly above the ego, never at
 *   an arbitrary angle. Rings with fewer than four events are spread over the
 *   full circle for the same reason: a clock face needs a face.
 * - **The 79-node maximum.** Ring radii are circumference-driven, so a ring is
 *   exactly big enough to hold its members without collision and never bigger.
 *   The bound is proved, not hoped for: see {@link ringRadiusFor}.
 */

const TAU = Math.PI * 2;

/** Node radius floor and ceiling, in world px. */
const NODE_MIN_RADIUS = 11;
/**
 * Exported because the canvas needs it for its fit padding. Every other consumer of a node's size
 * reads {@link EntityGraphPlacement.radius}, which this module computed — there is deliberately no
 * second radius curve anywhere, because the relaxation pass's no-overlap guarantee holds only
 * while the drawn radius equals the reserved one.
 */
export const NODE_MAX_RADIUS = 34;
/**
 * Degree maps to radius on a log scale. Entity degree spans roughly 1..17 and a
 * linear map makes 90% of the catalog one indistinguishable size.
 */
const NODE_RADIUS_SCALE = 8.5;
/** The ego is drawn slightly larger than its degree alone would give it. */
const EGO_RADIUS_SCALE = 1.15;

/** Minimum clear space between two adjacent nodes on the same ring. */
const NODE_PAD = 14;
/** Minimum clear space between a ring's outer edge and the next ring's nodes. */
const RING_GAP = 26;
/**
 * Radial space reserved between rings for the label a node carries. Labels are
 * DOM, rendered outside the node's own circle, so without this reserve a ring-2
 * label would sit on top of a ring-1 node at the same angle.
 */
const LABEL_BAND = 18;

/** Padding kept between the outermost ring and the viewport edge when spreading. */
const VIEWPORT_MARGIN = 48;
/**
 * Rings may expand to fill a generous viewport but never contract: contracting
 * below the circumference-driven radius would reintroduce collisions. The cap
 * stops a three-node ego from being flung to the corners of a large window.
 */
const MAX_SPREAD = 1.8;

/** Below this many events the clock face collapses into a wedge, so it is not used. */
const CLOCK_MIN_EVENTS = 4;

export interface EntityGraphViewport {
  width: number;
  height: number;
}

/** Node radius from measured connection count. Nothing else feeds size. */
export function nodeRadius(degree: number): number {
  const safe = Number.isFinite(degree) ? Math.max(degree, 0) : 0;
  return Math.min(NODE_MAX_RADIUS, NODE_MIN_RADIUS + NODE_RADIUS_SCALE * Math.log1p(safe));
}

function egoRadius(node: CatalogEntityGraphNode | null): number {
  return nodeRadius(node?.degree ?? 0) * EGO_RADIUS_SCALE;
}

/** Fold any angle into `[0, TAU)`. */
function normalizeAngle(angle: number): number {
  const wrapped = angle % TAU;
  return wrapped < 0 ? wrapped + TAU : wrapped;
}

interface Arc {
  /** How many members share this arc. */
  count: number;
  /** Angular width in radians. */
  span: number;
}

/**
 * The smallest radius at which a ring can hold its members.
 *
 * Two constraints, whichever binds harder:
 *
 * 1. **Clearance.** The ring must clear the ring inside it, including the band
 *    reserved for that ring's labels.
 * 2. **Circumference.** `count` members of radius `r` placed evenly across an
 *    arc of `span` radians need `count * (2r + NODE_PAD) / span` radius for
 *    adjacent members to stay `NODE_PAD` apart. Sizing from this, rather than
 *    from a fixed constant, is what stops a three-node ego from being marooned
 *    inside an 800 px void and a 48-peer ego from overlapping itself.
 *
 * Constraint 2 is also the proof the relaxation pass relies on: with radius at
 * least the circumference requirement, the sum of the required angular
 * separations around the ring is at most TAU, so a feasible arrangement exists.
 */
function ringRadiusFor(
  arcs: readonly Arc[],
  maxMemberRadius: number,
  innerEdge: number,
): number {
  let radius = innerEdge + LABEL_BAND + maxMemberRadius + RING_GAP;
  for (const arc of arcs) {
    if (arc.count <= 0 || arc.span <= 0) continue;
    radius = Math.max(radius, (arc.count * (2 * maxMemberRadius + NODE_PAD)) / arc.span);
  }
  return radius;
}

/**
 * Angles for `count` members across one arc.
 *
 * A wrapping arc (the whole circle) places its first member exactly at the arc
 * start, so a lone event lands at 12 o'clock rather than at 6. A bounded arc
 * insets by half a step, so the two halves of the clock face cannot touch at
 * the 3 o'clock and 9 o'clock seams.
 */
function arcAngles(count: number, start: number, span: number, wrapping: boolean): number[] {
  if (count <= 0) return [];
  const step = span / count;
  const offset = wrapping ? 0 : step / 2;
  const angles: number[] = [];
  for (let index = 0; index < count; index += 1) {
    angles.push(normalizeAngle(start + offset + index * step));
  }
  return angles;
}

/**
 * Screen-space position of a point on a ring.
 *
 * Angle 0 is 12 o'clock and angle increases clockwise, matching how the event
 * ring is read and how the keyboard's Left/Right arrows step around it.
 */
function ringPoint(angle: number, radius: number): { x: number; y: number } {
  return { x: radius * Math.sin(angle), y: -radius * Math.cos(angle) };
}

interface RingMember {
  node: CatalogEntityGraphNode;
  angle: number;
  radius: number;
}

/**
 * One relaxation pass over a ring, exact and non-iterative.
 *
 * Members arrive at their desired angles — for peers, the mean angle of their
 * shared events — which can bunch several into one place. The gaps between
 * consecutive members are redistributed so that every gap reaches its required
 * separation, taking the shortfall proportionally out of the gaps that have
 * room to give. Because {@link ringRadiusFor} guarantees the required
 * separations sum to at most TAU, the surplus always covers the deficit, so one
 * pass is exact and there is no convergence criterion to state.
 *
 * The first member keeps its desired angle, so the ring stays anchored to its
 * evidence rather than rotating as a whole.
 *
 * O(n) given a ring already sorted by angle.
 */
function relaxRing(members: readonly RingMember[], ringRadius: number): number[] {
  const count = members.length;
  if (count === 0) return [];
  if (count === 1 || ringRadius <= 0) return members.map((member) => member.angle);

  const required: number[] = [];
  const gaps: number[] = [];
  for (let index = 0; index < count; index += 1) {
    const current = members[index];
    const next = members[(index + 1) % count];
    required.push((current.radius + next.radius + NODE_PAD) / ringRadius);
    const raw = next.angle - current.angle;
    gaps.push(index === count - 1 ? raw + TAU : raw);
  }

  let deficit = 0;
  let surplus = 0;
  for (let index = 0; index < count; index += 1) {
    deficit += Math.max(0, required[index] - gaps[index]);
    surplus += Math.max(0, gaps[index] - required[index]);
  }
  if (deficit <= 0) return members.map((member) => member.angle);

  // Never above 1: the guard is for a caller that shrank the ring by hand.
  const take = surplus > 0 ? Math.min(1, deficit / surplus) : 1;
  const angles: number[] = [members[0].angle];
  for (let index = 1; index < count; index += 1) {
    const gap = gaps[index - 1];
    const need = required[index - 1];
    const adjusted = gap >= need ? gap - (gap - need) * take : need;
    angles.push(angles[index - 1] + adjusted);
  }
  return angles.map(normalizeAngle);
}

/** The circular mean of a set of angles; null when they cancel out exactly. */
function circularMean(angles: readonly number[]): number | null {
  if (angles.length === 0) return null;
  let sumSin = 0;
  let sumCos = 0;
  for (const angle of angles) {
    sumSin += Math.sin(angle);
    sumCos += Math.cos(angle);
  }
  // Antipodal inputs cancel and leave the mean undefined; the caller falls back
  // to the first shared event, which is deterministic and beside real evidence.
  if (Math.abs(sumSin) < 1e-9 && Math.abs(sumCos) < 1e-9) return null;
  return normalizeAngle(Math.atan2(sumSin, sumCos));
}

/** The widest gap between consecutive angles on a ring, as `{ start, span }`. */
function widestGap(angles: readonly number[]): { start: number; span: number } {
  if (angles.length === 0) return { start: 0, span: TAU };
  const sorted = [...angles].sort((left, right) => left - right);
  if (sorted.length === 1) {
    return { start: normalizeAngle(sorted[0] + Math.PI / 2), span: Math.PI };
  }
  let bestStart = sorted[sorted.length - 1];
  let bestSpan = sorted[0] + TAU - sorted[sorted.length - 1];
  for (let index = 0; index + 1 < sorted.length; index += 1) {
    const span = sorted[index + 1] - sorted[index];
    if (span > bestSpan) {
      bestSpan = span;
      bestStart = sorted[index];
    }
  }
  return { start: bestStart, span: bestSpan };
}

function maxRadiusOf(nodes: readonly CatalogEntityGraphNode[]): number {
  let largest = 0;
  for (const node of nodes) largest = Math.max(largest, nodeRadius(node.degree));
  return largest;
}

function boundsOf(placements: readonly EntityGraphPlacement[]): EntityGraphBounds {
  if (placements.length === 0) return { minX: 0, minY: 0, maxX: 0, maxY: 0 };
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const placement of placements) {
    minX = Math.min(minX, placement.x - placement.radius);
    minY = Math.min(minY, placement.y - placement.radius);
    maxX = Math.max(maxX, placement.x + placement.radius);
    maxY = Math.max(maxY, placement.y + placement.radius);
  }
  return { minX, minY, maxX, maxY };
}

/**
 * How far the rings expand to use the viewport.
 *
 * Always at least 1: shrinking a ring below its circumference-driven radius
 * would reintroduce the collisions the sizing exists to prevent. A viewport of
 * zero, NaN, or a smaller size than the figure needs therefore leaves the
 * layout exactly as computed and lets the camera's fit control handle it.
 */
function spreadFactor(outerExtent: number, viewport: EntityGraphViewport): number {
  const width = Number.isFinite(viewport.width) ? viewport.width : 0;
  const height = Number.isFinite(viewport.height) ? viewport.height : 0;
  const usable = Math.min(width, height) / 2 - VIEWPORT_MARGIN;
  if (!(outerExtent > 0) || !(usable > 0)) return 1;
  return Math.min(MAX_SPREAD, Math.max(1, usable / outerExtent));
}

/**
 * Lay one scene out into world coordinates.
 *
 * Pure and total: every node in the model receives exactly one placement, in
 * the model's own reading order (ego, events clockwise from 12, peers, topics),
 * so the DOM node layer's tab order is the array order and needs no second sort.
 */
export function layoutEgoRings(
  scene: EntityGraphSceneModel,
  viewport: EntityGraphViewport,
): EntityGraphScene {
  const { ego, events, peers, topics } = scene;

  const egoDrawRadius = egoRadius(ego);
  const placements: EntityGraphPlacement[] = [];

  /* ---- Ring 1: events, on a clock face. ---------------------------- */

  const upcoming = events.filter((node) => node.is_past !== true);
  const past = events.filter((node) => node.is_past === true);
  // Four events is the floor for a clock: below it a half-circle becomes a
  // wedge or a single angle, which reads as a rendering fault rather than as
  // "nothing upcoming". Below the floor the events spread over the full circle.
  const useClock = events.length >= CLOCK_MIN_EVENTS;

  const eventMaxRadius = maxRadiusOf(events);
  const eventArcs: Arc[] = useClock
    ? [{ count: upcoming.length, span: Math.PI }, { count: past.length, span: Math.PI }]
    : [{ count: events.length, span: TAU }];
  const eventRingRadius = events.length > 0
    ? ringRadiusFor(eventArcs, eventMaxRadius, egoDrawRadius)
    : egoDrawRadius;

  const eventAngles = new Map<string, number>();
  if (useClock) {
    // Upcoming across 0deg-180deg ascending, past across 180deg-360deg
    // descending, clockwise from 12. An entity with nothing upcoming therefore
    // draws an empty upper half, which is the fact and not a defect.
    const upcomingAngles = arcAngles(upcoming.length, 0, Math.PI, false);
    upcoming.forEach((node, index) => eventAngles.set(node.node_id, upcomingAngles[index]));
    const pastAngles = arcAngles(past.length, Math.PI, Math.PI, false);
    past.forEach((node, index) => eventAngles.set(node.node_id, pastAngles[index]));
  } else {
    const angles = arcAngles(events.length, 0, TAU, true);
    events.forEach((node, index) => eventAngles.set(node.node_id, angles[index]));
  }

  /* ---- Ring 2: peers, anchored to their evidence. ------------------ */

  const peerMaxRadius = maxRadiusOf(peers);
  const peerInnerEdge = events.length > 0
    ? eventRingRadius + eventMaxRadius
    : egoDrawRadius;
  const peerRingRadius = peers.length > 0
    ? ringRadiusFor([{ count: peers.length, span: TAU }], peerMaxRadius, peerInnerEdge)
    : peerInnerEdge;

  const peerMembers: RingMember[] = peers.map((node, index) => {
    const shared = scene.peerEvents.get(node.node_id) ?? [];
    const sharedAngles = shared
      .map((eventId) => eventAngles.get(eventId))
      .filter((angle): angle is number => typeof angle === "number");
    const anchor = circularMean(sharedAngles)
      ?? sharedAngles[0]
      // A peer with no drawn shared event cannot happen in a well-formed
      // payload; spreading by rank keeps the layout total if one arrives.
      ?? normalizeAngle((index * TAU) / Math.max(peers.length, 1));
    return { node, angle: anchor, radius: nodeRadius(node.degree) };
  });
  // Sorting by angle is what the relaxation pass assumes; the node id breaks
  // ties so two peers anchored on the same event never swap between runs.
  peerMembers.sort((left, right) => (
    left.angle !== right.angle
      ? left.angle - right.angle
      : (left.node.node_id < right.node.node_id ? -1 : left.node.node_id > right.node.node_id ? 1 : 0)
  ));
  const peerAngles = relaxRing(peerMembers, peerRingRadius);

  /* ---- Ring 3: topics, in the widest gap the peers leave. ---------- */

  const topicMaxRadius = maxRadiusOf(topics);
  const topicInnerEdge = peers.length > 0
    ? peerRingRadius + peerMaxRadius
    : peerInnerEdge;
  // The topic ring sits outside the peer ring, so the gap is chosen for reading
  // rather than for collision. A gap narrower than a quadrant would crush six
  // topics into a sliver, so below that the ring takes the whole circle.
  const peerGap = widestGap(peerAngles);
  const topicWrapping = peers.length === 0 || peerGap.span < Math.PI / 2;
  const topicSpan = topicWrapping ? TAU : peerGap.span;
  const topicStart = topicWrapping ? 0 : peerGap.start;
  const topicRingRadius = topics.length > 0
    ? ringRadiusFor([{ count: topics.length, span: topicSpan }], topicMaxRadius, topicInnerEdge)
    : topicInnerEdge;
  const topicAngles = arcAngles(topics.length, topicStart, topicSpan, topicWrapping);

  /* ---- Spread to the viewport, then emit. -------------------------- */

  const outerExtent = Math.max(
    egoDrawRadius,
    events.length > 0 ? eventRingRadius + eventMaxRadius : 0,
    peers.length > 0 ? peerRingRadius + peerMaxRadius : 0,
    topics.length > 0 ? topicRingRadius + topicMaxRadius : 0,
  );
  const spread = spreadFactor(outerExtent, viewport);

  if (ego) {
    placements.push({ node_id: ego.node_id, x: 0, y: 0, radius: egoDrawRadius });
  }
  for (const node of events) {
    const angle = eventAngles.get(node.node_id) ?? 0;
    const point = ringPoint(angle, eventRingRadius * spread);
    placements.push({
      node_id: node.node_id,
      x: point.x,
      y: point.y,
      radius: nodeRadius(node.degree),
    });
  }
  peerMembers.forEach((member, index) => {
    const point = ringPoint(peerAngles[index], peerRingRadius * spread);
    placements.push({
      node_id: member.node.node_id,
      x: point.x,
      y: point.y,
      radius: member.radius,
    });
  });
  topics.forEach((node, index) => {
    const point = ringPoint(topicAngles[index], topicRingRadius * spread);
    placements.push({
      node_id: node.node_id,
      x: point.x,
      y: point.y,
      radius: nodeRadius(node.degree),
    });
  });

  // Peers are emitted in relaxed-angle order rather than in rank order, so the
  // list is restored to the model's reading order for the tab sequence.
  const rank = new Map<string, number>();
  scene.order.forEach((nodeId, index) => rank.set(nodeId, index));
  placements.sort((left, right) => (
    (rank.get(left.node_id) ?? Number.MAX_SAFE_INTEGER)
    - (rank.get(right.node_id) ?? Number.MAX_SAFE_INTEGER)
  ));

  return { graph: scene.graph, placements, bounds: boundsOf(placements) };
}

/**
 * Derive and lay out in one call, for callers holding a raw payload.
 *
 * Identical output to `layoutEgoRings(deriveEntityGraphScene(graph), viewport)`;
 * it exists so a component does not have to remember the two-step.
 */
export function layoutCatalogEntityGraph(
  graph: CatalogEntityGraph | RawCatalogEntityGraph,
  viewport: EntityGraphViewport,
): EntityGraphScene {
  return layoutEgoRings(deriveEntityGraphScene(graph), viewport);
}

/* ================================================================== *
 * The overview layout: many hubs, no ego.
 * ================================================================== */

/**
 * `layoutEntityOverview` - a deterministic component-packed layout for the
 * landing graph.
 *
 * The ring layout above answers "what surrounds this one entity". The landing
 * graph asks a different question - "which hubs are there, and how do they
 * interconnect" - and concentric rings are the wrong instrument for it: with no
 * ego there is no centre to be concentric about, and forty hubs placed on one
 * circle would put every drawn connection through the middle of the figure as a
 * chord, which is the picture a reader cannot untangle.
 *
 * The payload it lays out keeps the same bipartite spine as the ego graph:
 * entity -> event -> entity, never a synthesised entity-to-entity edge. What
 * the server samples is *which* event stands for a connected pair - one
 * representative, the most recent shared one - and the view discloses that in
 * words. This module draws whatever it is handed and samples nothing.
 *
 * ## Guarantees
 *
 * Pure and total. No `Math.random`, no `Date.now`, no `performance.now`, no
 * force simulation, no iteration to convergence. Every ordering is total (node
 * id is the last tiebreaker everywhere) and every nudge loop is bounded by a
 * constant, so identical input produces byte-identical output in every process
 * - which is what makes a screenshot usable as evidence and a deep link land on
 * the same picture twice.
 *
 * The only way the viewport enters the result is through the row-packing target
 * width and the isolated block's column count, both derived from the aspect
 * ratio alone. Fixing the viewport therefore fixes the output completely.
 *
 * ## The shape
 *
 * 1. Connected components over the drawn nodes and edges.
 * 2. Components holding more than one node are *clusters*; a lone entity with
 *    no drawn connection is *isolated*.
 * 3. Inside a cluster, entities sit on a circle in depth-first order over the
 *    entity projection, so two entities joined by a bridge land near each other
 *    and the chords stay short. Each bridge event is then placed at the
 *    centroid of the entities it joins - literally between its endpoints - and
 *    pushed toward the cluster centre only as far as it must be to stop
 *    overlapping them.
 * 4. Clusters are packed largest-first into shelves (rows) whose target width
 *    comes from the viewport's aspect ratio.
 * 5. Isolated entities are packed into a compact grid below everything, behind
 *    a gutter more than twice the size of the one between clusters, so the
 *    block reads as "these have no drawn connection" rather than as noise.
 * 6. The finished figure is translated so its bounding box is centred on the
 *    world origin, which is where the canvas's clock-angle tab order and its
 *    fit camera both expect to find it.
 *
 * ## Complexity and termination
 *
 * O(N log N + E) for N nodes and E edges - the sorts dominate; the component
 * sweep, the depth-first order, the entity placement and the bounds sweep are
 * each one linear pass. The bridge placement is the only step that is not a
 * single traversal: each bridge tries at most
 * `1 + OVERVIEW_CLEAR_RINGS * OVERVIEW_CLEAR_ANGLES` = 81 candidate positions
 * against at most `N` already-placed discs, so the worst case is `81 * B * N`
 * distance tests - at the capacity this view requests (60 hubs + 60 bridges)
 * under 600k, measured at well under a millisecond, and in practice almost
 * every bridge is clear at its first candidate. It cannot spin: the ring budget
 * is a compile-time constant and the anchor is kept when no candidate comes
 * back clear, so every node receives exactly one placement whatever happens and
 * none is ever dropped for want of room.
 *
 * ## Degenerate cases, all handled explicitly
 *
 * - **No nodes.** Empty placements and a zero bounding box, never NaN.
 * - **One node.** A single placement at the origin.
 * - **Every entity isolated** (the 40 top hubs share nothing). No cluster
 *   shelves at all; the isolated grid becomes the whole figure and the gutter
 *   above it is not drawn, because there is nothing above it.
 * - **A cluster of one entity carrying bridges.** Possible only if the server
 *   emits a bridge whose other endpoint was not drawn. The entity takes the
 *   centre and its bridges ring it, rather than the circle collapsing to a
 *   point.
 * - **Capacity.** 60 entities and 60 bridges, the widest the endpoint's caps
 *   allow. Cluster radii are circumference-driven, so a cluster is exactly big
 *   enough for its members.
 */

/** Circumference reserved per cluster member for the label the DOM draws beneath it. */
const OVERVIEW_LABEL_PAD = 26;
/** Gutter between two packed clusters. */
const OVERVIEW_COMPONENT_GAP = 56;
/**
 * Gutter above the isolated block. Deliberately more than twice
 * {@link OVERVIEW_COMPONENT_GAP}: the block means something different from the
 * clusters, and the only thing carrying that meaning in the geometry is the gap.
 */
const OVERVIEW_ISOLATED_GAP = 132;
/**
 * Smallest cluster circle, so a two-entity pair is not drawn on top of itself.
 *
 * 64 was measured and rejected. It does shrink the packed figure — the drawn height fell from 451px
 * to 423px — but a tighter cluster leaves less clear space around each node, and the canvas's greedy
 * label pass then drops far more names: visible labels fell from 26 to 15 of 53. Height is cheap
 * here (the reader can pan) and names are not, so the ring stays generous.
 */
const OVERVIEW_MIN_RING = 96;
/**
 * The bounded search a bridge uses to find clear space, and the whole reason the
 * placement pass provably terminates: at most
 * `1 + OVERVIEW_CLEAR_RINGS * OVERVIEW_CLEAR_ANGLES` candidate positions are
 * tried, then the anchor is kept whatever the answer.
 */
const OVERVIEW_CLEAR_RINGS = 10;
const OVERVIEW_CLEAR_ANGLES = 8;
/** Aspect ratios outside this range pack into unreadable ribbons. */
const OVERVIEW_MIN_ASPECT = 0.5;
const OVERVIEW_MAX_ASPECT = 3;
const OVERVIEW_DEFAULT_ASPECT = 1.5;

interface OverviewPoint {
  x: number;
  y: number;
}

interface OverviewDisc extends OverviewPoint {
  radius: number;
}

interface OverviewBox {
  /** Local placements, before the component is translated into its shelf slot. */
  placements: EntityGraphPlacement[];
  width: number;
  height: number;
  /** Where the local origin sits inside the box, so translation is one addition. */
  offsetX: number;
  offsetY: number;
  /** Sort key components, kept explicit so the ordering is auditable. */
  entityCount: number;
  nodeCount: number;
  leadNodeId: string;
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

/** Entities before bridges, then most connected first, then label, then id. */
function compareOverviewNodes(
  left: CatalogEntityGraphNode,
  right: CatalogEntityGraphNode,
): number {
  const leftRank = left.node_kind === "entity" ? 0 : left.node_kind === "event" ? 1 : 2;
  const rightRank = right.node_kind === "entity" ? 0 : right.node_kind === "event" ? 1 : 2;
  if (leftRank !== rightRank) return leftRank - rightRank;
  if (left.degree !== right.degree) return right.degree - left.degree;
  const byLabel = compareText(left.label, right.label);
  return byLabel !== 0 ? byLabel : compareText(left.node_id, right.node_id);
}

function separationOf(from: OverviewPoint, to: OverviewPoint): number {
  return Math.hypot(from.x - to.x, from.y - to.y);
}

function pushAdjacency(index: Map<string, string[]>, from: string, to: string): void {
  const existing = index.get(from);
  if (existing) existing.push(to);
  else index.set(from, [to]);
}

/**
 * The aspect ratio the packer targets.
 *
 * Clamped rather than trusted: a zero-height viewport (a hidden tab, a first
 * paint before measurement) would otherwise produce Infinity and NaN
 * coordinates, and the layout must stay total.
 */
function overviewAspect(viewport: EntityGraphViewport): number {
  const width = Number.isFinite(viewport.width) ? viewport.width : 0;
  const height = Number.isFinite(viewport.height) ? viewport.height : 0;
  if (!(width > 0) || !(height > 0)) return OVERVIEW_DEFAULT_ASPECT;
  return Math.min(OVERVIEW_MAX_ASPECT, Math.max(OVERVIEW_MIN_ASPECT, width / height));
}

/**
 * Connected components over the drawn nodes, in a canonical order.
 *
 * Seeds are taken in {@link compareOverviewNodes} order and each frontier is
 * expanded in that same order, so the traversal - and therefore every
 * component's member list - is identical on every run.
 */
function overviewComponents(
  ordered: readonly CatalogEntityGraphNode[],
  rank: ReadonlyMap<string, number>,
  adjacency: ReadonlyMap<string, string[]>,
): CatalogEntityGraphNode[][] {
  const seen = new Set<string>();
  const components: CatalogEntityGraphNode[][] = [];
  for (const seed of ordered) {
    if (seen.has(seed.node_id)) continue;
    seen.add(seed.node_id);
    const queue = [seed.node_id];
    const members: string[] = [];
    for (let cursor = 0; cursor < queue.length; cursor += 1) {
      const current = queue[cursor];
      members.push(current);
      for (const neighbor of adjacency.get(current) ?? []) {
        if (seen.has(neighbor)) continue;
        seen.add(neighbor);
        queue.push(neighbor);
      }
    }
    members.sort((left, right) => (
      (rank.get(left) ?? Number.MAX_SAFE_INTEGER) - (rank.get(right) ?? Number.MAX_SAFE_INTEGER)
    ));
    components.push(members.map((nodeId) => ordered[rank.get(nodeId) as number]));
  }
  return components;
}

/**
 * Depth-first order over the entity projection of one cluster.
 *
 * The projection is for *ordering only* and is never drawn: two entities are
 * adjacent in it when a drawn bridge names both. Walking it depth-first and
 * seating entities on the circle in that order keeps a pair's two endpoints
 * close together, which is what stops a cluster's chords from crossing in the
 * middle. Any entity the walk cannot reach - impossible inside a connected
 * component, but cheap to survive - is appended in rank order.
 */
function overviewEntityOrder(
  entities: readonly CatalogEntityGraphNode[],
  adjacency: ReadonlyMap<string, string[]>,
  entityIds: ReadonlySet<string>,
): CatalogEntityGraphNode[] {
  const byId = new Map(entities.map((node) => [node.node_id, node]));
  const localRank = new Map<string, number>();
  entities.forEach((node, index) => localRank.set(node.node_id, index));
  const byRank = (left: string, right: string): number => (
    (localRank.get(left) ?? Number.MAX_SAFE_INTEGER)
    - (localRank.get(right) ?? Number.MAX_SAFE_INTEGER)
  );

  const projection = new Map<string, string[]>();
  for (const entity of entities) {
    const reached = new Set<string>();
    for (const bridge of adjacency.get(entity.node_id) ?? []) {
      if (entityIds.has(bridge)) continue;
      for (const other of adjacency.get(bridge) ?? []) {
        if (other !== entity.node_id && byId.has(other)) reached.add(other);
      }
    }
    projection.set(entity.node_id, [...reached].sort(byRank));
  }

  const seen = new Set<string>();
  const walk: CatalogEntityGraphNode[] = [];
  for (const seed of entities) {
    if (seen.has(seed.node_id)) continue;
    // An explicit stack, not recursion: the visit order is then the sorted one
    // by construction rather than by call-order intuition.
    const stack = [seed.node_id];
    while (stack.length > 0) {
      const current = stack.pop() as string;
      if (seen.has(current)) continue;
      seen.add(current);
      const node = byId.get(current);
      if (node) walk.push(node);
      const next = projection.get(current) ?? [];
      // Pushed in reverse so the highest-ranked neighbour is popped first.
      for (let index = next.length - 1; index >= 0; index -= 1) {
        if (!seen.has(next[index])) stack.push(next[index]);
      }
    }
  }
  return walk;
}

/**
 * The angular offsets a widening search tries, in order: straight toward the
 * circle centre first, then either side of it, then finally outward.
 *
 * Fixed and shared rather than rebuilt per bridge, because it is the same list
 * every time and because a reader auditing determinism should be able to see
 * the whole search order in one place.
 */
const OVERVIEW_SEARCH_OFFSETS: readonly number[] = (() => {
  const offsets = [0];
  const stride = TAU / OVERVIEW_CLEAR_ANGLES;
  for (let index = 1; index <= OVERVIEW_CLEAR_ANGLES / 2; index += 1) {
    offsets.push(index * stride);
    if (index !== OVERVIEW_CLEAR_ANGLES / 2) offsets.push(-index * stride);
  }
  return offsets;
})();

/** True when a disc of this radius at this point touches nothing already placed. */
function isClear(
  point: OverviewPoint,
  radius: number,
  obstacles: readonly OverviewDisc[],
): boolean {
  for (const obstacle of obstacles) {
    if (separationOf(point, obstacle) < obstacle.radius + radius + NODE_PAD) return false;
  }
  return true;
}

/**
 * The anchor if it is clear, otherwise the nearest clear position to it.
 *
 * A bounded, deterministic, widening ring search: the anchor, then eight
 * directions at one node-width out, then at two, up to
 * {@link OVERVIEW_CLEAR_RINGS}. Directions are ordered from the circle centre
 * outward, so a bridge that must move moves *into* its cluster rather than away
 * from the two hubs it is explaining. When nothing comes back clear the anchor
 * is kept: a bridge sitting where it means something and slightly overlapping is
 * a better picture than one flung to the edge of the figure, and it is the
 * outcome a reader can still make sense of.
 */
function clearPosition(
  anchor: OverviewPoint,
  radius: number,
  obstacles: readonly OverviewDisc[],
): OverviewPoint {
  if (isClear(anchor, radius, obstacles)) return anchor;
  const toCentre = Math.hypot(anchor.x, anchor.y);
  const baseAngle = toCentre > 1e-6 ? Math.atan2(-anchor.y, -anchor.x) : 0;
  const step = 2 * radius + NODE_PAD;
  for (let ring = 1; ring <= OVERVIEW_CLEAR_RINGS; ring += 1) {
    for (const offset of OVERVIEW_SEARCH_OFFSETS) {
      const angle = baseAngle + offset;
      const candidate = {
        x: anchor.x + Math.cos(angle) * step * ring,
        y: anchor.y + Math.sin(angle) * step * ring,
      };
      if (isClear(candidate, radius, obstacles)) return candidate;
    }
  }
  return anchor;
}

/**
 * Lay one cluster out around its own origin.
 *
 * Entities take a circle sized by circumference, exactly as
 * {@link ringRadiusFor} sizes a ring: enough room for every member plus its
 * label, and never more. Bridges are then placed between the entities they
 * join, so the geometry states the connection rather than decorating it.
 */
function layoutOverviewCluster(
  members: readonly CatalogEntityGraphNode[],
  adjacency: ReadonlyMap<string, string[]>,
  entityIds: ReadonlySet<string>,
): EntityGraphPlacement[] {
  const entities = members.filter((node) => entityIds.has(node.node_id));
  const bridges = members.filter((node) => !entityIds.has(node.node_id));
  const placements: EntityGraphPlacement[] = [];

  if (entities.length === 0) {
    // No entity to anchor to. Draw the bridges on a plain ring so nothing is
    // dropped; a well-formed payload never reaches this branch.
    const largest = maxRadiusOf(bridges);
    const ring = bridges.length > 1
      ? Math.max(OVERVIEW_MIN_RING, (bridges.length * (2 * largest + NODE_PAD)) / TAU)
      : 0;
    bridges.forEach((node, index) => {
      const point = ringPoint((index * TAU) / bridges.length, ring);
      placements.push({
        node_id: node.node_id,
        x: point.x,
        y: point.y,
        radius: nodeRadius(node.degree),
      });
    });
    return placements;
  }

  const seated = overviewEntityOrder(entities, adjacency, entityIds);
  const entityMaxRadius = maxRadiusOf(entities);
  const circleRadius = seated.length > 1
    ? Math.max(
      OVERVIEW_MIN_RING,
      (seated.length * (2 * entityMaxRadius + NODE_PAD + OVERVIEW_LABEL_PAD)) / TAU,
    )
    : 0;

  const entityAt = new Map<string, OverviewDisc>();
  seated.forEach((node, index) => {
    const point = ringPoint((index * TAU) / seated.length, circleRadius);
    const radius = nodeRadius(node.degree);
    entityAt.set(node.node_id, { x: point.x, y: point.y, radius });
    placements.push({ node_id: node.node_id, x: point.x, y: point.y, radius });
  });

  const placedBridges: OverviewDisc[] = [];
  for (const bridge of bridges) {
    const radius = nodeRadius(bridge.degree);
    const endpoints = (adjacency.get(bridge.node_id) ?? [])
      .map((nodeId) => entityAt.get(nodeId))
      .filter((point): point is OverviewDisc => point !== undefined);

    let anchor: OverviewPoint = { x: 0, y: 0 };
    if (endpoints.length > 0) {
      let sumX = 0;
      let sumY = 0;
      for (const point of endpoints) {
        sumX += point.x;
        sumY += point.y;
      }
      anchor = { x: sumX / endpoints.length, y: sumY / endpoints.length };
    }

    /*
     * Find the anchor, or the nearest clear place to it.
     *
     * The anchor is where the bridge means something: on the chord between the
     * two hubs it joins. But for two neighbouring seats that chord runs close
     * to the rim, so the anchor can land on top of an endpoint, and two chords
     * of one circle can share a midpoint. So the anchor is tried first and
     * kept if it is clear, and only otherwise does the search widen - in rings
     * of a node's own width, preferring the direction of the circle's centre so
     * a displaced bridge moves *inside* the ring rather than out of the cluster.
     *
     * One obstacle set, entities and already-placed bridges together. Clearing
     * them in two passes was the bug this replaced: the second pass moved a
     * bridge off another bridge and straight back onto an entity, and the
     * measured live payload put three nodes 26px inside each other.
     */
    const obstacles = [...entityAt.values(), ...placedBridges];
    const resolved = clearPosition(anchor, radius, obstacles);

    placedBridges.push({ x: resolved.x, y: resolved.y, radius });
    placements.push({ node_id: bridge.node_id, x: resolved.x, y: resolved.y, radius });
  }

  return placements;
}

/** Wrap local placements in the box the shelf packer moves around. */
function overviewBox(
  placements: EntityGraphPlacement[],
  entityCount: number,
  leadNodeId: string,
): OverviewBox {
  const bounds = boundsOf(placements);
  return {
    placements,
    width: bounds.maxX - bounds.minX,
    height: bounds.maxY - bounds.minY,
    offsetX: -bounds.minX,
    offsetY: -bounds.minY,
    entityCount,
    nodeCount: placements.length,
    leadNodeId,
  };
}

/**
 * The isolated block: entities with no drawn connection, in a compact grid.
 *
 * They are returned by the endpoint because they are genuinely top hubs - an
 * entity can run forty events and share none of them with another top hub - so
 * dropping them would misreport the ranking. Drawing them among the clusters
 * would read as "connected to nothing in particular"; drawing them together
 * behind a wide gutter reads as the fact: no drawn connection.
 */
function layoutOverviewIsolated(
  entities: readonly CatalogEntityGraphNode[],
  aspect: number,
): EntityGraphPlacement[] {
  if (entities.length === 0) return [];
  const largest = maxRadiusOf(entities);
  const cell = 2 * largest + NODE_PAD + OVERVIEW_LABEL_PAD;
  const columns = Math.max(
    1,
    Math.min(entities.length, Math.round(Math.sqrt(entities.length * aspect))),
  );
  return entities.map((node, index) => ({
    node_id: node.node_id,
    x: (index % columns) * cell,
    y: Math.floor(index / columns) * cell,
    radius: nodeRadius(node.degree),
  }));
}

/**
 * Lay an overview payload out into world coordinates.
 *
 * Takes the same derived scene model the ego layout takes, but reads only the
 * parts that are meaningful without an ego: the node list and the edges. The
 * model's `ego`/`peers` split is deliberately ignored - `focus_id` is
 * `"overview"`, which names no node, so that split would arbitrarily promote
 * whichever entity the payload happened to list first.
 */
export function layoutEntityOverview(
  model: EntityGraphSceneModel,
  viewport: EntityGraphViewport,
): EntityGraphScene {
  const nodes = model.graph.nodes;
  if (nodes.length === 0) {
    return { graph: model.graph, placements: [], bounds: boundsOf([]) };
  }

  const ordered = [...nodes].sort(compareOverviewNodes);
  const rank = new Map<string, number>();
  ordered.forEach((node, index) => rank.set(node.node_id, index));
  const entityIds = new Set(
    ordered.filter((node) => node.node_kind === "entity").map((node) => node.node_id),
  );

  // Adjacency is rebuilt from the edges rather than reused from the model so a
  // dangling endpoint - an edge naming a node the payload did not include -
  // cannot pull a phantom into a component.
  const adjacency = new Map<string, string[]>();
  const seenPairs = new Set<string>();
  for (const edge of model.graph.edges) {
    if (!rank.has(edge.a) || !rank.has(edge.b) || edge.a === edge.b) continue;
    const key = edge.a < edge.b ? `${edge.a}|${edge.b}` : `${edge.b}|${edge.a}`;
    if (seenPairs.has(key)) continue;
    seenPairs.add(key);
    pushAdjacency(adjacency, edge.a, edge.b);
    pushAdjacency(adjacency, edge.b, edge.a);
  }
  for (const [key, value] of adjacency) {
    adjacency.set(key, value.sort((left, right) => (
      (rank.get(left) ?? Number.MAX_SAFE_INTEGER) - (rank.get(right) ?? Number.MAX_SAFE_INTEGER)
    )));
  }

  const isolated: CatalogEntityGraphNode[] = [];
  const boxes: OverviewBox[] = [];
  for (const members of overviewComponents(ordered, rank, adjacency)) {
    if (members.length === 1 && entityIds.has(members[0].node_id)) {
      isolated.push(members[0]);
      continue;
    }
    boxes.push(overviewBox(
      layoutOverviewCluster(members, adjacency, entityIds),
      members.filter((node) => entityIds.has(node.node_id)).length,
      members[0].node_id,
    ));
  }

  // Largest cluster first, so the packer opens with the figure a reader should
  // look at first and the ragged small ones fill in behind it.
  boxes.sort((left, right) => (
    right.entityCount - left.entityCount
    || right.nodeCount - left.nodeCount
    || compareText(left.leadNodeId, right.leadNodeId)
  ));

  const aspect = overviewAspect(viewport);
  let widest = 0;
  let area = 0;
  for (const box of boxes) {
    widest = Math.max(widest, box.width);
    area += (box.width + OVERVIEW_COMPONENT_GAP) * (box.height + OVERVIEW_COMPONENT_GAP);
  }
  const targetWidth = Math.max(widest, Math.sqrt(Math.max(area, 1) * aspect));

  const placements: EntityGraphPlacement[] = [];
  let cursorX = 0;
  let cursorY = 0;
  let rowHeight = 0;
  for (const box of boxes) {
    if (cursorX > 0 && cursorX + box.width > targetWidth) {
      cursorX = 0;
      cursorY += rowHeight + OVERVIEW_COMPONENT_GAP;
      rowHeight = 0;
    }
    const dx = cursorX + box.offsetX;
    const dy = cursorY + box.offsetY;
    for (const placement of box.placements) {
      placements.push({
        node_id: placement.node_id,
        x: placement.x + dx,
        y: placement.y + dy,
        radius: placement.radius,
      });
    }
    cursorX += box.width + OVERVIEW_COMPONENT_GAP;
    rowHeight = Math.max(rowHeight, box.height);
  }

  if (isolated.length > 0) {
    // No gutter when there is nothing above the block: an all-isolated graph is
    // the whole figure, not a footnote to an empty one.
    const top = boxes.length > 0 ? cursorY + rowHeight + OVERVIEW_ISOLATED_GAP : 0;
    for (const placement of layoutOverviewIsolated(isolated, aspect)) {
      placements.push({ ...placement, y: placement.y + top });
    }
  }

  /*
   * Centre the finished figure on the world origin.
   *
   * The canvas orders its roving tab stops by clock angle measured from the
   * origin and its fit camera centres on the bounding box; leaving the whole
   * layout in the positive quadrant would make every node read as roughly the
   * same angle and the tab walk arbitrary.
   */
  const raw = boundsOf(placements);
  const shiftX = -(raw.minX + raw.maxX) / 2;
  const shiftY = -(raw.minY + raw.maxY) / 2;
  const centred = placements.map((placement) => ({
    node_id: placement.node_id,
    x: placement.x + shiftX,
    y: placement.y + shiftY,
    radius: placement.radius,
  }));

  // Reading order is the canonical node order - entities by connectedness, then
  // bridges - so the DOM node layer's array order matches the ranking the
  // landing view claims to be showing.
  centred.sort((left, right) => (
    (rank.get(left.node_id) ?? Number.MAX_SAFE_INTEGER)
    - (rank.get(right.node_id) ?? Number.MAX_SAFE_INTEGER)
  ));

  return { graph: model.graph, placements: centred, bounds: boundsOf(centred) };
}

/** Derive and lay out in one call, for callers holding a raw overview payload. */
export function layoutCatalogEntityOverview(
  graph: CatalogEntityGraph | RawCatalogEntityGraph,
  viewport: EntityGraphViewport,
): EntityGraphScene {
  return layoutEntityOverview(deriveEntityGraphScene(graph), viewport);
}
