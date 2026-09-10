"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api";
import { getAdminCommandDetail, getAdminCommandInvestigation } from "@/lib/admin-api";
import { shouldPollAdminCommandDetail } from "@/lib/admin-command-polling";
import { codeRevisionLabel, codeSourcePath, commandWaitExplanation, mergeCommandInvestigation, commandActivityMetrics, currentTaskObservation, elapsedAt, isCommandError, COMMAND_PANELS, commandPanelForKey, type CommandPanel } from "@/lib/admin-command-investigation";
import { startAdminPolling } from "@/lib/admin-polling";
import type { AdminCommandSelection } from "@/lib/admin-history";
import type { AdminCommandDetail, AdminCommandInvestigation, AdminCommandTask } from "@/lib/admin-types";
import { Action, Chip, LoadState, Segment, age, int, kit, ms, stamp } from "./console-kit";

import styles from "./command-investigation.module.css";

interface Snapshot { detail: AdminCommandDetail; investigation: AdminCommandInvestigation | null }

function snapshotHasActiveWork(snapshot: Snapshot): boolean {
  return shouldPollAdminCommandDetail(snapshot.detail, snapshot.detail.command.status)
    || Boolean(snapshot.investigation?.plan.tasks.some((task) => task.status === "running" || task.run?.status === "running"));
}

