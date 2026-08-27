import { api } from "@/lib/api";
import { collectRoster } from "./admin-source-roster.ts";
import type {
  AdminCatalogEventCursor,
  AdminCatalogEventPage,
  AdminCommand,
  AdminCommandDetail,
  AdminCommandList,
  AdminCatalogFreshness,
  AdminConcentration,
  AdminFilterMetadata,
  AdminFleetShape,
  AdminFleetSummary,
  AdminOverview,
  AdminStageSummary,
  AdminThroughput,
  AdminRunFilters,
  AdminRunPage,
  AdminSourceDetail,
  AdminSourceConfigurationInput,
  AdminSourceConfigurationUpdate,
  AdminSourceBulkEnabledUpdate,
  AdminSourceEnabledTarget,
  AdminSourceFilters,
  AdminSourceHealthList,
  AdminSourcePage,
} from "@/lib/admin-types";

function setOptional(query: URLSearchParams, key: string, value: string): void {
  if (value) query.set(key, value);
}

export function getAdminOverview(): Promise<AdminOverview> {
  return api<AdminOverview>("/admin/v1/ingestion/overview", { cache: "no-store" });
}

/**
 * Server-computed rollups.
 *
 * These replace reducing the run ledger in the browser. Each is a single round trip returning a
 * result the database already aggregated, so a wider window costs one query rather than one
 * request per hundred rows — which is what made the 30-day pipeline view render zeros.
 */
export function getAdminFleetSummary(
  windowHours: number,
  includeFixtures = false,
): Promise<AdminFleetSummary> {
  const query = new URLSearchParams({
    window_hours: String(windowHours),
    include_fixtures: String(includeFixtures),
  });
  return api<AdminFleetSummary>(`/admin/v1/ingestion/summary?${query}`, {
    cache: "no-store",
  });
}

export function getAdminStageSummary(
  windowHours: number,
  includeFixtures = false,
): Promise<AdminStageSummary> {
  const query = new URLSearchParams({
    window_hours: String(windowHours),
    include_fixtures: String(includeFixtures),
  });
  return api<AdminStageSummary>(`/admin/v1/ingestion/stages?${query}`, {
    cache: "no-store",
  });
}

export function getAdminSourceHealth(
  includeFixtures = false,
): Promise<AdminSourceHealthList> {
  const query = new URLSearchParams({ include_fixtures: String(includeFixtures) });
  return api<AdminSourceHealthList>(`/admin/v1/ingestion/source-health?${query}`, {
    cache: "no-store",
  });
}

export function getAdminFleetShape(): Promise<AdminFleetShape> {
  return api<AdminFleetShape>("/admin/v1/ingestion/shape", { cache: "no-store" });
}

export function getAdminThroughput(
  windowHours: number,
  bucketHours: number,
): Promise<AdminThroughput> {
  const query = new URLSearchParams({
    window_hours: String(windowHours),
    bucket_hours: String(bucketHours),
  });
  return api<AdminThroughput>(`/admin/v1/ingestion/throughput?${query}`, {
    cache: "no-store",
  });
}

export function getAdminConcentration(limit = 15): Promise<AdminConcentration> {
  const query = new URLSearchParams({ limit: String(limit) });
  return api<AdminConcentration>(`/admin/v1/ingestion/concentration?${query}`, {
    cache: "no-store",
  });
}

export function getAdminCatalogFreshness(): Promise<AdminCatalogFreshness> {
  return api<AdminCatalogFreshness>("/admin/v1/ingestion/catalog-freshness", {
    cache: "no-store",
  });
}

export function getAdminFilters(
  filters: AdminSourceFilters,
): Promise<AdminFilterMetadata> {
  const query = new URLSearchParams({
    state: filters.state,
    include_fixtures: String(filters.includeFixtures),
  });
  setOptional(query, "query", filters.query.trim());
  setOptional(query, "mode", filters.mode);
  setOptional(query, "publisher", filters.publisher);
  setOptional(query, "region", filters.region);
  return api<AdminFilterMetadata>(`/admin/v1/ingestion/filters?${query}`, {
    cache: "no-store",
  });
}

