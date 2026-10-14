import type { RegistrationAccountSnapshot } from "./registration-accounts.ts";

export type RegistrationStatus = "queued" | "in_progress" | "needs_input" | "awaiting_approval" | "waitlisted" | "registered" | "failed" | "uncertain";
export type RegistrationStatusCounts = Record<RegistrationStatus, number>;
export interface RegistrationTask {
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
export type RegistrationFilter = "all" | "queued" | "active" | "registered" | "attention" | "waiting";
export const registrationFilters: Array<{ key: RegistrationFilter; label: string }> = [
  { key: "all", label: "All" }, { key: "queued", label: "Queued" },
  { key: "active", label: "In progress" }, { key: "registered", label: "Registered" },
  { key: "attention", label: "Needs attention" }, { key: "waiting", label: "Waiting" },
];
export function registrationSummary(counts: RegistrationStatusCounts) {
  return { queued: counts.queued, active: counts.in_progress, registered: counts.registered,
    attention: counts.needs_input + counts.uncertain + counts.failed,
    waiting: counts.awaiting_approval + counts.waitlisted };
}
export function registrationFilterCount(counts: RegistrationStatusCounts, filter: RegistrationFilter): number {
  return filter === "all" ? Object.values(counts).reduce((total, count) => total + count, 0) : registrationSummary(counts)[filter];
}
export function registrationMatchesFilter(task: RegistrationTask, filter: RegistrationFilter): boolean {
  switch (filter) {
    case "all": return true;
    case "queued": return task.status === "queued";
    case "active": return task.status === "in_progress";
    case "registered": return task.status === "registered";
    case "attention": return ["needs_input", "uncertain", "failed"].includes(task.status);
    case "waiting": return ["awaiting_approval", "waitlisted"].includes(task.status);
  }
}
