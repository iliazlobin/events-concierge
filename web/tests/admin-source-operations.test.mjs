import assert from "node:assert/strict";
import test from "node:test";
import { sourceCollectionEligibility, sourceCollectionTimestamp, summarizeSourceCollection } from "../lib/admin-source-operations.ts";

const NOW = Date.parse("2026-09-08T16:00:00Z");
const source = (key, overrides = {}) => ({
  source_key: key, display_name: key, enabled: true, retired_at: null,
  review_status: "reviewed", effective_status: "active", due: false,
  latest_run: { status: "succeeded" }, last_succeeded_at: "2026-09-08T15:00:00Z",
  next_due_at: "2026-09-09T00:00:00Z", ...overrides,
});

test("eligibility respects lifecycle, approval and the explicit scheduling states", () => {
  for (const effective_status of ["active", "due", "running"]) {
    assert.equal(sourceCollectionEligibility(source("active", { effective_status })), "eligible");
  }
  for (const [overrides, expected] of [
    [{ retired_at: "2026-01-01T00:00:00Z", enabled: false }, "retired"],
    [{ effective_status: "retired" }, "retired"],
    [{ enabled: false }, "paused"],
    [{ effective_status: "disabled" }, "paused"],
    [{ review_status: "unreviewed" }, "blocked"],
    [{ review_status: "expired" }, "blocked"],
    [{ effective_status: "review_expired" }, "blocked"],
    [{ effective_status: "policy_blocked" }, "blocked"],
    [{ effective_status: "future_unknown_state" }, "unknown"],
  ]) assert.equal(sourceCollectionEligibility(source("case", overrides)), expected);
});

test("due waiting, collecting and first-success evidence have explicit denominators", () => {
  const result = summarizeSourceCollection([
    source("failed-due", { due: true, latest_run: { status: "failed" } }),
    source("collecting", { due: true, effective_status: "running", latest_run: { status: "running" } }),
    source("first", { due: true, last_succeeded_at: null, latest_run: null }),
    source("oldest", { last_succeeded_at: "2026-01-01T00:00:00Z" }),
    source("paused", { enabled: false, due: true, last_succeeded_at: "2000-01-01T00:00:00Z" }),
    source("blocked", { review_status: "expired", latest_run: { status: "running" } }),
    source("retired", { retired_at: "2026-01-01T00:00:00Z", last_succeeded_at: null }),
  ], "", NOW);
  assert.equal(result.state, "available");
  assert.equal(result.eligibleCount, 4);
  assert.equal(result.dueCount, 2);
  assert.equal(result.collectingCount, 1);
  assert.equal(result.awaitingSuccessCount, 1);
  assert.equal(result.oldestSuccess.sourceKey, "oldest");
  assert.deepEqual(result.nextEligibility.map(item => item.sourceKey), ["oldest"]);
});

test("future eligibility is bounded, ordered by instant, and excludes current work or invalid dates", () => {
  const rows = [
    source("late", { next_due_at: "2026-09-12T00:00:00Z" }),
    source("first", { next_due_at: "2026-09-08T18:00:00+01:00" }),
    source("second", { next_due_at: "2026-09-08T18:00:00Z" }),
    source("third", { next_due_at: "2026-09-08T19:00:00Z" }),
    source("missing", { next_due_at: null }), source("invalid", { next_due_at: "invalid" }),
    source("past", { next_due_at: "2026-09-08T15:00:00Z" }),
    source("now", { next_due_at: "2026-09-08T16:00:00Z" }),
    source("already-due", { due: true }),
    source("running", { latest_run: { status: "running" } }),
    source("paused", { enabled: false }), source("blocked", { review_status: "expired" }),
  ];
  const before = JSON.stringify(rows);
  assert.deepEqual(summarizeSourceCollection(rows, "", NOW).nextEligibility.map(item => item.sourceKey), ["first", "second", "third"]);
  assert.equal(JSON.stringify(rows), before);
  assert.equal(sourceCollectionTimestamp("2099-01-02T00:00:00Z"), "2099-01-02 00:00 UTC");
  assert.equal(sourceCollectionTimestamp("2026-09-08T18:00:00+01:00"), "2026-09-08 17:00 UTC");
  assert.equal(sourceCollectionTimestamp("invalid"), "Unknown");
});

test("missing reads and missing or ambiguous selected records never become zero counts", () => {
  for (const result of [
    summarizeSourceCollection(null, "", NOW),
    summarizeSourceCollection([], "missing", NOW),
    summarizeSourceCollection([source("other")], "missing", NOW),
    summarizeSourceCollection([source("same"), source("same")], "same", NOW),
  ]) {
    assert.equal(result.state, "unknown");
    assert.equal(result.dueCount, null);
    assert.equal(result.collectingCount, null);
    assert.equal(result.awaitingSuccessCount, null);
    assert.deepEqual(result.nextEligibility, []);
  }
  const empty = summarizeSourceCollection([], "", NOW);
  assert.equal(empty.state, "available");
  assert.equal(empty.eligibleCount, 0);
  assert.equal(empty.dueCount, 0);
  assert.equal(empty.oldestSuccess, null);
});

test("selected source scope never borrows other sources' dates or lifecycle", () => {
  const rows = [source("one", { due: true }), source("two"), source("paused", { enabled: false })];
  const one = summarizeSourceCollection(rows, "one", NOW);
  assert.equal(one.eligibleCount, 1);
  assert.equal(one.dueCount, 1);
  assert.deepEqual(one.nextEligibility, []);
  const two = summarizeSourceCollection(rows, "two", NOW);
  assert.equal(two.dueCount, 0);
  assert.equal(two.nextEligibility[0].sourceKey, "two");
  const paused = summarizeSourceCollection(rows, "paused", NOW);
  assert.equal(paused.state, "excluded");
  assert.equal(paused.eligibility, "paused");
  assert.equal(paused.dueCount, null);
  assert.deepEqual(paused.nextEligibility, []);
  assert.equal(paused.selectedLastSuccessAt, "2026-09-08T15:00:00Z");
  for (const last_succeeded_at of [null, "invalid", "2099-01-02T00:00:00Z"]) {
    const excluded = summarizeSourceCollection([source("paused", { enabled: false, last_succeeded_at })], "paused", NOW);
    assert.equal(excluded.selectedLastSuccessAt, null);
    assert.equal(excluded.successDatesUnknown, last_succeeded_at !== null);
    assert.deepEqual(excluded.nextEligibility, []);
  }
});

test("malformed success dates remain unknown rather than becoming first success or apparent freshness", () => {
  for (const date of ["invalid", "2099-01-02T00:00:00Z"]) {
    const result = summarizeSourceCollection([source("known"), source("bad", { last_succeeded_at: date })], "", NOW);
    assert.equal(result.successDatesUnknown, true);
    assert.equal(result.oldestSuccess, null);
    assert.equal(result.awaitingSuccessCount, 0);
  }
});
