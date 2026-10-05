import assert from "node:assert/strict";
import test from "node:test";
import { budgetAmount, budgetState, usageFiltersFromUrl, usageFiltersUrl, usageWindow, usd, DEFAULT_USAGE_FILTERS } from "../lib/admin-model-usage.ts";
import { adminHistoryLocationFromUrl } from "../lib/admin-history.ts";

test("custom dates are UTC, inclusive, bounded and invalid dates never normalize silently", () => {
  const now = new Date("2026-10-04T12:00:00Z");
  const filters = { ...DEFAULT_USAGE_FILTERS, range: "custom", start: "2026-09-01", end: "2026-09-30" };
  assert.deepEqual(usageWindow(filters, now), { start: "2026-09-01T00:00:00.000Z", end: "2026-10-01T00:00:00.000Z", hours: 24 });
  for (const dates of [{ start: "2026-02-30", end: "2026-03-01" }, { start: "2026-13-01", end: "2026-13-02" }, { start: "2026-01-01", end: "2026-09-30" }, { start: "2026-10-05", end: "2026-10-05" }]) {
    assert.equal(usageWindow({ ...filters, ...dates }, now), null);
  }
  assert.equal(usageWindow({ ...DEFAULT_USAGE_FILTERS, range: "24h" }, now).hours, 1);
});

test("model usage bookmarks preserve other workspace filters and restore in browser history", () => {
  const href = "https://events.test/admin?registry_query=library#current";
  const filters = { ...DEFAULT_USAGE_FILTERS, range: "30d", model: "provider/fallback", metric: "tokens" };
  const next = new URL(usageFiltersUrl(filters, href), href);
  assert.deepEqual(usageFiltersFromUrl(next.href), filters);
  assert.equal(next.searchParams.get("registry_query"), "library");
  assert.equal(next.hash, "#current");
  assert.equal(adminHistoryLocationFromUrl(next.href).tab, "models");
  assert.equal(usageFiltersFromUrl("https://events.test/admin?usage_range=invalid&usage_model=a%0Ab").model, "");
});

test("budget amounts preserve explicit zero, unset and exact decimal strings", () => {
  assert.equal(budgetAmount(""), null);
  assert.equal(budgetAmount("0"), "0");
  assert.equal(budgetAmount(" 2.50000000 "), "2.50000000");
  for (const value of ["NaN", "-1", "1e3", "100001", "0.123456789"]) assert.throws(() => budgetAmount(value));
  assert.equal(usd(null), "Unknown");
  assert.equal(usd("0"), "$0.00");
  assert.equal(usd(".000001"), "$0.000001");
});

test("budget alerts reflect unset limits, unknown charges and exhaustion independently of chart scope", () => {
  const budget = { daily_limit_usd: null, monthly_limit_usd: null, daily_used_usd: "1", monthly_used_usd: "2", alert_percent: 80, unknown_calls: 0 };
  assert.equal(budgetState(budget), "unset");
  assert.equal(budgetState({ ...budget, daily_limit_usd: "0" }), "reached");
  assert.equal(budgetState({ ...budget, daily_limit_usd: "2", unknown_calls: 1 }), "unknown");
  assert.equal(budgetState({ ...budget, monthly_limit_usd: "2.5" }), "warning");
  assert.equal(budgetState({ ...budget, daily_limit_usd: "2", monthly_limit_usd: "10" }), "within");
});
