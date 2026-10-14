import { api } from "./api.ts";
import type { EventItem } from "./types.ts";
import type { RegistrationAccountSnapshot } from "./registration-accounts.ts";
import type { RegistrationStatus, RegistrationStatusCounts, RegistrationTask } from "./registrations.ts";

export const MUSE_URL = "https://muse.ai/";
const hosts = new Set(["lu.ma", "www.lu.ma", "luma.com", "www.luma.com", "meetup.com", "www.meetup.com"]);

export type MuseStatus = RegistrationStatus;
export interface MuseConnection { connected: boolean; expires_at: string | null }
export interface MuseSignupItem {
  registration_account: RegistrationAccountSnapshot;
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

export interface MuseRegistration extends RegistrationTask {}
export interface MuseRegistrationPage {
  items: MuseRegistration[];
  total: number;
  unread_count: number;
  next_cursor: string | null;
  status_counts?: RegistrationStatusCounts;
}

export function museInstruction(): string {
  return "Use my Events Concierge connector to read every page of my signup batches (limit 100; cursor is the last batch ID). Register only the free events I queued. Skip registered and failed tasks. Check waitlisted or awaiting-approval results without submitting again. Claim each queued event before acting. Use registration_account from the claim response; when resuming an existing attempt, keep its saved account. When expected_email is set, verify the visible provider account matches it before provider action; otherwise use the current browser account. Never infer a provider account from my Events Concierge sign-in or switch accounts without my instruction. Check for an existing RSVP and verify title, date, price and availability. Resume an existing attempt without claiming a new attempt; if a submission is uncertain, check its status before retrying. Preserve your approval checks. Ask me for login or unanswered form questions; do not invent answers, pay or substitute events. Report each outcome with provider confirmation evidence.";
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
  queued: "Queued", in_progress: "In progress", needs_input: "Needs your input",
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

/** Look up saved tasks for visible cards without paging through the account queue. */
export function getMuseRegistrationsForEvents(tenantId: string | null, eventIds: string[], signal?: AbortSignal) {
  const query = new URLSearchParams();
  eventIds.forEach(id => query.append("event_id", id));
  return api<MuseRegistration[]>(`/v1/me/muse/registrations/by-event?${query}`, { tenantId, signal });
}
