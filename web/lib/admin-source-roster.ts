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
}

export async function collectRoster<T>(
  options: CollectRosterOptions<T>,
): Promise<RosterResult<T>> {
  const { fetchPage, identity, ceiling } = options;

  const first = await fetchPage(0);
  const items: T[] = [];
  const seen = new Set<string>();
  let requests = 1;

  const absorb = (page: RosterPage<T>): number => {
    let added = 0;
    for (const item of page.items) {
      const key = identity(item);
      if (seen.has(key)) continue;
      seen.add(key);
      items.push(item);
      added += 1;
    }
    return added;
  };

  absorb(first);
  const total = first.total;

  while (items.length < total && items.length < ceiling) {
    const next = await fetchPage(items.length);
    requests += 1;
    // A page that returns nothing ends collection; the shortfall is then reported, not hidden.
    if (!next.items.length) break;
    if (absorb(next) === 0) break;
  }

  return {
    items,
    total,
    truncated: items.length < total,
    requests,
  };
}
