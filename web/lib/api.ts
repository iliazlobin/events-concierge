import type {
  CatalogFilters,
  CatalogEntity,
  CatalogEntityDetail,
  CatalogDaySummary,
  CatalogPage,
  FeedPage,
  Me,
  UiConfig,
  PreferencesResult,
  SavedFilter,
  AccountErasureReceipt,
  Profile,
  Page,
  RequestSummary,
  RegistrationSummary,
  TaskSummary,
  ApiKey,
  IssuedApiKey,
  AvatarUploaded,
} from "./types.ts";
import {
  filterWindow,
  normalizeDateRangeFilters,
  serializeDateRangeFilterForApi,
} from "./date.ts";

export const SESSION_KEY = "events-concierge.local-session.v1";

interface RequestOptions extends RequestInit {
  tenantId?: string | null;
  bodyJson?: unknown;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(
    message: string,
    status: number,
  ) {
    super(message);
    this.status = status;
  }
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

let csrfCookieName: string | null = null;
let csrfHeaderName: string | null = null;

/**
 * Publish the deployment's CSRF contract from `/v1/ui-config`.
 *
 * The server verifies three factors on every consumer mutation: the session cookie's tenant, the
 * `Origin` header, and a double-submit token that must match the hash stored in the session record.
 * The browser can only satisfy the third, so `api()` stays unable to mutate until this runs. Call it
 * before the first `getMe`, or a mutation can race an unset contract and 403.
 */
export function setCsrfContract(cookieName: string | null, headerName: string | null): void {
  csrfCookieName = cookieName;
  csrfHeaderName = headerName;
}

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const entry = part.trim();
    if (entry.startsWith(prefix)) return decodeURIComponent(entry.slice(prefix.length));
  }
  return null;
}

/** Render an unknown thrown value as one bounded sentence for a form or toast. */
export function readableError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

