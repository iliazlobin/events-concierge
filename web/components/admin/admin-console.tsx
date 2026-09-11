"use client";

import {
Activity,
ArrowDown,
ArrowRight,
ArrowUp,
CheckCircle2,
ChevronDown,
ChevronLeft,
ChevronRight,
ChevronsUpDown,
CircleAlert,
Command as CommandIcon,
Database,
FileClock,
Filter,
Layers3,
ListFilter,
LoaderCircle,
Pause,
Power,
RefreshCw,
Search,
X
} from "lucide-react";
import { Fragment,useCallback,useEffect,useMemo,useRef,useState } from "react";

import { CommandsView } from "@/components/admin/commands-view";
import { PipelineView } from "@/components/admin/pipeline-view";
import { RunsView } from "@/components/admin/runs-view";
import { RunStatisticsView } from "@/components/admin/run-statistics-view";
import { SystemView } from "@/components/admin/system-view";
import {
enqueueAdminCommand,
getAdminCommandDetail,
getAdminCommands,
getAdminFilters,
getAdminOperatorSession,
getAdminOverview,
getAllAdminSources,
setAdminSourceEnabled,
setAdminSourcesEnabled
} from "@/lib/admin-api";
import { catalogLocationUrl, type CatalogDateScope } from "@/lib/admin-catalog-navigation";
import {
readAdminCommandProgress
} from "@/lib/admin-command-polling";
import {
adminHistoryLocationFromUrl,
adminHistoryUrl,
adminSourceFiltersFromUrl,
adminSourceFiltersUrl,
adminSourceLocationUrl,
DEFAULT_ADMIN_SOURCE_FILTERS as DEFAULT_SOURCE_FILTERS,
type AdminCommandSelection,
} from "@/lib/admin-history";
import { operationsNavigationUrl } from "@/lib/admin-operations-navigation";
import { startAdminPolling } from "@/lib/admin-polling";
import {
displayedRunStatus,
formatDateTime,
formatDuration,
formatFullDate,
formatNumber,
formatRelativeTime,
humanize,
runStatusLabel
} from "@/lib/admin-presentation";
import { runLedgerLocationUrl } from "@/lib/admin-run-workspace";
import {
adminSourceEnabledTargets,
selectableAdminSources,
} from "@/lib/admin-source-bulk";
import {
adminSourceIsRetired,
adminSourceLifecycleLabel,
} from "@/lib/admin-source-lifecycle";
import { filterSourceRegistry,parseSourceRegistryLens,sourceRegistryHealth,sourceRegistryLensLabel,type SourceRegistryLens } from "@/lib/admin-source-workspace";
import type {
AdminCommand,
AdminFilterMetadata,
AdminRunFilters,
AdminSortDirection,
AdminSource,
AdminSourceConfigurationUpdate,
AdminSourceFilters,
AdminSourcePage,
AdminSourceSort,
AdminTab
} from "@/lib/admin-types";
import { ApiError } from "@/lib/api";
import { CatalogView } from "./catalog-view";
import { Action,Chip,kit,PageHead } from "./console-kit";
import { SourceExpandedDetails } from "./source-expanded-details";
import { SourceRegistrySummary } from "./source-registry-summary";
import { SourceRegistrationHistory } from "./source-registration-history";
import sourceStyles from "./sources-view.module.css";
import { useAdminInspector } from "./use-admin-inspector";
import { useAdminSnapshot } from "./use-admin-snapshot";

const SOURCE_SORT_DEFAULT_DIRECTION: Record<AdminSourceSort, AdminSortDirection> = {
  source: "asc",
  health: "asc",
  catalog: "desc",
  catalog_total: "desc",
  last_success: "desc",
  latest_run: "asc",
  output: "desc",
};

const DEFAULT_RUN_FILTERS: AdminRunFilters = {
  status: "",
  sourceKey: "",
  windowHours: 168,
  includeFixtures: false,
};

const TABS: Array<{
  value: AdminTab;
  label: string;
  icon: typeof Activity;
}> = [
  { value: "overview", label: "Overview", icon: Activity },
  { value: "sources", label: "Sources", icon: Layers3 },
  { value: "catalog", label: "Catalog", icon: Database },
  { value: "run-stats", label: "Runs", icon: FileClock },
  { value: "commands", label: "Commands", icon: CommandIcon },
];

const CONTEXTUAL_VIEWS: Partial<Record<AdminTab, {
  title: string;
  parent: "run-stats";
  backLabel: string;
}>> = {
  runs: { title: "Run history", parent: "run-stats", backLabel: "Back to Runs" },
  pipeline: { title: "Pipeline", parent: "run-stats", backLabel: "Back to Runs" },
};

function sourceIsCommandEligible(source: AdminSource | null): source is AdminSource {
  return Boolean(
    source
    && !adminSourceIsRetired(source)
    && source.enabled
    && source.review_status === "reviewed"
    && source.effective_status !== "running"
    && source.effective_status !== "policy_blocked",
  );
}

function readableError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "The ingestion control plane did not respond.";
}

function sourceStatusTone(status: string): string {
  if (status === "failed" || status === "policy_blocked" || status === "review_expired") {
    return "critical";
  }
  if (
    status === "due"
    || status === "paused"
    || status === "unreviewed"
    || status === "needs_review"
  ) {
    return "warning";
  }
  if (status === "succeeded" || status === "active" || status === "running") return "healthy";
  return "neutral";
}

function AdminStatus({
  status,
  label,
}: {
  status: string;
  label?: string;
}) {
  return (
    <span className={`admin-status admin-status--${sourceStatusTone(status)}`}>
      <i />
      {label ?? humanize(status)}
    </span>
  );
}

function SourceSortHeader({
  field,
  label,
  filters,
  onChange,
}: {
  field: AdminSourceSort;
  label: string;
  filters: AdminSourceFilters;
  onChange: (field: AdminSourceSort) => void;
}) {
  const active = filters.sortBy === field;
  const ariaSort = active
    ? filters.sortDirection === "asc" ? "ascending" : "descending"
    : "none";
  return (
    <th aria-sort={ariaSort}>
      <button
        className={`admin-source-sort ${active ? "is-active" : ""}`}
        type="button"
        onClick={() => onChange(field)}
        title={`Sort by ${label.toLowerCase()}${active ? `, currently ${ariaSort}` : ""}`}
      >
        <span>{label}</span>
        {active
          ? filters.sortDirection === "asc"
            ? <ArrowUp aria-hidden="true" />
            : <ArrowDown aria-hidden="true" />
          : <ChevronsUpDown aria-hidden="true" />}
      </button>
    </th>
  );
}

interface SourcesPanelProps {
  canEnable: boolean;
  canConfigure: boolean;
  canRefresh: boolean;
  refreshSubmitting: boolean;
  policyAllowed: boolean | null;
  onQueueRefresh: (source: AdminSource) => void;
  onConfigurationSaved: (result: AdminSourceConfigurationUpdate) => void;
  onOpenRuns: (filters: AdminRunFilters, runKey?: string) => void;
  onOpenCatalog: (sourceKey: string, dateScope?: CatalogDateScope) => void;
  onReload: () => void;
  refreshVersion: number;
  page: AdminSourcePage | null;
  metadata: AdminFilterMetadata | null;
  filters: AdminSourceFilters;
  loading: boolean;
  error: string | null;
  mutationError: string | null;
  onDismissMutationError: () => void;
  metadataError: string | null;
  onRetryMetadata: () => void;
  fixtureCount: number | null;
  busySourceKey: string | null;
  bulkMutation: { enabled: boolean; total: number } | null;
  onFiltersChange: (filters: AdminSourceFilters) => void;
  onOpenSource: (sourceKey: string) => void;
  onSetEnabled: (source: AdminSource, enabled: boolean) => void;
  onSetSelectedEnabled: (sources: AdminSource[], enabled: boolean) => Promise<boolean>;
}

