import type {
  AdminCommand,
  AdminRun,
  AdminRunCore,
  AdminSource,
  AdminSourceDetail,
  RunStatus,
  SourceStatus,
} from "@/lib/admin-types";
import {
  adminSourceIsRetired,
  adminSourceLifecycleLabel,
  adminSourceRetirementDescription,
} from "./admin-source-lifecycle.ts";

const numberFormat = new Intl.NumberFormat("en-US");
const compactNumberFormat = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});
const dateTimeFormat = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});
const fullDateFormat = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});

export function formatNumber(value: number): string {
  return numberFormat.format(value);
}

export function formatCompactNumber(value: number): string {
  return compactNumberFormat.format(value);
}

export function formatDateTime(value: string | null): string {
  if (!value) return "Never";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : dateTimeFormat.format(date);
}

export function formatFullDate(value: string | null): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : fullDateFormat.format(date);
}

export function formatRelativeTime(value: string | null): string {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  const deltaSeconds = Math.round((date.getTime() - Date.now()) / 1000);
  const absolute = Math.abs(deltaSeconds);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (absolute < 60) return formatter.format(deltaSeconds, "second");
  if (absolute < 3_600) return formatter.format(Math.round(deltaSeconds / 60), "minute");
  if (absolute < 86_400) return formatter.format(Math.round(deltaSeconds / 3_600), "hour");
  if (absolute < 2_592_000) return formatter.format(Math.round(deltaSeconds / 86_400), "day");
  return formatter.format(Math.round(deltaSeconds / 2_592_000), "month");
}

export function formatDuration(value: number | null): string {
  if (value === null) return "—";
  if (value < 1_000) return `${value} ms`;
  if (value < 60_000) return `${(value / 1_000).toFixed(value < 10_000 ? 1 : 0)} s`;
  return `${(value / 60_000).toFixed(1)} min`;
}

export function formatPercent(value: number | null): string {
  if (value === null) return "—";
  return `${Math.round(value * 100)}%`;
}

export function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

const COMMAND_REVIEW_RESULT_KEYS = ["deferred", "failed", "skipped"] as const;
const COMMAND_REVIEW_OUTCOMES = new Set(COMMAND_REVIEW_RESULT_KEYS);
const REFRESH_SOURCE_EVIDENCE_KEYS = [
  "action",
  "source_key",
  "outcome",
  "run_key",
  "candidate_count",
  "canonical_count",
  "retry_after_seconds",
] as const;
const REFRESH_DUE_EVIDENCE_KEYS = [
  "action",
  "due_sources",
  "attempted",
  "succeeded",
  "deferred",
  "failed",
  "skipped",
  "queued",
  "busy",
  "progressed",
  "already_succeeded",
  "retry_after_seconds",
] as const;
const COMMAND_OUTCOMES = new Set([
  "succeeded",
  "failed",
  "queued",
  "skipped",
  "busy",
  "already_succeeded",
  "deferred",
  "progressed",
]);
const RUN_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$/;

export type CommandEvidenceEntry = [
  string,
  string | number | boolean | null,
];

export type CommandOutcomeTone =
  | "success"
  | "warning"
  | "danger"
  | "active"
  | "neutral";

export interface CommandOutcomeMetric {
  key: string;
  label: string;
  value: string;
  tone: CommandOutcomeTone;
}

export interface CommandOutcomeSegment {
  key: string;
  label: string;
  value: number;
  tone: CommandOutcomeTone;
}

export interface CommandOutcomePresentation {
  tone: CommandOutcomeTone;
  headline: string;
  detail: string;
  progressPercent: number | null;
  metrics: CommandOutcomeMetric[];
  segments: CommandOutcomeSegment[];
}

export interface CommandOutcomePresentationContext {
  containedHistorical?: boolean;
}

export interface CommandExecutionFact {
  key: string;
  label: string;
  value: string;
}

