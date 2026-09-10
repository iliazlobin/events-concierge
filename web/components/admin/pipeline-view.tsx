"use client";

import { ArrowRight, RefreshCw } from "lucide-react";
import { useCallback, useState } from "react";

import {
  getAdminFleetSummary,
  getAdminSourceDetail,
  getAdminSourceHealth,
  getAdminStageSummary,
  getAdminThroughput,
} from "@/lib/admin-api";
import type {
  AdminFleetSummary, AdminRunFilters, AdminRunSort, AdminSourceDetail, AdminSourceHealthList,
  AdminStageSummary, AdminStageSummaryEntry, AdminThroughput,
} from "@/lib/admin-types";

import { pipelineAttentionSources, pipelineBucketScope, pipelineRunScope } from "@/lib/admin-pipeline-workspace";
import { sourceNeedsAttention, sourceSignal } from "@/lib/system-operations";

import { Action, Chip, LoadState, Metrics, PageHead, Section, Segment, age, int, kit, ms, stamp } from "./console-kit";
import { useAdminInspector } from "./use-admin-inspector";
import { useAdminSnapshot } from "./use-admin-snapshot";
import styles from "./pipeline-view.module.css";

const WINDOWS = [
  { value: "24", label: "24h" }, { value: "168", label: "7d" },
  { value: "336", label: "14d" }, { value: "720", label: "30d" },
] as const;
const PANELS = ["overview", "evidence"] as const;
const STAGES = [
  { id: "admission", label: "Admission", role: "Check source policy, pacing, and lease ownership before collection can begin.", boundary: "Policy and refresh admission", records: "No record count is recorded at admission." },
  { id: "collect", label: "Collect", role: "Read approved source pages or feeds and collect candidate event records.", boundary: "Adapter collection", records: "Candidate records are counted here, once per refresh run." },
  { id: "extract_enrich", label: "Extract + enrich", role: "Extract structured event fields and available public context within the source adapter.", boundary: "Included in adapter collection", records: "No separate record count is available at this boundary." },
  { id: "normalize_dedupe", label: "Normalize + dedupe", role: "Normalize event identity and merge repeated observations before catalog publication.", boundary: "Included in catalog commit", records: "Merging is many-to-one; there is no separately measured loss count." },
  { id: "catalog_publish", label: "Catalog + publish", role: "Upsert canonical event observations into the catalog and retain refresh evidence.", boundary: "Catalog commit", records: "Canonical records are counted per run. Repeated refreshes can publish the same event again." },
] as const;

type Snapshot = {
  kind: "fleet"; stages: AdminStageSummary; fleet: AdminFleetSummary;
  throughput: AdminThroughput; health: AdminSourceHealthList;
} | { kind: "source"; detail: AdminSourceDetail; health: AdminSourceHealthList };
interface ActivityBucket { at: string; runs: number; succeeded: number; failed: number; collected: number; published: number }

export interface PipelineViewProps {
  filters: AdminRunFilters;
  onFiltersChange: (filters: AdminRunFilters) => void;
  onOpenRuns: (filters: AdminRunFilters, sortBy?: AdminRunSort) => void;
  onOpenSource: (sourceKey: string) => void;
  refreshVersion?: number;
}

