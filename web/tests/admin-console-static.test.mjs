import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const adminConsole = readFileSync(
  new URL("../components/admin/admin-console.tsx", import.meta.url),
  "utf8",
);
const adminCss = readFileSync(
  new URL("../app/admin/admin.css", import.meta.url),
  "utf8",
);
const adminSourceBulk = readFileSync(
  new URL("../lib/admin-source-bulk.ts", import.meta.url),
  "utf8",
);
const adminTypes = readFileSync(
  new URL("../lib/admin-types.ts", import.meta.url),
  "utf8",
);
const adminApi = readFileSync(
  new URL("../lib/admin-api.ts", import.meta.url),
  "utf8",
);
const runExecutionEvidence = readFileSync(
  new URL("../components/admin/run-execution-evidence.tsx", import.meta.url),
  "utf8",
);
const adminProxy = readFileSync(
  new URL("../app/admin/v1/[...path]/route.ts", import.meta.url),
  "utf8",
);
const runsPanel = adminConsole.slice(
  adminConsole.indexOf("function RunsPanel("),
  adminConsole.indexOf("\nfunction commandDetailElapsed("),
);

test("capped operating trends identify every aggregate surface as a partial sample", () => {
  assert.match(adminConsole, /const runDataPartial = runs\.length < runTotal/);
  assert.match(adminConsole, />Partial run sample\.</);
  assert.match(adminConsole, /partial \? "Loaded sample" : "Window total"/);
  assert.match(adminConsole, /partial \? "Sample success" : "Success"/);
  assert.doesNotMatch(adminConsole, /Exact ingestion run data/);
});

test("the prerendered overview never derives chart buckets from the ambient clock", () => {
  assert.match(
    adminConsole,
    /overview\?\.generated_at\s*\?\s*buildRunBuckets\(runs, overview\.generated_at, runWindow\)\s*:\s*\[\]/,
  );
  assert.match(adminConsole, />Loading run history…</);
  assert.doesNotMatch(
    adminConsole,
    /buildRunBuckets\(runs, overview\?\.generated_at \?\? null, runWindow\)/,
  );
});

test("command relaunch controls disclose fresh scope and honor current eligibility", () => {
  assert.match(adminConsole, /sourceIsCommandEligible/);
  assert.match(adminConsole, /submitting \|\| !policyAllowed \|\| !sourceEligible/);
  assert.match(adminConsole, /creates a new command ID/);
  assert.match(adminConsole, /\? "Refresh currently due"\s*: "Launch again"/);
});

test("command status filters expose their selected state", () => {
  assert.match(adminConsole, /aria-label="Filter commands by status"/);
  assert.match(adminConsole, /aria-pressed=\{status === value\}/);
});

test("the command ledger has an intermediate layout before the sidebar collapses", () => {
  assert.match(adminCss, /@media \(min-width: 761px\) and \(max-width: 980px\)/);
  assert.match(adminCss, /grid-template-columns: 90px minmax\(0, 1fr\) 34px/);
});

