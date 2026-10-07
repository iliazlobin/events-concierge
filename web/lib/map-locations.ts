import type { EventItem } from "./types.ts";

export type MapCoordinate = [longitude: number, latitude: number];
export type MappableEvent = Pick<EventItem, "latitude" | "longitude">
  & Partial<Pick<EventItem, "city" | "venue_name" | "source_keys" | "sources">>;

export interface MapLocation {
  coordinate: MapCoordinate;
  precision: "venue" | "area";
  areaKey?: string;
  label?: string;
}

interface Area {
  label: string;
  coordinate: MapCoordinate;
}

/**
 * Display-only area centers, verified 2026-10-06. They never become catalog coordinates,
 * deduplication evidence, radius-search inputs or directions to an event's venue.
 * SF Find neighborhood bounding-box midpoints (public domain):
 * https://data.sf.gov/Geographic-Locations-and-Boundaries/SF-Find-Neighborhoods/gfpk-269f
 */
const SF_AREAS: Readonly<Record<string, Area>> = Object.freeze({
  "alamo square": { label: "Alamo Square", coordinate: [-122.4347, 37.7763] },
  castro: { label: "Castro", coordinate: [-122.4340, 37.7625] },
  chinatown: { label: "Chinatown", coordinate: [-122.4066, 37.7941] },
  "civic center": { label: "Civic Center", coordinate: [-122.4181, 37.7777] },
  "cow hollow": { label: "Cow Hollow", coordinate: [-122.4424, 37.7976] },
  dogpatch: { label: "Dogpatch", coordinate: [-122.3899, 37.7598] },
  "downtown (sf)": { label: "Downtown / Union Square", coordinate: [-122.4062, 37.7871] },
  "duboce triangle": { label: "Duboce Triangle", coordinate: [-122.4316, 37.7670] },
  "fidi (sf)": { label: "Financial District", coordinate: [-122.3970, 37.7925] },
  "fisherman's wharf": { label: "Fisherman's Wharf", coordinate: [-122.4141, 37.8082] },
  "golden gate park": { label: "Golden Gate Park", coordinate: [-122.4830, 37.7693] },
  "haight ashbury": { label: "Haight Ashbury", coordinate: [-122.4487, 37.7697] },
  "hayes valley": { label: "Hayes Valley", coordinate: [-122.4271, 37.7765] },
  "lower haight": { label: "Lower Haight", coordinate: [-122.4298, 37.7727] },
  "lower nob hill": { label: "Lower Nob Hill", coordinate: [-122.4160, 37.7883] },
  marina: { label: "Marina", coordinate: [-122.4372, 37.8038] },
  mission: { label: "Mission", coordinate: [-122.4141, 37.7591] },
  "mission bay": { label: "Mission Bay", coordinate: [-122.3916, 37.7714] },
  "nob hill": { label: "Nob Hill", coordinate: [-122.4136, 37.7932] },
  "north beach": { label: "North Beach", coordinate: [-122.4089, 37.8008] },
  "pacific heights": { label: "Pacific Heights", coordinate: [-122.4345, 37.7925] },
  panhandle: { label: "Panhandle", coordinate: [-122.4459, 37.7739] },
  "potrero hill": { label: "Potrero Hill", coordinate: [-122.3995, 37.7578] },
  "presidio heights": { label: "Presidio Heights", coordinate: [-122.4524, 37.7889] },
  "rincon hill": { label: "Rincon Hill", coordinate: [-122.3909, 37.7872] },
  "russian hill": { label: "Russian Hill", coordinate: [-122.4176, 37.8004] },
  soma: { label: "South of Market", coordinate: [-122.4081, 37.7801] },
  "south beach": { label: "South Beach", coordinate: [-122.3901, 37.7819] },
  "telegraph hill": { label: "Telegraph Hill", coordinate: [-122.4055, 37.8023] },
  "union square (sf)": { label: "Downtown / Union Square", coordinate: [-122.4062, 37.7871] },
  // OpenStreetMap contributors (ODbL); existing map attribution also applies.
  // https://www.openstreetmap.org/node/358803650
  "jackson square": { label: "Jackson Square", coordinate: [-122.4030, 37.7969] },
  // Combined extent midpoint of waterfront street sections (not Embarcadero station):
  // https://www.openstreetmap.org/way/769778928
  // https://www.openstreetmap.org/way/8922234
  // https://www.openstreetmap.org/way/417536971
  embarcadero: { label: "Embarcadero", coordinate: [-122.3998, 37.7951] },
  // https://www.openstreetmap.org/relation/8524888
  "salesforce park": { label: "Salesforce Park", coordinate: [-122.3967, 37.7891] },
});

export function eventMapLocation(event: MappableEvent): MapLocation | null {
  const { latitude, longitude } = event;
  if (typeof latitude === "number" && typeof longitude === "number"
    && Number.isFinite(latitude) && Number.isFinite(longitude)
    && latitude >= -90 && latitude <= 90 && longitude >= -180 && longitude <= 180) {
    return { coordinate: [longitude, latitude], precision: "venue" };
  }
  // A malformed or half-supplied coordinate must not be hidden by the area fallback.
  if (latitude !== null || longitude !== null) return null;
  const city = event.city?.normalize("NFKD").replace(/[^a-z0-9]/gi, "").toLowerCase();
  const sourceKey = "tech-week-sf-2026";
  if (city !== "sanfrancisco" || !(event.source_keys?.includes(sourceKey)
    || event.sources?.some((source) => source.source_key === sourceKey))) return null;
  // Exact vocabulary only: no city-center guesses, address parsing or substring matching.
  const name = event.venue_name?.trim().toLowerCase() ?? "";
  const area = Object.hasOwn(SF_AREAS, name) ? SF_AREAS[name] : undefined;
  return area ? {
    coordinate: [...area.coordinate],
    precision: "area",
    areaKey: `sf:${area.label}`,
    label: area.label,
  } : null;
}

export interface MapMarkerGroup<Event> {
  location: MapLocation;
  events: Event[];
}

/** Area centers share one count marker; individual provider coordinates keep their own pins. */
export function mapMarkerGroups<Event extends MappableEvent>(events: Event[]): MapMarkerGroup<Event>[] {
  const groups: MapMarkerGroup<Event>[] = [];
  const areas = new Map<string, MapMarkerGroup<Event>>();
  for (const event of events) {
    const location = eventMapLocation(event);
    if (!location) continue;
    const existing = location.areaKey ? areas.get(location.areaKey) : undefined;
    if (existing) existing.events.push(event);
    else {
      const group = { location, events: [event] };
      groups.push(group);
      if (location.areaKey) areas.set(location.areaKey, group);
    }
  }
  return groups;
}
