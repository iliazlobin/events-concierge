"use client";

import { Building2, Hash, LoaderCircle, UserRound, UsersRound } from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { EntityGraphCanvas } from "@/components/entity-graph-canvas";
import { EntityGraphView } from "@/components/entity-graph-view";
import { GraphEventInspector } from "@/components/graph-event-inspector";
import {
  ENTITY_SEARCH_DEBOUNCE_MS,
  EntityFilterChips,
  EntitySearchBox,
} from "@/components/entity-filter-controls";
import {
  getCatalogEntityDirectory,
  getCatalogEntityOverviewGraph,
} from "@/lib/entity-graph-api";
import type { CatalogEntityIdentityFilter } from "@/lib/entity-graph-api";
import type {
  CatalogEntityDirectory,
  CatalogEntityGraph,
  CatalogEntityGraphNode,
} from "@/lib/entity-graph";
import { deriveEntityGraphScene } from "@/lib/entity-graph";
import {
  readEntityDirectory,
  readEntityOverview,
  writeEntityDirectory,
  writeEntityOverview,
} from "@/lib/entity-graph-cache";
import { layoutEntityOverview } from "@/lib/entity-graph-layout";
import type { CatalogEntityKind, EventEntityReference } from "@/lib/types";

import "@/app/entity-graph.css";

/**
 * The Entities tab, which opens on a graph.
 *
 * There are exactly two states and no third: no entity selected draws the overview — the top hubs
 * and how they interconnect — and a selected entity draws that entity's ego graph. The
 * alphabetical wall that used to be the second half of this file is gone. It answered no question
 * a reader arrives with: the catalog holds thousands of entities, four fifths of them appear at a
 * single event, and a reader who already knows the name types it into the search box, which is
 * still here and now narrows the graph itself.
 *
 * The overview keeps the same bipartite spine as every other picture in this feature: a drawn
 * connection between two hubs is an event both are recorded at — `entity -> event -> entity` —
 * never a synthesised entity-to-entity edge, and never a name match. What the server samples is
 * *which* of a pair's shared events stands for the pair, and the view says so in words, in the
 * same register as the coverage line: a picture that quietly showed one event where a pair shares
 * eighty would be a claim about the catalog rather than a drawing of it.
 */

/**
 * The caps this view asks the endpoint for.
 *
 * Measured, not guessed. Across the live catalog the top 40 entities by event count are bridged by
 * 268 events, which is unreadable drawn in full — but those 268 events cover only 18 connected
 * pairs, so one representative event per pair draws the same connectivity in 58 nodes. The pair
 * cap is set above the measured 18 so that a filtered view, whose top 40 are a different 40, is
 * not silently truncated at the number that happened to fit the unfiltered one.
 */
const OVERVIEW_LIMITS = { entities: 40, pairs: 40 } as const;

/**
 * The second read, and the reason it is not a second read per keystroke.
 *
 * `totals` and `coverage` in the directory payload are computed over `catalog_entities` and
 * `catalog_entity_event_mentions` with no query, kind or city predicate applied — they are
 * whole-catalog figures by construction, not figures about the rows the filters selected. So this
 * view asks for them once, unfiltered, and the answer is cached for the life of the page: pressing
 * a chip or typing in the box re-reads the graph and nothing else, and the chip counts and the
 * coverage line stay the fixed denominators they claim to be.
 *
 * The alternative — deriving the counts from the 40 drawn hubs — was rejected. A chip reading "23"
 * that changes every time you press a different chip is not a count of anything a reader can name.
 */
const FACTS_LIMIT = 1;
const FACTS_MIN_EVENTS = 1;

const DEFAULT_VIEWPORT = { width: 960, height: 640 };

interface EntitiesViewProps {
  tenantId: string | null;
  canRefresh?: boolean;
  selectedEntityId: string | null;
  onSelectEntity: (entityId: string | null) => void;
  onEntitySelect: (reference: EventEntityReference) => void;
  onTopicSelect: (topic: string) => void;
}

