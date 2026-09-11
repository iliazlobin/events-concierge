"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import {
  getAdminWorkRecords, WORK_ERROR_MAX_OFFSET,
  type WorkErrorRecord, type WorkRecordQueue, type WorkRecordScope,
} from "@/lib/admin-work-errors";
import { WORK_PREVIEW_LIMIT } from "@/lib/admin-work-preview";

interface FeedState {
  key: string;
  version: number;
  items: WorkErrorRecord[];
  total: number | null;
  generatedAt: string | null;
  nextOffset: number;
  loading: boolean;
  loadingMore: boolean;
  failed: boolean;
  moreFailed: boolean;
  authorizationDenied: boolean;
  hasMore: boolean;
  needsRefresh: boolean;
}

const EMPTY_ITEMS: WorkErrorRecord[] = [];

function emptyFeed(key: string, version: number): FeedState {
  return {
    key, version, items: EMPTY_ITEMS, total: null, generatedAt: null, nextOffset: 0,
    loading: false, loadingMore: false, failed: false, moreFailed: false,
    authorizationDenied: false, hasMore: false, needsRefresh: true,
  };
}

/** Keep the loaded prefix while an inspector is open; each read remains independently cancellable. */
export function useWorkRecordFeed(
  queue: WorkRecordQueue,
  scope: WorkRecordScope,
  refreshVersion: number,
  active = true,
) {
  const key = `${queue}:${scope}`;
  const [feed, setFeed] = useState(() => emptyFeed(key, refreshVersion));
  const current = useRef(feed);
  const request = useRef<AbortController | null>(null);

  const publish = useCallback((next: FeedState) => {
    current.current = next;
    setFeed(next);
  }, []);

  const abort = useCallback(() => {
    request.current?.abort();
    request.current = null;
  }, []);

  const readPage = useCallback(async (append: boolean, retry = false) => {
    if (!active) return;
    const previous = current.current;
    if (append && (
      request.current || previous.key !== key || previous.version !== refreshVersion
      || previous.needsRefresh || previous.failed || previous.authorizationDenied
      || !previous.hasMore || (previous.moreFailed && retry !== true)
    )) return;

    abort();
    const controller = new AbortController();
    request.current = controller;
    const offset = append ? previous.nextOffset : 0;
    const started = append
      ? { ...previous, loadingMore: true, moreFailed: false }
      : { ...emptyFeed(key, refreshVersion), loading: true };
    publish(started);

    try {
      const page = await getAdminWorkRecords(queue, scope, offset, null, controller.signal, WORK_PREVIEW_LIMIT);
      if (controller.signal.aborted || request.current !== controller) return;
      if (page.offset !== offset || !Number.isSafeInteger(page.total) || page.total < 0
        || !Array.isArray(page.items) || page.items.length > WORK_PREVIEW_LIMIT) {
        throw new Error("Work records did not match the requested page.");
      }

      const seen = new Set(append ? previous.items.map(item => item.record_id) : []);
      const added = page.items.filter(item => {
        if (seen.has(item.record_id)) return false;
        seen.add(item.record_id);
        return true;
      });
      // The server offset advances over every returned row, including duplicates across snapshots.
      const nextOffset = page.offset + page.items.length;
      publish({
        ...started,
        items: append ? [...previous.items, ...added] : added,
        total: page.total,
        generatedAt: page.generated_at,
        nextOffset,
        loading: false,
        loadingMore: false,
        failed: false,
        moreFailed: false,
        authorizationDenied: false,
        hasMore: page.items.length > 0 && nextOffset < page.total && nextOffset <= WORK_ERROR_MAX_OFFSET,
        needsRefresh: false,
      });
    } catch (error) {
      if (controller.signal.aborted || request.current !== controller) return;
      const authorizationDenied = error instanceof ApiError && (error.status === 401 || error.status === 403);
      if (append && !authorizationDenied) {
        publish({ ...started, loadingMore: false, moreFailed: true });
      } else {
        publish({
          ...emptyFeed(key, refreshVersion), failed: true, authorizationDenied, needsRefresh: false,
        });
      }
    } finally {
      if (request.current === controller) request.current = null;
    }
  }, [abort, active, key, publish, queue, refreshVersion, scope]);

  const refresh = useCallback(async () => {
    abort();
    if (!active) {
      // Defer the read until the preview returns without discarding its retained prefix now.
      publish({ ...current.current, loading: false, loadingMore: false, needsRefresh: true });
      return;
    }
    await readPage(false);
  }, [abort, active, publish, readPage]);

  // Automatic scroll requests cannot retry failures; the Retry button calls loadMore(true).
  const loadMore = useCallback(async (retry = false) => {
    await readPage(true, retry);
  }, [readPage]);

  useEffect(() => {
    const previous = current.current;
    if (!active) {
      abort();
      if (previous.loading || previous.loadingMore) {
        publish({ ...previous, loading: false, loadingMore: false });
      }
    } else if (previous.key !== key || previous.version !== refreshVersion || previous.needsRefresh) {
      void readPage(false);
    }
    return abort;
  }, [abort, active, key, publish, readPage, refreshVersion]);

  // Render isolation also covers the interval before the effect cancels a previous scope's read.
  const matching = feed.key === key && feed.version === refreshVersion ? feed : null;
  const visible = matching && !(active && matching.needsRefresh) ? matching : null;
  return {
    items: visible?.items ?? EMPTY_ITEMS,
    total: visible?.total ?? null,
    generatedAt: visible?.generatedAt ?? null,
    loading: active && (!matching || matching.loading || matching.needsRefresh),
    loadingMore: active && (matching?.loadingMore ?? false),
    failed: matching?.failed ?? false,
    moreFailed: matching?.moreFailed ?? false,
    authorizationDenied: matching?.authorizationDenied ?? false,
    hasMore: visible?.hasMore ?? false,
    refresh,
    loadMore,
  };
}
