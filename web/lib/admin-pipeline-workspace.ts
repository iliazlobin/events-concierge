import type { AdminRunFilters, AdminSourceHealth } from "./admin-types.ts";
import { sourceNeedsAttention } from "./system-operations.ts";

/** Pipeline projections use the selected rolling window and all run outcomes. */
export function pipelineRunScope(filters: AdminRunFilters): AdminRunFilters {
  return { ...filters, startedAfter: undefined, startedBefore: undefined, stage: undefined, stageOutcome: undefined };
}

export function pipelineBucketScope(
  filters: AdminRunFilters,
  start: string,
  bucketHours: number,
  generatedAt: string,
  sourceScope: boolean,
  failedOnly = false,
): AdminRunFilters | null {
  const after = Date.parse(start);
  const snapshot = Date.parse(generatedAt);
  const before = Math.min(after + bucketHours * 3_600_000, snapshot);
  if (!Number.isFinite(after) || !Number.isFinite(before) || bucketHours <= 0 || before <= after) return null;
  // Fleet SQL counts from its grid-aligned first bucket, before the rolling lower bound.
  return {
    ...pipelineRunScope(filters), status: failedOnly ? "failed" : "",
    includeFixtures: sourceScope ? filters.includeFixtures : false,
    startedAfter: new Date(after).toISOString(), startedBefore: new Date(before).toISOString(),
  };
}

/** Active failures first, then cadence/retry signals; retained coverage breaks ties. */
export function pipelineAttentionSources(sources: AdminSourceHealth[], sourceKey: string): AdminSourceHealth[] {
  const rank = (source: AdminSourceHealth): number => {
    if (!sourceNeedsAttention(source)) return 4;
    if (source.run_state === "failed") return 0;
    if (source.freshness_state === "down" || source.retry_state === "severe") return 1;
    if (source.freshness_state === "never" || source.run_state === "never_run") return 2;
    return 3;
  };
  return sources.filter((source) => !sourceKey || source.source_key === sourceKey)
    .sort((left, right) => rank(left) - rank(right)
      || right.upcoming_events - left.upcoming_events
      || left.display_name.localeCompare(right.display_name)
      || left.source_key.localeCompare(right.source_key));
}
