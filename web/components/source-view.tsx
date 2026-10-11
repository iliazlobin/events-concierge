"use client";

import { ArrowLeft, ArrowUpRight, CalendarDays, LoaderCircle, MapPin } from "lucide-react";

import { EventList } from "@/components/event-list";
import { sourceEventSummary, sourceTitle, sourceWebsite } from "@/lib/source-view";
import type { CatalogProvider, EventEntityReference, EventItem } from "@/lib/types";

import styles from "./source-view.module.css";

export interface SourceViewProps {
  source: CatalogProvider | null;
  sourceKey: string;
  events: EventItem[];
  expandedId: string | null;
  onExpandedChange: (eventId: string | null) => void;
  loading: boolean;
  error: string | null;
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  onBack: () => void;
  onViewEvents?: () => void;
  onMap?: () => void;
  onCalendar?: () => void;
  onRetry?: () => void;
  onEntitySelect?: (reference: EventEntityReference) => void;
  onTopicSelect?: (topic: string) => void;
}

export function SourceView({
  source, sourceKey, events, expandedId, onExpandedChange, loading, error,
  hasMore = false, loadingMore = false, onLoadMore, onBack, onViewEvents,
  onMap, onCalendar, onRetry, onEntitySelect, onTopicSelect,
}: SourceViewProps) {
  const title = sourceTitle(source, sourceKey, events);
  const website = sourceWebsite(source);
  const summary = sourceEventSummary(events);
  const publisher = source?.publisher.trim();
  const showPublisher = publisher && publisher.toLocaleLowerCase() !== title.toLocaleLowerCase();
  const count = source && Number.isInteger(source.event_count) && source.event_count >= 0
    ? source.event_count : null;

  return (
    <section className={styles.view} aria-label={`${title} source`}>
      <button type="button" className={styles.back} onClick={onBack}>
        <ArrowLeft aria-hidden="true" />Back to events
      </button>
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>Source</p>
          <h1 className={styles.title}>{title}</h1>
          {showPublisher ? <p className={styles.publisher}>By {publisher}</p> : null}
        </div>
        {website ? <a className={styles.website} href={website} target="_blank" rel="noopener noreferrer">
          Visit website<ArrowUpRight aria-hidden="true" />
        </a> : null}
      </header>

      {!loading && !error ? <div className={styles.overview}>
        <div className={styles.count}>
          <strong>{(count ?? summary.loadedCount).toLocaleString()}</strong>
          <span>{count === null ? "Events loaded" : "Events in selected dates"}</span>
          {hasMore ? <small>{summary.loadedCount.toLocaleString()} loaded · more available</small> : null}
        </div>
        {summary.dateLabel || summary.cities.length || summary.topics.length ? <div className={styles.facts}>
          <p className={styles.factLabel}>In the events shown</p>
          {summary.dateLabel ? <p><CalendarDays aria-hidden="true" />{summary.dateLabel}</p> : null}
          {summary.cities.length ? <p><MapPin aria-hidden="true" />{summary.cities.slice(0, 4).join(" · ")}
            {summary.cities.length > 4 ? ` · +${summary.cities.length - 4} more places` : ""}</p> : null}
          {summary.topics.length ? <ul className={styles.topics} aria-label="Topics in these events">
            {summary.topics.slice(0, 6).map((topic) => <li key={topic.key}>
              {onTopicSelect ? <button type="button" onClick={() => onTopicSelect(topic.key)}>
                {topic.label}
              </button> : <span>{topic.label}</span>}
            </li>)}
          </ul> : null}
        </div> : null}
      </div> : null}

      <div className={styles.listHeading}>
        <h2>Events</h2>
        <div className={styles.views} aria-label="View this source's events">
          {onViewEvents ? <button type="button" onClick={onViewEvents}>All events</button> : null}
          {onMap ? <button type="button" onClick={onMap}><MapPin aria-hidden="true" />Map</button> : null}
          {onCalendar ? <button type="button" onClick={onCalendar}><CalendarDays aria-hidden="true" />Calendar</button> : null}
        </div>
      </div>
      {loading ? <p className={styles.loading} role="status"><LoaderCircle className="spin" aria-hidden="true" />Loading events</p>
        : error ? <div className={styles.error} role="alert">
          <p>Could not load this source’s events.</p>
          {onRetry ? <button type="button" onClick={onRetry}>Try again</button> : null}
        </div> : <EventList
          events={events}
          expandedId={expandedId}
          onExpandedChange={onExpandedChange}
          emptyTitle="No events in these dates"
          emptyCopy="Change the date filter to see more from this source."
          hasMore={hasMore}
          loadingMore={loadingMore}
          onLoadMore={onLoadMore}
          onEntitySelect={onEntitySelect}
          onTopicSelect={onTopicSelect}
        />}
    </section>
  );
}
