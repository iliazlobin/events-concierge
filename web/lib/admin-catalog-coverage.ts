import type {
  AdminCatalogEntityProfile,
  AdminCatalogEvent,
  AdminCatalogQualityIssue,
} from "./admin-types.ts";
import { safeEntityProfile } from "./event-facets.ts";
import { formatEventPrice } from "./event-price.ts";

export type AdminCatalogAvailability = "present" | "unknown" | "not_applicable";

export interface AdminCatalogFactLink {
  label: string;
  href: string;
}

export interface AdminCatalogFact {
  id: string;
  label: string;
  state: AdminCatalogAvailability;
  value: string;
  links?: AdminCatalogFactLink[];
}

export interface AdminCatalogFactGroup {
  id: "access" | "people" | "evidence";
  label: string;
  facts: AdminCatalogFact[];
}

export interface AdminCatalogReadiness {
  ready: boolean;
  gapCount: number;
  label: string;
  detail: string;
}

export const READINESS_FIELDS: ReadonlyArray<{
  issue: AdminCatalogQualityIssue;
  label: string;
}> = [
  { issue: "missing_description", label: "Description" },
  { issue: "missing_end_time", label: "End time" },
  { issue: "missing_venue", label: "Venue" },
  { issue: "missing_city", label: "City" },
  { issue: "missing_geo", label: "Coordinates" },
  { issue: "missing_registration_url", label: "Event URL" },
];

const ISSUE_LABELS = new Map(
  READINESS_FIELDS.map((field) => [field.issue, field.label.toLocaleLowerCase()]),
);

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function listValue(values: string[]): string {
  return values.length ? values.join(", ") : "Not published";
}

function roleFact(id: string, label: string, values: string[]): AdminCatalogFact {
  return {
    id,
    label,
    state: values.length ? "present" : "unknown",
    value: listValue(values),
  };
}

function safeDirectProfile(profile: AdminCatalogEntityProfile): AdminCatalogFactLink | null {
  const displayKind = profile.role === "organizer" || profile.role === "partner"
    ? "organization"
    : "person";
  const safe = safeEntityProfile(profile.profile_url, displayKind);
  return safe ? { label: profile.name, href: safe.url } : null;
}

function distinctNamedEntityCount(event: AdminCatalogEvent): number {
  const names = [
    event.organizer_name,
    ...event.host_names,
    ...event.speaker_names,
    ...event.partner_names,
  ].filter((name): name is string => Boolean(name));
  return new Set(names.map((name) => name.toLocaleLowerCase())).size;
}

export function canonicalReadiness(event: AdminCatalogEvent): AdminCatalogReadiness {
  const gaps = READINESS_FIELDS.filter((field) => event.quality_issues.includes(field.issue));
  if (!gaps.length) {
    return {
      ready: true,
      gapCount: 0,
      label: "Discovery ready",
      detail: "Canonical discovery signals are available",
    };
  }
  return {
    ready: false,
    gapCount: gaps.length,
    label: `${gaps.length} discovery ${gaps.length === 1 ? "gap" : "gaps"}`,
    detail: `Missing ${gaps.map((field) => ISSUE_LABELS.get(field.issue)).join(", ")}`,
  };
}

export function catalogEnrichmentSummary(event: AdminCatalogEvent): string[] {
  const summary: string[] = [];
  const price = formatEventPrice(event);
  if (price) summary.push(price);
  if (event.registration_status !== "unknown") {
    summary.push(humanize(event.registration_status));
  }
  const namedEntities = distinctNamedEntityCount(event);
  if (namedEntities) {
    summary.push(`${namedEntities} named ${namedEntities === 1 ? "entity" : "entities"}`);
  }
  if (event.attendance_count !== null) {
    summary.push(`${event.attendance_count.toLocaleString("en-US")} going`);
  }
  return summary.slice(0, 3);
}

export function catalogMetadataGroups(event: AdminCatalogEvent): AdminCatalogFactGroup[] {
  const formattedPrice = formatEventPrice(event);
  const exactPrice = event.price_status === "paid" && formattedPrice && formattedPrice !== "Paid"
    ? formattedPrice
    : null;
  const profileLinks = event.entity_profiles
    .map(safeDirectProfile)
    .filter((link): link is AdminCatalogFactLink => link !== null);
  const namedEntities = distinctNamedEntityCount(event);

  return [
    {
      id: "access",
      label: "Access",
      facts: [
        {
          id: "price_status",
          label: "Price classification",
          state: event.price_status === "unknown" ? "unknown" : "present",
          value: event.price_status === "unknown" ? "Not published" : humanize(event.price_status),
        },
        {
          id: "exact_price",
          label: "Exact price",
          state: event.price_status === "free"
            ? "not_applicable"
            : exactPrice
              ? "present"
              : "unknown",
          value: event.price_status === "free"
            ? "Free event"
            : exactPrice ?? "Amount not published",
        },
        {
          id: "registration_status",
          label: "Registration state",
          state: event.registration_status === "unknown" ? "unknown" : "present",
          value: event.registration_status === "unknown"
            ? "Not published"
            : humanize(event.registration_status),
        },
        {
          id: "event_page",
          label: "Event page",
          state: event.registration_url ? "present" : "unknown",
          value: event.registration_url ? "Available" : "Not published",
        },
      ],
    },
    {
      id: "people",
      label: "People & organizations",
      facts: [
        {
          id: "organizer",
          label: "Organizer",
          state: event.organizer_name ? "present" : "unknown",
          value: event.organizer_name ?? "Not published",
        },
        roleFact("hosts", "Hosts", event.host_names),
        roleFact("speakers", "Speakers", event.speaker_names),
        roleFact("partners", "Partners / vendors", event.partner_names),
        {
          id: "profiles",
          label: "Direct profiles",
          state: profileLinks.length
            ? "present"
            : namedEntities
              ? "unknown"
              : "not_applicable",
          value: profileLinks.length
            ? `${profileLinks.length} source-provided direct ${profileLinks.length === 1 ? "link" : "links"}`
            : namedEntities
              ? "No direct links published"
              : "No named entities",
          links: profileLinks,
        },
        {
          id: "attendance",
          label: "Public attendance",
          state: event.attendance_count === null ? "unknown" : "present",
          value: event.attendance_count === null
            ? "Not published"
            : `${event.attendance_count.toLocaleString("en-US")} going`,
        },
      ],
    },
    {
      id: "evidence",
      label: "Canonical evidence",
      facts: [
        {
          id: "source_record",
          label: "Source record",
          state: "present",
          value: event.source_event_id,
        },
        {
          id: "last_observed",
          label: "Last observed",
          state: "present",
          value: event.last_seen_at,
        },
        {
          id: "refresh_run",
          label: "Refresh run",
          state: "present",
          value: event.refresh_run_key,
        },
        {
          id: "normalization",
          label: "Normalization",
          state: "present",
          value: `normalizer ${event.normalizer_version} · merge ${event.merge_version}`,
        },
      ],
    },
  ];
}
