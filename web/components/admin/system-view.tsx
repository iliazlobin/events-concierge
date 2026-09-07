"use client";

import { ArrowRight, CheckCircle2, CircleAlert, Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getAdminConcentration,
  getAdminFleetShape,
  getAdminFleetSummary,
  getAdminSourceHealth,
  getAdminStageSummary,
  getAdminThroughput,
} from "@/lib/admin-api";
import type {
  AdminConcentration,
  AdminFleetShape,
  AdminFleetSummary,
  AdminSourceHealth,
  AdminSourceHealthList,
  AdminStageSummary,
  AdminThroughput,
} from "@/lib/admin-types";

import {
  Action,
  Chip,
  LoadState,
  Metrics,
  PageHead,
  Section,
  Segment,
  SortHeader,
  age,
  compare,
  int,
  kit,
  ms,
  type MetricSpec,
  type SortDir,
  type Tone,
} from "./console-kit";

/**
 * The system view.
 *
 * Leads with how the platform runs — the workflow, its shape, its throughput, where the catalog
 * comes from — and demotes exceptions to a lens over the same table. Every figure is a control:
 * the metric strip filters, every column sorts, every row opens its source.
 */

const WINDOWS = [
  { value: "168", label: "7d" },
  { value: "336", label: "14d" },
  { value: "720", label: "30d" },
] as const;

type Lens =
  | "all"
  | "exceptions"
  | "failing"
  | "late"
  | "retrying"
  | "empty"
  | "carrying"
  | "quiet";

const LENSES: ReadonlyArray<{ value: Lens; label: string }> = [
  { value: "all", label: "All" },
  { value: "exceptions", label: "Attention" },
  { value: "failing", label: "Failing" },
  { value: "late", label: "Late" },
  { value: "retrying", label: "Retries" },
  { value: "empty", label: "Empty" },
  { value: "carrying", label: "Carrying" },
  { value: "quiet", label: "Quiet" },
];

interface Snapshot {
  fleet: AdminFleetSummary;
  stages: AdminStageSummary;
  shape: AdminFleetShape;
  throughput: AdminThroughput;
  concentration: AdminConcentration;
  health: AdminSourceHealthList;
}

function isOnCadence(s: AdminSourceHealth): boolean {
  return s.enabled && !s.retired_at;
}

function isException(s: AdminSourceHealth): boolean {
  return (
    isOnCadence(s)
    && (
      s.run_state === "failed"
      || s.run_state === "never_run"
      || (s.freshness_state !== "ok" && s.freshness_state !== "not_scheduled")
      || s.retry_state !== "ok"
      || s.yield_state === "zero_yield"
    )
  );
}

function isFailing(s: AdminSourceHealth): boolean {
  return isOnCadence(s) && (s.run_state === "failed" || s.retry_state === "severe");
}

function isLate(s: AdminSourceHealth): boolean {
  return isOnCadence(s) && (s.freshness_state === "late" || s.freshness_state === "down");
}

function isRetrying(s: AdminSourceHealth): boolean {
  return isOnCadence(s) && (s.retry_state === "elevated" || s.retry_state === "severe");
}

function isEmpty(s: AdminSourceHealth): boolean {
  return isOnCadence(s) && s.yield_state === "zero_yield";
}

function healthTone(s: AdminSourceHealth): Tone {
  if (isException(s)) {
    return s.run_state === "failed" || s.retry_state === "severe" ? "bad" : "warn";
  }
  if (s.freshness_state === "not_scheduled") return "neutral";
  return "ok";
}

function healthWord(s: AdminSourceHealth): string {
  if (s.retired_at) return "retired";
  if (!s.enabled) return "paused";
  if (s.run_state === "failed") return "failing";
  if (s.run_state === "never_run") return "never ran";
  if (s.retry_state === "severe") return "retry storm";
  if (s.yield_state === "zero_yield") return "empty";
  if (s.freshness_state === "late" || s.freshness_state === "down") return "late";
  if (s.retry_state === "elevated" || s.freshness_state === "warn") return "watch";
  return "nominal";
}

export interface SystemViewProps {
  onOpenSource: (sourceKey: string) => void;
  onRefreshDue: () => void;
  refreshSubmitting: boolean;
  refreshDuePending: boolean;
}

