import type { AdminTab } from "@/lib/admin-types";

const ADMIN_TABS = new Set<AdminTab>([
  "overview",
  "pipeline",
  "sources",
  "runs",
  "commands",
]);
const SOURCE_KEY = /^[a-z0-9][a-z0-9-]{1,79}$/;

export interface AdminHistoryLocation {
  tab: AdminTab;
  sourceKey: string | null;
}

export function adminHistoryLocationFromUrl(currentHref: string): AdminHistoryLocation {
  const url = new URL(currentHref);
  const requestedTab = url.searchParams.get("tab");
  const requestedSource = url.searchParams.get("source");
  const sourceKey = requestedSource && SOURCE_KEY.test(requestedSource)
    ? requestedSource
    : null;
  return {
    tab: sourceKey
      ? "sources"
      : requestedTab && ADMIN_TABS.has(requestedTab as AdminTab)
        ? requestedTab as AdminTab
        : "overview",
    sourceKey,
  };
}

export function adminHistoryUrl(
  location: AdminHistoryLocation,
  currentHref: string,
): string {
  const url = new URL(currentHref);
  if (location.tab === "overview") url.searchParams.delete("tab");
  else url.searchParams.set("tab", location.tab);
  if (location.sourceKey) url.searchParams.set("source", location.sourceKey);
  else url.searchParams.delete("source");
  return `${url.pathname}${url.search}${url.hash}`;
}
