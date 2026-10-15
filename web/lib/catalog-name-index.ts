import type { CatalogNameIndex } from "./types.ts";

/** Use a server-computed remaining duration and a monotonic client clock, never wall-clock skew. */
export function catalogNameIndexExpiry(index: CatalogNameIndex, startedAt: number): number {
  const remaining = index.fresh_for_ms;
  return typeof remaining === "number" && Number.isFinite(remaining) && remaining >= 0
    ? startedAt + Math.min(remaining, 10_000) : 0;
}
