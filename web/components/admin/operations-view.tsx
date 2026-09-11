"use client";

import { ArrowRight, Check, ChevronDown, ChevronLeft, ChevronRight, Copy, ExternalLink, RefreshCw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { getAdminSourceHealth } from "@/lib/admin-api";
import type { AdminOverview, AdminRunFilters, AdminTab } from "@/lib/admin-types";
import { getBackendOperations } from "@/lib/backend-operations";
import { operationQueueDefinition, operationQueues } from "@/lib/admin-operations-overview";
import { operationsNavigation, operationsNavigationUrl } from "@/lib/admin-operations-navigation";
import { supportsWorkRecords, workErrorUrl, workRecordScopeUrl, type WorkRecordScope } from "@/lib/admin-work-errors";
import { Action, Chip, PageHead, age, kit, stamp } from "./console-kit";
import { useAdminSnapshot } from "./use-admin-snapshot";
import { WorkErrorInvestigation } from "./work-error-investigation";
import { WorkPreview } from "./work-preview";
import { OperatingOverview } from "./operating-overview";
import styles from "./operations-view.module.css";

const ARCHITECTURE_URL = "https://app.notion.com/p/391d865005a88164a182eabc18fe068f";
const RUNBOOK_URL = "https://github.com/iliazlobin/events-concierge/blob/review/admin-console-aggregates/deploy/development.md";
const loadSources = (signal: AbortSignal) => getAdminSourceHealth(false, signal);

export interface OperationsViewProps {
  refreshVersion?: number;
  catalogRead: {
    data: AdminOverview | null;
    loading: boolean;
    failed: boolean;
    authorizationDenied: boolean;
    refresh: () => Promise<void>;
  };
  retryAdminSession?: () => Promise<void>;
  onNavigate: (tab: AdminTab, filters?: AdminRunFilters) => void;
}

export function OperationsView({ refreshVersion = 0, catalogRead: catalog, retryAdminSession, onNavigate }: OperationsViewProps) {
  const backend = useAdminSnapshot(getBackendOperations, refreshVersion);
  const sources = useAdminSnapshot(loadSources, refreshVersion);
  const overview = backend.loading || backend.failed || backend.authorizationDenied ? null : backend.data?.overview ?? null;
  const sourceSnapshot = sources.loading || sources.failed || sources.authorizationDenied ? null : sources.data;
  const catalogSnapshot = catalog.loading || catalog.failed || catalog.authorizationDenied ? null : catalog.data;
  const [retrying, setRetrying] = useState(false);
  const [retryAreas, setRetryAreas] = useState<string[]>([]);
  const failedReads = [
    { name: "Work", read: backend }, { name: "Sources", read: sources }, { name: "Catalog", read: catalog },
  ].filter(({ read }) => read.failed || read.authorizationDenied);
  const unavailableAreas = [...new Set([...failedReads.map(({ name }) => name), ...(retrying ? retryAreas : [])])];
  const [selectedQueue, setSelectedQueue] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [recordsRefreshVersion, setRecordsRefreshVersion] = useState(0);
  const [previewSelectionVersion, setPreviewSelectionVersion] = useState(0);
  const [workPage, setWorkPage] = useState(1);
  const previousQueue = useRef<string | null>(null);
  const pendingFocus = useRef<string | null>(null);
  const page = useRef<HTMLDivElement>(null);
  const workList = useRef<HTMLElement>(null);
  const investigation = useRef<HTMLElement>(null);
  const definition = selectedQueue ? operationQueueDefinition(selectedQueue) : null;
  const queues = new Map(operationQueues(overview?.queues ?? []).map(row => [row.queue.queue, row]));
  const workEntries = [...new Set([
    "request_start", "notifications", ...[...queues.values()].filter(row => row.needsAttention).map(row => row.queue.queue),
    ...(selectedQueue && supportsWorkRecords(selectedQueue) ? [selectedQueue] : []),
  ])];
  const pageCount = Math.max(1, Math.ceil(workEntries.length / 5));
  const selectedIndex = selectedQueue ? workEntries.indexOf(selectedQueue) : -1;
  const visiblePage = selectedIndex >= 0 ? Math.floor(selectedIndex / 5) + 1 : Math.min(workPage, pageCount);
  const firstEntry = (visiblePage - 1) * 5;
  const visibleEntries = workEntries.slice(firstEntry, firstEntry + 5);

  const focusEntry = (queue: string | null) => requestAnimationFrame(() => {
    const trigger = queue ? document.querySelector<HTMLButtonElement>(`[data-work-entry="${CSS.escape(queue)}"]`) : null;
    const target = trigger ?? workList.current ?? page.current;
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: "nearest" });
  });

  useEffect(() => {
    const read = () => {
      const { queue, workPage: savedPage } = operationsNavigation(window.location.href);
      const previous = previousQueue.current;
      previousQueue.current = queue;
      setSelectedQueue(queue); setCopied(false);
      setWorkPage(savedPage ?? 1);
      if (!queue) pendingFocus.current = previous ?? window.history.state?.adminWorkFocus ?? null;
    };
    read(); window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  useEffect(() => {
    if (!selectedQueue && !backend.loading && pendingFocus.current) {
      focusEntry(pendingFocus.current);
      pendingFocus.current = null;
    }
  }, [selectedQueue, backend.loading, visiblePage]);

  useEffect(() => {
    if (!selectedQueue || backend.loading || visiblePage === workPage) return;
    // A bookmarked investigation follows its entry if the current work list changed.
    setWorkPage(visiblePage);
    const next = operationsNavigationUrl(window.location.href, { queue: selectedQueue, workPage: visiblePage });
    window.history.replaceState(window.history.state, "", next);
  }, [selectedQueue, backend.loading, visiblePage, workPage]);

  const inspectQueue = (queue: string | null, record?: string | null, scope?: WorkRecordScope) => {
    const previous = selectedQueue;
    let next = operationsNavigationUrl(window.location.href, { queue, workPage: visiblePage });
    if (queue && record !== undefined) {
      next = workErrorUrl(workRecordScopeUrl(next, scope ?? "pending"), record, 0);
      setPreviewSelectionVersion(value => value + 1);
    }
    if (queue && !previous) window.history.replaceState({ ...window.history.state, adminWorkFocus: queue }, "", window.location.href);
    if (next !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(window.history.state, "", next);
    previousQueue.current = queue;
    pendingFocus.current = !queue ? previous : null;
    setSelectedQueue(queue); setCopied(false);
    if (queue) requestAnimationFrame(() => {
      investigation.current?.focus({ preventScroll: true });
      investigation.current?.scrollIntoView({ block: "nearest" });
    });
  };
  const changeWorkPage = (nextPage: number) => {
    const next = operationsNavigationUrl(window.location.href, { queue: null, workPage: nextPage });
    window.history.pushState(window.history.state, "", next);
    setSelectedQueue(null); previousQueue.current = null; pendingFocus.current = null;
    setCopied(false); setWorkPage(nextPage);
  };
  const refresh = () => { void backend.refresh(); void sources.refresh(); void catalog.refresh(); void retryAdminSession?.(); setRecordsRefreshVersion(value => value + 1); };
  const retryUnavailable = async () => {
    if (retrying) return;
    setRetryAreas(failedReads.map(({ name }) => name));
    setRetrying(true);
    try { await Promise.allSettled([...failedReads.map(({ read }) => read.refresh()), ...(retryAdminSession ? [retryAdminSession()] : [])]); }
    finally { setRetrying(false); }
  };
  const copyLink = async () => {
    try { await navigator.clipboard.writeText(window.location.href); setCopied(true); } catch { setCopied(false); }
  };
  const investigationLabel = definition ? `${definition.name} ${selectedQueue === "entity_refresh" ? "errors" : "records"}` : "Work records";
  const expandedInvestigation = selectedQueue ? <section className={styles.investigation} ref={investigation} tabIndex={-1}
    id="work-investigation" aria-label={supportsWorkRecords(selectedQueue) ? investigationLabel : "Investigation unavailable"}>
    <div className={styles.investigationActions}><button type="button" onClick={() => inspectQueue(null)} aria-label={supportsWorkRecords(selectedQueue) ? `Collapse ${investigationLabel}` : "Collapse investigation"}><X aria-hidden="true" />Collapse</button></div>
    {supportsWorkRecords(selectedQueue) ? <WorkErrorInvestigation embedded key={`${selectedQueue}-${previewSelectionVersion}`} queue={selectedQueue} refreshVersion={refreshVersion + recordsRefreshVersion} onSelectionChange={() => setCopied(false)} />
      : <div className={styles.unavailable}><p>This saved link has no record investigation in the console.</p>
        {definition?.commands ? <button type="button" onClick={() => onNavigate("commands")}>Open commands<ArrowRight aria-hidden="true" /></button>
          : <a href={RUNBOOK_URL} target="_blank" rel="noreferrer">Open operations runbook<ExternalLink aria-hidden="true" /></a>}
      </div>}
    <div className={styles.investigationLinks}><button type="button" onClick={() => void copyLink()}>{copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}{copied ? "Copied" : "Copy inspection link"}</button>
      <a href={RUNBOOK_URL} target="_blank" rel="noreferrer">Operations runbook<ExternalLink aria-hidden="true" /></a></div>
  </section> : null;

  return <div className={`${kit.page} ${styles.page}`} ref={page} tabIndex={-1}>
    <PageHead eyebrow="Administration" title="Overview" sub="Current catalog coverage and background work." actions={<div className={styles.headerActions}>
      <Action onClick={refresh}><RefreshCw aria-hidden="true" />Refresh</Action>
    </div>} />
    <div className={styles.snapshotLine}>
      {overview ? <Chip>{overview.environment}</Chip> : null}
      <span title={overview ? stamp(overview.generated_at) : undefined}>Work {overview ? age(overview.generated_at) : backend.loading ? "loading…" : "unavailable"}{overview && backend.loading ? " · refreshing" : ""}</span>
      <span title={sourceSnapshot ? stamp(sourceSnapshot.generated_at) : undefined}>Sources {sourceSnapshot ? age(sourceSnapshot.generated_at) : sources.loading ? "loading…" : "unavailable"}</span>
      <span title={catalogSnapshot ? stamp(catalogSnapshot.generated_at) : undefined}>Catalog {catalogSnapshot ? age(catalogSnapshot.generated_at) : catalog.loading ? "loading…" : "unavailable"}</span>
      <details className={styles.snapshotDetails}>
        <summary>Snapshot details</summary>
        <div className={styles.snapshotPanel}><dl className={styles.facts}>
          <div><dt>Work snapshot</dt><dd>{overview ? stamp(overview.generated_at) : "Unavailable"}</dd></div>
          <div><dt>Source snapshot</dt><dd>{sourceSnapshot ? stamp(sourceSnapshot.generated_at) : "Unavailable"}</dd></div>
          <div><dt>Catalog snapshot</dt><dd>{catalogSnapshot ? stamp(catalogSnapshot.generated_at) : "Unavailable"}</dd></div>
          {overview ? <><div><dt>Release revision</dt><dd><code>{overview.release_revision}</code></dd></div>
            <div><dt>Image digest</dt><dd><code>{overview.image_digest ?? "Not reported"}</code></dd></div>
            <div><dt>Schema revisions</dt><dd><code>{overview.schema_revisions.join(", ") || "Not reported"}</code></dd></div>
            <div><dt>Operator</dt><dd>{backend.data?.session.subject ?? "Unavailable"}</dd></div>
            <div><dt>Role / authentication</dt><dd>{backend.data?.session.role ?? "Unknown"} · {backend.data?.session.authentication ?? "Unknown"}</dd></div></> : null}
        </dl></div>
      </details>
    </div>
    {unavailableAreas.length ? <div className={styles.readError} role="alert" aria-label="Overview data status">
      <span><strong>{unavailableAreas.length === 3 ? "Overview data unavailable." : "Some overview data unavailable."}</strong> {unavailableAreas.join(", ")} could not be loaded. Missing measurements are shown as Unknown.</span>
      <button type="button" disabled={retrying} onClick={() => void retryUnavailable()}>{retrying ? "Retrying…" : "Retry"}</button>
    </div> : null}

      <OperatingOverview evidence={{ sources: sourceSnapshot, catalog: catalogSnapshot }} onNavigate={onNavigate} />
      <div className={styles.overviewLinks}>
        <p className={styles.coverage}>Collection volume, outcomes and published output over time.</p>
        <button type="button" onClick={() => onNavigate("run-stats")}>View run statistics<ArrowRight aria-hidden="true" /></button>
      </div>
      <section className={styles.work} aria-label="Background work" ref={workList} tabIndex={-1}>
        <div className={styles.workHeading}><h2>Background work</h2><span>Queue insights and a few records to investigate</span></div>
        <ul className={styles.workRecords} aria-label="Work records">{visibleEntries.map(queue => {
          const row = queues.get(queue);
          const name = operationQueueDefinition(queue).name;
          const primary = queue === "request_start" || queue === "notifications";
          const detail = operationQueueDefinition(queue).purpose;
          const expanded = queue === selectedQueue;
          return <li key={queue} data-expanded={expanded} aria-label={`${name} overview`}><div className={styles.workEntry}><div><h3>{name}</h3><span>{detail}</span></div>
            {supportsWorkRecords(queue) ? <button type="button" data-work-entry={queue} aria-label={`Inspect ${primary ? name.toLowerCase() : name}`} aria-expanded={expanded} aria-controls={expanded ? "work-investigation" : undefined} onClick={() => inspectQueue(expanded ? null : queue)}>Inspect {primary ? "records" : "errors"}<ChevronDown aria-hidden="true" /></button>
              : <a href={RUNBOOK_URL} target="_blank" rel="noreferrer">Investigation guide<ExternalLink aria-hidden="true" /></a>}</div>
            {supportsWorkRecords(queue) ? <div hidden={expanded}><WorkPreview queue={queue} totals={row?.queue} refreshVersion={refreshVersion + recordsRefreshVersion} active={!expanded} onInspect={(record, scope) => inspectQueue(queue, record, scope)} /></div>
              : <p className={styles.coverage}>{row?.attention ?? row?.work ?? "Saved investigation"}</p>}
            {expanded ? expandedInvestigation : null}</li>;
        })}</ul>
        {pageCount > 1 ? <div className={styles.workPagination}>
          <span role="status">{firstEntry + 1}–{Math.min(firstEntry + 5, workEntries.length)} of {workEntries.length}</span>
          <nav aria-label="Background work pages"><button type="button" disabled={visiblePage <= 1} onClick={() => changeWorkPage(visiblePage - 1)}><ChevronLeft aria-hidden="true" />Previous</button>
            <button type="button" disabled={visiblePage >= pageCount} onClick={() => changeWorkPage(visiblePage + 1)}>Next<ChevronRight aria-hidden="true" /></button></nav>
        </div> : null}
      </section>
      {selectedQueue && selectedIndex < 0 ? expandedInvestigation : null}
      <div className={styles.overviewLinks}><p className={styles.coverage}>Recorded data only. Service availability and capacity are not measured.</p>
        <a href={ARCHITECTURE_URL} target="_blank" rel="noreferrer">System design<ExternalLink aria-hidden="true" /></a></div>
  </div>;
}