test("small command disclosures do not preserve the desktop identity gutter", () => {
  assert.match(
    adminCss,
    /@media \(max-width: 760px\) \{\s*\.admin-command-evidence \{\s*padding: 14px 12px 16px;/,
  );
});

test("command records are full-row accessible disclosures with quiet nested actions", () => {
  assert.match(adminConsole, /className="admin-command-ledger__trigger"/);
  assert.match(adminConsole, /aria-expanded=\{expanded\}/);
  assert.match(adminConsole, /aria-controls=\{evidenceId\}/);
  assert.match(adminConsole, /role="region"/);
  assert.match(adminConsole, /aria-labelledby=\{triggerId\}/);
  assert.match(adminConsole, /className="admin-command-evidence__actions"/);
  assert.match(adminConsole, /!visible\.some\(\(command\) => command\.command_id === expandedCommandId\)/);
  assert.doesNotMatch(adminConsole, /className="admin-command-ledger__actions"/);
  assert.doesNotMatch(adminConsole, />\s*Inspect\s*<ChevronDown/);
  assert.match(adminCss, /\.admin-command-ledger__trigger:focus-visible/);
  assert.match(adminCss, /\.admin-command-evidence__actions button:hover:not\(:disabled\)/);
});

test("run records are full-row keyboard-accessible disclosures without a visual action button", () => {
  assert.match(runsPanel, /className="admin-run-row__trigger"/);
  assert.match(runsPanel, /id=\{triggerId\}/);
  assert.match(runsPanel, /aria-expanded=\{expanded\}/);
  assert.match(runsPanel, /aria-controls=\{evidenceId\}/);
  assert.match(
    runsPanel,
    /\{expanded \? "Collapse" : "Expand"\}[\s\S]*run evidence for/,
  );
  assert.match(runsPanel, /role="region"/);
  assert.match(runsPanel, /aria-labelledby=\{triggerId\}/);
  assert.doesNotMatch(runsPanel, /className="admin-table-action"/);
  assert.doesNotMatch(runsPanel, /\{expanded \? "Hide" : "Details"\}/);
  assert.match(
    adminCss,
    /\.admin-run-row__trigger\s*\{[^}]*inset:\s*0;[^}]*position:\s*absolute;/s,
  );
  assert.match(adminCss, /\.admin-run-row__trigger:focus-visible/);
});

test("expanded commands lead with outcomes and progressively disclose technical evidence", () => {
  assert.match(adminConsole, /className="admin-command-outcome"/);
  assert.match(adminConsole, /className="admin-command-execution"/);
  assert.match(adminConsole, /commandExecutionPresentation\(command\)/);
  assert.match(adminConsole, /Live child progress is unavailable for this receipt/);
  assert.match(adminConsole, /className="admin-command-outcome__bar"/);
  assert.match(adminConsole, /className="admin-command-timeline"/);
  assert.match(adminConsole, /className="admin-command-technical"/);
  assert.match(adminConsole, />\s*Technical details\s*</);
  assert.match(adminConsole, />\s*Worker claimed\s*</);
  assert.match(adminConsole, /Open recent source runs/);
  assert.doesNotMatch(adminConsole, /admin-command-evidence__facts/);
  assert.doesNotMatch(adminConsole, /admin-command-evidence__identity/);
  assert.match(adminCss, /\.admin-command-technical\[open\] summary svg/);
  assert.match(adminCss, /\.admin-command-outcome__bar > span\[data-tone="danger"\]/);
  assert.match(adminCss, /\.admin-command-execution/);
});

test("expanded commands fetch and poll authoritative linked source-run progress", () => {
  assert.match(adminTypes, /export interface AdminCommandDetail/);
  assert.match(adminTypes, /worker_state: AdminCommandWorkerState \| null/);
  assert.match(adminTypes, /active_source_key: string \| null/);
  assert.match(adminTypes, /runs: AdminCommandLinkedRun\[\]/);
  assert.match(adminApi, /export function getAdminCommandDetail/);
  assert.match(
    adminApi,
    /`\/admin\/v1\/ingestion\/commands\/\$\{encodeURIComponent\(commandId\)\}`/,
  );
  assert.match(adminConsole, /getAdminCommandDetail\(commandId, request\.signal\)/);
  assert.match(adminConsole, /shouldPollAdminCommandDetail/);
  assert.match(adminConsole, /window\.setTimeout/);
  assert.match(adminConsole, /activeRequest\?\.abort\(\)/);
  assert.doesNotMatch(adminConsole, /setInterval\(\(\) => void loadDetail/);
  assert.match(adminConsole, /<CommandLiveExecution detail=\{commandDetail\} \/>/);
  assert.match(adminConsole, />Linked source runs</);
  assert.match(adminConsole, />Current source</);
  assert.match(adminConsole, />Last state change</);
  assert.match(adminConsole, />Command attempt</);
  assert.match(adminConsole, /Live child progress is unavailable for this receipt/);
  assert.doesNotMatch(adminConsole, />Raw logs</);
  assert.match(adminCss, /\.admin-command-live__progress/);
  assert.match(adminCss, /\.admin-command-children li\[data-state="running"\]/);
});

test("the same-origin admin proxy admits a bounded UUID command-detail route", () => {
  assert.match(adminProxy, /const commandId = \^?\/\^\[0-9a-f\]/);
  assert.match(adminProxy, /path\[1\] === "commands" && commandId\.test\(path\[2\]\)/);
});

test("contained historical command failures do not present as active incidents", () => {
  assert.match(adminConsole, /commandReviewDisposition\(command, sources\)/);
  assert.match(adminConsole, /sourceProjectionsReady/);
  assert.match(adminConsole, /data-review-disposition=\{reviewDisposition\}/);
  assert.match(adminConsole, /Contained \/ historical/);
  assert.match(adminConsole, /const canOfferRelaunch = !containedHistorical/);
  assert.match(adminConsole, /commandOutcomePresentation\(command, \{ containedHistorical \}\)/);
});

test("source fleet controls use explicit selection and disclose bounded scope", () => {
  assert.match(adminConsole, /aria-label="Bulk source controls"/);
  assert.match(adminConsole, /Select all \$\{formatNumber\(eligibleSources\.length\)\} matching/);
  assert.match(adminConsole, /Bulk actions affect this page only\./);
  assert.match(adminConsole, /Resume \$\{formatNumber\(selectedPaused\)\}/);
  assert.match(adminConsole, /Pause \$\{formatNumber\(selectedEnabled\)\}/);
  assert.match(adminSourceBulk, /expected_revision: source\.source_revision/);
  assert.match(adminCss, /\.admin-source-bulk-bar/);
  assert.doesNotMatch(adminConsole, />Enable all</);
});

test("the same-origin admin proxy admits the bounded bulk enabled-state route", () => {
  assert.match(adminProxy, /path\.length === 4/);
  assert.match(adminProxy, /path\[2\] === "bulk"/);
  assert.match(adminProxy, /path\[3\] === "enabled"/);
});

test("source registry columns expose one accessible server-backed sort contract", () => {
  assert.match(adminConsole, /function SourceSortHeader/);
  assert.match(adminConsole, /<th aria-sort=\{ariaSort\}>/);
  assert.match(adminConsole, /field="source" label="Source"/);
  assert.match(adminConsole, /field="health" label="Health"/);
  assert.match(adminConsole, /field="catalog" label="Catalog"/);
  assert.match(adminConsole, /field="last_success" label="Last success"/);
  assert.match(adminConsole, /field="latest_run" label="Latest run"/);
  assert.match(adminConsole, /field="output" label="Output"/);
  assert.match(adminConsole, /setSelectedSourceKeys\(new Set\(\)\);\s*onFiltersChange\(next\);/);
  assert.match(adminCss, /\.admin-source-sort:focus-visible/);
});

test("each workspace is a dedicated view backed by server-computed aggregates", () => {
  assert.match(adminTypes, /"overview" \| "pipeline" \| "sources" \| "runs" \| "commands"/);
  assert.match(adminConsole, /value: "pipeline", label: "Pipeline", icon: Workflow/);

  // Every tab renders its own component rather than an inline panel, so a view can be reasoned
  // about without loading the whole console file.
  assert.match(adminConsole, /<SystemView/);
  assert.match(adminConsole, /<PipelineView \/>/);
  assert.match(adminConsole, /<RunsView onOpenSource=\{openSource\} \/>/);
  assert.match(adminConsole, /<CommandsView/);

  // The pipeline view reads rollups; it must never page the run ledger to build a chart.
  const pipelineView = readFileSync(
    new URL("../components/admin/pipeline-view.tsx", import.meta.url),
    "utf8",
  );
  assert.match(pipelineView, /getAdminStageSummary/);
  assert.match(pipelineView, /getAdminThroughput/);
  assert.doesNotMatch(pipelineView, /getAllAdminRuns/);
});

test("pipeline stays aggregate while run evidence lives in the dedicated Runs workspace", () => {
  assert.match(adminConsole, />01 · Admission</);
  assert.match(adminConsole, />02 · Collect</);
  assert.match(adminConsole, />03 · Extract \+ enrich</);
  assert.match(adminConsole, />04 · Normalize \+ dedupe</);
  assert.match(adminConsole, />05 · Catalog \+ publish</);
  assert.match(adminConsole, /selected run window/);
  assert.match(adminConsole, /shown separately as a live snapshot/);
  assert.match(
    adminConsole,
    /data-stage="catalog">[\s\S]*?<strong>\{formatNumber\(summary\.canonicalCount\)\}<\/strong>/,
  );
  assert.match(adminConsole, /className="admin-pipeline-catalog-snapshot"/);
  assert.match(adminConsole, /formatNumber\(summary\.currentCatalogEvents\)/);
  assert.match(adminConsole, /not a sum of the selected run window/);
  assert.match(adminConsole, /\[\["candidate", "Collected"\], \["canonical", "Published"\]\]/);
  assert.match(adminCss, /\.admin-pipeline-catalog-snapshot/);
  assert.match(adminConsole, /className="admin-pipeline-trend"/);
  assert.doesNotMatch(adminConsole, /className=\{`admin-pipeline-ledger/);
  assert.doesNotMatch(adminConsole, /className="admin-pipeline-evidence-row"/);
  assert.match(adminConsole, /value: "runs", label: "Runs", icon: FileClock/);
  assert.match(adminConsole, /<h1>Runs<\/h1>/);
  assert.match(adminConsole, /aria-expanded=\{expanded\}/);
  assert.match(adminConsole, /<RunEvidence/);
});

test("the Runs workspace paginates its full evidence set without losing filter semantics", () => {
  assert.match(adminConsole, /const RUNS_PAGE_SIZE = 50/);
  assert.match(
    adminConsole,
    /presentedRuns\.slice\(pageStart, pageStart \+ RUNS_PAGE_SIZE\)/,
  );
  assert.match(adminConsole, /visibleRuns\.map\(\(\{ run, disposition \}\) =>/);
  assert.doesNotMatch(adminConsole, /presentedRuns\.map\(\(\{ run, disposition \}\) =>/);
  assert.match(adminConsole, /aria-label="Runs pagination"/);
  assert.match(adminConsole, /aria-label="Previous runs page"/);
  assert.match(adminConsole, /aria-label="Next runs page"/);
  assert.match(
    adminConsole,
    /filters\.includeFixtures,[\s\S]*filters\.sourceKey,[\s\S]*filters\.status,[\s\S]*filters\.windowHours,[\s\S]*view,/,
  );
  assert.match(adminConsole, /setExpandedRunKey\(null\);[\s\S]*setCurrentPage\(0\);/);
  assert.match(adminCss, /\.admin-run-pager \{/);
});

test("expanded runs prioritize a bounded structured execution log", () => {
  assert.match(adminTypes, /export interface AdminRunTimelineEntry/);
  assert.match(adminTypes, /complete: false/);
  assert.match(runExecutionEvidence, />Execution log</);
  assert.match(runExecutionEvidence, /Retained structured evidence · incomplete/);
  assert.match(runExecutionEvidence, /run\.timeline\.entries\.map/);
  assert.match(runExecutionEvidence, /<details className="admin-run-technical">/);
  assert.doesNotMatch(runExecutionEvidence, />Structured execution trace</);
  assert.doesNotMatch(runExecutionEvidence, />Active worker</);
  assert.match(adminConsole, /<th>Output<\/th>/);
  assert.doesNotMatch(adminConsole, /<th>Revision evidence<\/th>/);
});

test("run evidence presents collected and published record counts to operators", () => {
  assert.match(runExecutionEvidence, />Collected records to published records</);
  assert.match(runExecutionEvidence, /<dt>Collected records<\/dt>/);
  assert.match(runExecutionEvidence, /<dt>Published records<\/dt>/);
  assert.doesNotMatch(runExecutionEvidence, />Candidates to canonical events</);
  assert.doesNotMatch(runExecutionEvidence, /<dt>Candidate input<\/dt>/);
  assert.doesNotMatch(runExecutionEvidence, /<dt>Canonical output<\/dt>/);
});

test("the sidebar footer is an operator menu, not a duplicate navigation surface", () => {
  assert.match(adminConsole, /<details className="admin-operator-menu">/);
  assert.match(adminConsole, />Local operator</);
  assert.match(adminConsole, />Refresh access</);
  assert.match(adminConsole, />Authentication</);
  assert.match(adminConsole, />Not configured</);
  assert.doesNotMatch(adminConsole, />Refresh data</);
  assert.doesNotMatch(adminConsole, />Consumer app</);
  assert.match(adminCss, /\.admin-operator-menu__panel/);
});
