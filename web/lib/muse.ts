import { api } from "./api.ts";
import type { EventItem } from "./types.ts";

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

export interface MuseRegistration extends MuseSignupItem {
  batch_id: string;
  created_at: string;
  version: number;
  unread: boolean;
}
export interface MuseRegistrationPage {
  items: MuseRegistration[];
  total: number;
  unread_count: number;
  next_cursor: string | null;
}

const intentKey = "ec:muse:intent";
const intentLifetime = 30 * 60 * 1000;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Only the public event ID survives the user's explicit sign-in navigation. */
export function saveMuseIntent(storage: Pick<Storage, "setItem">, eventId: string, now = Date.now()): void {
  if (uuid.test(eventId)) storage.setItem(intentKey, JSON.stringify({ eventId, savedAt: now }));
}
export function takeMuseIntent(storage: Pick<Storage, "getItem" | "removeItem">, now = Date.now()): string | null {
  const raw = storage.getItem(intentKey);
  storage.removeItem(intentKey);
  try {
    const intent = JSON.parse(raw ?? "null");
    return intent && typeof intent.eventId === "string" && uuid.test(intent.eventId)
      && typeof intent.savedAt === "number" && intent.savedAt <= now && now - intent.savedAt <= intentLifetime
      ? intent.eventId : null;
  } catch { return null; }
}

export function museInstruction(): string {
  return "Use my Events Concierge connector to read every page of my signup batches (limit 100; cursor is the last batch ID). Register only the free events I queued. Skip registered and failed tasks. Check waitlisted or awaiting-approval results without submitting again. Claim each queued event before acting, check for an existing RSVP, and verify title, date, price and availability. Resume an existing attempt without claiming a new attempt; if a submission is uncertain, check its status before retrying. Preserve your approval checks. Ask me for login or unanswered form questions; do not invent answers, pay or substitute events. Report each outcome with provider confirmation evidence.";
}

/** Newer responses cannot roll back a task that a queue or outcome response already advanced. */
export function mergeMuseRegistrations(current: MuseRegistration[], incoming: MuseRegistration[]): MuseRegistration[] {
  const byId = new Map(current.map(item => [item.event.canonical_event_id, item]));
  for (const item of incoming) {
    const previous = byId.get(item.event.canonical_event_id);
    if (!previous || item.version >= previous.version) byId.set(item.event.canonical_event_id, item);
  }
  return [...byId.values()].sort((a, b) => b.created_at.localeCompare(a.created_at)
    || b.event.canonical_event_id.localeCompare(a.event.canonical_event_id));
}

/** A stale list response must not revive versions this account has already viewed. */
export function museUnreadCount(serverCount: number, items: MuseRegistration[], seen: ReadonlyMap<string, number>): number {
  const acknowledged = new Set(items.filter(item => item.unread
    && (seen.get(item.event.canonical_event_id) ?? 0) >= item.version).map(item => item.event.canonical_event_id));
  return Math.max(0, serverCount - acknowledged.size);
}

export function museRegistrationUpdates(previous: MuseRegistration[], incoming: MuseRegistration[]): string[] {
  const known = new Map(previous.map(item => [item.event.canonical_event_id, item]));
  return incoming.flatMap(item => {
    const old = known.get(item.event.canonical_event_id);
    const changed = old && (item.status !== old.status || JSON.stringify(item.outcome) !== JSON.stringify(old.outcome));
    return changed && item.version > old.version
      ? [`${item.event.title}: ${museStatusLabel[item.status]}.${item.outcome?.note ? ` ${item.outcome.note.slice(0, 160)}` : ""}`] : [];
  });
}

export function museEventDate(value: string): string {
  return new Date(value).toLocaleString(undefined, {
    month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
  });
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
export function queueMuseRegistration(tenantId: string | null, requestId: string, eventId: string, signal?: AbortSignal) {
  return api<MuseRegistration>("/v1/me/muse/registrations", {
    method: "POST", tenantId, signal, bodyJson: { request_id: requestId, event_id: eventId },
  });
}
export function getMuseRegistrations(tenantId: string | null, cursor: string | null = null, signal?: AbortSignal) {
  const query = new URLSearchParams({ limit: "50" });
  if (cursor) query.set("cursor", cursor);
  return api<MuseRegistrationPage>(`/v1/me/muse/registrations?${query}`, { tenantId, signal });
}
export function markMuseRegistrationsSeen(tenantId: string | null, items: Array<{ event_id: string; version: number }>, signal?: AbortSignal) {
  return api<void>("/v1/me/muse/registrations/seen", { method: "POST", tenantId, signal, bodyJson: { items } });
}
