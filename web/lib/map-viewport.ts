import type { EventItem } from "./types.ts";
import { locationScope } from "./location-scopes.ts";
import type { LocationScope } from "./types.ts";

export type MapCoordinate = [longitude: number, latitude: number];

export interface CenterViewport {
  kind: "center";
  center: MapCoordinate;
  zoom: number;
}

export interface BoundsViewport {
  kind: "bounds";
  coordinates: MapCoordinate[];
}

export type MapViewport = CenterViewport | BoundsViewport;

export interface MapBoundsLike {
  contains(coordinate: MapCoordinate): boolean;
}

export const DEFAULT_MAP_VIEWPORT: CenterViewport = {
  kind: "center",
  center: [-122.2, 37.72],
  zoom: 8.6,
};

const CITY_CENTERS: Readonly<Record<string, MapCoordinate>> = Object.freeze({
  alameda: [-122.2416, 37.7652],
  berkeley: [-122.2727, 37.8715],
  brentwood: [-121.6958, 37.9319],
  burlingame: [-122.3661, 37.5841],
  campbell: [-121.95, 37.2872],
  concord: [-122.0311, 37.978],
  crockett: [-122.22, 38.0524],
  cupertino: [-122.0322, 37.323],
  dalycity: [-122.4702, 37.6879],
  fremont: [-121.9886, 37.5485],
  gilroy: [-121.5683, 37.0058],
  halfmoonbay: [-122.4286, 37.4636],
  hayward: [-122.0808, 37.6688],
  lafayette: [-122.118, 37.8858],
  livermore: [-121.768, 37.6819],
  losangeles: [-118.2437, 34.0522],
  losaltos: [-122.1141, 37.3852],
  losgatos: [-121.9624, 37.2358],
  martinez: [-122.1341, 38.0194],
  menlopark: [-122.1817, 37.453],
  millbrae: [-122.3875, 37.5985],
  milpitas: [-121.9066, 37.4323],
  mountainview: [-122.0838, 37.3861],
  newyork: [-74.006, 40.7128],
  oakland: [-122.2711, 37.8044],
  orinda: [-122.1797, 37.8771],
  pacifica: [-122.4869, 37.6138],
  paloalto: [-122.143, 37.4419],
  pittsburg: [-121.8847, 38.028],
  pleasanton: [-121.8747, 37.6624],
  pleasanthill: [-122.0608, 37.948],
  redwoodcity: [-122.2364, 37.4852],
  richmond: [-122.3477, 37.9358],
  sanbruno: [-122.4111, 37.6305],
  sanfrancisco: [-122.4194, 37.7749],
  sanjose: [-121.8863, 37.3382],
  sanleandro: [-122.1561, 37.7249],
  sanmateo: [-122.3255, 37.563],
  sanramon: [-121.978, 37.7799],
  santamonica: [-118.4912, 34.0195],
  santaclara: [-121.9552, 37.3541],
  southsanfrancisco: [-122.4077, 37.6547],
  sunnyvale: [-122.0363, 37.3688],
  unioncity: [-122.0438, 37.5934],
  walnutcreek: [-122.0652, 37.9101],
});

function cityKey(value: string): string {
  return value.normalize("NFKD").replace(/[^a-z0-9]/gi, "").toLowerCase();
}

export function eventMapCoordinate(
  event: Pick<EventItem, "latitude" | "longitude">,
): MapCoordinate | null {
  const latitude = event.latitude;
  const longitude = event.longitude;
  if (
    typeof latitude !== "number"
    || typeof longitude !== "number"
    || !Number.isFinite(latitude)
    || !Number.isFinite(longitude)
    || latitude < -90
    || latitude > 90
    || longitude < -180
    || longitude > 180
  ) {
    return null;
  }
  return [longitude, latitude];
}

export function eventsInMapBounds<
  Event extends Pick<EventItem, "latitude" | "longitude">,
>(
  events: Event[],
  bounds: MapBoundsLike,
): Event[] {
  return events.filter((event) => {
    const coordinate = eventMapCoordinate(event);
    return coordinate !== null && bounds.contains(coordinate);
  });
}

export function cityMapViewport(city: string): CenterViewport {
  const center = CITY_CENTERS[cityKey(city)];
  return center
    ? { kind: "center", center, zoom: 11 }
    : DEFAULT_MAP_VIEWPORT;
}

export function mapViewportFor(
  events: Array<Pick<EventItem, "latitude" | "longitude">>,
  city: string | string[],
  scopes: LocationScope[] = [],
): MapViewport {
  const coordinates = events.flatMap((event) => {
    const coordinate = eventMapCoordinate(event);
    return coordinate ? [coordinate] : [];
  });
  if (coordinates.length === 1) {
    return { kind: "center", center: coordinates[0], zoom: 12 };
  }
  if (coordinates.length > 1) return { kind: "bounds", coordinates };

  const cities = typeof city === "string" ? (city ? [city] : []) : city;
  const fallbackCoordinates: MapCoordinate[] = [];
  for (const selectedScope of scopes) {
    const scope = locationScope(selectedScope);
    if (!scope) continue;
    fallbackCoordinates.push(scope.bounds[0], scope.bounds[1]);
  }
  for (const selectedCity of cities) {
    const viewport = cityMapViewport(selectedCity);
    if (viewport !== DEFAULT_MAP_VIEWPORT) fallbackCoordinates.push(viewport.center);
  }
  if (fallbackCoordinates.length > 1) {
    return { kind: "bounds", coordinates: fallbackCoordinates };
  }
  if (fallbackCoordinates.length === 1) {
    return { kind: "center", center: fallbackCoordinates[0], zoom: 11 };
  }
  return DEFAULT_MAP_VIEWPORT;
}
