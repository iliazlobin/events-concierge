const SOURCE_PAGE_SIZE = 50;
const RUN_PAGE_SIZE = 50;
const COMMAND_PAGE_SIZE = 30;
const POLL_BASE_MS = 3_000;
const POLL_MAX_MS = 30_000;
const DEFAULT_DETAIL_WINDOW_HOURS = 168;
const VALID_TABS = new Set(["overview", "sources", "runs", "commands", "logic"]);
const PENDING_STATUSES = new Set(["accepted", "pending", "queued", "running", "started"]);
const BLOCKED_SOURCE_STATUSES = new Set([
  "blocked",
  "disabled",
  "expired",
  "review_expired",
  "unreviewed",
  "quarantined",
  "policy_blocked",
  "running",
]);

const state = {
  operatorToken: "",
  overview: null,
  filters: null,
  currentBuild: null,
  activeTab: "overview",
  sources: [],
  sourceTotal: 0,
  sourceOffset: 0,
  sourceQuery: "",
  sourceState: "all",
  sourceMode: "",
  sourcePublisher: "",
  sourceRegion: "",
  sourceHealth: "",
  includeFixtures: false,
  sourceGeneration: 0,
  overviewSources: null,
  overviewSourceTotal: 0,
  overviewSourceQuery: "",
  overviewStage: "collect",
  selectedSourceKey: "",
  sourceDetail: null,
  sourceDetailWindowHours: DEFAULT_DETAIL_WINDOW_HOURS,
  sourceDetailGeneration: 0,
  sourceDetailInFlight: null,
  runs: [],
  overviewRuns: null,
  overviewRunTotal: 0,
  runTotal: 0,
  runOffset: 0,
  runStatus: "",
  runSourceKey: "",
  runWindow: "24h",
  runGeneration: 0,
  commands: [],
  commandStatus: "",
  commandAction: "",
  commandSourceKey: "",
  retryCommandIds: new Map(),
  requestInFlight: new Map(),
  pollTimer: null,
  pollInFlight: null,
  pollFailures: 0,
  dashboardInFlight: null,
  toastTimer: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined && content !== null) element.textContent = String(content);
  return element;
}

function svgNode(tag) {
  return document.createElementNS("http://www.w3.org/2000/svg", tag);
}

function append(parent, ...children) {
  if (!parent) return parent;
  for (const child of children) {
    if (child) parent.append(child);
  }
  return parent;
}

function setHidden(target, hidden) {
  if (target) target.hidden = hidden;
}

function setInlineError(target, message = "") {
  if (!target) return;
  target.textContent = message;
  target.hidden = !message;
}

function announce(message) {
  const live = $("#admin-live-status");
  if (!live) return;
  live.textContent = "";
  window.setTimeout(() => {
    live.textContent = message;
  }, 20);
}

function showToast(message, kind = "success") {
  const toast = $("#admin-toast");
  const copy = $("#admin-toast-copy");
  if (!toast || !copy) {
    announce(message);
    return;
  }
  copy.textContent = message;
  toast.classList.toggle("error", kind === "error");
  toast.setAttribute("role", kind === "error" ? "alert" : "status");
  toast.hidden = false;
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => {
    toast.hidden = true;
  }, kind === "error" ? 9_000 : 5_500);
  announce(message);
}

function apiErrorMessage(payload, status) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (status === 401) return "Operator authentication is required or has expired.";
  if (status === 403) return "This operator is not authorized to control ingestion.";
  if (status === 404) return "The requested ingestion record is unavailable.";
  if (status === 409) return "The command conflicts with current durable work. Refresh and retry.";
  if (status === 503) return "The ingestion control plane is temporarily unavailable.";
  return `The ingestion API returned an unexpected response (${status}).`;
}

async function performApiRequest(path, { method = "GET", body, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (state.operatorToken) headers.Authorization = `Bearer ${state.operatorToken}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
    signal,
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(apiErrorMessage(payload, response.status));
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function api(path, { method = "GET", body, signal } = {}) {
  if (method !== "GET" || signal) {
    return performApiRequest(path, { method, body, signal });
  }
  const existing = state.requestInFlight.get(path);
  if (existing) return existing;
  const request = performApiRequest(path, { method, body })
    .finally(() => {
      if (state.requestInFlight.get(path) === request) state.requestInFlight.delete(path);
    });
  state.requestInFlight.set(path, request);
  return request;
}

function humanize(value) {
  if (!value) return "Unknown";
  return String(value)
    .replaceAll("-", " ")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function optionalNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function formatCount(value) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(
    finiteNumber(value),
  );
}

function formatPercent(value) {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const number = Number(value);
  if (!Number.isFinite(number)) return "Unavailable";
  return new Intl.NumberFormat(undefined, {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(number);
}

function formatDuration(value) {
  const duration = optionalNumber(value);
  if (duration === null) return "Unavailable";
  if (duration < 1_000) return `${formatCount(duration)} ms`;
  if (duration < 60_000) {
    return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(
      duration / 1_000,
    )} s`;
  }
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(
    duration / 60_000,
  )} min`;
}

function validDate(value) {
  if (typeof value !== "string" || !value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDateTime(value) {
  const date = validDate(value);
  if (!date) return "Not reported";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function relativeTime(value) {
  const date = validDate(value);
  if (!date) return "Not reported";
  const seconds = Math.round((date.getTime() - Date.now()) / 1_000);
  const absolute = Math.abs(seconds);
  let divisor = 1;
  let unit = "second";
  if (absolute >= 86_400) {
    divisor = 86_400;
    unit = "day";
  } else if (absolute >= 3_600) {
    divisor = 3_600;
    unit = "hour";
  } else if (absolute >= 60) {
    divisor = 60;
    unit = "minute";
  }
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
    Math.round(seconds / divisor),
    unit,
  );
}

function safeExternalUrl(value) {
  if (typeof value !== "string") return null;
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "https:" ||
      parsed.username ||
      parsed.password ||
      parsed.origin === "null"
    ) {
      return null;
    }
    return parsed.href;
  } catch (_error) {
    return null;
  }
}

function safeSourceKey(value) {
  return typeof value === "string" && /^[a-z0-9][a-z0-9-]{1,79}$/.test(value)
    ? value
    : "";
}

function safeCommandQuery(value) {
  return typeof value === "string" && /^[a-z0-9-]{1,80}$/.test(value.toLowerCase())
    ? value.toLowerCase()
    : "";
}

function statusBadge(status) {
  const normalized = typeof status === "string" && status ? status.toLowerCase() : "unknown";
  return node(
    "span",
    `status-badge status-${normalized.replace(/[^a-z0-9-]/g, "-")}`,
    humanize(normalized),
  );
}

function boundedCopy(value, fallback = "") {
  if (value === null || value === undefined) return fallback;
  let copy;
  if (typeof value === "string") {
    copy = value;
  } else {
    try {
      copy = JSON.stringify(value);
    } catch (_error) {
      copy = String(value);
    }
  }
  return copy.length > 500 ? `${copy.slice(0, 497)}…` : copy;
}

function setConnection(kind, copy) {
  const connection = $("#connection-status");
  if (!connection) return;
  connection.classList.toggle("ready", kind === "ready");
  connection.classList.toggle("error", kind === "error");
  const label = $("span:last-child", connection);
  if (label) label.textContent = copy;
}

function revealShell() {
  setHidden($("#admin-boot"), true);
  setHidden($("#admin-shell"), false);
}

function readUrlState() {
  const url = new URL(window.location.href);
  const hashTab = url.hash.replace(/^#/, "");
  const queryTab = url.searchParams.get("tab") || "";
  state.activeTab = VALID_TABS.has(queryTab)
    ? queryTab
    : VALID_TABS.has(hashTab)
      ? hashTab
      : "overview";
  state.selectedSourceKey = safeSourceKey(url.searchParams.get("source"));
  if (state.selectedSourceKey) state.activeTab = "sources";
  state.sourceQuery = (url.searchParams.get("q") || "").slice(0, 160);
  state.sourceState = (url.searchParams.get("source_state") || "all").slice(0, 80);
  state.sourceMode = (url.searchParams.get("mode") || "").slice(0, 80);
  state.sourcePublisher = (url.searchParams.get("publisher") || "").slice(0, 300);
  state.sourceRegion = (url.searchParams.get("region") || "").slice(0, 120);
  state.includeFixtures = url.searchParams.get("fixtures") === "true";
  state.runStatus = (url.searchParams.get("run_status") || "").slice(0, 80);
  state.runSourceKey = safeSourceKey(url.searchParams.get("run_source"));
  const runWindow = url.searchParams.get("run_window") || "24h";
  state.runWindow = ["24h", "7d", "30d", ""].includes(runWindow) ? runWindow : "24h";
  const requestedWindow = Number(url.searchParams.get("window"));
  if ([24, 168, 720].includes(requestedWindow)) {
    state.sourceDetailWindowHours = requestedWindow;
  }
  state.commandStatus = (url.searchParams.get("command_status") || "").slice(0, 80);
  state.commandAction = (url.searchParams.get("command_action") || "").slice(0, 80);
  state.commandSourceKey = safeCommandQuery(url.searchParams.get("command_source"));
}

function setQueryValue(params, key, value, defaultValue = "") {
  if (value && value !== defaultValue) params.set(key, value);
  else params.delete(key);
}

function syncUrl({ push = false } = {}) {
  const url = new URL(window.location.href);
  setQueryValue(url.searchParams, "tab", state.activeTab, "overview");
  setQueryValue(url.searchParams, "source", state.selectedSourceKey);
  setQueryValue(url.searchParams, "q", state.sourceQuery);
  setQueryValue(url.searchParams, "source_state", state.sourceState, "all");
  setQueryValue(url.searchParams, "mode", state.sourceMode);
  setQueryValue(url.searchParams, "publisher", state.sourcePublisher);
  setQueryValue(url.searchParams, "region", state.sourceRegion);
  if (state.includeFixtures) url.searchParams.set("fixtures", "true");
  else url.searchParams.delete("fixtures");
  setQueryValue(url.searchParams, "run_status", state.runStatus);
  setQueryValue(url.searchParams, "run_source", state.runSourceKey);
  setQueryValue(url.searchParams, "run_window", state.runWindow, "24h");
  if (
    state.selectedSourceKey &&
    state.sourceDetailWindowHours !== DEFAULT_DETAIL_WINDOW_HOURS
  ) {
    url.searchParams.set("window", String(state.sourceDetailWindowHours));
  } else {
    url.searchParams.delete("window");
  }
  setQueryValue(url.searchParams, "command_status", state.commandStatus);
  setQueryValue(url.searchParams, "command_action", state.commandAction);
  setQueryValue(url.searchParams, "command_source", state.commandSourceKey);
  url.hash = state.activeTab;
  const method = push ? "pushState" : "replaceState";
  window.history[method]({}, "", url);
}

function applyStateToInputs() {
  const values = {
    "#source-query": state.sourceQuery,
    "#source-state": state.sourceState,
    "#source-mode": state.sourceMode,
    "#source-publisher": state.sourcePublisher,
    "#source-region": state.sourceRegion,
    "#run-status": state.runStatus,
    "#run-source": state.runSourceKey,
    "#run-window": state.runWindow,
    "#command-status": state.commandStatus,
    "#command-action": state.commandAction,
    "#command-source": state.commandSourceKey,
    "#source-detail-window": String(state.sourceDetailWindowHours),
  };
  for (const [selector, value] of Object.entries(values)) {
    const input = $(selector);
    if (input) input.value = value;
  }
  const fixtures = $("#include-fixtures");
  if (fixtures) fixtures.checked = state.includeFixtures;
  for (const button of $$("#overview-panel .panel-tools .segmented-button")) {
    const windowValue = {
      "24 hours": "24h",
      "7 days": "7d",
      "30 days": "30d",
    }[button.textContent.trim().toLowerCase()];
    button.classList.toggle("is-active", windowValue === state.runWindow);
    button.setAttribute("aria-pressed", String(windowValue === state.runWindow));
  }
}

function activateTab(tab, { push = false, focus = false } = {}) {
  const normalized = VALID_TABS.has(tab) ? tab : "overview";
  state.activeTab = normalized;
  for (const link of $$("[data-admin-tab]")) {
    const linkTab = link.dataset.adminTab || (link.getAttribute("href") || "").replace(/^#/, "");
    const active = linkTab === normalized;
    link.classList.toggle("active", active);
    link.classList.toggle("is-active", active);
    link.setAttribute("aria-selected", String(active));
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const panel of $$("[data-admin-panel]")) {
    panel.hidden = panel.id !== `${normalized}-panel`;
  }
  syncUrl({ push });
  if (focus) {
    const panel = $(`#${normalized}-panel`);
    const title = panel?.querySelector("h1, h2");
    if (title) {
      title.setAttribute("tabindex", "-1");
      title.focus({ preventScroll: true });
    }
  }
  renderAllVisualizations();
}

