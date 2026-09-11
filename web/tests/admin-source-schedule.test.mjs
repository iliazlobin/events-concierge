import assert from "node:assert/strict";
import test from "node:test";
import { projectSourceSchedule } from "../lib/admin-source-schedule.ts";

const NOW = Date.parse("2026-09-08T16:13:00Z");
const HOUR = 60 * 60 * 1000;
const at = hours => new Date(NOW + hours * HOUR).toISOString();
const keys = entries => entries.map(entry => entry.source.source_key);
const source = (source_key, overrides = {}) => ({
  source_key, display_name: source_key, enabled: true, retired_at: null,
  review_status: "reviewed", effective_status: "active", due: false,
  latest_run: { status: "succeeded" }, last_succeeded_at: at(-23),
  next_due_at: at(1), ...overrides,
});

test("overdue dates reveal waiting work despite stale due flags, while collecting is separate", () => {
  const schedule = projectSourceSchedule([
    source("past", { next_due_at: at(-4) }),
    source("now", { next_due_at: at(0) }),
    source("flag", { due: true, next_due_at: null }),
    source("status", { effective_status: "due", next_due_at: null }),
    source("conflicting-date", { due: true, next_due_at: at(3) }),
    source("collecting-status", { effective_status: "running", due: true, next_due_at: at(-6) }),
    source("collecting-run", { latest_run: { status: "running" }, next_due_at: at(-2) }),
    source("future"),
  ], "", NOW, 24);
  assert.equal(schedule.state, "available");
  assert.equal(schedule.eligibleCount, 8);
  assert.deepEqual(keys(schedule.due), ["past", "now", "conflicting-date", "flag", "status"]);
  assert.deepEqual(schedule.due.map(entry => entry.eligibleAt), [at(-4), at(0), null, null, null]);
  assert.deepEqual(keys(schedule.running), ["collecting-status", "collecting-run"]);
  assert.deepEqual(keys(schedule.upcoming), ["future"]);
  assert.equal(schedule.buckets.reduce((sum, bucket) => sum + bucket.count, 0), 1);
});

test("malformed eligibility dates remain undated rather than being guessed from cadence or success", () => {
  const schedule = projectSourceSchedule([
    source("missing", { next_due_at: null }),
    source("invalid", { next_due_at: "not-a-date" }),
    source("due-invalid", { next_due_at: "not-a-date", due: true }),
    source("status-invalid", { next_due_at: "not-a-date", effective_status: "due" }),
    source("empty", { next_due_at: "" }),
    source("collecting-invalid", { next_due_at: "invalid", latest_run: { status: "running" } }),
  ], "", NOW, 24);
  assert.deepEqual(keys(schedule.undated), ["empty", "invalid", "missing"]);
  assert.deepEqual(schedule.undated.map(entry => entry.eligibleAt), [null, null, null]);
  assert.deepEqual(keys(schedule.running), ["collecting-invalid"]);
  assert.deepEqual(schedule.upcoming, []);
  assert.deepEqual(keys(schedule.due), ["due-invalid", "status-invalid"]);
  assert.deepEqual(schedule.due.map(entry => entry.eligibleAt), [null, null]);
});

test("paused, retired, blocked and unknown lifecycle sources cannot enter the forecast", () => {
  const schedule = projectSourceSchedule([
    source("paused", { enabled: false, due: true }),
    source("retired", { retired_at: at(-2), effective_status: "running" }),
    source("expired", { review_status: "expired", next_due_at: at(-2) }),
    source("policy", { effective_status: "policy_blocked" }),
    source("unknown", { effective_status: "new-status" }),
    source("included"),
  ], "", NOW, 24);
  assert.deepEqual(schedule.excludedByReason, { paused: 1, retired: 1, blocked: 2, unknown: 1 });
  assert.equal(schedule.excludedCount, 5);
  assert.equal(schedule.eligibleCount, 1);
  assert.deepEqual(keys(schedule.upcoming), ["included"]);
  assert.deepEqual(schedule.due, []);
  assert.deepEqual(schedule.running, []);
});

