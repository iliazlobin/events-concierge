import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import * as workspace from "../lib/admin-pipeline-workspace.ts";
import * as operations from "../lib/system-operations.ts";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../components/admin/pipeline-view.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const filters = { sourceKey: "", windowHours: 24, includeFixtures: false, status: "failed" };
const at = "2026-09-08T10:00:00Z";
const health = { generated_at: at, total: 0, sources: [] };
const fleet = {
  kind: "fleet", stages: { generated_at: at, stages: [
    { stage: "collect", evidence_status: "measured", runs_with_evidence: 2, observations: 3, total_ms: 0, avg_ms: 0, p95_ms: 0, failed_count: 1, pct_of_wall: 0 },
    { stage: "extract_enrich", evidence_status: "not_separately_instrumented", folded_into: "adapter_boundary", runs_with_evidence: 0, observations: 0, total_ms: 0, avg_ms: null, p95_ms: null, failed_count: 0, pct_of_wall: 0 },
  ] },
  fleet: { runs: 2, candidates: 120, canonicals: 100, failed: 1, running: 0 },
  throughput: { generated_at: "2026-09-08T10:10:00Z", bucket_hours: 1, buckets: [{ bucket_start: at, runs: 2, succeeded: 1, failed: 1, collected: 120, published: 100 }] },
  health,
};

function render({ snapshot = fleet, failed = false, selection = "collect", panel = "overview", viewFilters = filters } = {}) {
  const calls = { windows: [], runs: [], sorts: [], sources: [], selections: [], panels: [], inspectorOptions: null, actions: [] };
  const node = (tag) => ({ children }) => React.createElement(tag, null, children);
  const kit = {
    kit: new Proxy({}, { get: (_, name) => name }),
    Action: ({ children, onClick, disabled }) => { calls.actions.push({ label: React.Children.toArray(children).filter((part) => typeof part === "string").join(""), children, onClick }); return React.createElement("button", { disabled }, children); },
    Chip: node("span"), Section: ({ title, scope, children }) => React.createElement("section", null, title, " ", scope, children),
    PageHead: ({ title, sub, actions }) => React.createElement("header", null, title, sub, actions),
    Segment: ({ value, onChange }) => { calls.changeWindow = onChange; return React.createElement("span", null, value); },
    Metrics: ({ items, onSelect }) => { calls.metrics = { items, onSelect }; return React.createElement("dl", null, items.map((item) => React.createElement("div", { key: item.key }, item.label, ": ", item.value))); },
    LoadState: ({ failed: error }) => React.createElement("div", null, error ? "Load failed" : "Loading"),
    age: (value) => value ? "recently" : "never", stamp: (value) => value ?? "—", int: String,
    ms: (value) => value === null || value === undefined ? "—" : `${value} ms`,
  };
  const module = { exports: {} };
  const dependency = (name) => {
    if (name === "./console-kit") return kit;
    if (name === "./use-admin-snapshot") return { useAdminSnapshot: () => ({ data: snapshot, failed, loading: false, refresh() {} }) };
    if (name === "./use-admin-inspector") return { useAdminInspector: (options) => { calls.inspectorOptions = options; return { selection, panel, select: (value) => calls.selections.push(value), setPanel: (value) => calls.panels.push(value), inspectorRef: { current: null } }; } };
    if (name.endsWith(".module.css")) return { default: new Proxy({}, { get: (_, key) => key }) };
    if (name === "@/lib/admin-api") return {};
    if (name === "@/lib/admin-pipeline-workspace") return workspace;
    if (name === "@/lib/system-operations") return operations;
    return require(name);
  };
  new Function("require", "module", "exports", compiled)(dependency, module, module.exports);
  const html = renderToStaticMarkup(React.createElement(module.exports.PipelineView, {
    filters: viewFilters, onFiltersChange: (next) => calls.windows.push(next), onOpenRuns: (next, sortBy) => { calls.runs.push(next); calls.sorts.push(sortBy); }, onOpenSource: (key) => calls.sources.push(key),
  }));
  return { html, calls };
}

test("five selectable stages and persistent inspector share bookmarked stage and panel state", () => {
  const { html, calls } = render();
  assert.equal((html.match(/aria-controls="pipeline-inspector"/g) ?? []).length, 5);
  assert.match(html, /<aside[^>]+id="pipeline-inspector"[^>]+tabindex="-1"/);
  assert.match(html, /Stage inspector view/);
  assert.equal(calls.inspectorOptions.selectionParam, "pipeline_stage");
  assert.equal(calls.inspectorOptions.panelParam, "pipeline_inspector");
  calls.changeWindow("168");
  assert.deepEqual(calls.windows, [{ ...workspace.pipelineRunScope(filters), windowHours: 168 }]);
  calls.actions.find((action) => action.label.startsWith("Inspect failed runs")).onClick();
  assert.deepEqual(calls.runs, [workspace.pipelineRunScope(filters)]);
});

