"use client";

import { Check, ChevronLeft, ChevronRight, Copy, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { getAdminCommandInvestigation } from "@/lib/admin-api";
import { isCommandError } from "@/lib/admin-command-investigation";
import { runLogContext, runWorkerEvidence } from "@/lib/admin-run-failure-evidence";
import type { AdminRun } from "@/lib/admin-types";

import { ms } from "./console-kit";
import { useAdminSnapshot } from "./use-admin-snapshot";
import styles from "./run-failure-evidence.module.css";

const label = (value: string) => value.replaceAll("_", " ");
const utc = (value: string) => Number.isFinite(Date.parse(value))
  ? `${new Date(value).toISOString().slice(0, 19).replace("T", " ")} UTC` : "Timestamp unavailable";
const PAGE_SIZE = 5;
const ERROR_LABELS: Record<string, string> = {
  validation: "validation error", network: "network error", timeout: "timeout",
  rate_limited: "rate limit", access_denied: "access denial", internal: "internal error",
};
const STAGE_LABELS: Record<string, string> = {
  collect: "collection", admission: "admission", extract_enrich: "extraction / enrichment",
  normalize_dedupe: "normalization / deduplication", catalog_publish: "catalog publication",
};

export function RunFailureEvidence({ run }: { run: AdminRun }) {
  const [page, setPage] = useState(0);
  const [copied, setCopied] = useState(false);
  const load = useCallback(async (signal: AbortSignal) => {
    if (!run.command) return null;
    const snapshot = await getAdminCommandInvestigation(run.command.command_id, null, signal, run.source_key);
    if (snapshot.command_id !== run.command.command_id) throw new Error("Command evidence did not match the selected run");
    return snapshot;
  }, [run]);
  const snapshot = useAdminSnapshot(load);
  // A failed refresh does not leave a previous error looking like current evidence.
  const data = snapshot.loading || snapshot.failed || snapshot.authorizationDenied ? null : snapshot.data;
  const { events, error, earlierAttempt, unknownAttempt } = runWorkerEvidence(run, data);
  const activePage = Math.min(page, Math.max(0, Math.ceil(events.length / PAGE_SIZE) - 1));
  const pageEvents = events.slice(activePage * PAGE_SIZE, (activePage + 1) * PAGE_SIZE);
  useEffect(() => { setPage(0); setCopied(false); }, [run.source_key, run.run_key, run.command?.command_id]);
  useEffect(() => {
    if (!copied) return;
    const timeout = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(timeout);
  }, [copied]);
  const copyContext = async () => {
    try { await navigator.clipboard.writeText(runLogContext(run, error)); setCopied(true); }
    catch { setCopied(false); }
  };

  return <section className={styles.failure} aria-label="Failure details">
    <header><h3>Failure details</h3>{run.command ? <button type="button" onClick={() => void snapshot.refresh()} disabled={snapshot.loading} aria-label="Refresh failure details"><RefreshCw aria-hidden="true" /></button> : null}</header>
    <p className={styles.reason}>{error && !earlierAttempt && !unknownAttempt
      ? `Recorded ${error.error_type ? ERROR_LABELS[error.error_type] ?? label(error.error_type) : label(error.outcome_code ?? error.event_code)}${error.stage ? ` during ${STAGE_LABELS[error.stage] ?? label(error.stage)}` : ""}`
      : label(run.error ?? "Failure reason not recorded")}</p>
    {error ? <p className={styles.meta}>{earlierAttempt ? "Earlier attempt error" : "Latest recorded error"} · <time dateTime={error.observed_at} title={error.observed_at}>{utc(error.observed_at)}</time>
      {earlierAttempt || unknownAttempt ? ` · ${label(error.error_type ?? error.outcome_code ?? error.event_code)}${error.stage ? ` · ${label(error.stage)}` : ""}` : ""}
      {error.task_attempt != null ? ` · task attempt ${error.task_attempt}` : ""}</p> : null}
    {!run.command ? <p>No command is linked to this run. Worker events are unavailable.</p>
      : snapshot.authorizationDenied ? <p role="status">Access to worker events was denied.</p>
        : snapshot.failed ? <p role="status">Worker events could not be read. Retry using Refresh failure details.</p>
          : snapshot.loading && !data ? <p role="status">Reading worker events…</p>
            : !error ? <p>No error event for this run is available in the linked command’s latest 100 events.</p>
              : earlierAttempt ? <p>The retained error predates this run’s latest attempt. Its precise failure reason is unavailable.</p>
                : unknownAttempt ? <p>Missing timestamps prevent matching this error to the latest attempt.</p> : null}
    <p>Exception messages and stack traces are not retained in these events. Process logs are not connected here.</p>
    <button type="button" className={styles.copy} onClick={() => void copyContext()}>{copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}{copied ? "Copied" : "Copy log context"}</button>
    {data ? <details className={styles.events}><summary>Worker events <span>{events.length}</span></summary>
      <p className={styles.meta}>Exact run · linked command · newest first. This is retained structured evidence, not a complete process log.</p>
      {events.length ? <ol aria-label="Worker event log">{pageEvents.map((event) => <li key={event.event_id} data-error={isCommandError(event)}>
        <time dateTime={event.observed_at} title={event.observed_at}>{utc(event.observed_at)}</time>
        <strong>{label(event.event_code)}{event.stage ? ` · ${label(event.stage)}` : ""}</strong>
        <span>{[event.error_type ? label(event.error_type) : null, event.outcome_code ? label(event.outcome_code) : null, event.duration_ms != null ? ms(event.duration_ms) : null].filter(Boolean).join(" · ")}</span>
        {event.request_count != null || event.page_count != null ? <span>{[
          event.request_count != null ? `${event.request_count} requests` : null,
          event.page_count != null ? `${event.page_count} pages` : null,
        ].filter(Boolean).join(" · ")}</span> : null}
        <span>Command attempt {event.command_attempt}{event.task_attempt != null ? ` · task attempt ${event.task_attempt}` : ""}</span>
      </li>)}</ol> : <p>No exact-run events are available in this snapshot.</p>}
      {events.length > PAGE_SIZE ? <nav className={styles.pagination} aria-label="Worker event pages">
        <button type="button" disabled={activePage === 0} onClick={() => setPage(activePage - 1)} aria-label="Previous worker events"><ChevronLeft aria-hidden="true" /></button>
        <span>{activePage * PAGE_SIZE + 1}–{Math.min(events.length, (activePage + 1) * PAGE_SIZE)} of {events.length}</span>
        <button type="button" disabled={(activePage + 1) * PAGE_SIZE >= events.length} onClick={() => setPage(activePage + 1)} aria-label="Next worker events"><ChevronRight aria-hidden="true" /></button>
      </nav> : null}
      <p className={styles.meta}>Filtered from the linked command’s latest 100 events. Older events and other commands may be absent.</p>
    </details> : null}
  </section>;
}
