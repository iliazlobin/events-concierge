import {
  ArrowRight,
  ChevronDown,
  ChevronRight,
  Clock3,
  Cpu,
  FileCode2,
  MemoryStick,
} from "lucide-react";
import type { ReactNode } from "react";

import {
  formatDateTime,
  formatDuration,
  formatNumber,
  humanize,
} from "@/lib/admin-presentation";
import type { AdminRun, AdminRunTimelineEntry } from "@/lib/admin-types";

interface RunExecutionEvidenceProps {
  run: AdminRun;
  outcome: string;
  note: string;
  status: ReactNode;
  historical?: boolean;
  onOpenSource?: (sourceKey: string) => void;
}

function formatBytes(value: number | null): string {
  if (value === null) return "Unavailable";
  if (value < 1024) return `${formatNumber(value)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = value / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  return `${amount >= 100 ? amount.toFixed(0) : amount.toFixed(1)} ${unit}`;
}

function timelineLabel(entry: AdminRunTimelineEntry): string {
  if (entry.event_code === "command_requested") return "Command accepted";
  if (entry.event_code === "command_started") return "Worker claimed command";
  if (entry.event_code === "run_attempt_started") return "Latest recorded attempt started";
  if (entry.event_code === "execution_observed") return "Worker execution observed";
  if (entry.event_code === "run_status_observed") return "Run status observed";
  if (entry.event_code === "command_completed") return "Command completed";
  return entry.stage ? humanize(entry.stage) : "Stage observed";
}

function timelineDetail(entry: AdminRunTimelineEntry): string {
  return [
    entry.outcome_code ? humanize(entry.outcome_code) : null,
    entry.duration_ms === null ? null : formatDuration(entry.duration_ms),
    entry.observation_count === null
      ? null
      : `${formatNumber(entry.observation_count)} observation${entry.observation_count === 1 ? "" : "s"}`,
  ].filter((value): value is string => value !== null).join(" · ");
}

export function RunExecutionEvidence({
  run,
  outcome,
  note,
  status,
  historical = false,
  onOpenSource,
}: RunExecutionEvidenceProps) {
  const resources = run.resources;
  const processMetricsUnavailable = resources?.measurement_scope === "activity_wall_clock_only";
  const cpuValue = resources?.cpu_utilization_percent === null
    || resources?.cpu_utilization_percent === undefined
    ? "Unavailable"
    : `${resources.cpu_utilization_percent.toFixed(1)}% avg`;
  const memoryValue = formatBytes(resources?.boundary_observed_peak_rss_bytes ?? null);
  const outputValue = `${run.candidate_count ?? "—"} → ${run.canonical_count ?? "—"}`;
  const measuredStages = run.stage_trace.filter(
    (stage) => stage.evidence_status === "measured",
  );

  return (
    <div className={`admin-run-evidence ${historical ? "is-historical" : ""}`}>
      <div className="admin-run-evidence__heading">
        <div>
          <span>Operator-safe run evidence</span>
          <strong>{outcome}</strong>
        </div>
        {status}
      </div>
      <p>{note}</p>

      <div className="admin-run-evidence__telemetry" aria-label="Run timing and resource evidence">
        <article>
          <Clock3 aria-hidden="true" />
          <span>Elapsed</span>
          <strong>{formatDuration(run.duration_ms)}</strong>
          <small>Recorded run interval</small>
        </article>
        <article>
          <ArrowRight aria-hidden="true" />
          <span>Output</span>
          <strong>{outputValue}</strong>
          <small>Collected records to published records</small>
        </article>
        <article>
          <Cpu aria-hidden="true" />
          <span>Process CPU</span>
          <strong>{cpuValue}</strong>
          <small>{processMetricsUnavailable ? "Unavailable on a shared worker" : resources ? "Best-effort sequential-worker delta" : "Not recorded for this run"}</small>
        </article>
        <article>
          <MemoryStick aria-hidden="true" />
          <span>Observed RSS</span>
          <strong>{memoryValue}</strong>
          <small>{processMetricsUnavailable ? "Unavailable on a shared worker" : resources ? "Largest run-boundary observation" : "Not recorded for this run"}</small>
        </article>
      </div>

      {run.timeline.entries.length ? (
        <section className="admin-run-log" aria-label="Structured execution log">
          <div>
            <span>Execution log</span>
            <small>
              Retained structured evidence · incomplete · no raw worker logs or payloads
            </small>
          </div>
          <ol>
            {run.timeline.entries.map((entry, index) => {
              const detail = timelineDetail(entry);
              return (
                <li key={`${entry.observed_at}:${entry.event_code}:${entry.stage ?? index}`}>
                  <time dateTime={entry.observed_at}>{formatDateTime(entry.observed_at)}</time>
                  <span aria-hidden="true" />
                  <div>
                    <strong>{timelineLabel(entry)}</strong>
                    {detail ? <small>{detail}</small> : null}
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
      ) : (
        <div className="admin-run-log-empty">
          Structured execution log is unavailable for this legacy run.
        </div>
      )}

      <details className="admin-run-technical">
        <summary>
          <span><FileCode2 aria-hidden="true" />Technical details</span>
          <ChevronDown aria-hidden="true" />
        </summary>
        <div className="admin-run-technical__body">
          <dl>
            <div className="is-wide"><dt>Run key</dt><dd><code>{run.run_key}</code></dd></div>
            <div><dt>Trigger</dt><dd>{humanize(run.trigger)}</dd></div>
            <div><dt>Outcome code</dt><dd>{run.error ? humanize(run.error) : "None"}</dd></div>
            <div><dt>Started</dt><dd>{formatDateTime(run.started_at)}</dd></div>
            <div><dt>Completed</dt><dd>{formatDateTime(run.completed_at)}</dd></div>
            <div><dt>Attempts</dt><dd>{formatNumber(run.attempt_count)}</dd></div>
            <div><dt>Collected records</dt><dd>{run.candidate_count ?? "—"}</dd></div>
            <div><dt>Published records</dt><dd>{run.canonical_count ?? "—"}</dd></div>
            <div><dt>Source revision</dt><dd>{run.source_revision ?? "—"}</dd></div>
            <div><dt>Provenance</dt><dd>{humanize(run.provenance_status)}</dd></div>
            <div><dt>Worker release</dt><dd><code>{run.release_revision ?? "Not recorded"}</code></dd></div>
            <div><dt>Image identity</dt><dd><code>{run.image_digest ?? "Not recorded"}</code></dd></div>
          </dl>

          {run.execution ? (
            <section>
              <h4>Code ownership</h4>
              <dl>
                <div><dt>Execution path</dt><dd>{humanize(run.execution.execution_path)}</dd></div>
                <div><dt>Worker service</dt><dd>{humanize(run.execution.worker_service)}</dd></div>
                <div><dt>Task queue</dt><dd><code>{run.execution.task_queue ?? "Direct worker"}</code></dd></div>
                <div><dt>Adapter</dt><dd>{humanize(run.execution.adapter_id)}</dd></div>
                <div className="is-wide"><dt>Adapter implementation</dt><dd><code>{run.execution.adapter_module}#{run.execution.adapter_symbol}</code></dd></div>
                <div className="is-wide"><dt>Orchestration</dt><dd><code>{run.execution.orchestration_module}#{run.execution.orchestration_symbol}</code></dd></div>
                <div className="is-wide"><dt>Worker entry</dt><dd><code>{run.execution.worker_module}#{run.execution.worker_symbol}</code></dd></div>
              </dl>
            </section>
          ) : null}

          {measuredStages.length ? (
            <section>
              <h4>Stage measurements</h4>
              <dl>
                {measuredStages.map((stage) => (
                  <div key={stage.stage}>
                    <dt>{stage.label}</dt>
                    <dd>
                      {formatDuration(stage.duration_ms)} · {formatNumber(stage.observation_count)} observed
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          ) : null}

          {run.source_configuration ? (
            <section>
              <h4>Source contract at execution</h4>
              <dl>
                <div><dt>Adapter mode</dt><dd>{humanize(run.source_configuration.mode)}</dd></div>
                <div><dt>Refresh cadence</dt><dd>{formatNumber(run.source_configuration.refresh_interval_minutes)} min</dd></div>
                <div><dt>Minimum pacing</dt><dd>{formatNumber(run.source_configuration.min_interval_ms)} ms</dd></div>
                <div><dt>Request-unit cap</dt><dd>{formatNumber(run.source_configuration.page_limit)}</dd></div>
                <div><dt>Reviewed</dt><dd>{formatDateTime(run.source_configuration.reviewed_at)}</dd></div>
                <div><dt>Review expires</dt><dd>{formatDateTime(run.source_configuration.review_expires_at)}</dd></div>
              </dl>
            </section>
          ) : null}

          {resources ? (
            <section>
              <h4>Measurement contract</h4>
              <dl>
                <div><dt>Scope</dt><dd>{humanize(resources.measurement_scope)}</dd></div>
                <div><dt>Quality</dt><dd>{humanize(resources.measurement_quality)}</dd></div>
                <div><dt>Source</dt><dd><code>{resources.measurement_source}</code></dd></div>
                <div><dt>Process CPU time</dt><dd>{formatDuration(resources.process_cpu_time_ms)}</dd></div>
                <div><dt>RSS before</dt><dd>{formatBytes(resources.rss_before_bytes)}</dd></div>
                <div><dt>RSS after</dt><dd>{formatBytes(resources.rss_after_bytes)}</dd></div>
                <div><dt>Boundary peak</dt><dd>{formatBytes(resources.boundary_observed_peak_rss_bytes)}</dd></div>
                <div><dt>Process lifetime peak</dt><dd>{formatBytes(resources.process_lifetime_peak_rss_bytes)}</dd></div>
              </dl>
              <p>
                Process-lifetime peak RSS is worker-process context, not memory attributed solely
                to this run. Shared Temporal workers intentionally omit CPU and RSS deltas.
              </p>
            </section>
          ) : null}

          {run.command ? (
            <section>
              <h4>Durable command</h4>
              <dl>
                <div className="is-wide"><dt>Command ID</dt><dd><code>{run.command.command_id}</code></dd></div>
                <div><dt>Requested</dt><dd>{formatDateTime(run.command.requested_at)}</dd></div>
                <div><dt>Worker started</dt><dd>{formatDateTime(run.command.started_at)}</dd></div>
                <div><dt>Command completed</dt><dd>{formatDateTime(run.command.completed_at)}</dd></div>
              </dl>
            </section>
          ) : null}
        </div>
      </details>

      {onOpenSource ? (
        <div className="admin-run-evidence__actions">
          <button type="button" onClick={() => onOpenSource(run.source_key)}>
            Open source diagnostics
            <ChevronRight aria-hidden="true" />
          </button>
        </div>
      ) : null}
    </div>
  );
}