export async function api<T>(
  path: string,
  { tenantId, bodyJson, headers, ...options }: RequestOptions = {},
): Promise<T> {
  const requestHeaders = new Headers(headers);
  requestHeaders.set("Accept", "application/json");
  if (tenantId) requestHeaders.set("X-EC-Tenant-ID", tenantId);
  if (bodyJson !== undefined) requestHeaders.set("Content-Type", "application/json");
  const method = (options.method ?? "GET").toUpperCase();
  if (!SAFE_METHODS.has(method) && csrfCookieName && csrfHeaderName) {
    const token = readCookie(csrfCookieName);
    if (token) requestHeaders.set(csrfHeaderName, token);
  }
  const response = await fetch(path, {
    ...options,
    headers: requestHeaders,
    body: bodyJson === undefined ? options.body : JSON.stringify(bodyJson),
    credentials: "same-origin",
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) message = payload.detail;
    } catch {
      // Preserve the bounded status message when the edge did not return JSON.
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function readSession(): string | null {
  try {
    const raw = window.localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { tenantId?: unknown };
    return typeof parsed.tenantId === "string" ? parsed.tenantId : null;
  } catch {
    return null;
  }
}

export function writeSession(tenantId: string): void {
  window.localStorage.setItem(SESSION_KEY, JSON.stringify({ tenantId }));
}

export function clearSession(): void {
  window.localStorage.removeItem(SESSION_KEY);
}

export function getUiConfig(): Promise<UiConfig> {
  return api<UiConfig>("/v1/ui-config");
}

export function getMe(tenantId: string | null): Promise<Me> {
  return api<Me>("/v1/me", { tenantId });
}

export function onboard(email: string): Promise<{ tenant_id: string }> {
  return api<{ tenant_id: string }>("/v1/onboard", {
    method: "POST",
    bodyJson: { notify_email: email },
  });
}

/**
 * Build the filter half of a catalog query. The paged agenda, the calendar day
 * summary, and a single-day agenda must express identical filters, or the grid
 * would summarize a catalog the list refuses to serve.
 *
 * ``dayKey`` replaces every composed range with that one local calendar day.
 */
function catalogFilterQuery(filters: CatalogFilters, dayKey?: string): URLSearchParams {
  const query = new URLSearchParams();
  if (dayKey) {
    query.append(
      "date_range",
      serializeDateRangeFilterForApi({ id: dayKey, start: dayKey, end: dayKey }),
    );
  } else {
    const dateRanges = normalizeDateRangeFilters(filters.dateRanges);
    if (dateRanges.length) {
      for (const range of dateRanges) {
        query.append("date_range", serializeDateRangeFilterForApi(range));
      }
    } else if (filters.datePreset !== "all") {
      // Legacy presets and singular custom links keep their established API
      // representation until a user explicitly composes additive date ranges.
      const window = filterWindow(filters);
      query.set("starts_after", window.start.toISOString());
      query.set("starts_before", window.end.toISOString());
    }
  }
  for (const sourceKey of [...new Set(filters.sourceKeys ?? [])]) {
    query.append("source_key", sourceKey);
  }
  if (filters.query.trim()) query.set("q", filters.query.trim());
  const cities = filters.cities?.length ? filters.cities : filters.city ? [filters.city] : [];
  for (const city of [...new Set(cities)]) query.append("city", city);
  for (const scope of [...new Set(filters.locationScopes ?? [])]) {
    query.append("location_scope", scope);
  }
  for (const topic of [...new Set(filters.topics ?? [])]) query.append("topic", topic);
  if (filters.price !== "any") query.set("price", filters.price);
  const bounds = catalogPriceBoundCents(filters);
  if (bounds.min !== null) query.set("price_min_cents", String(bounds.min));
  if (bounds.max !== null) query.set("price_max_cents", String(bounds.max));
  if ((filters.availability ?? "any") !== "any") {
    query.set("availability", filters.availability);
  }
  return query;
}

export function getCatalogPage(
  tenantId: string | null,
  filters: CatalogFilters,
  cursor: string | null = null,
  options: { limit?: number; includeFacets?: boolean } = {},
): Promise<CatalogPage> {
  const query = catalogFilterQuery(filters);
  query.set("limit", String(options.limit ?? 72));
  query.set("sort", filters.sort ?? "soonest");
  if (cursor) query.set("cursor", cursor);
  if (options.includeFacets === false) query.set("include_facets", "false");
  return api<CatalogPage>(`/v1/catalog/events?${query}`, { tenantId });
}

/**
 * Count a calendar range into local days in one request.
 *
 * The calendar grid renders per-day totals and topic chips only, so paging the
 * whole range to derive them re-read every event to display a number: a busy
 * month cost more than a hundred sequential requests and started over on reload.
 */
export function getCatalogSummary(
  tenantId: string | null,
  filters: CatalogFilters,
  timeZone: string,
): Promise<CatalogDaySummary> {
  const query = catalogFilterQuery(filters);
  query.set("time_zone", timeZone);
  return api<CatalogDaySummary>(`/v1/catalog/events/summary?${query}`, { tenantId });
}

/**
 * Read only the facet inventory for a filter.
 *
 * The calendar needs the source, city, and topic facets that power the filter
 * rail, but not the events themselves; the smallest page carries the same
 * facets the agenda's first page carries.
 */
export function getCatalogFacets(
  tenantId: string | null,
  filters: CatalogFilters,
): Promise<CatalogPage> {
  const query = catalogFilterQuery(filters);
  query.set("limit", "1");
  query.set("sort", filters.sort ?? "soonest");
  return api<CatalogPage>(`/v1/catalog/events?${query}`, { tenantId });
}

/**
 * Read one local calendar day of the same filtered catalog.
 *
 * The agenda and the week previews render `items` and nothing else, so they ask
 * for the page alone. The source, city, and topic inventories that otherwise
 * accompany a first page are three whole-catalog aggregations costing about
 * twice the page itself, and the calendar already has them from the filter rail.
 */
export function getCatalogDayPage(
  tenantId: string | null,
  filters: CatalogFilters,
  dayKey: string,
  limit: number,
  cursor: string | null = null,
): Promise<CatalogPage> {
  const query = catalogFilterQuery(filters, dayKey);
  query.set("limit", String(limit));
  query.set("sort", "soonest");
  query.set("include_facets", "false");
  if (cursor) query.set("cursor", cursor);
  return api<CatalogPage>(`/v1/catalog/events?${query}`, { tenantId });
}

export function getCatalogEntities(
  tenantId: string | null,
  queryValue = "",
): Promise<CatalogEntity[]> {
  const query = new URLSearchParams({ limit: "80" });
  if (queryValue.trim()) query.set("q", queryValue.trim());
  return api<CatalogEntity[]>(`/v1/catalog/entities?${query}`, { tenantId });
}

export function getCatalogEntity(
  tenantId: string | null,
  entityId: string,
): Promise<CatalogEntityDetail> {
  return api<CatalogEntityDetail>(`/v1/catalog/entities/${encodeURIComponent(entityId)}`, {
    tenantId,
  });
}

export function refreshCatalogEntity(
  tenantId: string | null,
  entityId: string,
): Promise<CatalogEntityDetail> {
  return api<CatalogEntityDetail>(
    `/v1/catalog/entities/${encodeURIComponent(entityId)}/refresh`,
    { tenantId, method: "POST" },
  );
}

export function resolveCatalogEventEntity(
  tenantId: string | null,
  canonicalEventId: string,
  role: string,
  name: string,
): Promise<{ entity_id: string }> {
  const query = new URLSearchParams({
    canonical_event_id: canonicalEventId,
    role,
    name,
  });
  return api<{ entity_id: string }>(`/v1/catalog/entity-resolution?${query}`, { tenantId });
}

/**
 * Resolve the selected comparison into the two bounds the API accepts.
 *
 * The server takes a floor and a ceiling; every comparison the UI offers is one of the four ways
 * to fill those two slots. Amounts that do not parse leave their slot empty rather than sending a
 * bound the reader did not express.
 */
export function catalogPriceBoundCents(
  filters: Pick<
    CatalogFilters,
    "price" | "priceComparison" | "priceMinDollars" | "priceMaxDollars"
  >,
): { min: number | null; max: number | null } {
  const comparison = filters.priceComparison ?? "any";
  const minimum = catalogPriceMaxCents(filters.priceMinDollars);
  const maximum = catalogPriceMaxCents(filters.priceMaxDollars);
  // A floor cannot describe an event with no price, so these categories carry no amount at all.
  const floorAllowed = filters.price !== "free" && filters.price !== "unknown";
  if (comparison === "at-most") return { min: null, max: maximum };
  if (comparison === "at-least") return { min: floorAllowed ? minimum : null, max: null };
  if (comparison === "exactly") {
    return floorAllowed && minimum !== null
      ? { min: minimum, max: minimum }
      : { min: null, max: null };
  }
  if (comparison === "between") {
    if (minimum === null || maximum === null || minimum > maximum) {
      return { min: null, max: null };
    }
    return { min: floorAllowed ? minimum : null, max: maximum };
  }
  return { min: null, max: null };
}

export function catalogPriceMaxCents(value: string | undefined): number | null {
  const normalized = (value ?? "").trim();
  if (!/^\d{1,7}(?:\.\d{1,2})?$/.test(normalized)) return null;
  const [whole, fraction = ""] = normalized.split(".");
  const cents = (Number(whole) * 100) + Number(fraction.padEnd(2, "0"));
  return Number.isSafeInteger(cents) && cents >= 1 && cents <= 100_000_000
    ? cents
    : null;
}

export function getFeed(tenantId: string | null, text: string): Promise<FeedPage> {
  return api<FeedPage>("/v1/feed", {
    tenantId,
    method: "POST",
    bodyJson: { text },
  });
}

export function recordFeedback(
  tenantId: string,
  canonicalEventId: string,
  kind: "like" | "dismiss" | "click",
): Promise<void> {
  return api<void>("/v1/feed-feedback", {
    tenantId,
    method: "POST",
    bodyJson: {
      signal_id: crypto.randomUUID(),
      canonical_event_id: canonicalEventId,
      kind,
    },
  });
}

/** Replace the whole explicit-interest set. The server rewrites affinities wholesale; there is no merge. */
/**
 * The tenant's saved filter selections, already ordered most-recently-used first.
 *
 * The server owns that order because it owns `last_used_at`; re-deriving it on the client would
 * drift the moment two tabs applied different selections.
 */
export function listSavedFilters(tenantId: string | null): Promise<SavedFilter[]> {
  return api<SavedFilter[]>("/v1/me/saved-filters", { tenantId });
}

export function createSavedFilter(
  name: string,
  filters: CatalogFilters,
  tenantId: string | null,
): Promise<SavedFilter> {
  return api<SavedFilter>("/v1/me/saved-filters", {
    method: "POST",
    tenantId,
    bodyJson: { name, filters },
  });
}

export function replaceSavedFilter(
  savedFilterId: string,
  name: string,
  filters: CatalogFilters,
  tenantId: string | null,
): Promise<SavedFilter> {
  return api<SavedFilter>(`/v1/me/saved-filters/${encodeURIComponent(savedFilterId)}`, {
    method: "PUT",
    tenantId,
    bodyJson: { name, filters },
  });
}

/** Move a selection to the front of the recency order without editing it. */
export function recordSavedFilterUse(
  savedFilterId: string,
  tenantId: string | null,
): Promise<SavedFilter> {
  return api<SavedFilter>(
    `/v1/me/saved-filters/${encodeURIComponent(savedFilterId)}/applied`,
    { method: "POST", tenantId },
  );
}

export function deleteSavedFilter(
  savedFilterId: string,
  tenantId: string | null,
): Promise<void> {
  return api<void>(`/v1/me/saved-filters/${encodeURIComponent(savedFilterId)}`, {
    method: "DELETE",
    tenantId,
  });
}

export function updatePreferences(
  interests: string[],
  revision: number,
  tenantId: string | null,
): Promise<PreferencesResult> {
  return api<PreferencesResult>("/v1/preferences", {
    method: "PUT",
    tenantId,
    bodyJson: { interests, revision },
  });
}

/** End the deployment session server-side. Local demo has no server session to end. */
export function logout(logoutUrl: string): Promise<void> {
  return api<void>(logoutUrl, { method: "POST" });
}

/**
 * Fence the account and enqueue erasure.
 *
 * `requestId` must be replayed verbatim on retry: a fresh id against a live erasure is a 409, not a
 * second attempt. A 428 means the deployment wants a fresh sign-in first.
 */
export function requestAccountErasure(
  requestId: string,
  tenantId: string | null,
): Promise<AccountErasureReceipt> {
  return api<AccountErasureReceipt>("/v1/me/erasure-requests", {
    method: "POST",
    tenantId,
    bodyJson: { request_id: requestId, confirmation: "DELETE MY ACCOUNT" },
  });
}

/** Begin a purpose-bound re-authentication hop and hand back where to send the browser. */
export function startReauthentication(
  reauthUrl: string,
  returnTo: string,
): Promise<{ authorization_url: string }> {
  return api<{ authorization_url: string }>(reauthUrl, {
    method: "POST",
    bodyJson: { return_to: returnTo },
  });
}

/** Replace the mutable profile facts. The revision is caller-minted and must strictly increase. */
export function updateProfile(
  body: { display_name: string | null; time_zone: string | null; revision: number },
  tenantId: string | null,
): Promise<Profile> {
  return api<Profile>("/v1/me/profile", { method: "PUT", tenantId, bodyJson: body });
}

export function getRequestHistory(tenantId: string | null): Promise<Page<RequestSummary>> {
  return api<Page<RequestSummary>>("/v1/requests?limit=20", { tenantId });
}

export function getRegistrations(tenantId: string | null): Promise<Page<RegistrationSummary>> {
  return api<Page<RegistrationSummary>>("/v1/registrations?limit=20", { tenantId });
}

export function getTasks(tenantId: string | null): Promise<Page<TaskSummary>> {
  return api<Page<TaskSummary>>("/v1/tasks?state=actionable&limit=20", { tenantId });
}

/**
 * Ask the concierge to withdraw. The lifecycle keys on the event, not a registration id -- there is
 * no registration identifier on the wire. A conflict here means "not yet", not "failed".
 */
export function withdrawRegistration(
  canonicalEventId: string,
  requestId: string,
  tenantId: string | null,
): Promise<{ status: string }> {
  return api<{ status: string }>("/v1/unrsvp", {
    method: "POST",
    tenantId,
    bodyJson: { canonical_event_id: canonicalEventId, request_id: requestId },
  });
}

/**
 * Upload one avatar as a raw image body.
 *
 * Deliberately not multipart: `image/*` is not a CORS-simple content type, so a cross-origin form
 * cannot reach the route, and there is no filename field to sanitize. The server re-encodes what it
 * receives, so the client crop is a convenience and never the security boundary.
 */
export function uploadAvatar(blob: Blob, tenantId: string | null): Promise<AvatarUploaded> {
  return api<AvatarUploaded>("/v1/me/avatar", {
    method: "POST",
    tenantId,
    body: blob,
    headers: { "Content-Type": blob.type },
  });
}

export function deleteAvatar(tenantId: string | null): Promise<void> {
  return api<void>("/v1/me/avatar", { method: "DELETE", tenantId });
}

export function getApiKeys(tenantId: string | null): Promise<ApiKey[]> {
  return api<ApiKey[]>("/v1/me/api-keys", { tenantId });
}

/** Create a key. The returned `secret` is shown once and is not recoverable afterwards. */
export function createApiKey(name: string, tenantId: string | null): Promise<IssuedApiKey> {
  return api<IssuedApiKey>("/v1/me/api-keys", { method: "POST", tenantId, bodyJson: { name } });
}

export function revokeApiKey(keyId: string, tenantId: string | null): Promise<ApiKey> {
  return api<ApiKey>(`/v1/me/api-keys/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
    tenantId,
  });
}
