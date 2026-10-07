import type { AgentBlock, AgentToolStep, SelectionEntry } from "./agent-stream.ts";

export type ViewName = "chat" | "events" | "map" | "calendar" | "entities";

export type CalendarMode = "week" | "month" | "six-months";

export type DatePreset =
  | "all"
  | "today"
  | "week"
  | "nextweek"
  | "workweek"
  | "nextworkweek"
  | "weekend"
  | "nextweekend"
  | "month"
  | "source"
  | "custom";

export type PriceFilter = "any" | "free" | "paid" | "unknown";

/** Registration posture exposed as a consumer filter. */
export type AvailabilityFilter = "any" | "available" | "sold_out";

/**
 * How the amount bounds read against an event's own price range.
 *
 * "any" ignores the amounts entirely. "at-most" is the original ceiling and still admits free
 * events, because nothing is below it. "at-least" cannot admit them, "exactly" pins both bounds
 * to one amount, and "between" is the open band.
 */
export type PriceComparison = "any" | "at-most" | "at-least" | "exactly" | "between";

export type CatalogSort = "soonest" | "latest";

export type LocationScope =
  | "bay_area"
  | "manhattan"
  | "los_angeles_area";

/**
 * An inclusive, local-calendar date range selected in the consumer UI.
 *
 * The id is deliberately persisted in browser history so React controls can
 * keep stable identities while a range is edited. Older links do not carry an
 * id; those ranges receive a deterministic id derived from their dates.
 */
export interface DateRangeFilter {
  id: string;
  start: string;
  end: string;
  label?: string;
}

export interface ConsumerIdentityConfig {
  project_id: string;
  api_key: string;
  auth_domain: string;
  providers: Array<"google.com" | "apple.com">;
}

export interface SignupLegalPolicy {
  terms_version: string;
  terms_url: string;
  privacy_version: string;
  privacy_url: string;
}

export interface UiConfig {
  release_profile?: "full" | "discovery";
  /** Muse remains unavailable unless the deployment explicitly enables it. */
  muse_enabled?: boolean;
  catalog_name_suggestions_enabled?: boolean;
  product_name: string;
  local_demo: boolean;
  auth_mode: "local_demo" | "deployment_session";
  /** Absent on older deployments; provider-specific UI must fail back to a generic label. */
  auth_provider?: "google" | "custom_claim" | "identity_platform" | null;
  anonymous_browsing?: boolean;
  identity_platform?: ConsumerIdentityConfig | null;
  /** Missing on older deployments: require real legal documents and acceptance. */
  consumer_legal_mode?: "required" | "deferred";
  legal_policy?: SignupLegalPolicy | null;
  auth_start_url: string | null;
  reauth_url: string | null;
  logout_url: string | null;
  csrf_cookie_name: string | null;
  csrf_header_name: string | null;
}

export interface Me {
  notify_email: string;
  interests: string[];
  preference_revision: number;
  /** Present in `MeOut` since the local-demo split; the client type had drifted behind it. */
  local_demo?: boolean;
  /** Whether to surface operator affordances. Presentation only; the admin API has its own gate. */
  is_admin?: boolean;
  /** Inbound-only concierge address provisioned at onboarding. Read-only, never editable. */
  relay_inbox?: string | null;
  profile?: Profile;
}

/** Mutable, user-authored display facts. Never an identity binding. */
export interface Profile {
  display_name: string | null;
  time_zone: string | null;
  avatar_url: string | null;
  revision: number;
  updated_at: string | null;
}

export interface PreferencesResult {
  status: string;
  interests: string[];
  revision: number;
}

export interface AccountErasureReceipt {
  request_id: string;
  status: string;
  workflow_targets: number;
  workflows_cancelled: number;
  calendar_targets: number;
  calendar_deleted: number;
  browser_sessions_revoked: boolean;
  credential_vault_purged: boolean;
  object_store_purged: boolean;
  retained_audit_rows: number;
  failed_stage: string | null;
}

export interface EventSource {
  source: string;
  registration_url: string;
  source_key?: string;
  label?: string;
  publisher?: string;
  provider?: string;
  seed_url?: string;
  source_event_id?: string;
  last_seen_at?: string;
  refresh_run_key?: string;
}

export type EventEntityRole =
  | "host"
  | "organizer"
  | "speaker"
  | "partner";

export type EventEntityKind = "person" | "organization";

export interface EventEntityProfile {
  name: string;
  role: EventEntityRole;
  kind: EventEntityKind;
  profile_url: string;
}

