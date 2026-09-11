"use client";

import { ArrowRight, RefreshCw } from "lucide-react";
import { useRef, useState } from "react";
import type { BackendQueue } from "@/lib/backend-operations";
import { workErrorState, workErrorTime, type WorkRecordQueue, type WorkRecordScope } from "@/lib/admin-work-errors";
import { workPreviewInsight } from "@/lib/admin-work-preview";
import { age, int, stamp } from "./console-kit";
import { useWorkRecordFeed } from "./use-work-record-feed";
import styles from "./work-preview.module.css";

interface Props {
  queue: WorkRecordQueue;
  totals?: BackendQueue;
  refreshVersion: number;
  active?: boolean;
  onInspect: (record: string | null, scope: WorkRecordScope) => void;
}

export function WorkPreview({ queue, totals, refreshVersion, active = true, onInspect }: Props) {
  const entity = queue === "entity_refresh";
  const [scope, changeScope] = useState<WorkRecordScope>(entity ? "errors" : "pending");
  const viewport = useRef<HTMLDivElement>(null);
  const snapshot = useWorkRecordFeed(queue, scope, refreshVersion, active);
  const records = snapshot.loading || snapshot.failed || snapshot.authorizationDenied || snapshot.total === null ? null : { ...snapshot, total: snapshot.total };
  const setScope = (next: WorkRecordScope) => {
    if (scope === next) return;
    changeScope(next);
    if (viewport.current) viewport.current.scrollTop = 0;
  };
  const refresh = () => {
    if (viewport.current) viewport.current.scrollTop = 0;
    void snapshot.refresh();
  };
  const errorScope = queue === "notifications" ? "failed" : "errors";
  const count = (value: number | undefined) => value === undefined ? "Unknown" : int(value);
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
        <button type="button" className={styles.refresh} aria-label="Refresh preview" disabled={snapshot.loading} onClick={refresh}><RefreshCw aria-hidden="true" /></button>
      </div>
      <div ref={viewport} className={styles.viewport} role="region" aria-label="Work records" tabIndex={0}
        onScroll={event => {
          const list = event.currentTarget;
          if (active && list.scrollHeight - list.scrollTop - list.clientHeight < 100) void snapshot.loadMore();
        }}>
      {snapshot.failed ? <div className={styles.unavailable} role="alert"><p>{snapshot.authorizationDenied ? "Records are unavailable for this operator session." : "Record preview could not be loaded."}</p><button type="button" onClick={() => void snapshot.refresh()}>Retry preview</button></div>
        : !records ? <p className={styles.empty} role="status">Loading record preview…</p>
          : !records.items.length ? <p className={styles.empty}>{entity ? "No profiles with source errors in this snapshot." : scope === "failed" ? "No retained failures in this snapshot." : scope === "errors" ? "No pending requests with errors in this snapshot." : "No pending records in this snapshot."}</p>
            : <ul className={styles.list}>{records.items.map(record => {
              const state = workErrorState(record.state, queue);
              const reason = entity ? record.sources[0]?.error_summary || record.error_summary : record.error_summary;
              const when = record.state === "failed" ? record.failed_at : record.next_attempt_at;
              return <li key={record.record_id}><button type="button" onClick={() => onInspect(record.record_id, scope)} aria-label={`Open ${record.label} ${record.record_id.slice(-8)}`}>
                <div className={styles.recordTitle}><strong title={record.label}>{record.label}</strong><span data-state={record.state} title={state.detail}>{state.label}</span><ArrowRight aria-hidden="true" /></div>
                {reason ? <p className={styles.reason} title={reason}>{reason}</p> : null}
                <div className={styles.recordMeta}><span>{record.state === "failed" ? "Failed" : entity ? "Next refresh" : record.error_code ? "Retry after" : "Available after"} <time dateTime={when ?? undefined}>{workErrorTime(when ?? null)}</time></span>
                  {record.attempt_count !== null && record.attempt_count > 0 ? <span>{int(record.attempt_count)} recorded {record.attempt_count === 1 ? "failure" : "failures"}</span> : null}
                  {entity && record.sources.length > 1 ? <span>{int(record.sources.length)} affected sources</span> : null}
                  <code>…{record.record_id.slice(-8)}</code></div>
              </button></li>;
            })}</ul>}
        {snapshot.moreFailed ? <div className={styles.loadMore} role="alert"><span>More records could not be loaded.</span><button type="button" onClick={() => void snapshot.loadMore(true)}>Retry loading more</button></div>
          : snapshot.hasMore ? <div className={styles.loadMore}><button type="button" disabled={snapshot.loadingMore} onClick={() => void snapshot.loadMore()}>{snapshot.loadingMore ? "Loading more…" : "Load more records"}</button></div> : null}
      </div>
      <div className={styles.footer}>
        <span role="status">{records ? records.items.length > records.total
          ? `${int(records.items.length)} loaded · latest count ${int(records.total)} · refresh for current records`
          : `${int(records.items.length)} of ${int(records.total)} loaded · oldest created first` : "3 rows visible"}</span>
        {records && snapshot.hasMore ? <span>Scroll for more</span> : null}
      </div>
      {records ? <p className={styles.snapshot} title={stamp(records.generatedAt)}>Records read {age(records.generatedAt)}{queue === "notifications" && scope === "pending" ? " · Includes audit work that does not send a message." : ""}</p> : null}
    </div>
  </div>;
}
