import type { AdminSource } from "./admin-types.ts";
import { adminSourceIsRetired } from "./admin-source-lifecycle.ts";

export type SourceCollectionEligibility = "eligible" | "paused" | "retired" | "blocked" | "unknown";

/** Scheduling eligibility, not a health verdict or a promise that a worker will start. */
export function sourceCollectionEligibility(source: AdminSource): SourceCollectionEligibility {
  if (adminSourceIsRetired(source)) return "retired";
  if (!source.enabled || source.effective_status === "disabled") return "paused";
  if (source.review_status !== "reviewed"
    || ["unreviewed", "review_expired", "policy_blocked"].includes(source.effective_status)) return "blocked";
  return ["active", "due", "running"].includes(source.effective_status) ? "eligible" : "unknown";
}

export interface SourceCollectionDate {
  sourceKey: string;
  sourceName: string;
  at: string;
}

export interface SourceCollectionSummary {
  state: "available" | "excluded" | "unknown";
  selected: AdminSource | null;
  eligibility: SourceCollectionEligibility | null;
  eligibleCount: number | null;
  dueCount: number | null;
  collectingCount: number | null;
  awaitingSuccessCount: number | null;
  oldestSuccess: SourceCollectionDate | null;
  selectedLastSuccessAt: string | null;
  successDatesUnknown: boolean;
  nextEligibility: SourceCollectionDate[];
}

const finiteDate = (value: string | null): number | null => {
  if (!value) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
};

const collecting = (source: AdminSource): boolean => source.effective_status === "running"
  || source.latest_run?.status === "running";

/** The caller supplies a complete registry snapshot; null never becomes an empty registry. */
export function summarizeSourceCollection(
  sources: readonly AdminSource[] | null,
  sourceKey: string,
  now: number,
): SourceCollectionSummary {
  const unknown: SourceCollectionSummary = {
    state: "unknown", selected: null, eligibility: null, eligibleCount: null,
    dueCount: null, collectingCount: null, awaitingSuccessCount: null,
    oldestSuccess: null, selectedLastSuccessAt: null, successDatesUnknown: false, nextEligibility: [],
  };
  if (!sources || !Number.isFinite(now)) return unknown;
  const matches = sourceKey ? sources.filter(source => source.source_key === sourceKey) : sources;
  if (sourceKey && matches.length !== 1) return unknown;
  const selected = sourceKey ? matches[0] : null;
  const eligibility = selected ? sourceCollectionEligibility(selected) : null;
  const selectedSuccessTime = finiteDate(selected?.last_succeeded_at ?? null);
  const selectedLastSuccessAt = selectedSuccessTime !== null && selectedSuccessTime <= now
    ? selected!.last_succeeded_at : null;
  if (selected && eligibility !== "eligible") {
    return {
      ...unknown, state: eligibility === "unknown" ? "unknown" : "excluded", selected, eligibility,
      selectedLastSuccessAt,
      successDatesUnknown: selected.last_succeeded_at !== null && selectedLastSuccessAt === null,
    };
  }
  const eligible = matches.filter(source => sourceCollectionEligibility(source) === "eligible");
  const successful = eligible.filter(source => source.last_succeeded_at !== null);
  const successDatesUnknown = successful.some(source => {
    const timestamp = finiteDate(source.last_succeeded_at);
    return timestamp === null || timestamp > now;
  });
  const dated = (source: AdminSource, at: string): SourceCollectionDate => ({
    sourceKey: source.source_key, sourceName: source.display_name, at,
  });
  const dates = successful
    .filter(source => finiteDate(source.last_succeeded_at) !== null)
    .map(source => dated(source, source.last_succeeded_at!))
    .sort((left, right) => Date.parse(left.at) - Date.parse(right.at) || left.sourceKey.localeCompare(right.sourceKey));
  const nextEligibility = eligible
    .filter(source => !collecting(source) && !source.due && source.effective_status !== "due"
      && (finiteDate(source.next_due_at) ?? -Infinity) > now)
    .map(source => dated(source, source.next_due_at!))
    .sort((left, right) => Date.parse(left.at) - Date.parse(right.at) || left.sourceKey.localeCompare(right.sourceKey))
    .slice(0, 3);
  return {
    state: "available", selected, eligibility, eligibleCount: eligible.length,
    dueCount: eligible.filter(source => !collecting(source) && (source.due || source.effective_status === "due")).length,
    collectingCount: eligible.filter(collecting).length,
    awaitingSuccessCount: eligible.filter(source => source.last_succeeded_at === null).length,
    oldestSuccess: successDatesUnknown ? null : dates[0] ?? null,
    selectedLastSuccessAt,
    successDatesUnknown,
    nextEligibility,
  };
}

export function sourceCollectionTimestamp(value: string): string {
  const timestamp = finiteDate(value);
  return timestamp === null ? "Unknown" : new Date(timestamp).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
