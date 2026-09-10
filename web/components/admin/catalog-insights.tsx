"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getAdminSourceDetail, getAdminThroughput } from "@/lib/admin-api";
import type { AdminCatalogListingPage, AdminRunFilters } from "@/lib/admin-types";
import { collectionBucketRuns, collectionTrendTimestamp, fleetCollectionSeries, sourceCollectionSeries, type CollectionTrendWindow } from "@/lib/admin-collection-trends";
import { sourceHistoryPlot } from "@/lib/admin-source-history";
import { useAdminSnapshot } from "./use-admin-snapshot";
import { int } from "./console-kit";
import { CatalogCollectionStatus } from "./catalog-collection-status";
import styles from "./catalog-insights.module.css";

interface Props {
  page: AdminCatalogListingPage | null;
  sourceKey: string;
  refreshToken: string | null;
  onOpenRuns: (filters: AdminRunFilters) => void;
  onOpenSource?: (sourceKey: string) => void;
}
const WINDOWS = [{ value: 24, label: "24h" }, { value: 168, label: "7d" }, { value: 720, label: "30d" }] as const;
const readWindow = (): CollectionTrendWindow => {
  const value = new URL(window.location.href).searchParams.get("catalog_trend");
  return value === "24" ? 24 : value === "168" ? 168 : 720;
};

export function CatalogInsights({ page, sourceKey, refreshToken, onOpenRuns, onOpenSource }: Props) {
  const [hours, setHours] = useState<CollectionTrendWindow>(720);
  const [activeAt, setActiveAt] = useState<string | null>(null);
  const refs = useRef<Array<SVGGElement | null>>([]);
  useEffect(() => {
    const read = () => { setHours(readWindow()); setActiveAt(null); };
    read(); window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const selectWindow = (next: CollectionTrendWindow) => {
    const url = new URL(window.location.href);
    if (next === 720) url.searchParams.delete("catalog_trend"); else url.searchParams.set("catalog_trend", String(next));
    const href = `${url.pathname}${url.search}${url.hash}`;
    if (href !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(window.history.state, "", href);
    setHours(next); setActiveAt(null);
  };
  const load = useCallback(async (signal: AbortSignal) => {
    const bucketHours = hours === 24 ? 1 : hours === 168 ? 6 : 24;
    // Changing Refresh invalidates history as well as the current event projection.
    void refreshToken;
    if (sourceKey) {
      const detail = await getAdminSourceDetail(sourceKey, hours, false, signal, bucketHours);
      if (detail.source.source_key !== sourceKey || detail.window.hours !== hours || detail.window.bucket_hours !== bucketHours) throw new Error("History scope mismatch");
      return sourceCollectionSeries(detail, hours);
    }
    const data = await getAdminThroughput(hours, bucketHours, signal);
    if (data.window_hours !== hours || data.bucket_hours !== bucketHours) throw new Error("History scope mismatch");
    return fleetCollectionSeries(data, hours);
  }, [hours, sourceKey, refreshToken]);
  const history = useAdminSnapshot(load);
  const series = history.failed || history.loading ? null : history.data;
  const plot = series ? sourceHistoryPlot(series, "records") : null;
  const index = Math.max(0, activeAt ? series?.buckets.findIndex(bucket => bucket.at === activeAt) ?? 0 : (series?.buckets.length ?? 1) - 1);
  const active = series?.buckets[index];
  return <section className={styles.overview} aria-label="Catalog overview">
    <dl className={styles.metrics} aria-live="polite">
      <div><dt>Matching events</dt><dd>{page ? int(page.total) : "—"}</dd></div>
      <div><dt>Upcoming</dt><dd>{page ? int(page.upcoming_total) : "—"}</dd></div>
      <div><dt>Sources</dt><dd>{page ? int(page.source_count) : "—"}</dd></div>
    </dl>
    <section className={styles.trend} aria-label="Catalog publication trend">
      <div className={styles.trendHeading}><h2>Published records</h2><div role="group" aria-label="Catalog trend window">{WINDOWS.map(option => <button type="button" key={option.value} aria-pressed={hours === option.value} onClick={() => selectWindow(option.value)}>{option.label}</button>)}</div></div>
      <p>Collection output · {sourceKey ? series?.sourceName ?? sourceKey : "All sources"} · repeated publication included</p>
      {series && plot && series.buckets.length ? <>
        <svg viewBox="0 0 1000 200" preserveAspectRatio="none" className={styles.chart} role="group" aria-label="Published records timeline">
          {[10, 100, 190].map(y => <line key={y} x1="0" x2="1000" y1={y} y2={y} className={styles.grid} vectorEffect="non-scaling-stroke" />)}
          <path d={plot.primaryPath} className={styles.line} vectorEffect="non-scaling-stroke" />
          {plot.points.map(point => {
            const bucket = series.buckets[point.index]; const scope = collectionBucketRuns(series, bucket);
            const left = point.index ? (plot.points[point.index - 1].x + point.x) / 2 : 0;
            const right = (plot.points[point.index + 1]?.x ?? 2000 - point.x) / 2 + point.x / 2;
            return <g key={point.at} ref={element => { refs.current[point.index] = element; }} role="button" tabIndex={index === point.index ? 0 : -1}
              aria-label={`Inspect published records from runs started ${collectionTrendTimestamp(point.at)}`} data-active={index === point.index}
              onMouseEnter={() => setActiveAt(point.at)} onFocus={() => setActiveAt(point.at)} onClick={() => scope && onOpenRuns(scope)}
              onKeyDown={event => {
                if ((event.key === "Enter" || event.key === " ") && scope) { event.preventDefault(); onOpenRuns(scope); return; }
                const next = event.key === "ArrowRight" ? Math.min(plot.points.length - 1, point.index + 1) : event.key === "ArrowLeft" ? Math.max(0, point.index - 1) : event.key === "Home" ? 0 : event.key === "End" ? plot.points.length - 1 : null;
                if (next !== null) { event.preventDefault(); refs.current[next]?.focus(); }
              }}>
              <title>{collectionTrendTimestamp(point.at)} · {int(bucket.published)} published · {int(bucket.runs)} runs</title>
              <rect x={left} y="0" width={right - left} height="200" className={styles.target} />
              <circle cx={point.x} cy={point.primaryY} r="4" className={styles.dot} />
            </g>;
          })}
        </svg>
        <div className={styles.axis}><span>{collectionTrendTimestamp(series.buckets[0].at).slice(5, 16)} UTC</span><span>0–{int(plot.maximum)}</span><span>{collectionTrendTimestamp(series.generatedAt).slice(5, 16)} UTC</span></div>
        <div className={styles.readout}><span>{active ? collectionTrendTimestamp(active.at) : ""}</span><strong>{active ? `${int(active.published)} published · ${int(active.runs)} runs` : ""}</strong></div>
      </> : <div className={styles.empty}>{history.failed ? <><span>Collection history unavailable.</span><button type="button" onClick={() => void history.refresh()}>Retry history</button></> : history.loading ? "Loading collection history…" : "No recorded intervals."}</div>}
    </section>
    {onOpenSource ? <CatalogCollectionStatus sourceKey={sourceKey} refreshToken={refreshToken} onOpenSource={onOpenSource} /> : null}
  </section>;
}
