export type UsageRange = "24h" | "7d" | "30d" | "90d" | "custom";
export type UsageMetric = "cost" | "tokens" | "calls" | "latency";
export interface UsageFilters {
  range: UsageRange;
  model: string;
  metric: UsageMetric;
  start: string;
  end: string;
}
export interface UsageTotals {
  calls: number;
  failed: number;
  cost_usd: string;
  unknown_cost_calls: number;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number | null;
}
export interface UsageBucket extends UsageTotals { at: string; until: string }
export interface UsageModel extends UsageTotals { model: string; cached_tokens: number; reasoning_tokens: number }
export interface UsageCall {
  call_id: string; started_at: string; completed_at: string | null;
  requested_model: string; actual_model: string | null; status: string;
  input_tokens: number | null; output_tokens: number | null; cost_usd: string | null;
  latency_ms: number | null; error_code: string | null;
}
export interface UsageReport {
  generated_at: string; start_at: string; end_at: string; bucket_hours: number;
  model: string | null; tracked_since: string | null; model_options: string[];
  totals: UsageTotals & { pending: number; unknown_token_calls: number; cached_tokens: number; reasoning_tokens: number };
  series: UsageBucket[]; models: UsageModel[]; recent: UsageCall[];
}
export interface ModelBudget {
  revision: number; mode: "warn" | "enforce";
  daily_limit_usd: string | null; monthly_limit_usd: string | null;
  daily_used_usd: string; monthly_used_usd: string; alert_percent: number;
  updated_at: string; generated_at: string; day_start: string; month_start: string;
  unknown_calls: number; pending_calls: number;
}
export interface BudgetUpdate {
  expected_revision: number; mode: "warn" | "enforce";
  daily_limit_usd: string | null; monthly_limit_usd: string | null; alert_percent: number;
}
export interface KeyUsage {
  status: "ok" | "unavailable" | "not_configured"; checked_at: string | null;
  usage: string | null; usage_daily: string | null; usage_weekly: string | null; usage_monthly: string | null;
  limit: string | null; limit_remaining: string | null; limit_reset: string | null;
  limit_state: "configured" | "unlimited" | "unknown";
}

const DAY = 86400_000;
const RANGES: UsageRange[] = ["24h", "7d", "30d", "90d", "custom"];
const METRICS: UsageMetric[] = ["cost", "tokens", "calls", "latency"];
export const DEFAULT_USAGE_FILTERS: UsageFilters = { range: "7d", model: "", metric: "cost", start: "", end: "" };

export function usageFiltersFromUrl(href: string): UsageFilters {
  const params = new URL(href).searchParams;
  const range = params.get("usage_range") as UsageRange;
  const metric = params.get("usage_metric") as UsageMetric;
  const model = params.get("usage_model") ?? "";
  const filters = {
    range: RANGES.includes(range) ? range : "7d" as UsageRange,
    metric: METRICS.includes(metric) ? metric : "cost" as UsageMetric,
    model: /^[!-~]{1,200}$/.test(model) ? model : "",
    start: params.get("usage_start") ?? "", end: params.get("usage_end") ?? "",
  };
  if (filters.range === "custom" && !usageWindow(filters, new Date())) {
    return { ...filters, range: "7d", start: "", end: "" };
  }
  return filters;
}

