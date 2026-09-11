"use client";

import { useCallback } from "react";
import { getAllAdminSources } from "@/lib/admin-api";
import { DEFAULT_ADMIN_SOURCE_FILTERS } from "@/lib/admin-history";
import { useAdminSnapshot } from "./use-admin-snapshot";

/** Current global source evidence, independent of the investigation table's filters. */
export function useSourceOperations(includeFixtures: boolean, refreshVersion: number | string | null) {
  const load = useCallback(async (signal: AbortSignal) => {
    void refreshVersion;
    const result = await getAllAdminSources({ ...DEFAULT_ADMIN_SOURCE_FILTERS, includeFixtures }, { signal });
    if (result.truncated || result.page.items.length !== result.page.total) throw new Error("Source registry read incomplete");
    return { ...result.page, read_at: new Date().toISOString() };
  }, [includeFixtures, refreshVersion]);
  return useAdminSnapshot(load);
}