export function PipelineView({ filters, onFiltersChange, onOpenRuns, onOpenSource, refreshVersion = 0 }: PipelineViewProps): React.JSX.Element {
  const { selection, panel, select, setPanel, inspectorRef } = useAdminInspector({
    selectionParam: "pipeline_stage", panelParam: "pipeline_inspector", panels: PANELS,
    defaultPanel: "overview", defaultSelection: "collect",
  });
  const load = useCallback(async (signal: AbortSignal): Promise<Snapshot> => {
    if (filters.sourceKey) {
      const [detail, health] = await Promise.all([
        getAdminSourceDetail(filters.sourceKey, filters.windowHours, filters.includeFixtures, signal),
        getAdminSourceHealth(filters.includeFixtures, signal),
      ]);
      return { kind: "source", detail, health };
    }
    const [stages, fleet, throughput, health] = await Promise.all([
      getAdminStageSummary(filters.windowHours, filters.includeFixtures, signal),
      getAdminFleetSummary(filters.windowHours, filters.includeFixtures, signal),
      getAdminThroughput(filters.windowHours, filters.windowHours <= 24 ? 1 : 24, signal),
      getAdminSourceHealth(filters.includeFixtures, signal),
    ]);
    return { kind: "fleet", stages, fleet, throughput, health };
  }, [filters.sourceKey, filters.windowHours, filters.includeFixtures]);
  const { data: snap, loading, failed, refresh } = useAdminSnapshot(load, refreshVersion);
  // Keep the last successful snapshot visible during transient refresh failures.
  if (!snap) return <LoadState failed={failed} onRetry={() => void refresh()} label="Reading the pipeline…" />;

  const source = snap.kind === "source" ? snap.detail : null;
  const fleet = snap.kind === "fleet" ? snap.fleet : null;
  const scope = source ? source.source.display_name : "Whole fleet";
  const generatedAt = source ? source.generated_at : (snap as Extract<Snapshot, { kind: "fleet" }>).stages.generated_at;
  const runScope = pipelineRunScope(filters);
  const candidates = source ? source.summary.candidate_count : fleet!.candidates;
  const runs = source ? source.summary.total_runs : fleet!.runs;
  const failedRuns = source ? source.summary.failed_runs : fleet!.failed;
  const runningRuns = source ? source.summary.running_runs : fleet!.running;
  const stageRows = snap.kind === "fleet" ? snap.stages.stages : [];
  const selected = STAGES.find((stage) => stage.id === selection) ?? STAGES[1];
  const entry = stageRows.find((stage) => stage.stage === selected.id);
  const measured = stageRows.filter((stage) => stage.evidence_status === "measured");
  const dominant = [...measured].filter((stage) => stage.pct_of_wall !== null)
    .sort((a, b) => (b.pct_of_wall ?? 0) - (a.pct_of_wall ?? 0))[0];
  const buckets: ActivityBucket[] = snap.kind === "source"
    ? snap.detail.history.map((bucket) => ({ at: bucket.bucket_start, runs: bucket.total_runs, succeeded: bucket.succeeded_runs, failed: bucket.failed_runs, collected: bucket.candidate_count, published: bucket.canonical_count }))
    : snap.throughput.buckets.map((bucket) => ({ at: bucket.bucket_start, runs: bucket.runs, succeeded: bucket.succeeded, failed: bucket.failed, collected: bucket.collected, published: bucket.published }));
  const bucketHours = snap.kind === "source" ? snap.detail.window.bucket_hours : snap.throughput.bucket_hours;
  const activityScope = source ? "Selected source · all outcomes" : "Fleet · fixtures excluded · all outcomes";
  const activityGeneratedAt = source ? source.generated_at : snap.kind === "fleet" ? snap.throughput.generated_at : generatedAt;
  const evidence = evidenceLabel(entry, Boolean(source));
  const recorded = entry?.evidence_status === "measured";

  return <div className={kit.page}>
    <PageHead eyebrow="Control room / pipeline" title="Collection pipeline"
      sub="Find collection issues and open the source runs behind them."
      actions={<Action disabled={loading} onClick={() => void refresh()}><RefreshCw aria-hidden="true" className={loading ? "spin" : undefined} />{loading ? "Refreshing" : "Refresh"}</Action>} />
    <div className={kit.context}>
      <span>{scope}</span><span>{filters.windowHours}h · all run outcomes</span>
      <span>{filters.includeFixtures ? "Fixtures included in run totals" : "Fixtures excluded from run totals"}</span>
      <span>Snapshot {age(generatedAt)}</span>
    </div>
    {failed ? <div className={styles.readError} role="status">Refresh failed. Showing the last successful snapshot from {stamp(generatedAt)}. <button type="button" onClick={() => void refresh()}>Retry</button></div> : null}
    <div className={kit.toolbar}>
      <Segment options={WINDOWS} value={String(filters.windowHours)} label="Pipeline window" onChange={(value) => onFiltersChange({ ...runScope, windowHours: Number(value) })} />
      {source ? <Action onClick={() => onFiltersChange({ ...runScope, sourceKey: "" })}>Whole fleet</Action> : null}
      <label className={styles.fixtureToggle}><input type="checkbox" checked={filters.includeFixtures} onChange={(event) => onFiltersChange({ ...runScope, includeFixtures: event.target.checked })} />Include fixtures</label>
      <span className={kit.spacer} /><Action onClick={() => onOpenRuns(runScope)}>Inspect {filters.status || "all"} runs <ArrowRight aria-hidden="true" /></Action>
    </div>
    <div className={kit.workspace}>
      <div className={kit.workspaceMain}>
        <div className={styles.summary}><Metrics onSelect={(key) => onOpenRuns({ ...runScope, status: key === "failed" ? "failed" : key === "running" ? "running" : "" })} items={[
          { key: "runs", label: "Refresh runs", value: int(runs), note: "inspect all outcomes in window", interactive: true },
          { key: "collected", label: "Collected", value: int(candidates), note: "candidate records" },
          { key: "failed", label: "Failed runs", value: int(failedRuns), note: "inspect recorded failures", interactive: true },
          { key: "running", label: "Running runs", value: int(runningRuns), note: "inspect active run evidence", interactive: true },
        ]} /></div>
        <SourceAttention key={`${filters.sourceKey}:${filters.includeFixtures}`} health={snap.health} filters={runScope} onOpenSource={onOpenSource} onOpenRuns={onOpenRuns} />
        <section className={styles.stageMap} aria-labelledby="pipeline-stage-title">
          <div className={styles.mapHeader}><h2 id="pipeline-stage-title">Collection stages</h2><span>{source ? "Source timing: inspect runs" : `${measured.length} of ${STAGES.length} stages measured`}</span></div>
          <div className={styles.stageFlow} role="group" aria-label="Select pipeline stage">
            {STAGES.map((stage, index) => {
              const row = stageRows.find((item) => item.stage === stage.id);
              const known = row?.evidence_status === "measured";
              return <button type="button" key={stage.id} className={styles.stage} aria-pressed={selected.id === stage.id} aria-controls="pipeline-inspector" onClick={() => select(stage.id)}>
                <span className={styles.stageNumber}>{String(index + 1).padStart(2, "0")}<ArrowRight aria-hidden="true" /></span>
                <strong>{stage.label}</strong>
                <span className={styles.stageDuration}>{known ? ms(row.total_ms) : "—"}</span>
                <span className={styles.stageEvidence} data-measured={known}>{evidenceLabel(row, Boolean(source))}</span>
                {known && row.pct_of_wall !== null ? <span className={styles.stageShare}>{row.pct_of_wall}% of recorded stage time</span> : null}
              </button>;
            })}
          </div>
          <p className={styles.mapNote}>{source ? "The source summary does not aggregate stage durations. Use related runs for measured boundaries; fleet totals are not substituted here." : dominant ? `${STAGES.find((stage) => stage.id === dominant.stage)?.label ?? dominant.stage} accounts for ${dominant.pct_of_wall}% of recorded stage time. Select a stage to inspect coverage and measurement limits.` : "No stage timings were observed in this window. Missing measurements are shown as unavailable, not zero."}</p>
        </section>
        <Section title="Run activity" scope={`${bucketHours}h buckets`}>
          <p className={styles.note}>Open a time bucket to inspect its runs, or select its failure count. {activityScope}.</p>
          <RunActivity key={`${filters.sourceKey}:${filters.windowHours}:${filters.includeFixtures}`} buckets={buckets} bucketHours={bucketHours} generatedAt={activityGeneratedAt} filters={runScope} sourceScope={Boolean(source)} onOpenRuns={onOpenRuns} />
          <p className={kit.cap}>Start-time intervals are exact; the current bucket ends at the snapshot. Run outcomes may change after this read. Collected and published counts are per-run records, not distinct catalog size.</p>
          {!source && filters.includeFixtures ? <p className={styles.scopeNote}>This activity view excludes fixtures. Opening a bucket also excludes fixtures.</p> : null}
        </Section>
      </div>
      <aside className={kit.inspector} id="pipeline-inspector" ref={inspectorRef} tabIndex={-1} aria-labelledby="pipeline-inspector-title">
        <div className={kit.inspectorHeader}><span>Pipeline / stage {STAGES.indexOf(selected) + 1}</span><Chip tone={recorded ? "ok" : "neutral"}>{evidence}</Chip></div>
        <h2 id="pipeline-inspector-title" className={styles.inspectorTitle}>{selected.label}</h2>
        <p className={styles.role}>{selected.role}</p>
        <div className={kit.inspectorTabs} role="group" aria-label="Stage inspector view">
          {PANELS.map((value) => <button type="button" key={value} aria-pressed={panel === value} onClick={() => setPanel(value)}>{value === "overview" ? "Overview" : "Evidence"}</button>)}
        </div>
        {panel === "evidence" ? <>
          <h3 className={styles.inspectorSection}>Recorded measurements</h3>
          <dl className={kit.facts}>
            <div><dt>Runs with evidence</dt><dd>{entry && entry.evidence_status !== "not_separately_instrumented" ? int(entry.runs_with_evidence) : "Unavailable"}</dd></div>
            <div><dt>Observations</dt><dd>{recorded ? int(entry.observations) : "Unavailable"}</dd></div>
            <div><dt>Total time</dt><dd>{recorded ? ms(entry.total_ms) : "Unavailable"}</dd></div>
            <div><dt>Average / p95</dt><dd>{recorded ? `${ms(entry.avg_ms)} / ${ms(entry.p95_ms)}` : "Unavailable"}</dd></div>
            <div><dt>Runs last marked failed</dt><dd>{recorded ? int(entry.failed_count) : "Unavailable"}</dd></div>
            <div><dt>Share of recorded stage time</dt><dd>{recorded && entry.pct_of_wall !== null ? `${entry.pct_of_wall}%` : "Unavailable"}</dd></div>
          </dl>
          <p className={styles.note}>{source ? "Aggregate source timing is unavailable from this endpoint. Recent runs are not extrapolated into window totals." : "Repeated attempts can add observations to the same run. Average and p95 use accumulated stage duration per run; failed counts use each run’s last stage outcome."}</p>
          <details className={styles.references}><summary>Code and storage references</summary><p>Current implementation references; these are not a trace of a deployed release.</p>
            <code>src/events_concierge/application/catalog_run_evidence.py</code><code>CatalogRunEvidenceSession · CatalogRunStageTimer</code><code>catalog_refresh_run_stage_metrics</code><code>fn_get_ingestion_admin_stage_summary_v1</code>
            <p>Exact run evidence carries execution routing and available release provenance.</p>
          </details>
        </> : <>
          <h3 className={styles.inspectorSection}>Boundary</h3><p className={styles.note}>{selected.boundary}</p>
          <p className={styles.note}>{selected.records}</p>
          {entry?.evidence_status === "not_separately_instrumented" ? <div className={styles.unavailable}><strong>No separate duration</strong><p>This work is included in an adjacent measured boundary. No duration can be attributed to this stage.</p>{entry.folded_into ? <code>{entry.folded_into}</code> : null}</div> : !recorded ? <div className={styles.unavailable}><strong>{source ? "Inspect source run evidence" : "No timing observed"}</strong><p>{source ? "Stage timing is available on individual runs when recorded. The source summary reports window totals without stage aggregation." : "No stage duration was recorded for this window. This does not establish that the stage took zero time."}</p></div> : <div className={styles.highlight}><strong>{ms(entry.total_ms)}</strong><span>recorded across {int(entry.runs_with_evidence)} runs</span><small>{ms(entry.p95_ms)} p95 · {int(entry.observations)} observations</small></div>}
        </>}
        {!["extract_enrich", "normalize_dedupe"].includes(selected.id) ? <div className={styles.related}>
          <div className={styles.stageActions}>
            <Action onClick={() => onOpenRuns({ ...runScope, status: "", stage: selected.id, stageOutcome: undefined }, "stage_duration")}>Slowest measured runs <ArrowRight aria-hidden="true" /></Action>
            <Action onClick={() => onOpenRuns({ ...runScope, status: "", stage: selected.id, stageOutcome: "failed" }, "stage_duration")}>Failed stage outcomes <ArrowRight aria-hidden="true" /></Action>
          </div></div> : null}
      </aside>
    </div>
  </div>;
}