export function usageFiltersUrl(filters: UsageFilters, href: string): string {
  const url = new URL(href);
  url.searchParams.set("tab", "models");
  for (const key of ["range", "model", "metric", "start", "end"] as const) {
    const value = key === "start" || key === "end" ? (filters.range === "custom" ? filters[key] : "") : filters[key];
    if (value && value !== DEFAULT_USAGE_FILTERS[key]) url.searchParams.set(`usage_${key}`, value);
    else url.searchParams.delete(`usage_${key}`);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

function validDate(value: string): boolean {
  const parsed = new Date(`${value}T00:00:00Z`);
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

export function usageWindow(filters: UsageFilters, now: Date): { start: string; end: string; hours: number } | null {
  let end = now.getTime();
  let start = end - (filters.range === "24h" ? 1 : filters.range === "30d" ? 30 : filters.range === "90d" ? 90 : 7) * DAY;
  if (filters.range === "custom") {
    if (!validDate(filters.start) || !validDate(filters.end)) return null;
    start = Date.parse(`${filters.start}T00:00:00Z`);
    end = Date.parse(`${filters.end}T00:00:00Z`) + DAY;
    if (end > now.getTime() + DAY || end <= start || end - start > 90 * DAY) return null;
  }
  return { start: new Date(start).toISOString(), end: new Date(end).toISOString(), hours: filters.range === "24h" ? 1 : 24 };
}

export function usd(value: string | number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "Unknown";
  const n = Number(value);
  if (n > 0 && n < .000001) return "<$0.000001";
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6 });
}

export function usageValue(row: UsageTotals, metric: UsageMetric): number | null {
  if (metric === "cost") return Number(row.cost_usd);
  if (metric === "tokens") return row.input_tokens + row.output_tokens;
  if (metric === "latency") return row.latency_ms;
  return row.calls;
}

export function usageChart(report: UsageReport, metric: UsageMetric) {
  const series = metric === "tokens"
    ? [{ key: "total", label: "Total", value: (row: UsageBucket) => usageValue(row, metric) },
      { key: "input", label: "Input", value: (row: UsageBucket) => row.input_tokens },
      { key: "output", label: "Output", value: (row: UsageBucket) => row.output_tokens }]
    : metric === "calls"
      ? [{ key: "total", label: "Calls", value: (row: UsageBucket) => row.calls },
        { key: "failed", label: "Failed", value: (row: UsageBucket) => row.failed }]
      : [{ key: "total", label: metric === "cost" ? "Reported spend" : "Mean latency", value: (row: UsageBucket) => usageValue(row, metric) }];
  const start = Date.parse(report.start_at);
  const duration = Math.max(1, Date.parse(report.end_at) - start);
  const tracked = report.tracked_since ? Date.parse(report.tracked_since) : null;
  const lines = series.map((series) => ({ ...series, points: report.series.map((bucket) => {
    const at = Date.parse(bucket.at);
    const until = Date.parse(bucket.until);
    const value = tracked !== null && until <= tracked ? null : series.value(bucket);
    return {
      x: Math.max(0, Math.min(100, ((at + until) / 2 - start) / duration * 100)),
      left: Math.max(0, (at - start) / duration * 100),
      width: Math.max(0, (until - at) / duration * 100),
      value: value !== null && Number.isFinite(value) ? value : null,
    };
  }) }));
  const peak = Math.max(0, ...lines.flatMap((line) => line.points.map((point) => point.value ?? 0)));
  const rawStep = peak / 4 || 1;
  const power = 10 ** Math.floor(Math.log10(rawStep));
  const step = Math.max(metric === "calls" || metric === "tokens" ? 1 : 0,
    [1, 2, 5, 10].find((multiple) => multiple * power >= rawStep)! * power);
  const top = step * 4;
  return {
    peak, top, ticks: [4, 3, 2, 1, 0].map((index) => index * step),
    lines: lines.map((line) => {
      let connected = false;
      const path = line.points.map((point) => {
        if (point.value === null) { connected = false; return ""; }
        const command = connected ? "L" : "M";
        connected = true;
        return `${command}${point.x * 10},${240 * (1 - point.value / top)}`;
      }).join(" ");
      return { ...line, path };
    }),
  };
}

export function budgetAmount(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (!/^\d+(?:\.\d{1,8})?$/.test(trimmed) || Number(trimmed) > 100_000) throw new Error("Use a USD amount from 0 to 100,000, or leave blank.");
  return trimmed;
}

export function budgetState(budget: ModelBudget): "unset" | "unknown" | "reached" | "warning" | "within" {
  const limits = [[budget.daily_limit_usd, budget.daily_used_usd], [budget.monthly_limit_usd, budget.monthly_used_usd]];
  const configured = limits.filter(([limit]) => limit !== null);
  if (!configured.length) return "unset";
  if (configured.some(([limit, used]) => Number(used) >= Number(limit))) return "reached";
  if (budget.unknown_calls > 0) return "unknown";
  if (configured.some(([limit, used]) => Number(used) >= Number(limit) * budget.alert_percent / 100)) return "warning";
  return "within";
}