function renderOverview() {
  const overview = state.overview;
  if (!overview) return;
  const summary = overview.summary || {};
  const policy = overview.policy || {};
  const allowed = policy.allowed === true;
  const banner = $("#policy-banner");
  if (banner) {
    banner.classList.toggle("allowed", allowed);
    banner.classList.toggle("blocked", !allowed);
  }
  const policySymbol = $("#policy-symbol");
  const policyTitle = $("#policy-title");
  const policyReason = $("#policy-reason");
  const policyCode = $("#policy-code");
  if (policySymbol) policySymbol.textContent = allowed ? "✓" : "!";
  if (policyTitle) {
    policyTitle.textContent = allowed
      ? "Refresh network access is admitted"
      : "Refresh network access is blocked";
  }
  if (policyReason) {
    policyReason.textContent = boundedCopy(
      policy.reason,
      allowed
        ? "Current operator policy permits reviewed source refreshes."
        : "Source policy has not admitted refresh work.",
    );
  }
  if (policyCode) policyCode.textContent = boundedCopy(policy.code, allowed ? "allowed" : "denied");

  const metrics = {
    "#metric-sources": summary.sources,
    "#metric-active": summary.active_sources,
    "#metric-due": summary.due_sources,
    "#metric-running": summary.running_runs,
    "#metric-failed": summary.failed_runs_24h,
    "#metric-events": summary.catalog_events,
    "#metric-commands": summary.pending_commands,
  };
  for (const [selector, value] of Object.entries(metrics)) {
    const metric = $(selector);
    if (metric) metric.textContent = formatCount(value);
  }
  const attentionMetrics = {
    "#attention-failed": summary.failed_runs_24h,
    "#attention-due": summary.due_sources,
    "#attention-running": summary.running_runs,
    "#attention-commands": summary.pending_commands,
  };
  for (const [selector, value] of Object.entries(attentionMetrics)) {
    const metric = $(selector);
    if (metric) metric.textContent = formatCount(value);
  }
  const actionable =
    finiteNumber(summary.failed_runs_24h) +
    finiteNumber(summary.due_sources) +
    finiteNumber(summary.running_runs) +
    finiteNumber(summary.pending_commands);
  const attentionSummary = $("#attention-summary");
  if (attentionSummary) {
    attentionSummary.textContent =
      actionable === 0
        ? "Queue clear"
        : `${formatCount(actionable)} ${actionable === 1 ? "signal" : "signals"}`;
  }
  const managementState = $("#management-state");
  if (managementState) {
    const failed = finiteNumber(summary.failed_runs_24h);
    const running = finiteNumber(summary.running_runs);
    const pending = finiteNumber(summary.pending_commands);
    const due = finiteNumber(summary.due_sources);
    managementState.classList.toggle("needs-attention", failed > 0);
    managementState.classList.toggle("is-live", failed === 0 && (running > 0 || pending > 0));
    const copy = $("span:last-child", managementState);
    if (copy) {
      copy.textContent =
        failed > 0
          ? `${formatCount(failed)} ${failed === 1 ? "failure needs" : "failures need"} review`
          : running > 0
            ? `${formatCount(running)} ${running === 1 ? "run" : "runs"} in progress`
            : pending > 0
              ? `${formatCount(pending)} ${pending === 1 ? "control" : "controls"} pending`
              : due > 0
                ? `${formatCount(due)} ${due === 1 ? "source is" : "sources are"} due`
                : "No immediate action";
    }
  }
  const diagnosticsSummary = $("#diagnostics-summary");
  if (diagnosticsSummary) {
    diagnosticsSummary.textContent = `${formatCount(summary.sources)} sources · ${formatCount(
      summary.catalog_events,
    )} catalog events`;
  }
  const latest = $("#latest-success-at");
  if (latest) {
    latest.textContent = overview.latest_success_at
      ? `${formatDateTime(overview.latest_success_at)} · ${relativeTime(overview.latest_success_at)}`
      : "No successful refresh reported";
  }
  const fixtureCount = finiteNumber(summary.fixture_sources);
  const fixtureSummary = $("#fixture-summary");
  if (fixtureSummary) {
    fixtureSummary.textContent = `${formatCount(fixtureCount)} fixture ${
      fixtureCount === 1 ? "source" : "sources"
    } hidden by default`;
  }
  const generated = $("#overview-generated");
  if (generated) {
    generated.textContent = overview.generated_at
      ? `Snapshot ${relativeTime(overview.generated_at)}`
      : "Snapshot time not reported";
  }
  const dueCopy = $("#refresh-due-count");
  if (dueCopy) {
    dueCopy.textContent = `${formatCount(summary.due_sources)} ${
      finiteNumber(summary.due_sources) === 1 ? "source is" : "sources are"
    } currently due.`;
  }

  updateCommandControls();
  renderAllVisualizations();
  schedulePolling();
}

function updateCommandControls() {
  const policyAllowed = state.overview?.policy?.allowed === true;
  const due = finiteNumber(state.overview?.summary?.due_sources);
  const duePending = state.commands.some(
    (command) =>
      command.action === "refresh_due" && PENDING_STATUSES.has(String(command.status).toLowerCase()),
  );
  const dueButton = $("#open-refresh-due");
  if (!dueButton) return;
  dueButton.disabled = !policyAllowed || due === 0 || duePending;
  dueButton.title = !policyAllowed
    ? "Network admission policy currently blocks refresh commands"
    : due === 0
      ? "No sources are currently due"
      : duePending
        ? "A due-source command is already pending"
        : "";
}

async function loadOverview() {
  const overview = await api("/admin/v1/ingestion/overview");
  state.overview = overview;
  renderOverview();
  return overview;
}

function selectOptions(target, items, defaultLabel, selectedValue) {
  if (!target) return;
  const options = [node("option", "", defaultLabel)];
  options[0].value = "";
  for (const item of items || []) {
    const value = typeof item?.value === "string" ? item.value : "";
    if (!value) continue;
    const count = optionalNumber(item.count);
    const label = typeof item.label === "string" ? item.label : humanize(value);
    const option = node(
      "option",
      "",
      count === null ? label : `${label} (${formatCount(count)})`,
    );
    option.value = value;
    options.push(option);
  }
  target.replaceChildren(...options);
  target.value = selectedValue;
  if (target.value !== selectedValue) target.value = "";
}

function renderFilterMetadata() {
  const filters = state.filters;
  if (!filters) return;
  selectOptions($("#source-mode"), filters.modes, "All collection modes", state.sourceMode);
  selectOptions(
    $("#source-publisher"),
    filters.publishers,
    "All publishers",
    state.sourcePublisher,
  );
  selectOptions($("#source-region"), filters.regions, "All regions", state.sourceRegion);
  selectOptions(
    $("#source-state"),
    filters.source_states,
    "All source states",
    state.sourceState === "all" ? "" : state.sourceState,
  );
  if ($("#source-state") && state.sourceState === "all") $("#source-state").value = "";
  selectOptions($("#run-status"), filters.run_statuses, "Any status", state.runStatus);

  const detailWindow = $("#source-detail-window");
  if (detailWindow) {
    const windowItems = (filters.window_hours || []).map((hours) => ({
      value: String(hours),
      label: hours < 48 ? "Last 24 hours" : hours < 720 ? "Last 7 days" : "Last 30 days",
    }));
    selectOptions(
      detailWindow,
      windowItems,
      "History window",
      String(state.sourceDetailWindowHours),
    );
  }
}

async function loadFilterMetadata() {
  try {
    state.filters = await api(
      `/admin/v1/ingestion/filters?include_fixtures=${String(state.includeFixtures)}`,
    );
    renderFilterMetadata();
    applyStateToInputs();
    return true;
  } catch (_error) {
    return false;
  }
}

async function loadCurrentBuild() {
  try {
    state.currentBuild = await api("/versionz");
  } catch (_error) {
    state.currentBuild = null;
  }
  renderLogic();
  return state.currentBuild;
}

function sourceQueryString() {
  const query = new URLSearchParams({
    query: state.sourceQuery,
    state: state.sourceState,
    include_fixtures: String(state.includeFixtures),
    limit: String(SOURCE_PAGE_SIZE),
    offset: String(state.sourceOffset),
  });
  if (state.sourceMode) query.set("mode", state.sourceMode);
  if (state.sourcePublisher) query.set("publisher", state.sourcePublisher);
  if (state.sourceRegion) query.set("region", state.sourceRegion);
  return query.toString();
}

async function loadSources() {
  const generation = ++state.sourceGeneration;
  setInlineError($("#sources-error"));
  const page = await api(`/admin/v1/ingestion/sources?${sourceQueryString()}`);
  if (generation !== state.sourceGeneration) return false;
  state.sources = Array.isArray(page?.items) ? page.items : [];
  state.sourceTotal = finiteNumber(page?.total);
  renderSources();
  renderAllVisualizations();
  return true;
}

async function loadOverviewSources() {
  const query = new URLSearchParams({
    state: "all",
    include_fixtures: "false",
    limit: String(SOURCE_PAGE_SIZE),
    offset: "0",
  });
  const page = await api(`/admin/v1/ingestion/sources?${query}`);
  state.overviewSources = Array.isArray(page?.items) ? page.items : [];
  state.overviewSourceTotal = finiteNumber(page?.total);
  renderAllVisualizations();
  return true;
}

function sourceCanRefresh(source) {
  if (state.overview?.policy?.allowed !== true || source?.enabled !== true) return false;
  const effective = String(source?.effective_status || "").toLowerCase();
  return !BLOCKED_SOURCE_STATUSES.has(effective);
}

function pendingSourceKeys() {
  return new Set(
    state.commands
      .filter(
        (command) =>
          command.action === "refresh_source" &&
          command.source_key &&
          PENDING_STATUSES.has(String(command.status).toLowerCase()),
      )
      .map((command) => command.source_key),
  );
}

function sourceIdentityCell(source) {
  const cell = node("td");
  cell.dataset.label = "Source";
  const stack = node("div", "source-identity");
  const inspect = node("button", "source-title-link", source.display_name || source.source_key);
  inspect.type = "button";
  inspect.dataset.sourceKey = source.source_key;
  inspect.title = `Inspect ${source.display_name || source.source_key} end to end`;
  inspect.addEventListener("click", () => {
    void openSourceDetail(source.source_key, { push: true });
  });
  const seedUrl = safeExternalUrl(source.seed_url);
  const identityTop = node("div", "source-title-row");
  identityTop.append(inspect);
  if (seedUrl) {
    const title = node("a", "source-seed-link", "↗");
    title.href = seedUrl;
    title.target = "_blank";
    title.rel = "noopener noreferrer";
    title.title = "Open reviewed source seed";
    title.setAttribute("aria-label", "Open reviewed source seed in a new tab");
    identityTop.append(title);
  }
  append(
    stack,
    identityTop,
    node("small", "", [source.publisher, source.region].filter(Boolean).join(" · ")),
    node("code", "source-key", source.source_key),
    node("small", "", humanize(source.mode)),
  );
  cell.append(stack);
  return cell;
}

function sourceAdmissionCell(source) {
  const cell = node("td");
  cell.dataset.label = "Admission";
  const stack = node("div", "cell-stack");
  append(
    stack,
    statusBadge(source.effective_status || (source.enabled ? "active" : "disabled")),
    node("small", "", `Review: ${humanize(source.review_status)}`),
    node("small", "", source.enabled ? "Registry enabled" : "Registry disabled"),
  );
  cell.append(stack);
  return cell;
}

