import assert from "node:assert/strict";
import test from "node:test";
import { operatingSummary } from "../lib/admin-operating-model.ts";

const empty = { sources: null, catalog: null };
const source = (name, patch = {}) => ({ source_key: name, display_name: name, enabled: true, retired_at: null, freshness_state: "ok", ...patch });
const item = (id, evidence) => operatingSummary(evidence).find(summary => summary.id === id);

test("missing snapshots remain unknown independently of recorded zero", () => {
  assert.ok(operatingSummary(empty).every(summary => summary.value === "Unknown"));
  const catalog = { summary: { catalog_events: 0 } };
  assert.equal(item("catalog", { ...empty, catalog }).value, "0");
  assert.equal(item("sources", { ...empty, catalog }).value, "Unknown");
});

test("freshness covers enabled scheduled sources without counting paused or retired sources", () => {
  const sources = { generated_at: "2026-09-08", total: 6, sources: [
    source("fresh"), source("late", { freshness_state: "late" }),
    source("paused", { enabled: false }), source("retired", { retired_at: "2026-01-01" }),
    source("unscheduled", { freshness_state: "not_scheduled" }), source("never", { freshness_state: "never" }),
  ] };
  const summary = item("sources", { ...empty, sources });
  assert.equal(summary.value, "1 / 3");
  assert.match(summary.detail, /enabled, scheduled sources/);
  assert.equal(item("sources", { ...empty, sources: { ...sources, sources: [] } }).value, "No scheduled sources");
});

test("catalog uses the canonical inventory aggregate, not source associations or collection output", () => {
  const evidence = { catalog: { summary: { catalog_events: 105 } }, sources: { sources: [source("one", { upcoming_events: 99 }), source("two", { upcoming_events: 99 })] } };
  const before = JSON.stringify(evidence);
  assert.equal(item("catalog", evidence).value, "105");
  assert.match(item("catalog", evidence).detail, /Upcoming unique events/);
  assert.match(item("catalog", evidence).detail, /cancelled events and fixtures excluded/);
  assert.equal(item("sources", { ...evidence, catalog: null }).value, "2 / 2");
  assert.equal(item("catalog", { ...evidence, sources: null }).value, "105");
  assert.equal(JSON.stringify(evidence), before);
  assert.doesNotMatch(JSON.stringify(operatingSummary(evidence)), /healthy|runtime available|growth/i);
});
