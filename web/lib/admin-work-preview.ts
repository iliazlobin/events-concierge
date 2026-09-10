import type { BackendQueue } from "./backend-operations.ts";
import type { WorkRecordScope } from "./admin-work-errors.ts";

export const WORK_PREVIEW_LIMIT = 3;

/** Interpret queue totals only; record samples never become fleet-wide conclusions. */
export function workPreviewInsight(queue: BackendQueue | undefined, scope: WorkRecordScope): string {
  if (!queue) return "Queue totals are unavailable. Records below have their own snapshot.";
  if (queue.queue === "entity_refresh") {
    return queue.failed > 0
      ? `${queue.failed.toLocaleString("en-US")} profiles have source errors. ${queue.pending.toLocaleString("en-US")} profiles are due across the full inventory; these groups can overlap.`
      : "No profiles with source errors are recorded in this snapshot.";
  }
  if (scope === "failed") return "Terminal notification failures are retained history, separate from pending work.";
  if (scope === "errors") return "These pending requests have a recorded start failure. Their retry date determines when they become eligible again.";
  if (!queue.pending) return "No pending work is recorded in this snapshot.";
  if (!queue.ready && !queue.leased) return "Nothing is ready to claim or currently leased. Check the scheduled dates below before treating pending work as stuck.";
  if (queue.ready) return `${queue.ready.toLocaleString("en-US")} ready to claim. A ready record does not confirm that a worker is processing it.`;
  return `${queue.leased.toLocaleString("en-US")} unexpired leases recorded. Lease state alone does not establish worker progress.`;
}
