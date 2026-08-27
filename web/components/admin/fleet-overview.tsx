"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getAdminCatalogFreshness,
  getAdminFleetSummary,
  getAdminSourceHealth,
} from "@/lib/admin-api";
import type {
  AdminCatalogFreshness,
  AdminFleetSummary,
  AdminSourceHealth,
  AdminSourceHealthList,
} from "@/lib/admin-types";

import styles from "./fleet-overview.module.css";

/**
 * The Overview surface: persistent sections, tables, no charts.
 *
 * Charts were considered and rejected for this data shape. Typically 4-16 sources are unhealthy
 * at once; below roughly fifteen marks a chart spends an axis, a scale and a legend to encode
 * what text carries with more precision, and it loses the row affordance the operator actually
 * needs, which is "open this source".
 *
 * Attention is derived from the four health COMPONENTS, never from the rolled-up `health` token.
 * That distinction is the whole point: `health` collapses run/freshness/retry/yield into one word
 * and reports lifecycle states (`paused`, `retired`) in the same field, so filtering on it hides
 * exactly the sources that matter. Measured while writing this, filtering on the token surfaced
 * four sources with no failed run, no freshness problem and no severe retry -- executions of
 * 2 to 4, which is trivia -- while hiding twelve sources carrying every real defect in the fleet
 * and serving 1,538 upcoming events, because their token happened to read `paused` or `retired`.
 *
 * A disabled source still serves whatever it last published: the write path never deletes. So
 * lifecycle is a label on the row, never a reason to drop it.
 */

type Tone = "ok" | "mid" | "bad" | "unk";

const TONE_CLASS: Record<Tone, string> = {
  ok: styles.toneOk,
  mid: styles.toneMid,
  bad: styles.toneBad,
  unk: styles.toneUnk,
};

const CHIP_CLASS: Record<Tone, string> = {
  ok: styles.chipOk,
  mid: styles.chipMid,
  bad: styles.chipBad,
  unk: styles.chipUnk,
};

/** One component's severity, and the words the operator sees. */
interface Component {
  key: "run" | "fresh" | "retry" | "yield";
  label: string;
  value: string;
  tone: Tone;
  /** True when this component is why the row is listed. */
  degraded: boolean;
}

function runComponent(source: AdminSourceHealth): Component {
  const state = source.run_state;
  const bad = state === "failed";
  return {
    key: "run",
    label: "run",
    value: state === "never_run" ? "never ran" : state,
    tone: bad ? "bad" : state === "never_run" ? "unk" : "ok",
    degraded: bad || state === "never_run",
  };
}

function freshnessComponent(source: AdminSourceHealth): Component {
  const state = source.freshness_state;
  const tone: Tone =
    state === "down" || state === "never" ? "bad" : state === "late" ? "mid" : state === "warn" ? "mid" : "ok";
  return {
    key: "fresh",
    label: "fresh",
    value: state === "ok" ? "on time" : state,
    tone,
    degraded: state !== "ok",
  };
}

function retryComponent(source: AdminSourceHealth): Component {
  const state = source.retry_state;
  const count = source.latest_attempt_count ?? 0;
  return {
    key: "retry",
    label: "retry",
    // The ratio is what matters: a slot should be claimed once.
    value: state === "ok" ? "1×" : `${count}×`,
    tone: state === "severe" ? "bad" : state === "elevated" ? "mid" : "ok",
    degraded: state !== "ok",
  };
}

function yieldComponent(source: AdminSourceHealth): Component {
  const state = source.yield_state;
  return {
    key: "yield",
    label: "yield",
    value: state === "zero_yield" ? "published 0" : state === "unknown" ? "unknown" : "ok",
    tone: state === "zero_yield" ? "bad" : state === "unknown" ? "unk" : "ok",
    degraded: state === "zero_yield",
  };
}

function componentsOf(source: AdminSourceHealth): Component[] {
  return [
    runComponent(source),
    freshnessComponent(source),
    retryComponent(source),
    yieldComponent(source),
  ];
}

/** Lifecycle is a label, not an exclusion. */
function lifecycleOf(source: AdminSourceHealth): { word: string; tone: Tone } | null {
  if (source.retired_at) return { word: "retired", tone: "unk" };
  if (!source.enabled) return { word: "paused", tone: "unk" };
  return null;
}

function integer(value: number): string {
  return value.toLocaleString("en-US");
}

