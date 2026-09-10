import { api } from "@/lib/api";
import type { OperatorSession } from "@/lib/backend-operations";

export function getAdminOperatorSession(signal?: AbortSignal): Promise<OperatorSession> {
  return api<OperatorSession>("/admin/v1/operator/session", { cache: "no-store", signal });
}
import { collectRoster } from "./admin-source-roster.ts";
import type {
  AdminCatalogEventCursor,
  AdminCatalogEventPage,
  AdminCatalogListingPage,
  AdminCommand,
  AdminCommandDetail,
  AdminCommandInvestigation,
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
  AdminRun,
  AdminRunSort,
  AdminRunPage,
  AdminSourceDetail,
  AdminSourceConfigurationInput,
  AdminSourceConfigurationUpdate,
  AdminSourceBulkEnabledUpdate,
  AdminSourceEnabledTarget,
  AdminSourceFilters,
  AdminSourceHealthList,
  AdminSourceRegistrationHistory,
  AdminSourcePage,
} from "@/lib/admin-types";

function setOptional(query: URLSearchParams, key: string, value: string): void {
  if (value) query.set(key, value);
}

export function getAdminOverview(signal?: AbortSignal): Promise<AdminOverview> {
  return api<AdminOverview>("/admin/v1/ingestion/overview", { cache: "no-store", signal });
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
  signal?: AbortSignal,
): Promise<AdminFleetSummary> {
  const query = new URLSearchParams({
    window_hours: String(windowHours),
    include_fixtures: String(includeFixtures),
  });
  return api<AdminFleetSummary>(`/admin/v1/ingestion/summary?${query}`, {
    cache: "no-store", signal,
  });
}

export function getAdminStageSummary(
  windowHours: number,
  includeFixtures = false,
  signal?: AbortSignal,
): Promise<AdminStageSummary> {
  const query = new URLSearchParams({
    window_hours: String(windowHours),
    include_fixtures: String(includeFixtures),
  });
  return api<AdminStageSummary>(`/admin/v1/ingestion/stages?${query}`, {
    cache: "no-store", signal,
  });
}

export function getAdminSourceHealth(
  includeFixtures = false,
  signal?: AbortSignal,
): Promise<AdminSourceHealthList> {
  const query = new URLSearchParams({ include_fixtures: String(includeFixtures) });
  return api<AdminSourceHealthList>(`/admin/v1/ingestion/source-health?${query}`, {
    cache: "no-store", signal,
  });
}

export function getAdminSourceRegistrationHistory(
  windowDays: 7 | 30 | 90,
  includeFixtures: boolean,
  signal?: AbortSignal,
): Promise<AdminSourceRegistrationHistory> {
  const query = new URLSearchParams({ window_days: String(windowDays), include_fixtures: String(includeFixtures) });
  return api<AdminSourceRegistrationHistory>(`/admin/v1/ingestion/source-registration-history?${query}`, {
    cache: "no-store", signal,
  });
}

export function getAdminFleetShape(signal?: AbortSignal): Promise<AdminFleetShape> {
  return api<AdminFleetShape>("/admin/v1/ingestion/shape", { cache: "no-store", signal });
}

export function getAdminThroughput(
  windowHours: number,
  bucketHours: number,
  signal?: AbortSignal,
): Promise<AdminThroughput> {
  const query = new URLSearchParams({
    window_hours: String(windowHours),
    bucket_hours: String(bucketHours),
  });
  return api<AdminThroughput>(`/admin/v1/ingestion/throughput?${query}`, {
    cache: "no-store", signal,
  });
}

export function getAdminConcentration(limit = 15, signal?: AbortSignal): Promise<AdminConcentration> {
  const query = new URLSearchParams({ limit: String(limit) });
  return api<AdminConcentration>(`/admin/v1/ingestion/concentration?${query}`, {
    cache: "no-store", signal,
  });
}

export function getAdminCatalogFreshness(signal?: AbortSignal): Promise<AdminCatalogFreshness> {
  return api<AdminCatalogFreshness>("/admin/v1/ingestion/catalog-freshness", {
    cache: "no-store", signal,
  });
}

export function getAdminFilters(
  filters: AdminSourceFilters,
  signal?: AbortSignal,
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
    cache: "no-store", signal,
  });
}

/** The API's own per-page maximum for the source projection. */
const ADMIN_SOURCE_PAGE_LIMIT = 100;

