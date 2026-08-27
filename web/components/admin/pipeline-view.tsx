"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getAdminCatalogFreshness,
  getAdminFleetSummary,
  getAdminStageSummary,
  getAdminThroughput,
} from "@/lib/admin-api";
import type {
  AdminCatalogFreshness,
  AdminFleetSummary,
  AdminStageSummary,
  AdminThroughput,
} from "@/lib/admin-types";

import {
  Action,
  Chip,
  LoadState,
  Metrics,
  PageHead,
  Section,
  Segment,
  age,
  int,
  kit,
  ms,
  type MetricSpec,
  type Tone,
} from "./console-kit";

/**
 * The pipeline view.
 *
 * Where time and records actually go. Two facts govern the design: records exist at exactly two
 * waypoints (candidates and canonicals), and two of the five declared stages can never report a
 * duration. Both are shown as what they are rather than smoothed into a five-step funnel.
 */

const WINDOWS = [
  { value: "24", label: "24h" },
  { value: "168", label: "7d" },
  { value: "720", label: "30d" },
] as const;

const FRESH_TONE: Record<string, Tone> = {
  fresh: "ok",
  aging: "warn",
  stale: "warn",
  dead: "bad",
  never: "neutral",
};

interface Snapshot {
  stages: AdminStageSummary;
  fleet: AdminFleetSummary;
  throughput: AdminThroughput;
  freshness: AdminCatalogFreshness;
}

