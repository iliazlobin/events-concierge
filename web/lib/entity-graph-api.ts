import { api } from "./api.ts";
import type {
  CatalogEntityDirectory,
  CatalogEntityGraph,
  CatalogEntityIdentityStatus,
} from "./entity-graph.ts";
import type { CatalogEntityKind, EventItem } from "./types.ts";

export function getCatalogGraphEvent(
  tenantId: string | null,
  canonicalEventId: string,
  signal?: AbortSignal,
): Promise<EventItem> {
  return api<EventItem>(`/v1/catalog/events/${encodeURIComponent(canonicalEventId)}`, {
    tenantId,
    signal,
  });
}

/** Graph limits are supplied by the caller and included in its cache key. */
/** The caps the graph capability accepts. The SQL rejects anything outside 1–24 / 1–48 / 0–6. */
export interface CatalogEntityGraphLimits {
  events: number;
  peers: number;
  topics: number;
}

export function getCatalogEntityGraph(
  tenantId: string | null,
  entityId: string,
  limits: CatalogEntityGraphLimits,
  signal?: AbortSignal,
): Promise<CatalogEntityGraph> {
  const query = new URLSearchParams({
    events: String(limits.events),
    peers: String(limits.peers),
    topics: String(limits.topics),
  });
  return api<CatalogEntityGraph>(
    `/v1/catalog/entities/${encodeURIComponent(entityId)}/graph?${query}`,
    { tenantId, signal },
  );
}

export function getCatalogEntityDirectory(
  tenantId: string | null,
  queryValue: string,
  kinds: readonly CatalogEntityKind[],
  cities: readonly string[],
  limit: number,
  minEvents: number,
  signal?: AbortSignal,
): Promise<CatalogEntityDirectory> {
  const query = new URLSearchParams({
    limit: String(limit),
    min_events: String(minEvents),
  });
  if (queryValue.trim()) query.set("q", queryValue.trim());
  for (const kind of kinds) query.append("kind", kind);
  // The capability rejects a ninth city outright; slicing keeps a stray extra chip from turning
  // into a 4xx the reader cannot explain.
  for (const city of cities.slice(0, 8)) query.append("city", city);
  return api<CatalogEntityDirectory>(`/v1/catalog/entity-directory?${query}`, {
    tenantId,
    signal,
  });
}

/**
 * "Any identity", or one of the two statuses the catalog records.
 *
 * The overview endpoint takes this as a parameter, unlike the directory, whose
 * identity chips narrow an already-ranked slice in the browser. That difference
 * is the whole reason the counts beside the two chip rows are sourced
 * differently - see `entity-filter-controls.tsx`.
 */
export type CatalogEntityIdentityFilter = "all" | CatalogEntityIdentityStatus;

/** The caps the overview capability accepts, both halves of its cache key. */
export interface CatalogEntityOverviewLimits {
  /** Top hubs to draw, ranked by event count. */
  entities: number;
  /** Connected pairs to draw, each as ONE representative bridge event. */
  pairs: number;
}

/**
 * The landing graph: the top hubs and how they interconnect.
 *
 * Same payload shape as the ego graph - `focus_id` is the literal string
 * `"overview"`, which names no node - so `normalizeCatalogEntityGraph` and
 * `deriveEntityGraphScene` are reused without a branch. Entities arrive at ring
 * 0 and bridge events at ring 1; entities in no drawn pair still arrive, as
 * isolated nodes, because they are top hubs whether or not they share an event.
 *
 * Every filter this takes is applied server-side, inside the one round trip.
 * There is deliberately no second request to narrow the result in the browser:
 * the pool is five connections with no overflow, and a chip is a keystroke away
 * from being pressed again.
 */
export function getCatalogEntityOverviewGraph(
  tenantId: string | null,
  queryValue: string,
  kinds: readonly CatalogEntityKind[],
  identity: CatalogEntityIdentityFilter,
  limits: CatalogEntityOverviewLimits,
  signal?: AbortSignal,
): Promise<CatalogEntityGraph> {
  const query = new URLSearchParams({
    entities: String(limits.entities),
    pairs: String(limits.pairs),
  });
  if (queryValue.trim()) query.set("q", queryValue.trim());
  for (const kind of kinds) query.append("kind", kind);
  // "all" is the absence of the filter, not a third value the server knows.
  if (identity !== "all") query.set("identity", identity);
  return api<CatalogEntityGraph>(`/v1/catalog/entity-overview-graph?${query}`, {
    tenantId,
    signal,
  });
}
