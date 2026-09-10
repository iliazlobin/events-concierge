"use client";

import { ArrowUpRight, Check, Copy, RefreshCw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { AdminRunFilters } from "@/lib/admin-types";
import { getAdminSourceHealth } from "@/lib/admin-api";
import { catalogLocationFromUrl, catalogLocationUrl, type CatalogLocation, type CatalogDateScope, type CatalogPriceScope } from "@/lib/admin-catalog-navigation";
import { SourceCatalogTable } from "./source-catalog-table";
import { Action, PageHead, kit } from "./console-kit";
import { useAdminSnapshot } from "./use-admin-snapshot";
import styles from "./catalog-view.module.css";

interface Props {
  refreshVersion?: number;
  onOpenSource: (sourceKey: string) => void;
  onOpenRun: (sourceKey: string, runKey: string) => void;
  onOpenRuns: (filters: AdminRunFilters) => void;
}
const loadSources = (signal: AbortSignal) => getAdminSourceHealth(false, signal);

export function CatalogView({ refreshVersion = 0, onOpenSource, onOpenRun, onOpenRuns }: Props) {
  const health = useAdminSnapshot(loadSources, refreshVersion);
  const [location, setLocation] = useState(() => catalogLocationFromUrl("http://localhost/admin"));
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [copyStatus, setCopyStatus] = useState<"copied" | "failed" | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const restore = () => { setLocation(catalogLocationFromUrl(window.location.href)); setCopyStatus(null); };
    restore(); window.addEventListener("popstate", restore);
    return () => { window.removeEventListener("popstate", restore); if (timer.current) clearTimeout(timer.current); };
  }, []);
  const change = (changes: Partial<CatalogLocation>, replace = false) => {
    const url = catalogLocationUrl({ ...location, ...changes }, window.location.href);
    setLocation(catalogLocationFromUrl(new URL(url, window.location.href).href));
    setCopyStatus(null);
    if (url !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history[replace ? "replaceState" : "pushState"](window.history.state, "", url);
  };
  const filter = (changes: Partial<CatalogLocation>) => change({ ...changes, eventId: null, afterStart: null, afterId: null });
  const sources = health.failed || health.authorizationDenied ? [] : health.data?.sources ?? [];
  const sorted = [...sources].sort((a, b) => a.display_name.localeCompare(b.display_name));
  const missingSource = location.sourceKey && !sources.some(source => source.source_key === location.sourceKey);
  const filtered = Boolean(location.sourceKey || location.runKey || location.query || location.dateScope !== "all" || location.priceScope !== "all");

  return <div className={`${kit.page} ${styles.page}`}>
    <PageHead eyebrow="Administration" title="Catalog" sub="Published events across all sources. Search, filter and open an event to inspect its records."
      actions={<><Action onClick={async () => {
        try {
          await navigator.clipboard.writeText(new URL(catalogLocationUrl(location, window.location.href), window.location.origin).href);
          setCopyStatus("copied");
        } catch { setCopyStatus("failed"); }
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopyStatus(null), 2500);
      }}>{copyStatus === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}{copyStatus === "copied" ? "Copied" : "Copy investigation link"}</Action>
      <Action onClick={() => { setRefreshNonce(value => value + 1); void health.refresh(); }}><RefreshCw aria-hidden="true" />Refresh</Action></>} />
    {copyStatus === "failed" ? <p role="status" className={styles.note}>Copy unavailable. The address bar contains this investigation link.</p> : null}
    <section className={styles.root} aria-label="Catalog investigation" id="catalog-records" tabIndex={-1}>
      {location.runKey ? <div className={styles.runContext}>
        <span>Publishing run <code>{location.runKey}</code></span>
        <div className={styles.actions}><button type="button" className={styles.button} onClick={() => onOpenRun(location.sourceKey, location.runKey!)}>Publishing run<ArrowUpRight aria-hidden="true" /></button>
          <button type="button" className={styles.button} onClick={() => filter({ runKey: undefined })}>Clear run filter<X aria-hidden="true" /></button></div>
        <p>Current records attributed to this run. Later refreshes can replace attribution; new versus updated records are not recorded.</p>
      </div> : null}
      <SourceCatalogTable onOpenRuns={onOpenRuns} sourceKey={location.sourceKey} runKey={location.runKey} dateScope={location.dateScope} priceScope={location.priceScope}
        refreshToken={`${refreshVersion}:${refreshNonce}`}
        filters={<div className={styles.filters}>
          <label className={styles.sourceFilter}><span>Source</span><select aria-label="Catalog source" value={location.sourceKey} onChange={event => filter({ sourceKey: event.target.value, runKey: undefined })}>
            <option value="">All sources</option>
            {missingSource ? <option value={location.sourceKey}>{location.sourceKey}</option> : null}
            {sorted.map(source => <option key={source.source_key} value={source.source_key}>{source.display_name}</option>)}
          </select></label>
          <label><span>Dates</span><select aria-label="Event dates" value={location.dateScope} onChange={event => filter({ dateScope: event.target.value as CatalogDateScope })}>
            <option value="all">All dates</option><option value="upcoming">Upcoming</option><option value="past">Past</option>
          </select></label>
          <label><span>Price</span><select aria-label="Event price" value={location.priceScope} onChange={event => filter({ priceScope: event.target.value as CatalogPriceScope })}>
            <option value="all">All prices</option><option value="free">Free</option><option value="paid">Paid</option><option value="unknown">Unknown price</option>
          </select></label>
          {filtered ? <button type="button" className={styles.clear} onClick={() => filter({ sourceKey: "", runKey: undefined, query: "", dateScope: "all", priceScope: "all" })}>Clear filters<X aria-hidden="true" /></button> : null}
        </div>}
        investigation={{ query: location.query, eventId: location.eventId,
          cursor: location.afterStart && location.afterId ? { startAt: location.afterStart, canonicalEventId: location.afterId } : null,
          onChange: changes => change({
            ...("query" in changes ? { query: changes.query ?? "" } : {}),
            ...("eventId" in changes ? { eventId: changes.eventId ?? null } : {}),
            ...("cursor" in changes ? { afterStart: changes.cursor?.startAt ?? null, afterId: changes.cursor?.canonicalEventId ?? null } : {}),
          }, "query" in changes), onOpenRun, onOpenSource,
        }} />
      {health.failed ? <p className={styles.note} role="status">Source filter choices are unavailable. Events and saved filters remain accessible. <button type="button" className={styles.clear} onClick={() => void health.refresh()}>Retry sources</button></p> : null}
    </section>
  </div>;
}