export interface EventEntityReference {
  canonicalEventId: string;
  role: EventEntityRole;
  name: string;
}

export type CatalogNameKind =
  | "event" | "venue" | "organizer" | "host" | "speaker" | "partner"
  | "person" | "organization";

export interface CatalogNameSuggestion {
  name: string;
  kinds: CatalogNameKind[];
  event_count: number;
}

export type CatalogEntityKind = "person" | "organization" | "unknown";

export interface CatalogEntity {
  entity_id: string;
  display_name: string;
  kind: CatalogEntityKind;
  identity_status: "profile_verified" | "source_scoped";
  canonical_profile_url: string | null;
  summary: string | null;
  website_url: string | null;
  logo_url: string | null;
  city: string | null;
  country: string | null;
  event_count: number;
  roles: EventEntityRole[];
  source_count: number;
  research_status:
    | "identity_required"
    | "researchable"
    | "queued"
    | "researching"
    | "review_pending";
}

export interface CatalogEntityEvent {
  canonical_event_id: string;
  title: string;
  start_at: string;
  end_at: string | null;
  venue_name: string | null;
  city: string | null;
  description: string;
  roles: EventEntityRole[];
  source_labels: string[];
  registration_url: string;
  is_past: boolean;
}

export interface CatalogEntityCollaborator {
  entity_id: string;
  display_name: string;
  kind: CatalogEntityKind;
  shared_event_count: number;
}

/** Derived from our own admitted catalog, never from an external provider. */
export interface CatalogEntityInsights {
  event_count: number;
  upcoming_count: number;
  past_count: number;
  first_event_at: string | null;
  last_event_at: string | null;
  recent_event_count: number;
  active_months: number;
  events_per_month: number | null;
  typical_attendance: number | null;
  free_count: number;
  paid_count: number;
  top_topics: string[];
  top_venues: string[];
  top_cities: string[];
  source_labels: string[];
  collaborators: CatalogEntityCollaborator[];
}

export interface CatalogEntityExternalSource {
  provider_key: string;
  external_id: string;
  source_url: string;
  display_name: string;
  status: "linked" | "fresh" | "failed" | "blocked";
  fetched_at: string | null;
  next_refresh_at: string;
  error_code: string | null;
}

export interface CatalogEntityExternalFact {
  provider_key: string;
  source_url: string;
  fact_key:
    | "description"
    | "website"
    | "location"
    | "country"
    | "founded"
    | "entity_type"
    | "focus"
    | "industry"
    | "profile"
    | "job_title"
    | "organization"
    | "known_for"
    | "public_repositories"
    | "followers"
    | "avatar";
  value: string;
  value_url: string | null;
  sort_order: number;
  observed_at: string;
}

export interface CatalogEntityDetail {
  entity: CatalogEntity;
  events: CatalogEntityEvent[];
  external_sources: CatalogEntityExternalSource[];
  external_facts: CatalogEntityExternalFact[];
  refresh_due: boolean;
  insights: CatalogEntityInsights | null;
}

export interface EventExtractionEvidence {
  field: "topic" | "price_status";
  value: string;
  source: "provider_metadata" | "title" | "description";
  rule: string;
}

export interface EventItem {
  discovery_state?: "upcoming" | "ongoing" | "past" | "cancelled" | "uncertain";
  source_freshness?: "recent" | "stale" | "unknown";
  additional_dates?: {
    canonical_event_id: string;
    start_at: string;
    end_at: string | null;
    registration_urls: string[];
  }[];
  canonical_event_id: string;
  title: string;
  image_url?: string | null;
  start_at: string;
  end_at: string | null;
  venue_name: string | null;
  city: string | null;
  description: string;
  price_status: "free" | "paid" | "unknown";
  price_min_cents: number | null;
  price_max_cents: number | null;
  price_currency: string | null;
  event_status: string;
  latitude: number | null;
  longitude: number | null;
  score: number | null;
  rationale: string;
  conflict: string;
  lanes: string[];
  registration_urls: string[];
  registerable: boolean | null;
  organizer_name: string | null;
  host_names: string[];
  speaker_names: string[];
  partner_names: string[];
  entity_profiles?: EventEntityProfile[];
  attendance_count: number | null;
  registration_status: "open" | "waitlist" | "sold_out" | "unknown";
  providers?: string[];
  calendar_labels?: string[];
  source_keys?: string[];
  sources: EventSource[];
  topics?: string[];
  extraction_evidence?: EventExtractionEvidence[];
}

