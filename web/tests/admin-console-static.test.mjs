import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const adminConsole = read("../components/admin/admin-console.tsx");
const adminCss = read("../app/admin/admin.css");
const adminSourceBulk = read("../lib/admin-source-bulk.ts");
const adminProxy = read("../app/admin/v1/[...path]/route.ts");
const runs = read("../components/admin/runs-view.tsx");
const investigation = read("../components/admin/command-investigation.tsx");

test("primary navigation includes Commands and contextual run views remain reachable", () => {
  const primaryNav = adminConsole.slice(adminConsole.indexOf("const TABS:"), adminConsole.indexOf("const CONTEXTUAL_VIEWS:"));
  assert.deepEqual([...primaryNav.matchAll(/value: "([a-z-]+)", label: "([^"]+)"/g)].map((match) => [match[1], match[2]]), [
    ["overview", "Overview"], ["sources", "Sources"], ["catalog", "Catalog"], ["run-stats", "Runs"], ["commands", "Commands"],
  ]);
  assert.match(adminConsole, /aria-label="Administration breadcrumb"/);
  assert.match(adminConsole, /runs: \{ title: "Run history", parent: "run-stats", backLabel: "Back to Runs" \}/);
  assert.match(adminConsole, /pipeline: \{ title: "Pipeline", parent: "run-stats", backLabel: "Back to Runs" \}/);
  assert.doesNotMatch(adminConsole, /commands: \{ title: "Command history", parent:/);
  assert.match(adminConsole, /selectTab\(contextualView\.parent\)/);
  assert.match(adminConsole, /const primaryTab = contextualView\?\.parent \?\? tab/);
  assert.equal([...adminConsole.matchAll(/aria-current=\{primaryTab === value \? "page" : undefined\}/g)].length, 2);
  assert.match(adminConsole, /operationsNavigationUrl\(historyUrl, \{ queue: null \}\)/);
  assert.match(adminConsole, /if \(sourceKey \|\| nextLocation\.tab === "overview"\) \{\s*window\.dispatchEvent\(new PopStateEvent\("popstate"\)\)/);
  assert.doesNotMatch(adminConsole, /onOpenCommands|Command history/);
  assert.doesNotMatch(adminConsole, /pendingCommandCount/);
});

test("the console renders the canonical workspace components without retired parallel panels", () => {
  for (const component of ["SystemView", "RunStatisticsView", "CatalogView", "PipelineView", "RunsView", "CommandsView", "SourcesPanel"]) {
    assert.match(adminConsole, new RegExp(`<${component}\\b`));
  }
  assert.doesNotMatch(adminConsole, /function (OverviewPanel|PipelinePanel|RunsPanel|CommandsPanel|RunEvidence|CommandLiveExecution)\b/);
  assert.doesNotMatch(adminConsole, /<SourceDetail\b|RunExecutionEvidence/);
  assert.match(adminConsole, /onOpenRuns=\{openRegistryRuns\}/);
  assert.match(adminConsole, /onOpenCatalog=\{openRegistryCatalog\}/);
  assert.match(adminConsole, /tab === "run-stats"[^]*?<RunStatisticsView refreshVersion=\{reloadNonce\} onOpenSource=\{openSource\} onOpenCatalog=\{openRegistryCatalog\}/);
});

test("fleet writes require an explicit complete selection and revision-bearing targets", () => {
  assert.match(adminConsole, /aria-label="Bulk source controls"/);
  assert.match(adminConsole, /Bulk actions require the complete matching registry\./);
  assert.match(adminConsole, /if \(!canEnable \|\| loading \|\| error \|\| truncated/);
  assert.match(adminConsole, /setSelectedSourceKeys\(new Set\(\)\)/);
  assert.match(adminSourceBulk, /expected_revision: source\.source_revision/);
  assert.doesNotMatch(adminConsole, />Enable all</);
});

test("source ordering exposes accessible sort state and a server-backed filter contract", () => {
  assert.match(adminConsole, /function SourceSortHeader/);
  assert.match(adminConsole, /<th aria-sort=\{ariaSort\}>/);
  assert.match(adminConsole, /field="source" label="Source"/);
  assert.match(adminConsole, /field="health" label="Health"/);
  assert.match(adminConsole, /field="catalog_total" label="All events"/);
  assert.match(adminConsole, /field="catalog" label="Upcoming events"/);
  assert.match(adminConsole, /aria-label="Sort sources"/);
});

test("the proxy retains narrow command receipt and bulk-write paths", () => {
  assert.match(adminProxy, /const commandId = \^?\/\^\[0-9a-f\]/);
  assert.match(adminProxy, /path\[1\] === "commands" && commandId\.test\(path\[2\]\)/);
  assert.match(adminProxy, /path\.length === 4/);
  assert.match(adminProxy, /path\[2\] === "bulk"/);
  assert.match(adminProxy, /path\[3\] === "enabled"/);
});

test("Pipeline reads server rollups without downloading the run ledger", () => {
  const pipeline = read("../components/admin/pipeline-view.tsx");
  assert.match(pipeline, /getAdminStageSummary/);
  assert.match(pipeline, /getAdminThroughput/);
  assert.doesNotMatch(pipeline, /getAllAdminRuns/);
});

test("Runs reads server pages and keeps structured evidence explicitly incomplete", () => {
  assert.match(runs, /getAdminRuns\(/);
  assert.match(runs, /limit: pageSize, offset, query/);
  assert.match(runs, /aria-label="Runs pagination"/);
  assert.match(runs, /The last successful snapshot is retained/);
  assert.match(runs, /retained entries · incomplete evidence/);
  assert.match(runs, /runTimelineEntries\(run\.timeline\?\.entries/);
  assert.doesNotMatch(runs, /getAllAdminRuns|Raw logs/);
});

test("Commands follows authoritative detail and event cursors through shared polling", () => {
  assert.match(investigation, /getAdminCommandDetail\(selection\.commandId, signal\)/);
  assert.match(investigation, /getAdminCommandInvestigation\(/);
  assert.match(investigation, /startAdminPolling\(/);
  assert.match(investigation, /shouldPollAdminCommandDetail/);
  assert.match(investigation, /Separate from a lease heartbeat/);
  assert.doesNotMatch(investigation, />Raw logs</);
});

test("operator identity and authentication remain in one footer menu", () => {
  assert.match(adminConsole, /<details className="admin-operator-menu">/);
  assert.match(adminConsole, /operator\?\.subject/);
  assert.match(adminConsole, />Refresh access</);
  assert.match(adminConsole, /operator\?\.authentication/);
  assert.match(adminCss, /\.admin-operator-menu__panel/);
});
