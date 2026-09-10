import type { CollectionTrendSeries } from "./admin-collection-trends.ts";

export type SourceHistoryWindow = 24 | 168 | 720 | 2160;
export type SourceHistoryMetric = "records" | "runs";
export const DEFAULT_SOURCE_HISTORY_WINDOW: SourceHistoryWindow = 720;

export function sourceHistoryWindow(href: string): SourceHistoryWindow {
  const value = new URL(href, "http://localhost").searchParams.get("source_window");
  return value === "24" ? 24 : value === "168" ? 168 : value === "2160" ? 2160 : DEFAULT_SOURCE_HISTORY_WINDOW;
}
export function sourceHistoryWindowUrl(href: string, hours: SourceHistoryWindow): string {
  const url = new URL(href, "http://localhost");
  if (hours === 24 || hours === 168 || hours === 2160) url.searchParams.set("source_window", String(hours));
  else url.searchParams.delete("source_window");
  return `${url.pathname}${url.search}${url.hash}`;
}
/** All choices divide the window exactly and stay within the API's 120-interval limit. */
export function sourceHistoryBucketHours(hours: SourceHistoryWindow): number {
  return hours === 24 ? 1 : hours === 168 ? 2 : hours === 2160 ? 24 : 6;
}

/** A vertex is one returned measurement; horizontal distance always represents elapsed time. */
export function sourceHistoryPlot(series: CollectionTrendSeries, metric: SourceHistoryMetric) {
  const buckets = series.buckets;
  const start = Date.parse(buckets[0]?.at ?? "");
  const end = Date.parse(series.generatedAt);
  const maximum = Math.max(0, ...buckets.flatMap(bucket => metric === "records" ? [bucket.published, bucket.collected] : [bucket.runs, bucket.failed]));
  const points = buckets.map((bucket, index) => {
    const primary = metric === "records" ? bucket.published : bucket.runs;
    const secondary = metric === "records" ? bucket.collected : bucket.failed;
    const x = end > start ? (Date.parse(bucket.at) - start) / (end - start) * 1000 : 0;
    return { index, at: bucket.at, x, primary, secondary, primaryY: 190 - primary / (maximum || 1) * 180, secondaryY: 190 - secondary / (maximum || 1) * 180 };
  });
  const path = (metric: "primaryY" | "secondaryY") => points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(3)},${point[metric].toFixed(3)}`).join(" ");
  return { maximum, points, primaryPath: path("primaryY"), secondaryPath: path("secondaryY") };
}
