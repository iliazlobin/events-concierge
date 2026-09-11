"use client";

import { ArrowRight, ArrowUpRight, Pause, Play, RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getAdminSourceDetail } from "@/lib/admin-api";
import type { CatalogDateScope } from "@/lib/admin-catalog-navigation";
import { collectionBucketRuns, collectionTrendTimestamp, sourceCollectionSeries } from "@/lib/admin-collection-trends";
import { DEFAULT_SOURCE_HISTORY_WINDOW, sourceHistoryBucketHours, sourceHistoryPlot, sourceHistoryWindow, sourceHistoryWindowUrl, type SourceHistoryMetric, type SourceHistoryWindow } from "@/lib/admin-source-history";
import { safeHttpUrl, sourceDiagnostics, type SourceDiagnostic } from "@/lib/admin-presentation";
import { adminSourceIsRetired } from "@/lib/admin-source-lifecycle";
import type { AdminRunFilters, AdminSource, AdminSourceConfigurationUpdate, AdminSourceDetail } from "@/lib/admin-types";

import { SourceConfigurationPanel } from "./source-configuration-panel";
import { Action, Chip, Segment, age, int, kit, ms, stamp, type Tone } from "./console-kit";
import { useAdminSnapshot } from "./use-admin-snapshot";
import styles from "./source-expanded-details.module.css";

const WINDOWS = [{ value: "24", label: "24h" }, { value: "168", label: "7d" }, { value: "720", label: "30d" }, { value: "2160", label: "90d" }] as const;
type HistoryMetric = SourceHistoryMetric;
const METRICS = [{ value: "records", label: "Published / collected" }, { value: "runs", label: "Runs / failed" }] as const;
const humanize = (value: string) => value.replaceAll("_", " ");
const recordedTime = (value: string | null) => value ? stamp(value) : "Not recorded";
const recordedCount = (value: number | null) => value === null ? "Not recorded" : int(value);

export interface SourceExpandedDetailsProps {
  sourceKey: string;
  includeFixtures: boolean;
  refreshVersion: number;
  canConfigure: boolean;
  canEnable: boolean;
  canRefresh: boolean;
  refreshSubmitting: boolean;
  policyAllowed: boolean | null;
  busy: boolean;
  panel?: string;
  onPanel?: (panel: string) => void;
  onConfigurationSaved: (result: AdminSourceConfigurationUpdate) => void;
  onOpenSource: (key: string) => void;
  onOpenRuns: (filters: AdminRunFilters, runKey?: string) => void;
  onOpenCatalog: (key: string, dateScope?: CatalogDateScope) => void;
  onSetEnabled: (source: AdminSource, enabled: boolean) => void;
  onQueueRefresh: (source: AdminSource) => void;
  onSavingChange?: (saving: boolean) => void;
  onClose: () => void;
}