function SourcesPanel({
  canEnable, canConfigure, onConfigurationSaved, onOpenRuns, onOpenCatalog, onReload, refreshVersion,
  canRefresh, refreshSubmitting, policyAllowed, onQueueRefresh,
  page,
  metadata,
  filters,
  loading,
  error,
  mutationError, onDismissMutationError,
  metadataError,
  onRetryMetadata,
  fixtureCount,
  busySourceKey,
  bulkMutation,
  onFiltersChange,
  onOpenSource,
  onSetEnabled,
  onSetSelectedEnabled,
}: SourcesPanelProps) {
  const sourceInspector = useAdminInspector({
    selectionParam: "source_selection", panelParam: "source_inspector",
    panels: ["overview", "evidence", "operations", "configuration"], defaultPanel: "overview",
  });
  const [lens, setLens] = useState<SourceRegistryLens>("all");
  const [sourceSaving, setSourceSaving] = useState(false);
  const [sourceAnchor, setSourceAnchor] = useState<{ key: string; fallback: boolean; row?: AdminSource } | null>(null);
  useEffect(() => {
    const read = () => setLens(parseSourceRegistryLens(new URL(window.location.href).searchParams.get("registry_lens")));
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  const selectionScope = JSON.stringify([filters, lens]);
  const [selection, setSelection] = useState({ scope: selectionScope, keys: new Set<string>() });
  const selectedSourceKeys = selection.scope === selectionScope ? selection.keys : new Set<string>();
  const setSelectedSourceKeys = (next: Set<string> | ((current: Set<string>) => Set<string>)) => setSelection((current) => ({
    scope: selectionScope,
    keys: typeof next === "function" ? next(current.scope === selectionScope ? current.keys : new Set()) : next,
  }));
  useEffect(() => setSelection({ scope: selectionScope, keys: new Set() }), [selectionScope]);
  const activeFilterCount = [
    filters.state !== "all",
    Boolean(filters.mode),
    Boolean(filters.publisher),
    Boolean(filters.region),
    filters.includeFixtures,
  ].filter(Boolean).length;
  const facetTotal = (facets: AdminFilterMetadata["modes"] | undefined): number | null => (
    facets ? facets.reduce((total, facet) => total + facet.count, 0) : null
  );
  const modeTotal = facetTotal(metadata?.modes);
  const publisherTotal = facetTotal(metadata?.publishers);
  const regionTotal = facetTotal(metadata?.regions);
  const allOptionLabel = (label: string, count: number | null): string => (
    count === null
      ? label
      : `${label} · ${formatNumber(count)} ${count === 1 ? "source" : "sources"}`
  );
  const registrySources = page?.items ?? [];
  const shownSources = filterSourceRegistry(registrySources, lens);
  const selectedInRows = shownSources.some((source) => source.source_key === sourceInspector.selection);
  useEffect(() => {
    if (!sourceInspector.selection) { setSourceAnchor(null); return; }
    if (sourceAnchor?.key === sourceInspector.selection) return;
    if (!selectedInRows && loading) return;
    setSourceAnchor({ key: sourceInspector.selection, fallback: !selectedInRows,
      row: shownSources.find((source) => source.source_key === sourceInspector.selection) });
  }, [sourceInspector.selection, sourceAnchor, selectedInRows, loading]);
  const anchored = sourceAnchor?.key === sourceInspector.selection;
  // Keep the editor's keyed table row mounted if a refreshed filter result excludes it.
  const retainedRow = anchored && sourceAnchor?.row && !selectedInRows ? sourceAnchor.row : null;
  const visibleRows = retainedRow ? [...shownSources, retainedRow] : shownSources;
  const eligibleSources = selectableAdminSources(shownSources);
  const selectedSources = eligibleSources.filter((source) => selectedSourceKeys.has(source.source_key));
  const allShownSelected = eligibleSources.length > 0
    && selectedSources.length === eligibleSources.length;
  const someShownSelected = selectedSources.length > 0 && !allShownSelected;
  const selectedEnabled = selectedSources.filter((source) => source.enabled).length;
  const selectedPaused = selectedSources.length - selectedEnabled;
  const shownRetired = shownSources.filter(adminSourceIsRetired).length;
  const shownEnabled = shownSources.filter(
    (source) => !adminSourceIsRetired(source) && source.enabled,
  ).length;
  const shownPaused = shownSources.length - shownEnabled - shownRetired;
  const shownLifecycleSummary = !page ? "Source states not yet loaded" : [
    `${formatNumber(shownEnabled)} enabled`,
    `${formatNumber(shownPaused)} paused`,
    shownRetired ? `${formatNumber(shownRetired)} retired` : null,
  ].filter(Boolean).join(" · ");
  const truncated = Boolean(page && page.total > registrySources.length);
  const scopeLabel = !page
    ? loading ? "Reading matching sources…" : "Registry evidence unavailable"
    : truncated
    ? `${formatNumber(shownSources.length)} shown · ${formatNumber(registrySources.length)} loaded of ${formatNumber(page?.total ?? 0)} matching`
    : `${formatNumber(shownSources.length)} matching${lens !== "all" ? ` · ${sourceRegistryLensLabel(lens)}` : ""}`;
  const filterLens = (next: SourceRegistryLens) => {
    if (sourceSaving) return;
    setSelectedSourceKeys(new Set());
    const url = new URL(window.location.href);
    if (next === "all") url.searchParams.delete("registry_lens");
    else url.searchParams.set("registry_lens", next);
    if (lens !== next) window.history.pushState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
    setLens(next);
  };
  const expandSource = (key: string, panel = "overview") => { if (!sourceSaving) sourceInspector.select(key, panel); };
  const closeExpandedSource = () => {
    if (sourceSaving) return;
    const key = sourceInspector.selection;
    sourceInspector.select(null);
    window.requestAnimationFrame(() => document.getElementById(`source-${key}-toggle`)?.focus({ preventScroll: true }));
  };
  const toggleExpandedSource = (key: string) => sourceInspector.selection === key ? closeExpandedSource() : expandSource(key);
  const selectScopeLabel = !page ? "Select sources" : truncated
    ? `Select ${formatNumber(eligibleSources.length)} shown`
    : eligibleSources.length === shownSources.length
      ? `Select all ${formatNumber(eligibleSources.length)} matching`
      : `Select ${formatNumber(eligibleSources.length)} reviewed`;
  const updateFilters = (nextFilters: AdminSourceFilters) => {
    if (sourceSaving) return;
    setSelectedSourceKeys(new Set());
    const next = new URL(adminSourceFiltersUrl(nextFilters, window.location.href), window.location.href);
    next.searchParams.delete("source_selection");
    next.searchParams.delete("source_inspector");
    const nextUrl = `${next.pathname}${next.search}${next.hash}`;
    if (nextUrl !== `${window.location.pathname}${window.location.search}${window.location.hash}`) {
      window.history[filters.query !== nextFilters.query ? "replaceState" : "pushState"](window.history.state, "", nextUrl);
    }
    onFiltersChange(nextFilters);
    window.dispatchEvent(new PopStateEvent("popstate"));
  };
  const inspectGlobalState = (nextLens: SourceRegistryLens) => {
    if (sourceSaving) return;
    const nextFilters = { ...DEFAULT_SOURCE_FILTERS, includeFixtures: filters.includeFixtures };
    const next = new URL(adminSourceFiltersUrl(nextFilters, window.location.href), window.location.href);
    next.searchParams.set("registry_lens", nextLens);
    next.searchParams.delete("source_selection");
    next.searchParams.delete("source_inspector");
    const href = `${next.pathname}${next.search}${next.hash}`;
    if (href !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(window.history.state, "", href);
    setSelectedSourceKeys(new Set());
    onFiltersChange(nextFilters);
    setLens(nextLens);
    window.dispatchEvent(new PopStateEvent("popstate"));
    window.requestAnimationFrame(() => document.getElementById("source-registry-table")?.scrollIntoView({ block: "start", behavior: "smooth" }));
  };
  const changeSort = (field: AdminSourceSort) => {
    updateFilters({
      ...filters,
      sortBy: field,
      sortDirection: filters.sortBy === field
        ? filters.sortDirection === "asc" ? "desc" : "asc"
        : SOURCE_SORT_DEFAULT_DIRECTION[field],
    });
  };
  const toggleSourceSelection = (sourceKey: string) => {
    setSelectedSourceKeys((current) => {
      const next = new Set(current);
      if (next.has(sourceKey)) next.delete(sourceKey);
      else next.add(sourceKey);
      return next;
    });
  };
  const toggleAllShown = () => {
    setSelectedSourceKeys(
      allShownSelected
        ? new Set()
        : new Set(eligibleSources.map((source) => source.source_key)),
    );
  };
  const applySelectedState = async (enabled: boolean) => {
    if (!canEnable || loading || error || truncated || bulkMutation || busySourceKey) return;
    const targets = selectedSources.filter((source) => source.enabled !== enabled);
    if (await onSetSelectedEnabled(targets, enabled)) {
      setSelectedSourceKeys(new Set());
    }
  };
  return (
    <div className={`${kit.page} ${sourceStyles.page}`}>
      <PageHead eyebrow="Operations / sources" title="Sources"
        sub="Track collection health, inspect run output and manage source settings. Expand a source to investigate."
        actions={<>
          <Action onClick={onReload} disabled={loading}><RefreshCw aria-hidden="true" />Refresh</Action>
        </>} />
      <div className={sourceStyles.content}>

      <fieldset disabled={sourceSaving} className={sourceStyles.filterFields}><SourceRegistrationHistory includeFixtures={filters.includeFixtures} refreshVersion={refreshVersion} onInspectState={inspectGlobalState} /></fieldset>

      <fieldset disabled={sourceSaving} className={sourceStyles.filterFields}><section className="admin-filter-surface">
        <label className="admin-search">
          <Search aria-hidden="true" />
          <span className="sr-only">Search sources</span>
          <input
            aria-label="Search sources"
            value={filters.query}
            onChange={(event) => updateFilters({ ...filters, query: event.target.value })}
            placeholder="Search source, publisher, mode, region, or URL"
          />
          {filters.query ? (
            <button type="button" onClick={() => updateFilters({ ...filters, query: "" })} aria-label="Clear search">
              <X aria-hidden="true" />
            </button>
          ) : null}
        </label>
        <div className="admin-source-state-tabs" aria-label="Source state">
          {(metadata?.source_states ?? [
            { value: "all", label: "All states" },
            { value: "active", label: "Active" },
            { value: "due", label: "Due" },
            { value: "blocked", label: "Blocked" },
            { value: "failed", label: "Failed" },
          ]).map((state) => (
            <button
              type="button"
              key={state.value}
              className={filters.state === state.value ? "is-active" : ""}
              onClick={() => updateFilters({
                ...filters,
                state: state.value as AdminSourceFilters["state"],
              })}
            >
              {state.label}
            </button>
          ))}
        </div>
        <div className="admin-filter-row">
          <label><span className="sr-only">Sort sources</span>
            <select aria-label="Sort sources" value={filters.sortBy} onChange={(event) => changeSort(event.target.value as AdminSourceSort)}>
              {[["source", "Source name"], ["health", "Collection state"], ["catalog_total", "All events"], ["catalog", "Upcoming events"], ["last_success", "Last success"], ["latest_run", "Latest run"], ["output", "Latest output"]].map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <button type="button" className="admin-text-button" onClick={() => updateFilters({ ...filters, sortDirection: filters.sortDirection === "asc" ? "desc" : "asc" })} aria-label="Reverse source sort order">{filters.sortDirection === "asc" ? "Ascending" : "Descending"}</button>
          <div className="admin-filter-label"><Filter aria-hidden="true" /><span>Filters</span>{activeFilterCount ? <b>{activeFilterCount}</b> : null}</div>
          <label>
            <span className="sr-only">Adapter mode</span>
            <select value={filters.mode} onChange={(event) => updateFilters({ ...filters, mode: event.target.value })}>
              <option value="">{allOptionLabel("All adapters", modeTotal)}</option>
              {filters.mode && !metadata?.modes.some((mode) => mode.value === filters.mode) ? (
                <option value={filters.mode}>{humanize(filters.mode)}{metadata ? " · 0 sources" : ""}</option>
              ) : null}
              {metadata?.modes.map((mode) => <option key={mode.value} value={mode.value}>{humanize(mode.value)} · {formatNumber(mode.count)} {mode.count === 1 ? "source" : "sources"}</option>)}
            </select>
          </label>
          <label>
            <span className="sr-only">Publisher</span>
            <select value={filters.publisher} onChange={(event) => updateFilters({ ...filters, publisher: event.target.value })}>
              <option value="">{allOptionLabel("All publishers", publisherTotal)}</option>
              {filters.publisher && !metadata?.publishers.some((publisher) => publisher.value === filters.publisher) ? (
                <option value={filters.publisher}>{filters.publisher}{metadata ? " · 0 sources" : ""}</option>
              ) : null}
              {metadata?.publishers.map((publisher) => <option key={publisher.value} value={publisher.value}>{publisher.value} · {formatNumber(publisher.count)} {publisher.count === 1 ? "source" : "sources"}</option>)}
            </select>
          </label>
          <label>
            <span className="sr-only">Region</span>
            <select value={filters.region} onChange={(event) => updateFilters({ ...filters, region: event.target.value })}>
              <option value="">{allOptionLabel("All regions", regionTotal)}</option>
              {filters.region && !metadata?.regions.some((region) => region.value === filters.region) ? (
                <option value={filters.region}>{humanize(filters.region)}{metadata ? " · 0 sources" : ""}</option>
              ) : null}
              {metadata?.regions.map((region) => <option key={region.value} value={region.value}>{humanize(region.value)} · {formatNumber(region.count)} {region.count === 1 ? "source" : "sources"}</option>)}
            </select>
          </label>
          <label className="admin-checkbox">
            <input
              type="checkbox"
              checked={filters.includeFixtures}
              onChange={(event) => updateFilters({ ...filters, includeFixtures: event.target.checked })}
            />
            <span>{fixtureCount === null ? "Include fixtures" : `Include ${formatNumber(fixtureCount)} fixtures`}</span>
          </label>
          {activeFilterCount ? (
            <button className="admin-text-button" type="button" onClick={() => updateFilters({ ...DEFAULT_SOURCE_FILTERS, query: filters.query })}>
              Clear filters
            </button>
          ) : null}
        </div>
      </section>

      </fieldset>

      {metadataError ? <div className="admin-inline-error" role="alert"><CircleAlert aria-hidden="true" />
        <span><strong>Source filters are unavailable.</strong> {metadataError}</span>
        <button type="button" onClick={onRetryMetadata}>Retry filters</button>
      </div> : null}
      {error ? <div className="admin-inline-error" role="alert"><CircleAlert aria-hidden="true" />
        <span><strong>Registry read incomplete.</strong> {error}{page ? " Showing the last available registry evidence. Refresh to retry." : " Refresh to retry."}</span>
      </div> : null}
      {mutationError ? <div className="admin-inline-error" role="alert"><CircleAlert aria-hidden="true" />
        <span><strong>Source change was not saved.</strong> {mutationError}</span>
        <button type="button" onClick={onDismissMutationError}>Dismiss</button>
      </div> : null}
      <fieldset disabled={sourceSaving} className={sourceStyles.filterFields}><SourceRegistrySummary sources={registrySources} total={page?.total ?? 0} loading={loading}
        failed={Boolean(error)} lens={lens} onFilter={filterLens} onSelectSource={(key) => { filterLens("all"); expandSource(key); }} />
      {lens !== "all" && page ? <div className={sourceStyles.lensNotice} role="status">
        <span className={sourceStyles.lensLabel}><Filter aria-hidden="true" />{sourceRegistryLensLabel(lens)}<span>{formatNumber(shownSources.length)} {shownSources.length === 1 ? "source" : "sources"}</span></span>
        <button type="button" onClick={() => filterLens("all")}>Show all matching sources <X aria-hidden="true" /></button>
      </div> : null}

      <section
        className={`admin-source-bulk-bar ${selectedSources.length ? "is-active" : ""}`}
        aria-label="Bulk source controls"
      >
        <label className="admin-source-select-all">
          <input
            type="checkbox"
            checked={allShownSelected}
            disabled={!eligibleSources.length || loading || truncated || Boolean(error) || Boolean(bulkMutation)}
            ref={(node) => {
              if (node) node.indeterminate = someShownSelected;
            }}
            onChange={toggleAllShown}
          />
          <span>
            <strong>
              {selectedSources.length
                ? `${formatNumber(selectedSources.length)} selected`
                : shownLifecycleSummary}
            </strong>
            <small>
              {selectedSources.length
                ? `${formatNumber(selectedEnabled)} enabled · ${formatNumber(selectedPaused)} paused`
                : scopeLabel}
            </small>
          </span>
        </label>
        <div className="admin-source-selection-tools">
          {selectedSources.length ? (
            <button type="button" onClick={() => setSelectedSourceKeys(new Set())} disabled={Boolean(bulkMutation)}>
              Clear selection
            </button>
          ) : (
            <button type="button" onClick={toggleAllShown} disabled={!eligibleSources.length || loading || truncated || Boolean(error) || Boolean(bulkMutation)}>
              {selectScopeLabel}
            </button>
          )}
          {truncated ? <span>Bulk actions require the complete matching registry.</span> : null}
        </div>
        {selectedSources.length > 0 ? <div className="admin-source-bulk-actions">
          <button
            className="admin-table-action"
            type="button"
            disabled={!canEnable || loading || truncated || Boolean(error) || !selectedPaused || Boolean(bulkMutation) || Boolean(busySourceKey)}
            title="Writes one audited revision per selected source; refresh remains a separate action"
            onClick={() => void applySelectedState(true)}
          >
            {bulkMutation?.enabled
              ? <LoaderCircle className="spin" aria-hidden="true" />
              : <Power aria-hidden="true" />}
            {bulkMutation?.enabled
              ? `Resuming ${formatNumber(bulkMutation.total)}`
              : `Resume ${formatNumber(selectedPaused)}`}
          </button>
          <button
            className="admin-table-action admin-table-action--pause"
            type="button"
            disabled={!canEnable || loading || truncated || Boolean(error) || !selectedEnabled || Boolean(bulkMutation) || Boolean(busySourceKey)}
            title="Fences future refreshes and retains catalog events; an in-flight fetch may still finish"
            onClick={() => void applySelectedState(false)}
          >
            {bulkMutation && !bulkMutation.enabled
              ? <LoaderCircle className="spin" aria-hidden="true" />
              : <Pause aria-hidden="true" />}
            {bulkMutation && !bulkMutation.enabled
              ? `Pausing ${formatNumber(bulkMutation.total)}`
              : `Pause ${formatNumber(selectedEnabled)}`}
          </button>
        </div> : null}
      </section>

      </fieldset>

      <section id="source-registry-list" tabIndex={-1} className={`admin-source-table-shell ${loading ? "is-loading" : ""}`}>
        <div className={`admin-table-scroll ${sourceStyles.tableScroll}`}>
          <table id="source-registry-table" className="admin-table admin-table--sources" aria-label="Source registry">
            <thead><tr>
              <th className="admin-source-select-column"><span className="sr-only">Select</span></th>
              <SourceSortHeader field="source" label="Source" filters={filters} onChange={changeSort} />
              <SourceSortHeader field="health" label="Health" filters={filters} onChange={changeSort} />
              <SourceSortHeader field="catalog_total" label="All events" filters={filters} onChange={changeSort} />
              <SourceSortHeader field="catalog" label="Upcoming events" filters={filters} onChange={changeSort} />
              <SourceSortHeader field="latest_run" label="Latest run" filters={filters} onChange={changeSort} />
              <SourceSortHeader field="output" label="Latest output" filters={filters} onChange={changeSort} />
              <SourceSortHeader field="last_success" label="Last success / schedule" filters={filters} onChange={changeSort} />
              <th><span className="sr-only">Expand details</span></th>
            </tr></thead>
            <tbody>
              {loading && !page ? Array.from({ length: 8 }, (_, index) => <tr key={index} className="admin-table-skeleton"><td colSpan={9}><i /></td></tr>) : null}
              {visibleRows.map((source) => {
                const retired = adminSourceIsRetired(source);
                const replacementKey = source.superseded_by_source_key;
                const health = sourceRegistryHealth(source);
                const latest = source.latest_run;
                const expanded = sourceInspector.selection === source.source_key && anchored && !sourceAnchor?.fallback;
                const detailId = `source-${source.source_key}-details`;
                const runFilters: AdminRunFilters = { sourceKey: source.source_key, status: "", windowHours: 168, includeFixtures: filters.includeFixtures };
                return <Fragment key={source.source_key}>
                {retainedRow?.source_key === source.source_key ? <tr><td colSpan={9}>
                  <strong>{source.display_name}</strong> · This selected source is outside the current results. Its current details remain open below.
                </td></tr> : <tr data-inspected={expanded} className={`${sourceStyles.sourceRow} ${selectedSourceKeys.has(source.source_key) ? "is-selected" : ""}`}
                  onClick={(event) => { if (!(event.target as HTMLElement).closest("button,a,input,label,select,textarea")) toggleExpandedSource(source.source_key); }}>
                  <td className="admin-source-select-column">
                    <input type="checkbox" checked={selectedSourceKeys.has(source.source_key)}
                      disabled={sourceSaving || loading || truncated || Boolean(error) || retired || source.review_status !== "reviewed" || Boolean(bulkMutation)}
                      title={retired ? "Retired sources cannot be changed" : source.review_status === "reviewed" ? "Select source" : "Review this source before changing its state"}
                      aria-label={`Select ${source.display_name}`} onChange={() => toggleSourceSelection(source.source_key)} />
                  </td>
                  <td><div className="admin-source-name-cell">
                    <button disabled={sourceSaving} className="admin-source-name" type="button" aria-expanded={expanded} aria-controls={expanded ? detailId : undefined}
                      onClick={() => toggleExpandedSource(source.source_key)}><strong>{source.display_name}</strong></button>
                    <div className={sourceStyles.sourceMeta}>
                      <span>{source.publisher}</span><span>·</span><span>{humanize(source.mode)}</span>
                    </div>
                    <button disabled={sourceSaving} type="button" className={sourceStyles.configLink} aria-label={`Configuration for ${source.display_name}`}
                      onClick={() => expandSource(source.source_key, "configuration")}>Configuration <span>rev {source.source_revision}</span></button>
                    {replacementKey ? <button disabled={sourceSaving} className="admin-source-replacement" type="button" onClick={() => onOpenSource(replacementKey)}>
                      Superseded by <code>{replacementKey}</code><ChevronRight aria-hidden="true" /></button> : null}
                  </div></td>
                  <td>
                    <button disabled={sourceSaving} type="button" className={sourceStyles.cellButton} aria-label={`Inspect health for ${source.display_name}`} onClick={() => expandSource(source.source_key)}>
                      <Chip tone={health === "healthy" ? "ok" : health === "failed" || health === "blocked" ? "bad" : health === "running" ? "info" : health === "due" || health === "deferred" ? "warn" : "neutral"}>
                        {retired ? adminSourceLifecycleLabel(source) : sourceRegistryLensLabel(health)}
                      </Chip>
                    </button>
                    <small>{retired ? "Retained history" : source.review_status !== "reviewed" ? `${humanize(source.review_status)} review` : !source.enabled ? "Collection paused" : "Enabled · reviewed"}</small>
                  </td>
                  <td>
                    <button disabled={sourceSaving} type="button" className={sourceStyles.metricButton} onClick={() => onOpenCatalog(source.source_key, "all")} aria-label={`Browse all events for ${source.display_name}`}>
                      {source.total_event_count == null ? "—" : formatNumber(source.total_event_count)} <ArrowRight aria-hidden="true" /></button>
                  </td>
                  <td>
                    <button disabled={sourceSaving} type="button" className={sourceStyles.metricButton} onClick={() => onOpenCatalog(source.source_key, "upcoming")} aria-label={`Browse upcoming events for ${source.display_name}`}>
                      {source.upcoming_event_count == null ? "—" : formatNumber(source.upcoming_event_count)} <ArrowRight aria-hidden="true" /></button>
                  </td>
                  <td>{latest ? <>
                    <button disabled={sourceSaving} type="button" className={sourceStyles.cellButton} aria-label={`Latest run for ${source.display_name}`} onClick={() => onOpenRuns(runFilters, latest.run_key)}>
                      <AdminStatus status={displayedRunStatus(latest)} label={runStatusLabel(latest)} /><ArrowRight aria-hidden="true" />
                    </button>
                    <small title={formatFullDate(latest.started_at)}>{formatRelativeTime(latest.started_at)} · {formatDuration(latest.duration_ms)}</small>
                    <small>{latest.attempt_count} claim{latest.attempt_count === 1 ? "" : "s"}{latest.error ? <button disabled={sourceSaving} type="button" className={sourceStyles.errorLink} onClick={() => onOpenRuns(runFilters, latest.run_key)} title={latest.error}>{humanize(latest.error)}</button> : null}</small>
                  </> : <span className="admin-muted">No run recorded</span>}</td>
                  <td>{latest ? <button disabled={sourceSaving} type="button" className={sourceStyles.outputButton} onClick={() => onOpenRuns(runFilters, latest.run_key)} aria-label={`Inspect output for ${source.display_name}`}>
                    <strong>{latest.canonical_count === null ? "—" : formatNumber(latest.canonical_count)} <span>published</span></strong>
                    <small>{latest.candidate_count === null ? "Collected not recorded" : `${formatNumber(latest.candidate_count)} collected`}</small>
                  </button> : <span className="admin-muted">—</span>}</td>
                  <td><button disabled={sourceSaving} type="button" className={sourceStyles.scheduleButton} aria-label={`History and schedule for ${source.display_name}`} onClick={() => expandSource(source.source_key)}>
                    <span title={formatFullDate(source.last_succeeded_at)}>{source.last_succeeded_at ? formatRelativeTime(source.last_succeeded_at) : "Never succeeded"}</span>
                    <small title={formatFullDate(source.next_due_at)}>{retired || !source.enabled ? "Not scheduled" : source.due ? "Next: due now" : source.next_due_at ? `Next: ${formatDateTime(source.next_due_at)}` : "Next: not recorded"}</small>
                  </button></td>
                  <td><button disabled={sourceSaving} id={`source-${source.source_key}-toggle`} className={sourceStyles.expandButton} type="button" onClick={() => toggleExpandedSource(source.source_key)}
                    aria-label={`Inspect ${source.display_name}`} aria-expanded={expanded} aria-controls={expanded ? detailId : undefined}><ChevronDown aria-hidden="true" /></button></td>
                </tr>}
                {expanded ? <tr className={sourceStyles.expandedRow} data-testid="source-expanded-row"><td colSpan={9}>
                  <div id={detailId} className={sourceStyles.expandedContent}>
                    <SourceExpandedDetails sourceKey={source.source_key} panel={sourceInspector.panel} onPanel={sourceInspector.setPanel}
                      includeFixtures={filters.includeFixtures} refreshVersion={refreshVersion} canConfigure={canConfigure} canEnable={canEnable}
                      canRefresh={canRefresh} refreshSubmitting={refreshSubmitting} policyAllowed={policyAllowed} onQueueRefresh={onQueueRefresh}
                      onSavingChange={setSourceSaving} busy={Boolean(bulkMutation) || busySourceKey === source.source_key}
                      onConfigurationSaved={onConfigurationSaved} onOpenSource={onOpenSource} onOpenRuns={onOpenRuns} onOpenCatalog={onOpenCatalog}
                      onSetEnabled={onSetEnabled} onClose={closeExpandedSource} />
                  </div>
                </td></tr> : null}
                </Fragment>;
              })}
            </tbody>
          </table>
        </div>
        {!loading && !error && !shownSources.length ? <div className="admin-empty-state">
          <ListFilter aria-hidden="true" /><h2>No sources match</h2><p>Clear a filter or search a different publisher, mode, source key, or region.</p>
          {lens !== "all" ? <button type="button" onClick={() => filterLens("all")}>Clear chart filter</button> : null}
        </div> : null}
      </section>
      {sourceInspector.selection && anchored && sourceAnchor?.fallback ? <div className={sourceStyles.expandedContent}>
        <p className={sourceStyles.lensNotice}>{selectedInRows ? "The source is now in the registry. This expansion stays here until closed to preserve your work." : "This source is outside the current registry results. Reading its exact details below."}</p>
        <SourceExpandedDetails key={sourceInspector.selection} sourceKey={sourceInspector.selection} panel={sourceInspector.panel} onPanel={sourceInspector.setPanel}
          includeFixtures={filters.includeFixtures} refreshVersion={refreshVersion} canConfigure={canConfigure} canEnable={canEnable}
          canRefresh={canRefresh} refreshSubmitting={refreshSubmitting} policyAllowed={policyAllowed} onQueueRefresh={onQueueRefresh}
          busy={Boolean(bulkMutation) || busySourceKey === sourceInspector.selection} onSavingChange={setSourceSaving}
          onConfigurationSaved={onConfigurationSaved} onOpenSource={onOpenSource} onOpenRuns={onOpenRuns} onOpenCatalog={onOpenCatalog}
          onSetEnabled={onSetEnabled} onClose={closeExpandedSource} />
      </div> : null}
      </div>
    </div>
  );
}

export function AdminConsole() {
  const [tab, setTab] = useState<AdminTab>("overview");
  const contextualView = CONTEXTUAL_VIEWS[tab];
  const primaryTab = contextualView?.parent ?? tab;
  const [urlReady, setUrlReady] = useState(false);
  const [sourceFilters, setSourceFilters] = useState<AdminSourceFilters>(DEFAULT_SOURCE_FILTERS);
  const sourceQuery = sourceFilters.query.trim();
  // Depend on the request's values, so inspector history and equivalent filter objects do not
  // restart roster paging. Facets do not depend on source ordering.
  const sourceFacetFilters = useMemo<AdminSourceFilters>(() => ({
    query: sourceQuery, state: sourceFilters.state, mode: sourceFilters.mode,
    publisher: sourceFilters.publisher, region: sourceFilters.region,
    includeFixtures: sourceFilters.includeFixtures, sortBy: "source", sortDirection: "asc",
  }), [sourceQuery, sourceFilters.state, sourceFilters.mode, sourceFilters.publisher,
    sourceFilters.region, sourceFilters.includeFixtures]);
  const sourceReadFilters = useMemo<AdminSourceFilters>(() => ({
    ...sourceFacetFilters, sortBy: sourceFilters.sortBy, sortDirection: sourceFilters.sortDirection,
  }), [sourceFacetFilters, sourceFilters.sortBy, sourceFilters.sortDirection]);
  const sourceScope = JSON.stringify(sourceReadFilters);
  const metadataScope = JSON.stringify(sourceFacetFilters);
  const [metadataSnapshot, setMetadataSnapshot] = useState<{ scope: string; data: AdminFilterMetadata } | null>(null);
  const metadata = metadataSnapshot?.scope === metadataScope ? metadataSnapshot.data : null;
  const [metadataFailure, setMetadataFailure] = useState<{ scope: string; message: string } | null>(null);
  const metadataError = metadataFailure?.scope === metadataScope ? metadataFailure.message : null;
  const [metadataReloadNonce, setMetadataReloadNonce] = useState(0);
  const [sourceSnapshot, setSourceSnapshot] = useState<{ scope: string; page: AdminSourcePage } | null>(null);
  const sourcePage = sourceSnapshot?.scope === sourceScope ? sourceSnapshot.page : null;
  const [pipelineFilters, setPipelineFilters] = useState<AdminRunFilters>(DEFAULT_RUN_FILTERS);
  const [runFilters, setRunFilters] = useState<AdminRunFilters>(DEFAULT_RUN_FILTERS);
  const [commands, setCommands] = useState<AdminCommand[]>([]);
  const [commandSelection, setCommandSelection] = useState<AdminCommandSelection | undefined>();
  const [trackedCommandId, setTrackedCommandId] = useState<string | null>(null);
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [commandsLoading, setCommandsLoading] = useState(true);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [sourceMutationError, setSourceMutationError] = useState<string | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const loadOverview = useCallback(async (signal: AbortSignal) => {
    try {
      const result = await getAdminOverview(signal);
      if (!signal.aborted) setGlobalError(null);
      return result;
    } catch (error) {
      if (!signal.aborted) setGlobalError(readableError(error));
      throw error;
    }
  }, []);
  const overviewRead = useAdminSnapshot(loadOverview, reloadNonce);
  const overviewLoading = overviewRead.loading;
  const overview = overviewRead.loading || overviewRead.failed || overviewRead.authorizationDenied ? null : overviewRead.data;
  const operatorRead = useAdminSnapshot(getAdminOperatorSession, reloadNonce);
  const operator = operatorRead.data;
  const canRefresh = operator?.capabilities.includes("ingestion.refresh") ?? false;
  const canEnable = operator?.capabilities.includes("ingestion.sources.enable") ?? false;
  const canConfigure = operator?.capabilities.includes("ingestion.sources.configure") ?? false;
  const [submitting, setSubmitting] = useState(false);
  const [sourceMutatingKey, setSourceMutatingKey] = useState<string | null>(null);
  const [sourceBulkMutation, setSourceBulkMutation] = useState<{
    enabled: boolean;
    total: number;
  } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [toastTracksCommand, setToastTracksCommand] = useState(false);

  useEffect(() => {
    const location = adminHistoryLocationFromUrl(window.location.href);
    window.history.replaceState(window.history.state, "", adminHistoryUrl(location, window.location.href));
    setTab(location.tab);
    setSourceFilters(adminSourceFiltersFromUrl(window.location.href));
    setCommandSelection(location.investigation);
    if (location.filters) { setRunFilters(location.filters); setPipelineFilters(location.filters); }
    setUrlReady(true);
  }, []);

  useEffect(() => {
    if (!urlReady) return;
    const handlePopState = () => {
      const location = adminHistoryLocationFromUrl(window.location.href);
      window.history.replaceState(window.history.state, "", adminHistoryUrl(location, window.location.href));
      setTab(location.tab);
      setSourceFilters(adminSourceFiltersFromUrl(window.location.href));
      setCommandSelection(location.investigation);
      if (location.filters) { setRunFilters(location.filters); setPipelineFilters(location.filters); }
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [urlReady]);

  useEffect(() => {
    if (!urlReady || tab !== "sources") return;
    const controller = new AbortController();
    setMetadataFailure(null);
    const timer = window.setTimeout(() => {
      void getAdminFilters(sourceFacetFilters, controller.signal)
        .then((next) => {
          if (controller.signal.aborted) return;
          setMetadataSnapshot({ scope: metadataScope, data: next });
          setMetadataFailure(null);
        })
        .catch((error) => {
          if (controller.signal.aborted) return;
          setMetadataFailure({ scope: metadataScope, message: readableError(error) });
          if (error instanceof ApiError && (error.status === 401 || error.status === 403)) setMetadataSnapshot(null);
        });
    }, sourceFacetFilters.query ? 220 : 0);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [reloadNonce, metadataReloadNonce, sourceFacetFilters, metadataScope, tab, urlReady]);

  useEffect(() => {
    if (!urlReady || tab !== "sources") return;
    const controller = new AbortController();
    setSourcesLoading(true);
    setSourceError(null);
    const timer = window.setTimeout(() => {
      void getAllAdminSources(sourceReadFilters, {
        signal: controller.signal,
        onProgress: ({ page: next, truncated }) => {
          if (controller.signal.aborted) return;
          setSourceSnapshot((current) => {
            // A complete snapshot remains visible during a same-scope refresh. Initial and new
            // scopes publish each received page, without waiting for the whole roster.
            if (truncated && current?.scope === sourceScope && current.page.items.length >= current.page.total) return current;
            return { scope: sourceScope, page: next };
          });
        },
      })
        .then(({ page: next }) => {
          if (controller.signal.aborted) return;
          setSourceSnapshot({ scope: sourceScope, page: next });
          setSourceError(null);
        })
        .catch((error) => {
          if (!controller.signal.aborted) {
            setSourceError(readableError(error));
            if (error instanceof ApiError && (error.status === 401 || error.status === 403)) setSourceSnapshot(null);
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setSourcesLoading(false);
        });
    }, sourceReadFilters.query ? 220 : 0);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [reloadNonce, sourceReadFilters, sourceScope, tab, urlReady]);





  const watchedCommands = useRef(new Set<string>());
  const commandsRef = useRef(commands);
  commandsRef.current = commands;
  useEffect(() => {
    if (!urlReady) return;
    setCommandsLoading(true);
    return startAdminPolling({
      load: (signal) => readAdminCommandProgress({
        loadList: () => getAdminCommands(signal),
        loadDetail: (commandId) => getAdminCommandDetail(commandId, signal),
        watched: watchedCommands.current,
        selectedCommandId: commandSelection?.commandId,
        signal,
      }),
      isVisible: () => document.visibilityState !== "hidden",
      retryError: (error) => !(error instanceof ApiError && error.status < 500),
      shouldContinue: (snapshot) => snapshot.continuePolling,
      onValue: ({ page, childrenSettled }) => {
        const previous = commandsRef.current;
        const settled = page.items.some((command) => (
          (command.status === "completed" || command.status === "failed")
          && previous.some((old) => old.command_id === command.command_id
            && (old.status === "queued" || old.status === "running"))
        ));
        commandsRef.current = page.items;
        setCommands(page.items);
        setCommandsLoading(false);
        setCommandError(null);
        if (settled || childrenSettled) setReloadNonce((current) => current + 1);
      },
      onError: (error) => {
        setCommandError(readableError(error));
        setCommandsLoading(false);
      },
    });
  }, [reloadNonce, urlReady, commandSelection?.commandId]);

  const refreshDuePending = commands.some(
    (command) => (
      command.action === "refresh_due"
      && (command.status === "queued" || command.status === "running")
    ),
  );

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 4_000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const navigateAdmin = useCallback((
    nextTab: AdminTab,
    sourceKey: string | null = null,
    filters?: AdminRunFilters,
    investigation?: AdminCommandSelection,
  ) => {
    const nextLocation = { tab: sourceKey ? "sources" as const : nextTab, sourceKey, filters, investigation };
    const historyUrl = adminHistoryUrl(nextLocation, window.location.href);
    const nextUrl = nextLocation.tab === "overview"
      ? operationsNavigationUrl(historyUrl, { queue: null })
      : historyUrl;
    if (nextUrl !== `${window.location.pathname}${window.location.search}${window.location.hash}`) {
      window.history.pushState(window.history.state, "", nextUrl);
    }
    setTab(nextLocation.tab);
    setCommandSelection(investigation);
    if (filters) { setRunFilters(filters); setPipelineFilters(filters); }
    if (sourceKey) {
      setSourceFilters(adminSourceFiltersFromUrl(window.location.href));
    }
    if (sourceKey || nextLocation.tab === "overview") {
      window.dispatchEvent(new PopStateEvent("popstate"));
    }
  }, []);

  const openSource = (sourceKey: string, panel: "overview" | "configuration" = "overview") => {
    const nextUrl = adminSourceLocationUrl(sourceKey, window.location.href, panel);
    if (nextUrl !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(window.history.state, "", nextUrl);
    window.dispatchEvent(new PopStateEvent("popstate"));
  };
  const openPipelineRuns = (filters: AdminRunFilters, sortBy: import("@/lib/admin-types").AdminRunSort = "started") => {
    navigateAdmin("runs", null, filters);
    // A drilldown opens its own matching set; old ledger search and selection must not narrow it.
    const url = new URL(window.location.href);
    url.searchParams.delete("run_selection");
    url.searchParams.delete("run_inspector");
    window.history.replaceState(window.history.state, "", runLedgerLocationUrl({
      page: 0, query: "", sort: sortBy, direction: "desc",
    }, url.href));
  };
  const openRegistryRuns = (filters: AdminRunFilters, runKey?: string) => {
    openPipelineRuns(filters);
    if (runKey) {
      const url = new URL(window.location.href);
      url.searchParams.set("run_selection", `${filters.sourceKey}|${runKey}`);
      window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
    }
  };
  const openRegistryCatalog = (sourceKey: string, runKey?: string, dateScope: CatalogDateScope = "all") => {
    navigateAdmin("catalog");
    window.history.replaceState(window.history.state, "", catalogLocationUrl({
      sourceKey, runKey, dateScope, priceScope: "all", sourceQuery: "", sourcePage: 1, query: "", eventId: null, afterStart: null, afterId: null,
    }, window.location.href));
  };
  const selectTab = (nextTab: AdminTab) => {
    navigateAdmin(nextTab, null, nextTab === "runs" ? runFilters : nextTab === "pipeline" ? pipelineFilters : undefined);
  };
  const refreshViews = useCallback(() => setReloadNonce((current) => current + 1), []);

  const trackLatestCommand = () => {
    navigateAdmin("commands", null, undefined, trackedCommandId ? { commandId: trackedCommandId } : undefined);
    setToast(null);
    setToastTracksCommand(false);
  };

  const submitCommand = async (
    action: "refresh_source" | "refresh_due",
    source: AdminSource | null,
  ) => {
    if (!canRefresh || submitting || !overview?.policy.allowed) return;
    if (action === "refresh_source" && !sourceIsCommandEligible(source)) {
      setToast(source && adminSourceIsRetired(source)
        ? "Retired sources cannot launch new collection commands."
        : "This source is not currently eligible to run.");
      setToastTracksCommand(false);
      return;
    }
    const duplicatePending = commands.find((command) => (
      command.action === action
      && command.source_key === (source?.source_key ?? null)
      && (command.status === "queued" || command.status === "running")
    ));
    if (duplicatePending) {
      setTrackedCommandId(duplicatePending.command_id);
      setToast(
        action === "refresh_due"
          ? "A due-source refresh is already queued."
          : `${source?.display_name ?? "Source"} already has a refresh queued.`,
      );
      setToastTracksCommand(true);
      return;
    }
    setSubmitting(true);
    try {
      const command = await enqueueAdminCommand(
        action,
        source?.source_key ?? null,
      );
      watchedCommands.current.add(command.command_id);
      setTrackedCommandId(command.command_id);
      setCommands((current) => [command, ...current.filter((item) => item.command_id !== command.command_id)]);
      setToast(
        action === "refresh_due"
          ? "Due-source refresh accepted into the durable queue."
          : `${source?.display_name ?? "Source"} refresh accepted into the durable queue.`,
      );
      setToastTracksCommand(true);
      setCommandError(null);
      setReloadNonce((current) => current + 1);
    } catch (error) {
      setCommandError(readableError(error));
      navigateAdmin("commands");
    } finally {
      setSubmitting(false);
    }
  };

  const setSourceEnabled = async (source: AdminSource, enabled: boolean) => {
    if (!canEnable || sourceMutatingKey || sourceBulkMutation) return;
    if (adminSourceIsRetired(source)) {
      setToast("Retired sources are immutable and cannot be resumed or paused.");
      setToastTracksCommand(false);
      return;
    }
    setSourceMutatingKey(source.source_key);
    setSourceMutationError(null);
    try {
      const result = await setAdminSourceEnabled(
        source.source_key,
        enabled,
        source.source_revision,
      );
      setToast(
        `${source.display_name} collection ${enabled ? "resumed" : "paused"} · registry revision ${result.source_revision}.`,
      );
      setToastTracksCommand(false);
      setReloadNonce((current) => current + 1);
    } catch (error) {
      const message = error instanceof ApiError && error.status === 409
        ? "This source changed before the update completed. Reloaded the latest revision; try again."
        : readableError(error);
      setSourceMutationError(message);
      setReloadNonce((current) => current + 1);
    } finally {
      setSourceMutatingKey(null);
    }
  };

  const setSelectedSourcesEnabled = async (
    sources: AdminSource[],
    enabled: boolean,
  ): Promise<boolean> => {
    if (!canEnable || sourceMutatingKey || sourceBulkMutation) return false;
    const targets = adminSourceEnabledTargets(sources, enabled);
    if (!targets.length) {
      setToast(`Every selected source is already ${enabled ? "enabled" : "paused"}.`);
      setToastTracksCommand(false);
      return true;
    }
    setSourceBulkMutation({ enabled, total: targets.length });
    setSourceMutationError(null);
    try {
      const result = await setAdminSourcesEnabled(targets, enabled);
      setToast(
        `${enabled ? "Resumed" : "Paused"} ${formatNumber(result.updated)} source${result.updated === 1 ? "" : "s"}`
        + (result.unchanged ? ` · ${formatNumber(result.unchanged)} already ${enabled ? "enabled" : "paused"}` : "")
        + ".",
      );
      setToastTracksCommand(false);
      setReloadNonce((current) => current + 1);
      return true;
    } catch (error) {
      const message = error instanceof ApiError && error.status === 409
        ? "The selected fleet changed before the update completed. Nothing was changed; the latest revisions are loading."
        : readableError(error);
      setSourceMutationError(message);
      setReloadNonce((current) => current + 1);
      return false;
    } finally {
      setSourceBulkMutation(null);
    }
  };

  return (
    <div className="admin-shell">
      <aside className="admin-sidebar">
        <div className="admin-sidebar__top">
          <a className="admin-brand" href="/admin">
            <span className="brand-symbol" aria-hidden="true"><i /><i /></span>
            <span>
              <strong>Events Concierge</strong>
              <small>Admin</small>
            </span>
          </a>
          <span className="admin-sidebar__label">Workspace</span>
          <nav className="admin-nav" aria-label="Administration navigation">
            {TABS.map(({ value, label, icon: Icon }) => (
              <button
                type="button"
                key={value}
                className={primaryTab === value ? "is-active" : ""}
                aria-current={primaryTab === value ? "page" : undefined}
                onClick={() => selectTab(value)}
              >
                <Icon aria-hidden="true" />
                <span>{label}</span>
              </button>
            ))}
          </nav>
        </div>
        <div className="admin-sidebar__footer">
          <details className="admin-operator-menu">
            <summary>
              <span className="admin-operator-menu__avatar" aria-hidden="true">LO</span>
              <span>
                <strong>{operator?.subject ?? "Operator"}</strong>
                <small><i />{operator?.environment ?? "Loading session"}</small>
              </span>
              <ChevronDown aria-hidden="true" />
            </summary>
            <div className="admin-operator-menu__panel">
              <span>Operator &amp; access</span>
              <dl>
                <div>
                  <dt>Session</dt>
                  <dd>{operator?.role ?? "Unavailable"}</dd>
                </div>
                <div>
                  <dt>Refresh access</dt>
                  <dd className={canRefresh && overview?.policy.allowed ? "is-allowed" : "is-blocked"}>
                    {overview
                      ? !canRefresh ? "Not granted" : overview.policy.allowed ? "Admitted" : "Policy blocked"
                      : overviewLoading ? "Checking" : "Unavailable"}
                  </dd>
                </div>
                <div>
                  <dt>Authentication</dt>
                  <dd>{operator?.authentication ?? "Unavailable"}</dd>
                </div>
              </dl>
              <p title={overview?.policy.reason}>
                {overview?.policy.code ?? "Local development session"}
              </p>
            </div>
          </details>
        </div>
      </aside>

      <header className="admin-mobile-header">
        <a className="admin-brand" href="/admin">
          <span className="brand-symbol" aria-hidden="true"><i /><i /></span>
          <span>
            <strong>Events Concierge</strong>
            <small>Admin</small>
          </span>
        </a>
        <div>
          <span className="admin-local-badge"><i />{operator?.role ?? "operator"}</span>
        </div>
      </header>

      {globalError && tab !== "overview" ? (
        <div className="admin-global-error">
          <CircleAlert aria-hidden="true" />
          <span>
            <strong>Admin overview is unavailable.</strong> {globalError}
            <small>Refresh to retry this read.</small>
          </span>
          <button type="button" onClick={refreshViews}>Retry</button>
          <button type="button" onClick={() => setGlobalError(null)} aria-label="Dismiss"><X aria-hidden="true" /></button>
        </div>
      ) : null}

      {urlReady ? <main className="admin-main">
            {contextualView ? (
              <nav className={`${kit.context} admin-context-navigation`} aria-label="Administration breadcrumb">
                <Action onClick={() => selectTab(contextualView.parent)}><ChevronLeft aria-hidden="true" />{contextualView.backLabel}</Action>
                <span aria-current="page">{contextualView.title}</span>
              </nav>
            ) : null}
            {tab === "overview" ? (
              <SystemView
                refreshVersion={reloadNonce}
                catalogRead={overviewRead}
                retryAdminSession={operatorRead.failed || operatorRead.authorizationDenied ? operatorRead.refresh : undefined}
                onNavigate={(nextTab, filters) => nextTab === "runs" && filters
                  ? openPipelineRuns(filters) : navigateAdmin(nextTab, null, filters)}
              />
            ) : null}
            {tab === "run-stats" ? (
              <RunStatisticsView refreshVersion={reloadNonce} onOpenSource={openSource} onOpenCatalog={openRegistryCatalog} />
            ) : null}
            {tab === "catalog" ? (
              <CatalogView
                onOpenRuns={openPipelineRuns}
                refreshVersion={reloadNonce}
                onOpenSource={(key) => openSource(key, "configuration")}
                onOpenRun={(sourceKey, runKey) => openRegistryRuns({ sourceKey, status: "", windowHours: 2160, includeFixtures: false }, runKey)}
              />
            ) : null}
            {tab === "pipeline" ? <PipelineView
              filters={pipelineFilters}
              onFiltersChange={(filters) => navigateAdmin("pipeline", null, filters)}
              onOpenRuns={openPipelineRuns}
              onOpenSource={openSource}
              refreshVersion={reloadNonce}
            /> : null}
            {tab === "sources" ? (
              <SourcesPanel
                onReload={refreshViews}
                refreshVersion={reloadNonce}
                canEnable={canEnable}
                canConfigure={canConfigure}
                canRefresh={canRefresh} refreshSubmitting={submitting} policyAllowed={overview?.policy.allowed ?? null}
                onQueueRefresh={(source) => void submitCommand("refresh_source", source)}
                onConfigurationSaved={(result) => {
                  setToast(`Reviewed configuration saved at registry revision ${result.source_revision}.`);
                  setToastTracksCommand(false);
                  refreshViews();
                }}
                onOpenRuns={openRegistryRuns}
                onOpenCatalog={(sourceKey, dateScope) => openRegistryCatalog(sourceKey, undefined, dateScope)}
                page={sourcePage}
                metadata={metadata}
                filters={sourceFilters}
                loading={sourcesLoading || (!sourcePage && !sourceError)}
                error={sourceError}
                mutationError={sourceMutationError} onDismissMutationError={() => setSourceMutationError(null)}
                metadataError={metadataError}
                onRetryMetadata={() => setMetadataReloadNonce((current) => current + 1)}
                fixtureCount={overview?.summary.fixture_sources ?? null}
                busySourceKey={sourceMutatingKey}
                bulkMutation={sourceBulkMutation}
                onFiltersChange={setSourceFilters}
                onOpenSource={openSource}
                onSetEnabled={(source, enabled) => void setSourceEnabled(source, enabled)}
                onSetSelectedEnabled={setSelectedSourcesEnabled}
              />
            ) : null}
            {tab === "runs" ? <RunsView onOpenSource={openSource} onOpenCatalog={openRegistryCatalog}
              filters={runFilters}
              onFiltersChange={(filters) => navigateAdmin("runs", null, filters)}
              refreshVersion={reloadNonce}
            /> : null}
            {tab === "commands" ? (
              <CommandsView
                selection={commandSelection}
                onSelect={(selection) => navigateAdmin("commands", null, undefined, selection)}
                canRefresh={canRefresh}
                commands={commands}
                loading={commandsLoading}
                error={commandError}
                onReload={refreshViews}
                refreshVersion={reloadNonce}
                onOpenSource={openSource}
                onRefreshDue={() => void submitCommand("refresh_due", null)}
                refreshSubmitting={submitting}
                refreshDuePending={refreshDuePending}
              />
            ) : null}
      </main> : null}

      <nav className="admin-mobile-nav" aria-label="Administration navigation">
        {TABS.map(({ value, label, icon: Icon }) => (
          <button
            type="button"
            key={value}
            className={primaryTab === value ? "is-active" : ""}
            aria-current={primaryTab === value ? "page" : undefined}
            onClick={() => selectTab(value)}
          >
            <Icon aria-hidden="true" />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      {toast ? (
        <div className="admin-toast" role="status">
          <CheckCircle2 aria-hidden="true" />
          <span>{toast}</span>
          {toastTracksCommand ? (
            <button type="button" onClick={trackLatestCommand}>Track command</button>
          ) : (
            <button type="button" onClick={() => setToast(null)}>Dismiss</button>
          )}
        </div>
      ) : null}

    </div>
  );
}
