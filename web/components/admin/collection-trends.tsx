"use client";

import { ChevronDown, RefreshCw, X } from "lucide-react";
import { Fragment, useCallback, useEffect, useId, useRef, useState } from "react";
import { getAdminSourceDetail, getAdminThroughput } from "@/lib/admin-api";
import type { AdminRunFilters, AdminSourceHealthList } from "@/lib/admin-types";
import {
  collectionBucketRuns, collectionRangeRuns, collectionRunExpansion, collectionRunExpansionUrl, collectionTrendLocation, collectionTrendTimestamp, collectionTrendTotals, collectionTrendUrl,
  fleetCollectionSeries, sourceCollectionSeries,
  type CollectionRunExpansion, type CollectionTrendLocation, type CollectionTrendSeries,
} from "@/lib/admin-collection-trends";
import { compact, int } from "./console-kit";
import { useAdminSnapshot } from "./use-admin-snapshot";
import { RunsView, type RunsViewProps } from "./runs-view";
import styles from "./collection-trends.module.css";

export interface CollectionTrendsProps {
  refreshVersion?: number;
  sources: AdminSourceHealthList | null;
  onOpenSource: RunsViewProps["onOpenSource"];
  onOpenCatalog: RunsViewProps["onOpenCatalog"];
}
const WINDOWS = [{ value: 24, label: "24h" }, { value: 168, label: "7d" }, { value: 720, label: "30d" }] as const;
const initial = (): CollectionTrendLocation => collectionTrendLocation(typeof window === "undefined" ? "/admin" : window.location.href);

