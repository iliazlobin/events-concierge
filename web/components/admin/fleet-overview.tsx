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
 * The Overview surface.
 *
 * Design rule, applied without exception: a number the operator cannot act on is noise. There are
 * four figures on this screen and each one is a control that opens the rows behind it. Everything
 * that used to be a static readout either became a drill-down or was removed.
 */

type DrillKey = "attention" | "freshness" | "throughput" | "retries";

type Tone = "ok" | "mid" | "bad" | "unk" | "neutral";

const TONE_CLASS: Record<Tone, string> = {
  ok: styles.toneOk,
  mid: styles.toneMid,
  bad: styles.toneBad,
  unk: styles.toneUnk,
  neutral: styles.toneNeutral,
};

const CHIP_CLASS: Record<string, string> = {
  down: styles.chipBad,
  never_succeeded: styles.chipBad,
  late: styles.chipMid,
  warn: styles.chipMid,
  paused: styles.chipUnk,
  retired: styles.chipUnk,
  healthy: styles.chipOk,
};

const CHIP_GLYPH: Record<string, string> = {
  down: "✕",
  never_succeeded: "✕",
  late: "▲",
  warn: "▲",
  paused: "‒",
  retired: "‒",
  healthy: "✓",
};

/** Health tokens that mean "someone has to do something". */
const ACTIONABLE = new Set(["down", "never_succeeded", "late", "warn"]);

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
  return `${(hours / 24).toFixed(1)}d`;
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
  const [drill, setDrill] = useState<DrillKey | null>(null);

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
      // A failed load is not an empty result and must never render as zeros.
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

  const attention = useMemo(
    () =>
      [...sources]
        .filter((source) => ACTIONABLE.has(source.health))
        .sort((left, right) => right.upcoming_events - left.upcoming_events),
    [sources],
  );

  const stale = useMemo(
    () =>
      [...sources]
        .filter((source) => source.upcoming_events > 0 && source.freshness_state !== "ok")
        .sort((left, right) => right.upcoming_events - left.upcoming_events),
    [sources],
  );

  const retriers = useMemo(
    () =>
      [...sources]
        .filter((source) => (source.latest_attempt_count ?? 0) > 1)
        .sort(
          (left, right) =>
            (right.latest_attempt_count ?? 0) - (left.latest_attempt_count ?? 0),
        ),
    [sources],
  );

  if (loading && !snapshot) {
    return <div className={styles.state}>Loading fleet…</div>;
  }

  if (failed || !snapshot) {
    return (
      <div className={styles.shell}>
        <div className={`${styles.state} ${styles.stateBad}`}>
          Fleet data is unavailable. This is a load failure, not an empty fleet.
          <div>
            <button type="button" className={styles.action} onClick={() => void load()}>
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  const { fleet, freshness } = snapshot;
  const staleEvents = freshness.buckets
    .filter((bucket) => bucket.bucket !== "fresh")
    .reduce((total, bucket) => total + bucket.events, 0);
  const stalePct = freshness.total_events
    ? (staleEvents / freshness.total_events) * 100
    : 0;

  const toggle = (key: DrillKey) => setDrill((current) => (current === key ? null : key));

  return (
    <div className={styles.shell}>
      <div className={styles.status}>
        <span className={styles.statusItem}>
          fleet <strong>{integer(snapshot.health.total)}</strong> sources
        </span>
        <span className={styles.statusItem}>
          window <strong>7 days</strong>
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

      <div className={styles.signals}>
        <SignalCard
          question="Needs attention"
          value={integer(attention.length)}
          tone={attention.length ? "bad" : "ok"}
          answer={
            attention.length
              ? `serving ${integer(
                  attention.reduce((total, source) => total + source.upcoming_events, 0),
                )} events people can see`
              : "every source is inside its cadence"
          }
          open={drill === "attention"}
          onClick={() => toggle("attention")}
        />
        <SignalCard
          question="Catalog going stale"
          value={`${stalePct.toFixed(1)}%`}
          tone={stalePct > 5 ? "bad" : stalePct > 1 ? "mid" : "ok"}
          answer={`${integer(staleEvents)} of ${integer(
            freshness.total_events,
          )} upcoming events`}
          open={drill === "freshness"}
          onClick={() => toggle("freshness")}
        />
        <SignalCard
          question="Runs, last 7 days"
          value={integer(fleet.runs)}
          tone={fleet.failed ? "mid" : "ok"}
          answer={`${integer(fleet.succeeded)} succeeded · ${integer(
            fleet.failed,
          )} failed · ${integer(fleet.paused)} deferred`}
          open={drill === "throughput"}
          onClick={() => toggle("throughput")}
        />
        <SignalCard
          question="Wasted retries"
          value={integer(fleet.attempts - fleet.runs)}
          tone={fleet.max_attempts > 20 ? "bad" : fleet.retrying_runs ? "mid" : "ok"}
          answer={`worst slot claimed ${integer(fleet.max_attempts)} times`}
          open={drill === "retries"}
          onClick={() => toggle("retries")}
        />
      </div>

      {drill === "attention" ? (
        <SourceDrill
          title="Sources needing attention"
          scope="ranked by events people can currently see"
          rows={attention}
          onOpenSource={onOpenSource}
          onClose={() => setDrill(null)}
        />
      ) : null}

      {drill === "freshness" ? (
        <SourceDrill
          title="Sources serving stale events"
          scope="graded by age of last successful fetch"
          rows={stale}
          onOpenSource={onOpenSource}
          onClose={() => setDrill(null)}
        />
      ) : null}

      {drill === "retries" ? (
        <SourceDrill
          title="Sources burning retries"
          scope="attempts on the current slot"
          rows={retriers}
          onOpenSource={onOpenSource}
          onClose={() => setDrill(null)}
        />
      ) : null}

      {drill === "throughput" ? (
        <div className={styles.drill}>
          <div className={styles.drillHead}>
            <span className={styles.drillTitle}>Run outcomes</span>
            <span className={styles.drillScope}>last 7 days</span>
            <button type="button" className={styles.close} onClick={() => setDrill(null)}>
              close ✕
            </button>
          </div>
          <div className={styles.stub}>
            <span className={styles.stubTag}>not built yet</span>
            A run-outcome breakdown over time belongs here — failures by class, and which sources
            they came from.{" "}
            <button type="button" className={styles.rowButton} onClick={onOpenRuns}>
              Open the Runs tab
            </button>{" "}
            for the raw ledger in the meantime.
          </div>
        </div>
      ) : null}
    </div>
  );
}

function SignalCard({
  question,
  value,
  answer,
  tone,
  open,
  onClick,
}: {
  question: string;
  value: string;
  answer: string;
  tone: Tone;
  open: boolean;
  onClick: () => void;
}): React.JSX.Element {
  return (
    <button
      type="button"
      className={open ? `${styles.card} ${styles.cardOpen}` : styles.card}
      onClick={onClick}
      aria-expanded={open}
    >
      <span className={styles.cardQuestion}>{question}</span>
      <span className={`${styles.cardValue} ${TONE_CLASS[tone]}`}>{value}</span>
      <span className={styles.cardAnswer}>{answer}</span>
      <span className={styles.cardMore}>{open ? "hide ▲" : "show ▾"}</span>
    </button>
  );
}

function SourceDrill({
  title,
  scope,
  rows,
  onOpenSource,
  onClose,
}: {
  title: string;
  scope: string;
  rows: AdminSourceHealth[];
  onOpenSource: (sourceKey: string) => void;
  onClose: () => void;
}): React.JSX.Element {
  return (
    <div className={styles.drill}>
      <div className={styles.drillHead}>
        <span className={styles.drillTitle}>{title}</span>
        <span className={styles.drillScope}>
          {rows.length} {rows.length === 1 ? "source" : "sources"} · {scope}
        </span>
        <button type="button" className={styles.close} onClick={onClose}>
          close ✕
        </button>
      </div>
      {rows.length === 0 ? (
        <div className={styles.state}>Nothing here right now.</div>
      ) : (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Source</th>
                <th>State</th>
                <th className={styles.num}>Attempts</th>
                <th className={styles.num}>Last try</th>
                <th className={styles.num}>Last success</th>
                <th className={styles.num}>Serving</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((source) => (
                <tr key={source.source_key}>
                  <td>
                    <button
                      type="button"
                      className={styles.rowButton}
                      onClick={() => onOpenSource(source.source_key)}
                    >
                      {source.display_name}
                    </button>
                    <span className={styles.rowKey}>{source.source_key}</span>
                  </td>
                  <td>
                    <span className={`${styles.chip} ${CHIP_CLASS[source.health]}`}>
                      {CHIP_GLYPH[source.health]} {source.health.replaceAll("_", " ")}
                    </span>
                  </td>
                  <td className={`${styles.num} ${styles.mono}`}>
                    {source.latest_attempt_count === null
                      ? "—"
                      : integer(source.latest_attempt_count)}
                  </td>
                  <td className={`${styles.num} ${styles.mono}`}>
                    {age(source.last_attempt_at)}
                  </td>
                  <td className={`${styles.num} ${styles.mono}`}>
                    {age(source.last_success_at)}
                  </td>
                  <td className={`${styles.num} ${styles.mono}`}>
                    {integer(source.upcoming_events)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
