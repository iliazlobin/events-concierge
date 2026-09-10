"use client";

import { ArrowUpRight, Check, ChevronLeft, ChevronRight, Copy, Pause, Radio, RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getAdminRun, getAdminRuns } from "@/lib/admin-api";
import { codeRevisionLabel, codeSourcePath } from "@/lib/admin-command-investigation";
import { adminHistoryUrl } from "@/lib/admin-history";
import { collectionHorizonLabel } from "@/lib/admin-source-configuration";
import {
  parseRunSelection, RUN_MAX_PAGE, RUN_PAGE_SIZE, runLedgerLocationFromUrl, runLedgerLocationUrl,
  runEvidenceLocationFromUrl, runEvidenceLocationUrl, runNeighbors, runPageEvidence, runSelectionKey,
  runStageDuration, runTimelineEntries, shouldFollowRun, type RunEvidenceLocation,
  type RunLedgerLocation, type RunSortKey, type RunStageKey, type RunTimelineFilter,
} from "@/lib/admin-run-workspace";
import type { AdminRun, AdminRunFilters, AdminRunSourceConfiguration } from "@/lib/admin-types";

import { Action, Chip, LoadState, PageHead, Segment, SortHeader, age, int, kit, ms, stamp, type Tone } from "./console-kit";
import { useAdminInspector } from "./use-admin-inspector";
import { useAdminSnapshot } from "./use-admin-snapshot";
import { RunFailureEvidence } from "./run-failure-evidence";
import styles from "./runs-view.module.css";

const WINDOWS = [
  { value: "24", label: "24h" }, { value: "168", label: "7d" },
  { value: "336", label: "14d" }, { value: "720", label: "30d" }, { value: "2160", label: "90d" },
] as const;
type Outcome = "all" | "succeeded" | "failed" | "running" | "paused";
const OUTCOMES: ReadonlyArray<{ value: Outcome; label: string }> = [
  { value: "all", label: "All" }, { value: "succeeded", label: "Succeeded" },
  { value: "failed", label: "Failed" }, { value: "running", label: "Running" },
  { value: "paused", label: "Deferred" },
];
const PANELS = ["summary", "stages", "execution"] as const;
const humanize = (code: string | null | undefined) => code ? code.replaceAll("_", " ") : "Not recorded";
const count = (value: number | null | undefined) => value == null ? "Not recorded" : int(value);
const recordedTime = (value: string | null | undefined) => value ? stamp(value) : "Not recorded";
function outcomeTone(status: string): Tone {
  if (status === "succeeded") return "ok";
  if (status === "failed") return "bad";
  if (status === "running") return "info";
  return "warn";
}
function bytes(value: number | null | undefined): string {
  if (value == null) return "Not recorded";
  if (value < 1024) return `${int(value)} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 ** 2).toFixed(1)} MiB`;
}

export interface RunsViewProps {
  filters: AdminRunFilters;
  onFiltersChange: (filters: AdminRunFilters) => void;
  refreshVersion?: number;
  embedded?: boolean;
  onOpenSource: (sourceKey: string) => void;
  onOpenCatalog?: (sourceKey: string, runKey: string) => void;
}