export function SystemView({
  onOpenSource,
  onRefreshDue,
  refreshSubmitting,
  refreshDuePending,
}: SystemViewProps): React.JSX.Element {
  const [windowHours, setWindowHours] = useState("336");
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [lens, setLens] = useState<Lens>("all");
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState("events");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [openMode, setOpenMode] = useState<string | null>(null);

  const load = useCallback(async (hours: string) => {
    setLoading(true);
    setFailed(false);
    const n = Number(hours);
    try {
      const [fleet, stages, shape, throughput, concentration, health] = await Promise.all([
        getAdminFleetSummary(n),
        getAdminStageSummary(n),
        getAdminFleetShape(),
        getAdminThroughput(n, 24),
        getAdminConcentration(200),
        getAdminSourceHealth(),
      ]);
      setSnap({ fleet, stages, shape, throughput, concentration, health });
    } catch {
      setFailed(true);
      setSnap(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(windowHours);
  }, [load, windowHours]);

  const sources = useMemo(() => snap?.health.sources ?? [], [snap]);
  const exceptions = useMemo(() => sources.filter(isException), [sources]);

  const eventsByKey = useMemo(() => {
    const map = new Map<string, number>();
    for (const row of snap?.concentration.sources ?? []) map.set(row.source_key, row.upcoming_events);
    return map;
  }, [snap]);

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    let list = sources;
    if (lens === "exceptions") list = list.filter(isException);
    if (lens === "failing") list = list.filter(isFailing);
    if (lens === "late") list = list.filter(isLate);
    if (lens === "retrying") list = list.filter(isRetrying);
    if (lens === "empty") list = list.filter(isEmpty);
    if (lens === "quiet") list = list.filter((s) => !isException(s) && s.upcoming_events === 0);
    if (lens === "carrying") {
      list = [...list].sort((a, b) => b.upcoming_events - a.upcoming_events).slice(0, 15);
    }
    if (openMode) list = list.filter((s) => s.mode === openMode);
    if (needle) {
      list = list.filter(
        (s) =>
          s.source_key.toLowerCase().includes(needle)
          || s.display_name.toLowerCase().includes(needle)
          || s.mode.toLowerCase().includes(needle)
          || s.publisher.toLowerCase().includes(needle),
      );
    }
    const pick = (s: AdminSourceHealth): string | number | null => {
      switch (sortKey) {
        case "source": return s.display_name;
        case "mode": return s.mode;
        case "state": return healthWord(s);
        case "attempts": return s.latest_attempt_count ?? null;
        case "success": return s.last_success_at ? new Date(s.last_success_at).getTime() : null;
        default: return s.upcoming_events;
      }
    };
    return [...list].sort((a, b) => compare(pick(a), pick(b), sortDir));
  }, [sources, lens, openMode, query, sortKey, sortDir]);

  const onSort = (key: string) => {
    if (key === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir(key === "source" || key === "mode" ? "asc" : "desc");
    }
  };

  if (loading && !snap) return <LoadState failed={false} onRetry={() => void load(windowHours)} label="Reading the fleet…" />;
  if (failed || !snap) return <LoadState failed onRetry={() => void load(windowHours)} label="" />;

  const { fleet, stages, shape, throughput, concentration } = snap;
  const days = Math.max(1, Math.round(Number(windowHours) / 24));
  const perDay = (total: number) => Math.round(total / days);
  const scheduled = shape.modes.reduce((t, m) => t + m.scheduled, 0);
  const collectStage = stages.stages.find((s) => s.stage === "collect");
  const folded = stages.stages.filter((s) => s.evidence_status === "not_separately_instrumented");
  const yieldPct = fleet.candidates ? (fleet.canonicals / fleet.candidates) * 100 : null;
  const failing = sources.filter(isFailing);
  const late = sources.filter(isLate);
  const retrying = sources.filter(isRetrying);
  const empty = sources.filter(isEmpty);
  const monitored = sources.filter(isOnCadence);
  const nominal = monitored.length - exceptions.length;
  const affectedEvents = exceptions.reduce((total, source) => total + source.upcoming_events, 0);
  const nominalPct = monitored.length ? (nominal / monitored.length) * 100 : 0;

  const selectLens = (next: Lens, focusRegistry = false) => {
    setLens(next);
    setOpenMode(null);
    if (focusRegistry) {
      window.requestAnimationFrame(() => {
        document.getElementById("system-registry")?.scrollIntoView({
          behavior: "smooth",
          block: "start",
        });
      });
    }
  };

  const metrics: MetricSpec[] = [
    { key: "all", label: "Reviewed sources", value: int(snap.health.total), note: `${scheduled} on cadence`, interactive: true },
    { key: "carrying", label: "Catalog now", value: int(concentration.total_events), note: "upcoming events", interactive: true },
    { key: "throughput", label: "Collected / day", value: int(perDay(fleet.candidates)), note: `${int(perDay(fleet.runs))} runs` },
    { key: "yield", label: "Publish yield", value: yieldPct === null ? "—" : `${yieldPct.toFixed(1)}%`, note: "collected → published", tone: "ok" },
    {
      key: "exceptions",
      label: "Needs attention",
      value: int(exceptions.length),
      note: exceptions.length ? `${int(affectedEvents)} events affected` : "all nominal",
      tone: exceptions.length ? "bad" : "ok",
      interactive: true,
    },
  ];

  return (
    <div className={kit.page}>
      <PageHead
        eyebrow="Control room / fleet overview"
        title="Ingestion system"
        sub="Understand fleet health, protect catalog coverage, and move from an exception to its owning source without leaving the operating context."
        statValue={age(fleet.generated_at)}
        statLabel="snapshot age"
      />

      <div className={kit.toolbar}>
        <Segment options={WINDOWS} value={windowHours} onChange={setWindowHours} label="Window" />
        <span className={kit.spacer} />
        <Action onClick={() => void load(windowHours)} disabled={loading}>
          <RefreshCw aria-hidden="true" className={loading ? "spin" : undefined} />
          {loading ? "Refreshing" : "Refresh data"}
        </Action>
        <Action onClick={onRefreshDue} disabled={refreshSubmitting || refreshDuePending} primary>
          <Play aria-hidden="true" />
          {refreshDuePending ? "Run in flight" : "Run due sources"}
        </Action>
      </div>

      <Metrics
        items={metrics}
        selected={lens === "all" ? null : lens}
        onSelect={(key) => {
          if (key === "throughput" || key === "yield") return;
          selectLens(lens === key ? "all" : (key as Lens), true);
        }}
      />

      <SystemHealthSummary
        total={monitored.length}
        nominal={nominal}
        nominalPct={nominalPct}
        exceptions={exceptions.length}
        affectedEvents={affectedEvents}
        lens={lens}
        issues={[
          { lens: "failing", label: "Failing", value: failing.length, note: "failed run or severe retry" },
          { lens: "late", label: "Late", value: late.length, note: "outside freshness target" },
          { lens: "retrying", label: "Retrying", value: retrying.length, note: "elevated claim pressure" },
          { lens: "empty", label: "Empty", value: empty.length, note: "successful run, zero output" },
        ]}
        onSelect={(next) => selectLens(next, true)}
      />

      {/* ---------------- flow ---------------- */}
      <Section title="System flow" scope={`${days}-day daily mean`}>
        <FlowDiagram
          scheduled={scheduled}
          modes={shape.modes.length}
          runsPerDay={perDay(fleet.runs)}
          collectedPerDay={perDay(fleet.candidates)}
          publishedPerDay={perDay(fleet.canonicals)}
          upcoming={concentration.total_events}
          p50={fleet.duration_p50_ms}
          collectPct={collectStage?.pct_of_wall ?? null}
          foldedCount={folded.length}
        />
        <p className={kit.cap}>
          <b>The slot is the constraint.</b> One fleet refresh command runs at a time, so every
          source queues behind every other — {scheduled} scheduled sources share{" "}
          {int(perDay(fleet.runs))} runs a day.
          {folded.length ? ` ${folded.length} of five stages are never timed separately; they are folded into the boundaries either side and drawn as a gap, not zero.` : ""}
        </p>
      </Section>

      {/* ---------------- shape ---------------- */}
      <Section title="Shape" scope="roster share vs catalog share">
        <div className={kit.tableScroll}>
          <table className={kit.table}>
            <thead>
              <tr>
                <th>Adapter mode</th>
                <th className={kit.num}>Sources</th>
                <th className={kit.num}>Share</th>
                <th className={kit.pairCell} />
                <th className={kit.num}>Events</th>
                <th className={kit.num}>Share</th>
                <th className={kit.num}>Per source</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {shape.modes.slice(0, 10).map((mode) => (
                <tr key={mode.mode}>
                  <td>
                    <button
                      type="button"
                      className={kit.rowBtn}
                      onClick={() => {
                        setOpenMode((c) => (c === mode.mode ? null : mode.mode));
                        setLens("all");
                      }}
                    >
                      {mode.mode}
                    </button>
                    <span className={kit.rowSub}>
                      {mode.scheduled} scheduled · {mode.paused} paused · {mode.retired} retired
                    </span>
                  </td>
                  <td className={`${kit.num} ${kit.big}`}>{mode.sources}</td>
                  <td className={`${kit.num} ${kit.mono}`}>{mode.pct_of_sources ?? 0}%</td>
                  <td className={kit.pairCell}>
                    <i className={`${kit.bar} ${kit.barCool}`} style={{ width: `${mode.pct_of_sources ?? 0}%` }} />
                    <i className={`${kit.bar} ${kit.barAccent}`} style={{ width: `${mode.pct_of_events ?? 0}%`, marginTop: 2 }} />
                  </td>
                  <td className={`${kit.num} ${kit.big}`}>{int(mode.upcoming_events)}</td>
                  <td className={`${kit.num} ${kit.mono}`}>{mode.pct_of_events ?? 0}%</td>
                  <td className={`${kit.num} ${kit.mono}`}>{mode.events_per_source ?? 0}</td>
                  <td className={kit.num}>
                    {openMode === mode.mode ? <Chip tone="info">filtering</Chip> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className={kit.cap}>
          Upper bar is share of sources, lower is share of events. Where they diverge, a mode is
          either carrying the catalog or costing slot time without contributing much — both poll at
          the same price. Select a mode to filter the registry below.
        </p>
      </Section>

      {/* ---------------- throughput ---------------- */}
      <Section title="Throughput" scope={`${throughput.buckets.length} daily buckets`}>
        <VolumeSeries buckets={throughput.buckets} />
        <p className={kit.cap}>
          Records collected per day. A day with no runs draws as a baseline tick rather than
          vanishing — an absent day is a fact about the scheduler, not missing data.
        </p>
      </Section>

      {/* ---------------- registry ---------------- */}
      <Section
        id="system-registry"
        title="Registry"
        scope={`${rows.length} of ${snap.health.total} sources${openMode ? ` · ${openMode}` : ""}`}
      >
        <div className={kit.toolbar}>
          <input
            className={kit.search}
            placeholder="Search source, publisher, or adapter"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <Segment options={LENSES} value={lens} onChange={(value) => selectLens(value)} label="Lens" />
          {openMode ? <Action onClick={() => setOpenMode(null)}>Clear {openMode}</Action> : null}
        </div>

        <div className={kit.tableScroll}>
          <table className={kit.table}>
            <thead>
              <tr>
                <SortHeader label="Source" columnKey="source" active={sortKey === "source"} dir={sortDir} onSort={onSort} />
                <SortHeader label="State" columnKey="state" active={sortKey === "state"} dir={sortDir} onSort={onSort} />
                <SortHeader label="Adapter" columnKey="mode" active={sortKey === "mode"} dir={sortDir} onSort={onSort} />
                <SortHeader label="Serving" columnKey="events" active={sortKey === "events"} dir={sortDir} onSort={onSort} align="right" />
                <SortHeader label="Runs" columnKey="attempts" active={sortKey === "attempts"} dir={sortDir} onSort={onSort} align="right" />
                <SortHeader label="Last success" columnKey="success" active={sortKey === "success"} dir={sortDir} onSort={onSort} align="right" />
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={6} className={kit.empty}>No sources match this filter.</td>
                </tr>
              ) : (
                rows.slice(0, 60).map((s) => (
                  <tr key={s.source_key}>
                    <td>
                      <button type="button" className={kit.rowBtn} onClick={() => onOpenSource(s.source_key)}>
                        {s.display_name}
                      </button>
                      <span className={kit.rowSub}>{s.source_key}</span>
                    </td>
                    <td><Chip tone={healthTone(s)}>{healthWord(s)}</Chip></td>
                    <td className={kit.mono}>{s.mode}</td>
                    <td className={`${kit.num} ${kit.big}`}>{int(eventsByKey.get(s.source_key) ?? s.upcoming_events)}</td>
                    <td className={`${kit.num} ${kit.mono}`}>
                      {s.latest_attempt_count === null ? "—" : `${s.latest_attempt_count}×`}
                    </td>
                    <td className={`${kit.num} ${kit.mono}`}>{age(s.last_success_at)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        {rows.length > 60 ? (
          <p className={kit.cap}>Showing the first 60 of {rows.length} matching. Narrow with search or a lens.</p>
        ) : null}
      </Section>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function SystemHealthSummary({
  total,
  nominal,
  nominalPct,
  exceptions,
  affectedEvents,
  lens,
  issues,
  onSelect,
}: {
  total: number;
  nominal: number;
  nominalPct: number;
  exceptions: number;
  affectedEvents: number;
  lens: Lens;
  issues: ReadonlyArray<{ lens: Lens; label: string; value: number; note: string }>;
  onSelect: (lens: Lens) => void;
}): React.JSX.Element {
  const attention = exceptions > 0;
  return (
    <section className={kit.systemSummary} aria-labelledby="fleet-health-title">
      <div className={kit.summaryStatus}>
        <span className={kit.summaryIcon} data-tone={attention ? "bad" : "ok"} aria-hidden="true">
          {attention ? <CircleAlert /> : <CheckCircle2 />}
        </span>
        <div>
          <span className={kit.summaryEyebrow}>Current posture</span>
          <h2 className={kit.summaryTitle} id="fleet-health-title">
            {attention ? "Fleet attention required" : "Fleet operating normally"}
          </h2>
          <p className={kit.summaryCopy}>
            {attention
              ? `${int(exceptions)} of ${int(total)} sources have an active health signal. ${int(affectedEvents)} upcoming events currently depend on those sources.`
              : `All ${int(total)} reviewed sources are inside their current health targets.`}
          </p>
          {attention ? (
            <button type="button" className={kit.summaryLink} onClick={() => onSelect("exceptions")}>
              Review all affected sources <ArrowRight aria-hidden="true" />
            </button>
          ) : null}
        </div>
        <div className={kit.summaryMeter} aria-label={`${nominal} of ${total} sources nominal`}>
          <span className={kit.summaryMeterValue}>{Math.round(nominalPct)}%</span>
          <span className={kit.summaryMeterLabel}>nominal sources</span>
          <div className={kit.summaryMeterTrack} aria-hidden="true">
            <i style={{ width: `${Math.max(0, Math.min(100, nominalPct))}%` }} />
          </div>
          <span className={kit.summaryMeterNote}>{int(nominal)} nominal · {int(exceptions)} attention</span>
        </div>
      </div>

      <div className={kit.issueGrid} role="group" aria-label="Filter registry by health signal">
        {issues.map((issue) => (
          <button
            type="button"
            key={issue.lens}
            className={`${kit.issueButton} ${lens === issue.lens ? kit.issueButtonActive : ""}`}
            onClick={() => onSelect(issue.lens)}
            aria-pressed={lens === issue.lens}
          >
            <span className={kit.issueCount}>{int(issue.value)}</span>
            <span className={kit.issueLabel}>{issue.label}</span>
            <span className={kit.issueNote}>{issue.note}</span>
            <ArrowRight aria-hidden="true" />
          </button>
        ))}
      </div>
    </section>
  );
}

function FlowDiagram(props: {
  scheduled: number; modes: number; runsPerDay: number; collectedPerDay: number;
  publishedPerDay: number; upcoming: number; p50: number | null;
  collectPct: number | null; foldedCount: number;
}): React.JSX.Element {
  const nodes = [
    { label: "Sources", value: `${props.scheduled} scheduled`, note: "4 cadence tiers" },
    { label: "Scheduler", value: "Every 5 min", note: "selects work that is due" },
    { label: "Shared slot", value: "Concurrency 1", note: "fleet-wide constraint" },
    { label: "Worker", value: `${int(props.runsPerDay)} runs / day`, note: `p50 ${ms(props.p50)}` },
    { label: "Adapters", value: `${props.modes} modes`, note: "collect + parse" },
    { label: "Catalog", value: `${int(props.upcoming)} upcoming`, note: "available to consumers" },
  ];
  return (
    <figure
      className={kit.flowMap}
      aria-label={`Pipeline: ${props.scheduled} scheduled sources, scheduler ticking every 300 seconds, one fleet slot, ${props.runsPerDay} runs per day across ${props.modes} adapters, ${props.collectedPerDay} collected and ${props.publishedPerDay} published per day, catalog of ${props.upcoming} upcoming events.`}
    >
      <div className={kit.flowThroughput}>
        <span data-tone="info"><b>{int(props.collectedPerDay)}</b> collected / day</span>
        <ArrowRight aria-hidden="true" />
        <span data-tone="ok"><b>{int(props.publishedPerDay)}</b> published / day</span>
      </div>
      <div className={kit.flowNodes}>
        {nodes.map((node, index) => (
          <article className={kit.flowNode} key={node.label}>
            <span className={kit.flowStep}>{String(index + 1).padStart(2, "0")}</span>
            <strong>{node.label}</strong>
            <span className={kit.flowValue}>{node.value}</span>
            <small>{node.note}</small>
          </article>
        ))}
      </div>
      <div className={kit.stageProfile}>
        <div className={kit.stageIntro}>
          <span>Stage wall clock inside each run</span>
          <strong>Collection owns {props.collectPct ?? "—"}%</strong>
          {props.foldedCount > 0 ? (
            <small>{props.foldedCount} of 5 stages are folded into measured boundaries.</small>
          ) : null}
        </div>
        <div>
          <div className={kit.stageTrack} aria-hidden="true">
            <i className={kit.stageMarker} />
            <i className={kit.stageMeasured} style={{ width: `${Math.max(0, Math.min(100, props.collectPct ?? 0))}%` }} />
            <i className={kit.stageMarker} />
          </div>
          <div className={kit.stageLabels}>
            <span>admission</span>
            <span>collect</span>
            <span>publish</span>
          </div>
        </div>
      </div>
    </figure>
  );
}

function VolumeSeries({ buckets }: { buckets: AdminThroughput["buckets"] }): React.JSX.Element {
  const [hover, setHover] = useState<number | null>(null);
  const width = 1000;
  const height = 140;
  const gap = 4;
  const peak = Math.max(1, ...buckets.map((b) => b.collected));
  const w = buckets.length ? Math.max(2, (width - gap * (buckets.length - 1)) / buckets.length) : 0;
  const active = hover !== null ? buckets[hover] : null;

  return (
    <div>
      <svg className={kit.figure} viewBox={`0 0 ${width} ${height + 30}`} role="img"
        aria-label={`Records collected per day across ${buckets.length} buckets, peaking at ${peak}.`}>
        <line x1="0" y1={height} x2={width} y2={height} stroke="rgba(255,255,255,0.1)" strokeWidth="1" />
        {buckets.map((b, i) => {
          const h = Math.round((b.collected / peak) * (height - 16));
          const x = i * (w + gap);
          const on = hover === i;
          return (
            <g key={b.bucket_start}>
              <rect x={x} y={0} width={w} height={height} fill="transparent"
                onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} style={{ cursor: "pointer" }} />
              {b.runs === 0 ? (
                <rect x={x} y={height - 2} width={w} height={2} fill="#686865" opacity="0.6" pointerEvents="none" />
              ) : (
                <rect x={x} y={height - h} width={w} height={Math.max(2, h)}
                  fill={on ? "#c9ff68" : "#a783ef"} opacity={on ? 1 : 0.75} pointerEvents="none" />
              )}
            </g>
          );
        })}
        <text x="0" y={height + 20} fontSize="9" fontFamily="ui-monospace, monospace" fill="#686865">
          {buckets.length ? new Date(buckets[0].bucket_start).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : ""}
        </text>
        <text x={width} y={height + 20} fontSize="9" fontFamily="ui-monospace, monospace" fill="#686865" textAnchor="end">
          {int(peak)} peak
        </text>
      </svg>
      <p className={kit.cap} style={{ minHeight: 18 }}>
        {active ? (
          <>
            <b>{new Date(active.bucket_start).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</b>
            {" · "}{int(active.collected)} collected → {int(active.published)} published
            {active.yield_pct !== null ? ` · ${active.yield_pct}% yield` : ""}
            {" · "}{active.runs} runs{active.failed ? `, ${active.failed} failed` : ""}
          </>
        ) : (
          "Hover a bar for that day's detail."
        )}
      </p>
    </div>
  );
}
