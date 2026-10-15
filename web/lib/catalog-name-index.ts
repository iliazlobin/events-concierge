import type { CatalogNameIndex } from "./types.ts";

/** A server-cache hit or slow preload must not extend the index's original lifetime. */
export function catalogNameIndexExpiry(index: CatalogNameIndex, startedAt: number): number {
  const expires = Date.parse(index.expires_at);
  return Number.isFinite(expires) ? Math.min(expires, startedAt + 10_000) : 0;
}
