"use client";

import { Network, CalendarDays, MapPin, X } from "lucide-react";
import type { RefObject } from "react";

import { formatEventDate, formatEventTime } from "@/lib/date";
import { eventImageUrl } from "@/lib/event-image";
import { eventRegistrationLabel, eventVisibilityLabel } from "@/lib/event-facets";
import { discoveryLabels } from "@/lib/event-discovery";
import { eventSourceFilter, useEventActions } from "@/components/event-actions-context";
import { EventMuseAction } from "@/components/event-muse-action";
import { eventLocationLabel, eventPageUrl, googleCalendarUrl } from "@/lib/event-links";
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
  const actions = useEventActions();
  const source = eventSourceFilter(event);
  const calendarUrl = googleCalendarUrl(event);
  const time = formatEventTime(event.start_at, event.end_at);
  const highlights = [...discoveryLabels(event), eventRegistrationLabel(event), eventVisibilityLabel(event)]
    .filter((value): value is string => Boolean(value));
  return (
    <article
      className={`map-preview${selected ? " is-selected" : ""}${muted ? " is-muted" : ""}`}
      role="listitem"
    >
      <div className="map-preview__summary" onClick={(interaction) => {
        if (interaction.target instanceof Element && !interaction.target.closest("a, button")) onSelect(event);
      }}>
        {!imageUrl && calendarUrl ? <a className="map-preview__date-link" href={calendarUrl}
          target="_blank" rel="noopener noreferrer" title="Add to Google Calendar"
          aria-label={`Add ${event.title} on ${date.weekday}, ${date.month} ${date.day} to Google Calendar`}
          onPointerDown={interaction => interaction.stopPropagation()}
          onClick={interaction => interaction.stopPropagation()}>
          <time className="map-preview__date" dateTime={event.start_at} aria-hidden="true">
            <span>{date.month}</span><strong>{date.day}</strong>
          </time>
        </a> : <button
          className="map-preview__select"
          type="button"
          aria-label={hasMapLocation ? `Focus ${event.title} on map` : `Show details for ${event.title}`}
          aria-pressed={selected}
          onClick={() => onSelect(event)}
        >
          {imageUrl ? <img className="map-preview__image" src={imageUrl} alt="" loading="lazy" referrerPolicy="no-referrer" />
            : <time className="map-preview__date" dateTime={event.start_at} aria-hidden="true">
              <span>{date.month}</span><strong>{date.day}</strong>
            </time>}
        </button>}
        <span className="map-preview__copy">
          <small className="map-preview__source">
            {source.sourceKey && actions?.onSourceSelect ? <button type="button"
              className="map-preview__source-filter"
              aria-label={`View source ${source.label}`} title={`View source ${source.label}`}
              onPointerDown={interaction => interaction.stopPropagation()}
              onClick={interaction => { interaction.preventDefault(); interaction.stopPropagation(); actions.onSourceSelect?.(source.sourceKey!); }}>
              {source.label}
            </button> : source.label}
          </small>
          <small>{calendarUrl ? <a className="map-preview__calendar-link" href={calendarUrl}
            target="_blank" rel="noopener noreferrer" title="Add to Google Calendar"
            aria-label={`Add ${event.title} on ${date.weekday}, ${date.month} ${date.day} at ${time} to Google Calendar`}
            onPointerDown={interaction => interaction.stopPropagation()}
            onClick={interaction => interaction.stopPropagation()}>
            <CalendarDays aria-hidden="true" />{date.weekday} · {time}
          </a> : <><CalendarDays aria-hidden="true" />{date.weekday} · {time}</>}</small>
          <span className="map-preview__title-row event-title-row">
            <strong className="event-title-text">{pageUrl ? <a className="map-preview__title-link" href={pageUrl}
              target="_blank" rel="noopener noreferrer" title="Open provider event page (opens in new tab)">
              {event.title}
            </a> : event.title}</strong>
            <EventMuseAction event={event} />
          </span>
          {!imageUrl && calendarUrl ? <button className="map-preview__location-focus" type="button"
            aria-label={hasMapLocation ? `Focus ${event.title} on map` : `Show details for ${event.title}`}
            aria-pressed={selected}
            onClick={interaction => { interaction.stopPropagation(); onSelect(event); }}>
            <MapPin aria-hidden="true" />{eventLocationLabel(event)}
          </button> : <span><MapPin aria-hidden="true" />{eventLocationLabel(event)}</span>}
          {!hasMapLocation ? <small className="map-preview__location-note">Not plotted on map</small> : null}
          {mapLocation?.precision === "area" ? (
            <small className="map-preview__location-note">Approximate area · {mapLocation.label}</small>
          ) : null}
        </span>
      </div>

      {highlights.length ? <ul className="map-preview__highlights" aria-label="Event highlights">
        {highlights.map(label => <li key={label}>{label}</li>)}
      </ul> : null}

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
          <h2>{showUnmapped ? "Events without pins" : "Visible events"}</h2>
        </div>
        <span aria-live="polite">
          {showUnmapped ? unmappedEvents.length : activeDay ? `${model.activeInView}/${model.totalInView}` : events.length}
        </span>
      </header>

      {showUnmapped ? <p className="map-preview-rail__notice">
        Map location unavailable.
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
          No loaded events in this area.
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
