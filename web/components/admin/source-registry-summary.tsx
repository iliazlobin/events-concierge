"use client";

import { ArrowRight, BarChart3, CircleAlert } from "lucide-react";
import { useMemo } from "react";
import type { AdminSource } from "@/lib/admin-types";
import { summarizeSourceRegistry, type SourceRegistryBucket, type SourceRegistryLens } from "@/lib/admin-source-workspace";
import { int } from "./console-kit";
import styles from "./source-registry-summary.module.css";

export interface SourceRegistrySummaryProps {
  sources: AdminSource[];
  total: number;
  loading: boolean;
  failed: boolean;
  onFilter: (lens: SourceRegistryLens) => void;
  lens: SourceRegistryLens;
  onSelectSource: (key: string) => void;
}

export function SourceRegistrySummary({ sources, total, loading, failed, onFilter, lens, onSelectSource }: SourceRegistrySummaryProps): React.JSX.Element {
  const summary = useMemo(() => summarizeSourceRegistry(sources), [sources]);
  const partial = total > sources.length;
  const disabled = loading || failed;
  const maxVolume = summary.volume[0]?.published ?? 0;
  return <section className={styles.summary} aria-label="Source registry charts" aria-busy={loading}>
    <div className={styles.heading}>
      <div><BarChart3 aria-hidden="true" /><h2>Registry signals</h2>
        <span>{loading && !sources.length ? "Reading matching sources…" : failed && !sources.length ? "Registry evidence unavailable" : partial ? `${int(sources.length)} loaded of ${int(total)} matching sources` : `${int(sources.length)} matching sources`}</span></div>
    </div>
    {failed ? <p className={styles.stale} role="status"><CircleAlert aria-hidden="true" />{sources.length ? "Last received registry evidence. Filtering and expansion resume after a successful refresh." : "No registry evidence is available. Refresh before interpreting counts."}</p> : null}
    {!sources.length ? <p className={styles.empty}>{loading ? "Reading source states and latest-run evidence…" : failed ? "Source charts are unavailable." : "No sources match the current registry filters."}</p> : <>
      <div className={styles.grid}>
        <div className={styles.panel}>
          <h3>Latest recorded runs</h3>
          <div className={styles.outcomes}>{summary.outcomes.map((bucket) => <CountBar key={bucket.lens} bucket={bucket}
            denominator={summary.count} selected={lens === bucket.lens} disabled={disabled} onFilter={onFilter} />)}</div>
          <p>One latest run per source, including paused and retired sources. Run dates can differ; these counts are not a time-window success rate.</p>
        </div>
        <div className={styles.panel}>
          <h3>Latest published output <span>top {summary.volume.length}</span></h3>
          <div className={styles.volume}>{summary.volume.map((source) => <button key={source.sourceKey} type="button" disabled={disabled}
            onClick={() => onSelectSource(source.sourceKey)} aria-label={`Expand source: ${source.label} · ${source.published} latest published`}>
            <span className={styles.volumeTitle}><span>{source.label}</span><strong>{int(source.published)}</strong><ArrowRight aria-hidden="true" /></span>
            <span className={styles.track}><i style={{ width: `${maxVolume > 0 ? source.published / maxVolume * 100 : 0}%` }} /></span>
          </button>)}</div>
          {summary.latestPublishedTotal === null ? <p>No latest run has a recorded published count.</p> : <p><b>{int(summary.latestPublishedTotal)}</b> recorded outputs across {int(summary.measuredOutputSources)} sources; {int(summary.zeroOutputSources)} recorded zero. These are latest-run counts, not current catalog size.</p>}
          <p className={styles.outputCoverage}>{int(summary.unknownOutputSources)} latest runs have unknown output · {int(summary.noRunSources)} sources have no recorded run.</p>
        </div>
      </div>
      <div className={styles.footer}>
        <div className={styles.errors}><span>Latest failed errors</span>{summary.errors.length ? summary.errors.map((bucket) => <button key={bucket.lens} type="button"
          aria-label={`Filter sources: ${bucket.label} (${bucket.count})`} aria-pressed={lens === bucket.lens} disabled={disabled} onClick={() => onFilter(bucket.lens)}>
          {bucket.label.replace("Latest error: ", "")}<b>{int(bucket.count)}</b>
        </button>) : <small>No latest failed run in this loaded set.</small>}
          {summary.errorGroups > summary.errors.length ? <small>Top {summary.errors.length} of {summary.errorGroups} error groups</small> : null}
        </div>
      </div>
      {partial ? <p className={styles.scope}>Charts and their filters cover these {int(sources.length)} loaded sources only. The remaining matching sources are not represented.</p> : null}
    </>}
  </section>;
}

function CountBar({ bucket, denominator, selected, disabled, onFilter }: { bucket: SourceRegistryBucket; denominator: number; selected: boolean; disabled: boolean; onFilter: (lens: SourceRegistryLens) => void }): React.JSX.Element {
  return <button type="button" className={styles.countBar} data-tone={tone(bucket.lens)} aria-pressed={selected}
    aria-label={`Filter sources: ${bucket.label} (${bucket.count})`} disabled={disabled || bucket.count === 0} onClick={() => onFilter(bucket.lens)}>
    <span>{bucket.label.replace("Latest ", "")}</span><span className={styles.track}><i style={{ width: `${denominator ? bucket.count / denominator * 100 : 0}%` }} /></span><strong>{int(bucket.count)}</strong>
  </button>;
}

function tone(lens: SourceRegistryLens): string {
  if (["healthy", "latest_succeeded"].includes(lens)) return "ok";
  if (["failed", "blocked", "latest_failed"].includes(lens) || lens.startsWith("error:")) return "bad";
  if (["due", "deferred", "latest_deferred"].includes(lens)) return "warn";
  if (["running", "latest_running"].includes(lens)) return "info";
  return "quiet";
}
