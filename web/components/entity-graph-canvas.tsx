"use client";

import {
  Building2,
  CalendarDays,
  Hash,
  UserRound,
  UsersRound,
} from "lucide-react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  CatalogEntityGraphEdge,
  CatalogEntityGraphNode,
  EntityGraphPlacement,
  EntityGraphScene,
} from "@/lib/entity-graph";
import { chooseRestingLabels, screenRadius } from "@/lib/entity-graph-labels";
import { NODE_MAX_RADIUS } from "@/lib/entity-graph-layout";

/**
 * Edges on one `<canvas>`, nodes as real DOM `<button>`s.
 *
 * The component owns no fetching and no data: the parent hands it a finished scene plus callbacks,
 * exactly the way `MapView` takes `events` plus callbacks.  Nothing here is transformed by CSS —
 * the canvas stays fixed in the viewport and every edge endpoint is projected into screen space per
 * frame, so zoom never clips an edge and never upscales the raster.  Nodes carry a fixed pixel size
 * independent of the zoom factor, which is what a ring layout actually wants: zooming spreads the
 * rings apart without inflating labels, and hit areas stay pixel-exact because the browser owns
 * hit testing.  There is no quadtree and no hand-written hit test in this file.
 */

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2.5;
const FIT_PADDING = NODE_MAX_RADIUS + 26;
// Leave room for the zoom rail, including its larger touch targets and bottom margin.
const FIT_CONTROL_CLEARANCE = 80;
const REFOCUS_DURATION_MS = 320;
/**
 * The zoom a re-centre pulls up to, when the reader is further out than this.
 *
 * Double-clicking a node that is a speck in a packed overview should end with
 * that node readable, not merely with the speck in the middle. It never zooms
 * *out*: a reader who has zoomed in to read a cluster asked for that, and this
 * gesture is "bring this here", not "reset the camera" — which is what Fit is.
 */
const RECENTRE_MIN_ZOOM = 1;

/** The palette the canvas cannot read through `var()`; resolved once at mount. */
const EDGE_TOKENS = {
  host: "--edge-host",
  organizer: "--edge-organizer",
  speaker: "--edge-speaker",
  partner: "--edge-partner",
  topic: "--edge-topic",
  dim: "--edge-dim",
} as const;

const EDGE_FALLBACKS: Record<EdgeBucket, string> = {
  host: "rgba(122, 162, 255, 0.55)",
  organizer: "rgba(201, 255, 104, 0.55)",
  speaker: "rgba(255, 134, 200, 0.55)",
  partner: "rgba(246, 196, 83, 0.55)",
  topic: "rgba(187, 156, 255, 0.3)",
  dim: "rgba(255, 255, 255, 0.12)",
};

type EdgeBucket = keyof typeof EDGE_TOKENS;

const EDGE_BUCKETS: EdgeBucket[] = [
  "dim",
  "topic",
  "partner",
  "speaker",
  "organizer",
  "host",
];

interface Camera {
  tx: number;
  ty: number;
  k: number;
}

export interface EntityGraphCanvasProps {
  scene: EntityGraphScene;
  /** The node the inspector is reading, owned by the parent. */
  selectedNodeId: string | null;
  /** An externally driven highlight — an inspector row under the pointer, say. */
  hoveredNodeId: string | null;
  /** Inspect a node without changing the ego. */
  onSelect: (nodeId: string) => void;
  /** Make this node the new ego.  Only entity nodes can answer this. */
  onFocus: (nodeId: string) => void;
}

/*
 * Node size is read off `placement.radius` and computed nowhere else in this file.
 *
 * The layout reserves the gap between two ring neighbours from the same number, and its no-overlap
 * proof holds only while the drawn radius equals the reserved one.  A second curve here — even one
 * calibrated to sit below the first today — makes that proof depend on arithmetic coincidence, and
 * one constant edit in either file would silently produce overlapping nodes with the suite still
 * green.  A degree-clamped curve here also flattened every event node, whose degree is its entity
 * count and reaches 35, into the same maximum size.
 */

function roleLabel(role: string): string {
  return role.replace(/^./, (value) => value.toUpperCase());
}

