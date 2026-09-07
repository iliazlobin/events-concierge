import { formatCity, formatLocation } from "./presentation.ts";
import type { EventItem } from "./types.ts";

type LinkableEvent = Pick<
  EventItem,
  | "title"
  | "start_at"
  | "end_at"
  | "venue_name"
  | "city"
  | "description"
  | "latitude"
  | "longitude"
  | "registration_urls"
  | "sources"
>;

const ONE_HOUR_MS = 60 * 60 * 1_000;

function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:"
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

function compactText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function linkLocation(event: LinkableEvent): string {
  const venue = event.venue_name?.trim() ?? "";
  const city = formatCity(event.city);
  const venueKey = venue.normalize("NFKD").replace(/[^a-z0-9]/gi, "").toLowerCase();
  const cityKey = city.normalize("NFKD").replace(/[^a-z0-9]/gi, "").toLowerCase();
  return venueKey && venueKey === cityKey
    ? city
    : [venue, city].filter(Boolean).join(", ");
}

function googleTimestamp(value: Date): string {
  return value.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

function validCoordinate(
  value: number | null,
  minimum: number,
  maximum: number,
): value is number {
  return typeof value === "number"
    && Number.isFinite(value)
    && value >= minimum
    && value <= maximum;
}

export function eventLocationLabel(
  event: Pick<
    LinkableEvent,
    "venue_name" | "city" | "latitude" | "longitude"
  >,
): string {
  const location = formatLocation(event.venue_name, event.city);
  if (location !== "Location TBA") return location;
  return validCoordinate(event.latitude, -90, 90)
    && validCoordinate(event.longitude, -180, 180)
    ? "Mapped location"
    : location;
}

export function eventPageUrl(event: LinkableEvent): string | null {
  const candidates = [
    ...event.registration_urls,
    ...event.sources.map((source) => source.registration_url),
  ];
  for (const candidate of candidates) {
    const url = safeHttpUrl(candidate);
    if (url) return url;
  }
  return null;
}

export function googleCalendarUrl(event: LinkableEvent): string | null {
  const start = new Date(event.start_at);
  if (Number.isNaN(start.valueOf())) return null;

  const requestedEnd = event.end_at ? new Date(event.end_at) : null;
  const end = requestedEnd
    && !Number.isNaN(requestedEnd.valueOf())
    && requestedEnd > start
    ? requestedEnd
    : new Date(start.valueOf() + ONE_HOUR_MS);
  const query = new URLSearchParams({
    action: "TEMPLATE",
    text: event.title,
    dates: `${googleTimestamp(start)}/${googleTimestamp(end)}`,
  });
  const location = linkLocation(event);
  if (location) query.set("location", location);

  const pageUrl = eventPageUrl(event);
  const details = [
    compactText(event.description),
    pageUrl ? `Event page: ${pageUrl}` : "",
  ].filter(Boolean);
  if (details.length) query.set("details", details.join("\n\n"));

  return `https://calendar.google.com/calendar/render?${query.toString()}`;
}

export function googleMapsUrl(event: LinkableEvent): string | null {
  let query = "";
  if (
    validCoordinate(event.latitude, -90, 90)
    && validCoordinate(event.longitude, -180, 180)
  ) {
    query = `${event.latitude},${event.longitude}`;
  } else {
    query = linkLocation(event);
  }
  if (!query) return null;
  const parameters = new URLSearchParams({ api: "1", query });
  return `https://www.google.com/maps/search/?${parameters.toString()}`;
}
