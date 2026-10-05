import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  adminHistoryLocationFromUrl,
  adminHistoryUrl,
  adminSourceLocationUrl,
  adminSourceFiltersFromUrl,
  adminSourceFiltersUrl,
  DEFAULT_ADMIN_SOURCE_FILTERS,
} from "../lib/admin-history.ts";
import { operationsNavigationUrl } from "../lib/admin-operations-navigation.ts";

test("source filters survive inspector links without changing System source filters", () => {
  const filters = { ...DEFAULT_ADMIN_SOURCE_FILTERS, query: "county library", state: "due", region: "Bay Area", sortBy: "last_success", sortDirection: "desc" };
  const url = adminSourceFiltersUrl(filters, "https://events.test/admin?tab=sources&source_query=system-filter&source_selection=library-events");
  assert.deepEqual(adminSourceFiltersFromUrl(new URL(url, "https://events.test").href), filters);
  assert.match(url, /source_query=system-filter/);
  assert.match(url, /source_selection=library-events/);
});

test("legacy source links reveal one canonical row without inheriting conflicting registry filters", () => {
  for (const [query, panel] of [
    ["tab=sources&source=luma-sf", "overview"],
    ["tab=catalog&catalog_config=luma-sf", "configuration"],
    ["tab=system&store=catalog&catalog_config=luma-sf", "configuration"],
  ]) {
    const original = `https://events.test/admin?${query}&registry_query=other&registry_lens=paused&registry_state=blocked&registry_publisher=Other&registry_fixtures=true&campaign=ops#evidence`;
    const legacy = adminHistoryLocationFromUrl(original);
    assert.equal(legacy.tab, "sources");
    assert.equal(legacy.sourceKey, "luma-sf");
    const normalized = new URL(adminSourceLocationUrl(legacy.sourceKey, original, legacy.sourcePanel), original);
    assert.equal(normalized.searchParams.get("source_selection"), "luma-sf");
    assert.equal(normalized.searchParams.get("source_inspector") ?? "overview", panel);
    for (const key of ["source", "catalog_config", "registry_query", "registry_lens", "registry_state", "registry_publisher"]) assert.equal(normalized.searchParams.has(key), false, key);
    assert.equal(normalized.searchParams.get("registry_fixtures"), "true");
    assert.equal(normalized.searchParams.get("campaign"), "ops");
    assert.equal(normalized.hash, "#evidence");
    assert.deepEqual(adminHistoryLocationFromUrl(normalized.href), { tab: "sources", sourceKey: null });
  }
});

test("opening a different run scope discards the old page but preserves explicit investigation identity", () => {
  const url = adminHistoryUrl({ tab: "runs", sourceKey: null, filters: { sourceKey: "new-source", status: "", windowHours: 168, includeFixtures: false } },
    "https://events.test/admin?tab=runs&run_source=old-source&window=168&run_page=7&run_selection=old-source%7Cold-run&run_inspector=stages");
  const params = new URL(url, "https://events.test").searchParams;
  assert.equal(params.get("run_source"), "new-source");
  assert.equal(params.has("run_page"), false);
  assert.equal(params.get("run_selection"), "old-source|old-run");
});

const adminConsole = readFileSync(
  new URL("../components/admin/admin-console.tsx", import.meta.url),
  "utf8",
);

test("admin history URLs round-trip every workspace and source investigation", () => {
  for (const tab of ["overview", "pipeline", "sources", "catalog", "run-stats", "runs", "commands", "models"]) {
    const url = adminHistoryUrl(
      { tab, sourceKey: null },
      "https://events.test/admin?campaign=ops#workspace",
    );
    const parsed = new URL(url, "https://events.test");
    assert.equal(parsed.searchParams.get("campaign"), "ops");
    assert.equal(adminHistoryLocationFromUrl(`https://events.test${url}`).tab, tab);
  }

  const sourceUrl = adminHistoryUrl(
    { tab: "sources", sourceKey: "luma-sf" },
    "https://events.test/admin?tab=pipeline",
  );
  assert.deepEqual(
    adminHistoryLocationFromUrl(`https://events.test${sourceUrl}`),
    { tab: "sources", sourceKey: null },
  );
  assert.equal(new URL(sourceUrl, "https://events.test").searchParams.get("source_selection"), "luma-sf");
});