function evidenceLabel(entry: AdminStageSummaryEntry | undefined, sourceScope: boolean): string {
  if (sourceScope) return "Inspect runs";
  if (entry?.evidence_status === "not_separately_instrumented") return "Included in boundary";
  return entry?.evidence_status === "measured" ? "Measured" : "Not observed";
}

function SourceAttention({ health, filters, onOpenSource, onOpenRuns }: {
  health: AdminSourceHealthList; filters: AdminRunFilters;
  onOpenSource: PipelineViewProps["onOpenSource"]; onOpenRuns: PipelineViewProps["onOpenRuns"];
}): React.JSX.Element {
  const [showAll, setShowAll] = useState(false);
  const [limit, setLimit] = useState(8);
  const sources = pipelineAttentionSources(health.sources, filters.sourceKey);
  const attention = sources.filter(sourceNeedsAttention);
  const rows = showAll || filters.sourceKey ? sources : attention;
  return <Section title="Source attention" scope="current source snapshot">
    <div className={styles.attentionHeader}>
      <p>{!sources.length ? "No source health evidence is available for this scope." : attention.length ? `${int(attention.length)} source${attention.length === 1 ? "" : "s"} with current collection signals to inspect.` : "No current attention signals in this source scope."}</p>
      {!filters.sourceKey ? <button type="button" aria-pressed={showAll} onClick={() => { setShowAll(!showAll); setLimit(8); }}>{showAll ? "Show attention only" : `Show all ${int(sources.length)} sources`}</button> : null}
    </div>
    {rows.length ? <ul className={styles.attentionList} aria-label="Sources requiring investigation">
      {rows.slice(0, limit).map((item) => <li key={item.source_key}>
        <div className={styles.sourceSignal}>
          <button type="button" onClick={() => onOpenSource(item.source_key)}>{item.display_name}<ArrowRight aria-hidden="true" /></button>
          <span>{sourceSignal(item)}</span>
          {item.latest_run_error ? <code>{item.latest_run_error.replaceAll("_", " ")}</code> : null}
          <small>Last success {age(item.last_success_at)} · {int(item.upcoming_events)} upcoming events retained</small>
        </div>
        <Action onClick={() => onOpenRuns({ ...pipelineRunScope(filters), status: "", sourceKey: item.source_key })}>Inspect runs <ArrowRight aria-hidden="true" /></Action>
      </li>)}
    </ul> : null}
    {rows.length > limit ? <Action onClick={() => setLimit(limit + 8)}>Show more sources ({int(rows.length - limit)} remaining)</Action> : null}
    <p className={kit.cap}>Latest outcomes, retries and cadence freshness · {age(health.generated_at)}. Independent of the run window; paused and retired sources are excluded from attention.</p>
  </Section>;
}

