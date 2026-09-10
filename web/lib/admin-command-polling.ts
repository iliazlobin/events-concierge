import type {
  AdminCommandDetail,
  AdminCommandList,
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

/** Follow work observed active, including child runs that outlive their dispatch receipt. */
export async function readAdminCommandProgress({
  loadList, loadDetail, watched, signal, selectedCommandId,
}: {
  loadList: () => Promise<AdminCommandList>;
  loadDetail: (commandId: string) => Promise<AdminCommandDetail>;
  watched: Set<string>;
  signal: AbortSignal;
  selectedCommandId?: string;
}): Promise<{ page: AdminCommandList; childrenSettled: boolean; continuePolling: boolean }> {
  const page = await loadList();
  if (signal.aborted) return { page, childrenSettled: false, continuePolling: false };
  for (const command of page.items) {
    if (commandIsActive(command.status)) watched.add(command.command_id);
  }
  let childrenSettled = false;
  // Round-robin four detail reads at most per cycle; never download all recent receipts.
  for (const commandId of [...watched].filter((id) => id !== selectedCommandId).slice(0, 4)) {
    const detail = await loadDetail(commandId);
    if (signal.aborted) break;
    watched.delete(commandId);
    if (shouldPollAdminCommandDetail(detail, detail.command.status)) watched.add(commandId);
    else childrenSettled = true;
  }
  return {
    page,
    childrenSettled,
    continuePolling: watched.size > 0 || page.items.some((command) => commandIsActive(command.status)),
  };
}