/** Expanded beneath its source row; every fact and write target comes from exact source evidence. */
export function SourceExpandedDetails({
  sourceKey, includeFixtures, refreshVersion, canConfigure, canEnable, canRefresh, refreshSubmitting, policyAllowed, busy, panel, onPanel,
  onConfigurationSaved, onOpenSource, onOpenRuns, onOpenCatalog, onSetEnabled, onQueueRefresh, onSavingChange, onClose,
}: SourceExpandedDetailsProps) {
  const [windowHours, setWindowHours] = useState<SourceHistoryWindow>(DEFAULT_SOURCE_HISTORY_WINDOW);
  const [metric, setMetric] = useState<HistoryMetric>("records");
  const [localPanel, setLocalPanel] = useState("overview");
  const [localRefresh, setLocalRefresh] = useState(0);
  const [configurationSaving, setConfigurationSaving] = useState(false);
  const [configurationDirty, setConfigurationDirty] = useState(false);
  const [configurationDenied, setConfigurationDenied] = useState(false);
  const mounted = useRef(true);
  const sectionRef = useRef<HTMLElement>(null);
  const headerRef = useRef<HTMLDivElement>(null);
  const focusedSource = useRef<string | null>(null);
  const currentSourceKey = useRef(sourceKey);
  currentSourceKey.current = sourceKey;
  const selectedPanel = panel ?? localPanel;
  const configuration = selectedPanel === "configuration" || selectedPanel === "operations";
  const load = useCallback((signal: AbortSignal) => getAdminSourceDetail(sourceKey, windowHours, includeFixtures, signal, sourceHistoryBucketHours(windowHours)), [sourceKey, windowHours, includeFixtures]);
  const snapshot = useAdminSnapshot(load, refreshVersion);
  const denied = snapshot.authorizationDenied || configurationDenied;
  const detail = !denied && snapshot.data?.source.source_key === sourceKey ? snapshot.data : null;
  const source = detail?.source;
  const disabled = busy || refreshSubmitting || configurationSaving || snapshot.loading || snapshot.failed || denied || !source;
  const runFilters: AdminRunFilters = { sourceKey, windowHours, includeFixtures, status: "" };
  const retired = Boolean(source && adminSourceIsRetired(source));
  const sourcePage = safeHttpUrl(source?.seed_url ?? "");
  const diagnostics = useMemo(() => source ? sourceDiagnostics(source, detail).filter((item) => item.tone !== "healthy") : [], [source, detail]);
  const cannotRefreshReason = !canRefresh ? "Queueing collection requires operator access."
    : refreshSubmitting ? "A refresh command is being submitted."
    : disabled ? "Current source details must be available before queueing collection."
    : configurationDirty ? "Save or discard the configuration draft before queueing collection."
    : retired ? "Retired sources cannot launch collection."
    : policyAllowed === null ? "Collection policy is unavailable; queueing collection is paused."
    : !policyAllowed || source?.policy_blocked || source?.effective_status === "policy_blocked" ? "Fleet collection policy is blocking this source."
    : !source?.enabled ? "Resume collection before queueing a refresh."
    : source.review_status !== "reviewed" ? "Review the source configuration before queueing collection."
    : source.effective_status === "running" || source.latest_run?.status === "running" ? "A source run is already active."
    : null;

  useEffect(() => {
    const readWindow = () => setWindowHours(sourceHistoryWindow(window.location.href));
    readWindow(); window.addEventListener("popstate", readWindow);
    return () => window.removeEventListener("popstate", readWindow);
  }, []);
  const changeWindow = (value: string) => {
    const next = Number(value) as SourceHistoryWindow;
    const href = sourceHistoryWindowUrl(window.location.href, next);
    if (href !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(window.history.state, "", href);
    setWindowHours(next);
  };
  useEffect(() => {
    if (snapshot.data && !snapshot.loading && !snapshot.failed && !snapshot.authorizationDenied) setConfigurationDenied(false);
  }, [snapshot.data, snapshot.loading, snapshot.failed, snapshot.authorizationDenied]);
  useEffect(() => { onSavingChange?.(configurationSaving); }, [configurationSaving, onSavingChange]);
  useEffect(() => () => { onSavingChange?.(false); }, [sourceKey, onSavingChange]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    if (snapshot.loading || focusedSource.current === sourceKey) return;
    const frame = window.requestAnimationFrame(() => {
      const section = sectionRef.current;
      const header = headerRef.current;
      if (!section || !header) return;
      focusedSource.current = sourceKey;
      const active = document.activeElement;
      if (active instanceof HTMLElement && section.contains(active) && active.closest("form")) return;
      const rect = header.getBoundingClientRect();
      if (rect.top < 70 || rect.bottom > window.innerHeight) {
        section.scrollIntoView({ block: "nearest", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      }
      if (window.matchMedia("(max-width: 1250px)").matches) section.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [sourceKey, snapshot.loading]);
  const changePanel = (next: string) => {
    if (configurationSaving) return;
    setLocalPanel(next);
    onPanel?.(next);
  };
  const refresh = () => {
    setLocalRefresh((value) => value + 1);
    void snapshot.refresh();
  };
  const saved = (result: AdminSourceConfigurationUpdate) => {
    onConfigurationSaved(result);
    if (mounted.current && result.source_key === currentSourceKey.current) void snapshot.refresh();
  };
  const inspectDiagnostic = (diagnostic: SourceDiagnostic) => {
    if (source && retired && source.superseded_by_source_key) onOpenSource(source.superseded_by_source_key);
    else if (diagnostic.target === "configuration" || diagnostic.target === "admission") changePanel("configuration");
    else if (diagnostic.target === "catalog") onOpenCatalog(sourceKey);
    else onOpenRuns({ ...runFilters, status: diagnostic.target === "runs" ? "failed" : "" });
  };
  const diagnosticAction = (diagnostic: SourceDiagnostic) => source && retired && source.superseded_by_source_key ? "Open replacement"
    : diagnostic.target === "configuration" || diagnostic.target === "admission" ? "Inspect configuration"
    : diagnostic.target === "catalog" ? "Browse events" : diagnostic.target === "runs" ? "Inspect failed runs" : "Inspect source runs";

  return <section ref={sectionRef} className={styles.details} aria-label={`Source details for ${sourceKey}`} tabIndex={-1}>
    <div ref={headerRef} className={styles.header}>
      <div className={styles.identity}><h3>{configuration ? "Source configuration" : source?.display_name ?? "Source details"}</h3><code>{sourceKey}</code></div>
      <div className={styles.actions}>
        {sourcePage ? <a className={styles.publisherLink} href={sourcePage} target="_blank" rel="noopener noreferrer">Source page <ArrowUpRight aria-hidden="true" /></a> : null}
        {source && !retired ? <button type="button" className={`${kit.action} ${kit.actionPrimary}`} disabled={Boolean(cannotRefreshReason)} title={cannotRefreshReason ?? "Queue a bounded collection refresh"}
          onClick={() => { if (!cannotRefreshReason) onQueueRefresh(source); }}><RefreshCw aria-hidden="true" />{refreshSubmitting ? "Queueing…" : "Queue refresh"}</button> : null}
        <Action disabled={snapshot.loading || configurationSaving} onClick={refresh}><RefreshCw aria-hidden="true" />Refresh source details</Action>
      </div>
      <button type="button" className={styles.close} aria-label="Close source details" disabled={configurationSaving} onClick={onClose}><X aria-hidden="true" /></button>
    </div>
    {source && !retired && cannotRefreshReason ? <p className={styles.controlNote} role="status">{cannotRefreshReason}</p> : null}
    <div className={styles.toolbar}>
      <div className={styles.tabs} role="group" aria-label="Source detail perspective">
        <button type="button" aria-pressed={!configuration} disabled={configurationSaving} onClick={() => changePanel("overview")}>Overview</button>
        <button type="button" aria-pressed={configuration} onClick={() => changePanel("configuration")}>Configuration</button>
      </div>
      {!configuration ? <Segment options={WINDOWS} value={String(windowHours)} label="Source detail window" onChange={changeWindow} /> : null}
      {!configuration && detail ? <span className={styles.readAt} title={stamp(detail.generated_at)}>{snapshot.loading ? "Reading source details…" : snapshot.failed ? "Retained snapshot" : `Snapshot ${age(detail.generated_at)}`} · revision {detail.source.source_revision}</span> : null}
    </div>

    {configuration && snapshot.failed && !denied ? <div className={styles.readError} role="alert">The source detail read failed. Configuration changes are paused until a successful read. <button type="button" onClick={refresh}>Retry source details</button></div> : null}
    {configuration && !snapshot.authorizationDenied ? <SourceConfigurationPanel key={sourceKey} embedded sourceKey={sourceKey} includeFixtures={includeFixtures}
      canConfigure={canConfigure && !snapshot.authorizationDenied} disabled={busy || refreshSubmitting || snapshot.loading || snapshot.failed || snapshot.authorizationDenied} refreshVersion={refreshVersion + localRefresh}
      onSaved={saved} onClose={() => changePanel("overview")}
      onDirtyChange={setConfigurationDirty} onSavingChange={setConfigurationSaving} onAuthorizationDeniedChange={setConfigurationDenied} />
      : !detail ? <div className={styles.empty} role={snapshot.failed || denied ? "alert" : "status"}>
        <p>{denied ? "Source detail access was denied. Previous source evidence is no longer displayed."
          : snapshot.failed ? "This source detail could not be read." : "Reading the selected source history and configuration…"}</p>
        {snapshot.failed || denied ? <Action onClick={refresh}>Retry source details</Action> : null}
      </div> : <>
        {snapshot.failed ? <div className={styles.readError} role="alert">The latest source detail read failed. The last successful snapshot is retained; source controls are read-only until a successful read. <button type="button" onClick={refresh}>Retry source details</button></div> : null}
        {diagnostics.length ? <section className={styles.diagnostics} aria-label="Source attention">
          {diagnostics.map((diagnostic) => <div key={diagnostic.title} data-tone={diagnostic.tone}>
            <strong>{diagnostic.title}</strong><p>{diagnostic.detail}</p>
            <button type="button" onClick={() => inspectDiagnostic(diagnostic)}>{diagnosticAction(diagnostic)}<ArrowRight aria-hidden="true" /></button>
          </div>)}
        </section> : null}
        <div className={styles.summary}>
          <button type="button" onClick={() => onOpenRuns(runFilters)}><span>Published records</span><strong>{int(detail.summary.canonical_count)}</strong><small>{int(detail.summary.candidate_count)} collected in this window</small></button>
          <button type="button" onClick={() => onOpenRuns(runFilters)}><span>Refresh runs</span><strong>{int(detail.summary.total_runs)}</strong><small>{int(detail.summary.succeeded_runs)} succeeded · {int(detail.summary.running_runs)} running or deferred</small></button>
          <button type="button" onClick={() => onOpenRuns({ ...runFilters, status: "failed" })} data-attention={detail.summary.failed_runs > 0}><span>Failed runs</span><strong>{int(detail.summary.failed_runs)}</strong><small>{detail.summary.success_rate === null ? "Success rate not recorded" : `${(detail.summary.success_rate * 100).toFixed(1)}% terminal success rate`}</small></button>
          <button type="button" onClick={() => onOpenCatalog(sourceKey, "upcoming")}><span>Upcoming events</span><strong>{detail.source.upcoming_event_count == null ? "—" : int(detail.source.upcoming_event_count)}</strong><small>Open in Catalog <ArrowUpRight aria-hidden="true" /></small></button>
        </div>
        <SourceHistory detail={detail} metric={metric} onMetric={setMetric} filters={runFilters} onOpenRuns={onOpenRuns} />

        <div className={styles.columns}>
          <section className={styles.recent} aria-label="Recent source runs">
            <div className={styles.sectionHeading}><h4>Recent runs</h4><button type="button" onClick={() => onOpenRuns(runFilters)}>All source runs<ArrowUpRight aria-hidden="true" /></button></div>
            {detail.recent_runs.length ? <div className={styles.runList}>{detail.recent_runs.slice(0, 6).map((run) => {
              const tone: Tone = run.status === "failed" ? "bad" : run.status === "succeeded" ? "ok" : run.status === "running" ? "info" : "warn";
              return <button type="button" key={`${run.source_key}:${run.run_key}`} aria-label={`Inspect run ${run.run_key}`} onClick={() => onOpenRuns(runFilters, run.run_key)}>
                <span><span className={styles.runIdentity}>{run.run_key}</span><small>{recordedTime(run.started_at)} · {ms(run.duration_ms)} · {int(run.attempt_count)} claims</small>
                  <small>{recordedCount(run.canonical_count)} published / {recordedCount(run.candidate_count)} collected{run.error ? ` · ${humanize(run.error)}` : ""}</small></span>
                <Chip tone={tone}>{run.status === "paused" ? "Deferred" : humanize(run.status)}</Chip><ArrowRight aria-hidden="true" />
              </button>;
            })}</div> : <p className={styles.empty}>No source runs were recorded in this window.</p>}
            <p className={styles.note}>Showing {Math.min(6, detail.recent_runs.length)} of {int(detail.recent_runs.length)} loaded recent runs · {windowHours === 24 ? "24h" : `${windowHours / 24}d`} window. All source runs opens the full matching history; each run link opens its exact evidence.</p>
          </section>
          <section className={styles.configuration} aria-label="Source collection schedule">
            <div className={styles.sectionHeading}><h4>Collection schedule</h4></div>
            <dl className={styles.facts}>
              <div><dt>Collection</dt><dd>{adminSourceIsRetired(detail.source) ? "Retired" : detail.source.enabled ? "Enabled" : "Paused"}</dd></div>
              <div><dt>Cadence</dt><dd>{detail.source.refresh_interval_minutes === 1440 ? "Daily" : `Every ${int(detail.source.refresh_interval_minutes)} min`}</dd></div>
              <div><dt>Last success</dt><dd>{recordedTime(detail.source.last_succeeded_at)}</dd></div>
              <div><dt>Next due</dt><dd>{recordedTime(detail.source.next_due_at)}</dd></div>
            </dl>
            <div className={styles.configurationActions}>
              {canEnable && !retired ? <Action disabled={disabled || detail.source.review_status !== "reviewed"} onClick={() => { if (!disabled && detail.source.review_status === "reviewed") onSetEnabled(detail.source, !detail.source.enabled); }}>{detail.source.enabled ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}{detail.source.enabled ? "Pause collection" : "Resume collection"}</Action> : null}
              {retired && detail.source.superseded_by_source_key ? <Action onClick={() => onOpenSource(detail.source.superseded_by_source_key!)}>Open replacement<ArrowUpRight aria-hidden="true" /></Action> : null}
            </div>
            <p className={styles.note}>Cadence is measured from the last successful completion. Collection settings are in Configuration.</p>
            <details className={styles.technical}>
              <summary>Technical identity</summary>
              <dl className={styles.facts}>
                <div><dt>API release</dt><dd><code>{detail.current_build.release_revision}</code></dd></div>
                <div><dt>Image digest</dt><dd><code>{detail.current_build.image_digest ?? "Not recorded"}</code></dd></div>
                <div><dt>Latest run revision</dt><dd>{detail.source.latest_run?.source_revision ?? "Not recorded"}</dd></div>
              </dl>
            </details>
          </section>
        </div>
      </>}
  </section>;
}

function SourceHistory({ detail, metric, onMetric, filters, onOpenRuns }: {
  detail: AdminSourceDetail; metric: HistoryMetric; onMetric: (metric: HistoryMetric) => void;
  filters: AdminRunFilters; onOpenRuns: SourceExpandedDetailsProps["onOpenRuns"];
}) {
  const [focusedBucket, setFocusedBucket] = useState<string | null>(null);
  const pointRefs = useRef<Array<SVGGElement | null>>([]);
  const series = useMemo(() => sourceCollectionSeries(detail, filters.windowHours), [detail, filters.windowHours]);
  const buckets = series.buckets;
  const plot = useMemo(() => sourceHistoryPlot(series, metric), [series, metric]);
  const selectedIndex = Math.max(0, focusedBucket ? buckets.findIndex(bucket => bucket.at === focusedBucket) : buckets.length - 1);
  const selected = buckets[selectedIndex];
  const scope = (index: number) => {
    const bucket = buckets[index];
    const next = bucket ? collectionBucketRuns(series, bucket) : null;
    return next ? { ...next, includeFixtures: filters.includeFixtures } : null;
  };
  const selectedScope = scope(selectedIndex);

  return <section className={styles.history} aria-label="Source history">
    <div className={styles.sectionHeading}><h4>Collection history</h4>
      <Segment options={METRICS} value={metric} onChange={onMetric} label="Source history metric" />
    </div>
    <div className={styles.chartLegend}><span><i />{metric === "records" ? "Published" : "Runs"}</span><span data-secondary={metric}><i />{metric === "records" ? "Collected" : "Failed"}</span><span>{detail.window.bucket_hours}h intervals · {buckets.length} recorded points · select a point to inspect runs</span></div>
    {buckets.length ? <>
      <div className={styles.chart}>
        <div className={styles.chartScale}><span>{int(plot.maximum)}</span><span>0</span></div>
        <svg className={styles.lineChart} viewBox="0 0 1000 200" preserveAspectRatio="none" role="group" aria-label="Source history buckets" data-chart="line">
          {[10, 100, 190].map(y => <line key={y} x1="0" x2="1000" y1={y} y2={y} className={styles.gridLine} vectorEffect="non-scaling-stroke" />)}
          <path d={plot.secondaryPath} className={styles.secondaryLine} data-metric={metric} vectorEffect="non-scaling-stroke" />
          <path d={plot.primaryPath} className={styles.primaryLine} vectorEffect="non-scaling-stroke" />
          {plot.points.map(point => {
            const bucket = buckets[point.index];
            const next = scope(point.index);
            const previousX = plot.points[point.index - 1]?.x;
            const nextX = plot.points[point.index + 1]?.x;
            const left = previousX === undefined ? 0 : (previousX + point.x) / 2;
            const right = nextX === undefined ? 1000 : (nextX + point.x) / 2;
            return <g key={point.at} ref={element => { pointRefs.current[point.index] = element; }} className={styles.historyPoint} data-selected={selectedIndex === point.index}
              role="button" tabIndex={selectedIndex === point.index ? 0 : -1} aria-disabled={!next}
              aria-label={`Inspect source runs started ${collectionTrendTimestamp(point.at)}`}
              onMouseEnter={() => setFocusedBucket(point.at)} onFocus={() => setFocusedBucket(point.at)} onClick={() => next && onOpenRuns(next)}
              onKeyDown={event => {
                if ((event.key === "Enter" || event.key === " ") && next) { event.preventDefault(); onOpenRuns(next); return; }
                const index = event.key === "ArrowRight" ? Math.min(buckets.length - 1, point.index + 1) : event.key === "ArrowLeft" ? Math.max(0, point.index - 1) : event.key === "Home" ? 0 : event.key === "End" ? buckets.length - 1 : null;
                if (index !== null) { event.preventDefault(); pointRefs.current[index]?.focus(); }
              }}>
              <title>{collectionTrendTimestamp(point.at)} · {int(bucket.runs)} runs · {int(bucket.published)} published · {int(bucket.collected)} collected · {int(bucket.failed)} failed</title>
              <rect x={left} y="0" width={right - left} height="200" className={styles.pointTarget} />
              <line x1={point.x} x2={point.x} y1="0" y2="200" className={styles.selectedLine} vectorEffect="non-scaling-stroke" />
              <circle cx={point.x} cy={point.secondaryY} r="2.7" className={styles.secondaryDot} data-metric={metric} vectorEffect="non-scaling-stroke" />
              <circle cx={point.x} cy={point.primaryY} r="3" className={styles.primaryDot} vectorEffect="non-scaling-stroke" />
            </g>;
          })}
        </svg>
      </div>
      <div className={styles.chartDates}><span>{collectionTrendTimestamp(buckets[0].at)}</span><span>{collectionTrendTimestamp(series.generatedAt)}</span></div>
      {selected ? <div className={styles.bucketReadout} aria-live="polite"><span>{collectionTrendTimestamp(selected.at)} → {selectedScope?.startedBefore ? collectionTrendTimestamp(selectedScope.startedBefore) : "Unavailable"}</span>
        <span>{int(selected.published)} published / {int(selected.collected)} collected</span><span>{int(selected.runs)} runs · {int(selected.failed)} failed</span>
        {selectedScope ? <button type="button" onClick={() => onOpenRuns(selectedScope)}>Inspect this interval<ArrowRight aria-hidden="true" /></button> : null}
        {selectedScope && selected.failed > 0 ? <button type="button" onClick={() => onOpenRuns({ ...selectedScope, status: "failed" })}>Inspect failed runs in bucket<ArrowRight aria-hidden="true" /></button> : null}
      </div> : null}
      {buckets.every(bucket => bucket.runs === 0) ? <p className={styles.note}>No runs were recorded in these intervals. Returned zero values remain on the timeline.</p> : null}
    </> : <p className={styles.empty}>No history buckets were returned for this source and time window.</p>}
    <p className={styles.note}>Each point is a recorded interval; quiet periods remain on the time axis. Published and collected values are per-run observations, not distinct catalog events.</p>
  </section>;
}
