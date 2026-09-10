import { adminHistoryUrl } from "./admin-history.ts";

export type CatalogDateScope = "all" | "upcoming" | "past";
export type CatalogPriceScope = "all" | "free" | "paid" | "unknown";

export interface CatalogLocation {
  sourceKey: string;
  runKey?: string;
  configurationKey?: string;
  sourceQuery: string;
  sourcePage: number;
  dateScope: CatalogDateScope;
  priceScope: CatalogPriceScope;
  query: string;
  eventId: string | null;
  afterStart: string | null;
  afterId: string | null;
}

type CatalogLocationInput = Omit<CatalogLocation, "dateScope" | "priceScope" | "sourceQuery" | "sourcePage"> &
  Partial<Pick<CatalogLocation, "dateScope" | "priceScope" | "sourceQuery" | "sourcePage">>;

const SOURCE_KEY = /^[a-z0-9][a-z0-9-]{1,79}$/;
const RUN_KEY = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$/;
const EVENT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})[Tt ](?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/;

function validTimestamp(value: string | null): value is string {
  if (!value || value.length > 64) return false;
  const parts = TIMESTAMP.exec(value);
  if (!parts || !Number.isFinite(Date.parse(value))) return false;
  // Preserve PostgreSQL microseconds, while rejecting dates Date.parse silently rolls forward.
  const year = Number(parts[1]); const month = Number(parts[2]); const day = Number(parts[3]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1];
}

function normalize(input: CatalogLocationInput): CatalogLocation {
  const validCursor = validTimestamp(input.afterStart) && Boolean(input.afterId && EVENT_ID.test(input.afterId));
  const query = (value: string) => value.replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 160);
  return {
    sourceKey: SOURCE_KEY.test(input.sourceKey) ? input.sourceKey : "",
    ...(SOURCE_KEY.test(input.sourceKey) && input.runKey && RUN_KEY.test(input.runKey) ? { runKey: input.runKey } : {}),
    ...(input.configurationKey && SOURCE_KEY.test(input.configurationKey) ? { configurationKey: input.configurationKey } : {}),
    // Source selection is now an optional filter on one global list.
    sourceQuery: "",
    sourcePage: 1,
    dateScope: input.dateScope === "upcoming" || input.dateScope === "past" ? input.dateScope : "all",
    priceScope: input.priceScope === "free" || input.priceScope === "paid" || input.priceScope === "unknown" ? input.priceScope : "all",
    query: query(input.query),
    eventId: input.eventId && EVENT_ID.test(input.eventId) ? input.eventId.toLowerCase() : null,
    afterStart: validCursor ? input.afterStart : null,
    afterId: validCursor ? input.afterId!.toLowerCase() : null,
  };
}

/** Retain existing store record links without coupling investigations to the retired diagram. */
export function catalogLocationFromUrl(href: string): CatalogLocation {
  const params = new URL(href).searchParams;
  return normalize({
    sourceKey: params.get("store_source") ?? "",
    runKey: params.get("store_run") ?? undefined,
    configurationKey: params.get("catalog_config") ?? undefined,
    dateScope: params.get("store_dates") as CatalogDateScope,
    priceScope: params.get("store_price") as CatalogPriceScope,
    query: params.get("store_query") ?? "",
    eventId: params.get("store_event"),
    afterStart: params.get("store_after_start"),
    afterId: params.get("store_after_id"),
  });
}

export function catalogLocationUrl(location: CatalogLocationInput, href: string): string {
  const url = new URL(adminHistoryUrl({ tab: "catalog", sourceKey: null }, href), href);
  const next = normalize(location);
  const values = {
    store_source: next.sourceKey, store_source_query: "",
    store_run: next.runKey,
    store_source_page: "",
    store_dates: next.dateScope === "all" ? "" : next.dateScope,
    store_price: next.priceScope === "all" ? "" : next.priceScope,
    store_query: next.query, store_event: next.eventId,
    store_after_start: next.afterStart, store_after_id: next.afterId,
  };
  for (const [key, value] of Object.entries(values)) {
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