function count(value: number): string {
  return value.toLocaleString();
}

function nodeGlyph(node: CatalogEntityGraphNode): ReactNode {
  if (node.node_kind === "topic") return <Hash aria-hidden="true" />;
  if (node.entity_kind === "organization") return <Building2 aria-hidden="true" />;
  if (node.entity_kind === "person") return <UserRound aria-hidden="true" />;
  return <UsersRound aria-hidden="true" />;
}

export function EntitiesView({
  tenantId,
  canRefresh = false,
  selectedEntityId,
  onSelectEntity,
  onEntitySelect,
  onTopicSelect,
}: EntitiesViewProps) {
  if (selectedEntityId) {
    return (
      <EntityGraphView
        tenantId={tenantId}
        canRefresh={canRefresh}
        entityId={selectedEntityId}
        onSelectEntity={onSelectEntity}
        onEntitySelect={onEntitySelect}
        onTopicSelect={onTopicSelect}
      />
    );
  }

  return <EntityOverviewGraph tenantId={tenantId} onSelectEntity={onSelectEntity}
    onEntitySelect={onEntitySelect} onTopicSelect={onTopicSelect} />;
}

interface EntityOverviewGraphProps {
  tenantId: string | null;
  onSelectEntity: (entityId: string) => void;
  onEntitySelect: (reference: EventEntityReference) => void;
  onTopicSelect: (topic: string) => void;
}

