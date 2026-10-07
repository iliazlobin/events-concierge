"use client";

import { Network, CalendarDays, MapPin, X } from "lucide-react";
import type { RefObject } from "react";

import { formatEventDate, formatEventTime } from "@/lib/date";
import { eventImageUrl } from "@/lib/event-image";
import { eventLocationLabel, eventPageUrl } from "@/lib/event-links";
import { dayCompactLabel, dayTrackLabel } from "@/lib/map-days";
import type { MapDayGroup, MapDayModel } from "@/lib/map-days";
import { eventMapCoordinate } from "@/lib/map-viewport";
import { eventMapLocation } from "@/lib/map-locations";
import type { EventEntityReference, EventItem } from "@/lib/types";

interface MapPreviewRailProps {
  events: EventItem[];
  unmappedEvents: EventItem[];
  mappedCount: number;
  showUnmapped: boolean;
  approximateCount: number;
  areaLabel?: string;
  onAreaClear: () => void;
  onListChange: (unmapped: boolean) => void;
  selectedId: string | null;
  onSelect: (event: EventItem) => void;
  onEntitySelect?: (reference: EventEntityReference) => void;
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
  onEntitySelect?: (reference: EventEntityReference) => void;
}

function MapPreviewCard({ event, selected, muted, onSelect, onEntitySelect }: MapPreviewCardProps) {
  const graphEntity = event.organizer_name
    ? { canonicalEventId: event.canonical_event_id, name: event.organizer_name, role: "organizer" as const }
    : event.host_names?.[0]
      ? { canonicalEventId: event.canonical_event_id, name: event.host_names[0], role: "host" as const }
      : event.speaker_names?.[0]
        ? { canonicalEventId: event.canonical_event_id, name: event.speaker_names[0], role: "speaker" as const }
        : null;
  const date = formatEventDate(event.start_at);
  const imageUrl = eventImageUrl(event);
  const pageUrl = eventPageUrl(event);
  const hasMapLocation = eventMapCoordinate(event) !== null;
  const mapLocation = eventMapLocation(event);
  const sourceLabel = event.calendar_labels?.find((value) => value.trim())
    ?? event.sources.find((source) => source.label?.trim())?.label
    ?? event.providers?.find((value) => value.trim());
  return (
    <article
      className={`map-preview${selected ? " is-selected" : ""}${muted ? " is-muted" : ""}`}
      role="listitem"
    >
      <div className="map-preview__summary" onClick={(interaction) => {
        if (interaction.target instanceof Element && !interaction.target.closest("a, button")) onSelect(event);
      }}>
        <button
          className="map-preview__select"
          type="button"
          aria-label={hasMapLocation ? `Focus ${event.title} on map` : `Show details for ${event.title}`}
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
        </button>
        <span className="map-preview__copy">
          {sourceLabel ? (
            <small className="map-preview__source">{sourceLabel}</small>
          ) : null}
          <small>
            <CalendarDays aria-hidden="true" />
            {date.weekday} · {formatEventTime(event.start_at, event.end_at)}
          </small>
          <strong>{pageUrl ? <a className="map-preview__title-link" href={pageUrl}
            target="_blank" rel="noopener noreferrer" title="Open provider event page (opens in new tab)">
            {event.title}
          </a> : event.title}</strong>
          <span>
            <MapPin aria-hidden="true" />
            {eventLocationLabel(event)}
          </span>
          {!hasMapLocation ? <small className="map-preview__location-note">Not plotted on map</small> : null}
          {mapLocation?.precision === "area" ? (
            <small className="map-preview__location-note">Approximate area · {mapLocation.label}</small>
          ) : null}
        </span>
      </div>

      {graphEntity && onEntitySelect ? <div className="map-preview__actions">
        <button type="button" className="map-preview__link"
          aria-label={`View ${event.title} in ${graphEntity.name}'s graph`}
          onClick={() => onEntitySelect(graphEntity)}>
          <Network aria-hidden="true" /> View in graph
        </button>
      </div> : null}
    </article>
  );
}

export function MapPreviewRail({
  events,
  unmappedEvents,
  mappedCount,
  showUnmapped,
  approximateCount,
  areaLabel,
  onAreaClear,
  onListChange,
  selectedId,
  onSelect,
  onEntitySelect,
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
  const grouped = !showUnmapped && model.cells.length > 1;
  const displayedEvents = showUnmapped ? unmappedEvents : events;

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
      aria-label={showUnmapped ? "Events without map locations" : "Events visible in map"}
    >
      {unmappedEvents.length ? (
        <div className="map-preview-rail__tabs" role="group" aria-label="Map event lists">
          <button type="button" aria-pressed={!showUnmapped} onClick={() => onListChange(false)}
            disabled={mappedCount === 0}>
            In this area <span>{events.length}</span>
          </button>
          <button type="button" aria-pressed={showUnmapped} onClick={() => onListChange(true)}>
            Without map locations <span>{unmappedEvents.length}</span>
          </button>
        </div>
      ) : null}
      <header className="map-preview-rail__heading">
        <div>
          <p>{showUnmapped ? "MATCHING YOUR FILTERS" : "IN THIS AREA"}</p>
          <h2>{showUnmapped ? "Events without pins" : "Visible events"}</h2>
        </div>
        <span aria-live="polite">
          {showUnmapped ? unmappedEvents.length : activeDay ? `${model.activeInView}/${model.totalInView}` : events.length}
        </span>
      </header>

      {showUnmapped ? <p className="map-preview-rail__notice">
        These events have no map coordinates. Open an event for its location details.
      </p> : approximateCount ? <p className="map-preview-rail__notice">
        Outlined markers show approximate areas, not venues. Check the event page for the address.
      </p> : null}

      {areaLabel && !showUnmapped ? (
        <div className="map-preview-rail__filter">
          <span>Approximate area · {areaLabel}</span>
          <button type="button" onClick={onAreaClear} aria-label="Show all areas">
            <X aria-hidden="true" /> All areas
          </button>
        </div>
      ) : null}

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

      {!displayedEvents.length && !sections.length ? (
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
                        onEntitySelect={onEntitySelect}
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
          {displayedEvents.map((event) => (
            <MapPreviewCard
              key={event.canonical_event_id}
              event={event}
              selected={selectedId === event.canonical_event_id}
              muted={false}
              onSelect={onSelect}
                        onEntitySelect={onEntitySelect}
            />
          ))}
        </div>
      )}
    </aside>
  );
}
