import type { RegistrationAccountSnapshot } from "./registration-accounts.ts";

export type RegistrationStatus = "queued" | "in_progress" | "needs_input" | "awaiting_approval" | "waitlisted" | "registered" | "failed" | "uncertain";
export type RegistrationStatusCounts = Record<RegistrationStatus, number> & { removed?: number };
export interface RegistrationTask {
  removed_at?: string | null;
  registration_account?: RegistrationAccountSnapshot;
  event: { canonical_event_id: string; title: string; start_at: string; end_at: string | null;
    venue_name: string | null; city: string | null; registration_url: string; price_status: "free"; observed_at: string };
  status: RegistrationStatus;
  attempt_id: string | null;
  outcome: { status: RegistrationStatus; note: string; confirmation_reference: string | null; evidence_url: string | null } | null;
  batch_id: string;
  created_at: string;
  updated_at: string;
  version: number;
  unread: boolean;
}

export interface RegistrationActivity {
  status: RegistrationStatus;
  at: string;
}

/** The API retains the queue time and latest state, not intermediate browser steps. */
export function registrationActivity(task: RegistrationTask): RegistrationActivity[] {
  const activity: RegistrationActivity[] = [{ status: "queued", at: task.created_at }];
  if (task.status !== "queued") activity.push({ status: task.status, at: task.updated_at });
  return activity;
}

export const registrationProgress: Record<RegistrationStatus, string> = {
  queued: "Not started. Open Muse to process the queue.",
  in_progress: "Picked up by Muse; no result reported yet.",
  needs_input: "Your input is needed in Muse.",
  awaiting_approval: "Waiting for the organizer to approve your registration.",
  waitlisted: "Waiting for a place to become available.",
  registered: "Muse reported a confirmed registration.",
  failed: "Muse reported that registration failed.",
  uncertain: "Check provider confirmation before retrying.",
};

export type RegistrationFilter = "all" | "queued" | "active" | "registered" | "attention" | "waiting" | "removed";
export const registrationFilters: Array<{ key: RegistrationFilter; label: string }> = [
  { key: "all", label: "All" }, { key: "queued", label: "Queued" },
  { key: "active", label: "In progress" }, { key: "registered", label: "Registered" },
  { key: "attention", label: "Needs attention" }, { key: "waiting", label: "Waiting" }, { key: "removed", label: "Removed" },
];
export function registrationSummary(counts: RegistrationStatusCounts) {
  return { queued: counts.queued, active: counts.in_progress, registered: counts.registered,
    attention: counts.needs_input + counts.uncertain + counts.failed,
    waiting: counts.awaiting_approval + counts.waitlisted };
}
export function registrationFilterCount(counts: RegistrationStatusCounts, filter: RegistrationFilter): number {
  if (filter === "removed") return counts.removed ?? 0;
  return filter === "all" ? Object.values(registrationSummary(counts)).reduce((total, count) => total + count, 0) : registrationSummary(counts)[filter];
}
export function registrationMatchesFilter(task: RegistrationTask, filter: RegistrationFilter): boolean {
  if (filter === "removed") return Boolean(task.removed_at);
  if (task.removed_at) return false;
  switch (filter) {
    case "all": return true;
    case "queued": return task.status === "queued";
    case "active": return task.status === "in_progress";
    case "registered": return task.status === "registered";
    case "attention": return ["needs_input", "uncertain", "failed"].includes(task.status);
    case "waiting": return ["awaiting_approval", "waitlisted"].includes(task.status);
  }
}