function edgeBucket(edge: CatalogEntityGraphEdge): EdgeBucket {
  if (edge.kind === "topic") return "topic";
  const role = (edge.roles ?? [])[0];
  if (role === "host" || role === "organizer" || role === "speaker" || role === "partner") {
    return role;
  }
  return "dim";
}

function nodeGlyph(node: CatalogEntityGraphNode): ReactNode {
  if (node.node_kind === "event") return <CalendarDays aria-hidden="true" />;
  if (node.node_kind === "topic") return <Hash aria-hidden="true" />;
  if (node.entity_kind === "organization") return <Building2 aria-hidden="true" />;
  if (node.entity_kind === "person") return <UserRound aria-hidden="true" />;
  return <UsersRound aria-hidden="true" />;
}

function nodeKindAttribute(node: CatalogEntityGraphNode): string {
  if (node.node_kind === "event") return "event";
  if (node.node_kind === "topic") return "topic";
  return node.entity_kind ?? "unknown";
}

function shortDate(value: string | null): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(parsed);
}

/**
 * The spoken form of a node.  Everything a sighted reader gets from size, ring and colour is said
 * here in words, because size, ring and colour are the three things a screen reader cannot see.
 */
function nodeAriaLabel(node: CatalogEntityGraphNode): string {
  const parts: string[] = [node.label];
  if (node.node_kind === "entity") {
    parts.push(node.entity_kind ?? "unknown kind");
    parts.push(
      node.identity_status === "profile_verified"
        ? "direct profile on file"
        : "scoped to the source that named it",
    );
    parts.push(`${node.degree} ${node.degree === 1 ? "event" : "events"}`);
    if (node.shared_event_count !== null) {
      parts.push(`${node.shared_event_count} shared with the focus`);
    }
    const roles = node.roles ?? [];
    if (roles.length) parts.push(roles.map(roleLabel).join(", "));
    parts.push("Enter to inspect, Shift plus Enter to focus");
  } else if (node.node_kind === "event") {
    parts.push("event");
    const when = shortDate(node.start_at);
    if (when) parts.push(node.is_past ? `past, ${when}` : `upcoming, ${when}`);
    const egoRoles = node.ego_roles ?? [];
    if (egoRoles.length) parts.push(`focus attended as ${egoRoles.map(roleLabel).join(", ")}`);
    parts.push(`${node.degree} ${node.degree === 1 ? "entity" : "entities"} named`);
    parts.push("Enter to inspect");
  } else {
    parts.push("topic");
    parts.push(`${node.degree} of the shown events`);
    parts.push("Enter to inspect");
  }
  return parts.join(", ");
}

/** Clockwise from 12 o'clock, in [0, 2π) — the order the reader's eye and the tab key both take. */
function clockAngle(placement: EntityGraphPlacement): number {
  const angle = Math.atan2(placement.x, -placement.y);
  return angle < 0 ? angle + Math.PI * 2 : angle;
}

function fitCamera(
  bounds: EntityGraphScene["bounds"],
  width: number,
  height: number,
): Camera {
  const spanX = Math.max(bounds.maxX - bounds.minX, 1) + FIT_PADDING * 2;
  const spanY = Math.max(bounds.maxY - bounds.minY, 1) + FIT_PADDING * 2;
  const availableHeight = Math.max(1, height - FIT_CONTROL_CLEARANCE);
  const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.min(width / spanX, availableHeight / spanY)));
  const centreX = (bounds.minX + bounds.maxX) / 2;
  const centreY = (bounds.minY + bounds.maxY) / 2;
  return { k, tx: width / 2 - centreX * k, ty: availableHeight / 2 - centreY * k };
}