function sourceCadenceCell(source) {
  const cell = node("td");
  cell.dataset.label = "Cadence";
  const stack = node("div", "cell-stack");
  if (source.due) stack.append(statusBadge("due"));
  append(
    stack,
    node(
      "span",
      "",
      source.next_due_at
        ? `Next ${relativeTime(source.next_due_at)}`
        : "Next cadence not reported",
    ),
    node(
      "small",
      "",
      source.last_succeeded_at
        ? `Last success ${relativeTime(source.last_succeeded_at)}`
        : "No successful run",
    ),
  );
  cell.append(stack);
  return cell;
}

function sourceEventCell(source) {
  const cell = node("td");
  cell.dataset.label = "Events";
  const stack = node("div", "cell-stack");
  append(
    stack,
    node("strong", "", formatCount(source.event_count)),
    node("small", "", "Current observations"),
  );
  cell.append(stack);
  return cell;
}

function latestRunCell(source) {
  const cell = node("td");
  cell.dataset.label = "Latest run";
  const run = source.latest_run;
  if (!run) {
    cell.append(node("span", "run-detail", "No refresh run"));
    return cell;
  }
  const stack = node("div", "cell-stack");
  append(
    stack,
    statusBadge(run.status),
    node(
      "small",
      "",
      `${formatCount(run.canonical_count)} canonical · ${formatCount(
        run.candidate_count,
      )} candidates`,
    ),
    node(
      "small",
      "",
      run.completed_at
        ? `${relativeTime(run.completed_at)} · attempt ${finiteNumber(run.attempt_count)}`
        : `Started ${relativeTime(run.started_at)}`,
    ),
  );
  if (run.duration_ms !== null && run.duration_ms !== undefined) {
    stack.append(node("small", "", `Duration ${formatDuration(run.duration_ms)}`));
  }
  if (run.error) stack.append(node("span", "run-error", boundedCopy(run.error)));
  cell.append(stack);
  return cell;
}

function sourceActionCell(source, pendingKeys) {
  const cell = node("td", "source-action-cell");
  cell.dataset.label = "Actions";
  const controls = node("div", "row-actions");
  const details = node("button", "button button-quiet source-details", "Inspect");
  details.type = "button";
  details.dataset.sourceKey = source.source_key;
  details.addEventListener("click", () => {
    void openSourceDetail(source.source_key, { push: true });
  });
  const pending = pendingKeys.has(source.source_key);
  const button = node("button", "button button-secondary source-refresh", pending ? "Queued" : "Refresh");
  button.type = "button";
  button.disabled = pending || !sourceCanRefresh(source);
  button.dataset.sourceKey = source.source_key;
  button.setAttribute("aria-busy", String(pending));
  if (pending) {
    button.title = "A refresh command for this source is pending";
  } else if (!sourceCanRefresh(source)) {
    button.title = "This source is not currently admitted for refresh";
  } else {
    button.title = `Queue a refresh for ${source.display_name || source.source_key}`;
  }
  button.addEventListener("click", () => {
    void submitCommand("refresh_source", source.source_key, button);
  });
  append(controls, details, button);
  cell.append(controls);
  return cell;
}

function sourceMatchesClientFilters(source) {
  if (state.sourceHealth === "healthy") {
    return (
      ["active", "due"].includes(String(source.effective_status)) &&
      (!source.latest_run || source.latest_run.status === "succeeded")
    );
  }
  if (["failed", "failing"].includes(state.sourceHealth)) {
    return source.latest_run?.status === "failed";
  }
  if (["no_runs", "never_run"].includes(state.sourceHealth)) return !source.latest_run;
  if (state.sourceHealth === "degraded") {
    return (
      source.latest_run?.status === "failed" ||
      ["policy_blocked", "review_expired", "unreviewed"].includes(
        String(source.effective_status),
      )
    );
  }
  if (state.sourceHealth === "stale") return source.due === true;
  return true;
}

function renderSources() {
  const rows = $("#source-rows");
  if (!rows) return;
  rows.replaceChildren();
  const pendingKeys = pendingSourceKeys();
  const visibleSources = state.sources.filter(sourceMatchesClientFilters);
  for (const source of visibleSources) {
    const row = node("tr");
    row.dataset.sourceKey = source.source_key;
    if (source.source_key === state.selectedSourceKey) row.classList.add("selected");
    append(
      row,
      sourceIdentityCell(source),
      sourceAdmissionCell(source),
      sourceCadenceCell(source),
      sourceEventCell(source),
      latestRunCell(source),
      sourceActionCell(source, pendingKeys),
    );
    rows.append(row);
  }
  setHidden($("#sources-empty"), visibleSources.length !== 0);
  const start = state.sourceTotal === 0 ? 0 : state.sourceOffset + 1;
  const end = Math.min(state.sourceOffset + state.sources.length, state.sourceTotal);
  const sourceCount = $("#sources-count");
  if (sourceCount) {
    sourceCount.textContent =
      state.sourceTotal === 0
        ? "No sources loaded"
        : `Showing ${formatCount(start)}–${formatCount(end)} of ${formatCount(
            state.sourceTotal,
          )}`;
    if (visibleSources.length !== state.sources.length) {
      sourceCount.textContent += ` · ${formatCount(visibleSources.length)} match local health`;
    }
  }
  const page = Math.floor(state.sourceOffset / SOURCE_PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(state.sourceTotal / SOURCE_PAGE_SIZE));
  const pageCopy = $("#sources-page");
  if (pageCopy) pageCopy.textContent = `Page ${formatCount(page)} of ${formatCount(pages)}`;
  const previous = $("#sources-previous");
  const next = $("#sources-next");
  if (previous) previous.disabled = state.sourceOffset === 0;
  if (next) next.disabled = state.sourceOffset + state.sources.length >= state.sourceTotal;
}

function runQueryString() {
  const query = new URLSearchParams({
    include_fixtures: String(state.includeFixtures),
    limit: String(RUN_PAGE_SIZE),
    offset: String(state.runOffset),
  });
  if (state.runStatus) query.set("status", state.runStatus);
  if (state.runSourceKey) query.set("source_key", state.runSourceKey);
  const windowHours = runWindowHours();
  if (windowHours) query.set("window_hours", String(windowHours));
  return query.toString();
}

function runWindowHours() {
  return (
    {
      "24h": 24,
      "7d": 168,
      "30d": 720,
    }[state.runWindow] || 0
  );
}

async function loadRuns() {
  const generation = ++state.runGeneration;
  setInlineError($("#runs-error"));
  const page = await api(`/admin/v1/ingestion/runs?${runQueryString()}`);
  if (generation !== state.runGeneration) return false;
  state.runs = Array.isArray(page?.items) ? page.items : [];
  state.runTotal = finiteNumber(page?.total);
  renderRuns();
  renderAllVisualizations();
  renderLogic();
  return true;
}

async function loadOverviewRuns() {
  const query = new URLSearchParams({
    include_fixtures: "false",
    limit: String(RUN_PAGE_SIZE),
    offset: "0",
  });
  const windowHours = runWindowHours();
  if (windowHours) query.set("window_hours", String(windowHours));
  const page = await api(`/admin/v1/ingestion/runs?${query}`);
  state.overviewRuns = Array.isArray(page?.items) ? page.items : [];
  state.overviewRunTotal = finiteNumber(page?.total);
  renderAllVisualizations();
  return true;
}

function activityMarker(status, symbol) {
  const normalized = String(status || "").toLowerCase();
  const kind = normalized === "failed" ? "failed" : PENDING_STATUSES.has(normalized) ? "pending" : "";
  return node("span", `activity-marker ${kind}`.trim(), symbol);
}

function runInWindow(run) {
  return Boolean(run);
}

function sourceInspectButton(sourceKey) {
  const safeKey = safeSourceKey(sourceKey);
  if (!safeKey) return null;
  const button = node("button", "button-link", "Inspect source");
  button.type = "button";
  button.dataset.sourceKey = safeKey;
  button.addEventListener("click", () => {
    void openSourceDetail(safeKey, { push: true });
  });
  return button;
}

function runProvenanceCopy(run) {
  if (run.provenance_status === "claim_recorded") {
    return `Worker claim · ${run.release_revision || "unknown release"} · source r${formatCount(
      run.source_revision,
    )}`;
  }
  if (run.provenance_status === "source_revision_only") {
    return `Source revision recorded · source r${formatCount(
      run.source_revision,
    )} · worker claim unavailable`;
  }
  return "Legacy run · worker claim unavailable";
}

function renderRuns() {
  const list = $("#run-list");
  if (!list) return;
  list.replaceChildren();
  const visibleRuns = state.runs.filter(runInWindow);
  for (const run of visibleRuns) {
    const item = node("li", "activity-item");
    item.dataset.sourceKey = run.source_key || "";
    const copy = node("div", "activity-copy");
    const top = node("div", "activity-topline");
    append(
      top,
      node("strong", "", run.display_name || run.source_key || "Unknown source"),
      statusBadge(run.status),
    );
    const meta = node("div", "activity-meta");
    append(
      meta,
      node("code", "", run.run_key || "run key unavailable"),
      node(
        "span",
        "",
        run.completed_at
          ? formatDateTime(run.completed_at)
          : `Started ${formatDateTime(run.started_at)}`,
      ),
      node("span", "", `Attempt ${formatCount(run.attempt_count)}`),
      run.duration_ms === null || run.duration_ms === undefined
        ? null
        : node("span", "", formatDuration(run.duration_ms)),
      node("span", "provenance-copy", runProvenanceCopy(run)),
    );
    const result = node(
      "p",
      "activity-result",
      `${formatCount(run.canonical_count)} canonical from ${formatCount(
        run.candidate_count,
      )} candidates`,
    );
    append(copy, top, meta, result, sourceInspectButton(run.source_key));
    if (run.error) copy.append(node("p", "run-error", boundedCopy(run.error)));
    append(
      item,
      activityMarker(run.status, String(run.status).toLowerCase() === "failed" ? "!" : "↻"),
      copy,
    );
    list.append(item);
  }
  setHidden($("#runs-empty"), visibleRuns.length !== 0);
  const count = $("#runs-count");
  if (count) {
    count.textContent = `${formatCount(visibleRuns.length)} shown · ${formatCount(
      state.runTotal,
    )} total`;
  }
}

async function loadCommands() {
  setInlineError($("#commands-error"));
  const page = await api(`/admin/v1/ingestion/commands?limit=${COMMAND_PAGE_SIZE}`);
  state.commands = Array.isArray(page?.items) ? page.items : [];
  renderCommands();
  renderSources();
  renderAllVisualizations();
  renderLogic();
  updateCommandControls();
  schedulePolling();
  return state.commands;
}

function commandTitle(command) {
  if (command.action === "refresh_due") return "Refresh due sources";
  if (command.action === "refresh_source") {
    return command.source_key ? `Refresh ${command.source_key}` : "Refresh source";
  }
  return humanize(command.action);
}

function commandMatchesFilters(command) {
  if (
    state.commandStatus === "pending" &&
    !PENDING_STATUSES.has(String(command.status).toLowerCase())
  ) {
    return false;
  }
  if (
    state.commandStatus &&
    state.commandStatus !== "pending" &&
    command.status !== state.commandStatus
  ) {
    return false;
  }
  if (state.commandAction && command.action !== state.commandAction) return false;
  if (state.commandSourceKey) {
    const query = state.commandSourceKey.toLowerCase();
    const source = String(command.source_key || "").toLowerCase();
    const identifier = String(command.command_id || "").toLowerCase();
    if (!source.includes(query) && !identifier.includes(query)) return false;
  }
  return true;
}

function commandIdentityCopy(releaseRevision, sourceRevision, imageDigest) {
  const parts = [];
  if (releaseRevision) parts.push(String(releaseRevision));
  if (sourceRevision) parts.push(`source r${formatCount(sourceRevision)}`);
  if (imageDigest) parts.push(`${String(imageDigest).slice(0, 18)}…`);
  return parts.length ? parts.join(" · ") : "Unavailable";
}

