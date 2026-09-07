import type { CatalogFilters, CatalogSort } from "@/lib/types";

export function emptyCatalogFilters(sort: CatalogSort = "soonest"): CatalogFilters {
  return {
    query: "",
    sort,
    datePreset: "all",
    customStart: "",
    customEnd: "",
    dateRanges: [],
    sourceKeys: [],
    city: "",
    cities: [],
    locationScopes: [],
    price: "any",
    priceComparison: "any",
    priceMinDollars: "",
    priceMaxDollars: "",
    availability: "any",
    topics: [],
  };
}

export const DEFAULT_CATALOG_FILTERS: CatalogFilters = emptyCatalogFilters();

/**
 * What a brand new session starts on.
 *
 * Deliberately not `emptyCatalogFilters()`: an unfiltered catalog is every event in every city
 * forever, which is a worse first screen than a real one. Reset still goes to the empty state —
 * "show me everything" has to remain reachable, and it is a different intent from "start here".
 */
export function initialCatalogFilters(sort: CatalogSort = "soonest"): CatalogFilters {
  return {
    ...emptyCatalogFilters(sort),
    datePreset: "week",
    city: "sanfrancisco",
    cities: ["sanfrancisco"],
  };
}

/**
 * Canonical identity of a catalog request.
 *
 * Two filter objects that serialize the same describe the same request, however
 * they were constructed. Browsing state that is not a filter — which card is
 * expanded, which view is open — must not appear here, or opening a card would
 * read as a new request and discard the list the reader is looking at.
 *
 * `includeSort` is false for callers that only count events: ordering cannot
 * change a total.
 *
 * `includeDateWindow` is false for the calendar's day cache. A day's count is
 * decided by that day alone, so the same filters asked over a wider window
 * return the identical count for every day the narrower window held. Dropping
 * the window from the key is what lets one six-month read serve the month and
 * week grids inside it instead of re-reading the catalog for each.
 */
export function catalogFilterKey(
  filters: CatalogFilters,
  {
    includeSort = true,
    includeDateWindow = true,
  }: { includeSort?: boolean; includeDateWindow?: boolean } = {},
): string {
  const cities = filters.cities?.length
    ? filters.cities
    : filters.city
      ? [filters.city]
      : [];
  return JSON.stringify([
    filters.query.trim(),
    includeSort ? filters.sort ?? "soonest" : null,
    includeDateWindow ? filters.datePreset : null,
    includeDateWindow ? filters.customStart : null,
    includeDateWindow ? filters.customEnd : null,
    includeDateWindow
      ? (filters.dateRanges ?? [])
        .map((range) => `${range.start}..${range.end}`)
        .sort()
      : null,
    [...new Set(filters.sourceKeys ?? [])].sort(),
    [...new Set(cities)].sort(),
    [...new Set(filters.locationScopes ?? [])].sort(),
    [...new Set(filters.topics ?? [])].sort(),
    filters.price,
    filters.priceComparison ?? "any",
    filters.priceMinDollars ?? "",
    filters.priceMaxDollars ?? "",
    filters.availability ?? "any",
  ]);
}
