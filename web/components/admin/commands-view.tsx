"use client";

import { Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { getAdminCommands } from "@/lib/admin-api";
import type { AdminCommand } from "@/lib/admin-types";

import {
  Action,
  Chip,
  LoadState,
  Metrics,
  PageHead,
  Section,
  Segment,
  age,
  compare,
  int,
  kit,
  ms,
  stamp,
  type MetricSpec,
  type SortDir,
  type Tone,
} from "./console-kit";

/**
 * The command queue.
 *
 * The fleet runs one refresh command at a time, so this page is really about a single shared
 * resource: who holds it, how long they have held it, and what is waiting. That framing is why
 * occupancy leads rather than a raw list.
 */

type Filter = "all" | "queued" | "running" | "completed" | "failed";

const FILTERS: ReadonlyArray<{ value: Filter; label: string }> = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "queued", label: "Queued" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
];

function statusTone(status: string): Tone {
  if (status === "completed") return "ok";
  if (status === "failed") return "bad";
  if (status === "running") return "info";
  return "warn";
}

function elapsed(from: string | null, to: string | null): number | null {
  if (!from) return null;
  const start = new Date(from).getTime();
  const end = to ? new Date(to).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  return Math.max(0, end - start);
}

export interface CommandsViewProps {
  onOpenSource: (sourceKey: string) => void;
  onRefreshDue: () => void;
  refreshSubmitting: boolean;
  refreshDuePending: boolean;
}

