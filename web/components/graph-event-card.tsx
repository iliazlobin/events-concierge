"use client";

import { X } from "lucide-react";
import type { ReactNode } from "react";

import { EventCard } from "@/components/event-card";
import type { EventEntityReference, EventItem } from "@/lib/types";

interface GraphEventCardProps {
  event: EventItem | null;
  onClose: () => void;
  closeLabel?: string;
  loading?: boolean;
  beforeDetails?: ReactNode;
  connections?: ReactNode;
  children?: ReactNode;
  onEntitySelect?: (reference: EventEntityReference) => void;
  onTopicSelect?: (topic: string) => void;
}

/** The same expanded event facts as Map, with a close control for graph selection. */
export function GraphEventCard({
  event, onClose, closeLabel = "Back to the graph", loading = false,
  beforeDetails, connections, children, onEntitySelect, onTopicSelect,
}: GraphEventCardProps) {
  return (
    <aside className="entity-graph-inspector entity-graph-inspector--event" aria-label="Event detail" aria-busy={loading}>
      <header className="graph-event-toolbar">
        <span>Event</span>
        <button type="button" className="entity-graph-inspector__close" onClick={onClose}>
          <X aria-hidden="true" /><span className="sr-only">{closeLabel}</span>
        </button>
      </header>
      {beforeDetails}
      {event ? (
        <EventCard event={event} expanded compact entityDetails={connections}
          onEntitySelect={onEntitySelect} onTopicSelect={onTopicSelect} />
      ) : children}
    </aside>
  );
}
