import type { AdminSourceHealth } from "./admin-types.ts";
import type { BackendQueue } from "./backend-operations.ts";

export interface QueueEvidence {
  tone: "neutral" | "info" | "warn";
  label: string;
  summary: string;
  failureLabel: string;
  failureMeaning: string;
  progressLabel: string;
}

const PENDING_FAILURE_QUEUES = new Set([
  "request_start", "account_erasure", "change_delivery", "calendar_repair",
  "handoff_expiry", "watch_projection",
]);

/** Mirrors the distinct predicates in fn_get_operator_backend_overview_v1 (0182). */
export function queueEvidence(
  queue: BackendQueue | undefined,
  unavailable = false,
): QueueEvidence {
  const historical = queue?.queue === "notifications" || queue?.queue === "ingestion_commands";
  const entity = queue?.queue === "entity_refresh";
  const pendingFailure = queue !== undefined && PENDING_FAILURE_QUEUES.has(queue.queue);
  const meanings = {
    failureLabel: historical ? "Recorded failures" : entity ? "Failed / blocked entities" : "Pending with errors",
    failureMeaning: historical
      ? "Recorded terminal failures across retained history; separate from pending work and not a current incident count."
      : entity
        ? "Entities with a failed or blocked external source. These may not currently be due for refresh."
        : pendingFailure
          ? "Pending work with a recorded error; overlaps the pending count and does not establish a current outage."
          : "Failure signals from the durable projection; their relationship to pending work is unknown.",
    progressLabel: entity ? "Last refresh evidence" : queue?.queue === "account_erasure" ? "Last completed stage" : "Last completion",
  };
  if (!pendingFailure && !historical && !entity) meanings.failureLabel = "Failure signals";

  if (!queue || unavailable) {
    return {
      ...meanings,
      tone: "neutral",
      label: "Unknown",
      summary: "Current queue state is unavailable. Worker liveness is not measured.",
    };
  }

  if (entity) {
    return {
      ...meanings,
      tone: queue.failed > 0 ? "warn" : queue.pending > 0 ? "info" : "neutral",
      label: queue.failed > 0 ? "Refresh evidence to inspect" : queue.pending > 0 ? "Refresh due" : "No refresh due",
      summary: `${queue.pending} entities due${queue.failed > 0 ? ` · ${queue.failed} with failed or blocked source evidence` : ""}. No claim lease is measured.`,
    };
  }

  const work = queue.pending > 0
    ? `${queue.pending} pending · ${queue.ready} ready · ${queue.leased} leased`
    : "No pending work recorded";
  const failures = queue.failed > 0
    ? historical
      ? ` · ${queue.failed} recorded terminal failures`
      : pendingFailure
        ? ` · ${queue.failed} pending with errors`
        : ` · ${queue.failed} failure signals`
    : "";
  const currentFailureEvidence = queue.failed > 0 && !historical;
  return {
    ...meanings,
    tone: currentFailureEvidence ? "warn" : queue.pending > 0 ? "info" : "neutral",
    label: currentFailureEvidence
      ? "Errors to inspect"
      : queue.ready > 0
        ? "Ready work"
        : queue.leased > 0
          ? "Leased work"
          : queue.pending > 0
            ? "Pending work"
            : queue.failed > 0
              ? "Failure history"
              : "No pending work",
    summary: `${work}${failures}. Worker liveness is not measured.`,
  };
}

/** Lifecycle status is separate from an active source's evidence requiring inspection. */
export function sourceNeedsAttention(source: AdminSourceHealth): boolean {
  if (!source.enabled || source.retired_at) return false;
  return source.run_state === "failed"
    || source.run_state === "never_run"
    || !["ok", "not_scheduled"].includes(source.freshness_state)
    || source.retry_state !== "ok"
    || source.yield_state === "zero_yield";
}

export function sourceSignal(source: AdminSourceHealth): string {
  if (source.retired_at) return "Retired";
  if (!source.enabled) return "Paused";
  if (source.run_state === "failed") return "Latest run failed";
  if (source.run_state === "never_run") return "No run recorded";
  if (source.freshness_state === "never") return "No successful refresh recorded";
  if (source.freshness_state === "down") return "No successful refresh for over 7 days";
  if (source.freshness_state === "late") return "Refresh overdue";
  if (source.retry_state !== "ok") {
    return source.latest_attempt_count === null
      ? "Repeated attempts on latest run"
      : `${source.latest_attempt_count} attempts on latest run`;
  }
  if (source.freshness_state === "warn") return "Refresh past its freshness target";
  if (source.yield_state === "zero_yield") return "No catalog output on latest success";
  if (source.run_state === "running") return "Run recorded as running";
  if (source.run_state === "deferred") return "Latest run deferred";
  if (source.freshness_state === "not_scheduled") return "Not scheduled";
  return "Within freshness target";
}