/** The ledger stays visible while one exact source/run is inspected. */
export function RunsView({ onOpenSource, onOpenCatalog, filters, onFiltersChange, refreshVersion = 0, embedded = false }: RunsViewProps): React.JSX.Element {
  const [location, setLocation] = useState<RunLedgerLocation>(() => runLedgerLocationFromUrl("http://localhost/admin"));
  const [evidenceLocation, setEvidenceLocation] = useState<RunEvidenceLocation>(() => runEvidenceLocationFromUrl("http://localhost/admin"));
  const [query, setQuery] = useState("");
  const [following, setFollowing] = useState(false);
  const readBusy = useRef(false);
  const inspector = useAdminInspector({
    selectionParam: "run_selection", panelParam: "run_inspector", panels: PANELS, defaultPanel: "summary",
  });
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [snapshotAt, setSnapshotAt] = useState<string | null>(null);
  const [selectedRead, setSelectedRead] = useState<{ run: AdminRun; at: string } | null>(null);
  const pageSize = embedded ? 10 : RUN_PAGE_SIZE;
  const offset = location.page * pageSize;
  const scopeKey = JSON.stringify(filters);
  const previousScope = useRef(scopeKey);
  const effectiveSort = location.sort === "stage_duration" && !filters.stage ? "started" : location.sort;
  const load = useCallback((signal: AbortSignal) => getAdminRuns(
    filters, { limit: pageSize, offset, query, sortBy: effectiveSort, sortDirection: location.direction }, signal,
  ), [filters.sourceKey, filters.windowHours, filters.status, filters.includeFixtures, filters.startedAfter, filters.startedBefore, filters.stage, filters.stageOutcome, pageSize, offset, query, effectiveSort, location.direction]);
  const pageSnapshot = useAdminSnapshot(load, refreshVersion);
  const selectedIdentity = useMemo(() => parseRunSelection(inspector.selection), [inspector.selection]);
  const loadSelected = useCallback((signal: AbortSignal): Promise<AdminRun | null> => selectedIdentity
    ? getAdminRun(selectedIdentity.sourceKey, selectedIdentity.runKey, filters.includeFixtures, signal)
    : Promise.resolve(null), [selectedIdentity, filters.includeFixtures]);
  const selectedSnapshot = useAdminSnapshot(loadSelected, refreshVersion);
  const denied = pageSnapshot.authorizationDenied || selectedSnapshot.authorizationDenied;
  const { loading, failed, refresh } = pageSnapshot;
  const page = denied ? null : pageSnapshot.data;
  const selected = denied ? null : selectedSnapshot.data;
  const runs = useMemo(() => page?.items ?? [], [page]);
  const total = page?.total;
  const neighbors = runNeighbors(runs, inspector.selection);
  const anyLoading = loading || selectedSnapshot.loading;
  const anyFailed = failed || selectedSnapshot.failed;
  const queryPending = query !== location.query;
  const refreshAll = useCallback(async () => {
    if (readBusy.current) return;
    readBusy.current = true;
    try { await Promise.all([refresh(), selectedSnapshot.refresh()]); }
    finally { readBusy.current = false; }
  }, [refresh, selectedSnapshot.refresh]);

  useEffect(() => {
    const read = () => {
      setLocation(runLedgerLocationFromUrl(window.location.href));
      setEvidenceLocation(runEvidenceLocationFromUrl(window.location.href));
    };
    read();
    window.addEventListener("popstate", read);
    return () => { window.removeEventListener("popstate", read); if (copyTimer.current) clearTimeout(copyTimer.current); };
  }, []);
  useEffect(() => {
    const timer = setTimeout(() => setQuery(location.query), 300);
    return () => clearTimeout(timer);
  }, [location.query]);
  useEffect(() => { setFollowing(false); }, [inspector.selection, scopeKey]);
  useEffect(() => {
    if (!following) return;
    if (anyFailed || denied || !selected || selected.status !== "running") {
      setFollowing(false);
      return;
    }
    const timer = setInterval(() => {
      if (shouldFollowRun({ enabled: following, status: selected.status,
        visible: document.visibilityState === "visible", loading: anyLoading || readBusy.current || queryPending,
        failed: anyFailed, authorizationDenied: denied })) void refreshAll();
    }, 5000);
    return () => clearInterval(timer);
  }, [following, anyFailed, denied, selected?.status, anyLoading, queryPending, refreshAll]);
  useEffect(() => { if (page) setSnapshotAt(new Date().toISOString()); }, [page]);
  useEffect(() => {
    if (selectedSnapshot.data) setSelectedRead({ run: selectedSnapshot.data, at: new Date().toISOString() });
  }, [selectedSnapshot.data]);
  useEffect(() => { setCopied(false); }, [inspector.selection, inspector.panel, location, evidenceLocation, scopeKey]);
  useEffect(() => {
    if (previousScope.current === scopeKey) return;
    previousScope.current = scopeKey;
    // Filter controls reset paging explicitly; browser history restores its own saved page.
    setSnapshotAt(null);
  }, [scopeKey]);

  const changeLocation = (changes: Partial<RunLedgerLocation>, replace = false) => {
    const next = { ...location, ...changes };
    setLocation(next);
    const url = runLedgerLocationUrl(next, window.location.href);
    window.history[replace ? "replaceState" : "pushState"](window.history.state, "", url);
  };
  const changeEvidence = (changes: Partial<RunEvidenceLocation>, panel?: string) => {
    const next = { ...evidenceLocation, ...changes };
    setEvidenceLocation(next);
    if (panel && panel !== inspector.panel) inspector.setPanel(panel);
    const url = runEvidenceLocationUrl(next, window.location.href);
    window.history[panel ? "replaceState" : "pushState"](window.history.state, "", url);
  };
  const changeFilters = (next: AdminRunFilters) => {
    // Reset paging on the new scope entry, preserving the previous scope's page for Back.
    onFiltersChange(next);
    changeLocation({ page: 0, ...(location.sort === "stage_duration" && !next.stage ? { sort: "started" } : {}) }, true);
  };
  const evidence = runPageEvidence(runs);
  const onSort = (sort: string) => changeLocation({
    page: 0,
    sort: sort as RunSortKey,
    direction: sort === location.sort ? location.direction === "asc" ? "desc" : "asc" : sort === "source" ? "asc" : "desc",
  });
  const selectRun = (key: string | null, panel = inspector.panel) => {
    inspector.select(key, panel);
    if (embedded && key && !window.matchMedia("(max-width: 1200px)").matches) {
      window.requestAnimationFrame(() => {
        inspector.inspectorRef.current?.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
        inspector.inspectorRef.current?.focus({ preventScroll: true });
      });
    }
  };
  const selectNeighbor = (key: string | null) => { if (key) selectRun(key); };
  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      if (copyTimer.current) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch { setCopied(false); }
  };
  const backToLedger = () => {
    const ledger = document.getElementById("runs-ledger");
    ledger?.focus({ preventScroll: true });
    ledger?.scrollIntoView({ block: "start" });
  };

  return <div className={`${kit.page} ${embedded ? styles.embedded : ""}`}>
    {!embedded ? <PageHead eyebrow="Evidence / source runs" title="Runs"
      sub="Inspect run outcomes, stages and execution evidence."
      actions={<Action onClick={() => void refreshAll()} disabled={anyLoading}>
        <RefreshCw aria-hidden="true" className={anyLoading ? "spin" : undefined} />{anyLoading ? "Reading…" : "Refresh"}
      </Action>} /> : null}
    <div className={kit.context}>
      {!embedded ? <span>{filters.sourceKey ? `Source: ${filters.sourceKey}` : "All sources"} · {filters.startedAfter || filters.startedBefore ? "Selected interval" : `${filters.windowHours / 24} day window`}</span> : null}
      <span>{failed ? "Ledger read failed" : queryPending ? "Updating search…" : loading ? "Reading ledger…" : snapshotAt ? `Ledger read ${age(snapshotAt)}` : "Ledger not read"}</span>
      {!embedded ? <span>{filters.includeFixtures ? "Includes fixtures" : "Non-fixture sources"}</span> : null}
      {embedded ? <button type="button" className={styles.refreshInline} onClick={() => void refreshAll()} disabled={anyLoading}><RefreshCw aria-hidden="true" />{anyLoading ? "Reading…" : "Refresh runs"}</button> : null}
    </div>
    {failed && page ? <div className={styles.readError} role="status">
      The latest read failed. The last successful snapshot is retained; its run states may have changed.
    </div> : null}

    <div className={embedded ? styles.embeddedWorkspace : kit.workspace}>
      <div className={kit.workspaceMain}>
        <section id="runs-ledger" className={styles.ledger} tabIndex={-1} aria-label="Source run ledger">
          <div className={styles.filters}>
            {!embedded ? <Segment options={WINDOWS} value={filters.startedAfter || filters.startedBefore ? "" : String(filters.windowHours)} onChange={(value) => changeFilters({ ...filters, windowHours: Number(value), startedAfter: undefined, startedBefore: undefined })} label="Run window" /> : null}
            <Segment options={OUTCOMES} value={filters.status || "all"} onChange={(value) => changeFilters({ ...filters, status: value === "all" ? "" : value })} label="Run outcome" />
            {!embedded && filters.sourceKey ? <Action onClick={() => changeFilters({ ...filters, sourceKey: "" })}>All sources</Action> : null}
            {!embedded ? <label className={styles.fixtureToggle}><input type="checkbox" checked={filters.includeFixtures}
              onChange={(event) => changeFilters({ ...filters, includeFixtures: event.target.checked })} />Include fixtures</label> : null}
          </div>
          {filters.stage || filters.stageOutcome || (!embedded && (filters.startedAfter || filters.startedBefore)) ? <div className={styles.filterChips} aria-label="Run investigation filters">
            {!embedded && (filters.startedAfter || filters.startedBefore) ? <button type="button" onClick={() => changeFilters({ ...filters, startedAfter: undefined, startedBefore: undefined })} aria-label="Clear selected time interval">
              {filters.startedAfter ? stamp(filters.startedAfter) : "Any start"} → {filters.startedBefore ? stamp(filters.startedBefore) : "Now"}<X aria-hidden="true" /></button> : null}
            {filters.stage ? <button type="button" onClick={() => changeFilters({ ...filters, stage: undefined, stageOutcome: undefined })} aria-label="Clear stage filter">Stage: {humanize(filters.stage)}<X aria-hidden="true" /></button> : null}
            {filters.stageOutcome ? <button type="button" onClick={() => changeFilters({ ...filters, stageOutcome: undefined })} aria-label="Clear stage outcome filter">Stage outcome: {filters.stageOutcome}<X aria-hidden="true" /></button> : null}
          </div> : null}
          <div className={styles.searchBar}>
            <input className={kit.search} aria-label="Search runs" maxLength={160} placeholder="Find a source, run key, or outcome code"
              value={location.query} onChange={(event) => changeLocation({ query: event.target.value, page: 0 }, true)} />
            {location.query ? <button type="button" className={styles.textLink} onClick={() => changeLocation({ query: "", page: 0 }, true)}>Clear search<X aria-hidden="true" /></button> : null}
            <span>Search and sort cover all matching runs</span>
          </div>
          {!embedded && page ? <div className={styles.pageEvidence} aria-label="Loaded page evidence">
            <span>{int(evidence.succeeded)} succeeded</span><span>{int(evidence.failed)} failed</span>
            <span>{int(evidence.reclaimed)} claimed more than once</span>
            <span>{evidence.output === null ? "Output not recorded" : `${int(evidence.output)} published observations (${int(evidence.outputMeasuredRuns)} measured runs)`}</span>
          </div> : null}
          {!page ? <LoadState failed={failed || denied} onRetry={() => void refreshAll()} label="Reading the run ledger…" /> : <>
            <div className={kit.tableScroll}>
              <table className={`${kit.table} ${styles.table}`} aria-busy={loading || queryPending}>
                <thead><tr>
                  <SortHeader label="Source / run" columnKey="source" active={location.sort === "source"} dir={location.direction} onSort={onSort} />
                  <SortHeader label="Outcome" columnKey="status" active={location.sort === "status"} dir={location.direction} onSort={onSort} />
                  <SortHeader label="Started" columnKey="started" active={location.sort === "started"} dir={location.direction} onSort={onSort} align="right" />
                  <SortHeader label={filters.stage ? "Stage time" : "Duration"} columnKey={filters.stage ? "stage_duration" : "duration"} active={effectiveSort === (filters.stage ? "stage_duration" : "duration")} dir={location.direction} onSort={onSort} align="right" />
                  <SortHeader label="Claims" columnKey="attempts" active={location.sort === "attempts"} dir={location.direction} onSort={onSort} align="right" />
                  <SortHeader label="Output" columnKey="output" active={location.sort === "output"} dir={location.direction} onSort={onSort} align="right" />
                </tr></thead>
                <tbody>{runs.length ? runs.map((run, index) => {
                  const id = runSelectionKey(run);
                  const active = inspector.selection === id;
                  return <tr key={id} data-selected={active} onClick={() => selectRun(id)}>
                    <td><button type="button" className={styles.runSelect} aria-pressed={active} aria-controls="run-inspector"
                      onClick={(event) => { event.stopPropagation(); selectRun(id); }}
                      onKeyDown={(event) => {
                        const target = event.key === "ArrowDown" ? index + 1 : event.key === "ArrowUp" ? index - 1 : event.key === "Home" ? 0 : event.key === "End" ? runs.length - 1 : -1;
                        if (target < 0 || target >= runs.length) return;
                        event.preventDefault();
                        event.currentTarget.closest("tbody")?.querySelectorAll<HTMLButtonElement>('button[aria-controls="run-inspector"]')[target]?.focus({ preventScroll: true });
                        selectNeighbor(runSelectionKey(runs[target]));
                      }}>
                      <strong>{run.display_name ?? run.source_key}</strong><span>{run.run_key}</span>
                    </button></td>
                    <td><Chip tone={outcomeTone(run.status)}>{run.status === "paused" ? "Deferred" : humanize(run.status)}</Chip>
                      {run.error ? <span className={kit.rowSub}>{humanize(run.error)}</span> : null}</td>
                    <td className={`${kit.num} ${styles.date}`} title={recordedTime(run.started_at)}>{run.started_at ? age(run.started_at) : "Not recorded"}<span>{stamp(run.started_at)}</span></td>
                    <td className={`${kit.num} ${kit.mono}`}>{ms(filters.stage ? runStageDuration(run.stage_trace, filters.stage) : run.duration_ms)}</td>
                    <td className={`${kit.num} ${kit.mono}`}>{int(run.attempt_count)}</td>
                    <td className={`${kit.num} ${kit.mono}`}>{run.canonical_count === null ? "—" : int(run.canonical_count)}</td>
                  </tr>;
                }) : <tr><td colSpan={6} className={kit.empty}>{location.query ? "No runs match this search and scope." : "No runs are recorded on this page for the selected scope."}</td></tr>}</tbody>
              </table>
            </div>
            <div className={styles.pagination} aria-label="Runs pagination">
              <Action disabled={loading || location.page === 0} onClick={() => changeLocation({ page: Math.max(0, location.page - 1) })}>Previous</Action>
              <span>{runs.length ? offset + 1 : 0}–{offset + runs.length} of {int(total ?? 0)} · page {location.page + 1}</span>
              <Action disabled={loading || location.page >= RUN_MAX_PAGE || offset + pageSize >= (total ?? 0)} onClick={() => changeLocation({ page: location.page + 1 })}>Next</Action>
            </div>
          </>}
        </section>
        <p className={styles.scopeNote}>Use ↑ / ↓ on a run to inspect adjacent rows.{!embedded ? " Page counts describe retained observations; published records can overlap across runs." : ""}</p>
      </div>

      {!embedded || inspector.selection ? <aside id="run-inspector" ref={inspector.inspectorRef} className={`${kit.inspector} ${styles.inspector} ${embedded ? styles.embeddedInspector : ""}`} tabIndex={-1} aria-labelledby="run-inspector-title">
        {!embedded ? <button type="button" className={styles.backToLedger} onClick={backToLedger}><ChevronLeft aria-hidden="true" />Back to ledger</button> : null}
        <div className={kit.inspectorHeader}><span className={kit.eyebrow}>Selected source run</span>
          {inspector.selection ? <button className={styles.copyLink} type="button" onClick={() => void copyLink()}>
            {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}{copied ? "Copied" : "Copy link"}
          </button> : null}
          {embedded ? <button type="button" className={styles.closeInline} onClick={() => { inspector.select(null); backToLedger(); }} aria-label="Close run details"><X aria-hidden="true" /></button> : null}</div>
        <h2 id="run-inspector-title">{selected?.display_name ?? selected?.source_key ?? selectedIdentity?.sourceKey ?? "Inspect a run"}</h2>
        {!embedded && inspector.selection ? <nav className={styles.breadcrumb} aria-label="Run breadcrumb">
          <button type="button" onClick={() => inspector.select(null)}>Runs</button><span>/</span><span>{selected?.source_key ?? selectedIdentity?.sourceKey ?? "Selected run"}</span>
        </nav> : null}
        {selected ? <>
          <div className={styles.runNavigation} aria-label="Selected run navigation">
            <button type="button" disabled={!neighbors.previous} onClick={() => selectNeighbor(neighbors.previous)} aria-label="Inspect previous run"><ChevronLeft aria-hidden="true" />Previous run</button>
            <span>{neighbors.index < 0 ? "Outside this page" : `${neighbors.index + 1} of ${runs.length}`}</span>
            <button type="button" disabled={!neighbors.next} onClick={() => selectNeighbor(neighbors.next)} aria-label="Inspect next run">Next run<ChevronRight aria-hidden="true" /></button>
          </div>
          <p className={styles.inspectorRead} title={selectedRead?.run === selected ? stamp(selectedRead.at) : undefined}>
            {selectedRead?.run === selected ? `Selected run read ${age(selectedRead.at)}` : "Recording selected read time…"}
            {selectedSnapshot.loading ? " · refreshing" : ""}
          </p>
          <div className={kit.inspectorTabs} role="group" aria-label="Run perspective">
            {PANELS.map((panel) => <button type="button" key={panel} aria-pressed={inspector.panel === panel} onClick={() => inspector.setPanel(panel)}>{panel.charAt(0).toUpperCase() + panel.slice(1)}</button>)}
          </div>
          {selectedSnapshot.failed ? <p className={styles.stale}>Retained run evidence · current state is unverified. <button type="button" onClick={() => void refreshAll()}>Retry read</button></p> : null}
          {selected.status === "running" || following ? <div className={styles.follow}>
            <button type="button" aria-pressed={following} disabled={!following && (anyLoading || anyFailed || denied)} onClick={() => setFollowing(!following)}>
              {following ? <Pause aria-hidden="true" /> : <Radio aria-hidden="true" />}{following ? "Pause following" : "Follow active run"}</button>
            <span>{following ? "Read every 5s while visible; stops on error or completion." : "Follow recorded evidence as this run changes."}</span>
          </div> : null}
          <div className={styles.related}>
            {!embedded && filters.sourceKey !== selected.source_key ? <button type="button" onClick={() => changeFilters({ ...filters, sourceKey: selected.source_key })}>Only this source</button> : null}
            <button type="button" onClick={() => onOpenSource(selected.source_key)}>Manage source<ArrowUpRight aria-hidden="true" /></button>
            {selected.command ? <a href={adminHistoryUrl({ tab: "commands", sourceKey: null, investigation: {
              commandId: selected.command.command_id, sourceKey: selected.source_key, runKey: selected.run_key,
            } }, typeof window === "undefined" ? "http://localhost/admin" : window.location.href)}>Command receipt<ArrowUpRight aria-hidden="true" /></a> : null}
          </div>
          {inspector.panel === "summary" ? <RunSummary run={selected} onOpenCatalog={onOpenCatalog} onInspectStage={(stage) => changeEvidence({ stage }, "stages")} /> : null}
          {inspector.panel === "stages" ? <RunStages run={selected} selectedStage={evidenceLocation.stage} onSelectStage={(stage) => changeEvidence({ stage })} onOpenTimeline={() => changeEvidence({ timeline: "stages" }, "execution")} /> : null}
          {inspector.panel === "execution" ? <RunExecution run={selected} timelineFilter={evidenceLocation.timeline} onTimelineFilter={(timeline) => changeEvidence({ timeline })} /> : null}
        </> : <div className={styles.emptyInspector}>
          <p>{inspector.selection ? selectedSnapshot.loading ? "Reading the selected run…" : selectedSnapshot.failed ? "The selected run could not be read. Its bookmark remains selected." : "The selected run is unavailable." : "Select a row to inspect its outcome and execution evidence while keeping the ledger in view."}</p>
          {selectedSnapshot.failed ? <button type="button" className={styles.textLink} onClick={() => void refreshAll()}>Retry selected run<RefreshCw aria-hidden="true" /></button> : null}
          {selectedIdentity ? <><code>{selectedIdentity.runKey}</code><button type="button" className={styles.textLink} onClick={() => onOpenSource(selectedIdentity.sourceKey)}>Manage source<ArrowUpRight aria-hidden="true" /></button></> : null}
        </div>}
      </aside> : null}
    </div>
  </div>;
}