export function getAdminSources(
  filters: AdminSourceFilters,
  page: { offset?: number } = {},
  signal?: AbortSignal,
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
    cache: "no-store", signal,
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
  options: {
    signal?: AbortSignal;
    onProgress?: (progress: { page: AdminSourcePage; truncated: boolean }) => void;
  } = {},
): Promise<{ page: AdminSourcePage; truncated: boolean }> {
  // `collectRoster` always fetches offset 0 first, so this is assigned before it resolves.
  let first: AdminSourcePage | undefined;

  const collected = await collectRoster({
    fetchPage: async (offset) => {
      const page = await getAdminSources(filters, { offset }, options.signal);
      first ??= page;
      return page;
    },
    identity: (source) => source.source_key,
    ceiling: ADMIN_SOURCE_ROSTER_CEILING,
    maxRequests: Math.ceil(ADMIN_SOURCE_ROSTER_CEILING / ADMIN_SOURCE_PAGE_LIMIT),
    signal: options.signal,
    onProgress: (progress) => {
      if (first) options.onProgress?.({ page: { ...first, items: progress.items }, truncated: progress.truncated });
    },
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
  signal?: AbortSignal,
  historyBucketHours?: number,
): Promise<AdminSourceDetail> {
  const bucketHours = historyBucketHours ?? (windowHours === 24 ? 4 : 24);
  const query = new URLSearchParams({
    window_hours: String(windowHours),
    bucket_hours: String(bucketHours),
    include_fixtures: String(includeFixtures),
  });
  return api<AdminSourceDetail>(
    `/admin/v1/ingestion/sources/${encodeURIComponent(sourceKey)}?${query}`,
    { cache: "no-store", signal },
  );
}

export function getAdminSourceEvents(
  sourceKey: string,
  queryText: string,
  cursor: AdminCatalogEventCursor | null,
  limit = 20,
  signal?: AbortSignal,
  runKey?: string,
): Promise<AdminCatalogEventPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  setOptional(query, "q", queryText.trim());
  if (runKey) query.set("run_key", runKey);
  if (cursor) {
    query.set("after_start_at", cursor.startAt);
    query.set("after_canonical_event_id", cursor.canonicalEventId);
  }
  return api<AdminCatalogEventPage>(
    `/admin/v1/ingestion/sources/${encodeURIComponent(sourceKey)}/events?${query}`,
    { cache: "no-store", signal },
  );
}

export function getAdminCatalogEvents(
  sourceKey: string,
  queryText: string,
  cursor: AdminCatalogEventCursor | null,
  dateScope: AdminCatalogListingPage["date_scope"] = "all",
  priceScope: AdminCatalogListingPage["price_status"] = "all",
  limit = 20,
  signal?: AbortSignal,
  runKey?: string,
): Promise<AdminCatalogListingPage> {
  const query = new URLSearchParams({ limit: String(limit), date_scope: dateScope, price_status: priceScope });
  setOptional(query, "q", queryText.trim());
  setOptional(query, "source_key", sourceKey);
  if (runKey) query.set("run_key", runKey);
  if (cursor) {
    query.set("after_start_at", cursor.startAt);
    query.set("after_canonical_event_id", cursor.canonicalEventId);
  }
  return api<AdminCatalogListingPage>(`/admin/v1/ingestion/events?${query}`, { cache: "no-store", signal });
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
  expectedRevision: number,
): Promise<AdminSourceConfigurationUpdate> {
  const result = await setAdminSourcesEnabled([
    { source_key: sourceKey, expected_revision: expectedRevision },
  ], enabled);
  const updated = result.items.find((item) => item.source_key === sourceKey);
  if (!updated) throw new Error("Source update receipt is unavailable; reload the source before retrying.");
  return updated;
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
  page: { limit?: number; offset?: number; query?: string; sortBy?: AdminRunSort; sortDirection?: "asc" | "desc" } = {},
  signal?: AbortSignal,
): Promise<AdminRunPage> {
  const query = new URLSearchParams({
    window_hours: String(filters.windowHours),
    include_fixtures: String(filters.includeFixtures),
    limit: String(page.limit ?? 100),
    offset: String(page.offset ?? 0),
  });
  setOptional(query, "status", filters.status);
  setOptional(query, "source_key", filters.sourceKey);
  setOptional(query, "query", page.query?.trim() ?? "");
  setOptional(query, "sort_by", page.sortBy ?? "");
  setOptional(query, "sort_direction", page.sortDirection ?? "");
  setOptional(query, "started_after", filters.startedAfter ?? "");
  setOptional(query, "started_before", filters.startedBefore ?? "");
  setOptional(query, "stage", filters.stage ?? "");
  setOptional(query, "stage_outcome", filters.stageOutcome ?? "");
  return api<AdminRunPage>(`/admin/v1/ingestion/runs?${query}`, {
    cache: "no-store", signal,
  });
}

/** Read one exact run regardless of the current ledger page or time window. */
export function getAdminRun(
  sourceKey: string,
  runKey: string,
  includeFixtures = false,
  signal?: AbortSignal,
): Promise<AdminRun> {
  const query = new URLSearchParams({ source_key: sourceKey, run_key: runKey, include_fixtures: String(includeFixtures) });
  return api<AdminRun>(`/admin/v1/ingestion/runs/lookup?${query}`, { cache: "no-store", signal });
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

export function getAdminCommands(signal?: AbortSignal): Promise<AdminCommandList> {
  return api<AdminCommandList>("/admin/v1/ingestion/commands?limit=100", {
    cache: "no-store", signal,
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

export function getAdminCommandInvestigation(
  commandId: string,
  afterEventId?: string | null,
  signal?: AbortSignal,
  sourceKey?: string,
): Promise<AdminCommandInvestigation> {
  const query = new URLSearchParams({ limit: "100" });
  if (afterEventId) query.set("after_event_id", afterEventId);
  if (sourceKey) query.set("source_key", sourceKey);
  return api<AdminCommandInvestigation>(
    `/admin/v1/ingestion/commands/${encodeURIComponent(commandId)}/investigation?${query}`,
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
