"use client";

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

type Lens = "all" | "carrying" | "exceptions" | "quiet";

const LENSES: ReadonlyArray<{ value: Lens; label: string }> = [
  { value: "all", label: "All" },
  { value: "carrying", label: "Carrying" },
  { value: "exceptions", label: "Exceptions" },
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

function isException(s: AdminSourceHealth): boolean {
  return (
    s.run_state === "failed"
    || s.run_state === "never_run"
    || (s.freshness_state !== "ok" && s.freshness_state !== "not_scheduled")
    || s.retry_state !== "ok"
    || s.yield_state === "zero_yield"
  );
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

  const metrics: MetricSpec[] = [
    { key: "all", label: "Sources", value: int(snap.health.total), note: `${scheduled} on cadence` },
    { key: "carrying", label: "Serving now", value: int(concentration.total_events), note: "upcoming events" },
    { key: "throughput", label: "Collected / day", value: int(perDay(fleet.candidates)), note: `${int(perDay(fleet.runs))} runs` },
    { key: "yield", label: "Yield", value: yieldPct === null ? "—" : `${yieldPct.toFixed(1)}%`, note: "candidate → canonical", tone: "ok" },
    {
      key: "exceptions",
      label: "Exceptions",
      value: int(exceptions.length),
      note: exceptions.length ? `${int(exceptions.reduce((t, s) => t + s.upcoming_events, 0))} events` : "all nominal",
      tone: exceptions.length ? "bad" : "ok",
    },
  ];

  return (
    <div className={kit.page}>
      <PageHead
        eyebrow="platform / system"
        title="Ingestion system"
        sub="The workflow end to end, the shape of the fleet against the catalog it produces, and the volume moving through it."
      />

      <div className={kit.toolbar}>
        <Segment options={WINDOWS} value={windowHours} onChange={setWindowHours} label="Window" />
        <span className={kit.spacer} />
        <Action onClick={() => void load(windowHours)} disabled={loading}>
          {loading ? "Refreshing" : "Refresh"}
        </Action>
        <Action onClick={onRefreshDue} disabled={refreshSubmitting || refreshDuePending} primary>
          {refreshDuePending ? "Run in flight" : "Run due sources"}
        </Action>
      </div>

      <Metrics
        items={metrics}
        selected={lens === "all" ? null : lens}
        onSelect={(key) => {
          if (key === "throughput" || key === "yield") return;
          setLens((current) => (current === key ? "all" : (key as Lens)));
          setOpenMode(null);
        }}
      />

      {/* ---------------- flow ---------------- */}
      <Section title="Flow" scope={`${days}-day daily mean`}>
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
          <Segment options={LENSES} value={lens} onChange={(v) => { setLens(v); setOpenMode(null); }} label="Lens" />
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

function FlowDiagram(props: {
  scheduled: number; modes: number; runsPerDay: number; collectedPerDay: number;
  publishedPerDay: number; upcoming: number; p50: number | null;
  collectPct: number | null; foldedCount: number;
}): React.JSX.Element {
  const nodes = [
    { x: 0, label: "Sources", a: `${props.scheduled} scheduled`, b: "4 cadence tiers" },
    { x: 180, label: "Scheduler", a: "tick 300s", b: "picks due" },
    { x: 350, label: "Slot", a: "concurrency 1", b: "fleet-wide" },
    { x: 500, label: "Worker", a: `${int(props.runsPerDay)} runs / day`, b: `p50 ${ms(props.p50)}` },
    { x: 665, label: "Adapters", a: `${props.modes} modes`, b: "fetch + parse" },
    { x: 845, label: "Catalog", a: `${int(props.upcoming)} upcoming`, b: "served now" },
  ];
  return (
    <svg className={kit.figure} viewBox="0 0 1000 250" role="img"
      aria-label={`Pipeline: ${props.scheduled} scheduled sources, scheduler ticking every 300 seconds, one fleet slot, ${props.runsPerDay} runs per day across ${props.modes} adapters, ${props.collectedPerDay} collected and ${props.publishedPerDay} published per day, catalog of ${props.upcoming} upcoming events.`}>
      <defs>
        <marker id="sv-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M0,1 L7,4 L0,7" fill="none" stroke="rgba(255,255,255,0.18)" strokeWidth="1" />
        </marker>
      </defs>
      <line x1="0" y1="108" x2="1000" y2="108" stroke="rgba(255,255,255,0.1)" strokeWidth="1" />
      {nodes.map((n, i) => (
        <g key={n.label}>
          <rect x={n.x} y={102} width={2} height={12} fill="#c9ff68" />
          <text x={n.x} y={93} fontSize="12.5" fontWeight="600" fill="#f5f5f0">{n.label}</text>
          <text x={n.x} y={132} fontSize="10" fontFamily="ui-monospace, monospace" fill="#9b9b96">{n.a}</text>
          <text x={n.x} y={149} fontSize="10" fontFamily="ui-monospace, monospace" fill="#686865">{n.b}</text>
          {i < nodes.length - 1 ? (
            <line x1={n.x + 74} y1={108} x2={nodes[i + 1].x - 10} y2={108}
              stroke="rgba(255,255,255,0.18)" strokeWidth="1" markerEnd="url(#sv-arrow)" />
          ) : null}
        </g>
      ))}
      <text x={660} y={72} fontSize="10" fontFamily="ui-monospace, monospace" fill="#a783ef">
        {int(props.collectedPerDay)} collected / day
      </text>
      <text x={843} y={72} fontSize="10" fontFamily="ui-monospace, monospace" fill="#c9ff68">
        {int(props.publishedPerDay)} published / day
      </text>
      <line x1="500" y1="182" x2="962" y2="182" stroke="rgba(255,255,255,0.1)" strokeWidth="1" />
      <text x={500} y={201} fontSize="8.5" fontFamily="ui-monospace, monospace" fill="#686865" letterSpacing="1.6">
        STAGE WALL CLOCK, INSIDE EACH RUN
      </text>
      <rect x={500} y={211} width={3} height={13} fill="#9b9b96" />
      <rect x={506} y={211} width={446} height={13} fill="#ffd074" opacity="0.85" />
      <rect x={955} y={211} width={3} height={13} fill="#9b9b96" />
      <text x={500} y={240} fontSize="9" fontFamily="ui-monospace, monospace" fill="#686865">admission</text>
      <text x={690} y={240} fontSize="9" fontFamily="ui-monospace, monospace" fill="#ffd98e">
        collect{props.collectPct !== null ? ` · ${props.collectPct}%` : ""}
      </text>
      <text x={903} y={240} fontSize="9" fontFamily="ui-monospace, monospace" fill="#686865">publish</text>
      {props.foldedCount > 0 ? (
        <>
          <text x={0} y={205} fontSize="9.5" fontFamily="ui-monospace, monospace" fill="#686865">{props.foldedCount} of 5 stages are</text>
          <text x={0} y={221} fontSize="9.5" fontFamily="ui-monospace, monospace" fill="#686865">never timed separately —</text>
          <text x={0} y={237} fontSize="9.5" fontFamily="ui-monospace, monospace" fill="#686865">folded into these.</text>
        </>
      ) : null}
    </svg>
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