function RunSummary({ run, onInspectStage, onOpenCatalog }: { run: AdminRun; onInspectStage: (stage: RunStageKey) => void; onOpenCatalog?: RunsViewProps["onOpenCatalog"] }) {
  return <>
    <div className={styles.outcome}><Chip tone={outcomeTone(run.status)}>{run.status === "paused" ? "Deferred" : humanize(run.status)}</Chip>
      <p>{run.resolved_by_newer_success ? "A newer successful run is recorded for this source." : run.is_latest_for_source ? "Latest recorded run for this source." : "Historical source run."}</p>
    </div>
    {run.status === "failed" ? <RunFailureEvidence key={`${run.source_key}:${run.run_key}`} run={run} /> : null}
    <dl className={kit.facts}>
      <div><dt>Started</dt><dd>{recordedTime(run.started_at)}</dd></div>
      <div><dt>Completed</dt><dd>{recordedTime(run.completed_at)}</dd></div>
      <div><dt>Recorded interval</dt><dd>{run.duration_ms === null ? "Not recorded" : ms(run.duration_ms)}</dd></div>
      <div><dt>Source claims</dt><dd>{int(run.attempt_count)}</dd></div>
      <div><dt>Collected records</dt><dd>{count(run.candidate_count)}</dd></div>
      <div><dt>Published records</dt><dd>{count(run.canonical_count)} {onOpenCatalog ? <button type="button" className={styles.textLink} onClick={() => onOpenCatalog(run.source_key, run.run_key)}>View published records<ArrowUpRight aria-hidden="true" /></button> : null}</dd></div>
      <div><dt>Trigger</dt><dd>{humanize(run.trigger)}</dd></div>
      <div><dt>Outcome code</dt><dd>{run.error ? humanize(run.error) : "None recorded"}</dd></div>
    </dl>
    {run.stage_trace.length ? <><h3 className={styles.sectionTitle}>Where time was recorded</h3>
      <StageChoices run={run} onSelect={onInspectStage} />
      <p className={styles.scopeNote}>Select a stage to inspect its observations. Bar lengths compare measured stage durations for this run.</p>
    </> : null}
    <details className={styles.disclosure}><summary>Run identity and source settings</summary>
      <code className={styles.runKey}>{run.run_key}</code>
      <dl className={kit.facts}><div><dt>Source revision</dt><dd>{count(run.source_revision)}</dd></div>
        <div><dt>Provenance</dt><dd>{humanize(run.provenance_status)}</dd></div>
      </dl>
      <section aria-label="Frozen run event window">
        <h3>Frozen run event window</h3>
        {run.collection_window ? <>
          <dl className={kit.facts}>
            <div><dt>Window source revision</dt><dd>{int(run.collection_window.window_source_revision)}</dd></div>
            <div><dt>Horizon</dt><dd>{int(run.collection_window.horizon_days)} days</dd></div>
            <div><dt>Start · included</dt><dd><time dateTime={run.collection_window.start_at} title={run.collection_window.start_at}>{recordedTime(run.collection_window.start_at)}</time></dd></div>
            <div><dt>End · excluded</dt><dd><time dateTime={run.collection_window.end_at} title={run.collection_window.end_at}>{recordedTime(run.collection_window.end_at)}</time></dd></div>
          </dl>
          <p className={styles.scopeNote}>These event-start bounds remain fixed when this run is retried.</p>
        </> : <p className={styles.scopeNote}>Event window was not recorded for this run.</p>}
      </section>
      <section aria-label="Captured attempt settings">
        <h3>Captured attempt settings</h3>
        {run.execution_configuration ? <>
          <dl className={kit.facts}><div><dt>Execution source revision</dt><dd>{int(run.execution_configuration.source_revision)}</dd></div></dl>
          <RunConfigurationFacts configuration={run.execution_configuration} />
          <p className={styles.scopeNote}>Settings captured for the latest recorded source attempt. The frozen run window may use an earlier revision.</p>
        </> : <p className={styles.scopeNote}>Execution settings were not recorded for this attempt.</p>}
      </section>
      {run.source_configuration ? <section aria-label="Current source settings"><h3>Current source settings</h3>
        <RunConfigurationFacts configuration={run.source_configuration} />
        <p className={styles.scopeNote}>Current registry settings may differ from those used by this run.</p>
      </section> : null}
    </details>
    <p className={styles.scopeNote}>A run outcome describes source execution. The linked command records its separate acceptance and dispatch lifecycle.</p>
  </>;
}