function EntityOverviewGraph({ tenantId, onSelectEntity, onEntitySelect, onTopicSelect }: EntityOverviewGraphProps) {
  const observerRef = useRef<ResizeObserver | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [query, setQuery] = useState("");
  const [kinds, setKinds] = useState<CatalogEntityKind[]>([]);
  const [identity, setIdentity] = useState<CatalogEntityIdentityFilter>("all");
  const [graph, setGraph] = useState<CatalogEntityGraph | null>(null);
  const [facts, setFacts] = useState<CatalogEntityDirectory | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewport, setViewport] = useState(DEFAULT_VIEWPORT);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");

  /**
   * Measure the drawing column, not the page.
   *
   * A callback ref rather than a mount-time effect, for the reason the ego view gives: the column
   * does not exist until the first payload arrives, so a mount-time effect would find `null`,
   * return, and pin every layout to `DEFAULT_VIEWPORT` for the life of the view.
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
  useEffect(() => () => abortRef.current?.abort(), []);

  /** The unfiltered denominators, fetched once and then served from cache. */
  useEffect(() => {
    const request = {
      tenantId,
      query: "",
      kinds: [] as CatalogEntityKind[],
      cities: [] as string[],
      limit: FACTS_LIMIT,
      minEvents: FACTS_MIN_EVENTS,
    };
    const cached = readEntityDirectory(request);
    if (cached) {
      setFacts(cached);
      return;
    }
    let cancelled = false;
    void getCatalogEntityDirectory(tenantId, "", [], [], FACTS_LIMIT, FACTS_MIN_EVENTS)
      .then((payload) => {
        writeEntityDirectory(request, payload);
        if (!cancelled) setFacts(payload);
      })
      // A failed denominator is not a failed view: the graph still draws, the chips simply render
      // without their counts and the coverage line stays off rather than guessing at one.
      .catch(() => {
        if (!cancelled) setFacts(null);
      });
    return () => {
      cancelled = true;
    };
  }, [tenantId]);

  /** One request per filter state, always. A new filter cancels the one before it. */
  useEffect(() => {
    setSelectedNodeId(null);
    setHoveredNodeId(null);
    const request = { tenantId, query, kinds, identity, ...OVERVIEW_LIMITS };
    const cached = readEntityOverview(request);
    if (cached) {
      setGraph(cached);
      setError(null);
      setLoading(false);
      return;
    }
    const timer = window.setTimeout(() => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setLoading(true);
      setError(null);
      void getCatalogEntityOverviewGraph(
        tenantId,
        query,
        kinds,
        identity,
        OVERVIEW_LIMITS,
        controller.signal,
      )
        .then((payload) => {
          if (controller.signal.aborted) return;
          writeEntityOverview(request, payload);
          setGraph(payload);
        })
        .catch((caught: unknown) => {
          if (controller.signal.aborted) return;
          setGraph(null);
          setError(caught instanceof Error ? caught.message : "The entity graph could not be loaded.");
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, query ? ENTITY_SEARCH_DEBOUNCE_MS : 0);
    return () => {
      window.clearTimeout(timer);
      /*
       * Abort here, not only at the head of the next timer.
       *
       * Cancelling the timer alone leaves an already-issued request live for the whole debounce
       * window. If it resolves in that window its `aborted` check is still false, so it writes its
       * payload — and a filter the reader has already moved off can repaint over the one they
       * chose, whenever the older request happens to be the slower one.
       */
      abortRef.current?.abort();
    };
  }, [identity, kinds, query, tenantId]);

  /**
   * Derived once, laid out once, from the same payload.
   *
   * `deriveEntityGraphScene` is reused unchanged: the overview payload is the ego payload's shape,
   * and `layoutEntityOverview` reads only the parts of the model that mean anything without an ego.
   */
  const model = useMemo(() => (graph ? deriveEntityGraphScene(graph) : null), [graph]);
  const scene = useMemo(
    () => (model ? layoutEntityOverview(model, viewport) : null),
    [model, viewport],
  );

  const drawn = useMemo(() => {
    const nodes = graph?.nodes ?? [];
    const entities = nodes.filter((node) => node.node_kind === "entity");
    const bridges = nodes.filter((node) => node.node_kind !== "entity");
    const connected = new Set<string>();
    for (const edge of graph?.edges ?? []) {
      connected.add(edge.a);
      connected.add(edge.b);
    }
    /*
     * Connections are counted as hub PAIRS sharing a drawn event, not as drawn event nodes.
     * One event naming three hubs is three connections, and the pair is the thing the reader is
     * being told about — "who is linked to whom" — so it is the thing that gets counted.
     */
    const pairs = new Set<string>();
    for (const bridge of bridges) {
      const ends = (graph?.edges ?? [])
        .filter((edge) => edge.a === bridge.node_id || edge.b === bridge.node_id)
        .map((edge) => (edge.a === bridge.node_id ? edge.b : edge.a))
        .sort();
      for (let i = 0; i < ends.length; i += 1) {
        for (let j = i + 1; j < ends.length; j += 1) pairs.add(`${ends[i]}|${ends[j]}`);
      }
    }
    return {
      entities: entities.length,
      bridges: bridges.length,
      connections: pairs.size,
      isolated: entities.filter((node) => !connected.has(node.node_id)).length,
    };
  }, [graph]);

  useEffect(() => {
    if (!graph) return;
    setAnnouncement(
      `${drawn.entities} ${drawn.entities === 1 ? "hub" : "hubs"} drawn, `
      + `${drawn.connections} ${drawn.connections === 1 ? "connection" : "connections"} `
      + `through ${drawn.bridges} shared ${drawn.bridges === 1 ? "event" : "events"}, `
      + `${drawn.isolated} with no drawn connection.`,
    );
  }, [drawn, graph]);

  const focusNodeId = useCallback((nodeId: string) => {
    const node = model?.byId.get(nodeId);
    if (node?.node_kind === "entity" && node.entity_id) onSelectEntity(node.entity_id);
  }, [model, onSelectEntity]);

  const selectedNode = selectedNodeId ? model?.byId.get(selectedNodeId) ?? null : null;

  const coverage = facts?.coverage ?? null;
  const coveragePercent = coverage && coverage.events_total > 0
    ? ((coverage.events_with_entities / coverage.events_total) * 100).toFixed(1)
    : null;

  const kindCounts = facts
    ? {
      organization: facts.totals.organization_count,
      person: facts.totals.person_count,
      unknown: facts.totals.unknown_count,
    }
    : null;
  const identityCounts = facts
    ? {
      all: facts.totals.entity_count,
      profile_verified: facts.totals.verified_count,
      source_scoped: facts.totals.scoped_count,
    }
    : null;

  return (
    <section className="workspace entities-view">
      <header className="workspace-heading entities-heading">
        <div>
          <p>PEOPLE &amp; ORGANIZATIONS</p>
          <h1>Entity explorer</h1>
        </div>
      </header>

      <div className="entity-overview">
        <EntitySearchBox
          query={query}
          onQueryChange={setQuery}
          loading={loading}
          placeholder="Search organizers, hosts, speakers, companies…"
        />

        {error ? <p className="workspace-error" role="alert">{error}</p> : null}

        <EntityFilterChips
          kinds={kinds}
          onKindsChange={setKinds}
          kindCounts={kindCounts}
          identity={identity}
          onIdentityChange={setIdentity}
          identityCounts={identityCounts}
          identityScope="In the whole catalog"
          label="Narrow the graph"
        />

        {/*
          * The counts a reader needs at a glance, and the two disclosures they need once.
          *
          * Both disclosures are load-bearing — the graph is drawn from 5% of the catalog, and each
          * line stands for a pair's whole shared history rather than a single meeting — so neither
          * can be dropped. But three stacked paragraphs of prose ahead of the picture is a wall,
          * and a wall is read once and then skipped forever, which is the same as not being there.
          * Folded away they are one click from anyone who wants them and silent for everyone else.
          */}
        {graph ? (
          <div className="entity-overview-meta">
            <p className="entity-directory-matched">
              {count(drawn.entities)} {drawn.entities === 1 ? "hub" : "hubs"} drawn
              {graph.truncated.peers && graph.counts.peers_total > drawn.entities
                ? ` of ${count(graph.counts.peers_total)} matching`
                : ""}
              {" · "}
              {/*
                A connection is a PAIR of hubs, and a drawn event node can carry several of them —
                three hubs at one event is three connections through one node. Reporting the node
                count as the connection count therefore under-reports the graph: 18 connections were
                drawn through 13 events in the unfiltered catalog. Both numbers are said, because
                only one of them answers "how many hubs are linked" and only the other answers
                "how many events am I looking at".
              */}
              {count(drawn.connections)} {drawn.connections === 1 ? "connection" : "connections"}
              {graph.truncated.events && graph.counts.events_total > drawn.connections
                ? ` of ${count(graph.counts.events_total)}`
                : ""}
              {drawn.bridges
                ? ` through ${count(drawn.bridges)} shared ${drawn.bridges === 1 ? "event" : "events"}`
                : ""}
              {drawn.isolated ? ` · ${count(drawn.isolated)} unconnected` : ""}
            </p>
            <details className="entity-overview-about">
              <summary>How this is drawn</summary>
              <div>
                {coverage && coveragePercent ? (
                  <p>
                    {count(coverage.events_with_entities)} of {count(coverage.events_total)}{" "}
                    catalog events ({coveragePercent}%) name an organizer, host, speaker or
                    partner. This graph is drawn from those events only.
                  </p>
                ) : null}
                <p>
                  Every line is a recorded mention, never an inferred relationship: two hubs are
                  joined through an event both are named at. Each connection shows{" "}
                  <strong>one representative event</strong> — the most recent the pair shares —
                  standing for every event they share. Hubs sharing no event with another hub are
                  still ranked here, grouped at the bottom with no line drawn.
                </p>
              </div>
            </details>
          </div>
        ) : null}

        <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

        {loading && !scene ? (
          <div className="entity-loading"><LoaderCircle className="spin" />Loading entity graph</div>
        ) : null}

        {scene && scene.placements.length > 0 ? (
          <div className="entity-graph-stage" data-selection={selectedNode?.node_kind === "event" ? "event" : "none"}>
            <div className="entity-graph-column" ref={measureColumn}>
              <EntityGraphCanvas
                scene={scene}
                selectedNodeId={selectedNodeId}
                hoveredNodeId={hoveredNodeId}
                onSelect={setSelectedNodeId}
                onFocus={focusNodeId}
              />
            </div>
            {selectedNode?.node_kind === "event" && model ? (
              <GraphEventInspector
                key={`${tenantId ?? "anonymous"}:${selectedNode.canonical_event_id ?? selectedNode.node_id}`}
                tenantId={tenantId}
                subject={selectedNode}
                model={model}
                contextNote={selectedNode.shared_event_count !== null
                  ? `Representative of ${count(selectedNode.shared_event_count)} shared ${selectedNode.shared_event_count === 1 ? "event" : "events"}.`
                  : undefined}
                onSelectNode={setSelectedNodeId}
                onHoverNode={setHoveredNodeId}
                onFocusEntity={onSelectEntity}
                onEntitySelect={onEntitySelect}
                onTopicSelect={onTopicSelect}
              />
            ) : null}
          </div>
        ) : null}

        {scene && scene.placements.length === 0 && !loading ? (
          <div className="empty-state">
            <span />
            <h2>No entities match</h2>
            <p>Try a broader name, or clear a filter.</p>
          </div>
        ) : null}

        {selectedNode && selectedNode.node_kind !== "event" ? (
          <OverviewSelection node={selectedNode} onOpen={onSelectEntity} />
        ) : !selectedNode ? (
          <p className="entity-overview-hint">
            Click a node to read it. Double-click a hub to open its own graph; double-click an event
            to bring it to the centre.
          </p>
        ) : null}

        <ul className="entity-graph-legend">
          <li data-kind="person">Person</li>
          <li data-kind="organization">Organization</li>
          <li data-kind="unknown">Unclassified</li>
          <li data-kind="event">Shared event</li>
          <li className="entity-graph-legend__note">
            Bigger means more connections. A solid ring means we hold a direct profile URL — not
            that we verified the person.
          </li>
        </ul>
      </div>
    </section>
  );
}

/**
 * Entity and topic summaries use the graph payload; event selections use the shared event card.
 */
function OverviewSelection({
  node,
  onOpen,
}: {
  node: CatalogEntityGraphNode;
  onOpen: (entityId: string) => void;
}) {
  const facts: string[] = [];
  if (node.node_kind === "entity") {
    facts.push(`${count(node.degree)} ${node.degree === 1 ? "event" : "events"}`);
    facts.push(
      node.identity_status === "profile_verified"
        ? "Direct profile URL on file"
        : "Scoped to the source that named it",
    );
    if ((node.roles ?? []).length) {
      facts.push((node.roles ?? []).map((role) => (
        role.replace(/^./, (value) => value.toUpperCase())
      )).join(" · "));
    }
  } else {
    facts.push(`${count(node.degree)} of the drawn events`);
  }

  return (
    <div className="entity-overview-selection">
      <span className="entity-overview-selection__glyph" data-kind={
        node.node_kind === "entity" ? node.entity_kind ?? "unknown" : node.node_kind
      }>
        {nodeGlyph(node)}
      </span>
      <span className="entity-overview-selection__copy">
        <strong>{node.label}</strong>
        <span>{facts.join(" · ")}</span>
      </span>
      {node.node_kind === "entity" && node.entity_id ? (
        <button
          type="button"
          className="entity-overview-selection__open"
          onClick={() => onOpen(node.entity_id as string)}
        >
          Open this graph
        </button>
      ) : null}
    </div>
  );
}
