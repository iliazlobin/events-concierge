"use client";

import { useCallback, useEffect, useState } from "react";

import {
  getAdminCatalogFreshness,
  getAdminFleetSummary,
  getAdminSourceHealth,
  getAdminStageSummary,
} from "@/lib/admin-api";
import { buildStageFlow, stageFlowEmptyReason } from "@/lib/admin-stage-flow";
import type {
  AdminCatalogFreshness,
  AdminFleetSummary,
  AdminSourceHealth,
  AdminSourceHealthList,
  AdminStageSummary,
} from "@/lib/admin-types";

/**
 * A review surface for the server-computed projections.
 *
 * This is a NEW route rather than a rewrite of the 3,859-line console, so the redesign can be
 * evaluated against live data without destabilising the screen the operator uses today. Every
 * number here comes from one aggregate endpoint; nothing is reduced in the browser.
 */

const WINDOWS = [
  { hours: 24, label: "24h" },
  { hours: 168, label: "7d" },
  { hours: 720, label: "30d" },
] as const;

const HEALTH_ORDER = [
  "down",
  "never_succeeded",
  "late",
  "warn",
  "paused",
  "retired",
  "healthy",
] as const;

const HEALTH_TONE: Record<string, "bad" | "mid" | "unk" | "ok"> = {
  down: "bad",
  never_succeeded: "bad",
  late: "mid",
  warn: "mid",
  paused: "unk",
  retired: "unk",
  healthy: "ok",
};

const HEALTH_GLYPH: Record<string, string> = {
  down: "✕",
  never_succeeded: "✕",
  late: "▲",
  warn: "▲",
  paused: "‒",
  retired: "‒",
  healthy: "✓",
};

const FRESHNESS_TONE: Record<string, "ok" | "mid" | "bad" | "unk"> = {
  fresh: "ok",
  aging: "mid",
  stale: "mid",
  dead: "bad",
  never: "unk",
};

function integer(value: number): string {
  return value.toLocaleString("en-US");
}

function duration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} s`;
  return `${ms} ms`;
}

function age(iso: string | null): string {
  if (!iso) return "never";
  const delta = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(delta)) return "—";
  const minutes = delta / 60_000;
  if (minutes < 60) return `${Math.max(0, Math.round(minutes))} min`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1)} h`;
  return `${(hours / 24).toFixed(1)} d`;
}

interface Loaded {
  fleet: AdminFleetSummary;
  stages: AdminStageSummary;
  freshness: AdminCatalogFreshness;
  health: AdminSourceHealthList;
}