function commandProvenance(command) {
  const wrapper = node("div", "command-provenance");
  const accepted = node("span");
  append(
    accepted,
    node("strong", "", "Accepted by"),
    node(
      "code",
      "",
      commandIdentityCopy(
        command.release_revision,
        command.source_revision,
        command.image_digest,
      ),
    ),
  );
  const claimed = node("span");
  const hasExecutorIdentity = Boolean(
    command.executor_release_revision ||
      command.executor_source_revision ||
      command.executor_image_digest,
  );
  const pending = PENDING_STATUSES.has(String(command.status || "").toLowerCase());
  append(
    claimed,
    node("strong", "", "Claimed by"),
    node(
      "code",
      "",
      hasExecutorIdentity
        ? commandIdentityCopy(
            command.executor_release_revision,
            command.executor_source_revision,
            command.executor_image_digest,
          )
        : pending
          ? "Not claimed yet"
          : "Unavailable",
    ),
  );
  append(wrapper, accepted, claimed);
  return wrapper;
}

function commandResultCopy(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    return boundedCopy(result);
  }
  const entries = Object.entries(result)
    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
    .slice(0, 5);
  if (!entries.length) return boundedCopy(result);
  return entries
    .map(([key, value]) => `${humanize(key)}: ${boundedCopy(value)}`)
    .join(" · ");
}

function renderCommands() {
  const list = $("#command-list");
  if (!list) return;
  list.replaceChildren();
  const visibleCommands = state.commands.filter(commandMatchesFilters);
  for (const command of visibleCommands) {
    const item = node("li", "activity-item");
    const copy = node("div", "activity-copy");
    const top = node("div", "activity-topline");
    append(top, node("strong", "", commandTitle(command)), statusBadge(command.status));
    const meta = node("div", "activity-meta");
    append(
      meta,
      node("code", "", command.command_id || "command id unavailable"),
      node("span", "", formatDateTime(command.requested_at)),
    );
    if (command.started_at) {
      meta.append(node("span", "", `Started ${relativeTime(command.started_at)}`));
    }
    if (command.completed_at) {
      meta.append(node("span", "", `Completed ${relativeTime(command.completed_at)}`));
    }
    append(copy, top, meta, commandProvenance(command));
    const resultCopy = commandResultCopy(command.result);
    if (resultCopy) copy.append(node("p", "activity-result", resultCopy));
    if (command.error_code) {
      copy.append(node("p", "run-error", `Error code: ${boundedCopy(command.error_code)}`));
    }
    append(copy, sourceInspectButton(command.source_key));
    const normalized = String(command.status || "").toLowerCase();
    const symbol =
      normalized === "failed" ? "!" : PENDING_STATUSES.has(normalized) ? "…" : "✓";
    append(item, activityMarker(normalized, symbol), copy);
    list.append(item);
  }
  setHidden($("#commands-empty"), visibleCommands.length !== 0);
  const count = $("#commands-count");
  if (count) {
    count.textContent = `${formatCount(visibleCommands.length)} of ${formatCount(
      state.commands.length,
    )} recent commands`;
  }
}

function vizBars(target, entries, emptyCopy) {
  if (!target) return;
  target.replaceChildren();
  const usable = entries
    .map((entry) => ({ ...entry, value: finiteNumber(entry.value) }))
    .filter((entry) => entry.value > 0);
  if (!usable.length) {
    target.append(node("p", "viz-empty", emptyCopy));
    return;
  }
  const maximum = Math.max(...usable.map((entry) => entry.value), 1);
  const list = node("div", "viz-bars");
  for (const entry of usable) {
    const row = node("div", "viz-row");
    const label = node("span", "viz-label", entry.label);
    const track = node("span", "viz-track");
    const fill = node("span", `viz-fill ${entry.kind || ""}`.trim());
    fill.style.setProperty("--bar-size", `${Math.max(3, (entry.value / maximum) * 100)}%`);
    track.append(fill);
    append(row, label, track, node("strong", "viz-value", formatCount(entry.value)));
    list.append(row);
  }
  target.append(list);
}

function overviewSourceCollection() {
  return Array.isArray(state.overviewSources) ? state.overviewSources : state.sources;
}

function overviewRunCollection() {
  return Array.isArray(state.overviewRuns) ? state.overviewRuns : state.runs;
}

function sourceOperationalState(source) {
  if (String(source?.latest_run?.status || "").toLowerCase() === "failed") return "failed";
  if (!source?.latest_run) return "never_run";
  return String(source?.effective_status || "unknown").toLowerCase();
}

function sourceUrgency(source) {
  const status = sourceOperationalState(source);
  if (status === "failed") return 0;
  if (["policy_blocked", "review_expired", "unreviewed", "blocked"].includes(status)) return 1;
  if (status === "running") return 2;
  if (source?.due === true || status === "due") return 3;
  if (status === "never_run") return 4;
  if (status === "active") return 5;
  return 6;
}

