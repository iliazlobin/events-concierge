import type {
  AdminCommandDetail,
  CommandStatus,
} from "@/lib/admin-types";

export const ADMIN_COMMAND_DETAIL_POLL_INTERVAL_MS = 2_500;

function commandIsActive(status: CommandStatus | null): boolean {
  return status === "queued" || status === "running";
}

export function commandDetailHasActiveRuns(
  detail: AdminCommandDetail,
): boolean {
  // Legacy commands created before durable command/run links can expose aggregate
  // "pending" counts reconstructed from their terminal receipt. They have no child
  // state to follow, so only linked plans may extend polling beyond command completion.
  return detail.runs.length > 0 && (detail.progress.pending > 0
    || detail.progress.running > 0
    || detail.runs.some(
      (run) => run.status === "pending" || run.status === "running",
    ));
}

export function shouldPollAdminCommandDetail(
  detail: AdminCommandDetail | null,
  fallbackStatus: CommandStatus | null,
): boolean {
  if (!detail) return commandIsActive(fallbackStatus);
  return commandIsActive(detail.command.status)
    || commandDetailHasActiveRuns(detail);
}

export function isAdminCommandPollAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
