/**
 * Roster collection, kept pure so it can be tested without a network or a path alias.
 *
 * The console previously requested a single page of 100 sources and presented the result as the
 * whole registry: the attention total, the action queue and every dropdown silently capped, with
 * nothing on screen saying so. A roster is a bounded set — every comparable console renders it in
 * full — so this pages to completion against the server's own `total`, and when it cannot, it
 * reports the shortfall instead of returning a short list that reads as complete.
 *
 * This is deliberately NOT the pattern for the run ledger. A ledger grows without bound and must
 * be aggregated in SQL; a roster is tens of rows and bounded by the registry.
 */

export interface RosterPage<T> {
  items: T[];
  total: number;
}

export interface RosterResult<T> {
  items: T[];
  total: number;
  /** True when fewer rows were collected than the server said exist. */
  truncated: boolean;
  requests: number;
}

export interface CollectRosterOptions<T> {
  /** Fetch one page starting at `offset`. */
  fetchPage: (offset: number) => Promise<RosterPage<T>>;
  /** Stable identity used to drop rows repeated across pages. */
  identity: (item: T) => string;
  /** Hard ceiling so a wrong `total` cannot spin forever. */
  ceiling: number;
  /** Bound network work independently of deduplicated row count. */
  maxRequests?: number;
  signal?: AbortSignal;
  /** Each callback receives its own array, including the first available page. */
  onProgress?: (progress: RosterResult<T>) => void;
}

function checkAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw signal.reason ?? new DOMException("The roster read was aborted", "AbortError");
}

/** Stop waiting even if a custom page reader does not cooperate with cancellation. */
function readPage<T>(options: CollectRosterOptions<T>, offset: number): Promise<RosterPage<T>> {
  const { signal } = options;
  checkAborted(signal);
  if (!signal) return options.fetchPage(offset);
  return new Promise((resolve, reject) => {
    const aborted = () => reject(signal.reason ?? new DOMException("The roster read was aborted", "AbortError"));
    signal.addEventListener("abort", aborted, { once: true });
    Promise.resolve().then(() => {
      checkAborted(signal);
      return options.fetchPage(offset);
    }).then(resolve, reject).finally(() => signal.removeEventListener("abort", aborted));
  });
}

export async function collectRoster<T>(
  options: CollectRosterOptions<T>,
): Promise<RosterResult<T>> {
  const { identity, ceiling, maxRequests = 100, signal, onProgress } = options;
  if (!Number.isInteger(ceiling) || ceiling < 1 || !Number.isInteger(maxRequests) || maxRequests < 1) {
    throw new RangeError("Roster row and request limits must be positive integers");
  }

  const first = await readPage(options, 0);
  checkAborted(signal);
  const items: T[] = [];
  const seen = new Set<string>();
  let requests = 1;

  const absorb = (page: RosterPage<T>): void => {
    for (const item of page.items) {
      if (items.length >= ceiling) break;
      const key = identity(item);
      if (seen.has(key)) continue;
      seen.add(key);
      items.push(item);
    }
  };

  const total = first.total;
  const snapshot = (): RosterResult<T> => ({ items: [...items], total, truncated: items.length < total, requests });
  absorb(first);
  onProgress?.(snapshot());
  let offset = first.items.length;

  while (offset > 0 && offset < total && items.length < total && items.length < ceiling && requests < maxRequests) {
    checkAborted(signal);
    const next = await readPage(options, offset);
    checkAborted(signal);
    requests += 1;
    // A page that returns nothing ends collection; the shortfall is then reported, not hidden.
    if (!next.items.length) break;
    // Offset belongs to the server's row stream; deduplication must not rewind it.
    offset += next.items.length;
    absorb(next);
    onProgress?.(snapshot());
  }

  checkAborted(signal);
  return snapshot();
}