test("admin workspace navigation pushes entries and popstate restores the URL state", () => {
  assert.match(adminConsole, /window\.history\.pushState\(/);
  assert.match(adminConsole, /adminHistoryUrl\(nextLocation, window\.location\.href\)/);
  assert.match(adminConsole, /window\.addEventListener\("popstate", handlePopState\)/);
  assert.match(
    adminConsole,
    /const handlePopState = \(\) => \{[\s\S]*?adminHistoryLocationFromUrl\(window\.location\.href\)[\s\S]*?setTab\(location\.tab\)[\s\S]*?setSourceFilters\(adminSourceFiltersFromUrl\(window\.location\.href\)\)/,
  );
  assert.match(adminConsole, /adminSourceLocationUrl\(sourceKey, window\.location\.href, panel\)/);
});

test("contextual workspace URLs remain valid independently of primary navigation", () => {
  const sourceHref = "https://events.test/admin?tab=sources&registry_query=county&registry_state=failed&source_selection=county-events&source_inspector=evidence";
  const commandsUrl = adminHistoryUrl({ tab: "commands", sourceKey: null }, sourceHref);
  assert.equal(adminHistoryLocationFromUrl(new URL(commandsUrl, sourceHref).href).tab, "commands");
  assert.deepEqual(adminSourceFiltersFromUrl(new URL(commandsUrl, sourceHref).href), adminSourceFiltersFromUrl(sourceHref));
  assert.equal(new URL(commandsUrl, sourceHref).searchParams.get("source_selection"), "county-events");

  for (const tab of ["runs", "pipeline"]) {
    const filters = { sourceKey: "county-events", status: "failed", windowHours: 720, includeFixtures: true };
    const contextualUrl = new URL(adminHistoryUrl({ tab, sourceKey: null, filters }, sourceHref), sourceHref);
    const overviewUrl = operationsNavigationUrl(adminHistoryUrl({ tab: "overview", sourceKey: null }, contextualUrl.href), { queue: null });
    assert.equal(adminHistoryLocationFromUrl(new URL(overviewUrl, sourceHref).href).tab, "overview");
    // Back restores the saved entry, including the contextual view's original scope.
    assert.deepEqual(adminHistoryLocationFromUrl(contextualUrl.href), { tab, sourceKey: null, filters });
  }
});

test("returning to Overview clears a previous record investigation without changing its history entry", () => {
  const contextualHref = "https://events.test/admin?tab=commands&ops_queue=request_start&ops_scope=pending&ops_offset=10&ops_record=019a7137-8b68-7bf4-b75c-f3a04c98123f&registry_query=county";
  const overviewUrl = operationsNavigationUrl(adminHistoryUrl({ tab: "overview", sourceKey: null }, contextualHref), { queue: null });
  const params = new URL(overviewUrl, contextualHref).searchParams;
  for (const key of ["tab", "ops_queue", "ops_scope", "ops_offset", "ops_record"]) assert.equal(params.has(key), false, key);
  assert.equal(params.get("registry_query"), "county");
  assert.equal(adminHistoryLocationFromUrl(contextualHref).tab, "commands");
  assert.equal(new URL(contextualHref).searchParams.get("ops_offset"), "10");
});

test("Run statistics is independent of the saved run ledger scope and retains trend controls", () => {
  const statisticsHref = "https://events.test/admin?tab=run-stats&trend_source=county-events&trend_window=720&trend_metric=records";
  assert.deepEqual(adminHistoryLocationFromUrl(statisticsHref), { tab: "run-stats", sourceKey: null });
  const filters = { sourceKey: "county-events", status: "failed", windowHours: 720, includeFixtures: false,
    startedAfter: "2026-09-08T12:00:00.123456Z", startedBefore: "2026-09-09T12:00:00.123456Z" };
  const ledgerHref = new URL(adminHistoryUrl({ tab: "runs", sourceKey: null, filters }, statisticsHref), statisticsHref).href;
  assert.deepEqual(adminHistoryLocationFromUrl(ledgerHref), { tab: "runs", sourceKey: null, filters });
  const returnHref = new URL(adminHistoryUrl({ tab: "run-stats", sourceKey: null }, ledgerHref), ledgerHref).href;
  assert.equal(returnHref, statisticsHref);
  assert.deepEqual(adminHistoryLocationFromUrl(returnHref), { tab: "run-stats", sourceKey: null });
  // Navigating back to the saved ledger entry restores its exact interval and status.
  assert.deepEqual(adminHistoryLocationFromUrl(ledgerHref).filters, filters);
});

test("Catalog and Command history return to the matching Sources context", () => {
  const sourcesHref = "https://events.test/admin?tab=sources&registry_query=county&registry_state=failed&source_selection=county-events&source_inspector=operations";
  for (const tab of ["catalog", "commands"]) {
    const contextualHref = new URL(adminHistoryUrl({ tab, sourceKey: null }, sourcesHref), sourcesHref).href;
    assert.equal(adminHistoryLocationFromUrl(contextualHref).tab, tab);
    const returnHref = new URL(adminHistoryUrl({ tab: "sources", sourceKey: null }, contextualHref), contextualHref).href;
    assert.equal(returnHref, sourcesHref);
    assert.deepEqual(adminSourceFiltersFromUrl(returnHref), adminSourceFiltersFromUrl(sourcesHref));
  }
});

test("source, window, outcome and fixture context survive reload and browser back", () => {
  for (const [tab, windowHours] of [["runs", 720], ["pipeline", 720], ["runs", 2160]]) {
    const filters = { sourceKey: "luma-sf", windowHours, status: "failed", includeFixtures: true };
    const location = { tab, sourceKey: null, filters };
    const url = adminHistoryUrl(location, "https://events.test/admin?source=old-source");
    assert.deepEqual(adminHistoryLocationFromUrl(`https://events.test${url}`), location);
    assert.equal(new URL(url, "https://events.test").searchParams.has("source"), false);
  }
});

test("invalid investigation filters cannot become unbounded API requests", () => {
  const location = adminHistoryLocationFromUrl("https://events.test/admin?tab=runs&run_source=../secret&window=999999&status=deleted&fixtures=yes");
  assert.deepEqual(location.filters, { sourceKey: "", windowHours: 168, status: "", includeFixtures: false });
});

test("time bucket and stage drilldowns survive history with an exact bounded interval", () => {
  const filters = { sourceKey: "luma-sf", windowHours: 168, status: "", includeFixtures: false,
    startedAfter: "2026-09-08T00:00:00Z", startedBefore: "2026-09-09T00:00:00Z",
    stage: "collect", stageOutcome: "failed" };
  const url = adminHistoryUrl({ tab: "runs", sourceKey: null, filters },
    "https://events.test/admin?tab=runs&run_page=8&run_stage=catalog_publish");
  assert.deepEqual(adminHistoryLocationFromUrl(`https://events.test${url}`).filters, filters);
  assert.equal(new URL(url, "https://events.test").searchParams.has("run_page"), false);
});

test("invalid, partial, naive and excessive intervals cannot become server scopes", () => {
  for (const suffix of [
    "run_after=2026-09-08T00:00:00Z", "run_before=2026-09-08T00:00:00Z",
    "run_after=2026-09-08T00:00:00&run_before=2026-09-09T00:00:00",
    "run_after=2026-09-09T00:00:00Z&run_before=2026-09-08T00:00:00Z",
    "run_after=2025-09-08T00:00:00Z&run_before=2026-09-08T00:00:00Z",
    "run_stage=arbitrary&run_stage_outcome=failed",
  ]) {
    const filters = adminHistoryLocationFromUrl(`https://events.test/admin?tab=runs&${suffix}`).filters;
    assert.equal(filters.startedAfter, undefined);
    assert.equal(filters.startedBefore, undefined);
    assert.equal(filters.stageOutcome, undefined);
  }
});

test("leaving a filtered investigation clears its URL filters without losing unrelated state", () => {
  const url = adminHistoryUrl({ tab: "overview", sourceKey: null },
    "https://events.test/admin?tab=runs&run_source=luma-sf&window=720&status=failed&fixtures=true&campaign=ops");
  assert.equal(url, "/admin?campaign=ops");
});

test("command investigations restore exact receipt, attempt, and source run", () => {
  const investigation = {
    commandId: "926b8762-6d9f-5ade-b85a-b024ceea89cb", attempt: 7,
    sourceKey: "luma-sf", runKey: "cadence:luma-sf:2026-09-08T00:00:00Z",
  };
  const url = adminHistoryUrl({ tab: "commands", sourceKey: null, investigation },
    "https://events.test/admin?source=old-source&campaign=ops#workspace");
  assert.deepEqual(adminHistoryLocationFromUrl(`https://events.test${url}`),
    { tab: "commands", sourceKey: null, investigation });
  assert.equal(new URL(url, "https://events.test").searchParams.get("campaign"), "ops");
  const exit = adminHistoryUrl({ tab: "overview", sourceKey: null }, `https://events.test${url}`);
  assert.equal(exit, "/admin?campaign=ops#workspace");
});

test("malformed command selections never become API selectors", () => {
  const base = "https://events.test/admin?tab=commands";
  assert.equal(adminHistoryLocationFromUrl(`${base}&command=../../secret`).investigation, undefined);
  const selection = adminHistoryLocationFromUrl(`${base}&command=926b8762-6d9f-5ade-b85a-b024ceea89cb&command_attempt=1e4&command_source=../private&command_run=../run`).investigation;
  assert.deepEqual(selection, { commandId: "926b8762-6d9f-5ade-b85a-b024ceea89cb" });
});

test("legacy catalog bookmarks route to Catalog without overriding an explicit workspace or source", () => {
  for (const [query, tab] of [
    ["store=catalog&store_source=luma-sf", "catalog"],
    ["tab=catalog&store_source=luma-sf", "catalog"],
    ["tab=overview&store=catalog", "overview"],
    ["tab=runs&store=catalog", "runs"],
    ["tab=run-stats&store=catalog", "run-stats"],
    ["tab=commands&store=catalog", "commands"],
    ["tab=invalid&store=catalog", "overview"],
    ["source=luma-sf&store=catalog", "sources"],
    ["source=luma-sf&tab=catalog", "sources"],
    ["source=../invalid&store=catalog", "overview"],
    ["component=collection&usecase=ingestion&focus=collection", "overview"],
    ["store=postgres&component=postgres", "overview"],
  ]) {
    assert.equal(adminHistoryLocationFromUrl(`https://events.test/admin?${query}`).tab, tab, query);
  }
});

test("workspace navigation retires diagram parameters while keeping Catalog investigations for return and history", () => {
  const obsolete = ["component", "path", "usecase", "focus", "node", "edge", "inspect", "level", "view", "source_lens", "source_query", "source_page", "store"];
  const saved = {
    store_source: "luma-sf", store_query: "Art & music", store_event: "019a7137-8b68-7bf4-b75c-f3a04c98123f",
    store_after_start: "2026-09-08T09:00:00.123456-07:00", store_after_id: "019a7137-8b68-7bf4-b75c-f3a04c981230",
    store_source_query: "Bay Area", store_source_page: "2",
  };
  const original = new URL("https://events.test/admin?campaign=ops&registry_query=luma&ops_queue=request_start#evidence");
  for (const key of obsolete) original.searchParams.set(key, key === "store" ? "catalog" : "old");
  for (const [key, value] of Object.entries(saved)) original.searchParams.set(key, value);
  assert.equal(adminHistoryLocationFromUrl(original.href).tab, "catalog");
  const catalogUrl = adminHistoryUrl({ tab: "catalog", sourceKey: null }, original.href);
  let current = new URL(catalogUrl, original).href;
  for (const tab of ["overview", "sources", "pipeline", "catalog"]) {
    const relative = adminHistoryUrl({ tab, sourceKey: null }, current);
    const url = new URL(relative, original);
    assert.equal(adminHistoryLocationFromUrl(url.href).tab, tab);
    assert.equal(url.searchParams.get("tab"), tab === "overview" ? null : tab);
    for (const key of obsolete) assert.equal(url.searchParams.has(key), false, key);
    for (const [key, value] of Object.entries(saved)) assert.equal(url.searchParams.get(key), value, key);
    assert.equal(url.searchParams.get("campaign"), "ops");
    assert.equal(url.searchParams.get("registry_query"), "luma");
    assert.equal(url.searchParams.get("ops_queue"), "request_start");
    assert.equal(url.hash, "#evidence");
    current = url.href;
  }
  assert.equal(adminHistoryLocationFromUrl(new URL(catalogUrl, original).href).tab, "catalog");
});
