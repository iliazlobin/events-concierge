import type { EventEntityKind, EventItem } from "./types.ts";

type LocatedEvent = Pick<
  EventItem,
  "venue_name" | "city" | "latitude" | "longitude"
>;

type RegistrationEvent = Pick<EventItem, "registration_status">;

type DescribedEvent = Pick<EventItem, "title" | "description">;

export type EventFormatLabel = "Online" | "In person";

export type EventFacetSignal =
  | "Meetup"
  | "Founders"
  | "AI"
  | "Tech"
  | "Music"
  | "Arts"
  | "Family"
  | "Workshop";

export type EntityProfileNetwork = "linkedin" | "website";

export interface SafeEntityProfile {
  url: string;
  network: EntityProfileNetwork;
}

interface SignalRule {
  label: EventFacetSignal;
  pattern: RegExp;
}

const UNKNOWN_LOCATION = /^(?:location\s+)?(?:tba|tbd|unknown|n\/?a|to be announced)$/i;
const ONLINE_VENUE = /^(?:(?:online|virtual)(?:\s+(?:event|only|venue|via\s+.+))?|webinar|zoom(?:\s+event)?|live[\s-]?stream)$/i;
const LINKEDIN_HOSTS = new Set(["linkedin.com", "www.linkedin.com"]);
const LINKEDIN_PERSON_PATH = /^\/in\/[a-z0-9][a-z0-9_%.-]*\/?$/i;
const LINKEDIN_COMPANY_PATH = /^\/company\/[a-z0-9][a-z0-9_%.-]*\/?$/i;

const SIGNAL_RULES: readonly SignalRule[] = [
  {
    label: "Meetup",
    pattern: /\b(?:meet[\s-]?up|networking(?:\s+(?:event|mixer|night))?)\b/i,
  },
  {
    label: "Founders",
    pattern: /\b(?:founder|founders|co[\s-]?founder|entrepreneur|entrepreneurs)\b/i,
  },
  {
    label: "AI",
    pattern: /\b(?:ai|artificial intelligence|generative ai|machine learning|large language models?|llms?)\b/i,
  },
  {
    label: "Tech",
    pattern: /\b(?:tech|technology|software|hardware|developer|developers|coding)\b/i,
  },
  {
    label: "Music",
    pattern: /\b(?:music|concert|jazz|orchestra|band|singer|songwriter|dj set)\b/i,
  },
  {
    label: "Arts",
    pattern: /\b(?:art|arts|gallery|exhibition|museum|theatre|theater|dance|film|cinema|poetry)\b/i,
  },
  {
    label: "Family",
    pattern: /\b(?:family|families|kids?|children|child[\s-]?friendly|all ages)\b/i,
  },
  {
    label: "Workshop",
    pattern: /\b(?:workshop|hands[\s-]?on|training|bootcamp)\b/i,
  },
];

function compactText(value: string | null | undefined): string {
  return value?.replace(/\s+/g, " ").trim() ?? "";
}

function validCoordinate(
  value: number | null,
  minimum: number,
  maximum: number,
): boolean {
  return typeof value === "number"
    && Number.isFinite(value)
    && value >= minimum
    && value <= maximum;
}

function usefulLocationText(value: string | null): boolean {
  const text = compactText(value);
  return Boolean(text) && !UNKNOWN_LOCATION.test(text);
}

export function eventFormatLabel(event: LocatedEvent): EventFormatLabel | null {
  const venue = compactText(event.venue_name);
  if (venue && ONLINE_VENUE.test(venue)) return "Online";

  if (
    usefulLocationText(event.venue_name)
    || usefulLocationText(event.city)
    || (
      validCoordinate(event.latitude, -90, 90)
      && validCoordinate(event.longitude, -180, 180)
    )
  ) {
    return "In person";
  }
  return null;
}

export function eventRegistrationLabel(
  event: RegistrationEvent,
): "Registration open" | "Waitlist" | "Sold out" | null {
  if (event.registration_status === "open") return "Registration open";
  if (event.registration_status === "waitlist") return "Waitlist";
  if (event.registration_status === "sold_out") return "Sold out";
  return null;
}

export function eventFacetSignals(event: DescribedEvent): EventFacetSignal[] {
  const title = compactText(event.title);
  const description = compactText(event.description);
  return SIGNAL_RULES
    .map((rule, index) => ({
      index,
      label: rule.label,
      relevance: rule.pattern.test(title)
        ? 2
        : rule.pattern.test(description)
          ? 1
          : 0,
    }))
    .filter((match) => match.relevance > 0)
    .sort((left, right) => (
      right.relevance - left.relevance
      || left.index - right.index
    ))
    .slice(0, 4)
    .map((match) => match.label);
}

export function safeEntityProfile(
  value: string | null | undefined,
  kind: EventEntityKind,
): SafeEntityProfile | null {
  const candidate = value?.trim();
  if (!candidate || /[\u0000-\u001f\u007f]/.test(candidate)) return null;

  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return null;
  }
  if (
    url.protocol !== "https:"
    || !url.hostname
    || url.username
    || url.password
  ) {
    return null;
  }

  const hostname = url.hostname.toLocaleLowerCase();
  if (LINKEDIN_HOSTS.has(hostname)) {
    const directPath = kind === "person"
      ? LINKEDIN_PERSON_PATH
      : kind === "organization"
        ? LINKEDIN_COMPANY_PATH
        : null;
    if (!directPath?.test(url.pathname)) return null;
    url.search = "";
    url.hash = "";
    return {
      url: url.href,
      network: "linkedin",
    };
  }

  if (kind !== "organization") return null;
  url.hash = "";
  return {
    url: url.href,
    network: "website",
  };
}
