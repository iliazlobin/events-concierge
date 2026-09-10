"use client";

import { ArrowRight, ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { useCallback, useEffect } from "react";
import type { BackendQueue } from "@/lib/backend-operations";
import { getAdminWorkRecords, workErrorState, workErrorTime, WORK_ERROR_MAX_OFFSET, type WorkRecordQueue, type WorkRecordScope } from "@/lib/admin-work-errors";
import { WORK_PREVIEW_LIMIT, workPreviewInsight } from "@/lib/admin-work-preview";
import { age, int, stamp } from "./console-kit";
import { useAdminSnapshot } from "./use-admin-snapshot";
import styles from "./work-preview.module.css";

export interface WorkPreviewPage {
  scope: WorkRecordScope;
  offset: number;
}

interface Props {
  queue: WorkRecordQueue;
  totals?: BackendQueue;
  refreshVersion: number;
  page?: WorkPreviewPage;
  onPageChange: (page: WorkPreviewPage) => void;
  onInspect: (record: string | null, scope: WorkRecordScope) => void;
}

export function WorkPreview({ queue, totals, refreshVersion, page, onPageChange, onInspect }: Props) {
  const entity = queue === "entity_refresh";
  const { scope, offset } = page ?? { scope: entity ? "errors" : "pending", offset: 0 };
  const setScope = (scope: WorkRecordScope) => onPageChange({ scope, offset: 0 });
  const load = useCallback((signal: AbortSignal) => getAdminWorkRecords(queue, scope, offset, null, signal, WORK_PREVIEW_LIMIT), [queue, scope, offset]);
  const snapshot = useAdminSnapshot(load, refreshVersion);
  const records = snapshot.loading || snapshot.failed || snapshot.authorizationDenied ? null : snapshot.data;
  const errorScope = queue === "notifications" ? "failed" : "errors";
  const count = (value: number | undefined) => value === undefined ? "Unknown" : int(value);
  useEffect(() => {
    if (records && offset > 0 && offset >= records.total) {
      onPageChange({ scope, offset: Math.floor(Math.max(0, records.total - 1) / WORK_PREVIEW_LIMIT) * WORK_PREVIEW_LIMIT });
    }
  }, [records, offset, scope, onPageChange]);
  return <div className={styles.preview}>
    <div className={styles.insights}>
      <dl className={styles.metrics}>
        <div><dt>{entity ? "Due for refresh" : "Pending"}</dt><dd>{count(totals?.pending)}</dd></div>
        {entity ? null : <><div><dt>Ready to claim</dt><dd>{count(totals?.ready)}</dd></div><div><dt>Leases</dt><dd>{count(totals?.leased)}</dd></div></>}
        <div data-warning={Boolean(totals?.failed)}><dt>{entity ? "Profiles with errors" : queue === "notifications" ? "Failed history" : "Pending with errors"}</dt><dd>{count(totals?.failed)}</dd></div>
      </dl>
      <p className={styles.insight}>{workPreviewInsight(totals, scope)}</p>
      {totals ? <dl className={styles.evidence}>
        {!entity ? <div><dt>Oldest pending</dt><dd title={stamp(totals.oldest_pending_at)}>{totals.oldest_pending_at ? age(totals.oldest_pending_at) : "None recorded"}</dd></div> : null}
        <div><dt>{entity ? "Last refresh evidence" : "Last recorded progress"}</dt><dd title={stamp(totals.last_progress_at)}>{totals.last_progress_at ? age(totals.last_progress_at) : "Not recorded"}</dd></div>
      </dl> : null}
    </div>
    <div className={styles.records}>
      <div className={styles.toolbar}>
        {entity ? <h3>Profiles with source errors</h3> : <div className={styles.scopes} role="group" aria-label="Preview records">
          <button type="button" aria-pressed={scope === "pending"} onClick={() => setScope("pending")}>Pending</button>
          <button type="button" aria-pressed={scope === errorScope} onClick={() => setScope(errorScope)}>{queue === "notifications" ? "Failed history" : "With errors"}</button>
        </div>}
        <button type="button" className={styles.refresh} aria-label="Refresh preview" disabled={snapshot.loading} onClick={() => void snapshot.refresh()}><RefreshCw aria-hidden="true" /></button>
      </div>
      {snapshot.failed ? <div className={styles.unavailable} role="alert"><p>{snapshot.authorizationDenied ? "Records are unavailable for this operator session." : "Record preview could not be loaded."}</p><button type="button" onClick={() => void snapshot.refresh()}>Retry preview</button></div>
        : !records ? <p className={styles.empty} role="status">Loading record preview…</p>
          : !records.items.length ? <p className={styles.empty}>{entity ? "No profiles with source errors in this snapshot." : scope === "failed" ? "No retained failures in this snapshot." : scope === "errors" ? "No pending requests with errors in this snapshot." : "No pending records in this snapshot."}</p>
            : <ul className={styles.list}>{records.items.slice(0, WORK_PREVIEW_LIMIT).map(record => {
              const state = workErrorState(record.state, queue);
              const reason = entity ? record.sources[0]?.error_summary || record.error_summary : record.error_summary;
              const when = record.state === "failed" ? record.failed_at : record.next_attempt_at;
              return <li key={record.record_id}><button type="button" onClick={() => onInspect(record.record_id, scope)} aria-label={`Open ${record.label} ${record.record_id.slice(-8)}`}>
                <div className={styles.recordTitle}><strong>{record.label}</strong><span data-state={record.state} title={state.detail}>{state.label}</span><ArrowRight aria-hidden="true" /></div>
                {reason ? <p className={styles.reason}>{reason}</p> : null}
                <div className={styles.recordMeta}><span>{record.state === "failed" ? "Failed" : entity ? "Next refresh" : record.error_code ? "Retry after" : "Available after"} <time dateTime={when ?? undefined}>{workErrorTime(when ?? null)}</time></span>
                  {record.attempt_count !== null && record.attempt_count > 0 ? <span>{int(record.attempt_count)} recorded {record.attempt_count === 1 ? "failure" : "failures"}</span> : null}
                  {entity && record.sources.length > 1 ? <span>{int(record.sources.length)} affected sources</span> : null}
                  <code>…{record.record_id.slice(-8)}</code></div>
              </button></li>;
            })}</ul>}
      <div className={styles.footer}>
        <span role="status">{records ? `${records.items.length ? `${int(offset + 1)}–${int(offset + Math.min(records.items.length, WORK_PREVIEW_LIMIT))}` : "0"} of ${int(records.total)} · oldest created first` : `Page ${int(Math.floor(offset / WORK_PREVIEW_LIMIT) + 1)} · 3 per page`}</span>
        <nav className={styles.pagination} aria-label="Record pages">
          <button type="button" aria-label="Previous records" disabled={snapshot.loading || offset === 0} onClick={() => onPageChange({ scope, offset: Math.max(0, offset - WORK_PREVIEW_LIMIT) })}><ChevronLeft aria-hidden="true" />Previous</button>
          <button type="button" aria-label="Next records" disabled={!records || offset + WORK_PREVIEW_LIMIT >= records.total || offset + WORK_PREVIEW_LIMIT > WORK_ERROR_MAX_OFFSET} onClick={() => onPageChange({ scope, offset: offset + WORK_PREVIEW_LIMIT })}>Next<ChevronRight aria-hidden="true" /></button>
        </nav>
      </div>
      {records ? <p className={styles.snapshot} title={stamp(records.generated_at)}>Records read {age(records.generated_at)}{queue === "notifications" && scope === "pending" ? " · Includes audit work that does not send a message." : ""}</p> : null}
    </div>
  </div>;
}
