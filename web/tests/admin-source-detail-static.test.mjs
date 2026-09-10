import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const details = read("../components/admin/source-expanded-details.tsx");
const panel = read("../components/admin/source-configuration-panel.tsx");
const editor = read("../components/admin/source-configuration-editor.tsx");
const catalog = read("../components/admin/catalog-view.tsx");
const sourceCatalog = read("../components/admin/source-catalog-table.tsx");
const consoleSource = read("../components/admin/admin-console.tsx");

test("Sources has one inline investigation and one reviewed configuration panel", () => {
  assert.match(consoleSource, /<SourceExpandedDetails/);
  assert.match(consoleSource, /data-testid="source-expanded-row"/);
  assert.match(details, /aria-label=\{`Source details for \$\{sourceKey\}`\}/);
  assert.match(details, /<SourceConfigurationPanel/);
  assert.match(panel, /aria-label="Source configuration"/);
  for (const source of [details, panel, catalog, consoleSource]) {
    assert.doesNotMatch(source, /<SourceDetail\b|<CatalogSourceInspector\b|Full source workspace|Open full source workspace/);
  }
  assert.doesNotMatch(catalog, /SourceConfigurationPanel|SourceConfigurationEditor/);
  assert.match(sourceCatalog, /Manage source for/);
});

test("configuration edits require exact source evidence and preserve denial boundaries", () => {
  assert.match(panel, /getAdminSourceDetail\(sourceKey, 24, includeFixtures, signal\)/);
  assert.match(panel, /snapshot\.data\?\.source\.source_key === sourceKey/);
  assert.match(panel, /snapshot\.authorizationDenied/);
  assert.match(panel, /revisionChanged/);
  assert.match(panel, /verifyingSave/);
  assert.match(panel, /Retained fields are read-only/);
  assert.match(panel, /Previous configuration|previous configuration/);
});

test("source actions preserve admission and configuration draft safeguards", () => {
  assert.match(details, /Queue refresh/);
  assert.match(details, /cannotRefreshReason/);
  assert.match(details, /policyAllowed === null/);
  assert.match(details, /source\?\.policy_blocked/);
  assert.match(details, /configurationDirty/);
  assert.match(details, /if \(!cannotRefreshReason\) onQueueRefresh\(source\)/);
  assert.match(details, /source && !retired/);
  assert.doesNotMatch(details, /\bconfirm\s*\(/);
});

test("source attention and retained history hand off to the canonical workspaces", () => {
  assert.match(details, /aria-label="Source attention"/);
  assert.match(details, /sourceDiagnostics\(source, detail\)/);
  assert.match(details, /aria-label="Recent source runs"/);
  assert.match(details, /onOpenRuns\(runFilters, run\.run_key\)/);
  assert.match(details, /onOpenCatalog\(sourceKey\)/);
  assert.match(details, /collectionBucketRuns\(/);
  assert.doesNotMatch(details, /RunExecutionEvidence|dangerouslySetInnerHTML/);
});

test("reviewed configuration keeps identity and adapter fields managed", () => {
  assert.match(editor, /Identity, adapter, and policy/);
  assert.match(editor, /mode: source\.mode/);
  assert.match(editor, /expected_revision: source\.source_revision/);
  assert.match(editor, /review_acknowledged: true/);
  assert.doesNotMatch(editor, /editRequest|beginEdit|editControl|Edit reviewed config/);
  assert.match(editor, /<details className="admin-config-section admin-config-section--managed">/);
  assert.doesNotMatch(editor, /<select|onChange=.*mode/);
});

test("retired sources stay immutable and expose replacement navigation", () => {
  assert.match(details, /adminSourceIsRetired\(source\)/);
  assert.match(details, /Open replacement/);
  assert.match(details, /onOpenSource\(.*superseded_by_source_key/);
  assert.match(editor, /if \(!canConfigure \|\| retired\)/);
  assert.match(editor, /Retired · read-only/);
});

test("published catalog evidence retains quiet links and grouped optional metadata", () => {
  assert.equal(sourceCatalog.match(/className="admin-catalog-text-link"/g)?.length, 3);
  assert.match(sourceCatalog, /placeholder="Search title, venue, city, organizer, host, speaker…"/);
  assert.match(sourceCatalog, /Published-event discovery readiness/);
  assert.match(sourceCatalog, /<th>Published event<\/th>/);
  assert.match(sourceCatalog, /getAdminCatalogEvents/);
  assert.match(sourceCatalog, /event\.source_key, event\.refresh_run_key/);
  assert.match(sourceCatalog, /catalogMetadataGroups\(event\)/);
  assert.doesNotMatch(sourceCatalog, /<th>Canonical event<\/th>|Field coverage ·/);
});
