import { operationsQueueFromUrl } from "./admin-operations-overview.ts";

export interface OperationsNavigation {
  queue: string | null;
  workPage?: number;
}

function workPage(value: number): number {
  return Number.isSafeInteger(value) && value >= 1 ? Math.min(value, 100_000) : 1;
}

/** A queue unfolds its records in Overview; retired Work-only bookmarks show Overview. */
export function operationsNavigation(href: string): OperationsNavigation {
  const value = new URL(href, "http://localhost").searchParams.get("ops_work_page") ?? "1";
  return { queue: operationsQueueFromUrl(href), workPage: /^\d+$/.test(value) ? workPage(Number(value)) : 1 };
}

/** Preserve same-queue record links, and retire the old inventory and panel state. */
export function operationsNavigationUrl(href: string, location: OperationsNavigation): string {
  const url = new URL(href, "http://localhost");
  const previous = operationsQueueFromUrl(href);
  if (location.queue) url.searchParams.set("ops_queue", location.queue);
  else url.searchParams.delete("ops_queue");
  const next = operationsQueueFromUrl(url.href);
  if (!next) url.searchParams.delete("ops_queue");
  const page = workPage(location.workPage ?? operationsNavigation(href).workPage ?? 1);
  if (page > 1) url.searchParams.set("ops_work_page", String(page));
  else url.searchParams.delete("ops_work_page");
  for (const key of ["ops_work", "ops_area", "ops_lens", "ops_issues", "ops_source_page"]) url.searchParams.delete(key);
  if (!next || next !== previous) {
    for (const key of ["ops_record", "ops_offset", "ops_scope"]) url.searchParams.delete(key);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
