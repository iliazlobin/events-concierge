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
  AdminSourceHealthList,
  AdminStageSummary,
  AdminThroughput,
} from "@/lib/admin-types";

import styles from "./system-view.module.css";

/**
 * The system view.
 *
 * Every previous version of this screen was an exception report: what is late, what failed, what
 * is retrying. An operator opening it on a normal day learned nothing about how their platform
 * runs. This leads with the system — the workflow, its shape, its throughput, where the catalog
 * actually comes from — and demotes exceptions to a single line that expands only when there is
 * something to expand.
 */

const WINDOW_HOURS = 336;
const BUCKET_HOURS = 24;

interface Snapshot {
  fleet: AdminFleetSummary;
  stages: AdminStageSummary;
  shape: AdminFleetShape;
  throughput: AdminThroughput;
  concentration: AdminConcentration;
  health: AdminSourceHealthList;
}

const int = (n: number): string => n.toLocaleString("en-US");

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return int(n);
}

function ms(value: number | null): string {
  if (value === null) return "—";
  if (value >= 60_000) return `${(value / 60_000).toFixed(1)} min`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)} s`;
  return `${value} ms`;
}

/** A source is an exception when any of its four components has actually degraded. */
function isException(source: AdminSourceHealthList["sources"][number]): boolean {
  return (
    source.run_state === "failed"
    || source.run_state === "never_run"
    || (source.freshness_state !== "ok" && source.freshness_state !== "not_scheduled")
    || source.retry_state !== "ok"
    || source.yield_state === "zero_yield"
  );
}

export interface SystemViewProps {
  onOpenSource: (sourceKey: string) => void;
  onOpenExceptions: () => void;
}

export function SystemView({
  onOpenSource,
  onOpenExceptions,
}: SystemViewProps): React.JSX.Element {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const [fleet, stages, shape, throughput, concentration, health] = await Promise.all([
        getAdminFleetSummary(WINDOW_HOURS),
        getAdminStageSummary(WINDOW_HOURS),
        getAdminFleetShape(),
        getAdminThroughput(WINDOW_HOURS, BUCKET_HOURS),
        getAdminConcentration(12),
        getAdminSourceHealth(),
      ]);
      setSnap({ fleet, stages, shape, throughput, concentration, health });
    } catch {
      // A failed load is not an idle system, and must never render as zeros.
      setFailed(true);
      setSnap(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const exceptions = useMemo(
    () => (snap?.health.sources ?? []).filter(isException),
    [snap],
  );

  if (loading && !snap) return <div className={styles.state}>Reading the fleet…</div>;

  if (failed || !snap) {
    return (
      <div className={`${styles.state} ${styles.stateBad}`}>
        System data is unavailable — this is a load failure, not an idle fleet.
        <div>
          <button type="button" className={styles.retry} onClick={() => void load()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  const { fleet, stages, shape, throughput, concentration } = snap;

  const days = Math.max(1, Math.round(WINDOW_HOURS / 24));
  const perDay = (total: number): number => Math.round(total / days);

  const scheduled = shape.modes.reduce((t, m) => t + m.scheduled, 0);
  const collectStage = stages.stages.find((s) => s.stage === "collect");
  const foldedStages = stages.stages.filter(
    (s) => s.evidence_status === "not_separately_instrumented",
  );

  const buckets = throughput.buckets;
  const peak = Math.max(1, ...buckets.map((b) => b.collected));

  return (
    <div className={styles.shell}>

      {/* ---------------- 01 · FLOW ---------------- */}
      <section className={styles.section}>
        <div className={styles.head}>
          <span className={styles.mark}>01 — Flow</span>
          <h2 className={styles.title}>How an event reaches the catalog</h2>
          <p className={styles.lede}>
            Rates are the {days}-day daily mean. One stage owns almost all of the wall clock.
          </p>
        </div>

        <FlowDiagram
          scheduled={scheduled}
          modes={shape.modes.length}
          runsPerDay={perDay(fleet.runs)}
          collectedPerDay={perDay(fleet.candidates)}
          publishedPerDay={perDay(fleet.canonicals)}
          upcoming={shape.total_events}
          p50={fleet.duration_p50_ms}
          collectPct={collectStage?.pct_of_wall ?? null}
          foldedCount={foldedStages.length}
        />

        <p className={styles.cap}>
          <strong>The slot is the constraint.</strong> Exactly one fleet refresh command may be
          active at a time, so every source queues behind every other. {scheduled} scheduled
          sources are getting {perDay(fleet.runs)} runs a day between them.
          {foldedStages.length > 0 ? (
            <>
              {" "}
              {foldedStages.length} of the five declared stages are never timed separately — they
              are folded into the boundaries either side, and are shown as a gap rather than zero.
            </>
          ) : null}
        </p>
      </section>

      {/* ---------------- 02 · SHAPE ---------------- */}
      <section className={styles.section}>
        <div className={styles.head}>
          <span className={styles.mark}>02 — Shape</span>
          <h2 className={styles.title}>Where the sources are is not where the catalog is</h2>
          <p className={styles.lede}>
            Each adapter mode&rsquo;s share of the roster, against its share of what people can
            currently see.
          </p>
        </div>

        <div className={styles.scroll}>
          <table className={styles.tbl}>
            <thead>
              <tr>
                <th>Adapter mode</th>
                <th className={styles.num}>Sources</th>
                <th className={styles.num}>Share</th>
                <th className={styles.pair} />
                <th className={styles.num}>Events</th>
                <th className={styles.num}>Share</th>
                <th className={styles.num}>Per source</th>
              </tr>
            </thead>
            <tbody>
              {shape.modes.slice(0, 8).map((mode) => (
                <tr key={mode.mode}>
                  <td>{mode.mode}</td>
                  <td className={styles.num}><strong>{mode.sources}</strong></td>
                  <td className={styles.num}>{mode.pct_of_sources ?? 0}%</td>
                  <td className={styles.pair}>
                    <i
                      className={`${styles.bar} ${styles.barSrc}`}
                      style={{ width: `${mode.pct_of_sources ?? 0}%` }}
                    />
                    <i
                      className={`${styles.bar} ${styles.barEvt}`}
                      style={{ width: `${mode.pct_of_events ?? 0}%`, marginTop: "2px" }}
                    />
                  </td>
                  <td className={styles.num}><strong>{int(mode.upcoming_events)}</strong></td>
                  <td className={styles.num}>{mode.pct_of_events ?? 0}%</td>
                  <td className={styles.num}>{mode.events_per_source ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className={styles.cap}>
          Upper bar is share of sources, lower is share of events. Where they diverge, a mode is
          either carrying the catalog or costing slot time without contributing much — and both
          poll at the same price.
        </p>
      </section>

      {/* ---------------- 03 · THROUGHPUT ---------------- */}
      <section className={styles.section}>
        <div className={styles.head}>
          <span className={styles.mark}>03 — Throughput</span>
          <h2 className={styles.title}>{days} days of volume</h2>
        </div>

        <div className={styles.run}>
          <div className={styles.datum}>
            <span className={styles.v}>{int(perDay(fleet.candidates))}</span>
            <span className={styles.k}>Collected / day</span>
          </div>
          <div className={styles.datum}>
            <span className={styles.v}>{int(perDay(fleet.canonicals))}</span>
            <span className={styles.k}>Published / day</span>
          </div>
          <div className={`${styles.datum}`}>
            <span className={`${styles.v} ${styles.vOk}`}>
              {fleet.candidates
                ? `${((fleet.canonicals / fleet.candidates) * 100).toFixed(1)}%`
                : "—"}
            </span>
            <span className={styles.k}>Yield</span>
          </div>
          <div className={styles.datum}>
            <span className={styles.v}>{int(perDay(fleet.runs))}</span>
            <span className={styles.k}>Runs / day</span>
          </div>
          <div className={styles.datum}>
            <span className={styles.v}>{ms(fleet.duration_p50_ms)}</span>
            <span className={styles.k}>Median run</span>
          </div>
        </div>

        <VolumeSeries buckets={buckets} peak={peak} />

        <p className={styles.cap}>
          Records collected per day. Volume varies with how many sources the single slot reached;
          yield barely moves, because that is a property of the adapters rather than the schedule.
        </p>
      </section>

      {/* ---------------- 04 · COVERAGE ---------------- */}
      <section className={styles.section}>
        <div className={styles.head}>
          <span className={styles.mark}>04 — Coverage</span>
          <h2 className={styles.title}>Where the catalog comes from</h2>
          <p className={styles.lede}>
            {int(concentration.total_events)} upcoming events across{" "}
            {concentration.total_sources} contributing sources.
          </p>
        </div>

        <div className={styles.scroll}>
          <table className={styles.tbl}>
            <thead>
              <tr>
                <th className={styles.num}>#</th>
                <th>Source</th>
                <th className={styles.num}>Upcoming</th>
                <th className={styles.num}>Share</th>
                <th className={styles.num}>Cumulative</th>
                <th className={styles.pair} />
              </tr>
            </thead>
            <tbody>
              {concentration.sources.map((row) => (
                <tr key={row.source_key}>
                  <td className={styles.num}>{row.rank}</td>
                  <td>
                    <button
                      type="button"
                      className={styles.rowBtn}
                      onClick={() => onOpenSource(row.source_key)}
                    >
                      {row.display_name}
                    </button>
                    <span className={styles.sub}>{row.mode}</span>
                  </td>
                  <td className={styles.num}><strong>{int(row.upcoming_events)}</strong></td>
                  <td className={styles.num}>{row.pct ?? 0}%</td>
                  <td className={styles.num}>{row.cumulative_pct ?? 0}%</td>
                  <td className={styles.pair}>
                    <i
                      className={`${styles.bar} ${styles.barTrack}`}
                      style={{ width: "100%" }}
                    />
                    <i
                      className={`${styles.bar} ${styles.barEvt}`}
                      style={{
                        width: `${row.cumulative_pct ?? 0}%`,
                        marginTop: "-5px",
                      }}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className={styles.cap}>
          The cumulative column is the one that governs priority: a failure in the top rows costs
          more than every source below them combined.
        </p>
      </section>

      {/* ---------------- 05 · EXCEPTIONS ---------------- */}
      <section className={styles.section}>
        <div className={styles.exception}>
          {exceptions.length === 0 ? (
            <span>
              <strong className={styles.vOk}>All {snap.health.total} sources nominal.</strong>{" "}
              Nothing is failing, late, retrying or publishing empty.
            </span>
          ) : (
            <>
              <span>
                <strong className={styles.vBad}>{exceptions.length}</strong> of{" "}
                {snap.health.total} sources need a look
              </span>
              <span>
                {int(exceptions.reduce((t, s) => t + s.upcoming_events, 0))} events affected
              </span>
              <button type="button" className={styles.rowBtn} onClick={onOpenExceptions}>
                Open exceptions →
              </button>
            </>
          )}
        </div>
      </section>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function FlowDiagram({
  scheduled,
  modes,
  runsPerDay,
  collectedPerDay,
  publishedPerDay,
  upcoming,
  p50,
  collectPct,
  foldedCount,
}: {
  scheduled: number;
  modes: number;
  runsPerDay: number;
  collectedPerDay: number;
  publishedPerDay: number;
  upcoming: number;
  p50: number | null;
  collectPct: number | null;
  foldedCount: number;
}): React.JSX.Element {
  const nodes = [
    { x: 0, label: "Sources", a: `${scheduled} scheduled`, b: "4 cadence tiers" },
    { x: 180, label: "Scheduler", a: "tick 300s", b: "picks due" },
    { x: 350, label: "Slot", a: "concurrency 1", b: "fleet-wide" },
    { x: 500, label: "Worker", a: `${int(runsPerDay)} runs / day`, b: `p50 ${ms(p50)}` },
    { x: 660, label: "Adapters", a: `${modes} modes`, b: "fetch + parse" },
    { x: 840, label: "Catalog", a: `${int(upcoming)} upcoming`, b: "served now" },
  ];

  return (
    <svg
      className={styles.flow}
      viewBox="0 0 1000 250"
      role="img"
      aria-label={`Pipeline: ${scheduled} scheduled sources through a scheduler ticking every 300 seconds, a single fleet slot, a worker doing ${runsPerDay} runs per day, ${modes} adapter modes, producing ${collectedPerDay} collected and ${publishedPerDay} published records per day into a catalog of ${upcoming} upcoming events.`}
    >
      <defs>
        <marker id="sv-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M0,1 L7,4 L0,7" fill="none" stroke="var(--line-2)" strokeWidth="1" />
        </marker>
      </defs>

      <line x1="0" y1="108" x2="1000" y2="108" stroke="var(--line)" strokeWidth="1" />

      {nodes.map((n, i) => (
        <g key={n.label}>
          <rect x={n.x} y={102} width={2} height={12} fill="var(--ink)" />
          <text x={n.x} y={94} fontSize="13" fontWeight="600" fill="var(--ink)">{n.label}</text>
          <text x={n.x} y={134} fontSize="10.5" fontFamily="var(--mono)" fill="var(--dim)">{n.a}</text>
          <text x={n.x} y={151} fontSize="10.5" fontFamily="var(--mono)" fill="var(--faint)">{n.b}</text>
          {i < nodes.length - 1 ? (
            <line
              x1={n.x + 72}
              y1={108}
              x2={nodes[i + 1].x - 10}
              y2={108}
              stroke="var(--line-2)"
              strokeWidth="1"
              markerEnd="url(#sv-arrow)"
            />
          ) : null}
        </g>
      ))}

      <text x={655} y={72} fontSize="10" fontFamily="var(--mono)" fill="var(--cool)">
        {int(collectedPerDay)} collected / day
      </text>
      <text x={838} y={72} fontSize="10" fontFamily="var(--mono)" fill="var(--ok)">
        {int(publishedPerDay)} published / day
      </text>

      {/* stage wall clock, inside one run */}
      <line x1="500" y1="182" x2="962" y2="182" stroke="var(--line)" strokeWidth="1" />
      <text x={500} y={202} fontSize="9.5" fontFamily="var(--mono)" fill="var(--faint)" letterSpacing="1.5">
        STAGE WALL CLOCK, INSIDE EACH RUN
      </text>
      <rect x={500} y={212} width={3} height={13} fill="var(--dim)" />
      <rect x={506} y={212} width={446} height={13} fill="var(--mid)" />
      <rect x={955} y={212} width={3} height={13} fill="var(--dim)" />
      <text x={500} y={242} fontSize="9.5" fontFamily="var(--mono)" fill="var(--faint)">admission</text>
      <text x={690} y={242} fontSize="9.5" fontFamily="var(--mono)" fill="var(--mid)">
        collect{collectPct !== null ? ` · ${collectPct}%` : ""}
      </text>
      <text x={900} y={242} fontSize="9.5" fontFamily="var(--mono)" fill="var(--faint)">publish</text>

      {foldedCount > 0 ? (
        <>
          <text x={0} y={206} fontSize="10" fontFamily="var(--mono)" fill="var(--faint)">
            {foldedCount} of 5 stages are
          </text>
          <text x={0} y={222} fontSize="10" fontFamily="var(--mono)" fill="var(--faint)">
            never timed separately —
          </text>
          <text x={0} y={238} fontSize="10" fontFamily="var(--mono)" fill="var(--faint)">
            folded into these.
          </text>
        </>
      ) : null}
    </svg>
  );
}

function VolumeSeries({
  buckets,
  peak,
}: {
  buckets: AdminThroughput["buckets"];
  peak: number;
}): React.JSX.Element {
  const width = 1000;
  const height = 150;
  const gap = 4;
  const w = buckets.length ? Math.max(2, (width - gap * (buckets.length - 1)) / buckets.length) : 0;

  return (
    <svg
      className={styles.series}
      viewBox={`0 0 ${width} ${height + 26}`}
      role="img"
      aria-label={`Records collected per day across ${buckets.length} buckets, peaking at ${peak}.`}
    >
      <line x1="0" y1={height} x2={width} y2={height} stroke="var(--line)" strokeWidth="1" />
      {buckets.map((b, i) => {
        const h = Math.round((b.collected / peak) * (height - 18));
        const x = i * (w + gap);
        // A bucket with no runs is drawn as a baseline tick, never omitted: an absent day is a
        // fact about the scheduler, not missing data.
        return b.runs === 0 ? (
          <rect key={b.bucket_start} x={x} y={height - 2} width={w} height={2} fill="var(--faint)" opacity="0.5" />
        ) : (
          <rect key={b.bucket_start} x={x} y={height - h} width={w} height={Math.max(2, h)} fill="var(--cool)" opacity="0.85" />
        );
      })}
      <text x="0" y={height + 18} fontSize="9.5" fontFamily="var(--mono)" fill="var(--faint)">
        {buckets.length ? new Date(buckets[0].bucket_start).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : ""}
      </text>
      <text x={width} y={height + 18} fontSize="9.5" fontFamily="var(--mono)" fill="var(--faint)" textAnchor="end">
        {compact(peak)} peak
      </text>
    </svg>
  );
}
