"use client";

import { Check, ChevronDown, ChevronLeft, ChevronRight, Copy, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import {
  getAdminWorkRecords, workErrorLocation, workErrorState, workErrorTime, workErrorUrl, workRecordScope, workRecordScopeUrl, WORK_ERROR_MAX_OFFSET,
  type WorkRecordQueue, type WorkErrorRecord, type WorkRecordScope,
} from "@/lib/admin-work-errors";
import { int } from "./console-kit";
import { useAdminSnapshot } from "./use-admin-snapshot";
import styles from "./work-error-investigation.module.css";

interface Props { queue: WorkRecordQueue; refreshVersion?: number; onSelectionChange?: () => void; embedded?: boolean }
function initialLocation(queue: WorkRecordQueue) {
  const location = typeof window === "undefined" ? { record: null, offset: 0 } : workErrorLocation(window.location.href, queue);
  return { offset: location.offset, selected: location.record, filter: location.record, scope: workRecordScope(typeof window === "undefined" ? "/admin" : window.location.href, queue) };
}

export function WorkErrorInvestigation({ queue, refreshVersion = 0, onSelectionChange, embedded = false }: Props) {
  const entity = queue === "entity_refresh";
  const noun = entity ? "errors" : "records";
  const heading = entity ? "Errors" : "Records";
  const Heading = embedded ? "h3" : "h2";
  const [location, setLocation] = useState(() => initialLocation(queue));
  const [copied, setCopied] = useState<string | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);
  const [unsupported, setUnsupported] = useState(false);
  const load = useCallback(async (signal: AbortSignal) => {
    setUnsupported(false);
    try { return await getAdminWorkRecords(queue, location.scope, location.offset, location.filter, signal); }
    catch (error) {
      if (!signal.aborted && error instanceof ApiError && error.status === 404) setUnsupported(true);
      throw error;
    }
  }, [queue, location.scope, location.offset, location.filter]);
  const snapshot = useAdminSnapshot(load, refreshVersion);
  // Refreshes and failed reads never present a previous result as current evidence.
  const page = snapshot.failed || snapshot.authorizationDenied || snapshot.loading ? null : snapshot.data;

  useEffect(() => {
    const read = () => { setLocation(initialLocation(queue)); setCopied(null); setCopyFailed(false); };
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [queue]);

  const navigate = (next: typeof location) => {
    setLocation(next); setCopied(null); setCopyFailed(false);
    const base = next.scope !== location.scope ? workRecordScopeUrl(window.location.href, next.scope) : window.location.href;
    const href = workErrorUrl(base, next.selected, next.offset);
    if (href !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(window.history.state, "", href);
    onSelectionChange?.();
  };
  const setScope = (scope: WorkRecordScope) => navigate({ offset: 0, selected: null, filter: null, scope });
  const allErrors = () => navigate({ ...location, selected: null, filter: null });
  const select = (record: WorkErrorRecord) => {
    if (record.record_id === location.selected && location.filter) allErrors();
    else navigate({ ...location, selected: record.record_id === location.selected ? null : record.record_id });
  };
  const copyReference = async (record: WorkErrorRecord) => {
    try { await navigator.clipboard.writeText(record.record_id); setCopied(record.record_id); setCopyFailed(false); }
    catch { setCopied(null); setCopyFailed(true); }
  };

  return <section className={`${styles.section} ${embedded ? styles.embedded : ""}`} id="operations-errors" aria-label={heading} tabIndex={-1}>
    <div className={styles.heading}>
      <Heading id="operations-errors-title">{heading}{page ? <span>{int(page.total)}{location.filter ? " matching" : " recorded"}</span> : null}</Heading>
      <button type="button" aria-label={`Refresh ${noun}`} title={`Refresh ${noun}`} onClick={() => void snapshot.refresh()}><RefreshCw aria-hidden="true" /></button>
    </div>
    {!entity ? <div className={styles.scopes} role="group" aria-label="Record filter">
      <button type="button" aria-pressed={location.scope === "pending"} onClick={() => setScope("pending")}>Pending</button>
      <button type="button" aria-pressed={location.scope !== "pending"} onClick={() => setScope(queue === "notifications" ? "failed" : "errors")}>{queue === "notifications" ? "Failed history" : "With errors"}</button>
    </div> : null}
    <p className={styles.scope}>{entity ? "Profiles with failed or blocked source evidence, including those not due for refresh."
      : queue === "notifications" ? location.scope === "failed" ? "Retained terminal failures, including quarantined work. These records are no longer pending." : "Pending notification work, including audit records acknowledged without sending a message."
      : location.scope === "errors" ? "Pending requests with a recorded start error." : "Saved requests awaiting confirmed workflow acceptance."}</p>
    {location.filter ? <div className={styles.filter}><span>Selected reference</span><button type="button" onClick={allErrors}>All {noun}</button></div> : null}
    {snapshot.failed ? <div className={styles.readError} role="alert"><p>{snapshot.authorizationDenied ? `${heading} are unavailable for this operator session.` : unsupported ? `This backend does not expose individual ${noun} yet.` : `${heading} could not be loaded. Previous results are hidden.`}</p><button type="button" onClick={() => void snapshot.refresh()}>Retry {noun}</button></div>
      : !page ? <p className={styles.empty} role="status">Loading {entity ? "error " : ""}records…</p>
      : !page.items.length ? <div className={styles.empty}><p>{location.filter ? "This reference has no matching record in the selected view." : page.total ? `No ${noun} on this page. The recorded results may have changed.` : entity ? "No recorded errors in this snapshot." : "No records in this view."}</p>{location.offset > 0 && !location.filter ? <button type="button" onClick={() => navigate({ ...location, offset: 0, selected: null, filter: null })}>First page</button> : null}</div>
      : <>
        <ul className={styles.list}>{page.items.map(record => {
          const expanded = record.record_id === location.selected;
          const state = workErrorState(record.state, queue);
          return <li key={record.record_id}>
            <button type="button" className={styles.recordButton} aria-label={`Inspect ${entity ? "error" : "record"} ${record.label} ${record.record_id.slice(-8)}`} aria-expanded={expanded} aria-controls={`error-${record.record_id}`} onClick={() => select(record)}>
              <span><strong>{record.label} <code>…{record.record_id.slice(-8)}</code></strong>{!expanded && record.error_code ? <small>{record.error_summary || "An error is recorded; copy the reference to inspect logs."}</small> : null}<em>{state.label}</em></span><ChevronDown aria-hidden="true" />
            </button>
            {expanded ? <div className={styles.recordDetail} id={`error-${record.record_id}`}>
              <dl className={styles.facts}>
                {record.error_code ? <><div className={styles.full}><dt>Reason</dt><dd>{record.error_summary || "Raw error text is unavailable in this view. Copy the reference to inspect logs."}</dd></div>
                <div><dt>Error code</dt><dd><code>{record.error_code}</code></dd></div></> : null}
                <div><dt>Recorded state</dt><dd>{state.label}</dd></div>
                {record.attempt_count !== null ? <div><dt>{queue === "notifications" ? "Recorded delivery failures" : "Recorded failed attempts"}</dt><dd>{int(record.attempt_count)}</dd></div> : null}
                {record.state !== "failed" ? <div><dt>{entity ? "Next refresh" : record.error_code ? "Next retry" : "Available after"}</dt><dd><time dateTime={record.next_attempt_at ?? undefined}>{workErrorTime(record.next_attempt_at)}</time></dd></div> : null}
                {record.failed_at ? <div><dt>Failed</dt><dd><time dateTime={record.failed_at}>{workErrorTime(record.failed_at)}</time></dd></div> : null}
                {record.lease_expires_at ? <div><dt>Lease expires</dt><dd><time dateTime={record.lease_expires_at}>{workErrorTime(record.lease_expires_at)}</time></dd></div> : null}
                <div><dt>Created</dt><dd><time dateTime={record.created_at ?? undefined}>{workErrorTime(record.created_at)}</time></dd></div>
                {record.last_observed_at ? <div><dt>Last observed</dt><dd><time dateTime={record.last_observed_at}>{workErrorTime(record.last_observed_at)}</time></dd></div> : null}
              </dl>
              <p className={styles.note}>{state.detail}</p>
              {record.error_code === "test_retry_fixture" ? <p className={styles.note}>Matches a known request-start integration-test message.</p> : null}
              {record.attempt_count !== null ? <details className={styles.note}><summary>Attempt measurement</summary><p>{queue === "notifications" ? "Counts recorded materialization or delivery failures. Contention and quarantine may not consume an attempt." : "Counts recorded start failures, including the first failed attempt. The failure timestamp and full attempt history are not recorded here."}</p></details> : null}
              {record.error_code === "unclassified" ? <p className={styles.note}>Copy the reference to correlate this record with worker logs.</p> : null}
              {record.sources.length ? <div className={styles.sources}><h4>Source errors</h4>{record.sources.map(source => <div key={source.source_id} className={styles.source}>
                <h5>{source.provider_key}<span>{source.status}</span></h5><p>{source.error_summary || "Raw error text is unavailable in this view."}</p>
                <dl className={styles.facts}>
                  {source.error_code ? <div><dt>Error code</dt><dd><code>{source.error_code}</code></dd></div> : null}
                  <div><dt>Observed</dt><dd>{workErrorTime(source.observed_at)}</dd></div>
                  <div><dt>Next refresh</dt><dd>{workErrorTime(source.next_refresh_at)}</dd></div>
                </dl>
              </div>)}</div> : null}
              <div className={styles.reference}><span>{queue === "request_start" ? "Request reference" : entity ? "Entity reference" : "Notification reference"}</span><code>{record.record_id}</code><button type="button" onClick={() => void copyReference(record)}>{copied === record.record_id ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}{copied === record.record_id ? "Reference copied" : "Copy reference"}</button>{copyFailed ? <span role="status">Copy unavailable. Select the reference above.</span> : null}</div>
            </div> : null}
          </li>;
        })}</ul>
        {!location.filter ? <div className={styles.pager}><span>{page.offset + 1}–{page.offset + page.items.length} of {int(page.total)}</span><button type="button" aria-label={`Previous ${noun}`} disabled={page.offset === 0} onClick={() => navigate({ ...location, offset: Math.max(0, page.offset - page.limit), selected: null, filter: null })}><ChevronLeft aria-hidden="true" /></button><button type="button" aria-label={`Next ${noun}`} disabled={page.offset + page.items.length >= page.total || page.offset + page.limit > WORK_ERROR_MAX_OFFSET} onClick={() => navigate({ ...location, offset: page.offset + page.limit, selected: null, filter: null })}><ChevronRight aria-hidden="true" /></button></div> : null}
        {!location.filter && page.offset + page.limit > WORK_ERROR_MAX_OFFSET && page.offset + page.items.length < page.total ? <p className={styles.note}>This list shows the first {int(page.offset + page.items.length)} records. Use a saved inspection link to open a specific reference.</p> : null}
        <p className={styles.snapshot}>{entity ? "Error" : "Record"} snapshot <time dateTime={page.generated_at}>{workErrorTime(page.generated_at)}</time></p>
      </>}
  </section>;
}
