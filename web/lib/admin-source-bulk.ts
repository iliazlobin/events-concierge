import type { AdminSource, AdminSourceEnabledTarget } from "@/lib/admin-types";
import { adminSourceIsRetired } from "./admin-source-lifecycle.ts";

const supersededSourceKeys = new Set([
  "alameda-county-library-fremont-events",
  "sccld-milpitas-events",
  "sccld-saratoga-events",
  "smcl-millbrae-events",
]);

function sourceIsBulkEligible(source: AdminSource): boolean {
  return source.review_status === "reviewed"
    && !adminSourceIsRetired(source)
    && !supersededSourceKeys.has(source.source_key);
}

export function selectableAdminSources(sources: AdminSource[]): AdminSource[] {
  return sources.filter(sourceIsBulkEligible);
}

export function adminSourceEnabledTargets(
  sources: AdminSource[],
  enabled: boolean,
): AdminSourceEnabledTarget[] {
  return sources
    .filter((source) => sourceIsBulkEligible(source) && source.enabled !== enabled)
    .map((source) => ({
      source_key: source.source_key,
      expected_revision: source.source_revision,
    }));
}