function EntityGraphCanvasImpl({
  scene,
  selectedNodeId,
  hoveredNodeId,
  onSelect,
  onFocus,
}: EntityGraphCanvasProps) {
  const frameRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const nodeElements = useRef(new Map<string, HTMLButtonElement>());
  const cameraRef = useRef<Camera>({ tx: 0, ty: 0, k: 1 });
  const sizeRef = useRef({ width: 0, height: 0 });
  const pendingFrameRef = useRef<number | null>(null);
  const tweenFrameRef = useRef<number | null>(null);
  const edgeColorsRef = useRef<Record<EdgeBucket, string>>({ ...EDGE_FALLBACKS });
  const pointerHoverRef = useRef<string | null>(null);
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(false);
  const [zoomLabel, setZoomLabel] = useState(100);

  /**
   * Mirror refs, the `map-view.tsx` idiom: the imperative draw reads selection and hover without
   * the draw effect taking them as dependencies, so changing either never rebuilds the scene.
   */
  const selectedIdRef = useRef<string | null>(selectedNodeId);
  selectedIdRef.current = selectedNodeId;
  const hoveredIdRef = useRef<string | null>(hoveredNodeId);
  hoveredIdRef.current = hoveredNodeId;
  const reducedMotionRef = useRef(prefersReducedMotion);
  reducedMotionRef.current = prefersReducedMotion;

  const nodesById = useMemo(() => {
    const index = new Map<string, CatalogEntityGraphNode>();
    for (const node of scene.graph.nodes) index.set(node.node_id, node);
    return index;
  }, [scene]);

  /*
   * The node that is already the focus, or null when there is not one.
   *
   * `focus_id` is a node id on an ego payload and the literal string "overview" on the landing
   * payload, where it names no node at all. Guarding "cannot focus what is already focused"
   * against this rather than against `ring === 0` is what lets the same canvas serve both: on the
   * landing graph every entity sits at ring 0, so a ring test would have made every hub on it
   * unfocusable.
   */
  const egoNodeId = nodesById.has(scene.graph.focus_id) ? scene.graph.focus_id : null;

  const placementsById = useMemo(() => {
    const index = new Map<string, EntityGraphPlacement>();
    for (const placement of scene.placements) index.set(placement.node_id, placement);
    return index;
  }, [scene]);

  /** Tab order: ego, then events clockwise from 12, then peers, then topics. */
  const ordered = useMemo(() => {
    const rows = scene.placements
      .map((placement) => ({ placement, node: nodesById.get(placement.node_id) }))
      .filter((row): row is { placement: EntityGraphPlacement; node: CatalogEntityGraphNode } => (
        row.node !== undefined
      ));
    rows.sort((left, right) => (
      left.node.ring - right.node.ring
      || clockAngle(left.placement) - clockAngle(right.placement)
      || left.node.node_id.localeCompare(right.node.node_id)
    ));
    return rows;
  }, [nodesById, scene]);

  const edgeSegments = useMemo(() => {
    const segments: Array<{
      bucket: EdgeBucket;
      ax: number;
      ay: number;
      bx: number;
      by: number;
      a: string;
      b: string;
    }> = [];
    for (const edge of scene.graph.edges) {
      const from = placementsById.get(edge.a);
      const to = placementsById.get(edge.b);
      if (!from || !to) continue;
      segments.push({
        bucket: edgeBucket(edge),
        ax: from.x,
        ay: from.y,
        bx: to.x,
        by: to.y,
        a: edge.a,
        b: edge.b,
      });
    }
    return segments;
  }, [placementsById, scene]);

  /**
   * Place every node, size it to the camera, and decide which labels are legible where they land.
   *
   * All three belong in one pass because all three are functions of the same camera. Label
   * legibility especially: it is not a property of the graph, it is a property of how far out the
   * reader is standing, so it has to be re-decided on every zoom rather than settled once from a
   * node count. It is written imperatively for the same reason `data-pointer` is — a hover or a
   * wheel tick must not cost a React render of forty nodes.
   */
  const applyNodeTransforms = useCallback(() => {
    const camera = cameraRef.current;

    /* Pass one: place and size every node, and record where the discs landed. */
    const placed: {
      element: HTMLElement;
      label: string;
      cx: number;
      cy: number;
      radius: number;
    }[] = [];
    for (const { placement, node } of ordered) {
      const element = nodeElements.current.get(placement.node_id);
      if (!element) continue;
      const radius = screenRadius(placement.radius, camera.k);
      const cx = placement.x * camera.k + camera.tx;
      const cy = placement.y * camera.k + camera.ty;
      const size = `${Math.round(radius * 2)}px`;
      if (element.style.width !== size) {
        element.style.width = size;
        element.style.height = size;
      }
      element.style.transform =
        `translate3d(${Math.round(cx - radius)}px, ${Math.round(cy - radius)}px, 0)`;
      placed.push({ element, label: node.label, cx, cy, radius });
    }

    /*
     * Pass two: keep a label only where it is legible where it landed.
     *
     * Greedy, in the order `ordered` already fixes — inner rings first, then clockwise — so the
     * names that survive are stable as the camera moves and never depend on paint order. A label
     * has to clear two different things: every label already kept, and every node's disc. The
     * second is why this needs a pass of its own; a name that reads cleanly against its
     * neighbours' names can still be printed straight across the next row's circles, which is
     * the more obviously broken of the two.
     */
    const legible = chooseRestingLabels(placed);
    placed.forEach((entry, index) => {
      if (legible[index]) entry.element.dataset.label = "rest";
      else delete entry.element.dataset.label;
    });
  }, [ordered]);

  const paint = useCallback(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;
    const { width, height } = sizeRef.current;
    if (width <= 0 || height <= 0) return;
    const camera = cameraRef.current;
    const colors = edgeColorsRef.current;
    const active = hoveredIdRef.current ?? pointerHoverRef.current ?? selectedIdRef.current;

    context.clearRect(0, 0, width, height);
    context.lineCap = "round";

    // Five strokeStyle assignments per frame regardless of edge count: bucket first, stroke once.
    const batched = new Map<EdgeBucket, typeof edgeSegments>();
    for (const segment of edgeSegments) {
      const bucket = active && segment.a !== active && segment.b !== active
        ? "dim"
        : segment.bucket;
      const existing = batched.get(bucket);
      if (existing) existing.push(segment);
      else batched.set(bucket, [segment]);
    }

    for (const bucket of EDGE_BUCKETS) {
      const segments = batched.get(bucket);
      if (!segments?.length) continue;
      context.strokeStyle = colors[bucket];
      context.lineWidth = bucket === "dim" ? 1 : 1.6;
      if (bucket === "topic") context.setLineDash([4, 5]);
      else context.setLineDash([]);
      context.beginPath();
      for (const segment of segments) {
        context.moveTo(segment.ax * camera.k + camera.tx, segment.ay * camera.k + camera.ty);
        context.lineTo(segment.bx * camera.k + camera.tx, segment.by * camera.k + camera.ty);
      }
      context.stroke();
    }
    context.setLineDash([]);
    applyNodeTransforms();
  }, [applyNodeTransforms, edgeSegments]);

  /** One paint per frame at most; two triggers inside one frame coalesce into one. */
  const schedulePaint = useCallback(() => {
    if (pendingFrameRef.current !== null) return;
    pendingFrameRef.current = window.requestAnimationFrame(() => {
      pendingFrameRef.current = null;
      paint();
    });
  }, [paint]);

  const setCamera = useCallback((next: Camera) => {
    cameraRef.current = next;
    setZoomLabel(Math.round(next.k * 100));
    schedulePaint();
  }, [schedulePaint]);

  /** The one moving thing in this component, and it cancels itself the frame it lands. */
  const easeCamera = useCallback((target: Camera) => {
    if (tweenFrameRef.current !== null) {
      window.cancelAnimationFrame(tweenFrameRef.current);
      tweenFrameRef.current = null;
    }
    if (reducedMotionRef.current) {
      setCamera(target);
      return;
    }
    const from = { ...cameraRef.current };
    const started = performance.now();
    const step = () => {
      const elapsed = performance.now() - started;
      const progress = Math.min(1, elapsed / REFOCUS_DURATION_MS);
      const eased = 1 - (1 - progress) ** 3;
      cameraRef.current = {
        tx: from.tx + (target.tx - from.tx) * eased,
        ty: from.ty + (target.ty - from.ty) * eased,
        k: from.k + (target.k - from.k) * eased,
      };
      paint();
      if (progress < 1) {
        tweenFrameRef.current = window.requestAnimationFrame(step);
      } else {
        tweenFrameRef.current = null;
        setZoomLabel(Math.round(target.k * 100));
      }
    };
    tweenFrameRef.current = window.requestAnimationFrame(step);
  }, [paint, setCamera]);

  /**
   * Pan (and, if the reader is zoomed well out, zoom) so one node sits in the middle.
   *
   * This moves the same `cameraRef` every other gesture moves and touches nothing else: no fetch,
   * no selection change, no new scene. `Fit` therefore still works afterwards — it recomputes from
   * `scene.bounds`, which this cannot alter — and so do the wheel, the drag and the zoom buttons.
   * Motion goes through `easeCamera`, which jumps instead of animating when the reader has asked
   * for reduced motion.
   */
  const centreOnNode = useCallback((nodeId: string) => {
    const placement = placementsById.get(nodeId);
    const { width, height } = sizeRef.current;
    if (!placement || width <= 0 || height <= 0) return;
    const k = Math.min(MAX_ZOOM, Math.max(cameraRef.current.k, RECENTRE_MIN_ZOOM));
    easeCamera({
      k,
      tx: width / 2 - placement.x * k,
      ty: height / 2 - placement.y * k,
    });
  }, [easeCamera, placementsById]);

  const fitToScene = useCallback((animate: boolean) => {
    const { width, height } = sizeRef.current;
    if (width <= 0 || height <= 0) return;
    const target = fitCamera(scene.bounds, width, height);
    if (animate) easeCamera(target);
    else setCamera(target);
  }, [easeCamera, scene, setCamera]);

  const zoomBy = useCallback((factor: number, originX?: number, originY?: number) => {
    const { width, height } = sizeRef.current;
    const camera = cameraRef.current;
    const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, camera.k * factor));
    if (k === camera.k) return;
    const px = originX ?? width / 2;
    const py = originY ?? height / 2;
    setCamera({
      k,
      tx: px - ((px - camera.tx) / camera.k) * k,
      ty: py - ((py - camera.ty) / camera.k) * k,
    });
  }, [setCamera]);

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const syncPreference = () => setPrefersReducedMotion(preference.matches);
    syncPreference();
    preference.addEventListener("change", syncPreference);
    return () => preference.removeEventListener("change", syncPreference);
  }, []);

  useEffect(() => {
    const styles = getComputedStyle(document.documentElement);
    const resolved = { ...EDGE_FALLBACKS };
    for (const bucket of EDGE_BUCKETS) {
      const value = styles.getPropertyValue(EDGE_TOKENS[bucket]).trim();
      if (value) resolved[bucket] = value;
    }
    edgeColorsRef.current = resolved;
    schedulePaint();
  }, [schedulePaint]);

  /** Backing store in device pixels, drawing in CSS pixels. */
  useEffect(() => {
    const frame = frameRef.current;
    const canvas = canvasRef.current;
    if (!frame || !canvas) return;
    const syncSize = () => {
      const rect = frame.getBoundingClientRect();
      const width = Math.max(1, Math.round(rect.width));
      const height = Math.max(1, Math.round(rect.height));
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const first = sizeRef.current.width === 0;
      sizeRef.current = { width, height };
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      const context = canvas.getContext("2d");
      context?.setTransform(ratio, 0, 0, ratio, 0, 0);
      if (first) setCamera(fitCamera(scene.bounds, width, height));
      else schedulePaint();
      watchRatio();
    };
    /*
     * `ResizeObserver` cannot see a resolution change.  Dragging the window from a 2x display to a
     * 1x one leaves the frame's CSS box identical, so the observer stays silent while the backing
     * store keeps the old ratio and every edge renders at half resolution against crisp DOM nodes.
     * The media query is re-armed from inside `syncSize` so it always tracks the current ratio —
     * the same idiom as the `prefers-reduced-motion` effect above.
     */
    let ratioQuery: MediaQueryList | null = null;
    const onRatioChange = () => syncSize();
    const watchRatio = () => {
      ratioQuery?.removeEventListener("change", onRatioChange);
      ratioQuery = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
      ratioQuery.addEventListener("change", onRatioChange);
    };

    // `syncSize` arms the resolution watcher itself, so one call establishes both.
    syncSize();
    if (typeof ResizeObserver === "undefined") {
      return () => ratioQuery?.removeEventListener("change", onRatioChange);
    }
    const observer = new ResizeObserver(syncSize);
    observer.observe(frame);
    return () => {
      observer.disconnect();
      ratioQuery?.removeEventListener("change", onRatioChange);
    };
  }, [scene, schedulePaint, setCamera]);

  /** A new scene is a refocus: re-fit, easing unless the reader asked for stillness. */
  useEffect(() => {
    fitToScene(true);
  }, [fitToScene]);

  useEffect(() => {
    schedulePaint();
  }, [hoveredNodeId, schedulePaint, selectedNodeId]);

  useEffect(() => () => {
    if (pendingFrameRef.current !== null) window.cancelAnimationFrame(pendingFrameRef.current);
    if (tweenFrameRef.current !== null) window.cancelAnimationFrame(tweenFrameRef.current);
    pendingFrameRef.current = null;
    tweenFrameRef.current = null;
  }, []);

  /** Pan on the background, zoom about the cursor, pinch through the same two handlers. */
  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    const pointers = new Map<number, { x: number; y: number }>();
    let pinchDistance = 0;

    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = frame.getBoundingClientRect();
      zoomBy(
        Math.exp(-event.deltaY * 0.0016),
        event.clientX - rect.left,
        event.clientY - rect.top,
      );
    };
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Element && event.target.closest(".entity-graph-node")) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      frame.setPointerCapture(event.pointerId);
      if (pointers.size === 2) {
        const [first, second] = [...pointers.values()];
        pinchDistance = Math.hypot(first.x - second.x, first.y - second.y);
      }
    };
    const onPointerMove = (event: PointerEvent) => {
      const previous = pointers.get(event.pointerId);
      if (!previous) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 2) {
        const [first, second] = [...pointers.values()];
        const distance = Math.hypot(first.x - second.x, first.y - second.y);
        if (pinchDistance > 0 && distance > 0) {
          const rect = frame.getBoundingClientRect();
          zoomBy(
            distance / pinchDistance,
            (first.x + second.x) / 2 - rect.left,
            (first.y + second.y) / 2 - rect.top,
          );
        }
        pinchDistance = distance;
        return;
      }
      const camera = cameraRef.current;
      setCamera({
        k: camera.k,
        tx: camera.tx + (event.clientX - previous.x),
        ty: camera.ty + (event.clientY - previous.y),
      });
    };
    const onPointerUp = (event: PointerEvent) => {
      pointers.delete(event.pointerId);
      pinchDistance = 0;
      if (frame.hasPointerCapture(event.pointerId)) frame.releasePointerCapture(event.pointerId);
    };

    frame.addEventListener("wheel", onWheel, { passive: false });
    frame.addEventListener("pointerdown", onPointerDown);
    frame.addEventListener("pointermove", onPointerMove);
    frame.addEventListener("pointerup", onPointerUp);
    frame.addEventListener("pointercancel", onPointerUp);
    return () => {
      frame.removeEventListener("wheel", onWheel);
      frame.removeEventListener("pointerdown", onPointerDown);
      frame.removeEventListener("pointermove", onPointerMove);
      frame.removeEventListener("pointerup", onPointerUp);
      frame.removeEventListener("pointercancel", onPointerUp);
    };
  }, [setCamera, zoomBy]);

  /**
   * A roving tabindex: the graph is one tab stop, not 79.
   *
   * Up to 79 nodes render here, and the component already implements a composite-widget keyboard
   * model on the wrapper — Left/Right step within a ring, Up/Down cross rings — which is what the
   * `role="application"` and the aria-labels advertise. Leaving every button in the tab order
   * would make a reader press Tab up to 79 times to reach the inspector and would duplicate that
   * model with a second, differently ordered one.
   */
  const [tabStopNodeId, setTabStopNodeId] = useState<string | null>(null);
  const rovingNodeId = tabStopNodeId && nodesById.has(tabStopNodeId)
    ? tabStopNodeId
    : ordered[0]?.placement.node_id ?? null;

  const focusNode = useCallback((nodeId: string) => {
    setTabStopNodeId(nodeId);
    nodeElements.current.get(nodeId)?.focus();
  }, []);

  /**
   * Left/Right step angularly inside a ring; Up/Down cross rings to the angularly nearest node.
   * Ring order is the reading order, so the keyboard walk and the eye walk agree.
   */
  const stepFrom = useCallback((nodeId: string, key: string): string | null => {
    const index = ordered.findIndex((row) => row.placement.node_id === nodeId);
    if (index < 0) return null;
    const current = ordered[index];
    if (key === "ArrowRight" || key === "ArrowLeft") {
      const ring = ordered.filter((row) => row.node.ring === current.node.ring);
      if (ring.length < 2) return null;
      const position = ring.findIndex((row) => row.placement.node_id === nodeId);
      const next = (position + (key === "ArrowRight" ? 1 : -1) + ring.length) % ring.length;
      return ring[next].placement.node_id;
    }
    const direction = key === "ArrowDown" ? 1 : -1;
    const rings = [...new Set(ordered.map((row) => row.node.ring))].sort((a, b) => a - b);
    const ringPosition = rings.indexOf(current.node.ring);
    const targetRing = rings[ringPosition + direction];
    if (targetRing === undefined) return null;
    const candidates = ordered.filter((row) => row.node.ring === targetRing);
    if (!candidates.length) return null;
    const from = clockAngle(current.placement);
    let best = candidates[0];
    let bestDelta = Number.POSITIVE_INFINITY;
    for (const candidate of candidates) {
      const raw = Math.abs(clockAngle(candidate.placement) - from);
      const delta = Math.min(raw, Math.PI * 2 - raw);
      if (delta < bestDelta) {
        bestDelta = delta;
        best = candidate;
      }
    }
    return best.placement.node_id;
  }, [ordered]);

  const handleKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const target = event.target instanceof Element
      ? event.target.closest<HTMLElement>(".entity-graph-node")
      : null;
    const nodeId = target?.dataset.nodeId ?? null;
    if (
      event.key === "ArrowUp" || event.key === "ArrowDown"
      || event.key === "ArrowLeft" || event.key === "ArrowRight"
    ) {
      const next = nodeId ? stepFrom(nodeId, event.key) : ordered[0]?.placement.node_id ?? null;
      if (!next) return;
      event.preventDefault();
      focusNode(next);
      return;
    }
    if (event.key === "0") {
      event.preventDefault();
      fitToScene(false);
      return;
    }
    if (event.key === "f" || event.key === "F") {
      event.preventDefault();
      fitToScene(true);
      return;
    }
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomBy(1.2);
      return;
    }
    if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomBy(1 / 1.2);
    }
  }, [fitToScene, focusNode, ordered, stepFrom, zoomBy]);

  const registerNode = useCallback((nodeId: string) => (element: HTMLButtonElement | null) => {
    if (element) nodeElements.current.set(nodeId, element);
    else nodeElements.current.delete(nodeId);
  }, []);

  /**
   * Hover repaints the canvas and rewrites one dataset attribute; it re-renders nothing.
   *
   * That attribute is `data-pointer`, which React never writes, and deliberately not `data-state`.
   * `data-state` is rendered declaratively from `selectedNodeId`/`hoveredNodeId`, so writing it
   * here gave one attribute two owners: any re-render while the pointer rested on a node — a
   * wheel-zoom is enough, since `setCamera` sets the zoom label — reconciled it back to "idle",
   * the tint vanished, and `pointerleave` then found a value it did not recognise and skipped its
   * own reset, leaving the ref and the DOM disagreeing until the next hover.
   */
  const setPointerHover = useCallback((nodeId: string | null) => {
    const previous = pointerHoverRef.current;
    if (previous === nodeId) return;
    if (previous) {
      const element = nodeElements.current.get(previous);
      if (element) delete element.dataset.pointer;
    }
    pointerHoverRef.current = nodeId;
    if (nodeId && selectedIdRef.current !== nodeId) {
      const element = nodeElements.current.get(nodeId);
      if (element) element.dataset.pointer = "hovered";
    }
    schedulePaint();
  }, [schedulePaint]);

  const activeHighlight = hoveredNodeId ?? selectedNodeId;

  return (
    <div
      className="entity-graph-frame"
      ref={frameRef}
      role="application"
      aria-roledescription="entity graph"
      aria-label={egoNodeId
        ? `Entity graph around ${nodesById.get(egoNodeId)?.label ?? "the selected entity"}`
        : "Entity graph of the most connected people and organizations"}
      tabIndex={-1}
      onKeyDown={handleKeyDown}
    >
      <canvas className="entity-graph-canvas" ref={canvasRef} aria-hidden="true" />
      {/*
       * Which labels rest visible is decided in `applyNodeTransforms`, from where the nodes
       * actually land on screen — see the greedy pass there. It used to be decided here, from a
       * ring-1 node count, which is a proxy for crowding rather than a measurement of it: it could
       * hide a dozen perfectly legible labels on a sparse ring and still let a packed overview
       * smear its names together. Every node keeps its accessible name; hover or keyboard focus
       * also reveals the visual label.
       */}
      <div className="entity-graph-nodes">
        {ordered.map(({ placement, node }) => {
          const radius = placement.radius;
          const state = node.node_id === selectedNodeId
            ? "selected"
            : node.node_id === activeHighlight
              ? "hovered"
              : "idle";
          return (
            <button
              key={node.node_id}
              ref={registerNode(node.node_id)}
              type="button"
              className="entity-graph-node"
              tabIndex={node.node_id === rovingNodeId ? 0 : -1}
              onFocus={() => setTabStopNodeId(node.node_id)}
              data-node-id={node.node_id}
              data-kind={nodeKindAttribute(node)}
              data-ring={node.ring}
              data-ego={node.node_id === egoNodeId ? "true" : undefined}
              data-state={state}
              data-identity={node.node_kind === "entity" ? node.identity_status ?? "unknown" : undefined}
              data-past={node.node_kind === "event" && node.is_past ? "true" : undefined}
              style={{
                height: `${Math.round(screenRadius(radius, cameraRef.current.k) * 2)}px`,
                width: `${Math.round(screenRadius(radius, cameraRef.current.k) * 2)}px`,
                // Seeded from the live camera so a freshly mounted node paints in place instead of
                // flashing at the frame's top-left corner for one frame.
                transform: `translate3d(${Math.round(placement.x * cameraRef.current.k + cameraRef.current.tx - screenRadius(radius, cameraRef.current.k))}px, ${Math.round(placement.y * cameraRef.current.k + cameraRef.current.ty - screenRadius(radius, cameraRef.current.k))}px, 0)`,
              }}
              aria-label={nodeAriaLabel(node)}
              aria-pressed={node.node_id === selectedNodeId}
              onPointerEnter={() => setPointerHover(node.node_id)}
              onPointerLeave={() => setPointerHover(null)}
              onClick={(event) => {
                event.stopPropagation();
                if (event.shiftKey && node.node_kind === "entity" && node.node_id !== egoNodeId) {
                  onFocus(node.node_id);
                  return;
                }
                onSelect(node.node_id);
              }}
              onKeyDown={(event) => {
                if (event.key !== "Enter") return;
                if (!event.shiftKey) return;
                /*
                 * Shift+Enter is the keyboard form of the double-click, and it has to reach both
                 * halves of that gesture or the re-centre is a mouse-only feature. Entities answer
                 * with a new graph; an event or topic answers with the camera, exactly as the
                 * pointer path does.
                 */
                if (node.node_kind === "entity") {
                  if (node.node_id === egoNodeId) return;
                  event.preventDefault();
                  onFocus(node.node_id);
                  return;
                }
                event.preventDefault();
                centreOnNode(node.node_id);
              }}
              /*
               * Two different gestures, because the two node kinds answer to two different
               * questions. Double-clicking an entity asks "show me this one instead", which is a
               * new graph. Double-clicking an event or a topic asks "bring that over here", which
               * is the camera and nothing else — no refetch, no selection change, and the frame it
               * lands on is the frame that was already drawn.
               */
              onDoubleClick={(event) => {
                if (node.node_kind === "entity") {
                  if (node.node_id === egoNodeId) return;
                  event.stopPropagation();
                  onFocus(node.node_id);
                  return;
                }
                event.stopPropagation();
                centreOnNode(node.node_id);
              }}
            >
              <span className="entity-graph-node__glyph">{nodeGlyph(node)}</span>
              <span className="entity-graph-node__label" aria-hidden="true">{node.label}</span>
            </button>
          );
        })}
      </div>
      <div className="entity-graph-zoom" role="group" aria-label="Graph zoom">
        <button type="button" onClick={() => zoomBy(1 / 1.2)} aria-label="Zoom out">−</button>
        <span>{zoomLabel}%</span>
        <button type="button" onClick={() => zoomBy(1.2)} aria-label="Zoom in">+</button>
        <button type="button" onClick={() => fitToScene(!prefersReducedMotion)}>Fit</button>
      </div>
    </div>
  );
}

export const EntityGraphCanvas = memo(EntityGraphCanvasImpl);
