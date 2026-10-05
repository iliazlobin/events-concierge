"use client";

import { ArrowUpRight, LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { GraphEventCard } from "@/components/graph-event-card";
import { ApiError } from "@/lib/api";
import { formatEventDate, formatEventTime } from "@/lib/date";
import type { CatalogEntityGraphEdge, CatalogEntityGraphNode, EntityGraphSceneModel } from "@/lib/entity-graph";
import { getCatalogGraphEvent } from "@/lib/entity-graph-api";
import { readGraphEvent, writeGraphEvent } from "@/lib/entity-graph-cache";
import { roleLabel } from "@/lib/entity-inspector-model";
import { formatCity } from "@/lib/presentation";
import type { EventItem } from "@/lib/types";

interface GraphEventInspectorProps {
  tenantId: string | null;
  subject: CatalogEntityGraphNode;
  model: EntityGraphSceneModel;
  eventSessions?: CatalogEntityGraphNode[];
  contextNote?: string;
  onSelectNode: (nodeId: string | null) => void;
  onHoverNode: (nodeId: string | null) => void;
  onFocusEntity: (entityId: string) => void;
}

function edgeProvenance(edge: CatalogEntityGraphEdge): string {
  const observed = edge.observed_at ? new Date(edge.observed_at) : null;
  return [
    edge.source_labels.length ? `asserted by ${edge.source_labels.join(", ")}` : "",
    observed && !Number.isNaN(observed.getTime())
      ? `seen ${new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(observed)}`
      : "",
  ].filter(Boolean).join(" · ");
}

function publicEventUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

/** Mounted only for the selected occurrence; the graph itself stays a bounded projection. */
export function GraphEventInspector({
  tenantId, subject, model, eventSessions, contextNote, onSelectNode, onHoverNode, onFocusEntity,
}: GraphEventInspectorProps) {
  const eventId = subject.canonical_event_id;
  const [event, setEvent] = useState<EventItem | null>(() => (
    eventId ? readGraphEvent(tenantId, eventId) : null
  ));
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!eventId) {
      setError("Full details are unavailable for this event.");
      return;
    }
    const cached = readGraphEvent(tenantId, eventId);
    if (cached) {
      setEvent(cached);
      return;
    }
    const controller = new AbortController();
    setError(null);
    void getCatalogGraphEvent(tenantId, eventId, controller.signal).then((selected) => {
      if (controller.signal.aborted) return;
      if (selected.canonical_event_id !== eventId) throw new Error("Event identity mismatch");
      writeGraphEvent(tenantId, selected);
      setEvent(selected);
    }).catch((caught: unknown) => {
      if (controller.signal.aborted) return;
      setError(caught instanceof ApiError && caught.status === 404
        ? "This event is no longer available in the published catalog."
        : "Event details could not be loaded.");
    });
    return () => controller.abort();
  }, [tenantId, eventId, attempt]);

  const named = model.edges.filter((edge) => edge.kind === "mention" && edge.b === subject.node_id);
  const provenances = new Set(named.map(edgeProvenance));
  const sharedProvenance = named.length > 1 && provenances.size === 1
    ? [...provenances][0] || null
    : null;
  const connections = named.length ? (
    <details className="graph-event-connections">
      <summary>Explore connections ({named.length})</summary>
      {sharedProvenance ? <p className="entity-graph-inspector__provenance">{sharedProvenance}</p> : null}
      <ul className="entity-graph-inspector__mentions">
        {named.map((edge) => {
          const entity = model.byId.get(edge.a);
          const provenance = sharedProvenance ? "" : edgeProvenance(edge);
          return (
            <li key={`${edge.a} ${edge.b}`}>
              <button type="button"
                onMouseEnter={() => onHoverNode(edge.a)}
                onMouseLeave={() => onHoverNode(null)}
                onClick={() => entity?.entity_id ? onFocusEntity(entity.entity_id) : onSelectNode(edge.a)}>
                <strong>{entity?.label ?? edge.a}</strong>
                <small>{[edge.roles.map(roleLabel).join(" · ") || "Named", provenance].filter(Boolean).join(" · ")}</small>
              </button>
            </li>
          );
        })}
      </ul>
    </details>
  ) : undefined;
  const date = subject.start_at ? formatEventDate(subject.start_at) : null;
  const fallbackUrl = publicEventUrl(subject.registration_url);

  return (
    <GraphEventCard event={event} onClose={() => onSelectNode(null)}
      closeLabel={`Back to ${model.byId.get(model.focusId)?.label ?? "the graph"}`} loading={!event && !error}
      connections={connections} beforeDetails={<>
      {contextNote ? <p className="graph-event-context">{contextNote}</p> : null}
      {eventSessions && eventSessions.length > 1 ? (
        <section className="graph-event-dates" aria-label="Event dates">
          <h3>{eventSessions.length} dates in this graph</h3>
          <ul className="entity-graph-inspector__mentions">
            {eventSessions.map((session) => (
              <li key={session.node_id}>
                <button type="button" aria-pressed={session.node_id === subject.node_id} onClick={() => onSelectNode(session.node_id)}>
                  <strong>{session.start_at ? new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", weekday: "short" }).format(new Date(session.start_at)) : "Date unknown"}</strong>
                  <small>{session.start_at ? formatEventTime(session.start_at, session.end_at) : ""}{session.is_past ? " · Past event" : ""}{session.node_id === subject.node_id ? " · Selected" : ""}</small>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      </>}>
        <div className="graph-event-fallback">
          <h2>{subject.label}</h2>
          <p>{[
            date ? `${date.weekday} ${date.month} ${date.day}` : null,
            subject.start_at ? formatEventTime(subject.start_at, subject.end_at) : null,
            [subject.venue_name, formatCity(subject.city)].filter(Boolean).join(" · "),
          ].filter(Boolean).join(" — ")}</p>
          {error ? <div role="alert">
            <p>{error}</p>
            {eventId ? <button type="button" className="entity-graph-inspector__retry" onClick={() => setAttempt((value) => value + 1)}>Retry event details</button> : null}
          </div> : <p role="status"><LoaderCircle className="is-spinning" aria-hidden="true" /> Loading event details…</p>}
          {connections}
          {fallbackUrl ? <a className="event-action" href={fallbackUrl} target="_blank" rel="noopener noreferrer">View event <ArrowUpRight aria-hidden="true" /></a> : null}
        </div>
    </GraphEventCard>
  );
}