export function ReviewConsole(): React.JSX.Element {
  const [windowHours, setWindowHours] = useState<number>(720);
  const [data, setData] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [elapsedMs, setElapsedMs] = useState<number | null>(null);

  const load = useCallback(async (hours: number) => {
    setLoading(true);
    setFailed(false);
    const started = performance.now();
    try {
      // Four requests, not four hundred: each returns a result the database already reduced.
      const [fleet, stages, freshness, health] = await Promise.all([
        getAdminFleetSummary(hours),
        getAdminStageSummary(hours),
        getAdminCatalogFreshness(),
        getAdminSourceHealth(),
      ]);
      setData({ fleet, stages, freshness, health });
      setElapsedMs(performance.now() - started);
    } catch {
      // A failure to load is not an empty window, and must never render as zeros.
      setFailed(true);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(windowHours);
  }, [load, windowHours]);

  const flow = data ? buildStageFlow(data.stages, data.fleet) : null;
  const emptyReason = stageFlowEmptyReason(data?.fleet ?? null, failed);

  const byHealth = new Map<string, { count: number; events: number }>();
  for (const source of data?.health.sources ?? []) {
    const bucket = byHealth.get(source.health) ?? { count: 0, events: 0 };
    bucket.count += 1;
    bucket.events += source.upcoming_events;
    byHealth.set(source.health, bucket);
  }

  const attention: AdminSourceHealth[] = [...(data?.health.sources ?? [])]
    .filter((source) => source.health !== "healthy")
    .sort((left, right) => right.upcoming_events - left.upcoming_events)
    .slice(0, 12);

  const invisible = (data?.health.sources ?? []).filter(
    (source) => !source.enabled && source.upcoming_events > 0,
  );
  const invisibleEvents = invisible.reduce(
    (total, source) => total + source.upcoming_events,
    0,
  );

  return (
    <div className="rv">
      <header className="rv-head">
        <div>
          <p className="rv-eyebrow">events concierge · redesign review</p>
          <h1>Server-computed ingestion console</h1>
          <p className="rv-lede">
            Every panel below is one aggregate query. Nothing is reduced in the browser.
          </p>
        </div>
        <div className="rv-controls">
          {WINDOWS.map((option) => (
            <button
              key={option.hours}
              type="button"
              className={option.hours === windowHours ? "rv-win rv-win--on" : "rv-win"}
              onClick={() => setWindowHours(option.hours)}
            >
              {option.label}
            </button>
          ))}
          <span className="rv-timing">
            {loading ? "loading…" : elapsedMs !== null ? `${Math.round(elapsedMs)} ms` : ""}
          </span>
        </div>
      </header>

      {failed ? (
        <section className="rv-panel rv-panel--bad">
          <p className="rv-panel-title">Admin aggregates are unavailable</p>
          <p className="rv-note">
            This is a load failure, not an empty window. The old console rendered this state as
            five zeros and the sentence &ldquo;No recorded runs match this pipeline slice&rdquo;.
          </p>
          <button type="button" className="rv-btn" onClick={() => void load(windowHours)}>
            Retry
          </button>
        </section>
      ) : null}

      {data && flow ? (
        <>
          <section className="rv-panel">
            <div className="rv-panel-head">
              <span className="rv-panel-title">Source health</span>
              <span className="rv-scope">
                {data.health.total} sources · graded now · max-severity of four components
              </span>
            </div>
            <div className="rv-stats">
              {HEALTH_ORDER.map((token) => {
                const bucket = byHealth.get(token);
                if (!bucket) return null;
                return (
                  <div key={token} className="rv-stat">
                    <span className="rv-stat-l">
                      {HEALTH_GLYPH[token]} {token.replaceAll("_", " ")}
                    </span>
                    <span className={`rv-stat-v rv-${HEALTH_TONE[token]}`}>
                      {bucket.count}
                    </span>
                    <span className="rv-stat-n">
                      {integer(bucket.events)} upcoming events served
                    </span>
                  </div>
                );
              })}
            </div>
            {invisible.length ? (
              <p className="rv-callout">
                <strong>{invisible.length} disabled sources serve {integer(invisibleEvents)}{" "}
                upcoming events.</strong>{" "}
                Today&rsquo;s attention tile cannot count any of them:{" "}
                <code>sourceAttentionScore</code> opens with{" "}
                <code>if (!source.enabled) return 0</code>.
              </p>
            ) : null}
          </section>

          <section className="rv-panel">
            <div className="rv-panel-head">
              <span className="rv-panel-title">Catalog freshness</span>
              <span className="rv-scope">
                {integer(data.freshness.total_events)} upcoming events · by age of last
                successful fetch
              </span>
            </div>
            <div className="rv-bar">
              {data.freshness.buckets
                .filter((bucket) => bucket.events > 0)
                .map((bucket) => (
                  <span
                    key={bucket.bucket}
                    className={`rv-seg rv-seg--${FRESHNESS_TONE[bucket.bucket]}`}
                    style={{ width: `${bucket.pct ?? 0}%` }}
                  >
                    {(bucket.pct ?? 0) >= 8 ? `${bucket.pct}%` : ""}
                  </span>
                ))}
            </div>
            <div className="rv-legend">
              {data.freshness.buckets
                .filter((bucket) => bucket.events > 0 || bucket.sources > 0)
                .map((bucket) => (
                  <span key={bucket.bucket}>
                    <i className={`rv-key rv-key--${FRESHNESS_TONE[bucket.bucket]}`} />
                    {bucket.bucket} — {bucket.sources} sources, {integer(bucket.events)} events
                  </span>
                ))}
            </div>
          </section>

          <section className="rv-panel">
            <div className="rv-panel-head">
              <span className="rv-panel-title">Pipeline stages</span>
              <span className="rv-scope">
                {flow.windowHours}h window · {integer(data.fleet.runs)} runs
              </span>
            </div>
            {emptyReason === "no_runs" ? (
              <p className="rv-note">
                No runs started in this window. The window is empty; the query succeeded.
              </p>
            ) : (
              <div className="rv-chain">
                {flow.steps.map((step) => {
                  const folded = step.evidenceStatus === "not_separately_instrumented";
                  return (
                    <div
                      key={step.stage}
                      className={folded ? "rv-step rv-step--folded" : "rv-step"}
                    >
                      <span className="rv-step-k">
                        {String(step.position).padStart(2, "0")} {step.label}
                      </span>
                      <span className="rv-step-v">
                        {folded ? "not separately instrumented" : duration(step.totalMs)}
                      </span>
                      <span className="rv-step-s">
                        {folded
                          ? step.foldedInto
                          : `${step.pctOfWall ?? 0}% of wall · ${integer(step.runsWithEvidence)} runs`}
                      </span>
                      {step.recordCount !== null ? (
                        <span className="rv-step-r">
                          {integer(step.recordCount)} {step.recordLabel}
                        </span>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}
            <p className="rv-note">
              Records exist at two waypoints only:{" "}
              <strong>{integer(flow.candidates)} candidates</strong> →{" "}
              <strong>{integer(flow.canonicals)} canonicals</strong>, a delta of{" "}
              {integer(flow.mergedByDedupe)} merged by dedupe — not lost. The two hatched stages
              have never written a metrics row and are shown as gaps rather than zeros.
            </p>
          </section>

          <section className="rv-panel">
            <div className="rv-panel-head">
              <span className="rv-panel-title">Fleet, {flow.windowHours}h</span>
              <span className="rv-scope">one query</span>
            </div>
            <div className="rv-stats">
              <div className="rv-stat">
                <span className="rv-stat-l">runs</span>
                <span className="rv-stat-v">{integer(data.fleet.runs)}</span>
                <span className="rv-stat-n">
                  {integer(data.fleet.succeeded)} ok · {integer(data.fleet.failed)} failed ·{" "}
                  {integer(data.fleet.paused)} deferred
                </span>
              </div>
              <div className="rv-stat">
                <span className="rv-stat-l">attempts</span>
                <span className="rv-stat-v rv-bad">{integer(data.fleet.attempts)}</span>
                <span className="rv-stat-n">
                  worst slot claimed {integer(data.fleet.max_attempts)}×
                </span>
              </div>
              <div className="rv-stat">
                <span className="rv-stat-l">retried slots</span>
                <span className="rv-stat-v rv-mid">{integer(data.fleet.retrying_runs)}</span>
                <span className="rv-stat-n">claimed more than once</span>
              </div>
              <div className="rv-stat">
                <span className="rv-stat-l">duration p50 / p99</span>
                <span className="rv-stat-v">
                  {duration(data.fleet.duration_p50_ms)}
                </span>
                <span className="rv-stat-n">p99 {duration(data.fleet.duration_p99_ms)}</span>
              </div>
              <div className="rv-stat">
                <span className="rv-stat-l">zero-yield runs</span>
                <span className="rv-stat-v rv-mid">{integer(data.fleet.zero_yield_runs)}</span>
                <span className="rv-stat-n">succeeded, published nothing</span>
              </div>
            </div>
          </section>

          <section className="rv-panel">
            <div className="rv-panel-head">
              <span className="rv-panel-title">Attention, ranked by blast radius</span>
              <span className="rv-scope">upcoming events served by each unhealthy source</span>
            </div>
            <div className="rv-scroll">
              <table className="rv-table">
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Health</th>
                    <th>Run</th>
                    <th>Freshness</th>
                    <th>Retry</th>
                    <th className="rv-num">Attempts</th>
                    <th className="rv-num">Last attempt</th>
                    <th className="rv-num">Last success</th>
                    <th className="rv-num">Serving</th>
                  </tr>
                </thead>
                <tbody>
                  {attention.map((source) => (
                    <tr key={source.source_key}>
                      <td>
                        <span className="rv-name">{source.display_name}</span>
                        <span className="rv-key-text">{source.source_key}</span>
                      </td>
                      <td>
                        <span className={`rv-chip rv-chip--${HEALTH_TONE[source.health]}`}>
                          {HEALTH_GLYPH[source.health]} {source.health.replaceAll("_", " ")}
                        </span>
                      </td>
                      <td className="rv-mono">{source.run_state}</td>
                      <td className="rv-mono">{source.freshness_state}</td>
                      <td className="rv-mono">{source.retry_state}</td>
                      <td className="rv-num rv-mono">
                        {source.latest_attempt_count === null
                          ? "—"
                          : integer(source.latest_attempt_count)}
                      </td>
                      <td className="rv-num rv-mono">{age(source.last_attempt_at)}</td>
                      <td className="rv-num rv-mono">{age(source.last_success_at)}</td>
                      <td className="rv-num rv-mono">{integer(source.upcoming_events)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="rv-note">
              Three timestamps, not one. A source can have attempted minutes ago and last
              succeeded weeks ago — the shape the current console cannot express.
            </p>
          </section>
        </>
      ) : null}
    </div>
  );
}
