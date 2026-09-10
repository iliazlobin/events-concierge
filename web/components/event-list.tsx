"use client";

import { LoaderCircle } from "lucide-react";

import { groupEventSessions } from "@/lib/event-discovery";
import { EventCard } from "@/components/event-card";
import type { EventEntityReference, EventItem } from "@/lib/types";

interface EventListProps {
  events: EventItem[];
  groupSessions?: boolean;
  expandedId: string | null;
  onExpandedChange: (eventId: string | null) => void;
  emptyTitle?: string;
  emptyCopy?: string;
  compact?: boolean;
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  onSourceSelect?: (sourceKey: string) => void;
  onFacetSelect?: (value: string) => void;
  onPriceSelect?: (price: "free" | "paid" | "unknown") => void;
  onEntitySelect?: (reference: EventEntityReference) => void;
  onTopicSelect?: (topic: string) => void;
}

export function EventList({
  events,
  groupSessions = false,
  expandedId,
  onExpandedChange,
  emptyTitle = "Nothing here yet",
  emptyCopy = "Try a broader date range or clear a filter.",
  compact = false,
  hasMore = false,
  loadingMore = false,
  onLoadMore,
  onSourceSelect,
  onFacetSelect,
  onPriceSelect,
  onEntitySelect,
  onTopicSelect,
}: EventListProps) {
  if (!events.length) {
    return (
      <div className="empty-state">
        <span aria-hidden="true" />
        <h2>{emptyTitle}</h2>
        <p>{emptyCopy}</p>
      </div>
    );
  }

  return (
    <>
      <div className="event-list">
        {(groupSessions ? groupEventSessions(events) : events.map((event) => [event])).map((group) => {
          const renderEvent = (event: EventItem) => (
          <EventCard
            key={event.canonical_event_id}
            event={event}
            compact={compact}
            expanded={expandedId === event.canonical_event_id}
            onSourceSelect={onSourceSelect}
            onFacetSelect={onFacetSelect}
            onPriceSelect={onPriceSelect}
            onEntitySelect={onEntitySelect}
            onTopicSelect={onTopicSelect}
            onToggle={() => onExpandedChange(
              expandedId === event.canonical_event_id
                ? null
                : event.canonical_event_id,
            )}
          />
          );
          return <div key={group[0].canonical_event_id}>
            {renderEvent(group[0])}
            {group.length > 1 ? <details open={group.slice(1).some((event) => event.canonical_event_id === expandedId) || undefined}>
              <summary>{group.length - 1} more dates in these results</summary>
              {group.slice(1).map(renderEvent)}
            </details> : null}
          </div>;
        })}
      </div>
      {hasMore && onLoadMore ? (
        <button
          className="load-more"
          type="button"
          disabled={loadingMore}
          onClick={onLoadMore}
        >
          {loadingMore ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
          {loadingMore ? "Loading" : "More events"}
        </button>
      ) : null}
    </>
  );
}
