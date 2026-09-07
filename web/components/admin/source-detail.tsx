"use client";

import {
  Archive,
  ArrowLeft,
  ArrowUpRight,
  ChevronRight,
  CircleAlert,
  Code2,
  Database,
  ExternalLink,
  FileClock,
  LoaderCircle,
  Power,
  RefreshCw,
  Sparkles,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { SourceCatalogTable } from "@/components/admin/source-catalog-table";
import { SourceConfigurationEditor } from "@/components/admin/source-configuration-editor";
import {
  formatDateTime,
  formatDuration,
  formatNumber,
  formatPercent,
  humanize,
  isFailedRun,
  runStatusLabel,
  safeHttpUrl,
  shortRevision,
  sourceDiagnostics,
  statusLabel,
} from "@/lib/admin-presentation";
import type { SourceDiagnostic } from "@/lib/admin-presentation";
import {
  adminSourceIsRetired,
  adminSourceLifecycleLabel,
  adminSourceRetirementDescription,
} from "@/lib/admin-source-lifecycle";
import type {
  AdminSource,
  AdminSourceConfigurationUpdate,
  AdminSourceDetail,
} from "@/lib/admin-types";

interface SourceDetailProps {
  source: AdminSource | null;
  detail: AdminSourceDetail | null;
  loading: boolean;
  error: string | null;
  policyAllowed: boolean;
  onClose: () => void;
  onOpenPipeline: (sourceKey: string) => void;
  onOpenRuns: (sourceKey: string, failedOnly?: boolean) => void;
  onOpenSource: (sourceKey: string) => void;
  onRefresh: (source: AdminSource) => void;
  refreshSubmitting: boolean;
  stateChanging: boolean;
  onSetEnabled: (source: AdminSource, enabled: boolean) => void;
  modeOptions: string[];
  onConfigurationSaved: (result: AdminSourceConfigurationUpdate) => void;
}

function statusTone(status: string): string {
  if (status === "failed" || status === "policy_blocked" || status === "review_expired") {
    return "critical";
  }
  if (status === "due" || status === "paused" || status === "unreviewed") return "warning";
  if (status === "succeeded" || status === "active" || status === "running") return "healthy";
  return "neutral";
}

export function SourceDetail({
  source,
  detail,
  loading,
  error,
  policyAllowed,
  onClose,
  onOpenPipeline,
  onOpenRuns,
  onOpenSource,
  onRefresh,
  refreshSubmitting,
  stateChanging,
  onSetEnabled,
  onConfigurationSaved,
}: SourceDetailProps) {
  const visibleSource = detail?.source ?? source;
  const [configurationEditRequest, setConfigurationEditRequest] = useState<{
    sourceKey: string;
    nonce: number;
  } | null>(null);
  const catalogRef = useRef<HTMLDivElement>(null);
  const configurationRef = useRef<HTMLElement>(null);
  const retired = Boolean(visibleSource && adminSourceIsRetired(visibleSource));
  const diagnostics = useMemo(
    () => (visibleSource ? sourceDiagnostics(visibleSource, detail) : []),
    [detail, visibleSource],
  );
  const actionableDiagnostics = retired
    ? []
    : diagnostics.filter((diagnostic) => diagnostic.tone !== "healthy");
  const safeSeed = visibleSource ? safeHttpUrl(visibleSource.seed_url) : null;
  const latest = visibleSource?.latest_run ?? null;
  const cannotRefreshReason = !visibleSource
    ? "Source details are still loading"
    : refreshSubmitting
      ? "A refresh command is being submitted"
      : retired
        ? "Retired sources cannot launch new collection commands"
      : !policyAllowed
        ? "Fleet collection policy is blocking refreshes"
        : !visibleSource.enabled
          ? "Paused sources are owner-managed"
          : visibleSource.review_status !== "reviewed"
            ? "The source needs current owner review"
            : visibleSource.effective_status === "running"
              ? "A source run is already active"
              : null;
  const latestYield = latest?.candidate_count
    && latest.canonical_count !== null
    && latest.canonical_count !== undefined
    ? latest.canonical_count / latest.candidate_count
    : null;
  const admissionValue = !policyAllowed || detail?.source.policy_blocked
    ? "Blocked"
    : retired && visibleSource
      ? adminSourceLifecycleLabel(visibleSource)
    : !visibleSource?.enabled
      ? "Paused"
      : visibleSource.review_status !== "reviewed"
        ? "Review needed"
        : "Ready";
  const admissionDetail = visibleSource?.due
    ? "Due now"
    : retired && visibleSource?.retired_at
      ? `Retired ${formatDateTime(visibleSource.retired_at)}`
    : visibleSource?.next_due_at
      ? `Next ${formatDateTime(visibleSource.next_due_at)}`
      : "Cadence not recorded";

  useEffect(() => {
    setConfigurationEditRequest(null);
  }, [visibleSource?.source_key]);

  const scrollTo = (target: { current: HTMLElement | null }) => {
    window.requestAnimationFrame(() => {
      target.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  };

  const inspectDiagnostic = (diagnostic: SourceDiagnostic) => {
    if (retired && visibleSource?.superseded_by_source_key) {
      onOpenSource(visibleSource.superseded_by_source_key);
      return;
    }
    if (diagnostic.target === "runs") {
      if (visibleSource) onOpenRuns(visibleSource.source_key, true);
      return;
    }
    if (diagnostic.target === "configuration") {
      scrollTo(configurationRef);
      return;
    }
    if (diagnostic.target === "catalog") {
      scrollTo(catalogRef);
      return;
    }
    if (visibleSource) onOpenPipeline(visibleSource.source_key);
  };

  const editConfiguration = () => {
    if (!visibleSource) return;
    setConfigurationEditRequest((current) => ({
      sourceKey: visibleSource.source_key,
      nonce: (current?.nonce ?? 0) + 1,
    }));
    scrollTo(configurationRef);
  };

  return (
    <section
      className="admin-detail admin-detail--workspace"
      aria-labelledby="admin-source-detail-title"
    >
      <div className="admin-detail__rail">
        <button className="admin-detail-back" type="button" onClick={onClose}>
          <ArrowLeft aria-hidden="true" />
          Back to sources
        </button>
      </div>

      {loading && !detail ? (
        <div className="admin-detail-loading">
          <LoaderCircle className="spin" aria-hidden="true" />
          <span>Joining source, run, and history signals…</span>
        </div>
      ) : null}

      {error && !detail ? (
        <div className="admin-detail-error">
          <CircleAlert aria-hidden="true" />
          <h2>Couldn’t load this source</h2>
          <p>{error}</p>
          <button type="button" onClick={onClose}>Back to sources</button>
        </div>
      ) : null}

      {visibleSource && (detail || !loading) ? (
          <div className="admin-detail__content">
            <header className="admin-detail-hero">
              <div className="admin-detail-hero__eyebrow">
                <span className={`admin-status admin-status--${statusTone(visibleSource.effective_status)}`}>
                  <i />
                  {retired
                    ? adminSourceLifecycleLabel(visibleSource)
                    : statusLabel(visibleSource.effective_status)}
                </span>
                <code>{visibleSource.source_key}</code>
              </div>
              <div className="admin-detail-hero__title">
                <div>
                  <h1 id="admin-source-detail-title">{visibleSource.display_name}</h1>
                  <p>
                    {visibleSource.publisher} · {humanize(visibleSource.mode)} ·{" "}
                    {humanize(visibleSource.region)}
                  </p>
                </div>
                <div className="admin-detail-actions">
                  <button
                    type="button"
                    onClick={() => onOpenRuns(visibleSource.source_key, Boolean(latest && isFailedRun(latest)))}
                  >
                    <FileClock aria-hidden="true" />
                    Runs
                  </button>
                  {retired ? null : visibleSource.review_status === "reviewed" ? (
                    <button
                      className="admin-state-button"
                      type="button"
                      role="switch"
                      aria-checked={visibleSource.enabled}
                      disabled={stateChanging}
                      title="Writes a new audited registry revision; pausing fences future refreshes but retains catalog data"
                      onClick={() => onSetEnabled(visibleSource, !visibleSource.enabled)}
                    >
                      {stateChanging
                        ? <LoaderCircle className="spin" aria-hidden="true" />
                        : <Power aria-hidden="true" />}
                      {stateChanging
                        ? visibleSource.enabled ? "Pausing…" : "Resuming…"
                        : visibleSource.enabled ? "Pause collection" : "Resume collection"}
                    </button>
                  ) : (
                    <button type="button" onClick={editConfiguration}>
                      Review config
                      <ChevronRight aria-hidden="true" />
                    </button>
                  )}
                  {safeSeed ? (
                    <a href={safeSeed} target="_blank" rel="noreferrer">
                      Source page
                      <ArrowUpRight aria-hidden="true" />
                    </a>
                  ) : null}
                  {!retired ? (
                    <button
                      className="admin-primary-button"
                      type="button"
                      disabled={Boolean(cannotRefreshReason)}
                      title={cannotRefreshReason ?? "Queue a bounded refresh"}
                      onClick={() => onRefresh(visibleSource)}
                    >
                      {refreshSubmitting
                        ? <LoaderCircle className="spin" aria-hidden="true" />
                        : <RefreshCw aria-hidden="true" />}
                      {refreshSubmitting ? "Queueing…" : "Queue refresh"}
                    </button>
                  ) : null}
                </div>
              </div>
            </header>

            {retired ? (
              <section className="admin-source-retirement" aria-label="Source retirement">
                <Archive aria-hidden="true" />
                <div>
                  <span>{adminSourceLifecycleLabel(visibleSource)} source</span>
                  <strong>Collection is permanently retired</strong>
                  <p>{adminSourceRetirementDescription(visibleSource)}</p>
                  {visibleSource.retired_at ? (
                    <small>Retired {formatDateTime(visibleSource.retired_at)}</small>
                  ) : null}
                </div>
                {visibleSource.superseded_by_source_key ? (
                  <button
                    type="button"
                    onClick={() => onOpenSource(visibleSource.superseded_by_source_key!)}
                  >
                    <span>Open replacement</span>
                    <code>{visibleSource.superseded_by_source_key}</code>
                    <ArrowUpRight aria-hidden="true" />
                  </button>
                ) : null}
              </section>
            ) : null}

            {error ? (
              <div className="admin-inline-error">
                <CircleAlert aria-hidden="true" />
                Some live signals could not be refreshed: {error}
              </div>
            ) : null}

            {actionableDiagnostics.length ? (
              <section className="admin-detail-section">
                <div className="admin-section-heading">
                  <div>
                    <span>Diagnosis</span>
                    <h2>What needs attention</h2>
                  </div>
                  <p>Bounded operational signals only—no raw provider payloads.</p>
                </div>
                <div className="admin-diagnostic-grid">
                  {actionableDiagnostics.map((diagnostic) => (
                    <button
                      type="button"
                      className={`admin-diagnostic admin-diagnostic--${diagnostic.tone}`}
                      key={diagnostic.title}
                      onClick={() => inspectDiagnostic(diagnostic)}
                    >
                      <i aria-hidden="true" />
                      <div>
                        <strong>{diagnostic.title}</strong>
                        <p>{diagnostic.detail}</p>
                        <span>
                          {diagnostic.action}
                          <ChevronRight aria-hidden="true" />
                        </span>
                      </div>
                    </button>
                  ))}
                </div>
              </section>
            ) : null}

            {detail ? (
              <>
                <section className="admin-detail-section admin-detail-section--pipeline">
                  <div className="admin-section-heading admin-section-heading--controls">
                    <div>
                      <span>Latest run</span>
                      <h2>Collection pipeline</h2>
                    </div>
                    <button
                      className="admin-section-link"
                      type="button"
                      onClick={() => onOpenPipeline(visibleSource.source_key)}
                    >
                      View in Pipeline
                      <ChevronRight aria-hidden="true" />
                    </button>
                  </div>
                  <div className="admin-pipeline">
                    {[
                      {
                        icon: ShieldCheck,
                        label: "Admission",
                        value: admissionValue,
                        sub: admissionDetail,
                      },
                      {
                        icon: ExternalLink,
                        label: "Collect",
                        value: latest ? runStatusLabel(latest) : "No run",
                        sub: latest?.candidate_count === null || latest?.candidate_count === undefined
                          ? "No collected records"
                          : `${formatNumber(latest.candidate_count)} collected records`,
                      },
                      {
                        icon: Sparkles,
                        label: "Enrich",
                        value: "Configured",
                        sub: humanize(detail.source.mode),
                      },
                      {
                        icon: Code2,
                        label: "Normalize + dedupe",
                        value: latest?.canonical_count === null || latest?.canonical_count === undefined
                          ? "No published records"
                          : `${formatNumber(latest.canonical_count)} published records`,
                        sub: latestYield === null
                          ? "No recorded yield"
                          : `${formatPercent(latestYield)} latest yield`,
                      },
                      {
                        icon: Database,
                        label: "Current catalog",
                        value: `${formatNumber(visibleSource.event_count)} current`,
                        sub: visibleSource.enabled
                          ? `Updated ${formatDateTime(detail.summary.latest_success_at)}`
                          : retired ? "Retained after retirement" : "Retained while paused",
                      },
                    ].map(({ icon: Icon, label, value, sub }) => (
                      <div className="admin-pipeline-stage-wrap" key={label}>
                        <div className="admin-pipeline-stage admin-pipeline-stage--static">
                          <Icon aria-hidden="true" />
                          <span>{label}</span>
                          <strong>{value}</strong>
                          <small>{sub}</small>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>

                <section className="admin-detail-section admin-detail-section--latest-run">
                  <div className="admin-section-heading admin-section-heading--controls">
                    <div>
                      <span>Execution</span>
                      <h2>Latest run</h2>
                    </div>
                    <button
                      className="admin-section-link"
                      type="button"
                      onClick={() => onOpenRuns(visibleSource.source_key)}
                    >
                      View all runs
                      <ChevronRight aria-hidden="true" />
                    </button>
                  </div>
                  {latest ? (
                    <div className="admin-source-latest-run">
                      <div>
                        <span className={`admin-status admin-status--${statusTone(latest.status)}`}>
                          <i />
                          {runStatusLabel(latest)}
                        </span>
                        {latest.error ? <small>{humanize(latest.error)}</small> : null}
                      </div>
                      <dl>
                        <div><dt>Started</dt><dd>{formatDateTime(latest.started_at)}</dd></div>
                        <div>
                          <dt>Collected → published</dt>
                          <dd>{latest.candidate_count ?? "—"} → {latest.canonical_count ?? "—"}</dd>
                        </div>
                        <div><dt>Duration</dt><dd>{formatDuration(latest.duration_ms)}</dd></div>
                        <div><dt>Attempts</dt><dd>{formatNumber(latest.attempt_count)}</dd></div>
                      </dl>
                    </div>
                  ) : (
                    <p className="admin-source-latest-run__empty">No run has been recorded for this source.</p>
                  )}
                </section>

                <div ref={catalogRef}>
                  <SourceCatalogTable
                    key={`${visibleSource.source_key}:${latest?.run_key ?? "no-run"}`}
                    sourceKey={visibleSource.source_key}
                    sourceTotal={visibleSource.event_count}
                    refreshToken={latest?.run_key ?? null}
                  />
                </div>

                <section
                  className="admin-detail-section admin-detail-split"
                  ref={configurationRef}
                >
                  <div>
                    <div className="admin-section-heading">
                      <div>
                        <span>Registry</span>
                        <h2>Source configuration</h2>
                      </div>
                    </div>
                    <SourceConfigurationEditor
                      source={detail.source}
                      onSaved={onConfigurationSaved}
                      editRequest={configurationEditRequest}
                    />
                  </div>
                  <div>
                    <div className="admin-section-heading">
                      <div>
                        <span>Build</span>
                        <h2>Revision identity</h2>
                      </div>
                    </div>
                    <details className="admin-source-identity">
                      <summary>
                        <span>
                          <strong>Technical identity</strong>
                          <small>
                            {shortRevision(detail.current_build.release_revision)}
                            {" · "}registry r{detail.source.source_revision}
                          </small>
                        </span>
                        <ChevronRight aria-hidden="true" />
                      </summary>
                      <dl>
                        <div>
                          <dt>Current API release</dt>
                          <dd><code>{detail.current_build.release_revision}</code></dd>
                        </div>
                        <div>
                          <dt>Immutable image</dt>
                          <dd><code>{detail.current_build.image_digest ?? "Not recorded"}</code></dd>
                        </div>
                        <div>
                          <dt>Registry revision</dt>
                          <dd>{detail.source.source_revision}</dd>
                        </div>
                        <div>
                          <dt>Latest run revision</dt>
                          <dd>{latest?.source_revision ?? "Not recorded"}</dd>
                        </div>
                      </dl>
                    </details>
                  </div>
                </section>
              </>
            ) : null}
          </div>
      ) : null}
    </section>
  );
}
