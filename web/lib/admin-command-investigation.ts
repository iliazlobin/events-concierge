import type { AdminCommandEvent, AdminCommandInvestigation, AdminCommandTask } from "./admin-types.ts";

export const COMMAND_EVENT_TAIL_LIMIT = 500;
export const COMMAND_PANELS = ["overview", "activity", "execution"] as const;
export type CommandPanel = typeof COMMAND_PANELS[number];

export function commandPanelForKey(panel: CommandPanel, key: string): CommandPanel | undefined {
  if (key === "Home") return COMMAND_PANELS[0];
  if (key === "End") return COMMAND_PANELS.at(-1);
  const direction = key === "ArrowRight" ? 1 : key === "ArrowLeft" ? -1 : 0;
  return direction ? COMMAND_PANELS[(COMMAND_PANELS.indexOf(panel) + direction + COMMAND_PANELS.length) % COMMAND_PANELS.length] : undefined;
}

export function elapsedAt(from: string | null | undefined, to: string | null | undefined): number | null {
  if (!from || !to) return null;
  const duration = Date.parse(to) - Date.parse(from);
  return Number.isFinite(duration) ? Math.max(0, duration) : null;
}

export function isCommandError(event: AdminCommandEvent): boolean {
  return Boolean(event.error_type)
    || ["error", "lease_lost", "lease_renewal_error", "execution_error"].includes(event.event_code)
    || ["failed", "retry_exhausted"].includes(event.outcome_code ?? "");
}

/** Counters are cumulative within one collection execution. Never sum progress snapshots. */
export function commandActivityMetrics(events: AdminCommandEvent[]): { requests: number | null; pages: number | null } {
  const counts = new Map<string, { requests: number | null; pages: number | null }>();
  for (const event of events) {
    const key = JSON.stringify([event.command_attempt, event.source_key, event.run_key, event.task_attempt, event.worker_id]);
    const previous = event.event_code === "stage_started" && event.stage === "collect"
      ? { requests: null, pages: null } : counts.get(key) ?? { requests: null, pages: null };
    counts.set(key, {
      requests: event.request_count ?? previous.requests,
      pages: event.page_count ?? previous.pages,
    });
  }
  const total = (field: "requests" | "pages") => {
    const measured = [...counts.values()].flatMap((count) => count[field] === null ? [] : [count[field] as number]);
    return measured.length ? measured.reduce((sum, count) => sum + count, 0) : null;
  };
  return { requests: total("requests"), pages: total("pages") };
}

/** A stage observation is current only when tied to a running task's exact claim. */
export function currentTaskObservation(tasks: AdminCommandTask[], events: AdminCommandEvent[]): AdminCommandEvent | undefined {
  return events.findLast((event) => Boolean(event.stage) && tasks.some((task) =>
    task.source_key === event.source_key && task.run_key === event.run_key
    && task.attempt_count === event.task_attempt
    && ((task.status === "running" && task.lease_state !== "expired")
      || (task.status === "queued" && task.run?.status === "running"))));
}

/** Event cursors are strings; do not lose database bigint precision through Number(). */
export function mergeCommandEvents(
  current: AdminCommandEvent[], incoming: AdminCommandEvent[],
): AdminCommandEvent[] {
  const byId = new Map(current.map((event) => [event.event_id, event]));
  for (const event of incoming) byId.set(event.event_id, event);
  return [...byId.values()].sort((a, b) => {
    if (/^\d+$/.test(a.event_id) && /^\d+$/.test(b.event_id)) {
      return BigInt(a.event_id) < BigInt(b.event_id) ? -1 : BigInt(a.event_id) > BigInt(b.event_id) ? 1 : 0;
    }
    return a.observed_at.localeCompare(b.observed_at) || a.event_id.localeCompare(b.event_id);
  }).slice(-COMMAND_EVENT_TAIL_LIMIT);
}

export function mergeCommandInvestigation(
  current: AdminCommandInvestigation | null, next: AdminCommandInvestigation,
): AdminCommandInvestigation {
  return {
    ...next,
    events: mergeCommandEvents(current?.command_id === next.command_id ? current.events : [], next.events),
    next_event_id: next.next_event_id ?? (current?.command_id === next.command_id ? current.next_event_id : null),
  };
}

export function commandWaitExplanation(status: string, availableAt: string | null, now: number): string {
  if (status === "queued") return availableAt && Date.parse(availableAt) > now
    ? "Accepted; not eligible for another claim until the retry time."
    : "Accepted and eligible; awaiting a dispatch claim. Queue position and a blocking worker are not recorded.";
  if (status === "running") return "A dispatch claim is recorded. Check lease evidence separately from the last recorded progress.";
  return "Dispatch has ended. Source execution outcomes remain separate below.";
}

export function codeRevisionLabel(revision: string | null, digest: string | null): string {
  if (!revision) return "Execution revision not recorded";
  if (revision.includes("dirty") || revision === "development" || !digest) return `${revision} · local or unpublished; source is not pinned`;
  return `${revision} · recorded image ${digest}`;
}

export function codeSourcePath(module: string): string {
  return /^events_concierge(?:\.[A-Za-z_][A-Za-z0-9_]*)+$/.test(module)
    ? `src/${module.replaceAll(".", "/")}.py` : module;
}