test("forecast preserves source scope and distinguishes excluded, unknown and empty snapshots", () => {
  const rows = [source("one"), source("two", { due: true }), source("paused", { enabled: false })];
  const one = projectSourceSchedule(rows, "one", NOW, 24);
  assert.equal(one.selected, rows[0]);
  assert.equal(one.eligibility, "eligible");
  assert.equal(one.eligibleCount, 1);
  assert.deepEqual(keys(one.upcoming), ["one"]);
  assert.deepEqual(one.due, []);
  const paused = projectSourceSchedule(rows, "paused", NOW, 24);
  assert.equal(paused.state, "excluded");
  assert.equal(paused.eligibility, "paused");
  assert.equal(paused.excludedCount, 1);
  assert.deepEqual(paused.buckets, []);
  const unknown = projectSourceSchedule([source("unknown", { effective_status: "unknown" })], "unknown", NOW, 24);
  assert.equal(unknown.state, "unknown");
  assert.equal(unknown.excludedByReason.unknown, 1);
  assert.deepEqual(unknown.upcoming, []);
  for (const result of [
    projectSourceSchedule(null, "", NOW, 24),
    projectSourceSchedule([], "missing", NOW, 24),
    projectSourceSchedule(rows, "missing", NOW, 24),
    projectSourceSchedule([source("same"), source("same")], "same", NOW, 24),
    projectSourceSchedule(rows, "", NaN, 24),
    projectSourceSchedule(rows, "", 8.64e15, 168),
    projectSourceSchedule(rows, "", NOW, 12),
  ]) {
    assert.equal(result.state, "unknown");
    assert.equal(result.eligibleCount, null);
    assert.equal(result.excludedCount, null);
    assert.equal(result.laterCount, null);
    assert.equal(result.horizonEndsAt, null);
    assert.deepEqual(result.buckets, []);
  }
  const empty = projectSourceSchedule([], "", NOW, 24);
  assert.equal(empty.state, "available");
  assert.equal(empty.eligibleCount, 0);
  assert.equal(empty.excludedCount, 0);
  assert.equal(empty.buckets.length, 24);
});

test("future eligibility sorts by instant and stable source key, counts later work and never repeats a source", () => {
  const rows = [
    source("later", { next_due_at: at(30) }),
    source("last", { next_due_at: at(24) }),
    source("beta", { next_due_at: "2026-09-08T18:13:00+01:00" }),
    source("alpha", { next_due_at: at(1) }),
    source("first", { next_due_at: at(0.5) }),
  ];
  const before = JSON.stringify(rows);
  const schedule = projectSourceSchedule(rows, "", NOW, 24);
  assert.deepEqual(keys(schedule.upcoming), ["first", "alpha", "beta", "last"]);
  assert.equal(schedule.laterCount, 1);
  assert.equal(schedule.horizonEndsAt, at(24));
  assert.equal(JSON.stringify(rows), before);
  assert.deepEqual(keys(projectSourceSchedule(rows, "", NOW, 168).upcoming), ["first", "alpha", "beta", "last", "later"]);
  assert.equal(projectSourceSchedule(rows, "", NOW, 168).laterCount, 0);
});

test("interactive buckets contain the exact source lists, honor horizon boundaries and use the supplied clock", () => {
  for (const [horizon, width, bucketCount] of [[24, 1, 24], [48, 2, 24], [168, 12, 14]]) {
    const schedule = projectSourceSchedule([
      source("first", { next_due_at: at(0.25) }),
      source("boundary", { next_due_at: at(width) }),
      source("last", { next_due_at: at(horizon) }),
      source("beyond", { next_due_at: at(horizon + 0.001) }),
    ], "", NOW, horizon);
    assert.equal(schedule.buckets.length, bucketCount);
    assert.equal(schedule.buckets[0].startAt, at(0));
    assert.equal(schedule.buckets[0].endAt, at(width));
    assert.deepEqual(keys(schedule.buckets[0].sources), ["first"]);
    assert.deepEqual(keys(schedule.buckets[1].sources), ["boundary"]);
    assert.deepEqual(keys(schedule.buckets.at(-1).sources), ["last"]);
    assert.equal(schedule.buckets.at(-1).endAt, at(horizon));
    assert.equal(schedule.buckets.reduce((total, bucket) => total + bucket.count, 0), 3);
    assert.ok(schedule.buckets.every(bucket => bucket.count === bucket.sources.length));
    assert.deepEqual(keys(schedule.buckets.flatMap(bucket => bucket.sources)), keys(schedule.upcoming));
    assert.equal(schedule.laterCount, 1);
  }
  const progressed = projectSourceSchedule([source("was-future")], "", NOW + 2 * HOUR, 24);
  assert.deepEqual(keys(progressed.due), ["was-future"]);
  assert.deepEqual(progressed.upcoming, []);
  assert.equal(progressed.horizonEndsAt, at(26));
});