export function CommandsView({
  onOpenSource,
  onRefreshDue,
  refreshSubmitting,
  refreshDuePending,
}: CommandsViewProps): React.JSX.Element {
  const [commands, setCommands] = useState<AdminCommand[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [sortKey, setSortKey] = useState("requested");
  const [sortDir] = useState<SortDir>("desc");
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const page = await getAdminCommands();
      setCommands(page.items);
    } catch {
      setFailed(true);
      setCommands([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const rows = useMemo(() => {
    const list = filter === "all" ? commands : commands.filter((c) => c.status === filter);
    const pick = (c: AdminCommand): string | number | null =>
      sortKey === "requested" ? new Date(c.requested_at).getTime() : c.status;
    return [...list].sort((a, b) => compare(pick(a), pick(b), sortDir));
  }, [commands, filter, sortKey, sortDir]);

  if (loading && commands.length === 0) {
    return <LoadState failed={false} onRetry={() => void load()} label="Reading the queue…" />;
  }
  if (failed) return <LoadState failed onRetry={() => void load()} label="" />;

  const running = commands.find((c) => c.status === "running") ?? null;
  const queued = commands.filter((c) => c.status === "queued");
  const completed = commands.filter((c) => c.status === "completed");
  const failedCmds = commands.filter((c) => c.status === "failed");

  const durations = completed
    .map((c) => elapsed(c.started_at, c.completed_at))
    .filter((v): v is number => v !== null)
    .sort((a, b) => a - b);
  const median = durations.length ? durations[Math.floor(durations.length / 2)] : null;

  const metrics: MetricSpec[] = [
    {
      key: "slot",
      label: "Slot",
      value: running ? "held" : "free",
      note: running ? `${ms(elapsed(running.started_at, null))} elapsed` : "ready for work",
      tone: running ? "info" : "ok",
    },
    { key: "queued", label: "Waiting", value: int(queued.length), note: "behind the slot", tone: queued.length ? "warn" : "ok" },
    { key: "median", label: "Median hold", value: ms(median), note: `${completed.length} completed` },
    { key: "failed", label: "Failed", value: int(failedCmds.length), tone: failedCmds.length ? "bad" : "ok" },
    { key: "total", label: "Recent", value: int(commands.length), note: "newest 100" },
  ];

  return (
    <div className={kit.page}>
      <PageHead
        eyebrow="Control room / command queue"
        title="Command queue"
        sub="The fleet executes one refresh command at a time. This is who holds that slot, how long they have held it, and what is waiting behind them."
        statValue={running ? "Held" : "Free"}
        statLabel="shared slot"
      />

      <div className={kit.toolbar}>
        <Segment options={FILTERS} value={filter} onChange={setFilter} label="Status" />
        <span className={kit.spacer} />
        <Action onClick={() => void load()} disabled={loading}>
          <RefreshCw aria-hidden="true" className={loading ? "spin" : undefined} />
          {loading ? "Refreshing" : "Refresh data"}
        </Action>
        <Action onClick={onRefreshDue} disabled={refreshSubmitting || refreshDuePending} primary>
          <Play aria-hidden="true" />
          {refreshDuePending ? "Run in flight" : "Run due sources"}
        </Action>
      </div>

      <Metrics items={metrics} />

      {running ? (
        <Section title="Holding the slot" scope="fleet concurrency is 1">
          <div className={kit.tableScroll}>
            <table className={kit.table}>
              <tbody>
                <tr>
                  <td>
                    <span className={kit.rowName}>{running.action.replaceAll("_", " ")}</span>
                    <span className={kit.rowSub}>{running.command_id}</span>
                  </td>
                  <td><Chip tone="info">running</Chip></td>
                  <td className={`${kit.num} ${kit.big}`}>{ms(elapsed(running.started_at, null))}</td>
                  <td className={`${kit.num} ${kit.mono}`}>started {age(running.started_at)}</td>
                  <td className={kit.mono}>
                    {running.source_key ? (
                      <button type="button" className={kit.rowBtn} onClick={() => onOpenSource(running.source_key as string)}>
                        {running.source_key}
                      </button>
                    ) : (
                      "whole fleet"
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p className={kit.cap}>
            While this command holds the slot, every other due source waits. A long hold is not a
            failure, but it is the reason the fleet can fall behind its configured cadence.
          </p>
        </Section>
      ) : null}

      <Section title="Recent commands" scope={`${rows.length} shown`}>
        <div className={kit.tableScroll}>
          <table className={kit.table}>
            <thead>
              <tr>
                <th>Command</th>
                <th>Status</th>
                <th>Target</th>
                <th className={kit.num}>Requested</th>
                <th className={kit.num}>Waited</th>
                <th className={kit.num}>Held</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr><td colSpan={7} className={kit.empty}>No commands match this filter.</td></tr>
              ) : (
                rows.map((c) => {
                  const open = expanded === c.command_id;
                  const wait = elapsed(c.requested_at, c.started_at);
                  const hold = elapsed(c.started_at, c.completed_at);
                  return (
                    <>
                      <tr key={c.command_id}>
                        <td>
                          <span className={kit.rowName}>{c.action.replaceAll("_", " ")}</span>
                          <span className={kit.rowSub}>{c.command_id.slice(0, 8)}…</span>
                        </td>
                        <td>
                          <Chip tone={statusTone(c.status)}>{c.status}</Chip>
                          {c.error_code ? <span className={kit.rowSub}>{c.error_code.replaceAll("_", " ")}</span> : null}
                        </td>
                        <td className={kit.mono}>
                          {c.source_key ? (
                            <button type="button" className={kit.rowBtn} onClick={() => onOpenSource(c.source_key as string)}>
                              {c.source_key}
                            </button>
                          ) : (
                            <span style={{ color: "#686865" }}>whole fleet</span>
                          )}
                        </td>
                        <td className={`${kit.num} ${kit.mono}`}>
                          {age(c.requested_at)}
                          <span className={kit.rowSub}>{stamp(c.requested_at)}</span>
                        </td>
                        <td className={`${kit.num} ${kit.mono}`}>{ms(wait)}</td>
                        <td className={`${kit.num} ${kit.mono}`}>{ms(hold)}</td>
                        <td className={kit.num}>
                          <Action onClick={() => setExpanded(open ? null : c.command_id)}>
                            {open ? "Hide" : "Detail"}
                          </Action>
                        </td>
                      </tr>
                      {open ? (
                        <tr className={kit.expandRow} key={`${c.command_id}-detail`}>
                          <td colSpan={7}>
                            <div className={kit.expandInner}>
                              <div className={kit.ledger}>
                                <div>
                                  <span style={{ minWidth: "10rem", display: "inline-block" }}>requested</span>
                                  <span>{stamp(c.requested_at)}</span>
                                </div>
                                <div>
                                  <span style={{ minWidth: "10rem", display: "inline-block" }}>started</span>
                                  <span>{stamp(c.started_at)}</span>
                                </div>
                                <div>
                                  <span style={{ minWidth: "10rem", display: "inline-block" }}>completed</span>
                                  <span>{stamp(c.completed_at)}</span>
                                </div>
                                <div>
                                  <span style={{ minWidth: "10rem", display: "inline-block" }}>build</span>
                                  <span>{c.release_revision ?? "—"}</span>
                                </div>
                                {c.executor_release_revision && c.executor_release_revision !== c.release_revision ? (
                                  <div className={kit.ledgerMark}>
                                    <span style={{ minWidth: "10rem", display: "inline-block" }}>executed by</span>
                                    <span>{c.executor_release_revision} — differs from the build that requested it</span>
                                  </div>
                                ) : null}
                              </div>
                              <p className={kit.cap}>
                                Command identity is client-generated and idempotent, so an exact
                                replay returns the original receipt rather than queueing a second
                                run.
                              </p>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}
