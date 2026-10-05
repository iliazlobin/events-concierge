"use client";

import { useRef, useState } from "react";
import { usageChart, usd, type UsageMetric, type UsageReport } from "@/lib/admin-model-usage";
import { Segment, ms } from "./console-kit";
import styles from "./models-view.module.css";

const labels: Record<UsageMetric, string> = { cost: "Spend", tokens: "Tokens", calls: "Calls", latency: "Latency" };
const count = (value: number) => value.toLocaleString("en-US");
const utc = (value: string) => new Date(value).toLocaleString("en-US", { timeZone: "UTC", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const metricText = (value: number | null, metric: UsageMetric) => value === null ? "No measurement" : metric === "cost" ? usd(value) : metric === "latency" ? ms(value) : count(value);

export function UsageChart({ report, metric, onMetric }: { report: UsageReport; metric: UsageMetric; onMetric: (metric: UsageMetric) => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  const chart = usageChart(report, metric);
  const selectedIndex = report.series.findIndex((bucket) => bucket.at === selected);
  const row = report.series[selectedIndex];
  const points = chart.lines[0].points;
  const entry = selectedIndex >= 0 ? selectedIndex : Math.max(0, points.findLastIndex((point) => point.value !== null));
  const rangeStart = Date.parse(report.start_at);
  const rangeDuration = Date.parse(report.end_at) - rangeStart;
  return <section className={`${styles.section} ${styles.chart}`} aria-label="Model usage trend">
    <div className={styles.sectionHead}><h2>Over time</h2><Segment label="Chart metric" value={metric} onChange={onMetric} options={Object.entries(labels).map(([value, label]) => ({ value: value as UsageMetric, label }))} /></div>
    <div className={styles.axis}><span>{labels[metric]} · {report.bucket_hours === 1 ? "hourly" : "daily"} intervals (UTC)</span><span>Peak {points.some((point) => point.value !== null) ? metricText(chart.peak, metric) : "—"}</span></div>
    <div className={styles.legend} aria-label="Chart series">{chart.lines.map((line) => <span key={line.key}><i className={styles[line.key]} />{line.label}</span>)}</div>
    <div className={styles.chartGrid}>
      <div className={styles.yAxis} aria-label={`${labels[metric]} scale`}>{chart.ticks.map((value) => <span key={value}>{metricText(value, metric)}</span>)}</div>
      <div className={styles.plot} role="group" aria-label="Model usage intervals">
        <svg viewBox="0 0 1000 240" preserveAspectRatio="none" className={styles.lines} aria-hidden="true">
          {[0, 60, 120, 180, 240].map((y) => <line key={y} x1="0" x2="1000" y1={y} y2={y} className={styles.gridLine} />)}
          {chart.lines.map((line) => <path key={line.key} d={line.path} className={styles[line.key]} fill="none" strokeWidth={line.key === "total" ? 2.5 : 1.5} vectorEffect="non-scaling-stroke" />)}
        </svg>
        {report.series.map((bucket, index) => <button type="button" className={styles.point} key={bucket.at}
          ref={(button) => { buttons.current[index] = button; }} tabIndex={index === entry ? 0 : -1}
          style={{ left: `${points[index].left}%`, width: `${points[index].width}%` }}
          aria-label={`${utc(bucket.at)} UTC · ${labels[metric]} ${metricText(points[index].value, metric)} · ${bucket.calls} calls · ${bucket.unknown_cost_calls} unknown costs`}
          aria-pressed={index === selectedIndex} onFocus={() => setSelected(bucket.at)} onMouseEnter={() => setSelected(bucket.at)} onClick={() => setSelected(bucket.at)}
          onKeyDown={(event) => {
            const next = event.key === "ArrowLeft" ? Math.max(0, index - 1) : event.key === "ArrowRight" ? Math.min(report.series.length - 1, index + 1) : event.key === "Home" ? 0 : event.key === "End" ? report.series.length - 1 : null;
            if (next !== null) { event.preventDefault(); buttons.current[next]?.focus(); }
          }}>
          {chart.lines.map((line) => line.points[index].value === null ? null : <span key={line.key} className={`${styles.marker} ${styles[line.key]}`} style={{ bottom: `${line.points[index].value! / chart.top * 100}%` }} />)}
        </button>)}
      </div>
      <div className={styles.timeAxis} aria-label="Time scale (UTC)">{[0, .25, .5, .75, 1].map((fraction) => <span key={fraction}>{new Date(rangeStart + fraction * rangeDuration).toLocaleString("en-US", { timeZone: "UTC", month: report.bucket_hours === 1 ? undefined : "short", day: report.bucket_hours === 1 ? undefined : "numeric", hour: report.bucket_hours === 1 ? "numeric" : undefined, minute: report.bucket_hours === 1 ? "2-digit" : undefined })}</span>)}</div>
    </div>
    <div className={styles.selection} aria-live="polite">
      {row ? <>
        <strong>{utc(row.at)} – {utc(row.until)} UTC</strong>
        {report.tracked_since && Date.parse(row.until) <= Date.parse(report.tracked_since) ? <p className={styles.muted}>Before accounting began; no recorded history.</p> : <>
          <dl className={styles.intervalFacts}>
            <div><dt>Reported spend</dt><dd>{row.unknown_cost_calls ? "≥ " : ""}{usd(row.cost_usd)}</dd></div>
            <div><dt>Tokens</dt><dd>{count(row.input_tokens + row.output_tokens)} <small>{count(row.input_tokens)} input · {count(row.output_tokens)} output</small></dd></div>
            <div><dt>Calls</dt><dd>{count(row.calls)} <small>{count(row.failed)} failed</small></dd></div>
            <div><dt>Mean latency</dt><dd>{metricText(row.latency_ms, "latency")}</dd></div>
          </dl>
          {row.unknown_cost_calls ? <p className={styles.muted}>{count(row.unknown_cost_calls)} calls with unknown cost; reported spend excludes them.</p> : null}
        </>}
      </> : <span className={styles.muted}>Hover or focus a point for details. Use arrow keys to move between intervals.</span>}
    </div>
  </section>;
}
