"use client";

import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { getAdminRuns } from "@/lib/admin-api";
import type { AdminRun, AdminRunFilters } from "@/lib/admin-types";

import {
  Action,
  Chip,
  LoadState,
  Metrics,
  PageHead,
  Section,
  Segment,
  SortHeader,
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
 * The run ledger.
 *
 * One row per durable refresh slot, expandable in place to its stage evidence. Expansion rather
 * than navigation because the operator is usually comparing several runs at once, and leaving
 * the page discards that comparison.
 */

const WINDOWS = [
  { value: "24", label: "24h" },
  { value: "168", label: "7d" },
  { value: "720", label: "30d" },
] as const;

type Outcome = "all" | "succeeded" | "failed" | "running" | "paused";

const OUTCOMES: ReadonlyArray<{ value: Outcome; label: string }> = [
  { value: "all", label: "All" },
  { value: "succeeded", label: "Succeeded" },
  { value: "failed", label: "Failed" },
  { value: "running", label: "Running" },
  { value: "paused", label: "Deferred" },
];

const PAGE = 100;

function outcomeTone(status: string): Tone {
  if (status === "succeeded") return "ok";
  if (status === "failed") return "bad";
  if (status === "running") return "info";
  return "warn";
}

function humanize(code: string | null): string {
  if (!code) return "—";
  return code.replaceAll("_", " ");
}

export interface RunsViewProps {
  onOpenSource: (sourceKey: string) => void;
}

export function RunsView({ onOpenSource }: RunsViewProps): React.JSX.Element {
  const [windowHours, setWindowHours] = useState("168");
  const [outcome, setOutcome] = useState<Outcome>("all");
  const [query, setQuery] = useState("");
  const [runs, setRuns] = useState<AdminRun[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [sortKey, setSortKey] = useState("started");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async (hours: string, status: Outcome) => {
    setLoading(true);
    setFailed(false);
    try {
      const filters: AdminRunFilters = {
        status: status === "all" ? "" : status,
        sourceKey: "",
        windowHours: Number(hours),
        includeFixtures: false,
      };
      // One bounded page, server-filtered. The ledger is unbounded, so it is never paged whole.
      const page = await getAdminRuns(filters, { limit: PAGE, offset: 0 });
      setRuns(page.items);
      setTotal(page.total);
    } catch {
      setFailed(true);
      setRuns([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(windowHours, outcome);
  }, [load, windowHours, outcome]);

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = needle
      ? runs.filter(
          (r) =>
            r.source_key.toLowerCase().includes(needle)
            || (r.display_name ?? "").toLowerCase().includes(needle)
            || (r.error ?? "").toLowerCase().includes(needle),
        )
      : runs;
    const pick = (r: AdminRun): string | number | null => {
      switch (sortKey) {
        case "source": return r.display_name ?? r.source_key;
        case "status": return r.status;
        case "duration": return r.duration_ms;
        case "attempts": return r.attempt_count;
        case "output": return r.canonical_count;
        default: return r.started_at ? new Date(r.started_at).getTime() : null;
      }
    };
    return [...list].sort((a, b) => compare(pick(a), pick(b), sortDir));
  }, [runs, query, sortKey, sortDir]);

  const onSort = (key: string) => {
    if (key === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir(key === "source" ? "asc" : "desc"); }
  };

  if (loading && runs.length === 0) {
    return <LoadState failed={false} onRetry={() => void load(windowHours, outcome)} label="Reading the ledger…" />;
  }
  if (failed) return <LoadState failed onRetry={() => void load(windowHours, outcome)} label="" />;

  const succeeded = runs.filter((r) => r.status === "succeeded").length;
  const failedRuns = runs.filter((r) => r.status === "failed").length;
  const retried = runs.filter((r) => r.attempt_count > 1).length;
  const published = runs.reduce((t, r) => t + (r.canonical_count ?? 0), 0);

  const metrics: MetricSpec[] = [
    { key: "loaded", label: "In window", value: int(total), note: `${runs.length} loaded` },
    { key: "succeeded", label: "Succeeded", value: int(succeeded), tone: "ok" },
    { key: "failed", label: "Failed", value: int(failedRuns), tone: failedRuns ? "bad" : "ok" },
    { key: "retried", label: "Re-claimed", value: int(retried), note: "slots claimed >1×", tone: retried ? "warn" : "ok" },
    { key: "published", label: "Published", value: int(published), note: "records in view" },
  ];

  return (
    <div className={kit.page}>
      <PageHead
        eyebrow="Evidence / source runs"
        title="Run ledger"
        sub="One row per durable refresh slot. Expand a row for its stage evidence and the resources it consumed."
        statValue={int(total)}
        statLabel="runs in window"
      />

      <div className={kit.toolbar}>
        <input className={kit.search} placeholder="Search source or error class" value={query} onChange={(e) => setQuery(e.target.value)} />
        <Segment options={WINDOWS} value={windowHours} onChange={setWindowHours} label="Window" />
        <Segment options={OUTCOMES} value={outcome} onChange={setOutcome} label="Outcome" />
        <span className={kit.spacer} />
        <Action onClick={() => void load(windowHours, outcome)} disabled={loading}>
          <RefreshCw aria-hidden="true" className={loading ? "spin" : undefined} />
          {loading ? "Refreshing" : "Refresh data"}
        </Action>
      </div>

      <Metrics items={metrics} />

      <Section title="Runs" scope={`${rows.length} shown · newest ${PAGE} in window`}>
        <div className={kit.tableScroll}>
          <table className={kit.table}>
            <thead>
              <tr>
                <SortHeader label="Source" columnKey="source" active={sortKey === "source"} dir={sortDir} onSort={onSort} />
                <SortHeader label="Outcome" columnKey="status" active={sortKey === "status"} dir={sortDir} onSort={onSort} />
                <SortHeader label="Started" columnKey="started" active={sortKey === "started"} dir={sortDir} onSort={onSort} align="right" />
                <SortHeader label="Duration" columnKey="duration" active={sortKey === "duration"} dir={sortDir} onSort={onSort} align="right" />
                <SortHeader label="Attempts" columnKey="attempts" active={sortKey === "attempts"} dir={sortDir} onSort={onSort} align="right" />
                <SortHeader label="Output" columnKey="output" active={sortKey === "output"} dir={sortDir} onSort={onSort} align="right" />
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr><td colSpan={7} className={kit.empty}>No runs match this filter in the selected window.</td></tr>
              ) : (
                rows.map((r) => {
                  const id = `${r.source_key}:${r.run_key}`;
                  const open = expanded === id;
                  return (
                    <RunRow
                      key={id}
                      run={r}
                      open={open}
                      onToggle={() => setExpanded(open ? null : id)}
                      onOpenSource={onOpenSource}
                    />
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

function RunRow({
  run,
  open,
  onToggle,
  onOpenSource,
}: {
  run: AdminRun;
  open: boolean;
  onToggle: () => void;
  onOpenSource: (key: string) => void;
}): React.JSX.Element {
  const stages = run.stage_trace ?? [];
  const measured = stages.filter((s) => s.evidence_status === "measured");
  const totalMs = measured.reduce((t, s) => t + (s.duration_ms ?? 0), 0);

  return (
    <>
      <tr>
        <td>
          <button type="button" className={kit.rowBtn} onClick={() => onOpenSource(run.source_key)}>
            {run.display_name ?? run.source_key}
          </button>
          <span className={kit.rowSub}>{run.source_key}</span>
        </td>
        <td>
          <Chip tone={outcomeTone(run.status)}>{run.status}</Chip>
          {run.error ? <span className={kit.rowSub}>{humanize(run.error)}</span> : null}
        </td>
        <td className={`${kit.num} ${kit.mono}`}>
          {age(run.started_at)}
          <span className={kit.rowSub}>{stamp(run.started_at)}</span>
        </td>
        <td className={`${kit.num} ${kit.mono}`}>{ms(run.duration_ms)}</td>
        <td className={`${kit.num} ${kit.mono}`}>
          {run.attempt_count > 1 ? <strong style={{ color: "#ffd98e" }}>{run.attempt_count}×</strong> : "1×"}
        </td>
        <td className={`${kit.num} ${kit.big}`}>
          {run.canonical_count === null ? "—" : int(run.canonical_count)}
          {run.candidate_count !== null && run.canonical_count !== null ? (
            <span className={kit.rowSub}>{int(run.candidate_count)} collected</span>
          ) : null}
        </td>
        <td className={kit.num}>
          <Action onClick={onToggle}>{open ? "Hide" : "Evidence"}</Action>
        </td>
      </tr>
      {open ? (
        <tr className={kit.expandRow}>
          <td colSpan={7}>
            <div className={kit.expandInner}>
              <div className={kit.ledger}>
                {stages.map((s) => (
                  <div key={s.stage}>
                    <span style={{ minWidth: "11rem", display: "inline-block" }}>{s.label}</span>
                    <span style={{ minWidth: "9rem", display: "inline-block" }}>
                      {s.evidence_status === "measured"
                        ? ms(s.duration_ms)
                        : s.evidence_status.replaceAll("_", " ")}
                    </span>
                    <span style={{ minWidth: "8rem", display: "inline-block" }}>
                      {s.observation_count ? `${int(s.observation_count)} obs` : "—"}
                    </span>
                    <span>{s.note_code ? s.note_code : s.last_outcome_code ?? ""}</span>
                  </div>
                ))}
              </div>
              <p className={kit.cap}>
                {measured.length} of {stages.length} stages carry measured evidence, totalling{" "}
                {ms(totalMs)}. Stages marked <b>not separately instrumented</b> are folded into the
                boundary either side and can never report a duration — that is different from a
                stage that measured zero.
                {run.resources ? (
                  <>
                    {" "}This slot recorded <b>{int(run.resources.execution_count)}</b> worker
                    executions over {ms(run.resources.wall_time_ms)} of wall time.
                  </>
                ) : null}
              </p>
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}