function sourceSearchCopy(source) {
  return [
    source?.source_key,
    source?.display_name,
    source?.publisher,
    source?.region,
    source?.mode,
    source?.seed_url,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function sourceSeedHost(source) {
  try {
    return new URL(source?.seed_url || "").hostname.replace(/^www\./, "");
  } catch (_error) {
    return "";
  }
}

function overviewSourceCandidates() {
  const sources = overviewSourceCollection();
  const query = state.overviewSourceQuery.trim().toLowerCase();
  if (query) {
    return sources
      .filter((source) => sourceSearchCopy(source).includes(query))
      .sort((left, right) => {
        const leftName = String(left.display_name || left.source_key).toLowerCase();
        const rightName = String(right.display_name || right.source_key).toLowerCase();
        const leftRank = leftName.startsWith(query) ? 0 : sourceUrgency(left) + 1;
        const rightRank = rightName.startsWith(query) ? 0 : sourceUrgency(right) + 1;
        return leftRank - rightRank || leftName.localeCompare(rightName);
      })
      .slice(0, 6);
  }

  const byKey = new Map(sources.map((source) => [source.source_key, source]));
  const ordered = [];
  const seen = new Set();
  const add = (source) => {
    if (!source || seen.has(source.source_key)) return;
    seen.add(source.source_key);
    ordered.push(source);
  };
  for (const run of overviewRunCollection()) add(byKey.get(run.source_key));
  for (const source of [...sources].sort((left, right) => sourceUrgency(left) - sourceUrgency(right))) {
    add(source);
  }
  return ordered.slice(0, 4);
}

function sourceLauncherResult(source) {
  const button = node("button", "source-launcher-result");
  button.type = "button";
  button.dataset.sourceKey = source.source_key;
  button.title = `Inspect ${source.display_name || source.source_key}`;
  const identity = node("span", "source-launcher-identity");
  const host = sourceSeedHost(source);
  append(
    identity,
    node("strong", "", source.display_name || source.source_key),
    node(
      "small",
      "",
      [source.publisher, host || humanize(source.mode)].filter(Boolean).join(" · "),
    ),
  );
  const facts = node("span", "source-launcher-facts");
  append(
    facts,
    statusBadge(sourceOperationalState(source)),
    node("span", "", `${formatCount(source.event_count)} events`),
    node("span", "source-launcher-arrow", "→"),
  );
  append(button, identity, facts);
  button.addEventListener("click", () => {
    void openSourceDetail(source.source_key, { push: true });
  });
  return button;
}

function renderSourceLauncher() {
  const target = $("#overview-source-results");
  if (!target) return;
  const sources = overviewSourceCollection();
  const query = state.overviewSourceQuery.trim();
  const matches = overviewSourceCandidates();
  target.replaceChildren();
  if (!sources.length) {
    target.append(node("p", "admin-empty-copy", "Named sources are not available yet."));
  } else if (!matches.length) {
    target.append(
      node(
        "p",
        "admin-empty-copy",
        `No named source matches “${query}”. Open Sources to search the full registry.`,
      ),
    );
  } else {
    const list = node("div", "source-launcher-list");
    for (const source of matches) list.append(sourceLauncherResult(source));
    target.append(list);
  }

  const summary = $("#overview-source-index-summary");
  if (summary) {
    const total = state.overviewSourceTotal || sources.length;
    summary.textContent = query
      ? `${formatCount(matches.length)} ${
          matches.length === 1 ? "match" : "matches"
        } shown · search also checks source URLs and keys`
      : `${formatCount(total)} named sources indexed · showing recent and actionable sources`;
  }
}

function renderAttentionSources() {
  const target = $("#attention-source-list");
  if (!target) return;
  target.replaceChildren();
  const sources = overviewSourceCollection()
    .filter((source) => sourceUrgency(source) <= 4)
    .sort((left, right) => {
      const priority = sourceUrgency(left) - sourceUrgency(right);
      if (priority) return priority;
      return String(left.display_name || left.source_key).localeCompare(
        String(right.display_name || right.source_key),
      );
    })
    .slice(0, 4);
  if (!sources.length) {
    const clear = node("div", "attention-clear");
    append(
      clear,
      node("span", "", "✓"),
      node("p", "", "No source-level issues in the loaded registry."),
    );
    target.append(clear);
    return;
  }
  const heading = node("p", "attention-list-label", "Priority sources");
  const list = node("div", "attention-list");
  for (const source of sources) {
    const button = node("button", "attention-source");
    button.type = "button";
    button.dataset.sourceKey = source.source_key;
    const copy = node("span");
    append(
      copy,
      node("strong", "", source.display_name || source.source_key),
      node(
        "small",
        "",
        source.latest_run?.completed_at
          ? `Last run ${relativeTime(source.latest_run.completed_at)}`
          : source.latest_run?.started_at
            ? `Started ${relativeTime(source.latest_run.started_at)}`
            : "No refresh run recorded",
      ),
    );
    append(
      button,
      statusBadge(sourceOperationalState(source)),
      copy,
      node("span", "source-launcher-arrow", "→"),
    );
    button.addEventListener("click", () => {
      void openSourceDetail(source.source_key, { push: true });
    });
    list.append(button);
  }
  append(target, heading, list);
}

function openSourceWorkspace({ query = "", sourceState = "all" } = {}) {
  state.sourceQuery = query;
  state.sourceState = sourceState;
  state.sourceMode = "";
  state.sourcePublisher = "";
  state.sourceRegion = "";
  state.sourceHealth = "";
  state.sourceOffset = 0;
  applyStateToInputs();
  activateTab("sources", { push: true, focus: true });
  void loadSources().catch((error) => setInlineError($("#sources-error"), error.message));
}

function openRunWorkspace(status = "") {
  state.runStatus = status;
  state.runSourceKey = "";
  state.runOffset = 0;
  applyStateToInputs();
  activateTab("runs", { push: true, focus: true });
  void loadRuns().catch((error) => setInlineError($("#runs-error"), error.message));
}

function openCommandWorkspace(status = "") {
  state.commandStatus = status;
  state.commandAction = "";
  state.commandSourceKey = "";
  applyStateToInputs();
  activateTab("commands", { push: true, focus: true });
  renderCommands();
}

function handleOverviewRoute(route) {
  if (route === "all-sources") openSourceWorkspace();
  else if (route === "failed-sources") openSourceWorkspace({ sourceState: "failed" });
  else if (route === "due-sources") openSourceWorkspace({ sourceState: "due" });
  else if (route === "all-runs") openRunWorkspace();
  else if (route === "failed-runs") openRunWorkspace("failed");
  else if (route === "running-runs") openRunWorkspace("running");
  else if (route === "pending-commands") openCommandWorkspace("pending");
  else if (route === "refresh-due") $("#open-refresh-due")?.click();
}

function stageAction(label, route, primary = false) {
  const button = node(
    "button",
    `button ${primary ? "button-primary" : "button-secondary"}`,
    label,
  );
  button.type = "button";
  button.dataset.overviewRoute = route;
  return button;
}

function pipelineFacts() {
  const completed = overviewRunCollection().filter((run) =>
    ["succeeded", "failed"].includes(run.status),
  );
  const succeeded = completed.filter((run) => run.status === "succeeded").length;
  const failed = completed.filter((run) => run.status === "failed").length;
  const candidates = completed.reduce(
    (total, run) => total + finiteNumber(run.candidate_count),
    0,
  );
  const canonical = completed.reduce(
    (total, run) => total + finiteNumber(run.canonical_count),
    0,
  );
  const durations = completed
    .map((run) => optionalNumber(run.duration_ms))
    .filter((duration) => duration !== null);
  return {
    completed: completed.length,
    succeeded,
    failed,
    candidates,
    canonical,
    yieldRate: candidates ? canonical / candidates : null,
    averageDuration: durations.length
      ? durations.reduce((total, duration) => total + duration, 0) / durations.length
      : null,
  };
}

function pipelineStages(facts) {
  const summary = state.overview?.summary || {};
  const running = finiteNumber(summary.running_runs);
  const due = finiteNumber(summary.due_sources);
  return [
    {
      key: "registry",
      label: "Registry",
      value: finiteNumber(summary.sources),
      note: "reviewed sources",
      tone: "ok",
    },
    {
      key: "admission",
      label: "Admission",
      value: finiteNumber(summary.active_sources),
      note: due ? `${formatCount(due)} due` : "all caught up",
      tone: due ? "attention" : "ok",
    },
    {
      key: "collect",
      label: "Collection",
      value: facts.completed + running,
      note: running
        ? `${formatCount(running)} running`
        : `${formatCount(facts.completed)} loaded runs`,
      tone: facts.failed ? "failed" : running ? "live" : "ok",
    },
    {
      key: "normalize",
      label: "Normalize",
      value: facts.candidates,
      note: "loaded candidates",
      tone: "ok",
    },
    {
      key: "publish",
      label: "Catalog",
      value: finiteNumber(summary.catalog_events),
      note: `${formatCount(facts.canonical)} loaded output`,
      tone: "ok",
    },
  ];
}

function renderOverviewStageDetail(stages, facts) {
  const target = $("#overview-stage-detail");
  if (!target) return;
  const summary = state.overview?.summary || {};
  const selected = stages.find((stage) => stage.key === state.overviewStage) || stages[0];
  const sources = overviewSourceCollection();
  const blocked = sources.filter((source) =>
    ["policy_blocked", "review_expired", "unreviewed", "disabled"].includes(
      String(source.effective_status || ""),
    ),
  ).length;
  const detailByStage = {
    registry: {
      eyebrow: "Source registry",
      title: "Reviewed source inventory",
      copy: "Named sources are the operational unit. Adapter families are kept in diagnostics.",
      metrics: [
        ["Reviewed", formatCount(summary.sources)],
        ["Indexed here", formatCount(state.overviewSourceTotal || sources.length)],
        ["Fixtures hidden", formatCount(summary.fixture_sources)],
      ],
      actions: [stageAction("Browse named sources", "all-sources", true)],
    },
    admission: {
      eyebrow: "Policy & cadence",
      title: "Refresh eligibility",
      copy: "Admission combines registry enablement, review state, policy, and the next cadence slot.",
      metrics: [
        ["Active", formatCount(summary.active_sources)],
        ["Due now", formatCount(summary.due_sources)],
        ["Blocked in index", formatCount(blocked)],
      ],
      actions: [
        stageAction("Inspect due sources", "due-sources", true),
        stageAction("Refresh due", "refresh-due"),
      ],
    },
    collect: {
      eyebrow: "Loaded run window",
      title: "Collection execution",
      copy: "These counts describe the bounded run ledger currently loaded, not an all-time fleet funnel.",
      metrics: [
        ["Completed", formatCount(facts.completed)],
        ["Succeeded", formatCount(facts.succeeded)],
        ["Failed", formatCount(facts.failed)],
        ["Average", formatDuration(facts.averageDuration)],
      ],
      actions: [
        stageAction("Open run ledger", "all-runs", true),
        stageAction("Inspect failures", "failed-runs"),
      ],
    },
    normalize: {
      eyebrow: "Loaded run output",
      title: "Extraction & normalization",
      copy: "Candidate and canonical counts come from the selected run window and loaded page.",
      metrics: [
        ["Candidates", formatCount(facts.candidates)],
        ["Canonical", formatCount(facts.canonical)],
        ["Yield", facts.yieldRate === null ? "No candidates" : formatPercent(facts.yieldRate)],
      ],
      actions: [stageAction("Inspect run output", "all-runs", true)],
    },
    publish: {
      eyebrow: "Canonical catalog",
      title: "Published event inventory",
      copy: "The catalog total is fleet-wide; loaded output shows only the bounded run ledger above.",
      metrics: [
        ["Catalog events", formatCount(summary.catalog_events)],
        ["Loaded output", formatCount(facts.canonical)],
        [
          "Latest success",
          state.overview?.latest_success_at
            ? relativeTime(state.overview.latest_success_at)
            : "Unavailable",
        ],
      ],
      actions: [],
    },
  };
  const detail = detailByStage[selected.key];
  target.replaceChildren();
  const copy = node("div", "overview-stage-copy");
  append(
    copy,
    node("p", "eyebrow", detail.eyebrow),
    node("h4", "", detail.title),
    node("p", "", detail.copy),
  );
  const metrics = node("dl", "overview-stage-metrics");
  for (const [label, value] of detail.metrics) {
    const item = node("div");
    append(item, node("dt", "", label), node("dd", "", value));
    metrics.append(item);
  }
  const actions = node("div", "overview-stage-actions");
  for (const action of detail.actions) actions.append(action);
  if (selected.key === "publish") {
    const consumer = node("a", "button button-primary", "Browse catalog ↗");
    consumer.href = "/app";
    actions.append(consumer);
  }
  append(target, copy, metrics, actions);
}

function renderPipelineChart() {
  const target = $("#pipeline-chart");
  if (!target) return;
  const facts = pipelineFacts();
  const stages = pipelineStages(facts);
  const running =
    finiteNumber(state.overview?.summary?.running_runs) > 0 || pendingCommandCount() > 0;
  target.replaceChildren();
  const list = node("ol", `operations-flow-list${running ? " has-live-work" : ""}`);
  stages.forEach((stage, index) => {
    const item = node("li", `flow-stage-item ${stage.tone}`);
    const button = node("button", "flow-stage");
    button.type = "button";
    button.dataset.overviewStage = stage.key;
    button.setAttribute("aria-pressed", String(stage.key === state.overviewStage));
    button.setAttribute("aria-controls", "overview-stage-detail");
    const copy = node("span", "flow-stage-copy");
    append(copy, node("strong", "", stage.label), node("small", "", stage.note));
    append(
      button,
      node("span", "flow-step", String(index + 1).padStart(2, "0")),
      copy,
      node("span", "flow-stage-value", formatCount(stage.value)),
    );
    item.append(button);
    if (index < stages.length - 1) {
      const connector = node("span", "flow-connector");
      connector.setAttribute("aria-hidden", "true");
      connector.append(node("i"));
      item.append(connector);
    }
    list.append(item);
  });
  target.append(list);
  renderOverviewStageDetail(stages, facts);
  const windowCopy = $("#pipeline-window-copy");
  if (windowCopy) {
    windowCopy.textContent =
      { "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days" }[
        state.runWindow
      ] || "Loaded run window";
  }
}

function renderHealthDistribution() {
  const counts = new Map();
  for (const source of overviewSourceCollection()) {
    const key = String(source.effective_status || "unknown");
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  vizBars(
    $("#health-distribution"),
    Array.from(counts, ([label, value]) => ({
      label: humanize(label),
      value,
      kind: label === "failed" || label.includes("blocked") ? "failed" : label,
    })),
    "Source health is not loaded yet.",
  );
}

function renderSourceTypeBreakdown() {
  const counts = new Map();
  for (const source of overviewSourceCollection()) {
    const key = String(source.mode || "unknown");
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  vizBars(
    $("#source-type-breakdown"),
    Array.from(counts, ([label, value]) => ({
      label: humanize(label),
      value,
      kind: "mode",
    })),
    "Collection modes will appear when sources load.",
  );
}

function renderActivitySummary() {
  const target = $("#activity-summary");
  if (!target) return;
  target.replaceChildren();
  const completed = overviewRunCollection().filter((run) =>
    ["succeeded", "failed"].includes(run.status),
  );
  const succeeded = completed.filter((run) => run.status === "succeeded").length;
  const candidates = completed.reduce(
    (total, run) => total + finiteNumber(run.candidate_count),
    0,
  );
  const canonical = completed.reduce(
    (total, run) => total + finiteNumber(run.canonical_count),
    0,
  );
  const durations = completed
    .map((run) => optionalNumber(run.duration_ms))
    .filter((duration) => duration !== null);
  const averageDuration = durations.length
    ? durations.reduce((total, duration) => total + duration, 0) / durations.length
    : null;
  const cards = [
    ["Run success", completed.length ? formatPercent(succeeded / completed.length) : "No runs"],
    ["Candidate yield", candidates ? formatPercent(canonical / candidates) : "No candidates"],
    ["Average duration", formatDuration(averageDuration)],
    ["Pending controls", formatCount(pendingCommandCount())],
  ];
  for (const [label, value] of cards) {
    const card = node("div", "detail-kpi");
    append(card, node("span", "", label), node("strong", "", value));
    target.append(card);
  }
}

function renderAllVisualizations() {
  renderSourceLauncher();
  renderAttentionSources();
  renderPipelineChart();
  renderHealthDistribution();
  renderSourceTypeBreakdown();
  renderActivitySummary();
}

function detailEndpoint(sourceKey) {
  const query = new URLSearchParams({
    window_hours: String(state.sourceDetailWindowHours),
    bucket_hours: state.sourceDetailWindowHours <= 24 ? "1" : "24",
    include_fixtures: String(state.includeFixtures),
  });
  return `/admin/v1/ingestion/sources/${encodeURIComponent(sourceKey)}?${query}`;
}

function renderDetailLoading(sourceKey) {
  const title = $("#source-detail-title");
  const key = $("#source-detail-key");
  const status = $("#source-detail-status");
  if (title) title.textContent = "Loading source observability…";
  if (key) key.textContent = sourceKey;
  if (status) status.replaceChildren(statusBadge("loading"));
  for (const selector of [
    "#source-detail-summary",
    "#source-detail-pipeline",
    "#source-detail-history",
    "#source-detail-config",
    "#source-detail-logic",
    "#source-detail-provenance",
  ]) {
    const target = $(selector);
    if (target) target.replaceChildren(node("p", "detail-loading", "Loading durable records…"));
  }
  setInlineError($("#source-detail-error"));
}

function detailKpi(label, value, note = "") {
  const card = node("div", "detail-kpi");
  append(card, node("span", "", label), node("strong", "", value));
  if (note) card.append(node("small", "", note));
  return card;
}

function renderDetailSummary(detail) {
  const target = $("#source-detail-summary");
  if (!target) return;
  target.replaceChildren();
  const summary = detail.summary || {};
  const source = detail.source || {};
  append(
    target,
    detailKpi("Catalog observations", formatCount(source.event_count), "Current source records"),
    detailKpi("Runs", formatCount(summary.total_runs), `Last ${formatCount(detail.window?.hours)}h`),
    detailKpi("Success rate", formatPercent(summary.success_rate), `${formatCount(summary.failed_runs)} failed`),
    detailKpi("Candidate yield", formatPercent(summary.yield_rate), `${formatCount(summary.canonical_count)} canonical`),
    detailKpi("Average duration", formatDuration(summary.average_duration_ms), `p95 ${formatDuration(summary.p95_duration_ms)}`),
  );
}

function pipelineStage(label, value, note, status = "active") {
  const item = node("li", "pipeline-stage");
  item.dataset.status = status;
  append(
    item,
    node("span", "pipeline-dot", ""),
    node("strong", "", label),
    node("b", "", value),
    node("small", "", note),
  );
  return item;
}

function renderDetailPipeline(detail) {
  const target = $("#source-detail-pipeline");
  if (!target) return;
  target.replaceChildren();
  const source = detail.source || {};
  const summary = detail.summary || {};
  const flow = node("ol", "pipeline-flow");
  append(
    flow,
    pipelineStage(
      "Registry & policy",
      humanize(source.effective_status),
      source.review_status === "reviewed" ? "Reviewed configuration" : "Review requires attention",
      source.policy_blocked ? "failed" : "active",
    ),
    pipelineStage(
      "Collection",
      formatCount(summary.total_runs),
      `${humanize(source.mode)} · ${formatCount(source.page_limit)} page limit`,
      summary.failed_runs > 0 ? "warning" : "active",
    ),
    pipelineStage(
      "Extraction",
      formatCount(summary.candidate_count),
      "Candidates observed in the selected window",
      summary.total_runs > 0 ? "active" : "idle",
    ),
    pipelineStage(
      "Normalize & dedupe",
      formatCount(summary.canonical_count),
      `${formatPercent(summary.yield_rate)} candidate yield`,
      summary.total_runs > 0 ? "active" : "idle",
    ),
    pipelineStage(
      "Catalog persistence",
      formatCount(source.event_count),
      "Current canonical source observations",
      source.event_count > 0 ? "active" : "idle",
    ),
  );
  target.append(flow);
}

function chartPoints(history, field, width, height, maximum) {
  if (!history.length) return "";
  return history
    .map((bucket, index) => {
      const x = history.length === 1 ? width / 2 : (index / (history.length - 1)) * width;
      const y = height - (finiteNumber(bucket[field]) / maximum) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function detailTableRegion(table, label) {
  const region = node("div", "detail-table-scroll");
  region.tabIndex = 0;
  region.setAttribute("role", "region");
  region.setAttribute("aria-label", label);
  region.append(table);
  return region;
}

function renderHistoryChart(detail) {
  const target = $("#source-detail-history");
  if (!target) return;
  target.replaceChildren();
  const history = Array.isArray(detail.history) ? detail.history : [];
  if (!history.length) {
    target.append(node("p", "admin-empty-copy", "No run buckets exist in this history window."));
    return;
  }
  const width = 640;
  const height = 150;
  const maximum = Math.max(
    1,
    ...history.flatMap((bucket) => [
      finiteNumber(bucket.candidate_count),
      finiteNumber(bucket.canonical_count),
    ]),
  );
  const figure = node("figure", "history-chart");
  const svg = svgNode("svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute(
    "aria-label",
    "Candidate and canonical event volume across the selected history window",
  );
  for (const ratio of [0, 0.5, 1]) {
    const line = svgNode("line");
    const y = String(height - ratio * height);
    line.setAttribute("x1", "0");
    line.setAttribute("x2", String(width));
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    line.setAttribute("class", "history-gridline");
    svg.append(line);
  }
  const candidateLine = svgNode("polyline");
  candidateLine.setAttribute(
    "points",
    chartPoints(history, "candidate_count", width, height, maximum),
  );
  candidateLine.setAttribute("class", "history-line candidate");
  const canonicalLine = svgNode("polyline");
  canonicalLine.setAttribute(
    "points",
    chartPoints(history, "canonical_count", width, height, maximum),
  );
  canonicalLine.setAttribute("class", "history-line canonical");
  append(svg, candidateLine, canonicalLine);
  const legend = node("figcaption", "viz-legend");
  append(
    legend,
    node("span", "candidate", "Candidates"),
    node("span", "canonical", "Canonical"),
    node("small", "", `${formatDateTime(history[0].bucket_start)} → ${formatDateTime(
      history[history.length - 1].bucket_start,
    )}`),
  );
  append(figure, svg, legend);

  const table = node("table", "history-table");
  const caption = node("caption", "visually-hidden", "Source run history by time bucket");
  const head = node("thead");
  const header = node("tr");
  for (const label of ["Bucket", "Runs", "Succeeded", "Failed", "Candidates", "Canonical", "Avg duration"]) {
    header.append(node("th", "", label));
  }
  head.append(header);
  const body = node("tbody");
  for (const bucket of history.slice(-12).reverse()) {
    const row = node("tr");
    append(
      row,
      node("td", "", formatDateTime(bucket.bucket_start)),
      node("td", "", formatCount(bucket.total_runs)),
      node("td", "", formatCount(bucket.succeeded_runs)),
      node("td", "", formatCount(bucket.failed_runs)),
      node("td", "", formatCount(bucket.candidate_count)),
      node("td", "", formatCount(bucket.canonical_count)),
      node("td", "", formatDuration(bucket.average_duration_ms)),
    );
    body.append(row);
  }
  append(table, caption, head, body);
  append(target, figure, detailTableRegion(table, "Source run history table"));
}

function definitionItem(term, value, code = false) {
  const wrapper = node("div", "config-item");
  append(wrapper, node("dt", "", term), node(code ? "code" : "dd", "", value));
  return wrapper;
}

function renderDetailConfig(detail) {
  const target = $("#source-detail-config");
  if (!target) return;
  target.replaceChildren();
  const source = detail.source || {};
  const list = node("dl", "config-grid");
  const origins = Array.isArray(source.approved_origins) && source.approved_origins.length
    ? source.approved_origins.join(", ")
    : "None reported";
  append(
    list,
    definitionItem("Publisher", boundedCopy(source.publisher, "Unavailable")),
    definitionItem("Region", boundedCopy(source.region, "Unavailable")),
    definitionItem("Collection mode", humanize(source.mode)),
    definitionItem("Seed host", boundedCopy(source.seed_host, "Unavailable"), true),
    definitionItem("Approved origins", boundedCopy(origins), true),
    definitionItem("Refresh interval", `${formatCount(source.refresh_interval_minutes)} minutes`),
    definitionItem("Minimum request interval", `${formatCount(source.min_interval_ms)} ms`),
    definitionItem("Page limit", formatCount(source.page_limit)),
    definitionItem("Source revision", `r${formatCount(source.source_revision)}`, true),
    definitionItem("Reviewed", formatDateTime(source.reviewed_at)),
    definitionItem("Review expires", formatDateTime(source.review_expires_at)),
    definitionItem("Handoff only", source.handoff_only ? "Yes" : "No"),
  );
  const seedUrl = safeExternalUrl(source.seed_url);
  if (seedUrl) {
    const wrapper = node("div", "config-item");
    wrapper.append(node("dt", "", "Reviewed seed"));
    const link = node("a", "", boundedCopy(source.seed_url));
    link.href = seedUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    wrapper.append(link);
    list.append(wrapper);
  }
  target.append(list);
}

function identityIsMutable(revision, digest) {
  const normalized = String(revision || "").toLowerCase();
  return (
    !revision ||
    !digest ||
    normalized === "development" ||
    normalized.endsWith("-dirty") ||
    normalized.includes("dirty")
  );
}

function revisionIdentity(run) {
  if (run.provenance_status === "legacy_unavailable") {
    return {
      key: "legacy",
      title: "Legacy · worker claim unavailable",
      mutable: true,
    };
  }
  if (run.provenance_status === "source_revision_only") {
    const source = run.source_revision
      ? `source r${run.source_revision}`
      : "source revision unavailable";
    return {
      key: `source-only|${source}`,
      title: `Source revision recorded · ${source} · worker claim unavailable`,
      mutable: true,
    };
  }
  const release = run.release_revision || "release unavailable";
  const source = run.source_revision ? `source r${run.source_revision}` : "source revision unavailable";
  return {
    key: `${release}|${source}|${run.image_digest || ""}`,
    title: `Worker claim · ${release} · ${source}`,
    mutable: identityIsMutable(run.release_revision, run.image_digest),
  };
}

function average(values) {
  const usable = values.filter((value) => optionalNumber(value) !== null).map(Number);
  return usable.length ? usable.reduce((total, value) => total + value, 0) / usable.length : null;
}

function buildRevisionGroups(runs) {
  const groups = new Map();
  for (const run of runs) {
    const identity = revisionIdentity(run);
    if (!groups.has(identity.key)) {
      groups.set(identity.key, { ...identity, runs: [] });
    }
    groups.get(identity.key).runs.push(run);
  }
  return Array.from(groups.values()).map((group) => {
    const complete = group.runs.filter((run) => ["succeeded", "failed"].includes(run.status));
    const successful = complete.filter((run) => run.status === "succeeded");
    return {
      ...group,
      successRate: complete.length ? successful.length / complete.length : null,
      averageCanonical: average(successful.map((run) => run.canonical_count)),
      averageDuration: average(successful.map((run) => run.duration_ms)),
      latestAt: group.runs[0]?.completed_at || group.runs[0]?.started_at,
    };
  });
}

function deltaCopy(current, previous, formatter) {
  if (current === null || previous === null) return "Not comparable";
  const delta = current - previous;
  const sign = delta > 0 ? "+" : "";
  return `${sign}${formatter(delta)}`;
}

function renderDetailLogic(detail) {
  const target = $("#source-detail-logic") || $("#source-detail-provenance");
  if (!target) return;
  target.replaceChildren();
  target.append(node("h3", "", "Logic revisions & code ownership"));
  const source = detail.source || {};
  const ownership = node("div", "code-map detail-code-map");
  const ownershipHeading = node("strong", "", "Implementation ownership");
  const ownershipNote = node(
    "p",
    "provenance-note",
    "These modules own the configured pipeline path. Ownership plus a recorded revision helps locate a change; it is not proof that a module caused an outcome.",
  );
  append(ownership, ownershipHeading, ownershipNote);
  if (source.mode === "public_jsonld") {
    for (const [stage, modulePath] of [
      ["Collect & extract JSON-LD", "adapters/crawl/source.py"],
      ["Policy, lease & orchestration", "application/catalog_refresh.py"],
      ["Normalize, dedupe & persist", "adapters/postgres/catalog_refresh_commit.py"],
    ]) {
      const item = node("div");
      append(item, node("code", "", modulePath), node("strong", "", stage));
      ownership.append(item);
    }
  } else {
    const item = node("div");
    append(
      item,
      node("code", "", humanize(source.mode)),
      node(
        "strong",
        "",
        "No reviewed static module mapping is published for this crawler mode.",
      ),
    );
    ownership.append(item);
  }
  target.append(ownership);
  const runs = Array.isArray(detail.recent_runs) ? detail.recent_runs : [];
  if (!runs.length) {
    target.append(node("p", "admin-empty-copy", "No runs are available for revision comparison."));
    return;
  }
  const build = detail.current_build || state.currentBuild || {};
  const mutableBuild = identityIsMutable(build.release_revision, build.image_digest);
  const note = node(
    "p",
    `provenance-note ${mutableBuild ? "warning" : "recorded"}`,
    mutableBuild
      ? "Current build identity is mutable or has no immutable image digest. Associations below are useful for debugging, not immutable deployment proof."
      : "Current build has a release revision and immutable image digest. Run attribution is still observational, not proof of causation.",
  );
  target.append(note);

  const groups = buildRevisionGroups(runs);
  const comparison = node("div", "revision-comparison");
  if (groups.length >= 2) {
    const current = groups[0];
    const previous = groups[1];
    append(
      comparison,
      node("strong", "", "Latest revision change"),
      node("span", "", `${previous.title} → ${current.title}`),
      node(
        "span",
        "",
        `Success ${deltaCopy(current.successRate, previous.successRate, (value) =>
          new Intl.NumberFormat(undefined, {
            style: "percent",
            maximumFractionDigits: 1,
          }).format(value),
        )}`,
      ),
      node(
        "span",
        "",
        `Canonical/run ${deltaCopy(current.averageCanonical, previous.averageCanonical, (value) =>
          new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value),
        )}`,
      ),
      node(
        "span",
        "",
        `Duration ${deltaCopy(current.averageDuration, previous.averageDuration, (value) =>
          formatDuration(Math.abs(value)).replace(/^/, value < 0 ? "−" : "+"),
        )}`,
      ),
      node(
        "small",
        "",
        "This comparison is correlated with worker-claim revision identity; it does not establish that the claimed worker fetched data or that a code change caused the outcome.",
      ),
    );
  } else {
    comparison.append(
      node(
        "p",
        "",
        "Only one worker-claim revision group exists in the recent ledger, so a before/after comparison is not yet available.",
      ),
    );
  }
  target.append(comparison);

  const ledger = node("div", "revision-ledger");
  for (const group of groups) {
    const card = node("article", "revision-group");
    const heading = node("div", "revision-heading");
    append(
      heading,
      node("strong", "", group.title),
      statusBadge(group.mutable ? "mutable" : "claim_recorded"),
    );
    append(
      card,
      heading,
      node("small", "", `${formatCount(group.runs.length)} runs · latest ${relativeTime(group.latestAt)}`),
      node("span", "", `Success ${formatPercent(group.successRate)}`),
      node(
        "span",
        "",
        `Canonical/run ${
          group.averageCanonical === null
            ? "Unavailable"
            : new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(
                group.averageCanonical,
              )
        }`,
      ),
      node("span", "", `Average duration ${formatDuration(group.averageDuration)}`),
    );
    ledger.append(card);
  }
  target.append(ledger);

  const runTable = node("table", "history-table provenance-table");
  const caption = node("caption", "visually-hidden", "Recent run revision provenance");
  const head = node("thead");
  const header = node("tr");
  for (const label of [
    "Completed",
    "Run",
    "Status",
    "Output",
    "Source config",
    "Worker release claim",
    "Image",
  ]) {
    header.append(node("th", "", label));
  }
  head.append(header);
  const body = node("tbody");
  for (const run of runs) {
    const row = node("tr");
    const digest = run.image_digest ? `${run.image_digest.slice(0, 18)}…` : "Unavailable";
    append(
      row,
      node("td", "", formatDateTime(run.completed_at || run.started_at)),
      node("td", "", boundedCopy(run.run_key)),
      append(node("td"), statusBadge(run.status)),
      node("td", "", `${formatCount(run.canonical_count)}/${formatCount(run.candidate_count)}`),
      node("td", "", run.source_revision ? `r${run.source_revision}` : "Unavailable"),
      node("td", "", run.release_revision || "Unavailable"),
      node("td", "", digest),
    );
    body.append(row);
  }
  append(runTable, caption, head, body);
  target.append(
    detailTableRegion(runTable, "Recent run revision provenance table"),
  );
}

function renderSourceDetail() {
  const detail = state.sourceDetail;
  if (!detail) return;
  const source = detail.source || {};
  const title = $("#source-detail-title");
  const key = $("#source-detail-key");
  const status = $("#source-detail-status");
  if (title) title.textContent = source.display_name || source.source_key || "Source details";
  if (key) key.textContent = source.source_key || state.selectedSourceKey;
  if (status) status.replaceChildren(statusBadge(source.effective_status));
  const windowSelect = $("#source-detail-window");
  if (windowSelect) windowSelect.value = String(state.sourceDetailWindowHours);
  renderDetailSummary(detail);
  renderDetailPipeline(detail);
  renderHistoryChart(detail);
  renderDetailConfig(detail);
  renderDetailLogic(detail);
  const refresh = $("#source-detail-refresh");
  if (refresh) {
    const pending = pendingSourceKeys().has(source.source_key);
    refresh.disabled = pending || !sourceCanRefresh(source);
    refresh.dataset.sourceKey = source.source_key || "";
    refresh.textContent = pending ? "Refresh queued" : "Queue refresh";
  }
}

async function loadSourceDetail(sourceKey) {
  const safeKey = safeSourceKey(sourceKey);
  if (!safeKey) return false;
  const generation = ++state.sourceDetailGeneration;
  renderDetailLoading(safeKey);
  const request = api(detailEndpoint(safeKey));
  state.sourceDetailInFlight = request;
  try {
    const detail = await request;
    if (generation !== state.sourceDetailGeneration || state.selectedSourceKey !== safeKey) {
      return false;
    }
    state.sourceDetail = detail;
    renderSourceDetail();
    return true;
  } catch (error) {
    if (generation !== state.sourceDetailGeneration) return false;
    setInlineError($("#source-detail-error"), error.message);
    for (const selector of [
      "#source-detail-summary",
      "#source-detail-pipeline",
      "#source-detail-history",
      "#source-detail-config",
      "#source-detail-logic",
      "#source-detail-provenance",
    ]) {
      const target = $(selector);
      if (target) target.replaceChildren();
    }
    return false;
  } finally {
    if (state.sourceDetailInFlight === request) state.sourceDetailInFlight = null;
  }
}

async function openSourceDetail(sourceKey, { push = false } = {}) {
  const safeKey = safeSourceKey(sourceKey);
  if (!safeKey) {
    showToast("That source key cannot be inspected.", "error");
    return;
  }
  state.selectedSourceKey = safeKey;
  state.activeTab = "sources";
  state.sourceDetail = null;
  activateTab("sources", { push });
  renderSources();
  const dialog = $("#source-detail-dialog");
  if (dialog && !dialog.open) {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }
  await loadSourceDetail(safeKey);
}

function closeSourceDetail({ sync = true } = {}) {
  const previousKey = state.selectedSourceKey;
  state.selectedSourceKey = "";
  state.sourceDetail = null;
  state.sourceDetailGeneration += 1;
  if (sync) syncUrl();
  renderSources();
  const trigger = previousKey
    ? $(`.source-details[data-source-key="${CSS.escape(previousKey)}"]`)
    : null;
  if (trigger) trigger.focus({ preventScroll: true });
}

function pendingCommandCount() {
  return state.commands.filter((command) =>
    PENDING_STATUSES.has(String(command.status || "").toLowerCase()),
  ).length;
}

function shouldPoll() {
  return (
    pendingCommandCount() > 0 || finiteNumber(state.overview?.summary?.pending_commands) > 0
  );
}

function stopPolling() {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  setHidden($("#command-polling"), true);
}

function pollingDelay() {
  const exponent = Math.min(state.pollFailures, 4);
  return Math.min(POLL_MAX_MS, POLL_BASE_MS * 2 ** exponent);
}

function schedulePolling() {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  const pending = shouldPoll();
  setHidden($("#command-polling"), !pending);
  if (!pending || document.visibilityState !== "visible") return;
  state.pollTimer = window.setTimeout(() => {
    void pollPendingWork();
  }, pollingDelay());
}

async function pollPendingWork() {
  if (state.pollInFlight) return state.pollInFlight;
  state.pollInFlight = (async () => {
    try {
      await loadCommands();
      const refreshes = [
        loadOverview(),
        loadSources(),
        loadOverviewSources(),
        loadRuns(),
        loadOverviewRuns(),
      ];
      if (state.selectedSourceKey) refreshes.push(loadSourceDetail(state.selectedSourceKey));
      await Promise.allSettled(refreshes);
      state.pollFailures = 0;
    } catch (error) {
      state.pollFailures += 1;
      setConnection("error", "Status delayed");
      announce(`Pending command status could not refresh: ${error.message}`);
    } finally {
      state.pollInFlight = null;
      schedulePolling();
    }
  })();
  return state.pollInFlight;
}

function commandRetryKey(action, sourceKey) {
  return `${action}:${sourceKey || "*"}`;
}

function setButtonBusy(button, busy, busyCopy) {
  if (!button) return;
  if (!button.dataset.idleCopy) button.dataset.idleCopy = button.textContent;
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  button.textContent = busy ? busyCopy : button.dataset.idleCopy;
}

async function submitCommand(action, sourceKey, trigger) {
  if (state.overview?.policy?.allowed !== true) {
    showToast("Source policy currently blocks refresh commands.", "error");
    return;
  }
  const retryKey = commandRetryKey(action, sourceKey);
  const commandId = state.retryCommandIds.get(retryKey) || crypto.randomUUID();
  state.retryCommandIds.set(retryKey, commandId);
  setButtonBusy(trigger, true, "Queueing…");
  const body = { command_id: commandId, action };
  if (sourceKey) body.source_key = sourceKey;
  try {
    const command = await api("/admin/v1/ingestion/commands", {
      method: "POST",
      body,
    });
    state.retryCommandIds.delete(retryKey);
    if (command && typeof command === "object") {
      state.commands = [
        command,
        ...state.commands.filter((item) => item.command_id !== command.command_id),
      ].slice(0, COMMAND_PAGE_SIZE);
    }
    renderCommands();
    renderSources();
    updateCommandControls();
    showToast(
      action === "refresh_due"
        ? "Due-source refresh command accepted."
        : `Refresh command accepted for ${sourceKey}.`,
    );
    const refreshes = [
      loadCommands(),
      loadOverview(),
      loadSources(),
      loadOverviewSources(),
      loadRuns(),
      loadOverviewRuns(),
    ];
    if (state.selectedSourceKey === sourceKey) refreshes.push(loadSourceDetail(sourceKey));
    await Promise.allSettled(refreshes);
    schedulePolling();
  } catch (error) {
    showToast(
      `${error.message} Retrying this control will reuse command ${commandId}.`,
      "error",
    );
  } finally {
    setButtonBusy(trigger, false, "");
    renderSources();
    renderSourceDetail();
    updateCommandControls();
  }
}

function currentBuildLabel(build) {
  if (!build) return "Build identity unavailable";
  const digest = build.image_digest ? `${build.image_digest.slice(0, 18)}…` : "digest unavailable";
  return `${build.release_revision || "revision unavailable"} · ${digest}`;
}

function renderLogic() {
  const current = $("#current-build");
  const ledger = $("#logic-revision-timeline") || $("#revision-ledger");
  const note = $("#provenance-note");
  if (current) {
    current.replaceChildren();
    const mutable = identityIsMutable(
      state.currentBuild?.release_revision,
      state.currentBuild?.image_digest,
    );
    append(
      current,
      node("strong", "", currentBuildLabel(state.currentBuild)),
      statusBadge(mutable ? "mutable" : "immutable"),
    );
  }
  if (note) {
    const mutable = identityIsMutable(
      state.currentBuild?.release_revision,
      state.currentBuild?.image_digest,
    );
    note.textContent = mutable
      ? "Development, dirty, or digest-free builds are mutable. The ledger can correlate behavior with recorded values, but cannot prove which immutable artifact ran."
      : "Runs with claim-recorded release and image identities can be correlated to a deployable artifact claimed by a worker. That claim does not prove the worker performed the fetch. Legacy nulls remain explicitly unavailable.";
  }
  renderGlobalCodeMap();
  if (!ledger) return;
  ledger.replaceChildren();
  const groups = buildRevisionGroups(state.runs);
  if (!groups.length) {
    ledger.append(node("p", "admin-empty-copy", "No revision-aware runs are loaded."));
    renderGlobalImpact(groups);
    return;
  }
  for (const group of groups) {
    const card = node("article", "revision-group");
    const heading = node("div", "revision-heading");
    append(
      heading,
      node("strong", "", group.title),
      statusBadge(group.mutable ? "mutable" : "claim_recorded"),
    );
    append(
      card,
      heading,
      node(
        "small",
        "",
        `${formatCount(group.runs.length)} loaded runs · latest ${relativeTime(group.latestAt)}`,
      ),
      node("span", "", `Success ${formatPercent(group.successRate)}`),
      node(
        "span",
        "",
        `Canonical/run ${
          group.averageCanonical === null
            ? "Unavailable"
            : new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(
                group.averageCanonical,
              )
        }`,
      ),
      node("span", "", `Average duration ${formatDuration(group.averageDuration)}`),
    );
    ledger.append(card);
  }
  renderGlobalImpact(groups);
}

function renderGlobalImpact(groups) {
  const target = $("#logic-impact-summary");
  if (!target) return;
  target.replaceChildren();
  if (groups.length < 2) {
    target.append(
      node(
        "p",
        "admin-empty-copy",
        "Load runs from at least two worker-claim revision groups to compare operational outcomes.",
      ),
    );
    return;
  }
  const current = groups[0];
  const previous = groups[1];
  const list = node("dl");
  for (const [label, value] of [
    [
      "Success rate",
      `${formatPercent(previous.successRate)} → ${formatPercent(current.successRate)}`,
    ],
    [
      "Canonical / run",
      `${
        previous.averageCanonical === null
          ? "Unavailable"
          : new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(
              previous.averageCanonical,
            )
      } → ${
        current.averageCanonical === null
          ? "Unavailable"
          : new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(
              current.averageCanonical,
            )
      }`,
    ],
    [
      "Average duration",
      `${formatDuration(previous.averageDuration)} → ${formatDuration(
        current.averageDuration,
      )}`,
    ],
  ]) {
    const item = node("div");
    append(item, node("dt", "", label), node("dd", "", value));
    list.append(item);
  }
  append(
    target,
    node("strong", "", `${previous.title} → ${current.title}`),
    list,
    node(
      "p",
      "provenance-note",
      "Observed before/after correlation only; deploy and configuration changes may overlap.",
    ),
  );
}

function renderGlobalCodeMap() {
  const target = $("#logic-code-map");
  if (!target) return;
  target.replaceChildren();
  for (const [modulePath, owner, responsibility] of [
    ["adapters/crawl/source.py", "Public JSON-LD collector", "HTTPS fetch, ACL, parse"],
    ["application/catalog_refresh.py", "Refresh orchestration", "Policy, pacing, lease"],
    [
      "adapters/postgres/catalog_refresh_commit.py",
      "Catalog commit",
      "Normalize, dedupe, persist",
    ],
  ]) {
    const item = node("div");
    append(
      item,
      node("code", "", modulePath),
      node("strong", "", owner),
      node("span", "", responsibility),
    );
    target.append(item);
  }
}

async function loadDashboard({ announceResult = false } = {}) {
  if (state.dashboardInFlight) return state.dashboardInFlight;
  const refreshButton = $("#refresh-dashboard");
  setButtonBusy(refreshButton, true, "Refreshing…");
  setConnection("", "Refreshing");
  state.dashboardInFlight = (async () => {
    const results = await Promise.allSettled([
      loadOverview(),
      loadSources(),
      loadOverviewSources(),
      loadCommands(),
      loadRuns(),
      loadOverviewRuns(),
    ]);
    const sectionTargets = [
      null,
      $("#sources-error"),
      null,
      $("#commands-error"),
      $("#runs-error"),
      null,
    ];
    let failures = 0;
    let firstError = null;
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        failures += 1;
        if (!firstError) firstError = result.reason;
        const target = sectionTargets[index];
        if (target) setInlineError(target, result.reason.message);
      }
    });
    if (failures === 0) {
      setConnection("ready", "Control plane connected");
      if (announceResult) showToast("Ingestion status refreshed.");
    } else {
      setConnection("error", failures === results.length ? "Control plane unavailable" : "Partial data");
      if (firstError?.status === 401) {
        const access = $("#operator-access");
        if (access) access.open = true;
        setInlineError($("#operator-token-error"), firstError.message);
      }
      if (announceResult) showToast(firstError?.message || "Some ingestion data could not load.", "error");
    }
    revealShell();
    return failures === 0;
  })();
  try {
    return await state.dashboardInFlight;
  } finally {
    state.dashboardInFlight = null;
    setButtonBusy(refreshButton, false, "");
  }
}

function applySourceFilters(event) {
  event.preventDefault();
  state.sourceQuery = $("#source-query")?.value.trim() || "";
  state.sourceState = $("#source-state")?.value || "all";
  state.sourceMode = $("#source-mode")?.value || "";
  state.sourcePublisher = $("#source-publisher")?.value || "";
  state.sourceRegion = $("#source-region")?.value || "";
  state.sourceHealth = $("#source-health")?.value || "";
  state.includeFixtures = $("#include-fixtures").checked;
  state.sourceOffset = 0;
  state.runOffset = 0;
  syncUrl();
  void Promise.allSettled([loadSources(), loadRuns(), loadFilterMetadata()]).then(() => {
    announce("Source filters applied.");
  });
}

function applyRunFilters(event) {
  event.preventDefault();
  state.runStatus = $("#run-status")?.value || "";
  state.runSourceKey = safeSourceKey($("#run-source")?.value.trim());
  state.runWindow = $("#run-window")?.value || "";
  state.runOffset = 0;
  syncUrl();
  void loadRuns()
    .then(() => announce("Run filters applied."))
    .catch((error) => setInlineError($("#runs-error"), error.message));
}

function applyCommandFilters(event) {
  event.preventDefault();
  state.commandStatus = $("#command-status")?.value || "";
  state.commandAction = $("#command-action")?.value || "";
  state.commandSourceKey = safeCommandQuery($("#command-source")?.value.trim());
  syncUrl();
  renderCommands();
  announce("Command filters applied.");
}

function bindOptionalSubmit(selector, handler) {
  const form = $(selector);
  if (form) form.addEventListener("submit", handler);
}

function bindEvents() {
  $("#refresh-dashboard")?.addEventListener("click", () => {
    void Promise.allSettled([
      loadDashboard({ announceResult: true }),
      loadFilterMetadata(),
      loadCurrentBuild(),
    ]);
  });
  bindOptionalSubmit("#source-filters", applySourceFilters);
  bindOptionalSubmit("#run-filters", applyRunFilters);
  bindOptionalSubmit(".command-filters", applyCommandFilters);
  $(".command-filters")?.addEventListener("change", applyCommandFilters);
  $("#command-source")?.addEventListener("input", () => {
    window.clearTimeout($("#command-source")._filterTimer);
    $("#command-source")._filterTimer = window.setTimeout(() => {
      state.commandSourceKey = safeCommandQuery($("#command-source")?.value.trim());
      syncUrl();
      renderCommands();
    }, 180);
  });
  $("#source-filters")?.addEventListener("reset", () => {
    window.setTimeout(() => applySourceFilters(new Event("submit")), 0);
  });
  $("#overview-source-query")?.addEventListener("input", (event) => {
    state.overviewSourceQuery = event.currentTarget.value;
    renderSourceLauncher();
  });
  $("#overview-source-search")?.addEventListener("submit", (event) => {
    event.preventDefault();
    state.overviewSourceQuery = $("#overview-source-query")?.value.trim() || "";
    const match = overviewSourceCandidates()[0];
    if (state.overviewSourceQuery && match) {
      void openSourceDetail(match.source_key, { push: true });
      return;
    }
    openSourceWorkspace({ query: state.overviewSourceQuery });
  });
  $("#overview-browse-sources")?.addEventListener("click", () => openSourceWorkspace());
  $("#overview-panel")?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const routeControl = target?.closest("[data-overview-route]");
    if (routeControl) {
      event.preventDefault();
      handleOverviewRoute(routeControl.dataset.overviewRoute || "");
      return;
    }
    const stageControl = target?.closest("[data-overview-stage]");
    if (!stageControl) return;
    const stage = stageControl.dataset.overviewStage || "";
    if (!["registry", "admission", "collect", "normalize", "publish"].includes(stage)) return;
    state.overviewStage = stage;
    renderPipelineChart();
    announce(`${stageControl.textContent.trim()} pipeline stage selected.`);
  });

  for (const link of $$("[data-admin-tab]")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const tab = link.dataset.adminTab || (link.getAttribute("href") || "").replace(/^#/, "");
      if (VALID_TABS.has(tab)) activateTab(tab, { push: true, focus: true });
    });
  }
  for (const button of $$("#overview-panel .panel-tools .segmented-button")) {
    button.addEventListener("click", () => {
      const windowValue = {
        "24 hours": "24h",
        "7 days": "7d",
        "30 days": "30d",
      }[button.textContent.trim().toLowerCase()];
      if (!windowValue) return;
      state.runWindow = windowValue;
      state.runOffset = 0;
      applyStateToInputs();
      syncUrl();
      void loadOverviewRuns()
        .then(() => announce(`Overview window changed to ${button.textContent.trim()}.`))
        .catch((error) => announce(`Overview window could not load: ${error.message}`));
    });
  }

  $("#sources-previous")?.addEventListener("click", () => {
    state.sourceOffset = Math.max(0, state.sourceOffset - SOURCE_PAGE_SIZE);
    void loadSources()
      .then(() => $("#sources-title")?.focus({ preventScroll: true }))
      .catch((error) => setInlineError($("#sources-error"), error.message));
  });
  $("#sources-next")?.addEventListener("click", () => {
    state.sourceOffset += SOURCE_PAGE_SIZE;
    void loadSources()
      .then(() => $("#sources-title")?.focus({ preventScroll: true }))
      .catch((error) => setInlineError($("#sources-error"), error.message));
  });

  const dueDialog = $("#refresh-due-dialog");
  $("#open-refresh-due")?.addEventListener("click", () => {
    const count = $("#refresh-due-count");
    if (count) {
      count.textContent = `${formatCount(state.overview?.summary?.due_sources)} ${
        finiteNumber(state.overview?.summary?.due_sources) === 1 ? "source is" : "sources are"
      } currently due.`;
    }
    dueDialog?.showModal();
  });
  if (dueDialog) {
    dueDialog.addEventListener("close", () => {
      if (dueDialog.returnValue === "confirm") {
        void submitCommand("refresh_due", null, $("#open-refresh-due"));
      } else {
        $("#open-refresh-due")?.focus({ preventScroll: true });
      }
      dueDialog.returnValue = "";
    });
  }

  const detailDialog = $("#source-detail-dialog");
  detailDialog?.addEventListener("close", () => closeSourceDetail());
  for (const link of $$(".source-detail-nav a")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const body = $(".source-detail-body");
      const target = $(link.getAttribute("href"));
      if (!body || !target) return;
      const bodyBox = body.getBoundingClientRect();
      const targetBox = target.getBoundingClientRect();
      body.scrollTo({
        top: Math.max(0, body.scrollTop + targetBox.top - bodyBox.top - 4),
      });
    });
  }
  $("#source-detail-close")?.addEventListener("click", () => {
    if (detailDialog?.open) detailDialog.close();
    else closeSourceDetail();
  });
  $("#source-detail-window")?.addEventListener("change", (event) => {
    const hours = Number(event.currentTarget.value);
    if (!state.filters?.window_hours?.includes(hours) && ![24, 168, 720].includes(hours)) return;
    state.sourceDetailWindowHours = hours;
    syncUrl();
    if (state.selectedSourceKey) void loadSourceDetail(state.selectedSourceKey);
  });
  $("#source-detail-refresh")?.addEventListener("click", (event) => {
    const sourceKey = safeSourceKey(event.currentTarget.dataset.sourceKey);
    if (sourceKey) void submitCommand("refresh_source", sourceKey, event.currentTarget);
  });

  $("#operator-token-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#operator-token");
    const token = input?.value.trim() || "";
    if (!token) {
      setInlineError($("#operator-token-error"), "Enter a bearer token or use local mode without one.");
      input?.focus();
      return;
    }
    state.operatorToken = token;
    input.value = "";
    setInlineError($("#operator-token-error"));
    const accessState = $("#operator-access-state");
    if (accessState) accessState.textContent = "Bearer token active in page memory";
    const access = $("#operator-access");
    if (access) access.open = false;
    void loadDashboard({ announceResult: true });
  });
  $("#clear-operator-token")?.addEventListener("click", () => {
    state.operatorToken = "";
    const input = $("#operator-token");
    if (input) input.value = "";
    const accessState = $("#operator-access-state");
    if (accessState) accessState.textContent = "Local mode · no token entered";
    setInlineError($("#operator-token-error"));
    announce("Operator token cleared from page memory.");
    void loadDashboard();
  });

  $("#admin-toast-close")?.addEventListener("click", () => {
    setHidden($("#admin-toast"), true);
    window.clearTimeout(state.toastTimer);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") {
      stopPolling();
      return;
    }
    if (shouldPoll()) void pollPendingWork();
  });
  window.addEventListener("popstate", () => {
    const previousSource = state.selectedSourceKey;
    readUrlState();
    applyStateToInputs();
    activateTab(state.activeTab);
    if (state.selectedSourceKey && state.selectedSourceKey !== previousSource) {
      void openSourceDetail(state.selectedSourceKey);
    } else if (!state.selectedSourceKey) {
      const dialog = $("#source-detail-dialog");
      if (dialog?.open) dialog.close();
    }
  });
  window.addEventListener("pagehide", () => {
    state.operatorToken = "";
    stopPolling();
  });
}

async function boot() {
  readUrlState();
  bindEvents();
  applyStateToInputs();
  activateTab(state.activeTab);
  renderSources();
  renderRuns();
  renderCommands();
  const auxiliary = Promise.allSettled([loadFilterMetadata(), loadCurrentBuild()]);
  await loadDashboard();
  await auxiliary;
  if (state.selectedSourceKey) {
    await openSourceDetail(state.selectedSourceKey);
  }
}

void boot();
