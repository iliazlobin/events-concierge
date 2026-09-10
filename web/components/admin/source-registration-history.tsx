"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getAdminSourceRegistrationHistory } from "@/lib/admin-api";
import { registrationTimestamp, sourceHistoryDays, sourceRegistrationPlot, type SourceHistoryDays } from "@/lib/admin-source-registration-history";
import { useAdminSnapshot } from "./use-admin-snapshot";
import { int } from "./console-kit";
import type { SourceRegistryLens } from "@/lib/admin-source-workspace";
import { SourceCurrentState } from "./source-current-state";
import styles from "./source-registration-history.module.css";

export function SourceRegistrationHistory({ includeFixtures, refreshVersion, onInspectState }: { includeFixtures: boolean; refreshVersion: number; onInspectState: (lens: SourceRegistryLens) => void }) {
  const [days, setDays] = useState<SourceHistoryDays>(90);
  const [activeAt, setActiveAt] = useState<string | null>(null);
  const refs = useRef<Array<SVGGElement | null>>([]);
  useEffect(() => {
    const read = () => { setDays(sourceHistoryDays(new URL(window.location.href).searchParams.get("source_trend"))); setActiveAt(null); };
    read(); window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const chooseDays = (next: SourceHistoryDays) => {
    const url = new URL(window.location.href);
    if (next === 90) url.searchParams.delete("source_trend"); else url.searchParams.set("source_trend", String(next));
    const href = `${url.pathname}${url.search}${url.hash}`;
    if (href !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(window.history.state, "", href);
    setDays(next); setActiveAt(null);
  };
  const load = useCallback(async (signal: AbortSignal) => {
    const data = await getAdminSourceRegistrationHistory(days, includeFixtures, signal);
    if (data.window_days !== days || data.include_fixtures !== includeFixtures || data.history_scope !== "retained_registry" || data.bucket_hours !== 24) throw new Error("Registration history scope mismatch");
    return data;
  }, [days, includeFixtures]);
  const snapshot = useAdminSnapshot(load, refreshVersion);
  const data = snapshot.failed || snapshot.loading ? null : snapshot.data;
  const plot = data ? sourceRegistrationPlot(data) : null;
  const selected = plot ? Math.max(0, activeAt ? plot.points.findIndex(point => point.at === activeAt) : plot.points.length - 1) : 0;
  const active = plot?.points[selected];
  return <section className={styles.root} aria-label="Source registration history" aria-busy={snapshot.loading}>
    <div className={styles.heading}>
      <div><h2>Sources over time</h2><p>All sources · {includeFixtures ? "fixtures included" : "fixtures excluded"} · paused and retired included</p></div>
      <div role="group" aria-label="Source history window">{([7, 30, 90] as const).map(value => <button key={value} type="button" aria-pressed={days === value} onClick={() => chooseDays(value)}>{value}d</button>)}</div>
    </div>
    <div className={styles.body}>
      <dl className={styles.metrics}>
        <div><dt>Registered sources</dt><dd>{data ? int(data.total_sources) : "—"}</dd></div>
        <div><dt>Added in {days}d</dt><dd>{data ? `+${int(data.added_sources)}` : "—"}</dd></div>
      </dl>
      <div className={styles.trend}>
        {data && plot ? <>
          <svg viewBox="0 0 1000 200" preserveAspectRatio="none" className={styles.chart} role="group" aria-label="Registered sources timeline">
            {[10, 100, 190].map(y => <line key={y} x1="0" x2="1000" y1={y} y2={y} className={styles.grid} vectorEffect="non-scaling-stroke" />)}
            <path d={plot.path} className={styles.line} vectorEffect="non-scaling-stroke" />
            {plot.points.map(point => {
              const label = `${registrationTimestamp(point.at)}: ${int(point.count)} registered sources; ${point.index === 0 ? "start of period" : `${int(point.added)} added in previous 24h`}`;
              const left = point.index ? (plot.points[point.index - 1].x + point.x) / 2 : 0;
              const right = point.index === plot.points.length - 1 ? 1000 : (point.x + plot.points[point.index + 1].x) / 2;
              return <g key={point.at} role="img" aria-label={label} tabIndex={selected === point.index ? 0 : -1} ref={element => { refs.current[point.index] = element; }} data-active={selected === point.index}
                onMouseEnter={() => setActiveAt(point.at)} onFocus={() => setActiveAt(point.at)} onKeyDown={event => {
                  const next = event.key === "ArrowRight" ? Math.min(plot.points.length - 1, point.index + 1) : event.key === "ArrowLeft" ? Math.max(0, point.index - 1) : event.key === "Home" ? 0 : event.key === "End" ? plot.points.length - 1 : null;
                  if (next !== null) { event.preventDefault(); refs.current[next]?.focus(); }
                }}>
                <title>{label}</title><rect x={left} y="0" width={right - left} height="200" className={styles.target} />
                <circle cx={point.x} cy={point.y} r="4" className={styles.dot} />
              </g>;
            })}
          </svg>
          <div className={styles.axis}><span>{registrationTimestamp(data.window_start).slice(0, 10)}</span><span>0–{int(plot.maximum)} sources · UTC</span><span>{registrationTimestamp(data.generated_at).slice(0, 10)}</span></div>
          <div className={styles.readout} role="status"><span>{active ? registrationTimestamp(active.at) : ""}</span><strong>{active ? `${int(active.count)} registered · ${active.index === 0 ? "start of period" : `+${int(active.added)} in previous 24h`}` : ""}</strong></div>
        </> : <div className={styles.empty}>{snapshot.failed ? <><span>Registration history unavailable.</span><button type="button" onClick={() => void snapshot.refresh()}>Retry history</button></> : "Loading registration history…"}</div>}
      </div>
    </div>
    <p className={styles.note}>Database registration dates for sources retained in the registry. Table filters apply below.</p>
    <SourceCurrentState includeFixtures={includeFixtures} refreshVersion={refreshVersion} onInspectState={onInspectState} />
  </section>;
}
