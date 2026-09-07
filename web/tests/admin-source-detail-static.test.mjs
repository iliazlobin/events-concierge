import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const sourceDetail = readFileSync(
  new URL("../components/admin/source-detail.tsx", import.meta.url),
  "utf8",
);
const sourceConfiguration = readFileSync(
  new URL("../components/admin/source-configuration-editor.tsx", import.meta.url),
  "utf8",
);
const sourceCatalog = readFileSync(
  new URL("../components/admin/source-catalog-table.tsx", import.meta.url),
  "utf8",
);
const adminConsole = readFileSync(
  new URL("../components/admin/admin-console.tsx", import.meta.url),
  "utf8",
);
const adminCss = readFileSync(
  new URL("../app/admin/admin.css", import.meta.url),
  "utf8",
);

test("source investigation replaces the registry workspace instead of covering it with a drawer", () => {
  assert.match(sourceDetail, /className="admin-detail admin-detail--workspace"/);
  assert.match(sourceDetail, /aria-labelledby="admin-source-detail-title"/);
  assert.match(sourceDetail, />\s*Back to sources\s*</);
  assert.doesNotMatch(sourceDetail, /admin-detail-backdrop/);
  assert.doesNotMatch(sourceDetail, /aria-modal/);
  assert.doesNotMatch(sourceDetail, /role="dialog"/);
  assert.match(
    adminConsole,
    /<main className=\{`admin-main\$\{selectedSourceKey \? " admin-main--detail" : ""\}`\}>/,
  );
  assert.match(adminConsole, /\{selectedSourceKey \? \(\s*<SourceDetail/);
  assert.match(adminCss, /\.admin-detail\.admin-detail--workspace/);
  assert.match(adminCss, /container-name: admin-source-detail/);
  assert.match(adminCss, /@container admin-source-detail \(max-width: 900px\)/);
});

test("leaving a source investigation clears its route state", () => {
  assert.match(
    adminConsole,
    /const selectTab = \(nextTab: AdminTab\) => \{[\s\S]*?navigateAdmin\(nextTab\);/,
  );
  assert.match(adminConsole, /navigateAdmin\("sources"\)/);
});

test("source detail hands run investigation to the dedicated Runs workspace", () => {
  assert.match(sourceDetail, /className="admin-source-latest-run"/);
  assert.match(sourceDetail, /onOpenRuns\(visibleSource\.source_key\)/);
  assert.match(sourceDetail, />\s*View all runs\s*</);
  assert.match(adminConsole, /const openSourceRuns = \(sourceKey: string, failedOnly = false\)/);
  assert.match(adminConsole, /setRunView\("history"\)/);
  assert.match(adminConsole, /sourceKey,/);
  assert.doesNotMatch(sourceDetail, /admin-table--runs/);
  assert.doesNotMatch(sourceDetail, /RunExecutionEvidence/);
  assert.doesNotMatch(sourceDetail, /dangerouslySetInnerHTML/);
});

test("guarded source actions stay direct without a duplicated workbench", () => {
  assert.match(sourceDetail, /onClick=\{\(\) => onRefresh\(visibleSource\)\}/);
  assert.match(sourceDetail, /onClick=\{editConfiguration\}/);
  assert.match(sourceDetail, /onOpenRuns\(visibleSource\.source_key/);
  assert.match(sourceDetail, /scrollTo\(catalogRef\)/);
  assert.doesNotMatch(sourceDetail, />Source workbench</);
  assert.doesNotMatch(sourceDetail, /admin-source-workbench/);
  assert.doesNotMatch(adminCss, /\.admin-source-workbench/);
  assert.doesNotMatch(sourceDetail, /\bconfirm\s*\(/);
});

test("source health and pipeline prioritize compact actionable state", () => {
  assert.match(sourceDetail, /diagnostic\.tone !== "healthy"/);
  assert.match(sourceDetail, /\{actionableDiagnostics\.length \? \(/);
  assert.match(sourceDetail, /const admissionValue = !policyAllowed/);
  assert.match(sourceDetail, /latest \? runStatusLabel\(latest\) : "No run"/);
  assert.match(sourceDetail, /latest yield/);
  assert.match(sourceDetail, /admin-pipeline-stage--static/);
  assert.match(sourceDetail, /onOpenPipeline\(visibleSource\.source_key\)/);
  assert.doesNotMatch(sourceDetail, /admin-pipeline-inspector/);
  assert.doesNotMatch(sourceDetail, />Run history</);
  assert.doesNotMatch(sourceDetail, />Run evidence</);
  assert.doesNotMatch(sourceDetail, /Catalog delta/);
  assert.doesNotMatch(sourceDetail, /Window yield/);
  assert.match(
    adminCss,
    /\.admin-detail--workspace \.admin-pipeline \{[\s\S]*?grid-template-columns: repeat\(4, minmax\(0, 1fr\)\)/,
  );
  assert.match(
    adminCss,
    /\.admin-detail--workspace \.admin-pipeline-stage \{[\s\S]*?min-height: 62px/,
  );
});

test("secondary source identity is disclosed only when requested", () => {
  assert.match(sourceDetail, /<details className="admin-source-identity">/);
  assert.doesNotMatch(sourceDetail, /className="admin-build-card"/);
  assert.doesNotMatch(sourceDetail, />Average</);
  assert.match(sourceConfiguration, /<details className="admin-config-section admin-config-section--managed">/);
  assert.match(sourceConfiguration, /<summary className="admin-config-section__heading">/);
});

test("ordinary configuration editing preserves the system-managed adapter contract", () => {
  assert.match(sourceConfiguration, /System-managed adapter contract/);
  assert.match(sourceConfiguration, /mode: source\.mode/);
  assert.match(sourceConfiguration, /editRequest\?\.sourceKey !== source\.source_key/);
  assert.doesNotMatch(sourceConfiguration, /<select/);
  assert.doesNotMatch(sourceConfiguration, /onChange=.*mode/);
});

test("retired sources are terminal and navigate to their replacement", () => {
  assert.match(sourceDetail, /const retired = Boolean\(visibleSource && adminSourceIsRetired\(visibleSource\)\)/);
  assert.match(sourceDetail, /className="admin-source-retirement"/);
  assert.match(sourceDetail, />\s*Open replacement\s*</);
  assert.match(sourceDetail, /onOpenSource\(visibleSource\.superseded_by_source_key!\)/);
  assert.match(sourceDetail, /\{retired \? null : visibleSource\.review_status === "reviewed" \? \(/);
  assert.match(sourceDetail, /\{!retired \? \([\s\S]*?Queue refresh[\s\S]*?\) : null\}/);
  assert.match(sourceConfiguration, /\{!retired \? \([\s\S]*?Edit reviewed config[\s\S]*?\) : null\}/);
  assert.match(sourceConfiguration, /Retired · read-only/);
  assert.match(adminConsole, /!adminSourceIsRetired\(source\)[\s\S]*?&& source\.enabled/);
  assert.match(adminConsole, /onOpenSource\(replacementKey\)/);
  assert.match(adminCss, /\.admin-source-retirement/);
  assert.match(sourceDetail, /const actionableDiagnostics = retired\s*\? \[\]/);
});

test("catalog schedule and location are quiet links and search includes people", () => {
  assert.equal(sourceCatalog.match(/className="admin-catalog-text-link"/g)?.length, 2);
  assert.match(
    sourceCatalog,
    /placeholder="Search title, venue, city, organizer, host, speaker…"/,
  );
});

test("catalog readiness uses published-event language and groups optional metadata", () => {
  assert.match(sourceCatalog, />\s*Discovery readiness\s*</);
  assert.match(sourceCatalog, />Published-event discovery readiness</);
  assert.match(sourceCatalog, /<th>Published event<\/th>/);
  assert.match(sourceCatalog, /Published record fields and provenance/);
  assert.doesNotMatch(sourceCatalog, /<th>Canonical event<\/th>/);
  assert.doesNotMatch(sourceCatalog, />Canonical discovery readiness</);
  assert.match(sourceCatalog, /not source-specific\s+extraction/);
  assert.match(sourceCatalog, /className="admin-catalog-metadata-groups"/);
  assert.match(sourceCatalog, /catalogMetadataGroups\(event\)/);
  assert.match(sourceCatalog, /admin-catalog-profile-links/);
  assert.doesNotMatch(sourceCatalog, /FIELD_COUNT/);
  assert.doesNotMatch(sourceCatalog, /Field coverage ·/);
  assert.match(adminCss, /\.admin-catalog-metadata-groups/);
});

test("source detail uses collected and published pipeline vocabulary", () => {
  assert.match(sourceDetail, /No collected records/);
  assert.match(sourceDetail, /collected records/);
  assert.match(sourceDetail, /No published records/);
  assert.match(sourceDetail, /published records/);
  assert.match(sourceDetail, /<dt>Collected → published<\/dt>/);
  assert.doesNotMatch(sourceDetail, /No candidate output/);
  assert.doesNotMatch(sourceDetail, /\} candidates`/);
});