export function PipelineView(): React.JSX.Element {
  const [windowHours, setWindowHours] = useState("168");
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [openStage, setOpenStage] = useState<string | null>(null);

  const load = useCallback(async (hours: string) => {
    setLoading(true);
    setFailed(false);
    const n = Number(hours);
    try {
      const [stages, fleet, throughput, freshness] = await Promise.all([
        getAdminStageSummary(n),
        getAdminFleetSummary(n),
        getAdminThroughput(n, n <= 24 ? 1 : 24),
        getAdminCatalogFreshness(),
      ]);
      setSnap({ stages, fleet, throughput, freshness });
    } catch {
      setFailed(true);
      setSnap(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(windowHours);
  }, [load, windowHours]);

  const stageRows = useMemo(
    () => [...(snap?.stages.stages ?? [])].sort((a, b) => a.stage_position - b.stage_position),
    [snap],
  );

  if (loading && !snap) return <LoadState failed={false} onRetry={() => void load(windowHours)} label="Reading the pipeline…" />;
  if (failed || !snap) return <LoadState failed onRetry={() => void load(windowHours)} label="" />;

  const { fleet, freshness } = snap;
  const merged = Math.max(0, fleet.candidates - fleet.canonicals);
  const yieldPct = fleet.candidates ? (fleet.canonicals / fleet.candidates) * 100 : null;
  const measured = stageRows.filter((s) => s.evidence_status === "measured");
  const folded = stageRows.filter((s) => s.evidence_status === "not_separately_instrumented");
  const dominant = [...measured].sort((a, b) => (b.pct_of_wall ?? 0) - (a.pct_of_wall ?? 0))[0];

  const metrics: MetricSpec[] = [
    { key: "collected", label: "Collected", value: int(fleet.candidates), note: "candidate records" },
    { key: "published", label: "Published", value: int(fleet.canonicals), note: "canonical events" },
    { key: "merged", label: "Merged by dedupe", value: int(merged), note: "collapsed, not lost" },
    { key: "yield", label: "Yield", value: yieldPct === null ? "—" : `${yieldPct.toFixed(1)}%`, tone: "ok" },
    { key: "zero", label: "Empty runs", value: int(fleet.zero_yield_runs), note: "succeeded, published 0", tone: fleet.zero_yield_runs ? "warn" : "ok" },
  ];

  return (
    <div className={kit.page}>
      <PageHead
        eyebrow="operations / bounded-pipeline-evidence"
        title="Collection pipeline"
        sub="Where wall time and records actually go. Records exist at two waypoints only; the stages between them are timing boundaries, not counters."
      />

      <div className={kit.toolbar}>
        <Segment options={WINDOWS} value={windowHours} onChange={setWindowHours} label="Window" />
        <span className={kit.spacer} />
        <Action onClick={() => void load(windowHours)} disabled={loading}>
          {loading ? "Refreshing" : "Refresh"}
        </Action>
      </div>

      <Metrics items={metrics} />

      <Section title="Stages" scope={`${measured.length} of ${stageRows.length} instrumented`}>
        <div className={kit.tableScroll}>
          <table className={kit.table}>
            <thead>
              <tr>
                <th>Stage</th>
                <th>Evidence</th>
                <th className={kit.num}>Runs</th>
                <th className={kit.num}>Total time</th>
                <th className={kit.num}>Avg</th>
                <th className={kit.num}>p95</th>
                <th className={kit.pairCell}>Share of wall</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {stageRows.map((s) => {
                const isFolded = s.evidence_status === "not_separately_instrumented";
                const open = openStage === s.stage;
                return (
                  <>
                    <tr key={s.stage}>
                      <td>
                        <span className={kit.rowName}>
                          {String(s.stage_position).padStart(2, "0")} · {s.stage.replaceAll("_", " ")}
                        </span>
                      </td>
                      <td>
                        {isFolded ? (
                          <Chip tone="neutral">folded</Chip>
                        ) : s.evidence_status === "measured" ? (
                          <Chip tone="ok">measured</Chip>
                        ) : (
                          <Chip tone="warn">not observed</Chip>
                        )}
                      </td>
                      <td className={`${kit.num} ${kit.mono}`}>{isFolded ? "—" : int(s.runs_with_evidence)}</td>
                      <td className={`${kit.num} ${kit.big}`}>{isFolded ? "—" : ms(s.total_ms)}</td>
                      <td className={`${kit.num} ${kit.mono}`}>{isFolded ? "—" : ms(s.avg_ms)}</td>
                      <td className={`${kit.num} ${kit.mono}`}>{isFolded ? "—" : ms(s.p95_ms)}</td>
                      <td className={kit.pairCell}>
                        {isFolded ? (
                          <span className={kit.mono} style={{ color: "#686865" }}>no duration exists</span>
                        ) : (
                          <>
                            <i className={`${kit.bar} ${kit.barTrack}`} style={{ width: "100%" }} />
                            <i className={`${kit.bar} ${kit.barAccent}`} style={{ width: `${s.pct_of_wall ?? 0}%`, marginTop: -5 }} />
                            <span className={kit.rowSub}>{s.pct_of_wall ?? 0}%</span>
                          </>
                        )}
                      </td>
                      <td className={kit.num}>
                        {isFolded ? (
                          <Action onClick={() => setOpenStage(open ? null : s.stage)}>
                            {open ? "Hide" : "Why"}
                          </Action>
                        ) : null}
                      </td>
                    </tr>
                    {open && isFolded ? (
                      <tr className={kit.expandRow} key={`${s.stage}-why`}>
                        <td colSpan={8}>
                          <p className={kit.cap} style={{ paddingTop: 12 }}>
                            This stage has never written a metrics row and never will. Its work is
                            absorbed into the boundary named <b>{s.folded_into ?? "an adjacent stage"}</b>,
                            so no duration can be attributed to it. It is drawn as a gap rather than
                            zero, because a stage that cannot be measured is a different fact from a
                            stage that measured nothing.
                          </p>
                        </td>
                      </tr>
                    ) : null}
                  </>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className={kit.cap}>
          {dominant ? (
            <><b>{dominant.stage.replaceAll("_", " ")} owns {dominant.pct_of_wall}% of wall clock.</b>{" "}</>
          ) : null}
          Optimising anything else is rounding error until that changes.
        </p>
      </Section>

      <Section title="Record flow" scope="two waypoints, one delta">
        <div className={kit.tableScroll}>
          <table className={kit.table}>
            <thead>
              <tr>
                <th>Waypoint</th>
                <th className={kit.num}>Records</th>
                <th className={kit.pairCell} />
                <th>What happens here</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><span className={kit.rowName}>Collected</span><span className={kit.rowSub}>candidate_count</span></td>
                <td className={`${kit.num} ${kit.big}`}>{int(fleet.candidates)}</td>
                <td className={kit.pairCell}>
                  <i className={`${kit.bar} ${kit.barCool}`} style={{ width: "100%" }} />
                </td>
                <td className={kit.mono}>rows the adapter extracted from the source</td>
              </tr>
              <tr>
                <td><span className={kit.rowName}>Published</span><span className={kit.rowSub}>canonical_count</span></td>
                <td className={`${kit.num} ${kit.big}`}>{int(fleet.canonicals)}</td>
                <td className={kit.pairCell}>
                  <i className={`${kit.bar} ${kit.barAccent}`} style={{ width: `${yieldPct ?? 0}%` }} />
                </td>
                <td className={kit.mono}>distinct events that survived normalize and dedupe</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className={kit.cap}>
          The {int(merged)}-record delta is <b>collapsed input, not loss</b> — dedupe is a
          many-to-one merge and publish is an upsert. There is no per-stage record count between
          these two waypoints, so none is drawn: a tapering funnel here would be an invention.
        </p>
      </Section>

      <Section title="Catalog freshness" scope="served events by age of last successful fetch">
        <div className={kit.tableScroll}>
          <table className={kit.table}>
            <thead>
              <tr>
                <th>Origin freshness</th>
                <th className={kit.num}>Sources</th>
                <th className={kit.num}>Events served</th>
                <th className={kit.num}>Share</th>
                <th className={kit.pairCell} />
              </tr>
            </thead>
            <tbody>
              {freshness.buckets.filter((b) => b.sources > 0 || b.events > 0).map((b) => (
                <tr key={b.bucket}>
                  <td><Chip tone={FRESH_TONE[b.bucket] ?? "neutral"}>{b.bucket}</Chip></td>
                  <td className={`${kit.num} ${kit.mono}`}>{b.sources}</td>
                  <td className={`${kit.num} ${kit.big}`}>{int(b.events)}</td>
                  <td className={`${kit.num} ${kit.mono}`}>{b.pct ?? 0}%</td>
                  <td className={kit.pairCell}>
                    <i className={`${kit.bar} ${b.bucket === "fresh" ? kit.barAccent : kit.barTrack}`} style={{ width: `${b.pct ?? 0}%` }} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className={kit.cap}>
          {int(freshness.total_events)} upcoming events, graded by how recently their source last
          fetched successfully. Generated {age(freshness.generated_at)}.
        </p>
      </Section>
    </div>
  );
}
