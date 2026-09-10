"use client";

import { ChevronDown, ChevronRight, Play, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";

import type { AdminCommand, CommandStatus } from "@/lib/admin-types";
import type { AdminCommandSelection } from "@/lib/admin-history";
import { CommandInvestigation } from "./command-investigation";
import { Action, Chip, LoadState, PageHead, int, kit, stamp, type Tone } from "./console-kit";
import styles from "./commands-view.module.css";

type Filter = "all" | CommandStatus;
const FILTERS: ReadonlyArray<{ value: Filter; label: string }> = [
  { value: "all", label: "All statuses" }, { value: "running", label: "Running" },
  { value: "queued", label: "Queued" }, { value: "completed", label: "Dispatch completed" },
  { value: "failed", label: "Failed" },
];
function statusTone(status: string): Tone {
  return status === "failed" ? "bad" : status === "running" ? "info" : "neutral";
}

export interface CommandsViewProps {
  selection?: AdminCommandSelection;
  onSelect: (selection: AdminCommandSelection | undefined) => void;
  canRefresh: boolean;
  commands: AdminCommand[];
  loading: boolean;
  error: string | null;
  onReload: () => void;
  refreshVersion: number;
  onOpenSource: (sourceKey: string) => void;
  onRefreshDue: () => void;
  refreshSubmitting: boolean;
  refreshDuePending: boolean;
}

export function CommandsView({
  canRefresh, selection, onSelect,
  commands, loading, error, onReload, refreshVersion,
  onOpenSource, onRefreshDue, refreshSubmitting, refreshDuePending,
}: CommandsViewProps): React.JSX.Element {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [queueExpanded, setQueueExpanded] = useState(false);
  const rows = useMemo(() => commands.filter((command) =>
    (filter === "all" || command.status === filter)
    && `${command.command_id} ${command.action} ${command.source_key ?? "due sources"}`.toLowerCase().includes(query.toLowerCase())), [commands, filter, query]);
  if (loading && !commands.length && !selection) return <LoadState failed={false} onRetry={onReload} label="Reading commands…" />;
  return <div className={kit.page}>
    <PageHead eyebrow="Operations" title="Commands" sub="Durable dispatch and source execution."
      actions={<>
        <Action onClick={onReload} disabled={loading}><RefreshCw aria-hidden="true" />Refresh</Action>
        <Action onClick={onRefreshDue} disabled={!canRefresh || refreshSubmitting || refreshDuePending}>
          <Play aria-hidden="true" />{refreshDuePending ? "Dispatch pending" : "Run due sources"}
        </Action>
      </>} />
    <div className={styles.workspace}>
      <aside className={styles.queue} aria-label="Command queue" data-collapsed={Boolean(selection) && !queueExpanded}>
        <div className={styles.queueHeader}>
          <div><h2>Recent commands</h2><span>{int(rows.length)} of {int(commands.length)} · newest 100</span></div>
          {selection ? <button className={styles.queueToggle} type="button" onClick={() => setQueueExpanded((value) => !value)} aria-expanded={!selection || queueExpanded}>
            {selection && !queueExpanded ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}{selection && !queueExpanded ? "Show queue" : "Hide queue"}
          </button> : null}
        </div>
        <div className={styles.queueBody}>
          <div className={styles.queueControls}>
            <input className={kit.search} aria-label="Find a command" placeholder="Find command or source…" value={query} onChange={(event) => setQuery(event.target.value)} />
            <select className={kit.select} aria-label="Command status" value={filter} onChange={(event) => setFilter(event.target.value as Filter)}>
              {FILTERS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </div>
          {error ? <p role="status" className={styles.queueNotice}>Refresh failed. These are the last received receipts.</p> : null}
          <div className={styles.queueList}>
            {rows.map((command) => <button type="button" key={command.command_id} className={styles.receipt}
              aria-pressed={selection?.commandId === command.command_id}
              onClick={() => { onSelect({ commandId: command.command_id }); setQueueExpanded(false); }}>
              <span className={styles.receiptHeading}><strong>{command.action === "refresh_due" ? "Refresh due sources" : "Refresh source"}</strong><Chip tone={statusTone(command.status)}>{command.status}</Chip></span>
              <span className={styles.receiptTarget}>{command.source_key ?? "Due-source plan"}</span>
              <span className={styles.receiptMeta}><time>{stamp(command.requested_at)}</time><code title={command.command_id}>{command.command_id.slice(0, 8)}</code></span>
              {command.error_code ? <span className={styles.receiptError}>{command.error_code.replaceAll("_", " ")}</span> : null}
            </button>)}
            {!rows.length ? <p className={kit.empty}>No recent commands match.</p> : null}
          </div>
          <p className={styles.queueNotice}>Receipt status describes dispatch; source outcomes are separate.</p>
        </div>
      </aside>
      <div className={styles.detail}>
        {selection ? <CommandInvestigation key={selection.commandId} selection={selection}
          onSelect={onSelect} onOpenSource={onOpenSource} onSettled={onReload} refreshVersion={refreshVersion} />
          : <section className={styles.empty} aria-label="Command investigation"><h2>Select a command</h2><p>Inspect its current state, recorded activity and source executions.</p></section>}
      </div>
    </div>
  </div>;
}