test("measured zero is retained while folded timing never becomes zero in inspector", () => {
  const measured = render({ panel: "evidence" }).html.split("<aside")[1];
  assert.match(measured, /Total time<\/dt><dd>0 ms/);
  const folded = render({ selection: "extract_enrich", panel: "evidence" }).html.split("<aside")[1];
  assert.match(folded, /Total time<\/dt><dd>Unavailable/);
  assert.doesNotMatch(folded, /0 ms/);
  const overview = render({ selection: "extract_enrich" }).html.split("<aside")[1];
  assert.match(overview, /No separate duration/);
  assert.match(overview, /adapter_boundary/);
});

test("source window keeps actual scoped records and never borrows fleet stage timing", () => {
  const snapshot = { kind: "source", health, detail: {
    generated_at: at, source: { display_name: "Library source", event_count: 7, last_succeeded_at: at, effective_status: "active", next_due_at: at },
    summary: { total_runs: 1, failed_runs: 0, running_runs: 0, candidate_count: 9, canonical_count: 8 },
    window: { bucket_hours: 1 }, history: [{ bucket_start: at, total_runs: 1, succeeded_runs: 1, failed_runs: 0, candidate_count: 9, canonical_count: 8 }],
  } };
  const { html } = render({ snapshot, panel: "evidence", viewFilters: { ...filters, sourceKey: "library" } });
  assert.match(html, /Collected: 9/);
  assert.match(html, /Failed runs: 0/);
  assert.match(html, /Aggregate source timing is unavailable/);
  assert.match(html, /Selected source · all outcomes/);
  assert.match(html, /Total time<\/dt><dd>Unavailable/);
  assert.doesNotMatch(html, /Collected: 120/);
});

test("transient refresh failure retains snapshot with explicit age rather than load error", () => {
  const { html } = render({ failed: true });
  assert.match(html, /Refresh failed\. Showing the last successful snapshot/);
  assert.match(html, /Collected: 120/);
  assert.doesNotMatch(html, /Load failed/);
  assert.match(render({ snapshot: null, failed: true }).html, /Load failed/);
});

test("fixture-inclusive totals cannot silently relabel fixture-excluded activity", () => {
  const { html } = render({ viewFilters: { ...filters, includeFixtures: true } });
  assert.match(html, /Fixtures included in run totals/);
  assert.match(html, /Fleet · fixtures excluded · all outcomes/);
  assert.match(html, /Opening a bucket also excludes fixtures/);
  assert.doesNotMatch(html, /Record flow|Catalog freshness|Record trend/);
  assert.match(html, /Source attention/);
  assert.match(html, /Run activity/);
});


test("stage actions bind the selected measured boundary and clear unrelated run outcome filters", () => {
  const { calls } = render({ panel: "evidence" });
  calls.actions.find((action) => action.label === "Slowest measured runs ").onClick();
  calls.actions.find((action) => action.label === "Failed stage outcomes ").onClick();
  assert.deepEqual(calls.runs, [
    { ...workspace.pipelineRunScope(filters), status: "", stage: "collect", stageOutcome: undefined },
    { ...workspace.pipelineRunScope(filters), status: "", stage: "collect", stageOutcome: "failed" },
  ]);
  assert.deepEqual(calls.sorts, ["stage_duration", "stage_duration"]);
  const folded = render({ selection: "extract_enrich" }).html;
  assert.doesNotMatch(folded, /Slowest measured runs|Failed stage outcomes/);
});

test("attention is a current source diagnosis and opens an unambiguous source run scope", () => {
  const source = {
    source_key: "library", display_name: "Library source", enabled: true, retired_at: null,
    run_state: "failed", freshness_state: "late", retry_state: "elevated", yield_state: "unknown",
    latest_run_error: "transport_timeout", last_success_at: at, upcoming_events: 7,
  };
  const { html, calls } = render({ snapshot: { ...fleet, health: { ...health, total: 1, sources: [source] } } });
  assert.match(html, /Latest run failed/);
  assert.match(html, /transport timeout/);
  assert.match(html, /7 upcoming events retained/);
  assert.match(html, /Independent of the run window/);
  calls.actions.find((action) => action.label === "Inspect runs ").onClick();
  assert.deepEqual(calls.runs, [{ ...workspace.pipelineRunScope(filters), status: "", sourceKey: "library" }]);
});


test("run totals navigate to matching outcomes without inheriting stage or anchored filters", () => {
  const { calls } = render({ viewFilters: { ...filters, stage: "admission", stageOutcome: "failed", startedAfter: at, startedBefore: "2026-09-08T11:00:00Z" } });
  assert.deepEqual(calls.metrics.items.filter((item) => item.interactive).map((item) => item.key), ["runs", "failed", "running"]);
  calls.metrics.onSelect("failed");
  calls.metrics.onSelect("running");
  calls.metrics.onSelect("runs");
  assert.deepEqual(calls.runs.map((next) => next.status), ["failed", "running", ""]);
  assert.ok(calls.runs.every((next) => next.stage === undefined && next.startedAfter === undefined && next.startedBefore === undefined));
});