export function CommandInvestigation({ selection, onSelect, onOpenSource, onSettled, refreshVersion }: {
  selection: AdminCommandSelection;
  onSelect: (selection: AdminCommandSelection | undefined) => void;
  onOpenSource: (key: string) => void;
  onSettled: () => void;
  refreshVersion: number;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const snapshotRef = useRef<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const [activityFilter, setActivityFilter] = useState<"all" | "errors">("all");
  const [sourceQuery, setSourceQuery] = useState("");
  const [panel, setPanel] = useState<CommandPanel>(selection.sourceKey ? "execution" : "overview");
  useEffect(() => { if (selection.sourceKey) setPanel("execution"); }, [selection.sourceKey]);
  const [settled, setSettled] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const forceRefresh = useRef(false);
  const legacy = useRef(false);
  const tail = useRef<HTMLDivElement>(null);
  const previousSource = useRef(selection.sourceKey);
  const previousRefreshVersion = useRef(refreshVersion);
  useEffect(() => {
    const sourceChanged = previousSource.current !== selection.sourceKey;
    previousSource.current = selection.sourceKey;
    const externalRefresh = previousRefreshVersion.current !== refreshVersion;
    previousRefreshVersion.current = refreshVersion;
    if (!follow && !forceRefresh.current && !sourceChanged && !externalRefresh) return;
    forceRefresh.current = false;
    return startAdminPolling({
      isVisible: () => document.visibilityState !== "hidden",
      retryError: (failure) => follow && !(failure instanceof ApiError && failure.status < 500),
      load: async (signal): Promise<Snapshot> => {
        const [detail, investigation] = await Promise.all([
          getAdminCommandDetail(selection.commandId, signal),
          legacy.current ? Promise.resolve(null) : getAdminCommandInvestigation(
            selection.commandId, snapshotRef.current?.investigation?.next_event_id, signal, selection.sourceKey,
          ).catch((failure) => {
            if (failure instanceof ApiError && failure.status === 404) { legacy.current = true; return null; }
            throw failure;
          }),
        ]);
        return { detail, investigation: investigation
          ? mergeCommandInvestigation(snapshotRef.current?.investigation ?? null, investigation) : null };
      },
      shouldContinue: (next) => follow && (snapshotHasActiveWork(next) || Boolean(next.investigation?.has_more)),
      onValue: (next) => {
        const active = snapshotHasActiveWork(next);
        const previous = snapshotRef.current;
        snapshotRef.current = next;
        setSnapshot(next);
        setError(null);
        setSettled(!active && !next.investigation?.has_more);
        if (previous && snapshotHasActiveWork(previous) && !active) onSettled();
      },
      onError: (failure) => {
        if (failure instanceof ApiError && (failure.status === 401 || failure.status === 403)) {
          snapshotRef.current = null;
          setSnapshot(null);
          setError("Operator access is unavailable. Refresh after access is restored.");
        } else setError("Live update unavailable. The last successful snapshot is retained.");
      },
    });
  }, [selection.commandId, selection.sourceKey, follow, refresh, refreshVersion, onSettled]);
  const investigation = snapshot?.investigation;
  const lastEventId = investigation?.events.at(-1)?.event_id;
  useEffect(() => {
    if (follow && tail.current) tail.current.scrollTop = tail.current.scrollHeight;
  }, [follow, lastEventId, panel]);
  const refreshNow = () => { legacy.current = false; forceRefresh.current = true; setRefresh((n) => n + 1); };
  if (!snapshot) return <InvestigationFrame commandId={selection.commandId} onClose={() => onSelect(undefined)}>
    {error ? <p role="status">{error}</p> : null}
    <LoadState failed={Boolean(error)} onRetry={refreshNow} label="Reading command evidence…" />
  </InvestigationFrame>;
  const { detail } = snapshot;
  const command = detail.command;
  const attempts = investigation?.attempts ?? [];
  const currentAttempt = attempts.find((attempt) => attempt.attempt_count === command.attempt_count);
  const selectedAttempt = selection.attempt === undefined ? undefined
    : attempts.find((attempt) => attempt.attempt_count === selection.attempt);
  const tasks = investigation?.plan.tasks ?? [];
  const leasedTasks = tasks.filter((task) => task.status === "running" && task.lease_state === "live");
  const expiredTasks = tasks.filter((task) => task.lease_state === "expired");
  const selectedTask = tasks.find((task) => task.source_key === selection.sourceKey && (!selection.runKey || task.run_key === selection.runKey));
  const events = (investigation?.events ?? []).filter((event) =>
    (selection.attempt === undefined || event.command_attempt === selection.attempt)
    && (!selection.sourceKey || event.source_key === selection.sourceKey)
    && (!selection.runKey || event.run_key === selection.runKey));
  const eventMetrics = commandActivityMetrics(events);
  const currentObservation = currentTaskObservation(tasks, investigation?.events ?? []);
  const lastProgressAt = [currentAttempt?.last_progress_at, ...tasks.map((task) => task.last_progress_at)]
    .filter((value): value is string => Boolean(value)).sort().at(-1);
  const visibleEvents = events.filter((event) => activityFilter === "all" || isCommandError(event));
  const sourceName = (key: string) => detail.runs.find((run) => run.source_key === key)?.display_name ?? key;
  const matchedTasks = tasks.filter((task) => `${task.source_key} ${sourceName(task.source_key)}`.toLowerCase().includes(sourceQuery.toLowerCase()));
  const output = tasks.filter((task) => ["succeeded", "already_succeeded"].includes(task.status) && task.canonical_count !== null);
  return <InvestigationFrame commandId={selection.commandId} command={command} onClose={() => onSelect(undefined)}>
    <div className={styles.investigation}>
    <div className={kit.toolbar}>
      {error || !follow || !settled ? <Chip tone={error ? "warn" : "neutral"}>{error ? "Stale snapshot" : !follow ? "Following paused" : "Following live updates"}</Chip> : null}
      <span>Snapshot {age(investigation?.generated_at ?? detail.generated_at)}</span>
      <span className={kit.spacer} />
      <Action onClick={() => setFollow((value) => !value)}>{follow ? "Pause following" : "Follow live"}</Action>
    </div>
    {error ? <p role="status" className={kit.cap}>{error}</p> : null}
    <div className={kit.toolbar}>
      <label>Command attempt <select aria-label="Command attempt" className={kit.select} value={selection.attempt ?? ""} onChange={(event) => onSelect({ ...selection, attempt: event.target.value ? Number(event.target.value) : undefined })}>
        <option value="">All recorded attempts</option>
        {selection.attempt && !selectedAttempt ? <option value={selection.attempt}>#{selection.attempt} · history unavailable</option> : null}
        {attempts.map((attempt) => <option key={attempt.attempt_count} value={attempt.attempt_count}>#{attempt.attempt_count} · {attempt.kind}</option>)}
      </select></label>
      {investigation?.attempts_truncated ? <span className={kit.cap}>Newest {attempts.length} of {investigation.attempts_total} recorded claims</span> : null}
      {selection.sourceKey ? <Action onClick={() => onSelect({ commandId: selection.commandId, attempt: selection.attempt })}>All sources</Action> : null}
    </div>
    <div className={kit.inspectorTabs} role="tablist" aria-label="Command investigation views">
      {COMMAND_PANELS.map((item) => <button type="button" role="tab" key={item} id={`command-tab-${item}`}
        aria-selected={panel === item} aria-controls={`command-panel-${item}`} tabIndex={panel === item ? 0 : -1}
        onClick={() => setPanel(item)} onKeyDown={(event) => {
          const next = commandPanelForKey(item, event.key);
          if (next) { event.preventDefault(); setPanel(next); document.getElementById(`command-tab-${next}`)?.focus(); }
        }}>{item[0].toUpperCase() + item.slice(1)}</button>)}
    </div>
    <div role="tabpanel" id="command-panel-overview" aria-labelledby="command-tab-overview" hidden={panel !== "overview"} className={styles.panel}>
    <div className={styles.current} aria-label="Current command state">
      {currentObservation || command.status === "queued" || command.status === "running" ? <h3>{currentObservation ? `${sourceName(currentObservation.source_key!)} · ${currentObservation.stage?.replaceAll("_", " ")}` : command.status === "queued" ? "Waiting for a dispatch claim" : "Awaiting the next progress observation"}</h3> : null}
      {currentObservation ? <p>Latest stage evidence: {currentObservation.event_code.replaceAll("_", " ")} · {stamp(currentObservation.observed_at)}{currentObservation.outcome_code ? ` · ${currentObservation.outcome_code}` : ""}.</p> : null}
      <p>{commandWaitExplanation(command.status, command.available_at, Date.parse(detail.generated_at))}</p>
      {investigation ? <p className={kit.cap}>{leasedTasks.length} live task leases · {expiredTasks.length} expired · {tasks.filter((task) => task.status === "pending").length} waiting for dispatch · {tasks.filter((task) => task.status === "queued").length} handed to workflows.</p> : null}
    </div>
    <dl className={`${kit.facts} ${styles.overviewFacts}`}>
      <div><dt>Total elapsed</dt><dd>{ms(elapsedAt(command.requested_at, command.completed_at ?? detail.generated_at))}</dd><small>Since acceptance, including waiting</small></div>
      <div><dt>Current claim elapsed</dt><dd>{ms(elapsedAt(currentAttempt?.claimed_at, currentAttempt?.ended_at ?? detail.generated_at))}</dd><small>Command claim #{command.attempt_count}</small></div>
      <div><dt>Last recorded progress</dt><dd>{lastProgressAt ? age(lastProgressAt) : "Not recorded"}</dd><small>Separate from a lease heartbeat</small></div>
      <div><dt>Canonical output</dt><dd>{output.length ? int(output.reduce((sum, task) => sum + task.canonical_count!, 0)) : "Not recorded"}</dd><small>{output.length} successful outcomes · may overlap</small></div>
    </dl>
    {!investigation ? <p className={kit.cap}>This backend provides receipt evidence only. Linked rows below belong to its latest command attempt; earlier attempt history, progress heartbeats and the activity stream are unavailable.</p> : <p className={kit.cap}>Plan: {investigation.plan.status} · created {stamp(investigation.plan.created_at)} · {tasks.length} sources. Plan rows show the current snapshot; the attempt selector filters recorded activity. Events recorded since {stamp(investigation.evidence.events_since)}; history may be incomplete.</p>}
    <div className={kit.toolbar}><h3>Source executions</h3><span className={kit.spacer} />
      <input className={kit.search} aria-label="Find a command source" placeholder="Find a source…" value={sourceQuery} onChange={(event) => setSourceQuery(event.target.value)} />
    </div>
    <div className={styles.sources} aria-label="Command source executions">
      {tasks.length ? matchedTasks.map((task) => <button type="button" key={`${task.source_key}:${task.run_key}`}
        className={styles.source} aria-pressed={selectedTask === task}
        onClick={() => { forceRefresh.current = true; setPanel("execution"); onSelect({ ...selection, sourceKey: task.source_key, runKey: task.run_key }); }}>
        <strong>{sourceName(task.source_key)}</strong><Chip tone={task.status === "failed" || task.lease_state === "expired" ? "warn" : task.status === "succeeded" ? "ok" : "neutral"}>{task.status.replaceAll("_", " ")}</Chip>
        <span>Task claim {task.attempt_count} · {task.lease_state === "not_running" ? "no task lease" : `${task.lease_state} lease`}</span>
        <span>{task.last_outcome_code ?? "No outcome yet"} · progress {task.last_progress_at ? age(task.last_progress_at) : "not recorded"}</span>
        <span>{task.status === "pending" ? "—" : task.candidate_count ?? "—"} candidates → {task.status === "pending" ? "—" : task.canonical_count ?? "—"} canonical</span>
      </button>) : detail.runs.map((run) => <button className={styles.source} key={`${run.position}:${run.source_key}`} onClick={() => onOpenSource(run.source_key)}>
        <strong>{run.display_name}</strong><Chip>{run.status}</Chip><span>{run.error_code ?? "No error recorded"} · source run claims {run.attempt_count ?? "—"}</span>
      </button>)}
    </div>
    {!tasks.length && !detail.runs.length ? <p className={kit.empty}>No source plan recorded yet.</p> : null}
    </div>
    <div role="tabpanel" id="command-panel-activity" aria-labelledby="command-tab-activity" hidden={panel !== "activity"} className={styles.panel}>
    <dl className={kit.facts}>
      <div><dt>Requests completed</dt><dd>{eventMetrics.requests === null ? "Not measured" : int(eventMetrics.requests)}</dd></div>
      <div><dt>Pages completed</dt><dd>{eventMetrics.pages === null ? "Not measured" : int(eventMetrics.pages)}</dd></div>
      <div><dt>Activity scope</dt><dd>{selection.sourceKey ? sourceName(selection.sourceKey) : "All sources"} · {selection.attempt ? `claim #${selection.attempt}` : "all recorded claims"}</dd></div>
    </dl>
    <div className={kit.toolbar}><h3>Recorded activity</h3><span className={kit.spacer} />
      <Segment label="Activity severity" options={[{ value: "all", label: "All activity" }, { value: "errors", label: "Errors" }]} value={activityFilter} onChange={setActivityFilter} />
    </div>
    <p className={kit.cap}>Structured worker events · latest 500 retained · {visibleEvents.length} match the selection. Historical coverage may be incomplete; raw process logs are not connected. {investigation?.has_more ? "Catching up with recorded events…" : ""}</p>
    <div ref={tail} className={styles.tail} aria-label="Command activity tail" tabIndex={0}>
      {visibleEvents.map((event) => <article key={event.event_id} className={styles.event}>
        <time>{stamp(event.observed_at)}</time>
        <div><strong>{event.stage?.replaceAll("_", " ") ?? "Dispatch"} · {event.event_code.replaceAll("_", " ")}</strong>
          <p>{event.source_key ? sourceName(event.source_key) : "Command"} · command claim {event.command_attempt}{event.task_attempt !== null ? ` · task claim ${event.task_attempt}` : ""}</p>
          <p>{event.outcome_code ?? event.error_type ?? "Observation"}{event.error_type && event.outcome_code ? ` · ${event.error_type}` : ""}{event.duration_ms !== null ? ` · ${ms(event.duration_ms)}` : ""}
            {event.request_count != null ? ` · ${int(event.request_count)} requests completed` : ""}{event.page_count != null ? ` · ${int(event.page_count)} pages completed` : ""}
            {event.candidate_count !== null ? ` · ${int(event.candidate_count)} candidates` : ""}{event.canonical_count !== null ? ` · ${int(event.canonical_count)} canonical` : ""}
          </p>
          {event.worker_id || event.release_revision ? <details><summary>Executing worker evidence</summary><p>{event.worker_id ?? "Worker not recorded"} · {codeRevisionLabel(event.release_revision, event.image_digest)}</p></details> : null}
        </div>
        {isCommandError(event) ? <Chip tone="bad">Error</Chip> : null}
      </article>)}
      {!visibleEvents.length ? <p className={kit.empty}>No recorded activity matches this selection.</p> : null}
    </div>
    </div>
    <div role="tabpanel" id="command-panel-execution" aria-labelledby="command-tab-execution" hidden={panel !== "execution"} className={styles.panel}>
    <details className={styles.disclosure}><summary>Dispatch lease, timing and build evidence</summary>
      <dl className={kit.facts}>
        <div><dt>Accepted</dt><dd>{stamp(command.requested_at)} · {command.requested_by ?? "actor not recorded"}</dd></div>
        <div><dt>Command claims</dt><dd>{command.attempt_count} · continuations, retries and reclaims</dd></div>
        <div><dt>Dispatch lease</dt><dd>{command.worker_state === "heartbeat_live" ? "Unexpired at snapshot" : (command.worker_state ?? "not recorded").replaceAll("_", " ")}</dd></div>
        <div><dt>Lease expires</dt><dd>{stamp(command.lease_expires_at)}</dd></div>
        <div><dt>Eligible for claim</dt><dd>{stamp(command.available_at)}</dd></div>
        <div><dt>Last dispatch heartbeat</dt><dd>{stamp(currentAttempt?.last_heartbeat_at)}</dd></div>
        <div><dt>Last dispatch progress</dt><dd>{stamp(currentAttempt?.last_progress_at)}</dd></div>
        <div><dt>Dispatch worker build</dt><dd>{codeRevisionLabel(command.executor_release_revision, command.executor_image_digest)}</dd></div>
      </dl>
      <p className={kit.cap}>Leases show ownership, not active worker capacity or progress. Dispatch build identity does not prove a Temporal activity build.</p>
    </details>
    {selectedAttempt ? <p className={kit.cap}>Claim #{selectedAttempt.attempt_count}: {selectedAttempt.kind} · claimed {stamp(selectedAttempt.claimed_at)} · ended {stamp(selectedAttempt.ended_at)} · {selectedAttempt.outcome_code ?? "no terminal outcome recorded"}. Worker {selectedAttempt.worker_id ?? "not recorded"}. {codeRevisionLabel(selectedAttempt.release_revision, selectedAttempt.image_digest)}.</p> : null}
    <div className={kit.toolbar}><label>Source execution <select className={kit.select} aria-label="Source execution" value={selectedTask?.source_key ?? ""}
      onChange={(event) => {
        const task = tasks.find((item) => item.source_key === event.target.value);
        if (task) { forceRefresh.current = true; onSelect({ ...selection, sourceKey: task.source_key, runKey: task.run_key }); }
      }}><option value="">Select a source</option>{tasks.map((task) => <option key={task.source_key} value={task.source_key}>{sourceName(task.source_key)}</option>)}</select></label></div>
    {selectedTask ? <SourceRunEvidence key={`${selection.commandId}:${selectedTask.source_key}:${selectedTask.run_key}`} task={selectedTask} onOpenSource={onOpenSource} /> : selection.sourceKey ? <p className={kit.cap}>Selected source/run is not present in this plan snapshot.</p> : null}
    {!selection.sourceKey ? <p className={kit.cap}>Select a source execution to inspect its exact run, measured stages and code references.</p> : null}
    </div>
    </div>
  </InvestigationFrame>;
}

function SourceRunEvidence({ task, onOpenSource }: { task: AdminCommandTask; onOpenSource: (key: string) => void }) {
  const run = task.run;
  return <section aria-label="Selected source run evidence" className={styles.sourceEvidence}>
    <Action onClick={() => onOpenSource(task.source_key)}>Open source configuration</Action>
    <dl className={kit.facts}>
      <div><dt>Source key</dt><dd>{task.source_key}</dd></div>
      <div><dt>Run key</dt><dd>{task.run_key}</dd></div>
      <div><dt>Source run status</dt><dd>{run?.status ?? "No run projection recorded"}</dd></div>
      <div><dt>Task / source run claims</dt><dd>{task.attempt_count} / {run?.attempt_count ?? "not recorded"}</dd></div>
      <div><dt>Started / completed</dt><dd>{stamp(task.started_at)} / {stamp(task.completed_at)}</dd></div>
      <div><dt>Retry eligible</dt><dd>{stamp(task.available_at)}</dd></div>
      <div><dt>Last task outcome</dt><dd>{task.last_outcome_code ?? "No outcome recorded"}</dd></div>
      <div><dt>Source revision</dt><dd>{run?.source_revision ?? "Not recorded"}</dd></div>
    </dl>
    <div className={kit.tableScroll}>{run?.stage_trace?.length ? <table className={kit.table}><thead><tr><th>Stage</th><th>Evidence</th><th>Duration</th><th>Latest observation</th></tr></thead><tbody>{run.stage_trace.map((stage) => <tr key={stage.stage}><td>{stage.label}</td><td>{stage.evidence_status.replaceAll("_", " ")}</td><td>{ms(stage.duration_ms)}</td><td>{stamp(stage.last_observed_at)} · {stage.last_outcome_code ?? stage.note_code ?? "—"}</td></tr>)}</tbody></table> : <p className={kit.cap}>Stage measurements are not available for this run snapshot.</p>}</div>
    {run?.resources ? <p className={kit.cap}>{int(run.resources.execution_count)} measured executions · wall {ms(run.resources.wall_time_ms)} · CPU {ms(run.resources.process_cpu_time_ms)} · boundary peak RSS {run.resources.boundary_observed_peak_rss_bytes === null ? "not measured" : `${(run.resources.boundary_observed_peak_rss_bytes / 1048576).toFixed(1)} MiB`}. Scope: {run.resources.measurement_scope.replaceAll("_", " ")} · {run.resources.measurement_quality.replaceAll("_", " ")}.</p> : null}
    {run?.execution ? <details><summary>Code ownership and execution route</summary>
      <p>{run.execution.execution_path} · {run.execution.worker_service} · queue {run.execution.task_queue ?? "none"}</p>
      <ul>{[[run.execution.adapter_module, run.execution.adapter_symbol], [run.execution.orchestration_module, run.execution.orchestration_symbol], [run.execution.worker_module, run.execution.worker_symbol]].map(([path, symbol]) => <li key={`${path}:${symbol}`}><CodeReference path={path} symbol={symbol} /></li>)}</ul>
      <p className={kit.cap}>These are ownership references from the current registry. Actual executing build identity appears only where a worker event records it; the dispatch build does not prove a Temporal activity build.</p>
    </details> : null}
  </section>;
}

function CodeReference({ path, symbol }: { path: string; symbol: string }) {
  const [copied, setCopied] = useState<string | null>(null);
  const sourcePath = codeSourcePath(path);
  const copy = async (value: string, label: string) => {
    try { await navigator.clipboard.writeText(value); setCopied(`${label} copied`); }
    catch { setCopied("Copy unavailable; select the reference text below."); }
  };
  return <div className={styles.reference}><code>{sourcePath}</code><code>{symbol}</code>
    <div className={kit.toolbar}><Action onClick={() => void copy(sourcePath, "Path")}>Copy path</Action><Action onClick={() => void copy(symbol, "Symbol")}>Copy symbol</Action>{copied ? <span role="status">{copied}</span> : null}</div>
  </div>;
}

function InvestigationFrame({ commandId, command, onClose, children }: { commandId: string; command?: AdminCommandDetail["command"]; onClose: () => void; children: ReactNode }) {
  const frame = useRef<HTMLElement>(null);
  useEffect(() => {
    if (window.matchMedia("(max-width: 1200px)").matches) {
      frame.current?.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
      frame.current?.focus({ preventScroll: true });
    }
  }, [commandId]);
  return <section ref={frame} tabIndex={-1} className={`${kit.inspector} ${styles.surface}`} aria-label="Command investigation">
    <header className={`${kit.inspectorHeader} ${styles.header}`}><div>
      <h2>{command ? command.action === "refresh_due" ? "Refresh due sources" : "Refresh source" : "Command details"}</h2>
      {command ? <Chip tone={command.status === "failed" ? "bad" : command.status === "running" ? "info" : "neutral"}>Dispatch {command.status}</Chip> : null}
      <code className={styles.commandId}>{commandId}</code></div><Action onClick={onClose}>Close</Action></header>
    <div className={styles.body}>{children}</div>
  </section>;
}