export function CollectionTrends({ refreshVersion = 0, sources, onOpenSource, onOpenCatalog }: CollectionTrendsProps) {
  const [location, setLocation] = useState<CollectionTrendLocation>(() => collectionTrendLocation("/admin"));
  const [activeBucket, setActiveBucket] = useState<string | null>(null);
  const bars = useRef<Array<HTMLButtonElement | null>>([]);
  const detailId = useId();
  const expansionId = useId();
  const expandedRef = useRef<HTMLElement>(null);
  const expansionTrigger = useRef<HTMLButtonElement | null>(null);
  const [expansion, setExpansion] = useState<CollectionRunExpansion | null>(null);
  useEffect(() => {
    const read = () => { setLocation(initial()); setExpansion(collectionRunExpansion(window.location.href)); setActiveBucket(null); };
    read(); window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const navigate = (next: CollectionTrendLocation) => {
    const scopeChanged = next.sourceKey !== location.sourceKey || next.windowHours !== location.windowHours;
    const trendHref = collectionTrendUrl(window.location.href, next);
    const href = scopeChanged ? collectionRunExpansionUrl(trendHref, null) : trendHref;
    if (href !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(window.history.state, "", href);
    setLocation(next); setActiveBucket(null);
    if (scopeChanged) setExpansion(null);
  };
  const updateExpansion = (next: CollectionRunExpansion | null) => {
    const href = collectionRunExpansionUrl(window.location.href, next);
    if (href !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(window.history.state, "", href);
    setExpansion(next);
  };
  const closeExpansion = () => {
    updateExpansion(null);
    window.requestAnimationFrame(() => expansionTrigger.current?.focus({ preventScroll: true }));
  };
  const openRuns = (scope: AdminRunFilters, trigger: HTMLButtonElement, kind: CollectionRunExpansion["kind"] = "interval") => {
    if (!scope.startedAfter || !scope.startedBefore) return;
    expansionTrigger.current = trigger;
    if (expansion?.startedAfter === scope.startedAfter && expansion.startedBefore === scope.startedBefore && expansion.kind === kind) { closeExpansion(); return; }
    updateExpansion({ startedAfter: scope.startedAfter, startedBefore: scope.startedBefore, kind, status: "" });
    window.requestAnimationFrame(() => expandedRef.current?.scrollIntoView({ block: "nearest", behavior: "instant" }));
  };
  const load = useCallback(async (signal: AbortSignal): Promise<CollectionTrendSeries> => {
    if (location.sourceKey) {
      const data = await getAdminSourceDetail(location.sourceKey, location.windowHours, false, signal);
      if (data.source.source_key !== location.sourceKey || data.window.hours !== location.windowHours) throw new Error("Collection source did not match the requested scope.");
      return sourceCollectionSeries(data, location.windowHours);
    }
    const data = await getAdminThroughput(location.windowHours, location.windowHours === 24 ? 1 : 24, signal);
    if (data.window_hours !== location.windowHours) throw new Error("Collection window did not match the requested scope.");
    return fleetCollectionSeries(data, location.windowHours);
  }, [location.sourceKey, location.windowHours]);
  const snapshot = useAdminSnapshot(load, refreshVersion);
  const series = !snapshot.loading && !snapshot.failed && !snapshot.authorizationDenied ? snapshot.data : null;
  const displayedScope = series ? collectionRangeRuns(series) : null;
  const activity = series?.buckets.filter(bucket => bucket.runs > 0) ?? [];
  const totals = series ? collectionTrendTotals(series.buckets) : null;
  const options = [...(sources?.sources ?? [])].sort((a, b) => a.display_name.localeCompare(b.display_name));
  const missingOption = location.sourceKey && !options.some(source => source.source_key === location.sourceKey);
  const index = Math.max(0, activeBucket ? activity.findIndex(bucket => bucket.at === activeBucket) : activity.length - 1);
  const active = activity[index];
  const selectedScope = series && active ? collectionBucketRuns(series, active) : null;
  const first = series?.buckets[0];
  const last = series?.buckets.at(-1);
  const end = series && last ? collectionBucketRuns(series, last)?.startedBefore : null;
  const records = location.metric === "records";
  const maximum = Math.max(1, ...(series?.buckets.flatMap(bucket => records ? [bucket.collected, bucket.published] : [bucket.runs, bucket.failed]) ?? []));
  const chartHeight = (value: number) => `${value > 0 ? Math.max(2, value / maximum * 100) : 0}%`;
  const expandedBucket = expansion?.kind === "interval" && series ? activity.find(bucket => {
    const scope = collectionBucketRuns(series, bucket);
    return scope?.startedAfter === expansion.startedAfter && scope.startedBefore === expansion.startedBefore;
  }) : null;
  const expandedRuns = expansion ? <section id={expansionId} ref={expandedRef} className={styles.expansion} aria-label={`Runs in selected ${expansion.kind}`}>
    <header className={styles.expansionHeader}><div><h3>{expansion.kind === "range" ? "Runs in this range" : "Runs in this interval"}</h3><p>{collectionTrendTimestamp(expansion.startedAfter)} → {collectionTrendTimestamp(expansion.startedBefore)}</p>{expansion.kind === "interval" && !expandedBucket ? <p>Saved interval · exact bounds retained after chart refresh</p> : null}</div>
      <button type="button" onClick={closeExpansion} aria-label="Collapse runs"><X aria-hidden="true" />Collapse</button></header>
    <RunsView embedded key={`${location.sourceKey}|${location.windowHours}|${expansion.startedAfter}|${expansion.startedBefore}|${expansion.kind}`}
      filters={{ sourceKey: location.sourceKey, windowHours: location.windowHours, includeFixtures: false, status: expansion.status,
        startedAfter: expansion.startedAfter, startedBefore: expansion.startedBefore }}
      onFiltersChange={filters => updateExpansion({ ...expansion, status: filters.status })}
      onOpenSource={onOpenSource} onOpenCatalog={onOpenCatalog} refreshVersion={refreshVersion} />
  </section> : null;

  return <section className={styles.section} data-metric={location.metric} aria-label="Collection trends">
    <div className={styles.header}><h2>Collection activity</h2>
      <div className={styles.headerActions}><button type="button" className={styles.browse} disabled={!displayedScope} aria-expanded={expansion?.kind === "range"} aria-controls={expansion?.kind === "range" ? expansionId : undefined} onClick={event => displayedScope && openRuns(displayedScope, event.currentTarget, "range")}>Browse runs<ChevronDown aria-hidden="true" /></button>
        <button type="button" className={styles.refresh} aria-label="Refresh trends" disabled={snapshot.loading} onClick={() => void snapshot.refresh()}><RefreshCw aria-hidden="true" /></button></div></div>
    <div className={styles.controls}>
      <label className={styles.source}><span>Source</span><select aria-label="Collection source" value={location.sourceKey} onChange={event => navigate({ ...location, sourceKey: event.target.value })}>
        <option value="">All sources</option>
        {missingOption ? <option value={location.sourceKey}>{series?.sourceName ?? location.sourceKey}</option> : null}
        {options.map(source => <option key={source.source_key} value={source.source_key}>{source.display_name}</option>)}
      </select></label>
      <div className={styles.segments} role="group" aria-label="Trend metric">{(["runs", "records"] as const).map(metric => <button key={metric} type="button" aria-pressed={location.metric === metric} onClick={() => navigate({ ...location, metric })}>{metric === "runs" ? "Runs" : "Records"}</button>)}</div>
      <div className={styles.segments} role="group" aria-label="Trend window">{WINDOWS.map(option => <button key={option.value} type="button" aria-pressed={location.windowHours === option.value} onClick={() => navigate({ ...location, windowHours: option.value })}>{option.label}</button>)}</div>
    </div>
    {snapshot.failed ? <div className={styles.empty} role="alert"><p>{snapshot.authorizationDenied ? "Collection trends are unavailable for this operator session." : "Collection trends could not be loaded. Previous results are hidden."}</p><button type="button" onClick={() => void snapshot.refresh()}>Retry trends</button></div>
      : !series ? <p className={styles.empty} role="status">Loading collection trends…</p>
      : <>
        {series.buckets.length ? <div className={styles.summary}>
          {records ? <><div><span>Collected</span><strong>{int(totals!.collected)}</strong></div><div><span>Published</span><strong>{int(totals!.published)}</strong></div><div><span>Runs</span><strong>{int(totals!.runs)}</strong></div></>
            : <><div><span>Runs</span><strong>{int(totals!.runs)}</strong></div><div><span>Completed successfully</span><strong>{totals!.completedSuccessRate === null ? "—" : `${(totals!.completedSuccessRate * 100).toFixed(1)}%`}</strong><small>{int(totals!.succeeded)} of {int(totals!.completed)} completed runs</small></div><div><span>Failed runs</span><strong>{int(totals!.failed)}</strong></div></>}
        </div> : null}
        {first && end ? <p className={styles.range}>{collectionTrendTimestamp(first.at)} → {collectionTrendTimestamp(end)} · {series.bucketHours}h intervals · fixtures excluded</p> : null}
        {series.buckets.length && !activity.length ? <p className={styles.zero}>No runs in these intervals.</p> : null}
        {activity.length ? <>
          <div className={styles.legend}><span><i className={styles.primary} />{records ? "Collected" : "All runs"}</span><span><i className={styles.secondary} />{records ? "Published" : "Failed runs"}</span><small>Scale 0–{compact(maximum)}</small></div>
          <div className={styles.plot} role="group" aria-label="Collection timeline" style={{ gridTemplateColumns: `repeat(${series.buckets.length}, minmax(0, 1fr))` }}>
            {series.buckets.map(bucket => {
              if (!bucket.runs) return <span key={bucket.at} aria-hidden="true" />;
              const position = activity.indexOf(bucket);
              const scope = collectionBucketRuns(series, bucket);
              const title = `${collectionTrendTimestamp(bucket.at)} · ${int(bucket.runs)} runs · ${int(bucket.failed)} failed · ${int(bucket.collected)} collected · ${int(bucket.published)} published`;
              return <button type="button" key={bucket.at} ref={element => { bars.current[position] = element; }} className={styles.bucket} aria-label={`Inspect runs started ${collectionTrendTimestamp(bucket.at)}`} aria-describedby={detailId} aria-expanded={expandedBucket === bucket} aria-controls={expandedBucket === bucket ? expansionId : undefined} title={title} tabIndex={position === index ? 0 : -1} disabled={!scope} data-active={position === index} onMouseEnter={() => setActiveBucket(bucket.at)} onFocus={() => setActiveBucket(bucket.at)} onClick={event => scope && openRuns(scope, event.currentTarget)} onKeyDown={event => {
                const next = event.key === "ArrowRight" ? Math.min(activity.length - 1, position + 1) : event.key === "ArrowLeft" ? Math.max(0, position - 1) : event.key === "Home" ? 0 : event.key === "End" ? activity.length - 1 : null;
                if (next !== null) { event.preventDefault(); bars.current[next]?.focus(); }
              }}><span className={styles.primary} style={{ height: chartHeight(records ? bucket.collected : bucket.runs) }} /><span className={styles.secondary} style={{ height: chartHeight(records ? bucket.published : bucket.failed) }} /></button>;
            })}
          </div>
          <div className={styles.axis}><span>{collectionTrendTimestamp(first!.at).slice(5, 16)}</span><span>{collectionTrendTimestamp(end ?? series.generatedAt).slice(5, 16)}</span></div>
          <div className={styles.detail} id={detailId}>
            {active ? <><span><time dateTime={active.at}>{collectionTrendTimestamp(active.at)}</time>{selectedScope?.startedBefore ? ` → ${collectionTrendTimestamp(selectedScope.startedBefore)}` : ""}</span><span>{int(active.runs)} runs · {int(active.succeeded)} succeeded · {int(active.failed)} failed · {int(active.collected)} collected · {int(active.published)} published</span></> : null}
          </div>
          {expansion?.kind === "range" ? expandedRuns : null}
          <section className={styles.tableDetails} aria-label="Run activity table"><div className={styles.tableScroll}><table className={styles.activityTable}><caption>Collection activity by run start time · empty intervals hidden</caption><thead><tr><th>Started (UTC)</th><th>Runs</th><th>Succeeded</th><th>Failed</th><th>Collected</th><th>Published</th></tr></thead><tbody>{activity.map(bucket => {
            const scope = collectionBucketRuns(series, bucket);
            const expanded = expandedBucket === bucket;
            return <Fragment key={bucket.at}><tr data-expanded={expanded}><td><button type="button" disabled={!scope} aria-expanded={expanded} aria-controls={expanded ? expansionId : undefined} onClick={event => scope && openRuns(scope, event.currentTarget)}>{collectionTrendTimestamp(bucket.at)}<ChevronDown aria-hidden="true" /></button></td><td>{int(bucket.runs)}</td><td>{int(bucket.succeeded)}</td><td>{int(bucket.failed)}</td><td>{int(bucket.collected)}</td><td>{int(bucket.published)}</td></tr>
              {expanded ? <tr className={styles.expandedRow}><td colSpan={6}><div className={styles.expandedContent}>{expandedRuns}</div></td></tr> : null}</Fragment>;
          })}</tbody></table></div></section>
        </> : !series.buckets.length ? <p className={styles.empty}>No time intervals were returned for this scope.</p> : null}
        {expansion && (expansion.kind === "interval" ? !expandedBucket : !activity.length) ? expandedRuns : null}
        <p className={styles.note}>Totals use the intervals shown. Published records can repeat across runs; they are not unique catalog growth. Outcomes reflect this snapshot.</p>
      </>}
  </section>;
}