/** The API's own per-page maximum for the source projection. */
const ADMIN_SOURCE_PAGE_LIMIT = 100;

export function getAdminSources(
  filters: AdminSourceFilters,
  page: { offset?: number } = {},
): Promise<AdminSourcePage> {
  const query = new URLSearchParams({
    state: filters.state,
    include_fixtures: String(filters.includeFixtures),
    sort_by: filters.sortBy,
    sort_direction: filters.sortDirection,
    limit: String(ADMIN_SOURCE_PAGE_LIMIT),
    offset: String(page.offset ?? 0),
  });
  setOptional(query, "query", filters.query.trim());
  setOptional(query, "mode", filters.mode);
  setOptional(query, "publisher", filters.publisher);
  setOptional(query, "region", filters.region);
  return api<AdminSourcePage>(`/admin/v1/ingestion/sources?${query}`, {
    cache: "no-store",
  });
}

/** Hard ceiling on roster paging. A roster is bounded; a ledger is not. */
export const ADMIN_SOURCE_ROSTER_CEILING = 1_000;

/**
 * Fetch the complete source roster.
 *
 * `getAdminSources` requests a single page of 100, which is the API's maximum. That silently
 * capped every count derived from it — the attention total, the action queue and every dropdown —
 * with no indication on screen. A roster is a bounded set that every comparable console renders
 * in full, so this pages to completion against the server's own `total` and, when it cannot,
 * reports that rather than returning a short list that reads as complete.
 *
 * This is deliberately not the run-ledger pattern: the roster is tens of rows and bounded by the
 * registry, whereas the run ledger grows without limit and must be aggregated server-side.
 */
export async function getAllAdminSources(
  filters: AdminSourceFilters,
): Promise<{ page: AdminSourcePage; truncated: boolean }> {
  // `collectRoster` always fetches offset 0 first, so this is assigned before it resolves.
  let first: AdminSourcePage | undefined;

  const collected = await collectRoster({
    fetchPage: async (offset) => {
      const page = await getAdminSources(filters, { offset });
      first ??= page;
      return page;
    },
    identity: (source) => source.source_key,
    ceiling: ADMIN_SOURCE_ROSTER_CEILING,
  });

  if (!first) throw new Error("admin source roster returned no page");

  return {
    page: { ...first, items: collected.items },
    truncated: collected.truncated,
  };
}

export function getAdminSourceDetail(
  sourceKey: string,
  windowHours: number,
  includeFixtures = false,
): Promise<AdminSourceDetail> {
  const bucketHours = windowHours === 24 ? 4 : 24;
  const query = new URLSearchParams({
    window_hours: String(windowHours),
    bucket_hours: String(bucketHours),
    include_fixtures: String(includeFixtures),
  });
  return api<AdminSourceDetail>(
    `/admin/v1/ingestion/sources/${encodeURIComponent(sourceKey)}?${query}`,
    { cache: "no-store" },
  );
}

export function getAdminSourceEvents(
  sourceKey: string,
  queryText: string,
  cursor: AdminCatalogEventCursor | null,
  limit = 20,
): Promise<AdminCatalogEventPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  setOptional(query, "q", queryText.trim());
  if (cursor) {
    query.set("after_start_at", cursor.startAt);
    query.set("after_canonical_event_id", cursor.canonicalEventId);
  }
  return api<AdminCatalogEventPage>(
    `/admin/v1/ingestion/sources/${encodeURIComponent(sourceKey)}/events?${query}`,
    { cache: "no-store" },
  );
}

