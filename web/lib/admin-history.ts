import type { AdminRunFilters, AdminSourceFilters, AdminTab } from "@/lib/admin-types";

const ADMIN_TABS = new Set<AdminTab>([
  "overview",
  "pipeline",
  "sources",
  "catalog",
  "run-stats",
  "runs",
  "commands",
]);
const SOURCE_KEY = /^[a-z0-9][a-z0-9-]{1,79}$/;
const COMMAND_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RUN_KEY = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$/;
const OBSOLETE_DIAGRAM_PARAMS = [
  "component", "path", "usecase", "focus", "node", "edge", "inspect", "level", "view",
  "source_lens", "source_query", "source_page", "store",
] as const;

export interface AdminCommandSelection {
  commandId: string;
  attempt?: number;
  sourceKey?: string;
  runKey?: string;
}

export function commandSelectionFromUrl(url: URL): AdminCommandSelection | undefined {
  const commandId = url.searchParams.get("command") ?? "";
  if (!COMMAND_ID.test(commandId)) return undefined;
  const rawAttempt = url.searchParams.get("command_attempt") ?? "";
  const attempt = Number(rawAttempt);
  const sourceKey = url.searchParams.get("command_source") ?? "";
  const runKey = url.searchParams.get("command_run") ?? "";
  return {
    commandId: commandId.toLowerCase(),
    ...(/^\d+$/.test(rawAttempt) && attempt >= 1 && attempt <= 10_000 ? { attempt } : {}),
    ...(SOURCE_KEY.test(sourceKey) ? { sourceKey, ...(RUN_KEY.test(runKey) ? { runKey } : {}) } : {}),
  };
}

export interface AdminHistoryLocation {
  tab: AdminTab;
  /** Legacy full-page source links are normalized into the Sources row expansion. */
  sourceKey: string | null;
  sourcePanel?: "overview" | "configuration";
  filters?: AdminRunFilters;
  investigation?: AdminCommandSelection;
}

export const DEFAULT_ADMIN_RUN_FILTERS: AdminRunFilters = {
  sourceKey: "", status: "", windowHours: 168, includeFixtures: false,
};

export const DEFAULT_ADMIN_SOURCE_FILTERS: AdminSourceFilters = {
  query: "", state: "all", mode: "", publisher: "", region: "", includeFixtures: false,
  sortBy: "source", sortDirection: "asc",
};

export function adminSourceFiltersFromUrl(href: string): AdminSourceFilters {
  const params = new URL(href).searchParams;
  const text = (key: string) => (params.get(`registry_${key}`) ?? "").replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 160);
  const state = text("state");
  const sort = text("sort");
  return {
    query: text("query"), mode: text("mode"), publisher: text("publisher"), region: text("region"),
    state: ["all", "active", "due", "blocked", "failed"].includes(state) ? state as AdminSourceFilters["state"] : "all",
    includeFixtures: text("fixtures") === "true",
    sortBy: ["source", "health", "catalog", "catalog_total", "last_success", "latest_run", "output"].includes(sort) ? sort as AdminSourceFilters["sortBy"] : "source",
    sortDirection: text("direction") === "desc" ? "desc" : "asc",
  };
}