function age(iso: string | null): string {
  if (!iso) return "never";
  const delta = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(delta)) return "—";
  const minutes = delta / 60_000;
  if (minutes < 60) return `${Math.max(0, Math.round(minutes))}m`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1)}h`;
  return `${Math.round(hours / 24)}d`;
}

/**
 * How many of its own cadence intervals a source is behind. This is the figure that separates
 * "ran 4 hours ago on a 6-hour cadence" from "dark for 40 days" -- both read as a stale
 * timestamp, but only one is an outage.
 */
function lateness(source: AdminSourceHealth): string {
  const hours = source.hours_since_success;
  if (hours === null) return "never";
  const intervalHours = source.refresh_interval_minutes / 60;
  if (!intervalHours) return "—";
  const ratio = hours / intervalHours;
  if (ratio < 1) return "on time";
  return `${ratio < 10 ? ratio.toFixed(1) : Math.round(ratio)}\u00d7`;
}

function duration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} s`;
  return `${ms} ms`;
}

interface Snapshot {
  fleet: AdminFleetSummary;
  freshness: AdminCatalogFreshness;
  health: AdminSourceHealthList;
}

export interface FleetOverviewProps {
  onOpenSource: (sourceKey: string) => void;
  onOpenRuns: () => void;
  onRefreshDue: () => void;
  refreshSubmitting: boolean;
  refreshDuePending: boolean;
}