export interface CatalogProvider {
  source_key: string;
  label: string;
  display_name: string;
  publisher: string;
  provider: string;
  seed_url: string;
  event_count: number;
}

export interface CatalogTopic {
  topic: string;
  label: string;
  event_count: number;
}

export interface CatalogCity {
  city: string;
  event_count: number;
}

export interface CatalogDayTopic {
  topic: string;
  label: string;
  event_count: number;
}

export interface CatalogDay {
  /** A local calendar day in the summary's own time zone, as YYYY-MM-DD. */
  start_day: string;
  /** Distinct events that day, so it is not the sum of `topics`. */
  event_count: number;
  topics: CatalogDayTopic[];
}

export interface CatalogDaySummary {
  days: CatalogDay[];
  total_event_count: number;
  time_zone: string;
}

export interface CatalogPage {
  items: EventItem[];
  next_cursor: string | null;
  providers: CatalogProvider[];
  city_facets?: CatalogCity[];
  topic_facets: CatalogTopic[];
}

export interface FeedUnderstanding {
  categories: string[];
  free_only: boolean;
  window_start: string | null;
  window_end: string | null;
  radius_km: number | null;
}

export interface FeedPage {
  request_id: string;
  items: EventItem[];
  next_cursor: string | null;
  understood: FeedUnderstanding;
}

export interface CatalogFilters {
  query: string;
  sort: CatalogSort;
  datePreset: DatePreset;
  customStart: string;
  customEnd: string;
  /** Additive date windows. Omitted by legacy snapshots and callers. */
  dateRanges?: DateRangeFilter[];
  /** Additive source selection. The server unions them. */
  sourceKeys: string[];
  /** Legacy single-city value retained for chat interpretation compatibility. */
  city: string;
  cities: string[];
  locationScopes: LocationScope[];
  price: PriceFilter;
  priceComparison: PriceComparison;
  priceMinDollars: string;
  priceMaxDollars: string;
  availability: AvailabilityFilter;
  topics: string[];
}

/** A named catalog filter selection the tenant saved and can reapply. */
export interface SavedFilter {
  saved_filter_id: string;
  name: string;
  filters: CatalogFilters;
  created_at: string | null;
  updated_at: string | null;
  last_used_at: string | null;
}

/** How the saved-filter picker orders its list. */
export type SavedFilterSort = "recent" | "name";

export interface DateWindow {
  start: Date;
  end: Date;
  label: string;
}

export interface ChatTurn {
  id: string;
  role: "user" | "assistant";
  text: string;
  items?: EventItem[];
  /** Server-derived renderable blocks. The client never parses prose into layout. */
  blocks?: AgentBlock[];
  /** What the concierge did to answer, for the collapsed trace rail. */
  trace?: AgentToolStep[];
  /** For a user turn: the context it was sent with, so the prompt can be recalled intact. */
  selection?: SelectionEntry[];
  pending?: boolean;
  error?: string;
  createdAt: number;
}

export interface RequestOutcome {
  canonical_event_id: string;
  title: string;
  start_at: string;
  state: string;
  lane: string | null;
  source: string | null;
}

export interface RequestSummary {
  request_id: string;
  text: string;
  state: string;
  created_at: string;
  categories: string[];
  free_only: boolean;
  window_start: string | null;
  window_end: string | null;
  outcome: RequestOutcome | null;
}

export interface RegistrationSummary {
  canonical_event_id: string;
  title: string;
  start_at: string;
  end_at: string | null;
  venue_name: string | null;
  city: string | null;
  description: string;
  price_status: string;
  event_status: string;
  state: string;
  lane: string | null;
  source: string | null;
  conflict_warning: boolean;
  registration_url: string | null;
  updated_at: string;
  can_withdraw: boolean;
}

export interface TaskSummary {
  task_id: string;
  canonical_event_id: string;
  event_summary: string;
  title: string;
  start_at: string;
  venue_name: string | null;
  city: string | null;
  reason: string;
  state: string;
  deep_link: string;
  expires_at: string;
  created_at: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface ApiKey {
  key_id: string;
  name: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  active: boolean;
}

/** The only shape that ever carries a key secret, returned once at creation. */
export interface IssuedApiKey {
  key: ApiKey;
  secret: string;
}

export interface AvatarUploaded {
  avatar_url: string;
  width: number;
  height: number;
  content_type: string;
  byte_size: number;
  checksum: string;
}
