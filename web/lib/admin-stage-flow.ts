import type {
  AdminFleetSummary,
  AdminStageSummary,
  AdminStageSummaryEntry,
} from "@/lib/admin-types";

/**
 * Derive the pipeline chain from server-computed rollups.
 *
 * The old pipeline panel reduced a client-paged run ledger and rendered five stage cards from two
 * distinct numbers — `candidateCount` twice and `canonicalCount` twice — while two of the five
 * stages have never written a metrics row at all. This module keeps the two facts separate:
 *
 *  - TIME comes from `catalog_refresh_run_stage_metrics` and exists for three stages only.
 *  - RECORDS exist at exactly two waypoints, `candidates` and `canonicals`. There is no
 *    per-stage record count in between, so none is invented.
 *
 * A stage folded into another boundary reports `evidenceStatus: "not_separately_instrumented"`
 * and renders as a labelled gap, never as a measured zero.
 */

export type StageEvidence =
  | "measured"
  | "not_separately_instrumented"
  | "not_observed";

export interface StageFlowStep {
  stage: string;
  position: number;
  label: string;
  evidenceStatus: StageEvidence;
  /** The boundary that absorbed this stage's work, when it is not separately timed. */
  foldedInto: string | null;
  /** Null whenever the stage is not separately instrumented — never zero. */
  totalMs: number | null;
  avgMs: number | null;
  p95Ms: number | null;
  pctOfWall: number | null;
  failedCount: number;
  runsWithEvidence: number;
  /** A record count only where the pipeline genuinely produces one. */
  recordCount: number | null;
  recordLabel: string | null;
}

export interface StageFlow {
  steps: StageFlowStep[];
  /** True when at least one declared stage can never be timed. */
  hasFoldedStages: boolean;
  /** Records entering and leaving the pipeline; the delta is dedupe, not loss. */
  candidates: number;
  canonicals: number;
  mergedByDedupe: number;
  yieldRate: number | null;
  windowHours: number;
}

const STAGE_LABELS: Record<string, string> = {
  admission: "Admission",
  collect: "Collect",
  extract_enrich: "Extract + enrich",
  normalize_dedupe: "Normalize + dedupe",
  catalog_publish: "Catalog + publish",
};

/** The two waypoints where a record count actually exists. */
const RECORD_WAYPOINTS: Record<string, "candidates" | "canonicals"> = {
  collect: "candidates",
  catalog_publish: "canonicals",
};

const RECORD_LABELS: Record<string, string> = {
  candidates: "candidates collected",
  canonicals: "canonicals published",
};

function labelFor(stage: string): string {
  return STAGE_LABELS[stage] ?? stage.replaceAll("_", " ");
}

function stepFrom(
  entry: AdminStageSummaryEntry,
  fleet: AdminFleetSummary | null,
): StageFlowStep {
  const folded = entry.evidence_status === "not_separately_instrumented";
  const waypoint = RECORD_WAYPOINTS[entry.stage];
  const recordCount = waypoint && fleet
    ? waypoint === "candidates" ? fleet.candidates : fleet.canonicals
    : null;

  return {
    stage: entry.stage,
    position: entry.stage_position,
    label: labelFor(entry.stage),
    evidenceStatus: entry.evidence_status,
    foldedInto: entry.folded_into,
    // A folded stage has no duration of its own. Reporting 0 here would assert it ran instantly.
    totalMs: folded ? null : entry.total_ms,
    avgMs: folded ? null : entry.avg_ms,
    p95Ms: folded ? null : entry.p95_ms,
    pctOfWall: folded ? null : entry.pct_of_wall,
    failedCount: entry.failed_count,
    runsWithEvidence: entry.runs_with_evidence,
    recordCount,
    recordLabel: waypoint ? RECORD_LABELS[waypoint] : null,
  };
}

export function buildStageFlow(
  stageSummary: AdminStageSummary,
  fleet: AdminFleetSummary | null,
): StageFlow {
  const steps = [...stageSummary.stages]
    .sort((left, right) => left.stage_position - right.stage_position)
    .map((entry) => stepFrom(entry, fleet));

  const candidates = fleet?.candidates ?? 0;
  const canonicals = fleet?.canonicals ?? 0;

  return {
    steps,
    hasFoldedStages: steps.some(
      (step) => step.evidenceStatus === "not_separately_instrumented",
    ),
    candidates,
    canonicals,
    // Dedupe is a many-to-one merge, so this is collapsed input, not dropped records.
    mergedByDedupe: Math.max(0, candidates - canonicals),
    yieldRate: candidates > 0 ? canonicals / candidates : null,
    windowHours: stageSummary.window_hours,
  };
}

/**
 * Why a panel has nothing to draw. `no_runs` and `unavailable` must never render identically —
 * the first is a fact about the window, the second is a failure to load, and conflating them is
 * what let a broken paging loop present itself as "no runs match".
 */
export type StageFlowEmptyReason = "no_runs" | "unavailable" | null;

export function stageFlowEmptyReason(
  fleet: AdminFleetSummary | null,
  failed: boolean,
): StageFlowEmptyReason {
  if (failed) return "unavailable";
  if (fleet && fleet.runs === 0) return "no_runs";
  return null;
}
