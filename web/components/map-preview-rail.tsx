"use client";

import { ArrowUpRight, CalendarDays, MapPin, X } from "lucide-react";
import type { RefObject } from "react";

import { formatEventDate, formatEventTime } from "@/lib/date";
import { eventImageUrl } from "@/lib/event-image";
import { eventLocationLabel, eventPageUrl } from "@/lib/event-links";
import { dayCompactLabel, dayTrackLabel } from "@/lib/map-days";
import type { MapDayGroup, MapDayModel } from "@/lib/map-days";
import type { EventItem } from "@/lib/types";

interface MapPreviewRailProps {
  events: EventItem[];
  selectedId: string | null;
  onSelect: (event: EventItem) => void;
  groups: MapDayGroup[];
  model: MapDayModel;
  activeDay: string | null;
  daySectionRefs: RefObject<Map<string, HTMLElement>>;
  onDayActivate: (dayKey: string | null) => void;
  onDayFit: (dayKey: string | null) => void;
}

interface MapPreviewCardProps {
  event: EventItem;
  selected: boolean;
  muted: boolean;
  onSelect: (event: EventItem) => void;
}

function MapPreviewCard({ event, selected, muted, onSelect }: MapPreviewCardProps) {
  const date = formatEventDate(event.start_at);
  const imageUrl = eventImageUrl(event);
  const pageUrl = eventPageUrl(event);
  const sourceLabel = event.calendar_labels?.find((value) => value.trim())
    ?? event.sources.find((source) => source.label?.trim())?.label
    ?? event.providers?.find((value) => value.trim());
  return (
    <article
      className={`map-preview${selected ? " is-selected" : ""}${muted ? " is-muted" : ""}`}
      role="listitem"
    >
      <button
        className="map-preview__select"
        type="button"
        aria-label={`Focus ${event.title} on map`}
        aria-pressed={selected}
        onClick={() => onSelect(event)}
      >
        {imageUrl ? (
          <img
            className="map-preview__image"
            src={imageUrl}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
          />
        ) : (
          <time
            className="map-preview__date"
            dateTime={event.start_at}
            aria-hidden="true"
          >
            <span>{date.month}</span>
            <strong>{date.day}</strong>
          </time>
        )}
        <span className="map-preview__copy">
          {sourceLabel ? (
            <small className="map-preview__source">{sourceLabel}</small>
          ) : null}
          <small>
            <CalendarDays aria-hidden="true" />
            {date.weekday} · {formatEventTime(event.start_at, event.end_at)}
          </small>
          <strong>{event.title}</strong>
          <span>
            <MapPin aria-hidden="true" />
            {eventLocationLabel(event)}
          </span>
        </span>
      </button>

      {pageUrl ? (
        <a
          className="map-preview__link"
          href={pageUrl}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`View ${event.title} event page (opens in new tab)`}
        >
          View event
          <ArrowUpRight aria-hidden="true" />
        </a>
      ) : null}
    </article>
  );
}

export function MapPreviewRail({
  events,
  selectedId,
  onSelect,
  groups,
  model,
  activeDay,
  daySectionRefs,
  onDayActivate,
  onDayFit,
}: MapPreviewRailProps) {
  /**
   * Grouping follows the result set, not the viewport — the same test the day track
   * mounts on. Panning down to a single day must not silently drop the headers while
   * the track still says a day is emphasized. A single-day result has nothing to
   * group, so it renders exactly the flat list it always has.
   */
  const grouped = model.cells.length > 1;

  /**
   * An emphasized day that has been panned out of view owns no group, so it is
   * spliced back in as an empty section rather than vanishing from the list it is
   * supposed to be filtering.
   */
  const sections = grouped && activeDay && !groups.some((group) => group.key === activeDay)
    ? [...groups, { key: activeDay, events: [] }].sort(
      (left, right) => left.key.localeCompare(right.key),
    )
    : groups;

  return (
    <aside
      className="map-preview-rail"
      aria-label="Events visible in map"
    >
      <header className="map-preview-rail__heading">
        <div>
          <p>IN THIS AREA</p>
          <h2>Visible events</h2>
        </div>
        <span aria-live="polite">
          {activeDay ? `${model.activeInView}/${model.totalInView}` : events.length}
        </span>
      </header>

      {grouped && activeDay ? (
        <div className="map-preview-rail__filter">
          <span>{dayTrackLabel(activeDay)}</span>
          <button
            type="button"
            onClick={() => onDayActivate(null)}
            aria-label="Show all days"
          >
            <X aria-hidden="true" />
            All days
          </button>
        </div>
      ) : null}

      {!events.length && !sections.length ? (
        <p className="map-preview-rail__empty" role="status">
          Move or zoom the map to find events in this area.
        </p>
      ) : grouped ? (
        <div className="map-preview-rail__list">
          {sections.map((group) => {
            const active = activeDay === group.key;
            const muted = activeDay !== null && !active;
            const headingId = `map-preview-day-${group.key}`;
            const elsewhere = active ? model.activeTotal : 0;
            return (
              <section
                className={`map-preview-day${active ? " is-active" : ""}${muted ? " is-muted" : ""}`}
                key={group.key}
                ref={(node) => {
                  if (node) daySectionRefs.current.set(group.key, node);
                  else daySectionRefs.current.delete(group.key);
                }}
                role="group"
                aria-labelledby={headingId}
              >
                <button
                  className="map-preview-day__heading"
                  id={headingId}
                  type="button"
                  aria-pressed={active}
                  onClick={() => onDayActivate(active ? null : group.key)}
                >
                  <span>{dayTrackLabel(group.key)}</span>
                  <span className="map-preview-day__count">
                    {group.events.length} here
                    {muted ? <span className="sr-only">, dimmed</span> : null}
                  </span>
                </button>

                {group.events.length ? (
                  <div className="map-preview-day__list" role="list">
                    {group.events.map((event) => (
                      <MapPreviewCard
                        key={event.canonical_event_id}
                        event={event}
                        selected={selectedId === event.canonical_event_id}
                        muted={muted && selectedId !== event.canonical_event_id}
                        onSelect={onSelect}
                      />
                    ))}
                  </div>
                ) : (
                  <p className="map-preview-day__stub" role="status">
                    No {dayCompactLabel(group.key)} events in this area
                    {elsewhere ? ` · ${elsewhere} elsewhere` : ""}
                    {elsewhere ? (
                      <button type="button" onClick={() => onDayFit(group.key)}>
                        Fit to {dayCompactLabel(group.key)}
                      </button>
                    ) : null}
                  </p>
                )}
              </section>
            );
          })}
        </div>
      ) : (
        <div className="map-preview-rail__list" role="list">
          {events.map((event) => (
            <MapPreviewCard
              key={event.canonical_event_id}
              event={event}
              selected={selectedId === event.canonical_event_id}
              muted={false}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </aside>
  );
}
