"use client";

import { ArrowUpRight, RefreshCw, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { AdminSource } from "@/lib/admin-types";
import { projectSourceSchedule, type SourceScheduleHorizon } from "@/lib/admin-source-schedule";
import { sourceCollectionTimestamp } from "@/lib/admin-source-operations";
import { formatRelativeTime, humanize } from "@/lib/admin-presentation";
import { int } from "./console-kit";
import styles from "./source-collection-schedule.module.css";

type View = "next" | "due" | "running" | "undated";
const PAGE_SIZE = 8;
const WINDOWS = [{ hours: 24, label: "24h" }, { hours: 48, label: "48h" }, { hours: 168, label: "7d" }] as const;
const localTime = (value: string) => new Intl.DateTimeFormat(undefined, {
  month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
}).format(new Date(value));
const relativeTime = (value: string, now: number) => {
  const minutes = Math.round((Date.parse(value) - now) / 60_000);
  const format = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  return Math.abs(minutes) < 60 ? format.format(minutes, "minute")
    : Math.abs(minutes) < 1440 ? format.format(Math.round(minutes / 60), "hour")
    : format.format(Math.round(minutes / 1440), "day");
};

export function SourceCollectionSchedule({ sources, sourceKey, readAt, onRefresh, onOpenSource }: {
  sources: readonly AdminSource[] | null;
  sourceKey: string;
  readAt: string;
  onRefresh: () => void;
  onOpenSource: (key: string) => void;
}) {
  const [hours, setHours] = useState<SourceScheduleHorizon>(24);
  const [view, setView] = useState<View>("next");
  const [interval, setInterval] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(PAGE_SIZE);
  const now = Date.parse(readAt);
  const schedule = useMemo(() => projectSourceSchedule(sources, sourceKey, now, hours), [sources, sourceKey, now, hours]);
  const selectedBucket = schedule.buckets.find(bucket => bucket.startAt === interval);
  const due = new Set(schedule.due.map(entry => entry.source.source_key));
  const running = new Set(schedule.running.map(entry => entry.source.source_key));
  const allNext = [...schedule.due, ...schedule.upcoming];
  const entries = view === "due" ? schedule.due : view === "running" ? schedule.running
    : view === "undated" ? schedule.undated : selectedBucket?.sources ?? allNext;
  const needle = query.trim().toLowerCase();
  const matching = entries.filter(({ source }) => `${source.display_name} ${source.source_key} ${source.publisher}`.toLowerCase().includes(needle));
  const shown = matching.slice(0, limit);
  const maximum = Math.max(1, ...schedule.buckets.map(bucket => bucket.count));
  const selectView = (next: View) => { setView(next); setInterval(null); setLimit(PAGE_SIZE); };
  const scopeLabel = hours === 168 ? "7 days" : `${hours} hours`;
  if (schedule.state !== "available" || !schedule.horizonEndsAt) return null;

  return <section className={styles.schedule} aria-label="Upcoming collection schedule">
    <header className={styles.heading}>
      <div><h3>Upcoming collection</h3><p>Next refresh per source · times in {Intl.DateTimeFormat().resolvedOptions().timeZone}</p></div>
      <div className={styles.controls}>
        <div className={styles.windows} role="group" aria-label="Collection schedule window">{WINDOWS.map(option => <button key={option.hours} type="button" aria-pressed={hours === option.hours}
          onClick={() => { setHours(option.hours); setInterval(null); setLimit(PAGE_SIZE); }}>{option.label}</button>)}</div>
        <button type="button" className={styles.refresh} aria-label="Refresh collection schedule" onClick={onRefresh}><RefreshCw aria-hidden="true" />Refresh</button>
      </div>
    </header>
    <div className={styles.states} role="group" aria-label="Collection schedule state">
      {[{ value: "next", label: "Next up", aria: "Show next sources", count: allNext.length },
        { value: "due", label: "Due now", aria: "Show due sources", count: schedule.due.length },
        { value: "running", label: "Collecting", aria: "Show collecting sources", count: schedule.running.length },
        { value: "undated", label: "No due date", aria: "Show sources without a due date", count: schedule.undated.length }].map(item =>
        <button type="button" key={item.value} aria-label={item.aria} aria-pressed={view === item.value} data-attention={item.value === "due" && item.count > 0}
          onClick={() => selectView(item.value as View)}>{item.label}<strong>{int(item.count)}</strong></button>)}
    </div>
    <div className={styles.timelineHeading}><span>{int(schedule.upcoming.length)} sources become due in the next {scopeLabel}</span><span>{hours === 24 ? "1-hour" : hours === 48 ? "2-hour" : "12-hour"} intervals · select to inspect</span></div>
    <div className={styles.timeline} role="group" aria-label="Collection schedule timeline">
      {schedule.buckets.map(bucket => <button type="button" key={bucket.startAt} disabled={!bucket.count} aria-pressed={selectedBucket?.startAt === bucket.startAt}
        aria-label={`Inspect sources due ${sourceCollectionTimestamp(bucket.startAt)} to ${sourceCollectionTimestamp(bucket.endAt)}`}
        title={`${localTime(bucket.startAt)} – ${localTime(bucket.endAt)} · ${bucket.count} sources`}
        onClick={() => { setView("next"); setInterval(interval === bucket.startAt ? null : bucket.startAt); setLimit(PAGE_SIZE); }}>
        <span>{bucket.count || ""}</span><i style={{ height: `${bucket.count ? Math.max(6, bucket.count / maximum * 46) : 2}px` }} />
      </button>)}
    </div>
    <div className={styles.axis}><time dateTime={readAt}>Now · {localTime(readAt)}</time><time dateTime={schedule.horizonEndsAt}>{localTime(schedule.horizonEndsAt)}</time></div>
    <div className={styles.listHeading}>
      <div>{selectedBucket ? <><strong>{localTime(selectedBucket.startAt)} – {localTime(selectedBucket.endAt)}</strong>
        <button type="button" onClick={() => { setInterval(null); setLimit(PAGE_SIZE); }}>Show entire window<X aria-hidden="true" /></button></>
        : <strong>{view === "next" ? "Next sources by eligibility time" : view === "due" ? "Sources already due" : view === "running" ? "Recorded as collecting" : "Eligible sources without a recorded due date"}</strong>}</div>
      <label className={styles.search}><Search aria-hidden="true" /><input aria-label="Find scheduled source" placeholder="Find a source or publisher" value={query}
        onChange={event => { setQuery(event.target.value); setLimit(PAGE_SIZE); }} /></label>
    </div>
    {shown.length ? <div className={styles.tableScroll}><table className={styles.table} aria-label="Upcoming source collections">
      <thead><tr><th>Source</th><th>Next eligible</th><th>Latest recorded run</th></tr></thead>
      <tbody>{shown.map(({ source, eligibleAt }) => {
        const isDue = due.has(source.source_key); const isRunning = running.has(source.source_key);
        const run = source.latest_run;
        return <tr key={source.source_key}>
          <td><button type="button" aria-label={`Manage source for ${source.display_name}`} onClick={() => onOpenSource(source.source_key)}>{source.display_name}<ArrowUpRight aria-hidden="true" /></button>
            <small>{source.publisher}</small></td>
          <td>{isRunning ? <><strong className={styles.running}>Collecting</strong><small>Next date follows completion</small></>
            : isDue ? <><strong className={styles.due}>Due now</strong><small>{eligibleAt ? `Eligible ${relativeTime(eligibleAt, now)}` : "Waiting for collection"}</small></>
            : eligibleAt ? <><time dateTime={eligibleAt} title={sourceCollectionTimestamp(eligibleAt)}>{localTime(eligibleAt)}</time><small>{relativeTime(eligibleAt, now)}</small></>
            : <><span>Not recorded</span><small>Inspect source configuration</small></>}</td>
          <td><span data-outcome={run?.status}>{run ? run.status === "paused" ? "Deferred" : humanize(run.status) : "No run recorded"}</span>
            <small>{source.last_succeeded_at ? `Last success ${formatRelativeTime(source.last_succeeded_at)}` : "No successful collection yet"}</small></td>
        </tr>;
      })}</tbody>
    </table></div> : <p className={styles.empty} role="status">{needle ? "No sources match this search." : view === "due" ? "No sources are currently due." : view === "running" ? "No sources are recorded as collecting." : view === "undated" ? "All eligible sources have a recorded due date or collection state." : "No sources are due in this window. Choose a longer window to look ahead."}</p>}
    <footer className={styles.footer}><span>{int(shown.length)} of {int(matching.length)} sources{needle ? " matching search" : " in this view"}</span>
      {shown.length < matching.length ? <button type="button" aria-label="Show more sources" onClick={() => setLimit(value => value + PAGE_SIZE)}>Show {Math.min(PAGE_SIZE, matching.length - shown.length)} more sources</button> : null}
    </footer>
    <p className={styles.caption}>{schedule.laterCount ? `${int(schedule.laterCount)} sources become due after this window. ` : ""}{schedule.excludedCount ? `${int(schedule.excludedCount)} paused, retired or ineligible sources excluded. ` : ""}
      Forecast from current cadence; actual starts depend on worker availability. Read <time dateTime={readAt} title={sourceCollectionTimestamp(readAt)}>{localTime(readAt)}</time>.</p>
  </section>;
}
