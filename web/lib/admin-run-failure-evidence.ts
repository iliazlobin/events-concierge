import type { AdminCommandEvent, AdminCommandInvestigation, AdminRun } from "./admin-types.ts";
import { isCommandError } from "./admin-command-investigation.ts";

/** Command tasks and source runs have different attempt counters. Match identities, never counters. */
export function runWorkerEvidence(run: AdminRun, snapshot: AdminCommandInvestigation | null) {
  if (!run.command || !snapshot || snapshot.command_id !== run.command.command_id) {
    return { events: [], error: null, earlierAttempt: false, unknownAttempt: false };
  }
  const events = snapshot.events.filter((event) => event.source_key === run.source_key
    && event.run_key === run.run_key).sort((a, b) => {
    const time = Date.parse(b.observed_at) - Date.parse(a.observed_at);
    if (Number.isFinite(time) && time !== 0) return time;
    if (/^\d+$/.test(a.event_id) && /^\d+$/.test(b.event_id)) return BigInt(a.event_id) < BigInt(b.event_id) ? 1 : -1;
    return b.event_id.localeCompare(a.event_id);
  });
  const latestError = events.find(isCommandError);
  // The outer task can emit a stage-less error after the stage reports its failure.
  // Keep the more specific observation from that same task attempt when available.
  const error: AdminCommandEvent | null = latestError ? events.find((event) => isCommandError(event)
    && Boolean(event.error_type) && Boolean(event.stage)
    && event.command_attempt === latestError.command_attempt
    && event.task_attempt === latestError.task_attempt) ?? latestError : null;
  const started = Date.parse(run.started_at ?? "");
  const observed = Date.parse(error?.observed_at ?? "");
  const unknownAttempt = Boolean(error && (!Number.isFinite(started) || !Number.isFinite(observed)));
  const earlierAttempt = Boolean(error && !unknownAttempt && observed < started);
  // Completion and event timestamps come from separate writes. Do not invent a skew
  // allowance or infer causality from their order: this remains a recorded worker error.
  return { events, error, earlierAttempt, unknownAttempt };
}

export function runLogContext(run: AdminRun, error: AdminCommandEvent | null): string {
  return [
    `source_key=${run.source_key}`, `run_key=${run.run_key}`,
    run.command ? `command_id=${run.command.command_id}` : null,
    run.started_at ? `run_started_at=${run.started_at}` : null,
    run.completed_at ? `run_completed_at=${run.completed_at}` : null,
    run.execution ? `worker_service=${run.execution.worker_service}` : null,
    error ? `event_id=${error.event_id}\nobserved_at=${error.observed_at}\ncommand_attempt=${error.command_attempt}` : null,
    error?.task_attempt != null ? `task_attempt=${error.task_attempt}` : null,
    error?.worker_id ? `worker_id=${error.worker_id}` : null,
    error?.stage ? `stage=${error.stage}` : null,
    error?.error_type ? `error_type=${error.error_type}` : null,
    error?.release_revision ? `release_revision=${error.release_revision}` : null,
  ].filter(Boolean).join("\n");
}