export function FleetOverview({
  onOpenSource,
  onOpenRuns,
  onRefreshDue,
  refreshSubmitting,
  refreshDuePending,
}: FleetOverviewProps): React.JSX.Element {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const [fleet, freshness, health] = await Promise.all([
        getAdminFleetSummary(168),
        getAdminCatalogFreshness(),
        getAdminSourceHealth(),
      ]);
      setSnapshot({ fleet, freshness, health });
    } catch {
      setFailed(true);
      setSnapshot(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const sources = useMemo(() => snapshot?.health.sources ?? [], [snapshot]);

  /**
   * Actionable = any degraded component. Ranked by events currently being served, because that
   * is the blast radius: a broken source nobody reads from can wait behind one that is feeding
   * stale results to people right now.
   */
  const attention = useMemo(() => {
    return sources
      .map((source) => ({ source, components: componentsOf(source) }))
      .filter((row) => row.components.some((component) => component.degraded))
      .sort((left, right) => {
        const events = right.source.upcoming_events - left.source.upcoming_events;
        if (events !== 0) return events;
        return (right.source.hours_since_success ?? 0) - (left.source.hours_since_success ?? 0);
      });
  }, [sources]);

  const eventsAtRisk = attention.reduce(
    (total, row) => total + row.source.upcoming_events,
    0,
  );

  if (loading && !snapshot) {
    return <div className={styles.state}>Loading fleet…</div>;
  }

  if (failed || !snapshot) {
    return (
      <div className={styles.state + " " + styles.stateBad}>
        Fleet data is unavailable — this is a load failure, not an empty fleet.
        <div>
          <button type="button" className={styles.action} onClick={() => void load()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  const { fleet, freshness } = snapshot;
  const staleEvents = freshness.buckets
    .filter((bucket) => bucket.bucket !== "fresh")
    .reduce((total, bucket) => total + bucket.events, 0);

  return (
    <div className={styles.shell}>
      <div className={styles.status}>
        <span className={styles.statusItem}>
          fleet <strong>{integer(snapshot.health.total)}</strong> sources
        </span>
        <span className={styles.statusItem}>
          <strong className={attention.length ? styles.toneBad : styles.toneOk}>
            {attention.length}
          </strong>{" "}
          need attention
        </span>
        <span className={styles.statusItem}>
          <strong>{integer(eventsAtRisk)}</strong> events at risk
        </span>
        <span className={styles.spacer} />
        <button
          type="button"
          className={styles.action}
          onClick={onRefreshDue}
          disabled={refreshSubmitting || refreshDuePending}
        >
          {refreshDuePending ? "Refresh in flight" : "Refresh due sources"}
        </button>
      </div>

      {/* ---------- 1. Attention ---------- */}
      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>Needs attention</h2>
          <span className={styles.sectionScope}>
            {attention.length} of {snapshot.health.total} sources · ranked by events people can
            currently see
          </span>
        </div>

        {attention.length === 0 ? (
          <p className={styles.calm}>
            Every source is inside its cadence, publishing, and claiming its slot once.
          </p>
        ) : (
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Why</th>
                  <th className={styles.num}>Behind</th>
                  <th className={styles.num}>Last try</th>
                  <th className={styles.num}>Last success</th>
                  <th className={styles.num}>Serving</th>
                </tr>
              </thead>
              <tbody>
                {attention.map(({ source, components }) => {
                  const lifecycle = lifecycleOf(source);
                  return (
                    <tr key={source.source_key}>
                      <td>
                        <button
                          type="button"
                          className={styles.rowButton}
                          onClick={() => onOpenSource(source.source_key)}
                        >
                          {source.display_name}
                        </button>
                        <span className={styles.rowKey}>
                          {source.source_key}
                          {lifecycle ? (
                            <span className={`${styles.lifecycle} ${TONE_CLASS[lifecycle.tone]}`}>
                              {lifecycle.word}
                            </span>
                          ) : null}
                        </span>
                      </td>
                      <td>
                        {/* The quartet, not a single rolled-up word: a row failing only on retry
                            and a row failing on retry AND yield route to different fixes. */}
                        <span className={styles.quartet}>
                          {components.map((component) => (
                            <span
                              key={component.key}
                              className={
                                component.degraded
                                  ? `${styles.pill} ${CHIP_CLASS[component.tone]}`
                                  : `${styles.pill} ${styles.pillMuted}`
                              }
                              title={`${component.label}: ${component.value}`}
                            >
                              <span className={styles.pillLabel}>{component.label}</span>
                              <span className={styles.pillValue}>{component.value}</span>
                            </span>
                          ))}
                        </span>
                      </td>
                      <td className={`${styles.num} ${styles.mono}`}>
                        <span
                          className={
                            (source.hours_since_success ?? 0) * 60
                            > 3 * source.refresh_interval_minutes
                              ? styles.toneBad
                              : undefined
                          }
                        >
                          {lateness(source)}
                        </span>
                      </td>
                      <td className={`${styles.num} ${styles.mono}`}>
                        {age(source.last_attempt_at)}
                      </td>
                      <td className={`${styles.num} ${styles.mono}`}>
                        <span
                          className={
                            (source.hours_since_success ?? 0) > 168 ? styles.toneBad : undefined
                          }
                        >
                          {age(source.last_success_at)}
                        </span>
                      </td>
                      <td className={`${styles.num} ${styles.mono}`}>
                        {integer(source.upcoming_events)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ---------- 2. Catalog coverage ---------- */}
      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>Catalog coverage</h2>
          <span className={styles.sectionScope}>what people can currently see</span>
        </div>
        <p className={styles.lead}>
          <strong>{integer(freshness.total_events)}</strong> upcoming events are being served.{" "}
          <strong className={staleEvents ? styles.toneBad : styles.toneOk}>
            {integer(staleEvents)}
          </strong>{" "}
          of them come from a source that has not fetched successfully in over a day.
        </p>
        <div className={styles.scroll}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Freshness of origin</th>
                <th className={styles.num}>Sources</th>
                <th className={styles.num}>Events served</th>
                <th className={styles.num}>Share</th>
              </tr>
            </thead>
            <tbody>
              {freshness.buckets
                .filter((bucket) => bucket.sources > 0 || bucket.events > 0)
                .map((bucket) => (
                  <tr key={bucket.bucket}>
                    <td className={styles.mono}>{bucket.bucket}</td>
                    <td className={`${styles.num} ${styles.mono}`}>{bucket.sources}</td>
                    <td className={`${styles.num} ${styles.mono}`}>{integer(bucket.events)}</td>
                    <td className={`${styles.num} ${styles.mono}`}>
                      {bucket.pct === null ? "—" : `${bucket.pct}%`}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* ---------- 3. Fleet activity ---------- */}
      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>Fleet activity</h2>
          <span className={styles.sectionScope}>last 7 days</span>
        </div>
        <p className={styles.lead}>
          <strong>{integer(fleet.runs)}</strong> refresh slots ran —{" "}
          <span className={styles.toneOk}>{integer(fleet.succeeded)} succeeded</span>,{" "}
          <span className={fleet.failed ? styles.toneBad : undefined}>
            {integer(fleet.failed)} still failing
          </span>
          , {integer(fleet.paused)} waiting on pacing. Half finish within{" "}
          {duration(fleet.duration_p50_ms)}; the slowest 5% take over{" "}
          {duration(fleet.duration_p95_ms)}.{" "}
          <button type="button" className={styles.rowButton} onClick={onOpenRuns}>
            Open the run ledger
          </button>
        </p>
        <p className={styles.footnote}>
          Counted by each slot&rsquo;s final outcome, so a slot that failed and later succeeded
          reads as succeeded. Recent days therefore look worse than settled ones — no trend is
          drawn from this for that reason.
        </p>
      </section>
    </div>
  );
}
