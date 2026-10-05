"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, readableError } from "@/lib/api";
import {
  budgetAmount, budgetState, usageFiltersFromUrl, usageFiltersUrl, usageValue, usageWindow, usd,
  type BudgetUpdate, type KeyUsage, type ModelBudget, type UsageFilters, type UsageMetric, type UsageReport, type UsageTotals,
} from "@/lib/admin-model-usage";
import { Action, Chip, Metrics, PageHead, Segment, ms } from "./console-kit";
import { useAdminSnapshot } from "./use-admin-snapshot";
import styles from "./models-view.module.css";

const metricLabels: Record<UsageMetric, string> = { cost: "Spend", tokens: "Tokens", calls: "Calls", latency: "Latency" };
const count = (value: number) => value.toLocaleString("en-US");
const utc = (value: string) => new Date(value).toLocaleString("en-US", { timeZone: "UTC", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
function metricText(row: UsageTotals, metric: UsageMetric): string {
  const value = usageValue(row, metric);
  if (metric === "cost") return `${row.unknown_cost_calls ? "≥ " : ""}${usd(row.cost_usd)}`;
  if (metric === "latency") return ms(value);
  return value === null ? "Unknown" : count(value);
}

export function ModelsView({ refreshVersion, canConfigureBudget }: { refreshVersion: number; canConfigureBudget: boolean }) {
  const [filters, setFilters] = useState<UsageFilters | null>(null);
  useEffect(() => {
    const read = () => setFilters(usageFiltersFromUrl(window.location.href));
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const change = (next: UsageFilters) => {
    window.history.pushState(window.history.state, "", usageFiltersUrl(next, window.location.href));
    setFilters(next);
  };
  if (!filters) return <p className={styles.muted}>Loading model usage…</p>;
  return <ModelsWorkspace filters={filters} onChange={change} refreshVersion={refreshVersion} canConfigureBudget={canConfigureBudget} />;
}

function ModelsWorkspace({ filters, onChange, refreshVersion, canConfigureBudget }: {
  filters: UsageFilters; onChange: (filters: UsageFilters) => void; refreshVersion: number; canConfigureBudget: boolean;
}) {
  const loadReport = useCallback((signal: AbortSignal) => {
    const interval = usageWindow(filters, new Date());
    if (!interval) return Promise.reject(new Error("Choose a valid interval of up to 90 days."));
    const query = new URLSearchParams({ start_at: interval.start, end_at: interval.end, bucket_hours: String(interval.hours) });
    if (filters.model) query.set("model", filters.model);
    return api<UsageReport>(`/admin/v1/models/usage?${query}`, { signal, cache: "no-store" });
  }, [filters.range, filters.model, filters.start, filters.end]); // Metric changes only the chart.
  const loadBudget = useCallback((signal: AbortSignal) => api<ModelBudget>("/admin/v1/models/budget", { signal, cache: "no-store" }), []);
  const loadKey = useCallback((signal: AbortSignal) => api<KeyUsage>("/admin/v1/models/key", { signal, cache: "no-store" }), []);
  const reportRead = useAdminSnapshot(loadReport, refreshVersion);
  const budgetRead = useAdminSnapshot(loadBudget, refreshVersion);
  const keyRead = useAdminSnapshot(loadKey, refreshVersion);
  const refresh = useCallback(() => { void reportRead.refresh(); void budgetRead.refresh(); void keyRead.refresh(); },
    [reportRead.refresh, budgetRead.refresh, keyRead.refresh]);
  useEffect(() => {
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  const report = !reportRead.loading && !reportRead.failed ? reportRead.data : null;
  const budget = !budgetRead.failed ? budgetRead.data : null;
  const key = !keyRead.loading && !keyRead.failed ? keyRead.data : null;
  const [dates, setDates] = useState({ start: filters.start, end: filters.end });
  const [dateError, setDateError] = useState("");
  useEffect(() => { setDates({ start: filters.start, end: filters.end }); }, [filters.start, filters.end]);
  const models = [...new Set([...(report?.model_options ?? []), ...(filters.model ? [filters.model] : [])])];
  return <div className={styles.root}>
    <PageHead eyebrow="OpenRouter" title="Models" sub="Usage, costs and application budgets."
      actions={<Action onClick={refresh}><RefreshCw size={14} />Refresh usage</Action>} />
    <div className={styles.controls}>
      <label>Time range<select aria-label="Model usage time range" value={filters.range} onChange={(e) => {
        const range = e.target.value as UsageFilters["range"];
        setDateError("");
        if (range === "custom" && !filters.start) {
          const today = new Date().toISOString().slice(0, 10);
          onChange({ ...filters, range, start: today, end: today });
        } else onChange({ ...filters, range });
      }}>
        <option value="24h">Last 24 hours</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option><option value="90d">Last 90 days</option><option value="custom">Custom dates (UTC)</option>
      </select></label>
      <label>Model<select aria-label="Usage model" value={filters.model} onChange={(e) => onChange({ ...filters, model: e.target.value })}>
        <option value="">All models</option>{models.map((model) => <option key={model} value={model}>{model === "unreported" ? "Model not reported" : model}</option>)}
      </select></label>
      {filters.range === "custom" ? <>
        <label>From (UTC)<input type="date" value={dates.start} onChange={(e) => setDates({ ...dates, start: e.target.value })} /></label>
        <label>Through (UTC)<input type="date" value={dates.end} onChange={(e) => setDates({ ...dates, end: e.target.value })} /></label>
        <Action onClick={() => {
          const next = { ...filters, ...dates };
          if (usageWindow(next, new Date())) { onChange(next); setDateError(""); }
          else setDateError("Choose up to 90 days, ending today or earlier.");
        }}>Apply dates</Action>
      </> : null}
    </div>
    {dateError ? <p role="alert" className={styles.notice}>{dateError}</p> : null}
    {reportRead.failed ? <p role="alert" className={styles.notice}>Model usage is unavailable. <Action onClick={() => void reportRead.refresh()}>Retry usage</Action></p> : null}
    {reportRead.loading ? <p role="status" className={styles.muted}>Loading usage…</p> : null}
    {report ? <>
      <Metrics items={[
        { key: "spend", label: "Recorded spend", value: metricText(report.totals, "cost"), note: report.totals.unknown_cost_calls ? `${count(report.totals.unknown_cost_calls)} calls with unknown cost` : "Provider-reported USD" },
        { key: "tokens", label: "Tokens", value: metricText(report.totals, "tokens"), note: `${count(report.totals.cached_tokens)} cached · ${count(report.totals.reasoning_tokens)} reasoning` },
        { key: "calls", label: "Model calls", value: count(report.totals.calls), note: `${count(report.totals.failed)} failed · ${count(report.totals.pending)} pending` },
        { key: "latency", label: "Mean latency", value: ms(report.totals.latency_ms), note: "Completed requests" },
      ]} />
      <p className={styles.muted}>{report.tracked_since ? `History begins ${utc(report.tracked_since)} UTC. ` : "No model requests have been recorded yet. "}
        A chat turn can make several calls. Unknown costs and tokens are excluded from totals{report.totals.unknown_token_calls ? `; ${count(report.totals.unknown_token_calls)} calls have incomplete token counts` : ""}.</p>
      <UsageChart report={report} metric={filters.metric} onMetric={(metric) => onChange({ ...filters, metric })} />
      <section className={styles.section} aria-label="Usage by model">
        <h2>By model</h2>
        {report.models.length ? <div className={styles.table}><table><thead><tr><th>Actual model</th><th>Spend (USD)</th><th>Calls / failed</th><th>Input / output tokens</th><th>Cached / reasoning</th><th>Mean latency</th></tr></thead><tbody>
          {report.models.map((row) => <tr key={row.model}>
            <td><button className={styles.model} onClick={() => onChange({ ...filters, model: row.model })}>{row.model === "unreported" ? "Model not reported" : row.model}</button></td>
            <td>{metricText(row, "cost")}{row.unknown_cost_calls ? <div className={styles.muted}>{row.unknown_cost_calls} unknown</div> : null}</td>
            <td>{count(row.calls)} / {count(row.failed)}</td><td>{count(row.input_tokens)} / {count(row.output_tokens)}</td>
            <td>{count(row.cached_tokens)} / {count(row.reasoning_tokens)}</td><td>{ms(row.latency_ms)}</td>
          </tr>)}</tbody></table></div> : <p className={styles.muted}>No requests in this selection.</p>}
      </section>
    </> : null}
    <div className={styles.split}>
      <section className={styles.section} aria-label="Application model budget">
        <h2>Application budget</h2>
        <p className={styles.muted}>All models, independent of chart filters. Daily and monthly periods reset at midnight UTC.</p>
        {budgetRead.failed ? <p role="alert" className={styles.notice}>Budget unavailable. <Action onClick={() => void budgetRead.refresh()}>Retry budget</Action></p> : null}
        {budgetRead.loading && !budget ? <p className={styles.muted}>Loading budget…</p> : null}
        {budget ? <BudgetPanel budget={budget} canConfigure={canConfigureBudget} onSaved={() => void budgetRead.refresh()} /> : null}
      </section>
      <section className={styles.section} aria-label="OpenRouter key usage">
        <h2>OpenRouter key</h2>
        <p className={styles.muted}>Provider totals for the configured key, including other applications using it. Separate from this application's budget.</p>
        {key?.status === "ok" ? <>
          <dl className={styles.facts}><div><dt>Today</dt><dd>{usd(key.usage_daily)}</dd></div><div><dt>This month</dt><dd>{usd(key.usage_monthly)}</dd></div>
            <div><dt>Lifetime spend</dt><dd>{usd(key.usage)}</dd></div><div><dt>Key limit remaining</dt><dd>{key.limit_state === "unlimited" ? "Not set" : key.limit_state === "configured" ? usd(key.limit_remaining) : "Unknown"}</dd></div></dl>
          <p className={styles.muted}>Key limit: {key.limit_state === "unlimited" ? "not set" : usd(key.limit)}{key.limit_reset ? ` · resets ${key.limit_reset}` : ""}. Checked {key.checked_at ? `${utc(key.checked_at)} UTC` : "—"}.</p>
        </> : <p className={styles.muted}>{key?.status === "not_configured" ? "No OpenRouter key configured for this operator service." : keyRead.loading ? "Checking provider totals…" : "Provider totals unavailable."}</p>}
        <p className={styles.muted}><a href="https://openrouter.ai/activity" target="_blank" rel="noreferrer">OpenRouter activity</a> · <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noreferrer">Key limits</a></p>
      </section>
    </div>
    {report?.recent.length ? <details className={`${styles.section} ${styles.recent}`}><summary>Recent calls · latest {report.recent.length}</summary><div className={styles.table}><table>
      <thead><tr><th>Started (UTC)</th><th>Requested → actual model</th><th>Outcome</th><th>Spend (USD)</th><th>Input / output</th><th>Latency</th></tr></thead><tbody>
        {report.recent.map((call) => <tr key={call.call_id}><td>{utc(call.started_at)}</td><td>{call.requested_model} → {call.actual_model ?? "unreported"}</td>
          <td>{call.status}{call.error_code ? <div className={styles.muted}>{call.error_code}</div> : null}</td><td>{usd(call.cost_usd)}</td>
          <td>{call.input_tokens === null ? "?" : count(call.input_tokens)} / {call.output_tokens === null ? "?" : count(call.output_tokens)}</td><td>{ms(call.latency_ms)}</td></tr>)}
      </tbody></table></div></details> : null}
  </div>;
}

function UsageChart({ report, metric, onMetric }: { report: UsageReport; metric: UsageMetric; onMetric: (metric: UsageMetric) => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const row = report.series.find((bucket) => bucket.at === selected) ?? null;
  const peak = Math.max(0, ...report.series.map((bucket) => usageValue(bucket, metric) ?? 0));
  return <section className={`${styles.section} ${styles.chart}`} aria-label="Model usage trend">
    <div className={styles.sectionHead}><h2>Over time</h2><Segment label="Chart metric" value={metric} onChange={onMetric} options={Object.entries(metricLabels).map(([value, label]) => ({ value: value as UsageMetric, label }))} /></div>
    <div className={styles.axis}><span>{metricLabels[metric]} · {report.bucket_hours === 1 ? "hourly" : "daily"} intervals (UTC)</span><span>Peak {peak ? (metric === "cost" ? usd(peak) : metric === "latency" ? ms(peak) : count(peak)) : "—"}</span></div>
    <div className={styles.plot} role="group" aria-label="Model usage intervals">
      {report.series.map((bucket) => <button type="button" className={styles.bar} key={bucket.at}
        aria-label={`${utc(bucket.at)} UTC · ${metricLabels[metric]} ${metricText(bucket, metric)} · ${bucket.calls} calls · ${bucket.unknown_cost_calls} unknown costs`}
        aria-pressed={row?.at === bucket.at} onFocus={() => setSelected(bucket.at)} onMouseEnter={() => setSelected(bucket.at)} onClick={() => setSelected(bucket.at)}>
        <span style={{ height: `${peak ? Math.max(1, (usageValue(bucket, metric) ?? 0) / peak * 100) : 1}%` }} />
      </button>)}
    </div>
    <div className={styles.axis}><span>{utc(report.start_at)}</span><span>{utc(report.end_at)} UTC</span></div>
    <div className={styles.selection} aria-live="polite">{row ? `${utc(row.at)} – ${utc(row.until)} UTC · ${metricLabels[metric]} ${metricText(row, metric)} · ${row.calls} calls · ${row.failed} failed · ${row.unknown_cost_calls} unknown costs` : "Select or focus an interval for details."}</div>
  </section>;
}

function BudgetPanel({ budget, canConfigure, onSaved }: { budget: ModelBudget; canConfigure: boolean; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ daily: "", monthly: "", mode: "enforce" as ModelBudget["mode"], alert: 80, revision: 1 });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState("");
  const state = budgetState(budget);
  const label = { unset: "Limits not set", unknown: "Cost unknown", reached: "Limit reached", warning: "Near limit", within: "Within budget" }[state];
  const edit = () => {
    setDraft({ daily: budget.daily_limit_usd ?? "", monthly: budget.monthly_limit_usd ?? "", mode: budget.mode, alert: budget.alert_percent, revision: budget.revision });
    setError(""); setReceipt(""); setEditing(true);
  };
  const save = async () => {
    setError(""); setSaving(true);
    try {
      const body: BudgetUpdate = { expected_revision: draft.revision, mode: draft.mode, daily_limit_usd: budgetAmount(draft.daily), monthly_limit_usd: budgetAmount(draft.monthly), alert_percent: draft.alert };
      const saved = await api<ModelBudget>("/admin/v1/models/budget", { method: "PATCH", bodyJson: body });
      setEditing(false); setReceipt(`Budget saved · revision ${saved.revision}`); onSaved();
    } catch (e) { setError(readableError(e, "Budget update unavailable. Refresh its recorded state before retrying.")); }
    finally { setSaving(false); }
  };
  return <>
    <Chip tone={state === "reached" || state === "unknown" ? "warn" : "neutral"}>{label}</Chip>
    {[["Today", budget.daily_used_usd, budget.daily_limit_usd], ["This month", budget.monthly_used_usd, budget.monthly_limit_usd]].map(([name, used, limit]) =>
      <div key={name} className={styles.period}><header><strong>{name}</strong><span>{usd(used)} / {limit === null ? "no limit" : usd(limit)}</span></header>
        {limit !== null ? <progress aria-label={`${name} budget used`} max={100} value={Number(limit) === 0 ? 100 : Math.min(100, Number(used) / Number(limit) * 100)} /> : null}</div>)}
    <p className={styles.muted}>{budget.mode === "enforce" ? "Configured limits stop new model calls when recorded spend reaches the limit or a prior charge is unknown. Calls already running can still finish and exceed a limit." : "Warning mode alerts without stopping model calls."}
      {budget.unknown_calls > 0 ? ` ${budget.unknown_calls} prior charges are unknown; review OpenRouter activity.` : ""} {budget.pending_calls ? `${budget.pending_calls} calls are pending.` : ""}</p>
    {!editing && canConfigure ? <Action onClick={edit}>Edit budget</Action> : null}
    {receipt ? <p role="status" className={styles.muted}>{receipt}</p> : null}
    {editing && canConfigure ? <form className={styles.form} onSubmit={(event) => { event.preventDefault(); void save(); }} aria-label="Edit application budget">
      <label>Daily limit (USD)<input inputMode="decimal" value={draft.daily} placeholder="Not set" onChange={(e) => setDraft({ ...draft, daily: e.target.value })} disabled={saving} /></label>
      <label>Monthly limit (USD)<input inputMode="decimal" value={draft.monthly} placeholder="Not set" onChange={(e) => setDraft({ ...draft, monthly: e.target.value })} disabled={saving} /></label>
      <label>At the limit<select value={draft.mode} onChange={(e) => setDraft({ ...draft, mode: e.target.value as ModelBudget["mode"] })} disabled={saving}><option value="enforce">Stop new model calls</option><option value="warn">Warn only</option></select></label>
      <label>Warning threshold (%)<input type="number" min={1} max={100} required value={draft.alert} onChange={(e) => setDraft({ ...draft, alert: Number(e.target.value) })} disabled={saving} /></label>
      <p className={styles.muted}>Blank removes a limit. Zero stops all new calls in enforcement mode. Changes are audited.</p>
      {error ? <p role="alert" className={styles.notice}>{error}</p> : null}
      <div className={styles.formActions}><button type="submit" className="admin-primary-button" disabled={saving}>{saving ? "Saving…" : "Save budget"}</button><Action onClick={() => setEditing(false)} disabled={saving}>Cancel</Action></div>
    </form> : null}
  </>;
}