export function updateAdminSourceConfiguration(
  sourceKey: string,
  input: AdminSourceConfigurationInput,
): Promise<AdminSourceConfigurationUpdate> {
  return api<AdminSourceConfigurationUpdate>(
    `/admin/v1/ingestion/sources/${encodeURIComponent(sourceKey)}`,
    {
      method: "PATCH",
      cache: "no-store",
      bodyJson: input,
    },
  );
}

export async function setAdminSourceEnabled(
  sourceKey: string,
  enabled: boolean,
  includeFixtures = false,
): Promise<AdminSourceConfigurationUpdate> {
  const { source } = await getAdminSourceDetail(sourceKey, 24, includeFixtures);
  return updateAdminSourceConfiguration(sourceKey, {
    expected_revision: source.source_revision,
    seed_url: source.seed_url,
    approved_origins: source.approved_origins,
    mode: source.mode,
    enabled,
    handoff_only: true,
    review_expires_at: source.review_expires_at,
    refresh_interval_minutes: source.refresh_interval_minutes,
    min_interval_ms: source.min_interval_ms,
    page_limit: source.page_limit,
    review_acknowledged: true,
  });
}

export function setAdminSourcesEnabled(
  targets: AdminSourceEnabledTarget[],
  enabled: boolean,
): Promise<AdminSourceBulkEnabledUpdate> {
  return api<AdminSourceBulkEnabledUpdate>(
    "/admin/v1/ingestion/sources/bulk/enabled",
    {
      method: "PATCH",
      cache: "no-store",
      bodyJson: {
        targets,
        enabled,
        review_acknowledged: true,
      },
    },
  );
}

export function getAdminRuns(
  filters: AdminRunFilters,
  page: { limit?: number; offset?: number } = {},
): Promise<AdminRunPage> {
  const query = new URLSearchParams({
    window_hours: String(filters.windowHours),
    include_fixtures: String(filters.includeFixtures),
    limit: String(page.limit ?? 100),
    offset: String(page.offset ?? 0),
  });
  setOptional(query, "status", filters.status);
  setOptional(query, "source_key", filters.sourceKey);
  return api<AdminRunPage>(`/admin/v1/ingestion/runs?${query}`, {
    cache: "no-store",
  });
}

export async function getAllAdminRuns(
  filters: AdminRunFilters,
  maximum = 5_000,
): Promise<AdminRunPage> {
  const limit = 100;
  const first = await getAdminRuns(filters, { limit, offset: 0 });
  const items = [...first.items];
  const identities = new Set(items.map((run) => `${run.source_key}:${run.run_key}`));
  let expectedTotal = first.total;

  for (let offset = limit; offset < expectedTotal && offset < maximum; offset += limit) {
    const next = await getAdminRuns(filters, { limit, offset });
    expectedTotal = Math.max(expectedTotal, next.total);
    for (const run of next.items) {
      const identity = `${run.source_key}:${run.run_key}`;
      if (!identities.has(identity)) {
        identities.add(identity);
        items.push(run);
      }
    }
    if (!next.items.length) break;
  }

  return {
    ...first,
    items,
    total: expectedTotal,
    limit: items.length,
    offset: 0,
  };
}

export function getAdminCommands(): Promise<AdminCommandList> {
  return api<AdminCommandList>("/admin/v1/ingestion/commands?limit=100", {
    cache: "no-store",
  });
}

export function getAdminCommandDetail(
  commandId: string,
  signal?: AbortSignal,
): Promise<AdminCommandDetail> {
  return api<AdminCommandDetail>(
    `/admin/v1/ingestion/commands/${encodeURIComponent(commandId)}`,
    { cache: "no-store", signal },
  );
}

export function enqueueAdminCommand(
  action: "refresh_source" | "refresh_due",
  sourceKey: string | null,
): Promise<AdminCommand> {
  return api<AdminCommand>("/admin/v1/ingestion/commands", {
    method: "POST",
    cache: "no-store",
    bodyJson: {
      command_id: crypto.randomUUID(),
      action,
      source_key: sourceKey,
    },
  });
}
