"use client";

import type { Map as MapLibreMap, Marker } from "maplibre-gl";
import { LoaderCircle, LocateFixed, MapPin } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { EventList } from "@/components/event-list";
import { MapDayTrack } from "@/components/map-day-track";
import { MapPreviewRail } from "@/components/map-preview-rail";
import {
  dayFullLabel,
  eventDayKey,
  groupVisibleEventsByDay,
  mapDayModel,
  stepDay,
} from "@/lib/map-days";
import {
  DEFAULT_MAP_VIEWPORT,
  eventMapCoordinate,
  eventsInMapBounds,
  mapViewportFor,
} from "@/lib/map-viewport";
import type { EventEntityReference, EventItem } from "@/lib/types";
import type { LocationScope } from "@/lib/types";

interface MapViewProps {
  events: EventItem[];
  cities: string[];
  locationScopes: LocationScope[];
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onSourceSelect: (sourceKey: string) => void;
  onFacetSelect: (value: string) => void;
  onEntitySelect?: (reference: EventEntityReference) => void;
  onTopicSelect: (topic: string) => void;
}

const MAP_STYLE = {
  version: 8 as const,
  sources: {
    osm: {
      type: "raster" as const,
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [{ id: "osm", type: "raster" as const, source: "osm" }],
};

/** Above this many pins the hover preview stops paying for its class walk. */
const PEEK_MARKER_LIMIT = 800;

interface EventMarker {
  eventId: string;
  marker: Marker;
}

export function MapView({
  events,
  cities,
  locationScopes,
  loading,
  error,
  hasMore,
  loadingMore,
  onLoadMore,
  onSourceSelect,
  onFacetSelect,
  onEntitySelect,
  onTopicSelect,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const markersRef = useRef<EventMarker[]>([]);
  const mapped = useMemo(
    () => events.filter((event) => eventMapCoordinate(event) !== null),
    [events],
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedIdRef = useRef<string | null>(selectedId);
  selectedIdRef.current = selectedId;
  const [visibleIds, setVisibleIds] = useState<string[]>(
    () => mapped.map((event) => event.canonical_event_id),
  );
  const [mapReady, setMapReady] = useState(false);
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(false);

  /** The day the reader committed to, and the day they are only hovering. */
  const [activeDay, setActiveDay] = useState<string | null>(null);
  const [peekDay, setPeekDay] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  /** Peek borrows the map's emphasis for as long as the pointer is there and commits nothing. */
  const emphasisDay = peekDay ?? activeDay;
  const activeDayRef = useRef<string | null>(activeDay);
  activeDayRef.current = activeDay;
  /**
   * Read at marker-creation time the way `selectedIdRef.current` already is, so a
   * marker rebuilt mid-emphasis is born in the right tier and the build effect never
   * needs the emphasized day as a dependency.
   */
  const emphasisDayRef = useRef<string | null>(emphasisDay);
  emphasisDayRef.current = emphasisDay;
  /** Height the day track occupies over the canvas; feeds fitBounds' top padding. */
  const trackInsetRef = useRef(0);
  /**
   * The mapped ids the last automatic fit answered for. Appending a page is not a new
   * spatial question, so `Load more` must not yank the camera back to whole-result
   * bounds from wherever the reader had panned. Every other change still refits.
   */
  const fittedIdsRef = useRef<string[]>([]);
  const daySectionRefs = useRef(new Map<string, HTMLElement>());

  const selected = useMemo(
    () => events.filter((event) => event.canonical_event_id === selectedId),
    [events, selectedId],
  );
  const visibleEvents = useMemo(() => {
    const visible = new Set(visibleIds);
    return mapped.filter((event) => visible.has(event.canonical_event_id));
  }, [mapped, visibleIds]);
  const dayModel = useMemo(
    () => mapDayModel(mapped, visibleIds, activeDay),
    [activeDay, mapped, visibleIds],
  );
  const dayGroups = useMemo(
    () => groupVisibleEventsByDay(visibleEvents),
    [visibleEvents],
  );
  const multiDay = dayModel.cells.length > 1;
  const dayModelRef = useRef(dayModel);
  dayModelRef.current = dayModel;

  const selectEvent = useCallback((event: EventItem) => {
    setSelectedId(event.canonical_event_id);
    // Deliberately clicking a dimmed pin promotes its day — the fastest hop there is.
    // Browsing all days is a mode, so a click never creates an emphasis that was not
    // already there. The functional form keeps this out of the dependency array.
    setActiveDay((current) => (current ? eventDayKey(event) ?? current : current));
    const map = mapRef.current;
    const coordinate = eventMapCoordinate(event);
    if (!map || !coordinate) return;
    const view = {
      center: coordinate,
      zoom: Math.max(map.getZoom(), 12),
    };
    if (prefersReducedMotion) {
      map.jumpTo(view);
    } else {
      map.easeTo({ ...view, duration: 420 });
    }
  }, [prefersReducedMotion]);

  /** The only thing in this feature that moves the camera, and only when asked. */
  const fitToDay = useCallback((dayKey: string | null) => {
    const map = mapRef.current;
    if (!map) return;
    const coordinates = mapped
      .filter((event) => !dayKey || eventDayKey(event) === dayKey)
      .map((event) => eventMapCoordinate(event))
      .filter((coordinate): coordinate is [number, number] => coordinate !== null);
    if (!coordinates.length) return;
    const longitudes = coordinates.map(([longitude]) => longitude);
    const latitudes = coordinates.map(([, latitude]) => latitude);
    map.fitBounds(
      [
        [Math.min(...longitudes), Math.min(...latitudes)],
        [Math.max(...longitudes), Math.max(...latitudes)],
      ],
      {
        padding: {
          top: 70 + trackInsetRef.current,
          right: map.getContainer().clientWidth > 760 ? 300 : 70,
          bottom: 70,
          left: 70,
        },
        maxZoom: 14,
        duration: prefersReducedMotion ? 0 : 620,
      },
    );
  }, [mapped, prefersReducedMotion]);

  const activateDay = useCallback((dayKey: string | null) => {
    setPeekDay(null);
    setActiveDay(dayKey);
    if (!dayKey) return;
    const section = daySectionRefs.current.get(dayKey);
    const list = section?.parentElement;
    if (section && list) {
      list.scrollTo({
        top: Math.max(0, section.offsetTop - list.offsetTop),
        behavior: prefersReducedMotion ? "auto" : "smooth",
      });
    }
    // The one exception to "a day never moves the camera": a day with nothing in the
    // current viewport is the only case where standing still shows the reader nothing.
    const cell = dayModelRef.current.cells.find((entry) => entry.key === dayKey);
    if (cell && cell.inView === 0 && cell.total > 0) fitToDay(dayKey);
  }, [fitToDay, prefersReducedMotion]);

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const syncPreference = () => setPrefersReducedMotion(preference.matches);
    syncPreference();
    preference.addEventListener("change", syncPreference);
    return () => preference.removeEventListener("change", syncPreference);
  }, []);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    let disposed = false;
    let map: MapLibreMap | null = null;
    void import("maplibre-gl").then((maplibre) => {
      if (disposed || !containerRef.current) return;
      map = new maplibre.Map({
        container: containerRef.current,
        style: MAP_STYLE,
        center: DEFAULT_MAP_VIEWPORT.center,
        zoom: DEFAULT_MAP_VIEWPORT.zoom,
        attributionControl: false,
      });
      mapRef.current = map;
      map.addControl(
        new maplibre.AttributionControl({ compact: true }),
        "bottom-right",
      );
      map.addControl(
        new maplibre.NavigationControl({ showCompass: false }),
        "bottom-right",
      );
      setMapReady(true);
    });
    return () => {
      disposed = true;
      markersRef.current.forEach(({ marker }) => marker.remove());
      markersRef.current = [];
      fittedIdsRef.current = [];
      if (map) {
        map.remove();
      }
      if (mapRef.current === map) mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!mapReady || !mapRef.current) return;
    let cancelled = false;
    void import("maplibre-gl").then((maplibre) => {
      if (cancelled || !mapRef.current) return;
      const map = mapRef.current;
      markersRef.current.forEach(({ marker }) => marker.remove());
      markersRef.current = [];
      const emphasis = emphasisDayRef.current;
      for (const event of mapped) {
        const coordinates = eventMapCoordinate(event);
        if (!coordinates) continue;
        const element = document.createElement("button");
        element.className = "map-marker";
        element.type = "button";
        element.dataset.day = eventDayKey(event) ?? "";
        // maplibre writes inline transform and opacity onto this element on every
        // render, so every visual lives on the inner dot instead.
        const dot = document.createElement("span");
        dot.className = "map-marker__dot";
        element.append(dot);
        element.setAttribute(
          "aria-label",
          `Show ${event.title}, ${dayFullLabel(element.dataset.day)}`,
        );
        element.setAttribute(
          "aria-pressed",
          String(selectedIdRef.current === event.canonical_event_id),
        );
        element.classList.toggle(
          "is-selected",
          selectedIdRef.current === event.canonical_event_id,
        );
        element.classList.toggle(
          "is-day-active",
          emphasis !== null && emphasis === element.dataset.day,
        );
        element.classList.toggle(
          "is-muted",
          emphasis !== null && emphasis !== element.dataset.day,
        );
        element.addEventListener("click", (clickEvent) => {
          clickEvent.stopPropagation();
          selectEvent(event);
        });
        const marker = new maplibre.Marker({ element, anchor: "center" })
          .setLngLat(coordinates)
          .addTo(map);
        markersRef.current.push({
          eventId: event.canonical_event_id,
          marker,
        });
      }

      // Paginating appends to the head of the same result set; anything else — a new
      // filter, a new place — is a new spatial question and still refits.
      const mappedIds = mapped.map((event) => event.canonical_event_id);
      const fitted = fittedIdsRef.current;
      const appended = fitted.length > 0
        && mappedIds.length >= fitted.length
        && fitted.every((id, index) => mappedIds[index] === id);
      fittedIdsRef.current = mappedIds;
      if (appended) return;
      const viewport = mapViewportFor(mapped, cities, locationScopes);
      const duration = prefersReducedMotion ? 0 : 700;
      if (viewport.kind === "bounds") {
        const bounds = new maplibre.LngLatBounds();
        viewport.coordinates.forEach((coordinate) => bounds.extend(coordinate));
        map.fitBounds(bounds, {
          padding: {
            top: 70 + trackInsetRef.current,
            right: map.getContainer().clientWidth > 760 ? 300 : 70,
            bottom: 70,
            left: 70,
          },
          maxZoom: 12,
          duration,
        });
      } else if (prefersReducedMotion) {
        map.jumpTo({
          center: viewport.center,
          zoom: viewport.zoom,
        });
      } else {
        map.easeTo({
          center: viewport.center,
          zoom: viewport.zoom,
          duration,
        });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [cities, locationScopes, mapReady, mapped, prefersReducedMotion, selectEvent]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map) {
      setVisibleIds(mapped.map((event) => event.canonical_event_id));
      return;
    }
    const syncVisibleEvents = () => {
      setVisibleIds(
        eventsInMapBounds(mapped, map.getBounds())
          .map((event) => event.canonical_event_id),
      );
    };
    map.on("moveend", syncVisibleEvents);
    syncVisibleEvents();
    return () => {
      map.off("moveend", syncVisibleEvents);
    };
  }, [mapReady, mapped]);

  useEffect(() => {
    setSelectedId(null);
    setActiveDay(null);
    setPeekDay(null);
  }, [cities, locationScopes]);

  useEffect(() => {
    setSelectedId((current) => (
      current && !events.some((event) => event.canonical_event_id === current)
        ? null
        : current
    ));
  }, [events]);

  useEffect(() => {
    // A day that leaves the result set is dropped rather than re-pointed: a wrong day
    // that looks right is worse than no day.
    setActiveDay((current) => (
      current && !dayModel.keys.includes(current) ? null : current
    ));
  }, [dayModel.keys]);

  useEffect(() => {
    markersRef.current.forEach(({ eventId, marker }) => {
      const element = marker.getElement();
      const isSelected = selectedId === eventId;
      const day = element.dataset.day;
      marker.getElement().classList.toggle("is-selected", isSelected);
      element.classList.toggle(
        "is-day-active",
        emphasisDay !== null && day === emphasisDay,
      );
      element.classList.toggle(
        "is-muted",
        emphasisDay !== null && day !== emphasisDay,
      );
      element.setAttribute("aria-pressed", String(isSelected));
    });
  }, [emphasisDay, selectedId]);

  useEffect(() => {
    const stage = stageRef.current;
    const track = trackRef.current;
    if (!multiDay || !stage || !track || typeof ResizeObserver === "undefined") {
      trackInsetRef.current = 0;
      return;
    }
    const syncTrackInset = () => {
      const inset = track.getBoundingClientRect().height;
      trackInsetRef.current = inset;
      stage.style.setProperty("--map-day-track-height", `${Math.round(inset)}px`);
    };
    const observer = new ResizeObserver(syncTrackInset);
    observer.observe(track);
    syncTrackInset();
    return () => {
      observer.disconnect();
      stage.style.removeProperty("--map-day-track-height");
    };
  }, [multiDay]);

  useEffect(() => {
    if (!multiDay) return;
    const handleKeyDown = (keyEvent: KeyboardEvent) => {
      if (keyEvent.metaKey || keyEvent.ctrlKey || keyEvent.altKey) return;
      const target = keyEvent.target;
      if (
        target instanceof Element
        && target.closest("input, textarea, select, [contenteditable]")
      ) {
        return;
      }
      const keys = dayModelRef.current.keys;
      if (keyEvent.key === "]" || keyEvent.code === "BracketRight") {
        keyEvent.preventDefault();
        activateDay(stepDay(keys, activeDayRef.current, 1));
      } else if (keyEvent.key === "[" || keyEvent.code === "BracketLeft") {
        keyEvent.preventDefault();
        activateDay(activeDayRef.current ? stepDay(keys, activeDayRef.current, -1) : null);
      } else if (keyEvent.key === "0") {
        activateDay(null);
      } else if (/^[1-9]$/.test(keyEvent.key)) {
        const key = keys[Number(keyEvent.key) - 1];
        if (key) activateDay(key);
      } else if (keyEvent.key === "f" || keyEvent.key === "F") {
        fitToDay(activeDayRef.current);
      } else if (keyEvent.key === "Escape") {
        // A strict ladder, so one press never does two things.
        if (selectedIdRef.current) setSelectedId(null);
        else if (activeDayRef.current) activateDay(null);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [activateDay, fitToDay, multiDay]);

  useEffect(() => {
    if (!multiDay) {
      setAnnouncement("");
      return;
    }
    // Debounced so a fast scan through the week announces once, not eight times.
    const timer = window.setTimeout(() => {
      const model = dayModelRef.current;
      setAnnouncement(activeDay
        ? `Emphasizing ${dayFullLabel(activeDay)}. ${model.activeInView} of ${model.totalInView} events in this area, ${Math.max(0, model.activeTotal - model.activeInView)} outside.`
        : "Showing all days.");
    }, 400);
    return () => window.clearTimeout(timer);
  }, [activeDay, multiDay]);

  return (
    <section className="workspace map-view">
      <header className="map-heading">
        <h1>Event map</h1>
        <div className="view-result-state">
          <p>
            {loading && !events.length
              ? "Loading locations…"
              : `${mapped.length} mapped · ${events.length - mapped.length} list only${hasMore ? " · more available" : ""}`}
          </p>
          {hasMore ? (
            <button type="button" disabled={loadingMore} onClick={onLoadMore}>
              {loadingMore ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
              {loadingMore ? "Loading" : "Load more"}
            </button>
          ) : null}
        </div>
      </header>

      {error ? <p className="workspace-error" role="alert">{error}</p> : null}

      <div
        className="map-stage"
        ref={stageRef}
        data-day-track={multiDay ? "on" : "off"}
      >
        {multiDay ? (
          <MapDayTrack
            model={dayModel}
            activeDay={activeDay}
            peekDay={peekDay}
            peekEnabled={mapped.length <= PEEK_MARKER_LIMIT}
            trackRef={trackRef}
            onActivate={activateDay}
            onPeek={setPeekDay}
            onFit={fitToDay}
          />
        ) : null}

        <div
          className="map-frame"
          role="region"
          aria-label="Map of filtered events"
        >
          <div
            ref={containerRef}
            className="map-canvas"
            onClick={(event) => {
              const target = event.target;
              if (
                target instanceof Element
                && target.closest(".map-marker, .maplibregl-ctrl")
              ) {
                return;
              }
              setSelectedId(null);
            }}
          />
          {multiDay ? null : (
            <div className="map-watermark">
              <LocateFixed aria-hidden="true" />
              Mapped results
            </div>
          )}
          {!mapped.length ? (
            <div className="map-empty">
              <MapPin aria-hidden="true" />
              <p>No locations in this result set</p>
            </div>
          ) : null}
        </div>

        <MapPreviewRail
          onEntitySelect={onEntitySelect}
          events={visibleEvents}
          selectedId={selectedId}
          onSelect={selectEvent}
          groups={dayGroups}
          model={dayModel}
          activeDay={multiDay ? activeDay : null}
          daySectionRefs={daySectionRefs}
          onDayActivate={activateDay}
          onDayFit={fitToDay}
        />

        <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

        {selected.length ? (
          <aside className="map-selection">
            <EventList
              events={selected}
              compact
              expandedId={selectedId}
              onExpandedChange={setSelectedId}
              onSourceSelect={onSourceSelect}
              onFacetSelect={onFacetSelect}
              onEntitySelect={onEntitySelect}
              onTopicSelect={onTopicSelect}
            />
          </aside>
        ) : null}
      </div>
    </section>
  );
}
