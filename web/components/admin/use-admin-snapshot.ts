"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";

/** Active-view snapshots share cancellation and explicit mutation invalidation. */
export function useAdminSnapshot<T>(
  load: (signal: AbortSignal) => Promise<T>,
  refreshVersion = 0,
) {
  const [snapshot, setSnapshot] = useState<{ scope: typeof load; value: T } | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [authorizationDenied, setAuthorizationDenied] = useState(false);
  const active = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    active.current?.abort();
    const request = new AbortController();
    active.current = request;
    setLoading(true);
    setFailed(false);
    try {
      const next = await load(request.signal);
      if (!request.signal.aborted) {
        setSnapshot({ scope: load, value: next });
        setAuthorizationDenied(false);
      }
    } catch (error) {
      if (!request.signal.aborted) {
        setFailed(true);
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          setSnapshot(null);
          setAuthorizationDenied(true);
        }
      }
    } finally {
      if (!request.signal.aborted) setLoading(false);
    }
  }, [load]);
  useEffect(() => {
    void refresh();
    return () => active.current?.abort();
  }, [load, refresh, refreshVersion]);
  // Scope is checked during render, so a new source/window never briefly displays old evidence.
  const data = snapshot?.scope === load ? snapshot.value : null;
  return { data, loading, failed, refresh, authorizationDenied };
}