function RunConfigurationFacts({ configuration }: { configuration: AdminRunSourceConfiguration }) {
  return <dl className={kit.facts}>
    <div><dt>Adapter mode</dt><dd>{humanize(configuration.mode)}</dd></div>
    <div><dt>Cadence</dt><dd>{int(configuration.refresh_interval_minutes)} min</dd></div>
    <div><dt>Event window</dt><dd>{collectionHorizonLabel(configuration.collection_horizon_days)}</dd></div>
    <div><dt>Minimum pacing</dt><dd>{ms(configuration.min_interval_ms)}</dd></div>
    <div><dt>Request-unit cap</dt><dd>{int(configuration.page_limit)}</dd></div>
    <div><dt>Reviewed</dt><dd>{recordedTime(configuration.reviewed_at)}</dd></div>
    <div><dt>Review expires</dt><dd>{recordedTime(configuration.review_expires_at)}</dd></div>
  </dl>;
}

function StageChoices({ run, selectedStage, onSelect }: { run: AdminRun; selectedStage?: RunStageKey; onSelect: (stage: RunStageKey) => void }) {
  const longest = Math.max(0, ...run.stage_trace.filter((stage) => stage.evidence_status === "measured").map((stage) => stage.duration_ms ?? 0));
  return <div className={styles.stageChoices} role="group" aria-label="Run stages">{run.stage_trace.map((stage) => {
    const duration = runStageDuration(run.stage_trace, stage.stage);
    return <button type="button" key={stage.stage} aria-label={`Inspect ${stage.label}`} aria-pressed={selectedStage === stage.stage}
      onClick={() => onSelect(stage.stage)}>
      <span><strong>{stage.label}</strong><span>{duration === null ? "Not measured" : ms(duration)}</span></span>
      <span className={styles.stageBar} aria-hidden="true"><i style={{ width: `${duration !== null && longest > 0 ? duration / longest * 100 : 0}%` }} /></span>
    </button>;
  })}</div>;
}

