import type { CatalogEntityDirectory, CatalogEntityGraph } from "./entity-graph.ts";
import type { CatalogEntityDetail, CatalogEntityKind, EventItem } from "./types.ts";

/**
 * A bounded, in-memory cache for the two entity-graph reads.
 *
 * `web/lib/catalog-cache.ts` is the obvious template and this file follows its
 * shape — a keyed entry, a TTL, an explicit bound, and one `clear` that
 * sign-out and account erasure call — with one deliberate difference: **nothing
 * here is written to `sessionStorage`.** That file draws the line itself:
 * *"counts only. Event records, which carry titles, venues, and links, are never
 * written to storage."* A graph bundle carries event titles, venues,
 * registration links and people's names, and a directory bundle carries names
 * and cities, so both stay in memory for the life of the document and cannot
 * survive to the next person using a shared machine.
 *
 * Why a cache at all, given the route is 5 ms: the reason is not the database.
 * Every authenticated GET also runs two uncached auth reads against a pool of
 * five connections with no overflow, so walking back to an entity already
 * visited is three round trips that answer with a payload already held. The
 * cache turns a back-navigation and a re-selected peer into a paint.
 *
 * It is also deliberately not a prefetch. There is no speculative fetch on
 * hover anywhere in this feature: a 32-peer ring under that connection budget
 * is exactly the pressure it cannot absorb, and the measured latency does not
 * make the risk worth taking.
 *
 * Eviction is strict least-recently-used, and a read counts as a use, so a hub
 * revisited while browsing its ring outlives entries only fetched once.
 */

/** Graph bundles held for the life of the page. Sized for a long browsing session. */
const MAX_GRAPH_ENTRIES = 24;
/**
 * Directory bundles. Far fewer, because the directory is one screen whose key
 * changes only with the query and the filter chips.
 */
const MAX_DIRECTORY_ENTRIES = 8;
/** The catalog refreshes continuously; past this an entry is re-read. */
const ENTRY_TTL_MS = 5 * 60 * 1000;

/**
 * NUL separates key components. No tenant id, uuid, query, kind or city name
 * can contain one, so no combination of components can forge another key.
 */
const KEY_SEPARATOR = "\u0000";

interface CacheEntry<T> {
  at: number;
  value: T;
}

/**
 * A least-recently-used map with a hard entry bound and a TTL.
 *
 * `Map` iterates in insertion order, so re-inserting on every hit makes the
 * first key the least recently used one and eviction a single `delete`. Both
 * the bound and the TTL exist so a session that visits hundreds of entities
 * cannot grow this without limit.
 */
export class BoundedLruCache<T> {
  readonly #entries = new Map<string, CacheEntry<T>>();
  readonly #maxEntries: number;
  readonly #ttlMs: number;

  constructor(maxEntries: number, ttlMs: number = ENTRY_TTL_MS) {
    this.#maxEntries = Math.max(1, Math.floor(maxEntries));
    this.#ttlMs = ttlMs;
  }

  get size(): number {
    return this.#entries.size;
  }

