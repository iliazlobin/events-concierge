"use client";

import { useState } from "react";
import { ArrowRight } from "lucide-react";
import type { AdminRunFilters, AdminSourceHealth, AdminTab } from "@/lib/admin-types";
import { operatingSummary, type OperatingEvidence } from "@/lib/admin-operating-model";
import { overviewSourceVisuals, type OverviewFreshnessGroup } from "@/lib/admin-overview-visuals";
import { adminSourceLocationUrl } from "@/lib/admin-history";
import { age } from "./console-kit";
import styles from "./operating-overview.module.css";

interface OverviewProps {
  evidence: OperatingEvidence;
  onNavigate: (tab: AdminTab, filters?: AdminRunFilters) => void;
}

const count = (value: number) => value.toLocaleString("en-US");
const sourceHref = (source: AdminSourceHealth) => adminSourceLocationUrl(source.source_key, "http://localhost/admin");
const catalogHref = (source: AdminSourceHealth) => `/admin?${new URLSearchParams({ tab: "catalog", store_source: source.source_key, store_dates: "upcoming" })}`;

export function OperatingOverview({ evidence, onNavigate }: OverviewProps) {
  const [selection, setSelection] = useState<OverviewFreshnessGroup | "attention">("attention");
  const visuals = overviewSourceVisuals(evidence.sources);
  const [freshness, catalog] = operatingSummary(evidence);
  const selectedGroup = visuals?.grouped.find(group => group.id === selection);
  const shownSources = (selectedGroup?.sources ?? visuals?.attention ?? []).slice(0, 3);
  const selectedCount = selectedGroup?.sources.length ?? visuals?.attention.length ?? 0;

  return <section className={styles.summary} aria-label="System summary">
    <article className={styles.card} aria-label="Source freshness">
      <div className={styles.heading}>
        <div><h2>Fresh sources</h2><p>Collection freshness</p></div>
        <strong className={styles.value}>{freshness.value}</strong>
      </div>
      {visuals ? <>
        <div className={styles.distribution} role="img" aria-label={visuals.scheduled.length
          ? `Freshness of ${count(visuals.scheduled.length)} scheduled sources: ${visuals.grouped.map(group => `${count(group.sources.length)} ${group.label.toLowerCase()}`).join(", ")}`
          : "No scheduled sources"}>
          {visuals.grouped.filter(group => group.sources.length > 0).map(group => <span key={group.id} className={`${styles.segment} ${styles[group.id]}`}
            style={{ width: `${group.sources.length / visuals.scheduled.length * 100}%` }} />)}
        </div>
        <div className={styles.legend} role="group" aria-label="Inspect sources by freshness">
          {visuals.grouped.map(group => <button key={group.id} type="button" className={`${styles.legendItem} ${styles[group.id]}`}
            aria-pressed={selection === group.id} onClick={() => setSelection(group.id)}>
            <span className={styles.dot} aria-hidden="true" /><span>{group.label}</span><strong>{count(group.sources.length)}</strong>
          </button>)}
        </div>
        <div className={styles.insight}>
          <div className={styles.listHeading}>
            <h3>{selection === "attention" ? "Sources to inspect" : `${selectedGroup?.label ?? "Selected"} sources`}</h3>
            {selection !== "attention" ? <button type="button" className={styles.textLink} onClick={() => setSelection("attention")}>Show attention</button>
              : <span>{count(visuals.attention.length)} outside policy</span>}
          </div>
          <div aria-live="polite">
            {shownSources.length ? <ul className={styles.sourceList}>
              {shownSources.map(source => <li key={source.source_key}>
                <a href={sourceHref(source)} className={styles.sourceLink}>
                  <span className={styles.sourceName}>{source.display_name}</span><ArrowRight aria-hidden="true" />
                  <span className={styles.sourceEvidence}>{source.last_success_at ? `Last success ${age(source.last_success_at)}` : "No successful run recorded"}
                    {source.latest_run_status ? ` · ${source.latest_run_status}` : ""}</span>
                  {source.latest_run_error ? <span className={styles.error}>{source.latest_run_error.replaceAll("_", " ")}</span> : null}
                </a>
              </li>)}
            </ul> : <p className={styles.empty}>{selection === "attention" && visuals.scheduled.length
              ? "All scheduled sources are within freshness policy."
              : selection === "attention" ? "No enabled sources are scheduled." : `No ${selectedGroup?.label.toLowerCase()} sources in this snapshot.`}</p>}
            {selectedCount > shownSources.length ? <p className={styles.more}>Showing {shownSources.length} of {count(selectedCount)} sources</p> : null}
          </div>
        </div>
        <p className={styles.scope}>Enabled, scheduled sources · {count(visuals.excluded)} paused, retired or unscheduled excluded.</p>
      </> : <p className={styles.unavailable}>Source snapshot unavailable. Freshness and source details will appear when the snapshot is available.</p>}
      <button type="button" className={styles.openLink} onClick={() => onNavigate("sources")}>Open sources<ArrowRight aria-hidden="true" /></button>
    </article>
    <article className={styles.card} aria-label="Catalog coverage">
      <div className={styles.heading}>
        <div><h2>Catalog events</h2><p>Upcoming unique events</p></div>
        <strong className={styles.value}>{catalog.value}</strong>
      </div>
      {visuals ? <>
        <div className={styles.coverage}>
          <div><strong>{count(visuals.withEvents)}</strong><span> of {count(visuals.totalSources)} sources have upcoming events</span></div>
          <div className={styles.coverageBar} role="img" aria-label={`${count(visuals.withEvents)} sources with upcoming events; ${count(visuals.withoutEvents)} without upcoming events`}>
            <span style={{ width: `${visuals.totalSources ? visuals.withEvents / visuals.totalSources * 100 : 0}%` }} />
          </div>
        </div>
        <div className={styles.listHeading}><h3>Largest source catalogs</h3><span>Upcoming events</span></div>
        {visuals.largestSources.length ? <ol className={styles.volumeList}>
          {visuals.largestSources.map(source => <li key={source.source_key}>
            <a href={catalogHref(source)} className={styles.volumeLink} aria-label={`${source.display_name}: ${count(source.upcoming_events)} upcoming events. Open catalog`}>
              <span className={styles.volumeLabel}><span>{source.display_name}</span><strong>{count(source.upcoming_events)}</strong><ArrowRight aria-hidden="true" /></span>
              <span className={styles.volumeTrack} aria-hidden="true"><span style={{ width: `${source.upcoming_events / visuals.largestSourceEvents * 100}%` }} /></span>
            </a>
          </li>)}
        </ol> : <p className={styles.empty}>No sources have upcoming events in this snapshot.</p>}
        <p className={styles.scope}>Per-source counts can overlap and include retained events from paused sources. Unique total excludes cancelled events and fixtures.</p>
      </> : <p className={styles.unavailable}>Source coverage is unavailable. {evidence.catalog ? "The unique event total is from the catalog snapshot." : "Catalog snapshot unavailable."}</p>}
      {!evidence.catalog && visuals ? <p className={styles.scope}>Unique catalog total unavailable; source counts use their own snapshot.</p> : null}
      <button type="button" className={styles.openLink} onClick={() => onNavigate("catalog")}>Open catalog<ArrowRight aria-hidden="true" /></button>
    </article>
  </section>;
}