function RunStages({ run, selectedStage, onSelectStage, onOpenTimeline }: {
  run: AdminRun; selectedStage: RunStageKey; onSelectStage: (stage: RunStageKey) => void; onOpenTimeline: () => void;
}) {
  const stages = run.stage_trace ?? [];
  const measured = stages.filter((stage) => stage.evidence_status === "measured");
  const selected = stages.find((stage) => stage.stage === selectedStage);
  return <>
    <p className={styles.description}>{int(measured.length)} of {int(stages.length)} stages have measured evidence. Durations aggregate the retained observations for this run.</p>
    <StageChoices run={run} selectedStage={selectedStage} onSelect={onSelectStage} />
    {selected ? <section className={styles.stageDetail} aria-label="Selected stage evidence">
      <h3>{selected.label}</h3>
      <Chip tone={selected.evidence_status === "measured" ? "info" : "neutral"}>{humanize(selected.evidence_status)}</Chip>
      <p>{selected.evidence_status === "measured" ? "Measured observations retained for this exact source run."
        : selected.evidence_status === "not_separately_instrumented" ? "Included in an adjacent measured boundary; no separate duration."
          : "No duration is available for this stage."}</p>
      <dl className={kit.facts}>
        <div><dt>Observed duration</dt><dd>{selected.evidence_status === "measured" && selected.duration_ms !== null ? ms(selected.duration_ms) : "Not recorded"}</dd></div>
        <div><dt>Observations</dt><dd>{int(selected.observation_count)}</dd></div>
        <div><dt>Last outcome</dt><dd>{humanize(selected.last_outcome_code)}</dd></div>
        <div><dt>First observed</dt><dd>{recordedTime(selected.first_observed_at)}</dd></div>
        <div><dt>Last observed</dt><dd>{recordedTime(selected.last_observed_at)}</dd></div>
      </dl>
      {selected.note_code ? <p>{humanize(selected.note_code)}</p> : null}
      <button type="button" className={styles.textLink} onClick={onOpenTimeline}>Show retained stage events<ArrowUpRight aria-hidden="true" /></button>
    </section> : stages.length ? <p className={styles.description}>No retained evidence exists for this stage. Select another stage above.</p> : null}
    {!stages.length ? <p className={styles.emptyInspector}>Stage evidence was not retained for this run.</p> : null}
    <p className={styles.scopeNote}>Unobserved stages stay unknown. A measured duration of zero remains zero.</p>
  </>;
}

