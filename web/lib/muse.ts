import { api } from "./api.ts";
import type { EventItem } from "./types.ts";

export const MAX_MUSE_EVENTS = 5;
export const MUSE_URL = "https://muse.ai/";
const hosts = new Set(["lu.ma", "www.lu.ma", "luma.com", "www.luma.com", "meetup.com", "www.meetup.com"]);

export type MuseStatus = "queued" | "in_progress" | "needs_input" | "awaiting_approval" | "waitlisted" | "registered" | "failed" | "uncertain";
export interface MuseConnection { connected: boolean; expires_at: string | null }
export interface MuseSignupItem {
  event: { canonical_event_id: string; title: string; start_at: string; end_at: string | null;
    venue_name: string | null; city: string | null; registration_url: string; price_status: "free"; observed_at: string };
  status: MuseStatus;
  attempt_id: string | null;
  outcome: { status: MuseStatus; note: string; confirmation_reference: string | null; evidence_url: string | null } | null;
  updated_at: string;
}
export interface MuseBatch { batch_id: string; request_id: string; created_at: string; items: MuseSignupItem[] }
export interface MuseIssuedConnection { token: string; expires_at: string }

export function museProviderUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return value.length <= 2048 && /^[\x21-\x7e]+$/.test(value) && !value.includes("\\") && url.protocol === "https:"
      && hosts.has(url.hostname) && !url.username && !url.password && !url.port
      && Boolean(url.pathname.replaceAll("/", "")) && !url.hash ? value : null;
  } catch { return null; }
}

export function museEligibility(event: EventItem, now = Date.now()): string | null {
  if (event.price_status !== "free") return "Muse currently supports free events.";
  const start = Date.parse(event.start_at);
  if (event.event_status !== "scheduled" || !Number.isFinite(start) || start <= now) return "Choose an upcoming event.";
  if (event.registration_status === "sold_out") return "This event is sold out.";
  const urls = [...event.registration_urls, ...event.sources.map(source => source.registration_url)];
  if (!urls.some(url => museProviderUrl(url))) return "Muse currently supports Luma and Meetup.";
  return null;
}

const selectionKey = "ec:muse:selection";
const selectionLifetime = 30 * 60 * 1000;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Preserve only public occurrence IDs across the explicit sign-in navigation. */
export function saveMuseSelection(storage: Pick<Storage, "setItem">, ids: string[], now = Date.now()): void {
  const eventIds = [...new Set(ids)].filter(id => uuid.test(id)).slice(0, MAX_MUSE_EVENTS);
  storage.setItem(selectionKey, JSON.stringify({ eventIds, savedAt: now }));
}

export function takeMuseSelection(storage: Pick<Storage, "getItem" | "removeItem">, now = Date.now()): string[] {
  const raw = storage.getItem(selectionKey);
  storage.removeItem(selectionKey);
  try {
    const selection = JSON.parse(raw ?? "null");
    if (!selection || !Array.isArray(selection.eventIds) || typeof selection.savedAt !== "number"
      || selection.savedAt > now || now - selection.savedAt > selectionLifetime) return [];
    return [...new Set<string>(selection.eventIds.filter((id: unknown): id is string =>
      typeof id === "string" && uuid.test(id)))].slice(0, MAX_MUSE_EVENTS);
  } catch { return []; }
}

export function getMuseCatalogEvent(id: string, tenantId: string | null) {
  return api<EventItem>(`/v1/catalog/events/${encodeURIComponent(id)}`, { tenantId });
}

export function museInstruction(batchId: string): string {
  return `Use my Events Concierge connector to read signup batch ${batchId}. Sign me up for the selected free events using your browser. Claim each event before acting, check for an existing RSVP, and verify date, price and availability. Preserve your approval checks. Ask me for login or unanswered form questions; do not invent answers, pay or substitute events. Report each outcome with provider confirmation evidence. If a submission is uncertain, check its status before retrying.`;
}

export const museStatusLabel: Record<MuseStatus, string> = {
  queued: "Ready for Muse", in_progress: "In progress", needs_input: "Needs your input",
  awaiting_approval: "Awaiting organizer approval", waitlisted: "Waitlisted",
  registered: "Registered · reported by Muse", failed: "Failed", uncertain: "Needs verification",
};

export function getMuseConnection(tenantId: string | null) {
  return api<MuseConnection>("/v1/me/muse/connection", { tenantId });
}
export function createMuseConnection(tenantId: string | null) {
  return api<MuseIssuedConnection>("/v1/me/muse/connection", { method: "POST", tenantId });
}
export function revokeMuseConnection(tenantId: string | null) {
  return api<void>("/v1/me/muse/connection", { method: "DELETE", tenantId });
}
export function prepareMuseBatch(tenantId: string | null, requestId: string, eventIds: string[]) {
  return api<MuseBatch>("/v1/me/muse/batches", { method: "POST", tenantId, bodyJson: { request_id: requestId, event_ids: eventIds } });
}
export function getMuseBatches(tenantId: string | null) {
  return api<MuseBatch[]>("/v1/me/muse/batches", { tenantId });
}
