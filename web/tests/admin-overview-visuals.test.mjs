import assert from "node:assert/strict";
import test from "node:test";
import { overviewSourceVisuals } from "../lib/admin-overview-visuals.ts";

const source = (key, patch = {}) => ({ source_key: key, display_name: key, enabled: true,
  retired_at: null, freshness_state: "ok", upcoming_events: 0, hours_since_success: 0, ...patch });
const snapshot = sources => ({ sources, total: sources.length });

test("freshness distribution accounts for each scheduled source once and excludes other collection states", () => {
  const data = snapshot([
    source("fresh"), source("watch", { freshness_state: "warn" }),
    source("late", { freshness_state: "late", hours_since_success: 12 }),
    source("down", { freshness_state: "down", hours_since_success: 60 }),
    source("never", { freshness_state: "never" }), source("paused", { enabled: false }),
    source("retired", { retired_at: "2026-01-01" }), source("unscheduled", { freshness_state: "not_scheduled" }),
  ]);
  const before = JSON.stringify(data);
  const view = overviewSourceVisuals(data);
  assert.deepEqual(view.grouped.map(group => group.sources.length), [1, 1, 2, 1]);
  assert.equal(view.scheduled.length, 5);
  assert.equal(view.excluded, 3);
  assert.deepEqual(view.attention.map(row => row.source_key), ["never", "down", "late", "watch"]);
  assert.equal(JSON.stringify(data), before);
});

test("catalog source coverage includes retained paused sources and ranks bounded counts without adding them into a unique total", () => {
  const view = overviewSourceVisuals(snapshot([
    source("bbb", { upcoming_events: 10 }), source("aaa", { upcoming_events: 10 }),
    source("paused", { enabled: false, upcoming_events: 20 }), source("low", { upcoming_events: 1 }),
    source("last", { upcoming_events: 1 }), source("empty"),
  ]));
  assert.equal(view.withEvents, 5);
  assert.equal(view.withoutEvents, 1);
  assert.equal(view.totalSources, 6);
  assert.deepEqual(view.largestSources.map(row => row.source_key), ["paused", "aaa", "bbb", "last"]);
  assert.equal(view.largestSourceEvents, 20);
  assert.equal("totalEvents" in view, false);
});

test("missing and empty snapshots keep unknown distinct from recorded zero", () => {
  assert.equal(overviewSourceVisuals(null), null);
  const view = overviewSourceVisuals(snapshot([]));
  assert.equal(view.totalSources, 0);
  assert.equal(view.largestSourceEvents, 0);
  assert.deepEqual(view.largestSources, []);
  assert.deepEqual(view.attention, []);
});