function RunExecution({ run, timelineFilter, onTimelineFilter }: { run: AdminRun; timelineFilter: RunTimelineFilter; onTimelineFilter: (filter: RunTimelineFilter) => void }) {
  const resources = run.resources;
  const shared = resources?.measurement_scope === "activity_wall_clock_only";
  const entries = runTimelineEntries(run.timeline?.entries ?? [], timelineFilter);
  return <>
    <h3 className={styles.sectionTitle}>Retained timeline</h3>
    <Segment options={[{ value: "all", label: "All" }, { value: "stages", label: "Stages" }, { value: "lifecycle", label: "Lifecycle" }]} value={timelineFilter} onChange={onTimelineFilter} label="Timeline evidence" />
    <p className={styles.timelineScope}>{entries.length} of {run.timeline?.entries.length ?? 0} retained entries · incomplete evidence</p>
    <ol className={styles.timeline}>{entries.map((entry, index) => <li key={`${entry.observed_at}:${entry.event_code}:${entry.stage}:${index}`}>
      <time dateTime={entry.observed_at}>{stamp(entry.observed_at)}</time>
      <details><summary>{humanize(entry.event_code)}{entry.stage ? ` · ${humanize(entry.stage)}` : ""}</summary>
        <dl className={kit.facts}>
          <div><dt>Timestamp basis</dt><dd>{humanize(entry.timestamp_basis)}</dd></div>
          <div><dt>Observed at</dt><dd><time dateTime={entry.observed_at}>{entry.observed_at}</time></dd></div>
          <div><dt>Outcome</dt><dd>{humanize(entry.outcome_code)}</dd></div>
          <div><dt>Duration</dt><dd>{entry.duration_ms === null ? "Not recorded" : ms(entry.duration_ms)}</dd></div>
          <div><dt>Observations</dt><dd>{count(entry.observation_count)}</dd></div>
        </dl>
      </details>
      <span>{entry.outcome_code ? humanize(entry.outcome_code) : humanize(entry.timestamp_basis)}{entry.duration_ms === null ? "" : ` · ${ms(entry.duration_ms)}`}</span>
    </li>)}</ol>
    {!entries.length ? <p className={styles.description}>No retained entries match this timeline filter.</p> : null}
    <p className={styles.scopeNote}>Open an entry for its recorded basis. The command receipt has a separate activity stream; this timeline contains lifecycle transitions and aggregate observations.</p>
    <h3 className={styles.sectionTitle}>Recorded execution</h3>
    {resources ? <><dl className={kit.facts}>
      <div><dt>Worker executions</dt><dd>{int(resources.execution_count)}</dd></div>
      <div><dt>Observed wall time</dt><dd>{ms(resources.wall_time_ms)}</dd></div>
      <div><dt>Process CPU time</dt><dd>{shared ? "Not attributable" : resources.process_cpu_time_ms === null ? "Not recorded" : ms(resources.process_cpu_time_ms)}</dd></div>
      <div><dt>Boundary RSS peak</dt><dd>{shared ? "Not attributable" : bytes(resources.boundary_observed_peak_rss_bytes)}</dd></div>
    </dl><p className={styles.scopeNote}>{shared ? "Shared activity workers omit process CPU/RSS attribution." : "Process samples are best-effort boundary observations for a sequential worker."}</p></>
      : <p className={styles.description}>Resource observations are not available for this run.</p>}
    <details className={styles.disclosure}><summary>Build and code ownership</summary>
      <p>{codeRevisionLabel(run.release_revision, run.image_digest)}</p>
      <p className={styles.scopeNote}>Claim provenance and current code ownership are navigation evidence. They do not identify the build of a later Temporal activity.</p>
      {run.execution ? <dl className={`${kit.facts} ${styles.codeFacts}`}>
        <div><dt>Execution path</dt><dd>{humanize(run.execution.execution_path)}</dd></div>
        <div><dt>Worker service</dt><dd>{run.execution.worker_service}</dd></div>
        <div><dt>Task queue</dt><dd>{run.execution.task_queue ?? "Direct path"}</dd></div>
        <div><dt>Adapter</dt><dd><code>{codeSourcePath(run.execution.adapter_module)}#{run.execution.adapter_symbol}</code></dd></div>
        <div><dt>Orchestration</dt><dd><code>{codeSourcePath(run.execution.orchestration_module)}#{run.execution.orchestration_symbol}</code></dd></div>
        <div><dt>Worker entry</dt><dd><code>{codeSourcePath(run.execution.worker_module)}#{run.execution.worker_symbol}</code></dd></div>
      </dl> : <p>Code ownership is not available for this run.</p>}
    </details>
    {resources ? <details className={styles.disclosure}><summary>Measurement definitions</summary>
      <dl className={kit.facts}><div><dt>Scope</dt><dd>{humanize(resources.measurement_scope)}</dd></div>
        <div><dt>Quality</dt><dd>{humanize(resources.measurement_quality)}</dd></div>
        <div><dt>Average process CPU</dt><dd>{shared ? "Not attributable" : resources.cpu_utilization_percent === null ? "Not recorded" : `${resources.cpu_utilization_percent.toFixed(1)}%`}</dd></div>
        <div><dt>RSS before / after</dt><dd>{bytes(resources.rss_before_bytes)} / {bytes(resources.rss_after_bytes)}</dd></div>
        <div><dt>Process lifetime peak</dt><dd>{bytes(resources.process_lifetime_peak_rss_bytes)}</dd></div>
        <div><dt>Observation source</dt><dd>{humanize(resources.measurement_source)}</dd></div>
        <div><dt>Last outcome</dt><dd>{humanize(resources.last_outcome_code)}</dd></div>
        <div><dt>First observed</dt><dd>{recordedTime(resources.first_observed_at)}</dd></div>
        <div><dt>Last observed</dt><dd>{recordedTime(resources.last_observed_at)}</dd></div></dl>
      <p className={styles.scopeNote}>The lifetime peak describes the worker process and cannot be attributed solely to this run.</p>
    </details> : null}
  </>;
}