function RunActivity({ buckets, bucketHours, generatedAt, filters, sourceScope, onOpenRuns }: {
  buckets: ActivityBucket[]; bucketHours: number; generatedAt: string; filters: AdminRunFilters;
  sourceScope: boolean; onOpenRuns: PipelineViewProps["onOpenRuns"];
}): React.JSX.Element {
  const [showEmpty, setShowEmpty] = useState(false);
  const [limit, setLimit] = useState(8);
  const nonempty = buckets.filter((bucket) => bucket.runs > 0);
  const rows = [...(showEmpty ? buckets : nonempty)].sort((a, b) => b.at.localeCompare(a.at));
  return <div className={styles.activity}>
    <div className={styles.activityToolbar}>
      <span>{int(nonempty.length)} active / {int(buckets.length)} recorded buckets</span>
      <label><input type="checkbox" checked={showEmpty} onChange={(event) => { setShowEmpty(event.target.checked); setLimit(8); }} />Include empty buckets</label>
    </div>
    {rows.length ? <div className={kit.tableScroll}><table className={`${kit.table} ${styles.activityTable}`} aria-label="Run activity by start time">
      <thead><tr><th>Run started</th><th className={kit.num}>Runs</th><th className={kit.num}>Succeeded</th><th className={kit.num}>Failed</th><th className={kit.num}>Collected</th><th className={kit.num}>Published</th></tr></thead>
      <tbody>{rows.slice(0, limit).map((bucket) => {
        const next = pipelineBucketScope(filters, bucket.at, bucketHours, generatedAt, sourceScope);
        return <tr key={bucket.at}>
          <td><button type="button" disabled={!next} onClick={() => next && onOpenRuns(next)} aria-label={`Inspect runs started ${stamp(bucket.at)}`}>
            {stamp(bucket.at)}<ArrowRight aria-hidden="true" />
          </button><small>to {stamp(next?.startedBefore)}</small></td>
          <td className={kit.num}>{int(bucket.runs)}</td><td className={kit.num}>{int(bucket.succeeded)}</td>
          <td className={kit.num}>{bucket.failed > 0 ? <button type="button" className={styles.failedCount} disabled={!next} aria-label={`Inspect ${int(bucket.failed)} failed runs started ${stamp(bucket.at)}`} onClick={() => next && onOpenRuns({ ...next, status: "failed" })}>{int(bucket.failed)}</button> : int(bucket.failed)}</td>
          <td className={kit.num}>{int(bucket.collected)}</td><td className={kit.num}>{int(bucket.published)}</td>
        </tr>;
      })}</tbody>
    </table></div> : <p className={styles.empty}>No runs were recorded in these buckets. Include empty buckets to inspect the intervals.</p>}
    {rows.length > limit ? <Action onClick={() => setLimit(limit + 8)}>Show older buckets ({int(rows.length - limit)} remaining)</Action> : null}
  </div>;
}
