import type { AdminSource } from "./admin-types.ts";
import { sourceCollectionEligibility, type SourceCollectionEligibility } from "./admin-source-operations.ts";

export type SourceScheduleHorizon = 24 | 48 | 168;

export interface SourceScheduleEntry {
  source: AdminSource;
  /** Recorded eligibility instant, never a reserved worker start time. */
  eligibleAt: string | null;
}

export interface SourceScheduleBucket {
  startAt: string;
  endAt: string;
  count: number;
  sources: SourceScheduleEntry[];
}

type ExcludedEligibility = Exclude<SourceCollectionEligibility, "eligible">;

export interface SourceSchedule {
  state: "available" | "excluded" | "unknown";
  selected: AdminSource | null;
  eligibility: SourceCollectionEligibility | null;
  due: SourceScheduleEntry[];
  running: SourceScheduleEntry[];
  upcoming: SourceScheduleEntry[];
  undated: SourceScheduleEntry[];
  eligibleCount: number | null;
  laterCount: number | null;
  excludedCount: number | null;
  excludedByReason: Record<ExcludedEligibility, number>;
  horizonEndsAt: string | null;
  buckets: SourceScheduleBucket[];
}

const HOUR = 60 * 60 * 1000;
const bucketHours: Record<SourceScheduleHorizon, number> = { 24: 1, 48: 2, 168: 12 };

function finiteDate(value: string | null): number | null {
  if (!value) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function byEligibility(left: SourceScheduleEntry, right: SourceScheduleEntry): number {
  const leftTime = finiteDate(left.eligibleAt);
  const rightTime = finiteDate(right.eligibleAt);
  if (leftTime === null && rightTime !== null) return 1;
  if (leftTime !== null && rightTime === null) return -1;
  return (leftTime !== null && rightTime !== null ? leftTime - rightTime : 0)
    || left.source.source_key.localeCompare(right.source.source_key);
}

/**
 * Project each eligible source's next recorded eligibility once, from a complete
 * registry snapshot. This does not invent scheduler batches or repeat cadences.
 * Buckets cover (now, horizonEndsAt], with the final endpoint included.
 */
export function projectSourceSchedule(
  sources: readonly AdminSource[] | null,
  sourceKey: string,
  now: number,
  horizonHours: SourceScheduleHorizon,
): SourceSchedule {
  const result: SourceSchedule = {
    state: "unknown", selected: null, eligibility: null,
    due: [], running: [], upcoming: [], undated: [],
    eligibleCount: null, laterCount: null, excludedCount: null,
    excludedByReason: { paused: 0, retired: 0, blocked: 0, unknown: 0 },
    horizonEndsAt: null, buckets: [],
  };
  const horizonEnd = now + horizonHours * HOUR;
  if (!sources || !Object.hasOwn(bucketHours, horizonHours)
    || !Number.isFinite(new Date(now).getTime())
    || !Number.isFinite(new Date(horizonEnd).getTime())) return result;

  const matches = sourceKey ? sources.filter(source => source.source_key === sourceKey) : sources;
  if (sourceKey && matches.length !== 1) return result;
  result.selected = sourceKey ? matches[0] : null;
  result.eligibility = result.selected ? sourceCollectionEligibility(result.selected) : null;
  result.state = result.eligibility === "unknown" ? "unknown"
    : result.eligibility && result.eligibility !== "eligible" ? "excluded" : "available";
  result.eligibleCount = 0;
  result.laterCount = 0;
  result.excludedCount = 0;
  result.horizonEndsAt = new Date(horizonEnd).toISOString();

  const future: SourceScheduleEntry[] = [];
  for (const source of matches) {
    const eligibility = sourceCollectionEligibility(source);
    if (eligibility !== "eligible") {
      result.excludedByReason[eligibility] += 1;
      result.excludedCount += 1;
      continue;
    }
    result.eligibleCount += 1;
    const timestamp = finiteDate(source.next_due_at);
    const entry: SourceScheduleEntry = { source, eligibleAt: timestamp === null ? null : source.next_due_at };
    if (source.effective_status === "running" || source.latest_run?.status === "running") {
      result.running.push(entry);
    } else if (source.due || source.effective_status === "due" || (timestamp !== null && timestamp <= now)) {
      // A due flag can be newer than the recorded date. Do not display a future
      // eligibility date as the time this already-due source became eligible.
      result.due.push({ source, eligibleAt: timestamp !== null && timestamp <= now ? source.next_due_at : null });
    } else if (timestamp !== null) {
      future.push(entry);
    } else {
      result.undated.push(entry);
    }
  }
  result.due.sort(byEligibility);
  result.running.sort(byEligibility);
  result.undated.sort(byEligibility);
  future.sort(byEligibility);
  result.upcoming = future.filter(entry => Date.parse(entry.eligibleAt!) <= horizonEnd);
  result.laterCount = future.length - result.upcoming.length;

  if (result.state !== "available") return result;
  const width = bucketHours[horizonHours] * HOUR;
  result.buckets = Array.from({ length: horizonHours / bucketHours[horizonHours] }, (_, index) => ({
    startAt: new Date(now + index * width).toISOString(),
    endAt: new Date(Math.min(now + (index + 1) * width, horizonEnd)).toISOString(),
    count: 0,
    sources: [],
  }));
  for (const entry of result.upcoming) {
    const index = Math.min(Math.floor((Date.parse(entry.eligibleAt!) - now) / width), result.buckets.length - 1);
    result.buckets[index].sources.push(entry);
    result.buckets[index].count += 1;
  }
  return result;
}
