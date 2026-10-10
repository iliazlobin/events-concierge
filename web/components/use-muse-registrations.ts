"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { readableError } from "@/lib/api";
import {
  getMuseRegistrations, markMuseRegistrationsSeen, mergeMuseRegistrations, museRegistrationUpdates,
  museStatusLabel, museUnreadCount, queueMuseRegistration, type MuseRegistration,
} from "@/lib/muse";

interface QueueState {
  account: string | null;
  items: MuseRegistration[];
  unread: number;
  total: number;
  cursor: string | null;
  loading: boolean;
  error: string | null;
  pending: ReadonlySet<string>;
  notice: string | null;
}
const empty = (account: string | null): QueueState => ({
  account, items: [], unread: 0, total: 0, cursor: null, loading: false, error: null,
  pending: new Set(), notice: null,
});

/** The server owns registration state and unread versions; no account data is stored in the browser. */
export function useMuseRegistrations(enabled: boolean, account: string | null, tenantId: string | null, open: boolean) {
  const key = enabled ? account : null;
  const currentAccount = useRef(key);
  currentAccount.current = key;
  const [state, setState] = useState<QueueState>(() => empty(key));
  const stateRef = useRef(state);
  stateRef.current = state;
  const requests = useRef(new Set<AbortController>());
  const refreshing = useRef(false);
  const loadedCount = useRef(50);
  const initialized = useRef(false);
  const changes = useRef(0);
  const requestIds = useRef(new Map<string, string>());
  const pending = useRef(new Set<string>());
  const seenVersions = useRef(new Map<string, number>());
  const marking = useRef(new Set<string>());

  const controller = useCallback(() => {
    const request = new AbortController();
    requests.current.add(request);
    return request;
  }, []);
  const isCurrent = useCallback((request: AbortController) => Boolean(key)
    && currentAccount.current === key && !request.signal.aborted, [key]);
  const normalize = useCallback((items: MuseRegistration[]) => items.map(item => ({
    ...item, unread: item.unread && (seenVersions.current.get(item.event.canonical_event_id) ?? 0) < item.version,
  })), []);

  const refresh = useCallback(async function readQueue() {
    if (!key || refreshing.current) return;
    refreshing.current = true;
    const request = controller();
    const startedAt = changes.current;
    setState(old => old.account === key ? { ...old, loading: true, error: null } : old);
    try {
      let page = await getMuseRegistrations(tenantId, null, request.signal);
      const first = page;
      let incoming = page.items;
      const visited = new Set<string>();
      while (open && page.next_cursor && incoming.length < loadedCount.current && !visited.has(page.next_cursor)) {
        visited.add(page.next_cursor);
        page = await getMuseRegistrations(tenantId, page.next_cursor, request.signal);
        incoming = [...incoming, ...page.items];
      }
      if (!isCurrent(request)) return;
      const updates = initialized.current ? museRegistrationUpdates(stateRef.current.items, incoming) : [];
      initialized.current = true;
      const normalized = normalize(incoming);
      setState(old => old.account === key ? { ...old,
        items: mergeMuseRegistrations(old.items, normalized),
        unread: startedAt === changes.current ? museUnreadCount(first.unread_count, incoming, seenVersions.current)
          : Math.max(old.unread, normalized.filter(item => item.unread).length),
        total: startedAt === changes.current ? first.total : old.total, cursor: page.next_cursor, loading: false,
        notice: updates.length ? updates.slice(0, 3).join(" ") : old.notice,
      } : old);
    } catch (problem) {
      if (isCurrent(request)) setState(old => old.account === key ? { ...old, loading: false,
        error: readableError(problem, "Could not update registrations. Your saved tasks are unchanged."),
      } : old);
    } finally {
      requests.current.delete(request);
      if (currentAccount.current === key && !request.signal.aborted) {
        refreshing.current = false;
        if (startedAt !== changes.current) void readQueue();
      }
    }
  }, [controller, isCurrent, key, normalize, open, tenantId]);

  useEffect(() => {
    setState(empty(key));
    loadedCount.current = 50; initialized.current = false; refreshing.current = false;
    requestIds.current.clear(); pending.current.clear(); seenVersions.current.clear(); marking.current.clear();
    return () => {
      for (const request of requests.current) request.abort();
      requests.current.clear();
    };
  }, [key]);

  useEffect(() => {
    if (!key) return;
    const update = () => { if (document.visibilityState === "visible") void refresh(); };
    update();
    const interval = window.setInterval(update, 15_000);
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [key, refresh]);

  const queue = useCallback(async (eventId: string): Promise<MuseRegistration | null> => {
    if (!key || pending.current.has(eventId)) return null;
    pending.current.add(eventId);
    const request = controller();
    const requestId = requestIds.current.get(eventId) ?? crypto.randomUUID();
    requestIds.current.set(eventId, requestId);
    setState(old => old.account === key ? { ...old, pending: new Set(pending.current), error: null } : old);
    try {
      const item = await queueMuseRegistration(tenantId, requestId, eventId, request.signal);
      if (!isCurrent(request)) return null;
      changes.current += 1;
      const existing = stateRef.current.items.some(old => old.event.canonical_event_id === eventId);
      setState(old => old.account === key ? { ...old, items: mergeMuseRegistrations(old.items, normalize([item])),
        total: old.total + (existing ? 0 : 1), unread: old.unread + (!existing && item.unread ? 1 : 0),
        notice: `${item.event.title}: ${item.status === "queued" ? "Queued for Muse" : museStatusLabel[item.status]}.`,
      } : old);
      void refresh();
      return item;
    } catch (problem) {
      if (isCurrent(request)) setState(old => old.account === key ? { ...old,
        error: readableError(problem, "Could not queue this event. Try again."),
      } : old);
      return null;
    } finally {
      requests.current.delete(request);
      if (currentAccount.current === key && !request.signal.aborted) {
        pending.current.delete(eventId);
        setState(old => old.account === key ? { ...old, pending: new Set(pending.current) } : old);
      }
    }
  }, [controller, isCurrent, key, normalize, refresh, tenantId]);

  const loadMore = useCallback(async () => {
    const cursor = stateRef.current.account === key ? stateRef.current.cursor : null;
    if (!key || !cursor || refreshing.current) return;
    refreshing.current = true;
    const request = controller();
    const startedAt = changes.current;
    setState(old => old.account === key ? { ...old, loading: true, error: null } : old);
    try {
      const page = await getMuseRegistrations(tenantId, cursor, request.signal);
      if (!isCurrent(request)) return;
      loadedCount.current += page.items.length;
      const normalized = normalize(page.items);
      setState(old => old.account === key ? { ...old, items: mergeMuseRegistrations(old.items, normalized),
        total: startedAt === changes.current ? page.total : old.total,
        unread: startedAt === changes.current ? museUnreadCount(page.unread_count, page.items, seenVersions.current)
          : Math.max(old.unread, normalized.filter(item => item.unread).length),
        cursor: page.next_cursor, loading: false,
      } : old);
    } catch (problem) {
      if (isCurrent(request)) setState(old => old.account === key ? { ...old, loading: false,
        error: readableError(problem, "Could not load more registrations."),
      } : old);
    } finally {
      requests.current.delete(request);
      if (currentAccount.current === key && !request.signal.aborted) refreshing.current = false;
    }
  }, [controller, isCurrent, key, normalize, tenantId]);

  const markSeen = useCallback(async (observed: MuseRegistration[]) => {
    if (!key) return;
    const items = observed.filter(item => item.unread
      && !marking.current.has(`${item.event.canonical_event_id}:${item.version}`))
      .map(item => ({ event_id: item.event.canonical_event_id, version: item.version }));
    if (!items.length) return;
    items.forEach(item => marking.current.add(`${item.event_id}:${item.version}`));
    const request = controller();
    try {
      for (let start = 0; start < items.length; start += 50) {
        await markMuseRegistrationsSeen(tenantId, items.slice(start, start + 50), request.signal);
        if (!isCurrent(request)) return;
      }
      if (!isCurrent(request)) return;
      changes.current += 1;
      for (const item of items) seenVersions.current.set(item.event_id,
        Math.max(seenVersions.current.get(item.event_id) ?? 0, item.version));
      setState(old => {
        if (old.account !== key) return old;
        const next = normalize(old.items);
        const count = next.filter((item, index) => old.items[index].unread && !item.unread).length;
        return { ...old, items: next, unread: Math.max(0, old.unread - count) };
      });
    } catch (problem) {
      if (isCurrent(request)) setState(old => old.account === key ? { ...old,
        error: readableError(problem, "Could not mark registration updates as read."),
      } : old);
    } finally {
      requests.current.delete(request);
      if (currentAccount.current === key && !request.signal.aborted) items.forEach(item => marking.current.delete(`${item.event_id}:${item.version}`));
    }
  }, [controller, isCurrent, key, normalize, tenantId]);

  const clearNotice = useCallback(() => setState(old => ({ ...old, notice: null, error: null })), []);
  const current = state.account === key ? state : empty(key);
  return { ...current, queue, refresh, loadMore, markSeen, clearNotice };
}
