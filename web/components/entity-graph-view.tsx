"use client";

import { LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { GraphWorkspace } from "@/components/graph-workspace";
import { GraphWorkspaceHeading } from "@/components/graph-workspace-heading";
import { getCatalogEntityGraph } from "@/lib/entity-graph-api";
import type { CatalogEntityGraph } from "@/lib/entity-graph";
import { aggregateGraphSessions } from "@/lib/entity-graph-sessions";
import { deriveEntityGraphScene } from "@/lib/entity-graph";
import { readableGraphError } from "@/lib/entity-graph-errors";
import { readEntityGraph, writeEntityGraph } from "@/lib/entity-graph-cache";
import { layoutEgoRings } from "@/lib/entity-graph-layout";
import type { EventEntityReference } from "@/lib/types";

import "@/app/entity-graph.css";

/** Fetch one bounded graph per focus; drawing and selected-event reads stay separate. */

/**
 * The caps this view asks for, well inside the capability's own 24 / 48 / 6.
 *
 * The same object is passed to `getCatalogEntityGraph` and folded into the cache key, so the key
 * can never describe a request the fetch did not make. Raising any of these changes both at once.
 */
const GRAPH_LIMITS = { events: 18, peers: 32, topics: 4 } as const;

const DEFAULT_VIEWPORT = { width: 960, height: 640 };

export interface EntityGraphViewProps {
  tenantId: string | null;
  canRefresh?: boolean;
  entityId: string;
  /** `null` returns to the graph overview. */
  onSelectEntity: (entityId: string | null) => void;
  onEntitySelect: (reference: EventEntityReference) => void;
  onTopicSelect: (topic: string) => void;
}

export function EntityGraphView({
  tenantId,
  canRefresh = false,
  entityId,
  onSelectEntity,
  onEntitySelect,
  onTopicSelect,
}: EntityGraphViewProps) {
  const observerRef = useRef<ResizeObserver | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [graph, setGraph] = useState<CatalogEntityGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewport, setViewport] = useState(DEFAULT_VIEWPORT);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  /** Retry the current focus without leaving its graph. */
  const [reloadToken, setReloadToken] = useState(0);

  /**
   * Measure the drawing column, not the stage.
   *
   * The stage is a grid holding the canvas *and* the 320–400 px inspector; laying out against it
   * tells `layoutEgoRings` it has hundreds of pixels it does not have, so the rings spread past the
   * frame and the canvas answers by zooming out — a smaller figure with unused margin. This is a
   * callback ref rather than a mount-time effect because the column does not exist until the
   * first graph arrives: a mount-time effect would find `null`, return, and never run again, which
   * would pin every layout to `DEFAULT_VIEWPORT` for the life of the view.
   */
  const measureColumn = useCallback((node: HTMLDivElement | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!node) return;
    const sync = () => {
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const width = Math.round(rect.width);
      const height = Math.round(rect.height);
      // A sub-pixel wobble is not a new layout question.  Below this threshold the scene object is
      // left identical, which is also what keeps `EntityGraphCanvas`'s memo from re-rendering 79
      // buttons on every frame of a window drag.
      setViewport((current) => (
        Math.abs(current.width - width) < 8 && Math.abs(current.height - height) < 8
          ? current
          : { width, height }
      ));
    };
    sync();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(sync);
    observer.observe(node);
    observerRef.current = observer;
  }, []);

  useEffect(() => () => observerRef.current?.disconnect(), []);

  /** One request per hop, always.  A new focus cancels the one before it. */
  useEffect(() => {
    setSelectedNodeId(null);
    setHoveredNodeId(null);
    const request = { tenantId, entityId, ...GRAPH_LIMITS };
    const cached = readEntityGraph(request);
    if (cached) {
      setGraph(cached);
      setError(null);
      setLoading(false);
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    void getCatalogEntityGraph(tenantId, entityId, GRAPH_LIMITS, controller.signal)
      .then((payload) => {
        if (controller.signal.aborted) return;
        writeEntityGraph(request, payload);
        setGraph(payload);
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;
        setGraph(null);
        setError(readableGraphError(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
    };
  }, [entityId, tenantId, reloadToken]);

  useEffect(() => () => abortRef.current?.abort(), []);

  /**
   * Keep original occurrence evidence for the inspector. Only the canvas folds
   * matching sessions into one visual node; selecting a date resolves its original node ID.
   * Resizing changes layout without changing membership.
   */
  const model = useMemo(() => (graph ? deriveEntityGraphScene(graph) : null), [graph]);
  const sessions = useMemo(() => graph ? aggregateGraphSessions(graph) : null, [graph]);
  const canvasModel = useMemo(() => sessions ? deriveEntityGraphScene(sessions.graph) : null, [sessions]);
  const scene = useMemo(
    () => (canvasModel ? layoutEgoRings(canvasModel, viewport) : null),
    [canvasModel, viewport],
  );

  const ego = model?.ego ?? null;

  useEffect(() => {
    if (!ego || !graph) return;
    setAnnouncement(
      `Focused on ${ego.label}. ${graph.counts.events} of ${graph.counts.events_total} events, `
      + `${graph.counts.peers} of ${graph.counts.peers_total} connected entities.`,
    );
  }, [ego, graph]);

  const focusEntity = useCallback((nextEntityId: string) => {
    if (nextEntityId === entityId) {
      setSelectedNodeId(null);
      return;
    }
    onSelectEntity(nextEntityId);
  }, [entityId, onSelectEntity]);

  const focusNodeId = useCallback((nodeId: string) => {
    const node = model?.byId.get(nodeId);
    if (node?.node_kind === "entity" && node.entity_id) focusEntity(node.entity_id);
    else setSelectedNodeId(nodeId);
  }, [focusEntity, model]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      if (
        target instanceof Element
        && target.closest("input, textarea, select, [contenteditable]")
      ) return;
      if (event.key === "Escape") {
        // A strict ladder, so one press never does two things.
        if (selectedNodeId) setSelectedNodeId(null);
        else onSelectEntity(null);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onSelectEntity, selectedNodeId]);

  return (
    <div className="entity-graph-shell">
      <GraphWorkspaceHeading
        current={ego?.label ?? (error ? "Entity unavailable" : "Loading entity")}
        onRoot={() => onSelectEntity(null)}
        summary={graph ? (
            <>
              {graph.counts.events} {graph.counts.events === 1 ? "event" : "events"}
              {graph.truncated.events ? ` of ${graph.counts.events_total}` : ""}
              {" · "}
              {graph.counts.peers} connected
              {graph.truncated.peers ? ` of ${graph.counts.peers_total}` : ""}
              {graph.counts.topics ? ` · ${graph.counts.topics} topics` : ""}
            </>
          ) : null}
      />

      {error ? (
        <div className="entity-graph-failure" role="alert">
          <p>{error}</p>
          <button type="button" onClick={() => setReloadToken((token) => token + 1)}>
            Try again
          </button>
        </div>
      ) : null}
      {loading && !scene ? (
        <div className="entity-loading"><LoaderCircle className="spin" />Loading entity graph</div>
      ) : null}

      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

      {scene && model ? (
        <GraphWorkspace scene={scene} model={model} tenantId={tenantId} canRefresh={canRefresh}
          selectedNodeId={selectedNodeId} hoveredNodeId={hoveredNodeId}
          canvasSelectedNodeId={selectedNodeId ? sessions?.representative.get(selectedNodeId) ?? selectedNodeId : null}
          canvasHoveredNodeId={hoveredNodeId ? sessions?.representative.get(hoveredNodeId) ?? hoveredNodeId : null}
          eventSessions={sessions?.groups.get(sessions.representative.get(selectedNodeId ?? "") ?? "")}
          measureColumn={measureColumn} onSelectNode={setSelectedNodeId} onHoverNode={setHoveredNodeId}
          onFocusNode={focusNodeId} onFocusEntity={focusEntity}
          onEntitySelect={onEntitySelect} onTopicSelect={onTopicSelect} />
      ) : null}

      {sessions && [...sessions.groups.values()].some((group) => group.length > 1) ? (
        <p className="entity-graph-inspector__provenance">Matching sessions share a graph node. Select one to browse its dates in the detail panel. Counts refer to individual events in the loaded graph.</p>
      ) : null}

      {scene ? (
        <ul className="entity-graph-legend">
          <li data-kind="person">Person</li>
          <li data-kind="organization">Organization</li>
          <li data-kind="unknown">Unclassified</li>
          <li data-kind="event">Event</li>
          <li data-kind="topic">Topic</li>
          <li className="entity-graph-legend__note">
            Bigger means more connections. A solid ring means we hold a direct profile URL — not
            that we verified the person.
          </li>
        </ul>
      ) : null}
    </div>
  );
}