  /** Keys from least to most recently used. Exposed for tests and diagnostics. */
  keys(): string[] {
    return [...this.#entries.keys()];
  }

  get(key: string, now: number = Date.now()): T | null {
    const entry = this.#entries.get(key);
    if (!entry) return null;
    if (now - entry.at > this.#ttlMs) {
      this.#entries.delete(key);
      return null;
    }
    // Re-insert so the bound evicts by least-recent USE, not by insertion: a
    // hub returned to repeatedly must not be dropped for one visited once.
    this.#entries.delete(key);
    this.#entries.set(key, entry);
    return entry.value;
  }

  set(key: string, value: T, now: number = Date.now()): void {
    this.#entries.delete(key);
    this.#entries.set(key, { at: now, value });
    while (this.#entries.size > this.#maxEntries) {
      const oldest = this.#entries.keys().next();
      if (oldest.done) break;
      this.#entries.delete(oldest.value);
    }
  }

  delete(key: string): void {
    this.#entries.delete(key);
  }

  clear(): void {
    this.#entries.clear();
  }
}

/* ------------------------------------------------------------------ *
 * Keys.
 * ------------------------------------------------------------------ */

export interface EntityGraphRequest {
  tenantId: string | null;
  entityId: string;
  events: number;
  peers: number;
  topics: number;
}

/**
 * Every parameter that changes what the server returns takes part in the key,
 * and the tenant leads it, so one account can never read another's bundle.
 * The caps are in the key because a wider request is a different payload, not a
 * superset the narrower one can be sliced out of: the server ranks and
 * truncates before it aggregates.
 */
export function entityGraphCacheKey(request: EntityGraphRequest): string {
  return [
    request.tenantId ?? "anonymous",
    request.entityId,
    `${request.events}:${request.peers}:${request.topics}`,
  ].join(KEY_SEPARATOR);
}

export interface EntityDirectoryRequest {
  tenantId: string | null;
  query: string;
  kinds: readonly CatalogEntityKind[];
  cities: readonly string[];
  limit: number;
  minEvents: number;
}

/**
 * Filter arrays are sorted into the key, so selecting two chips in either order
 * is one cache entry rather than two. Trimming the query matches what the
 * request itself sends.
 */
export function entityDirectoryCacheKey(request: EntityDirectoryRequest): string {
  return [
    request.tenantId ?? "anonymous",
    request.query.trim(),
    [...request.kinds].sort().join(","),
    [...request.cities].sort().join(","),
    `${request.limit}:${request.minEvents}`,
  ].join(KEY_SEPARATOR);
}

export interface EntityOverviewRequest {
  tenantId: string | null;
  query: string;
  kinds: readonly CatalogEntityKind[];
  /** `"all"` or one of the two identity statuses; part of the request, so part of the key. */
  identity: string;
  entities: number;
  pairs: number;
}

/**
 * The landing graph's key.
 *
 * Same rules as the two above: the tenant leads it so one account can never
 * read another's bundle, the filters are sorted so two chips pressed in either
 * order are one entry, and both caps take part because a wider request is a
 * different payload rather than a superset — the server ranks the hubs and
 * samples one representative event per pair before it aggregates, so a 40-hub
 * answer is not a slice of a 60-hub one.
 */
export function entityOverviewCacheKey(request: EntityOverviewRequest): string {
  return [
    request.tenantId ?? "anonymous",
    request.query.trim(),
    [...request.kinds].sort().join(","),
    request.identity,
    `${request.entities}:${request.pairs}`,
  ].join(KEY_SEPARATOR);
}

/* ------------------------------------------------------------------ *
 * The caches.
 * ------------------------------------------------------------------ */

const graphCache = new BoundedLruCache<CatalogEntityGraph>(MAX_GRAPH_ENTRIES);
const directoryCache = new BoundedLruCache<CatalogEntityDirectory>(MAX_DIRECTORY_ENTRIES);
/**
 * Overview bundles. Sized like the directory rather than like the graph: this
 * is one screen whose key moves only with the chips and the search box, and a
 * reader who narrows, widens and narrows again should pay for it once.
 */
const overviewCache = new BoundedLruCache<CatalogEntityGraph>(MAX_DIRECTORY_ENTRIES);
const eventCache = new BoundedLruCache<EventItem>(48);

export function readGraphEvent(
  tenantId: string | null,
  canonicalEventId: string,
  now: number = Date.now(),
): EventItem | null {
  return eventCache.get(entityDetailKey(tenantId, canonicalEventId), now);
}

export function writeGraphEvent(
  tenantId: string | null,
  event: EventItem,
  now: number = Date.now(),
): void {
  eventCache.set(entityDetailKey(tenantId, event.canonical_event_id), event, now);
}

export function readEntityGraph(
  request: EntityGraphRequest,
  now: number = Date.now(),
): CatalogEntityGraph | null {
  return graphCache.get(entityGraphCacheKey(request), now);
}

export function writeEntityGraph(
  request: EntityGraphRequest,
  graph: CatalogEntityGraph,
  now: number = Date.now(),
): void {
  graphCache.set(entityGraphCacheKey(request), graph, now);
}

export function readEntityDirectory(
  request: EntityDirectoryRequest,
  now: number = Date.now(),
): CatalogEntityDirectory | null {
  return directoryCache.get(entityDirectoryCacheKey(request), now);
}

export function writeEntityDirectory(
  request: EntityDirectoryRequest,
  directory: CatalogEntityDirectory,
  now: number = Date.now(),
): void {
  directoryCache.set(entityDirectoryCacheKey(request), directory, now);
}

export function readEntityOverview(
  request: EntityOverviewRequest,
  now: number = Date.now(),
): CatalogEntityGraph | null {
  return overviewCache.get(entityOverviewCacheKey(request), now);
}

export function writeEntityOverview(
  request: EntityOverviewRequest,
  graph: CatalogEntityGraph,
  now: number = Date.now(),
): void {
  overviewCache.set(entityOverviewCacheKey(request), graph, now);
}

/**
 * The five-round-trip detail payload, cached for the inspector's "Profile & sources" tab.
 *
 * It lives here rather than beside its one consumer so that sign-out has a single symbol to call:
 * a detail payload carries the same names, venues and links the bundles above do, and a second
 * cache reachable only through a component module is a second thing to forget.
 */
const detailCache = new Map<string, CatalogEntityDetail>();

function entityDetailKey(tenantId: string | null, entityId: string): string {
  return [tenantId ?? "", entityId].join(KEY_SEPARATOR);
}

export function readEntityDetail(
  tenantId: string | null,
  entityId: string,
): CatalogEntityDetail | null {
  return detailCache.get(entityDetailKey(tenantId, entityId)) ?? null;
}

export function writeEntityDetail(
  tenantId: string | null,
  entityId: string,
  detail: CatalogEntityDetail,
): void {
  detailCache.set(entityDetailKey(tenantId, entityId), detail);
}

/**
 * Drop everything held, the detail payloads included.
 *
 * Sign-out and account erasure must call this alongside `clearCatalogCache`:
 * these bundles carry names, venues and links belonging to one account's view.
 */
export function clearEntityGraphCache(): void {
  graphCache.clear();
  directoryCache.clear();
  overviewCache.clear();
  detailCache.clear();
  eventCache.clear();
}

/** Entry counts, for tests and for the diagnostics panel. */
export function entityGraphCacheSizes(): {
  graphs: number;
  directories: number;
  overviews: number;
  details: number;
  events: number;
} {
  return {
    graphs: graphCache.size,
    directories: directoryCache.size,
    overviews: overviewCache.size,
    details: detailCache.size,
    events: eventCache.size,
  };
}
