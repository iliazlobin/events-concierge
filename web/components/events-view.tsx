"use client";

import { ArrowDownUp } from "lucide-react";

import { EventList } from "@/components/event-list";
import type { CatalogSort, EventEntityReference, EventItem } from "@/lib/types";

interface EventsViewProps {
  events: EventItem[];
  sort: CatalogSort;
  sourceName?: string;
  historicalWindow?: boolean;
  loading: boolean;
  error: string | null;
  expandedId: string | null;
  nextCursor: string | null;
  loadingMore: boolean;
  onExpandedChange: (eventId: string | null) => void;
  onSortChange: (sort: CatalogSort) => void;
  onSourceSelect: (sourceKey: string) => void;
  onLoadMore: () => void;
  onFacetSelect: (value: string) => void;
  onEntitySelect: (reference: EventEntityReference) => void;
  onTopicSelect: (topic: string) => void;
}

export function EventsView({
  events,
  sort,
  sourceName,
  historicalWindow = false,
  loading,
  error,
  expandedId,
  nextCursor,
  loadingMore,
  onExpandedChange,
  onSortChange,
  onSourceSelect,
  onLoadMore,
  onFacetSelect,
  onEntitySelect,
  onTopicSelect,
}: EventsViewProps) {
  const heading = sourceName
    ? `${sourceName} events`
    : historicalWindow
      ? "Events"
      : "Upcoming events";

  return (
    <section className="workspace events-view">
      <header className="workspace-heading">
        <div>
          <p>DISCOVER</p>
          <h1>{heading}</h1>
        </div>
        <label className="event-sort-control">
          <ArrowDownUp aria-hidden="true" />
          <span className="sr-only">Sort events</span>
          <select
            aria-label="Sort events"
            value={sort}
            onChange={(event) => onSortChange(event.target.value as CatalogSort)}
          >
            <option value="soonest">Soonest first</option>
            <option value="latest">Latest first</option>
          </select>
        </label>
      </header>

      {error ? <p className="workspace-error" role="alert">{error}</p> : null}
      {loading && !events.length ? (
        <div className="event-skeletons" aria-label="Loading events">
          <span />
          <span />
          <span />
        </div>
      ) : (
        <EventList
          events={events}
          expandedId={expandedId}
          onExpandedChange={onExpandedChange}
          hasMore={Boolean(nextCursor)}
          loadingMore={loadingMore}
          onLoadMore={onLoadMore}
          onSourceSelect={onSourceSelect}
          onFacetSelect={onFacetSelect}
          onEntitySelect={onEntitySelect}
          onTopicSelect={onTopicSelect}
          emptyTitle={historicalWindow ? "No retained events match" : "No events match"}
          emptyCopy={historicalWindow
            ? "Try another date or broader filters. Older provider listings may have rolled off before archival capture."
            : "Try another date, remove a provider, or search more broadly."}
        />
      )}
    </section>
  );
}