export function adminSourceFiltersUrl(filters: AdminSourceFilters, href: string): string {
  const url = new URL(href);
  for (const [key, value, defaultValue] of [
    ["query", filters.query, ""], ["state", filters.state, "all"], ["mode", filters.mode, ""],
    ["publisher", filters.publisher, ""], ["region", filters.region, ""],
    ["fixtures", String(filters.includeFixtures), "false"], ["sort", filters.sortBy, "source"],
    ["direction", filters.sortDirection, "asc"],
  ]) {
    if (value === defaultValue) url.searchParams.delete(`registry_${key}`);
    else url.searchParams.set(`registry_${key}`, value);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

export function adminRunFiltersFromUrl(url: URL): AdminRunFilters {
  const source = url.searchParams.get("run_source") ?? "";
  const status = url.searchParams.get("status") ?? "";
  const hours = Number(url.searchParams.get("window"));
  const after = url.searchParams.get("run_after") ?? "";
  const before = url.searchParams.get("run_before") ?? "";
  const aware = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;
  const span = Date.parse(before) - Date.parse(after);
  const validInterval = aware.test(after) && aware.test(before) && span > 0 && span <= 90 * 86400_000;
  const stage = url.searchParams.get("run_stage");
  const validStage = ["admission", "collect", "extract_enrich", "normalize_dedupe", "catalog_publish"].includes(stage ?? "");
  return {
    sourceKey: SOURCE_KEY.test(source) ? source : "",
    status: ["succeeded", "failed", "running", "paused"].includes(status)
      ? status as AdminRunFilters["status"] : "",
    windowHours: [24, 168, 336, 720, 2160].includes(hours) ? hours : 168,
    includeFixtures: url.searchParams.get("fixtures") === "true",
    ...(validInterval ? { startedAfter: after, startedBefore: before } : {}),
    ...(validStage ? { stage: stage as AdminRunFilters["stage"],
      ...(url.searchParams.get("run_stage_outcome") === "failed" ? { stageOutcome: "failed" as const } : {}),
    } : {}),
  };
}

export function adminHistoryLocationFromUrl(currentHref: string): AdminHistoryLocation {
  const url = new URL(currentHref);
  const requestedTab = url.searchParams.get("tab");
  const legacyCatalog = requestedTab === "catalog" || ((requestedTab === null || requestedTab === "system") && url.searchParams.get("store") === "catalog");
  const configurationSource = legacyCatalog ? url.searchParams.get("catalog_config") : null;
  const requestedSource = url.searchParams.get("source") ?? configurationSource;
  const sourceKey = requestedSource && SOURCE_KEY.test(requestedSource)
    ? requestedSource
    : null;
  const tab = sourceKey
      ? "sources"
      : requestedTab && ADMIN_TABS.has(requestedTab as AdminTab)
        ? requestedTab as AdminTab
        : (requestedTab === null || requestedTab === "system") && requestedSource === null && url.searchParams.get("store") === "catalog"
          ? "catalog"
          : "overview";
  return {
    tab,
    sourceKey,
    ...(sourceKey && (configurationSource === sourceKey || url.searchParams.get("source_inspector") === "configuration") ? { sourcePanel: "configuration" as const } : {}),
    ...(tab === "runs" || tab === "pipeline" ? { filters: adminRunFiltersFromUrl(url) } : {}),
    ...(tab === "commands" && commandSelectionFromUrl(url) ? { investigation: commandSelectionFromUrl(url) } : {}),
  };
}

export function adminHistoryUrl(
  location: AdminHistoryLocation,
  currentHref: string,
): string {
  const url = new URL(currentHref);
  for (const key of OBSOLETE_DIAGRAM_PARAMS) url.searchParams.delete(key);
  if (location.tab === "overview") url.searchParams.delete("tab");
  else url.searchParams.set("tab", location.tab);
  url.searchParams.delete("source");
  url.searchParams.delete("catalog_config");
  if (location.sourceKey && SOURCE_KEY.test(location.sourceKey)) {
    url.searchParams.set("tab", "sources");
    url.searchParams.set("source_selection", location.sourceKey);
    if (location.sourcePanel === "configuration") url.searchParams.set("source_inspector", "configuration");
    else url.searchParams.delete("source_inspector");
    // A source link must reveal its target instead of silently inheriting a conflicting filter.
    for (const key of ["query", "state", "mode", "publisher", "region", "lens"]) url.searchParams.delete(`registry_${key}`);
  }
  for (const key of ["run_source", "status", "window", "fixtures", "run_after", "run_before", "run_stage", "run_stage_outcome"]) url.searchParams.delete(key);
  for (const key of ["command", "command_attempt", "command_source", "command_run"]) url.searchParams.delete(key);
  if (location.tab === "commands" && location.investigation) {
    const selection = location.investigation;
    url.searchParams.set("command", selection.commandId);
    if (selection.attempt) url.searchParams.set("command_attempt", String(selection.attempt));
    if (selection.sourceKey) url.searchParams.set("command_source", selection.sourceKey);
    if (selection.runKey) url.searchParams.set("command_run", selection.runKey);
  }
  if ((location.tab === "runs" || location.tab === "pipeline") && location.filters) {
    const filters = location.filters;
    const previous = adminRunFiltersFromUrl(new URL(currentHref));
    if (location.tab === "runs" && (previous.sourceKey !== filters.sourceKey
      || previous.status !== filters.status || previous.windowHours !== filters.windowHours
      || previous.includeFixtures !== filters.includeFixtures
      || previous.startedAfter !== filters.startedAfter || previous.startedBefore !== filters.startedBefore
      || previous.stage !== filters.stage || previous.stageOutcome !== filters.stageOutcome)) {
      url.searchParams.delete("run_page");
    }
    if (filters.sourceKey) url.searchParams.set("run_source", filters.sourceKey);
    if (filters.status) url.searchParams.set("status", filters.status);
    url.searchParams.set("window", String(filters.windowHours));
    if (filters.includeFixtures) url.searchParams.set("fixtures", "true");
    if (filters.startedAfter && filters.startedBefore) {
      url.searchParams.set("run_after", filters.startedAfter);
      url.searchParams.set("run_before", filters.startedBefore);
    }
    if (filters.stage) {
      url.searchParams.set("run_stage", filters.stage);
      if (filters.stageOutcome) url.searchParams.set("run_stage_outcome", filters.stageOutcome);
    }
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

export function adminSourceLocationUrl(sourceKey: string, href: string, panel: "overview" | "configuration" = "overview"): string {
  return adminHistoryUrl({ tab: "sources", sourceKey, sourcePanel: panel }, href);
}
