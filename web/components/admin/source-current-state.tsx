"use client";

import { summarizeSourceRegistry, type SourceRegistryLens } from "@/lib/admin-source-workspace";
import { formatRelativeTime } from "@/lib/admin-presentation";
import { useSourceOperations } from "./use-source-operations";
import { int } from "./console-kit";
import styles from "./source-current-state.module.css";

export function SourceCurrentState({ includeFixtures, refreshVersion, onInspectState }: {
  includeFixtures: boolean;
  refreshVersion: number;
  onInspectState: (lens: SourceRegistryLens) => void;
}) {
  const snapshot = useSourceOperations(includeFixtures, refreshVersion);
  const page = snapshot.loading || snapshot.failed ? null : snapshot.data;
  const summary = page ? summarizeSourceRegistry(page.items) : null;
  const states = summary?.health.filter(state => state.count > 0) ?? [];
  return <section className={styles.root} aria-label="Current source collection state" aria-busy={snapshot.loading}>
    <div className={styles.heading}><h3>Current collection state</h3><span>All sources{page ? ` · read ${formatRelativeTime(page.read_at)}` : ""}</span></div>
    {summary ? <>
      <div className={styles.bar} aria-hidden="true">{states.map(state => <i key={state.lens} data-state={state.lens} style={{ flexGrow: state.count }} />)}</div>
      <div className={styles.states}>{states.map(state => <button key={state.lens} type="button" data-state={state.lens}
        aria-label={`Show ${state.label} sources (${state.count})`} onClick={() => onInspectState(state.lens)}>
        <i aria-hidden="true" /><span>{state.label}</span><strong>{int(state.count)}</strong>
      </button>)}</div>
      {summary.count === 0 ? <p>No sources in this registry.</p> : null}
      <p>Healthy: reviewed, enabled, not due, with a successful latest run. Current state only; historical health is not recorded.</p>
    </> : <div className={styles.unavailable} role="status">{snapshot.failed ? <><span>Current collection state unavailable.</span><button type="button" onClick={() => void snapshot.refresh()}>Retry collection state</button></> : "Loading current collection state…"}</div>}
  </section>;
}