export interface CommandExecutionPresentation {
  headline: string;
  detail: string;
  facts: CommandExecutionFact[];
}

export type CommandReviewDisposition = "none" | "current" | "contained";

function numericCommandResult(command: AdminCommand, key: string): number | null {
  const value = command.result?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function safeCommandResultValue(
  command: AdminCommand,
  key: string,
): string | number | boolean | null | undefined {
  const value = command.result?.[key];
  if (key === "action") return value === command.action ? value : undefined;
  if (key === "source_key") {
    return typeof value === "string" && value === command.source_key ? value : undefined;
  }
  if (key === "outcome") {
    return typeof value === "string" && COMMAND_OUTCOMES.has(value) ? value : undefined;
  }
  if (key === "run_key") {
    return typeof value === "string" && RUN_KEY_PATTERN.test(value) ? value : undefined;
  }
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined;
}

export function commandEvidence(command: AdminCommand): CommandEvidenceEntry[] {
  if (!command.result) return [];
  const keys = command.action === "refresh_source"
    ? REFRESH_SOURCE_EVIDENCE_KEYS
    : REFRESH_DUE_EVIDENCE_KEYS;
  return keys.flatMap((key) => {
    const value = safeCommandResultValue(command, key);
    return value === undefined ? [] : [[key, value] as CommandEvidenceEntry];
  });
}

export function commandNeedsReview(command: AdminCommand): boolean {
  if (command.status !== "completed" || !command.result) return false;
  const outcome = command.result.outcome;
  if (
    typeof outcome === "string"
    && COMMAND_REVIEW_OUTCOMES.has(outcome.toLowerCase() as typeof COMMAND_REVIEW_RESULT_KEYS[number])
  ) {
    return true;
  }
  return COMMAND_REVIEW_RESULT_KEYS.some((key) => (numericCommandResult(command, key) ?? 0) > 0);
}

function countLabel(value: number, singular: string, plural = `${singular}s`): string {
  return `${formatNumber(value)} ${value === 1 ? singular : plural}`;
}

function evidenceNumber(
  evidence: Map<string, CommandEvidenceEntry[1]>,
  key: string,
): number {
  const value = evidence.get(key);
  return typeof value === "number" ? value : 0;
}

function metric(
  key: string,
  label: string,
  value: number,
  tone: CommandOutcomeTone,
): CommandOutcomeMetric | null {
  return value > 0
    ? { key, label, value: formatNumber(value), tone }
    : null;
}

function compactMetrics(
  values: Array<CommandOutcomeMetric | null>,
): CommandOutcomeMetric[] {
  return values.filter((value): value is CommandOutcomeMetric => value !== null);
}

function rawCommandOutcomePresentation(
  command: AdminCommand,
): CommandOutcomePresentation {
  if (command.status === "queued") {
    return {
      tone: "neutral",
      headline: "Waiting for a worker",
      detail: "The command is safely queued and has not started yet.",
      progressPercent: null,
      metrics: [],
      segments: [],
    };
  }
  if (command.status === "running") {
    return {
      tone: "active",
      headline: command.action === "refresh_due"
        ? "Refreshing due sources"
        : "Refreshing source",
      detail: command.action === "refresh_due"
        ? "The worker is recalculating which reviewed sources are due, then invoking each eligible source through its normal guarded refresh path. Aggregate counts are recorded when this bounded pass finishes."
        : "The worker is collecting this source through its guarded refresh path. Candidate and catalog counts are recorded when it finishes.",
      progressPercent: null,
      metrics: [],
      segments: [],
    };
  }
  if (command.status === "failed" || command.error_code) {
    return {
      tone: "danger",
      headline: command.error_code ? humanize(command.error_code) : "Command failed",
      detail: "The worker did not confirm a successful catalog update.",
      progressPercent: null,
      metrics: [],
      segments: [],
    };
  }
  if (!command.result) {
    return {
      tone: "neutral",
      headline: "No result recorded",
      detail: "The command finished without a safe terminal receipt.",
      progressPercent: null,
      metrics: [],
      segments: [],
    };
  }

  const evidence = new Map(commandEvidence(command));
  if (command.action === "refresh_due") {
    const hasFleetEvidence = [
      "due_sources",
      "attempted",
      "succeeded",
      "failed",
      "deferred",
      "skipped",
      "queued",
      "busy",
      "progressed",
      "already_succeeded",
    ].some((key) => evidence.has(key));
    if (!hasFleetEvidence) {
      return {
        tone: "neutral",
        headline: "No bounded result recorded",
        detail: "The command completed without safe fleet outcome counts to display.",
        progressPercent: null,
        metrics: [],
        segments: [],
      };
    }
    const due = evidenceNumber(evidence, "due_sources");
    const attempted = evidenceNumber(evidence, "attempted");
    const succeeded = evidenceNumber(evidence, "succeeded");
    const failed = evidenceNumber(evidence, "failed");
    const deferred = evidenceNumber(evidence, "deferred");
    const skipped = evidenceNumber(evidence, "skipped");
    const queued = evidenceNumber(evidence, "queued");
    const busy = evidenceNumber(evidence, "busy");
    const alreadySucceeded = evidenceNumber(evidence, "already_succeeded");
    const progressed = evidenceNumber(evidence, "progressed");
    const completed = succeeded + alreadySucceeded;
    const attention = failed + deferred + skipped;
    const reported = completed + attention + queued + busy + progressed;
    const denominator = attempted || due || reported;
    const unreported = Math.max(0, attempted - reported);
    const progressPercent = denominator > 0
      ? Math.min(100, Math.round((completed / denominator) * 100))
      : null;
    const metrics = compactMetrics([
      metric("succeeded", "Refreshed", succeeded, "success"),
      metric("already_succeeded", "Already current", alreadySucceeded, "success"),
      metric("failed", "Failed", failed, "danger"),
      metric("deferred", "Deferred", deferred, "warning"),
      metric("skipped", "Skipped", skipped, "warning"),
      metric("queued", "Queued", queued, "active"),
      metric("busy", "Busy", busy, "active"),
      metric("progressed", "Progressed", progressed, "active"),
    ]);
    const segments = ([
      { key: "succeeded", label: "Refreshed", value: succeeded, tone: "success" },
      {
        key: "already_succeeded",
        label: "Already current",
        value: alreadySucceeded,
        tone: "success",
      },
      { key: "failed", label: "Failed", value: failed, tone: "danger" },
      { key: "deferred", label: "Deferred", value: deferred, tone: "warning" },
      { key: "skipped", label: "Skipped", value: skipped, tone: "warning" },
      { key: "queued", label: "Queued", value: queued, tone: "active" },
      { key: "progressed", label: "Continuing", value: progressed, tone: "active" },
      { key: "busy", label: "Busy", value: busy, tone: "active" },
      { key: "unreported", label: "Unreported", value: unreported, tone: "neutral" },
    ] satisfies CommandOutcomeSegment[]).filter((segment) => segment.value > 0);

    if (denominator === 0) {
      return {
        tone: "neutral",
        headline: "No sources were due",
        detail: "The cadence check found no eligible source that needed a refresh.",
        progressPercent: null,
        metrics: [],
        segments: [],
      };
    }
    if (attention > 0) {
      const attemptContext = due > 0 && attempted > 0 && due !== attempted
        ? `Attempted ${formatNumber(attempted)} of ${formatNumber(due)} due sources. `
        : "";
      return {
        tone: failed > 0 ? "danger" : "warning",
        headline: completed > 0
          ? `${formatNumber(completed)} of ${formatNumber(denominator)} sources refreshed`
          : `${countLabel(attention, "source")} need attention`,
        detail: `${attemptContext}${countLabel(attention, "source")} did not complete successfully. Review the highlighted outcome${attention === 1 ? "" : "s"} before retrying.`,
        progressPercent,
        metrics,
        segments,
      };
    }
    if (completed > 0) {
      return {
        tone: "success",
        headline: `${countLabel(completed, "source")} refreshed`,
        detail: completed === denominator && (due === 0 || due === attempted)
          ? "Every due source completed successfully."
          : due > 0 && attempted > 0 && due !== attempted
            ? `Attempted ${formatNumber(attempted)} of ${formatNumber(due)} due sources; every attempted source completed successfully.`
          : `${formatNumber(completed)} of ${formatNumber(denominator)} due sources completed; the remainder are still queued or busy.`,
        progressPercent,
        metrics,
        segments,
      };
    }
    return {
      tone: "active",
      headline: "Due sources are still in progress",
      detail: "No terminal source result has been recorded yet.",
      progressPercent,
      metrics,
      segments,
    };
  }

  const outcomeValue = evidence.get("outcome");
  const outcome = typeof outcomeValue === "string" ? outcomeValue : null;
  const candidates = evidenceNumber(evidence, "candidate_count");
  const canonical = evidenceNumber(evidence, "canonical_count");
  const retryAfter = evidenceNumber(evidence, "retry_after_seconds");
  const yieldPercent = candidates > 0
    ? Math.min(100, Math.round((canonical / candidates) * 100))
    : null;
  const yieldMetrics = compactMetrics([
    metric("candidate_count", "Candidates", candidates, "neutral"),
    metric("canonical_count", "Published", canonical, "success"),
    yieldPercent === null
      ? null
      : {
          key: "yield",
          label: "Catalog yield",
          value: `${yieldPercent}%`,
          tone: "success",
        },
  ]);
  const segments: CommandOutcomeSegment[] = candidates > 0
    ? ([
        {
          key: "canonical",
          label: "Published",
          value: Math.min(canonical, candidates),
          tone: "success",
        },
        {
          key: "filtered",
          label: "Filtered or merged",
          value: Math.max(0, candidates - canonical),
          tone: "neutral",
        },
      ] satisfies CommandOutcomeSegment[]).filter((segment) => segment.value > 0)
    : [];

  if (outcome === "deferred" || outcome === "busy") {
    return {
      tone: "warning",
      headline: outcome === "busy" ? "Source is already running" : "Source refresh deferred",
      detail: retryAfter > 0
        ? `The pacing guard recommends retrying in ${formatDuration(retryAfter * 1_000)}.`
        : "The source was protected by pacing or lease controls. It is safe to retry later.",
      progressPercent: null,
      metrics: retryAfter > 0
        ? [{
            key: "retry_after_seconds",
            label: "Retry in",
            value: formatDuration(retryAfter * 1_000),
            tone: "warning",
          }]
        : [],
      segments: [],
    };
  }
  if (outcome === "failed" || outcome === "skipped") {
    return {
      tone: outcome === "failed" ? "danger" : "warning",
      headline: outcome === "failed" ? "Source refresh failed" : "Source was skipped",
      detail: "No successful catalog update was confirmed for this command.",
      progressPercent: null,
      metrics: [],
      segments: [],
    };
  }
  if (outcome === "already_succeeded") {
    return {
      tone: "success",
      headline: "Source was already current",
      detail: "This command reused a successful result and did not repeat the provider request.",
      progressPercent: null,
      metrics: [],
      segments: [],
    };
  }
  if (outcome === "queued" || outcome === "progressed") {
    return {
      tone: "active",
      headline: "Source run is continuing",
      detail: "The command handed work to the source runner; a terminal catalog result is not available yet.",
      progressPercent: null,
      metrics: [],
      segments: [],
    };
  }
  if (outcome === "succeeded" && candidates > 0 && canonical === 0) {
    return {
      tone: "warning",
      headline: "No events reached the catalog",
      detail: `${countLabel(candidates, "candidate")} were found, but all were filtered or merged during normalization.`,
      progressPercent: 0,
      metrics: yieldMetrics,
      segments,
    };
  }
  if (canonical > 0 || outcome === "succeeded") {
    return {
      tone: "success",
      headline: canonical > 0
        ? `${countLabel(canonical, "event")} published`
        : "Source refreshed",
      detail: candidates > 0
        ? `${countLabel(candidates, "candidate")} produced ${countLabel(canonical, "catalog event")}.`
        : "The source completed successfully and returned no current events.",
      progressPercent: yieldPercent,
      metrics: yieldMetrics,
      segments,
    };
  }
  return {
    tone: "neutral",
    headline: "No bounded result recorded",
    detail: "The command completed without a safe source outcome to display.",
    progressPercent: null,
    metrics: [],
    segments: [],
  };
}

export function commandExecutionPresentation(
  command: AdminCommand,
): CommandExecutionPresentation | null {
  if (command.status !== "queued" && command.status !== "running") return null;

  if (command.action === "refresh_due") {
    return {
      headline: command.status === "running"
        ? "Bounded cadence pass in progress"
        : "Bounded cadence pass waiting for a worker",
      detail: command.status === "running"
        ? "The due-source set is read at execution time. Each selected source still passes review, enablement, policy, pacing, origin, and lease checks before collection."
        : "No provider work has started. The due-source set will be calculated after a worker claims this durable command.",
      facts: [
        {
          key: "scope",
          label: "Scope",
          value: command.status === "running"
            ? "Due sources calculated at worker execution"
            : "Calculated when a worker starts",
        },
        {
          key: "dispatch",
          label: "Dispatch",
          value: "Sequential guarded source refreshes",
        },
        {
          key: "evidence",
          label: "Evidence",
          value: "Source runs first; aggregate receipt on completion",
        },
      ],
    };
  }

  return {
    headline: command.status === "running"
      ? "Single-source refresh in progress"
      : "Single-source refresh waiting for a worker",
    detail: command.status === "running"
      ? "The selected source is executing through the same reviewed, policy-gated refresh boundary used by scheduled collection."
      : "No provider work has started. A worker must claim this durable command before the source refresh begins.",
    facts: [
      {
        key: "scope",
        label: "Scope",
        value: "One reviewed source",
      },
      {
        key: "dispatch",
        label: "Dispatch",
        value: "Guarded source refresh",
      },
      {
        key: "evidence",
        label: "Evidence",
        value: "Source run plus candidate and catalog counts",
      },
    ],
  };
}

export function commandOutcomePresentation(
  command: AdminCommand,
  context: CommandOutcomePresentationContext = {},
): CommandOutcomePresentation {
  const outcome = rawCommandOutcomePresentation(command);
  const hasTerminalAttention = command.status === "failed" || commandNeedsReview(command);
  if (!context.containedHistorical || !hasTerminalAttention) return outcome;
  return {
    ...outcome,
    tone: "neutral",
    headline: "Historical issue contained",
    detail: "This receipt retains the original unsuccessful outcome. Its implicated source is retired or no longer has a current enabled failure, so there is nothing to retry from this receipt.",
  };
}

export function commandResultSummary(
  command: AdminCommand,
  context: CommandOutcomePresentationContext = {},
): string {
  return commandOutcomePresentation(command, context).headline;
}

export type AdminRunView = "current" | "history";
export type AdminRunDisposition = "current" | "resolved" | "historical";

export interface PresentedAdminRun {
  run: AdminRun;
  disposition: AdminRunDisposition;
}

type ClassifiableAdminRun = Pick<AdminRunCore, "status" | "error">;

export function isPacerDeferredRun(
  run: ClassifiableAdminRun | null | undefined,
): boolean {
  return run?.status === "failed" && run.error === "pacer_deferred";
}

export function displayedRunStatus(run: ClassifiableAdminRun): RunStatus {
  return isPacerDeferredRun(run) ? "paused" : run.status;
}

export function runStatusLabel(run: ClassifiableAdminRun): string {
  return isPacerDeferredRun(run) ? "Deferred / paused" : humanize(run.status);
}

export function isFailedRun(run: ClassifiableAdminRun): boolean {
  return displayedRunStatus(run) === "failed";
}

export function commandReviewDisposition(
  command: AdminCommand,
  sources: AdminSource[],
): CommandReviewDisposition {
  const hasTerminalAttention = command.status === "failed" || commandNeedsReview(command);
  if (!hasTerminalAttention) return "none";

  const sourceHasCurrentEnabledFailure = (source: AdminSource): boolean => (
    source.enabled
    && !adminSourceIsRetired(source)
    && source.latest_run !== null
    && isFailedRun(source.latest_run)
  );

  if (command.action === "refresh_source") {
    // Source receipts have a durable source key, so only that source's current
    // projection can keep the historical receipt actionable. An unrelated fleet
    // failure must not revive it.
    const implicatedSource = command.source_key
      ? sources.find((source) => source.source_key === command.source_key)
      : null;
    return implicatedSource && sourceHasCurrentEnabledFailure(implicatedSource)
      ? "current"
      : "contained";
  }

  // refresh_due currently records aggregate outcome counts without failed source
  // keys. Until durable per-source correlation exists, the safest bounded signal
  // is whether any enabled source's latest run remains failed.
  const hasCurrentEnabledFailure = sources.some(sourceHasCurrentEnabledFailure);
  return hasCurrentEnabledFailure ? "current" : "contained";
}

export function sourceAttentionScore(source: AdminSource): number {
  if (!source.enabled) return 0;
  if (source.effective_status === "policy_blocked") return 100;
  if (source.review_status !== "reviewed") return 95;
  if (source.latest_run) {
    const latestStatus = displayedRunStatus(source.latest_run);
    if (latestStatus === "running" || latestStatus === "paused") return 0;
    if (latestStatus === "failed") return 90;
  }
  if (source.due && !source.last_succeeded_at) return 80;
  if (
    source.latest_run?.status === "succeeded"
    && (source.latest_run.candidate_count ?? 0) > 0
    && (source.latest_run.canonical_count ?? 0) === 0
  ) {
    return 75;
  }
  return 0;
}

type TruthfulAdminRun = Omit<
  AdminRun,
  "is_latest_for_source" | "resolved_by_newer_success"
> & {
  is_latest_for_source?: boolean;
  resolved_by_newer_success?: boolean;
};

function adminRunTime(run: AdminRun): number {
  const value = run.started_at ?? run.completed_at;
  if (!value) return Number.NEGATIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}

export function presentAdminRuns(
  runs: AdminRun[],
  view: AdminRunView,
  status: RunStatus | "",
): PresentedAdminRun[] {
  const ordered = runs
    .map((run, index) => ({ run, index }))
    .sort((left, right) => {
      const byTime = adminRunTime(right.run) - adminRunTime(left.run);
      return byTime || left.index - right.index;
    })
    .map(({ run }) => run);
  const sourcesWithNewerSuccess = new Set<string>();
  const latestSources = new Set<string>();
  const presented: PresentedAdminRun[] = [];

  for (const run of ordered) {
    const truthfulRun = run as TruthfulAdminRun;
    const inferredLatest = !latestSources.has(run.source_key);
    latestSources.add(run.source_key);
    const isLatest = typeof truthfulRun.is_latest_for_source === "boolean"
      ? truthfulRun.is_latest_for_source
      : inferredLatest;
    const failed = isFailedRun(run);
    const inferredResolved = (
      failed
      && sourcesWithNewerSuccess.has(run.source_key)
    );
    const resolved = (
      failed
      && (
        typeof truthfulRun.resolved_by_newer_success === "boolean"
          ? truthfulRun.resolved_by_newer_success
          : inferredResolved
      )
    );
    const disposition: AdminRunDisposition = isLatest
      ? "current"
      : resolved ? "resolved" : "historical";
    if (view === "history" || isLatest) {
      presented.push({ run, disposition });
    }
    if (run.status === "succeeded") {
      sourcesWithNewerSuccess.add(run.source_key);
    }
  }

  return status
    ? presented.filter(({ run }) => displayedRunStatus(run) === status)
    : presented;
}

export function shortRevision(value: string | null): string {
  if (!value) return "Not recorded";
  return value.length > 16 ? `${value.slice(0, 13)}…` : value;
}

export function safeHttpUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:"
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

export function statusLabel(status: SourceStatus): string {
  if (status === "review_expired") return "Review expired";
  if (status === "policy_blocked") return "Policy blocked";
  return humanize(status);
}

export type DiagnosticTone = "healthy" | "warning" | "critical" | "neutral";
export type DiagnosticTarget =
  | "admission"
  | "collect"
  | "normalize"
  | "catalog"
  | "runs"
  | "configuration";

export interface SourceDiagnostic {
  tone: DiagnosticTone;
  title: string;
  detail: string;
  target: DiagnosticTarget;
  action: string;
}

function runOutput(run: AdminRunCore | null): string {
  if (!run) return "No run has been recorded.";
  const candidate = run.candidate_count ?? 0;
  const canonical = run.canonical_count ?? 0;
  return `${formatNumber(candidate)} candidates produced ${formatNumber(canonical)} catalog events.`;
}

export function sourceDiagnostics(
  source: AdminSource,
  detail?: AdminSourceDetail | null,
): SourceDiagnostic[] {
  const diagnostics: SourceDiagnostic[] = [];
  const latest = source.latest_run;
  const latestPacerDeferred = isPacerDeferredRun(latest);

  if (adminSourceIsRetired(source)) {
    return [{
      tone: "neutral",
      title: `${adminSourceLifecycleLabel(source)} source`,
      detail: adminSourceRetirementDescription(source),
      target: "configuration",
      action: source.superseded_by_source_key ? "Open replacement" : "Inspect lifecycle",
    }];
  }

  if (source.effective_status === "policy_blocked") {
    diagnostics.push({
      tone: "critical",
      title: "Collection policy is blocking this source",
      detail: "No outbound fetch will occur until the fleet policy admits this mode.",
      target: "admission",
      action: "Inspect admission",
    });
  }
  if (source.review_status !== "reviewed") {
    diagnostics.push({
      tone: "critical",
      title: source.review_status === "expired" ? "Source review expired" : "Source is unreviewed",
      detail: "Review state is owner-managed; a refresh command cannot bypass it.",
      target: "configuration",
      action: "Review configuration",
    });
  }
  if (!source.enabled) {
    diagnostics.push({
      tone: "neutral",
      title: "Source is disabled",
      detail: "Collection is paused at the reviewed registry boundary.",
      target: "configuration",
      action: "Inspect configuration",
    });
  }
  if (latestPacerDeferred) {
    diagnostics.push({
      tone: "neutral",
      title: "Collection deferred by the pacing gate",
      detail: "Coordination delayed this attempt before provider collection; it is not a provider or parser failure.",
      target: "admission",
      action: "Inspect cadence gate",
    });
  } else if (latest && isFailedRun(latest)) {
    diagnostics.push({
      tone: "critical",
      title: `Latest run failed${latest.error ? ` · ${humanize(latest.error)}` : ""}`,
      detail: `${runOutput(latest)} Inspect revision and attempt history below.`,
      target: "runs",
      action: "Show failed runs",
    });
  }
  if (
    detail?.summary.success_rate !== null
    && detail?.summary.success_rate !== undefined
    && detail.summary.total_runs >= 3
    && detail.summary.success_rate < 0.8
    && detail.recent_runs.some((run) => isFailedRun(run))
  ) {
    diagnostics.push({
      tone: "warning",
      title: `${formatPercent(detail.summary.success_rate)} run success in this window`,
      detail: `${formatNumber(detail.summary.failed_runs)} of ${formatNumber(detail.summary.total_runs)} runs failed. Separate pacing or policy deferrals from provider and parser failures in the run ledger.`,
      target: "runs",
      action: "Show failed runs",
    });
  }
  if (!latest && source.event_count === 0 && source.enabled) {
    diagnostics.push({
      tone: "warning",
      title: "Never successfully collected",
      detail: "The source is registered but has no run history or current catalog output.",
      target: "collect",
      action: "Inspect collection",
    });
  }
  if (latest?.status === "succeeded" && (latest.candidate_count ?? 0) === 0) {
    diagnostics.push({
      tone: "neutral",
      title: "No publishable events in this window",
      detail: "The provider request completed successfully; the reviewed date and eligibility filters produced no current events. This is not a failed run.",
      target: "catalog",
      action: "Inspect catalog",
    });
  } else if (
    latest?.status === "succeeded"
    && (latest.candidate_count ?? 0) > 0
    && (latest.canonical_count ?? 0) === 0
  ) {
    diagnostics.push({
      tone: "warning",
      title: "Candidates were dropped before persistence",
      detail: "Collection worked; focus on parsing, normalization, validation, or deduplication.",
      target: "normalize",
      action: "Inspect normalization",
    });
  }
  if (
    detail?.summary.yield_rate !== null
    && detail?.summary.yield_rate !== undefined
    && detail.summary.candidate_count >= 10
    && detail.summary.yield_rate < 0.5
  ) {
    diagnostics.push({
      tone: "warning",
      title: `Low ${formatPercent(detail.summary.yield_rate)} catalog yield`,
      detail: "Compare candidate and canonical counts across revisions to locate data loss.",
      target: "normalize",
      action: "Inspect normalization",
    });
  }
  if (source.due && source.enabled && source.review_status === "reviewed") {
    diagnostics.push({
      tone: "warning",
      title: "Refresh is overdue",
      detail: source.next_due_at
        ? `The cadence deadline passed ${formatRelativeTime(source.next_due_at)}.`
        : "No next successful cadence boundary is recorded.",
      target: "admission",
      action: "Inspect cadence gate",
    });
  }
  if (!diagnostics.length) {
    diagnostics.push({
      tone: "healthy",
      title: "No immediate crawler issue detected",
      detail: `${formatNumber(source.event_count)} current catalog events; the latest bounded signals look healthy.`,
      target: "catalog",
      action: "Inspect catalog",
    });
  }
  return diagnostics.slice(0, 4);
}

const modeFolders: Record<string, string> = {
  public_jsonld: "crawl",
  livewhale_json: "livewhale",
  sf_gov_json: "sf_gov",
  datasf_our415: "datasf",
  bibliocommons_rss: "bibliocommons",
  san_jose_legistar: "legistar",
  sunnyvale_legistar: "legistar",
  alameda_legistar: "legistar",
  oakland_legistar: "legistar",
  communico_json: "communico",
  tribe_events_json: "tribe",
  localist_json: "localist",
  libcal_ics: "libcal",
  civic_engage_rss: "civic_engage",
  midpen_html: "midpen",
  usfca_html: "usfca",
  calperformances_json: "calperformances",
  berkeley_rep_html: "berkeley_rep",
  ybca_html: "ybca",
  oakland_html: "oakland",
  luma_calendar_json: "luma_calendar",
  luma_discover_json: "luma_discover",
};

export function implementationPath(mode: string): string {
  const folder = modeFolders[mode];
  return folder
    ? `src/events_concierge/adapters/${folder}/source.py`
    : "src/events_concierge/composition.py";
}
