import { api } from "./api.ts";

export type WorkErrorQueue = "request_start" | "entity_refresh";
export type WorkRecordQueue = WorkErrorQueue | "notifications";
export type WorkRecordScope = "pending" | "errors" | "failed";
export interface WorkErrorSource {
  source_id: string;
  provider_key: string;
  status: "failed" | "blocked";
  error_code: string | null;
  error_summary: string;
  observed_at: string | null;
  next_refresh_at: string | null;
}
export interface WorkErrorRecord {
  record_id: string;
  label: string;
  state: "ready" | "scheduled" | "leased" | "due" | "failed";
  error_code: string | null;
  error_summary: string | null;
  attempt_count: number | null;
  attempt_kind?: "failed_start_attempts" | "failed_delivery_attempts";
  failed_at?: string | null;
  created_at: string | null;
  next_attempt_at: string | null;
  lease_expires_at: string | null;
  last_observed_at: string | null;
  sources: WorkErrorSource[];
}
export interface WorkErrorPage {
  generated_at: string;
  queue: WorkErrorQueue;
  total: number;
  offset: number;
  limit: number;
  items: WorkErrorRecord[];
}

export const WORK_ERROR_PAGE_SIZE = 10;
export const WORK_ERROR_MAX_OFFSET = 10_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function supportsWorkErrors(queue: string | null): queue is WorkErrorQueue {
  return queue === "request_start" || queue === "entity_refresh";
}
export function workErrorLocation(href: string, queue: WorkRecordQueue) {
  const url = new URL(href, "http://localhost");
  if (url.searchParams.get("ops_queue") !== queue) return { record: null, offset: 0 };
  const candidate = url.searchParams.get("ops_record") ?? "";
  const rawOffset = url.searchParams.get("ops_offset") ?? "0";
  const offset = /^\d+$/.test(rawOffset) ? Number(rawOffset) : 0;
  return {
    record: validRecord(candidate, queue) ? candidate.toLowerCase() : null,
    offset: Number.isSafeInteger(offset) && offset <= WORK_ERROR_MAX_OFFSET ? offset : 0,
  };
}
export function workErrorUrl(href: string, record: string | null, offset: number) {
  const url = new URL(href, "http://localhost");
  if (record && validRecord(record, url.searchParams.get("ops_queue"))) url.searchParams.set("ops_record", record.toLowerCase());
  else url.searchParams.delete("ops_record");
  if (Number.isSafeInteger(offset) && offset > 0 && offset <= WORK_ERROR_MAX_OFFSET) url.searchParams.set("ops_offset", String(offset));
  else url.searchParams.delete("ops_offset");
  return `${url.pathname}${url.search}${url.hash}`;
}
export async function getAdminWorkErrors(queue: WorkErrorQueue, offset: number, record: string | null, signal: AbortSignal, limit = WORK_ERROR_PAGE_SIZE): Promise<WorkErrorPage> {
  const query = new URLSearchParams({ queue, offset: String(record ? 0 : offset), limit: String(recordPageSize(limit)) });
  if (record && UUID.test(record)) query.set("record_id", record);
  const page = await api<WorkErrorPage>(`/admin/v1/operations/errors?${query}`, { cache: "no-store", signal });
  if (page.queue !== queue) throw new Error("Work error snapshot did not match the selected queue.");
  return page;
}
/** Fixed UTC timestamps keep long-scheduled retries legible and independently inspectable. */
export function workErrorTime(value: string | null): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "Not recorded";
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
export function workErrorState(state: WorkErrorRecord["state"], queue: WorkRecordQueue = "request_start"): { label: string; detail: string } {
  switch (state) {
    case "failed": return { label: "Failed", detail: "A terminal failure is recorded. This item is no longer pending; history may include quarantined notification work." };
    case "scheduled": return { label: "Scheduled", detail: queue === "entity_refresh" ? "The next refresh time is in the future; this profile is not due." : "The recorded retry time is in the future; this item is not ready to claim." };
    case "leased": return { label: "Lease recorded", detail: "An unexpired claim is recorded. This does not establish that a worker is running." };
    case "due": return { label: "Due for refresh", detail: "This profile is eligible for refresh. Entity refresh does not record a claim lease." };
    case "ready": return { label: "Ready to claim", detail: "The item is eligible to be claimed. This does not establish that a worker is running." };
  }
}

/** IDs retain their native representation: UUIDs for requests/entities, decimal outbox IDs. */
function validRecord(record: string, queue: string | null): boolean {
  if (queue !== "notifications") return UUID.test(record);
  if (!/^(0|-?[1-9]\d{0,18})$/.test(record)) return false;
  const id = BigInt(record);
  return id >= BigInt("-9223372036854775808") && id <= BigInt("9223372036854775807");
}
export function supportsWorkRecords(queue: string | null): queue is WorkRecordQueue {
  return supportsWorkErrors(queue) || queue === "notifications";
}
export function workRecordScope(href: string, queue: WorkRecordQueue): WorkRecordScope {
  if (queue === "entity_refresh") return "errors";
  const url = new URL(href, "http://localhost");
  const scope = url.searchParams.get("ops_queue") === queue ? url.searchParams.get("ops_scope") : null;
  return queue === "notifications" && scope === "failed" ? "failed" : queue === "request_start" && scope === "errors" ? "errors" : "pending";
}
export function workRecordScopeUrl(href: string, scope: WorkRecordScope): string {
  const url = new URL(workErrorUrl(href, null, 0), "http://localhost");
  if (scope === "pending") url.searchParams.delete("ops_scope");
  else url.searchParams.set("ops_scope", scope);
  return `${url.pathname}${url.search}${url.hash}`;
}
export interface WorkRecordPage extends Omit<WorkErrorPage, "queue"> {
  queue: WorkRecordQueue;
  scope?: WorkRecordScope;
}
function recordPageSize(limit: number): number {
  return Number.isInteger(limit) && limit >= 1 && limit <= 50 ? limit : WORK_ERROR_PAGE_SIZE;
}
export async function getAdminWorkRecords(queue: WorkRecordQueue, scope: WorkRecordScope, offset: number, record: string | null, signal: AbortSignal, limit = WORK_ERROR_PAGE_SIZE): Promise<WorkRecordPage> {
  if (queue === "entity_refresh") return getAdminWorkErrors(queue, offset, record, signal, limit);
  const query = new URLSearchParams({ queue, scope, offset: String(record ? 0 : offset), limit: String(recordPageSize(limit)) });
  if (record && validRecord(record, queue)) query.set("record_id", record);
  const page = await api<WorkRecordPage>(`/admin/v1/operations/records?${query}`, { cache: "no-store", signal });
  if (page.queue !== queue || page.scope !== scope) throw new Error("Records did not match the selected investigation.");
  return page;
}
