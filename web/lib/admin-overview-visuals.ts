import type { AdminSourceHealth, AdminSourceHealthList } from "./admin-types.ts";

export const freshnessGroups = [
  { id: "fresh", label: "Fresh" },
  { id: "watch", label: "Watch" },
  { id: "overdue", label: "Overdue" },
  { id: "never", label: "Never succeeded" },
] as const;
export type OverviewFreshnessGroup = typeof freshnessGroups[number]["id"];

export function overviewFreshnessGroup(source: AdminSourceHealth): OverviewFreshnessGroup {
  if (source.freshness_state === "ok") return "fresh";
  if (source.freshness_state === "warn") return "watch";
  if (source.freshness_state === "never") return "never";
  return "overdue";
}

/** Source counts describe one current snapshot; they are not a success rate or history. */
export function overviewSourceVisuals(snapshot: AdminSourceHealthList | null) {
  if (!snapshot) return null;
  const scheduled = snapshot.sources.filter(source => source.enabled && !source.retired_at && source.freshness_state !== "not_scheduled");
  const grouped = freshnessGroups.map(group => ({
    ...group,
    sources: scheduled.filter(source => overviewFreshnessGroup(source) === group.id),
  }));
  const attention = scheduled.filter(source => overviewFreshnessGroup(source) !== "fresh").sort((left, right) => {
    const priority = { never: 0, overdue: 1, watch: 2, fresh: 3 };
    return priority[overviewFreshnessGroup(left)] - priority[overviewFreshnessGroup(right)]
      || (right.hours_since_success ?? 0) - (left.hours_since_success ?? 0)
      || left.source_key.localeCompare(right.source_key);
  });
  const withEvents = snapshot.sources.filter(source => source.upcoming_events > 0);
  const largestSources = [...withEvents].sort((left, right) => right.upcoming_events - left.upcoming_events
    || left.source_key.localeCompare(right.source_key)).slice(0, 4);
  return {
    scheduled, grouped, attention,
    fresh: grouped[0].sources.length,
    excluded: snapshot.sources.length - scheduled.length,
    totalSources: snapshot.sources.length,
    withEvents: withEvents.length,
    withoutEvents: snapshot.sources.length - withEvents.length,
    largestSources,
    // Source associations can overlap; never sum them into the unique catalog total.
    largestSourceEvents: largestSources[0]?.upcoming_events ?? 0,
  };
}
