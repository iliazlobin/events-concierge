import type { AdminOverview, AdminSourceHealthList } from "./admin-types.ts";

export interface OperatingEvidence {
  sources: AdminSourceHealthList | null;
  catalog: AdminOverview | null;
}
export interface OperatingSummaryItem {
  id: "sources" | "catalog";
  label: string;
  value: string;
  detail?: string;
}

const count = (value: number) => value.toLocaleString("en-US");

/** Current inventory is separate from the chart's historical collection observations. */
export function operatingSummary(evidence: OperatingEvidence): OperatingSummaryItem[] {
  const scheduled = evidence.sources?.sources.filter(source => source.enabled && !source.retired_at && source.freshness_state !== "not_scheduled");
  const fresh = scheduled?.filter(source => source.freshness_state === "ok").length ?? 0;
  return [
    {
      id: "sources", label: "Fresh sources",
      value: scheduled ? scheduled.length ? `${count(fresh)} / ${count(scheduled.length)}` : "No scheduled sources" : "Unknown",
      detail: scheduled ? "Within freshness policy · enabled, scheduled sources" : "Source snapshot unavailable",
    },
    {
      id: "catalog", label: "Catalog events",
      value: evidence.catalog ? count(evidence.catalog.summary.catalog_events) : "Unknown",
      detail: evidence.catalog ? "Upcoming unique events · cancelled events and fixtures excluded" : "Catalog snapshot unavailable",
    },
  ];
}
