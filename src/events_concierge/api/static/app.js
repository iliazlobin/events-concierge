const SESSION_KEY = "events-concierge.local-session.v1";
const ERASURE_REQUEST_KEY = "events-concierge.erasure-request.v1";
const ERASURE_CONFIRMATION = "DELETE MY ACCOUNT";
const MAX_VISIBLE_PICKS = 25;
const COLLECTION_PAGE_SIZE = 50;
const COLLECTION_PAGE_CAP = 4;
const COLLECTION_ITEM_CAP = COLLECTION_PAGE_SIZE * COLLECTION_PAGE_CAP;
const CATALOG_PAGE_SIZE = 50;
const CATALOG_PROVIDER_PAGE_CAP = 20;
const CATALOG_PROVIDER_ITEM_CAP = CATALOG_PAGE_SIZE * CATALOG_PROVIDER_PAGE_CAP;
const AUTO_REFRESH_BASE_MS = 30_000;
const AUTO_REFRESH_MAX_MS = 5 * 60_000;
const AUTO_REFRESH_WINDOW_MS = 6 * 60 * 60_000;
const DISCOVERY_DEFAULTS = Object.freeze({
  when: "any",
  price: "any",
  city: "any",
  source: "any",
  radius: "any",
  sort: "soonest",
});

const state = {
  config: null,
  tenantId: null,
  me: null,
  requests: [],
  registrations: [],
  registrationsCapped: false,
  tasks: [],
  tasksCapped: false,
  currentQuery: "",
  pendingPicks: [],
  nextCursor: null,
  currentTaskId: null,
  verifyingTasks: new Set(),
  toastTimer: null,
  autoRefreshTimer: null,
  autoRefreshFailures: 0,
  refreshInFlight: null,
  requestLoadGeneration: 0,
  registrationLoadGeneration: 0,
  taskLoadGeneration: 0,
  erasureRequestId: null,
  currentFeedItems: [],
  currentUnderstood: null,
  currentRawResultCount: 0,
  currentMatchCount: 0,
  availableProviders: new Map(),
  catalogProviders: new Map(),
  catalogEndpointAvailable: null,
  catalogLoadedSourceKey: null,
  catalogScopeRefreshPending: false,
  catalogResultCapped: false,
  currentResultKind: null,
  availableCities: new Map(),
  hasFeedResponse: false,
  discoveryInFlight: false,
  discoveryGeneration: 0,
  discoveryController: null,
  dismissedEventIds: new Set(),
  likedEventIds: new Set(),
  sessionGeneration: 0,
  workspaceMode: "chat",
  catalogSearch: "",
  catalogFilterSuggestions: [],
  catalogFilterSuggestionIndex: -1,
  catalogFilterSuggestionSignature: "",
  catalogFilterMenuDismissed: false,
  catalogFilterEditKind: null,
  catalogFilterEditDraft: "",
  eventMap: null,
  eventMapMarkers: null,
  mapMarkerByEvent: new Map(),
  mapFilterCenter: null,
  mapLastSignature: "",
  mapSuppressMoveNotice: false,
  selectedMapEventId: null,
  calendarMonth: null,
  calendarSelectedDateKey: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function node(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined && content !== null) element.textContent = String(content);
  return element;
}

function append(parent, ...children) {
  for (const child of children) {
    if (child) parent.append(child);
  }
  return parent;
}

function humanize(value) {
  if (!value) return "Unknown";
  return String(value)
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function apiErrorMessage(payload, status) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (Array.isArray(payload?.detail) && payload.detail[0]?.msg) return payload.detail[0].msg;
  if (status === 401) return "Your session is no longer available. Please sign in again.";
  if (status === 503) return "The service is temporarily unavailable. Your existing plans are safe.";
  return `The service returned an unexpected response (${status}).`;
}

function cookieValue(name) {
  if (!name) return null;
  const prefix = `${name}=`;
  const matches = document.cookie
    .split(";")
    .map((item) => item.trim())
    .filter((item) => item.startsWith(prefix));
  if (matches.length !== 1) return null;
  const value = matches[0].slice(prefix.length);
  return value || null;
}

async function api(path, { method = "GET", body, signal, keepalive = false } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.config?.local_demo && state.tenantId) {
    headers["X-EC-Tenant-ID"] = state.tenantId;
  } else if (!["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())) {
    const csrf = cookieValue(state.config?.csrf_cookie_name);
    if (csrf && state.config?.csrf_header_name) {
      headers[state.config.csrf_header_name] = csrf;
    }
  }
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
    signal,
    keepalive,
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

function readLocalSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return typeof parsed?.tenantId === "string" ? parsed.tenantId : null;
  } catch (_error) {
    return null;
  }
}

function writeLocalSession(tenantId) {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ tenantId }));
    return true;
  } catch (_error) {
    return false;
  }
}

function invalidateSessionLoads() {
  state.sessionGeneration += 1;
  state.requestLoadGeneration += 1;
  state.registrationLoadGeneration += 1;
  state.taskLoadGeneration += 1;
  state.refreshInFlight = null;
}

function clearPrivateClientState() {
  state.requests = [];
  state.registrations = [];
  state.registrationsCapped = false;
  state.tasks = [];
  state.tasksCapped = false;
  state.currentTaskId = null;
  state.verifyingTasks.clear();
  resetRecommendationSession();
  renderRecentRequests();
  renderPlans();
  renderTasks();
  syncIdentity();
  $("#onboarding-email").value = "";
  $$("#onboarding-interests [data-interest]").forEach((button) => {
    button.setAttribute("aria-pressed", "false");
  });
  $("#completion-copy").textContent = "I’ll verify the registration before changing your calendar.";
  if ($("#completion-dialog").open) $("#completion-dialog").close();
  resetErasureDialog();
  if ($("#account-erasure-dialog").open) $("#account-erasure-dialog").close();
  setError($("#preferences-error"));
  setButtonBusy($("#save-preferences"), false, "");
  window.clearTimeout(state.toastTimer);
  state.toastTimer = null;
  $("#toast-message").textContent = "";
  $("#toast").hidden = true;
  clearErasureRequestId();
}

function clearLocalSession() {
  stopAutoRefresh();
  invalidateSessionLoads();
  try {
    localStorage.removeItem(SESSION_KEY);
  } catch (_error) {
    // A privacy-restricted browser may refuse storage access; the in-memory session still clears.
  }
  state.tenantId = null;
  state.me = null;
  clearPrivateClientState();
}

function erasureRequestId() {
  if (state.erasureRequestId) return state.erasureRequestId;
  try {
    const stored = localStorage.getItem(ERASURE_REQUEST_KEY);
    if (stored && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(stored)) {
      state.erasureRequestId = stored;
      return stored;
    }
  } catch (_error) {
    // The in-memory request identity remains stable for retries in this document.
  }
  state.erasureRequestId = crypto.randomUUID();
  try {
    localStorage.setItem(ERASURE_REQUEST_KEY, state.erasureRequestId);
  } catch (_error) {
    // Privacy-restricted storage does not weaken the server's replay/conflict checks.
  }
  return state.erasureRequestId;
}

function clearErasureRequestId() {
  state.erasureRequestId = null;
  try {
    localStorage.removeItem(ERASURE_REQUEST_KEY);
  } catch (_error) {
    // The server-side tombstone remains authoritative after browser cleanup.
  }
}

function setScreen(screen) {
  $("#boot-screen").hidden = screen !== "boot";
  $("#welcome-view").hidden = screen !== "welcome";
  $("#erasure-view").hidden = screen !== "erasure";
  $("#product-shell").hidden = screen !== "product";
}

function setError(target, message = "") {
  target.textContent = message;
  target.hidden = !message;
}

function setButtonBusy(button, busy, busyLabel) {
  if (!button.dataset.idleLabel) {
    button.dataset.idleLabel = button.textContent.replace(/\s+/g, " ").trim();
  }
  button.disabled = busy;
  const label = [...button.children].find(
    (child) => child.tagName === "SPAN" && child.getAttribute("aria-hidden") !== "true",
  );
  if (label) {
    label.textContent = busy ? busyLabel : button.dataset.idleLabel.replace(/→$/, "").trim();
  } else {
    button.textContent = busy ? busyLabel : button.dataset.idleLabel;
  }
  button.setAttribute("aria-busy", String(busy));
}

function showToast(message, kind = "success") {
  const toast = $("#toast");
  $("#toast-message").textContent = message;
  $("#toast-icon").textContent = kind === "error" ? "!" : "✓";
  toast.dataset.kind = kind;
  toast.setAttribute("role", kind === "error" ? "alert" : "status");
  toast.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
  toast.hidden = false;
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => {
    toast.hidden = true;
  }, 6000);
}

function showWelcome() {
  stopAutoRefresh();
  const local = Boolean(state.config?.local_demo);
  $("#local-onboarding-fields").hidden = !local;
  $("#production-login").hidden = local;
  $("#demo-disclosure").hidden = !local;
  $("#onboarding-submit").disabled = !local && !state.config?.auth_start_url;
  $("#onboarding-submit span:first-child").textContent = local
    ? "Meet my concierge"
    : "Sign in securely";
  $("#welcome-description").textContent = local
    ? "Tell me what sounds good. I’ll find a few focused options, explain why they fit, and—when you ask—start a durable registration request."
    : "Sign in to see the plans, handoffs, and preferences tied to your private concierge account.";
  setError(
    $("#welcome-error"),
    !local && !state.config?.auth_start_url
      ? "Secure sign-in has not been configured for this deployment."
      : "",
  );
  setScreen("welcome");
}

function showBootFailure(error) {
  const boot = $("#boot-screen");
  boot.replaceChildren();
  append(
    boot,
    node("img", "boot-mark"),
    node("p", "", "The concierge could not start."),
  );
  const image = $("img", boot);
  image.src = "/assets/mark.svg";
  image.alt = "";
  const detail = node("p", "", error.message || "Please check the service and try again.");
  const retry = node("button", "button button-primary", "Try again");
  retry.type = "button";
  retry.addEventListener("click", () => window.location.reload());
  append(boot, detail, retry);
  setScreen("boot");
}

function syncIdentity() {
  const email = state.me?.notify_email || "Your account";
  const shortEmail = email.length > 23 ? `${email.slice(0, 20)}…` : email;
  $("#profile-initial").textContent = email.slice(0, 1).toUpperCase() || "E";
  $("#profile-short-email").textContent = shortEmail;
  $("#settings-email").textContent = email;
  $("#settings-session").textContent = state.config.local_demo
    ? "Local demo browser"
    : "Secure deployment session";
  $("#settings-environment").textContent = state.config.local_demo
    ? "Local mock cloud"
    : "Production-shaped deployment";
  $("#sign-out").hidden = !state.config.local_demo && !state.config.logout_url;
  $("#sign-out-note").textContent = state.config.local_demo
    ? "This only removes the local browser reference; it does not erase server data."
    : "Signing out revokes this browser session; it does not erase your account data.";
  $("#erasure-policy-note").textContent = state.config.local_demo
    ? "Local demo accounts require the exact typed confirmation. The durable worker continues after this browser reference is removed."
    : "Production accounts require a fresh provider sign-in and the exact typed confirmation.";
  const interests = new Set(state.me?.interests || []);
  $$("#settings-interests [data-interest]").forEach((button) => {
    button.setAttribute("aria-pressed", String(interests.has(button.dataset.interest)));
  });

  $("#today-label").textContent = "Recommendations";
  $("#greeting").textContent = "What sounds good?";
}

async function enterProduct(me) {
  invalidateSessionLoads();
  const sessionGeneration = state.sessionGeneration;
  state.me = me;
  syncIdentity();
  setScreen("product");
  const requestedView = window.location.hash.replace(/^#\/?/, "");
  navigate(
    ["concierge", "catalog", "map", "calendar", "plans", "tasks", "settings"].includes(requestedView)
      ? requestedView
      : "concierge",
    false,
  );
  void checkReadiness();
  await refreshAll();
  if (sessionGeneration !== state.sessionGeneration) return;
  startAutoRefresh();
}

async function boot() {
  setScreen("boot");
  try {
    state.config = await api("/v1/ui-config");
    if (state.config.local_demo) {
      state.tenantId = readLocalSession();
      if (!state.tenantId) {
        showWelcome();
        return;
      }
    }
    try {
      await enterProduct(await api("/v1/me"));
    } catch (error) {
      if (error.status === 401 || error.status === 404) {
        if (state.config.local_demo) clearLocalSession();
        showWelcome();
        return;
      }
      throw error;
    }
  } catch (error) {
    showBootFailure(error);
  }
}

async function checkReadiness() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 5000);
  const service = $("#service-status");
  const serviceCopy = $("span:last-child", service);
  const mobile = $("#mobile-status");
  try {
    const status = await api("/readyz", { signal: controller.signal });
    const databaseReady = status.components?.database === "ready";
    const temporalReady = status.components?.temporal === "ready";
    service.classList.toggle("ready", databaseReady && temporalReady);
    service.classList.toggle("degraded", !databaseReady || !temporalReady);
    mobile.classList.toggle("ready", databaseReady && temporalReady);
    mobile.classList.toggle("degraded", !databaseReady || !temporalReady);
    if (!databaseReady) {
      serviceCopy.textContent = "Service unavailable";
      mobile.title = "Service unavailable";
      mobile.textContent = "Unavailable";
    } else if (!temporalReady) {
      serviceCopy.textContent = "Requests safely queued";
      mobile.title = "Registration engine degraded; requests are safely queued";
      mobile.textContent = "Queued";
    } else {
      serviceCopy.textContent = "All systems ready";
      mobile.title = "All systems ready";
      mobile.textContent = "Ready";
    }
    mobile.setAttribute("aria-label", mobile.title);
  } catch (_error) {
    service.classList.add("degraded");
    serviceCopy.textContent = "Status unavailable";
    mobile.classList.add("degraded");
    mobile.title = "Service status unavailable";
    mobile.textContent = "Status unknown";
    mobile.setAttribute("aria-label", mobile.title);
  } finally {
    window.clearTimeout(timeout);
  }
}

function setWorkspaceMode(mode) {
  const normalized = ["catalog", "map", "calendar"].includes(mode) ? mode : "chat";
  state.workspaceMode = normalized;
  const page = $("#concierge-page");
  page.dataset.workspaceMode = normalized;
  $$(".workspace-tab").forEach((button) => {
    const active = button.dataset.workspaceView === normalized;
    button.classList.toggle("active", active);
    if (active) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  });
  const expectedResultKind = normalized === "chat" ? "feed" : "catalog";
  if (state.hasFeedResponse && state.currentResultKind === expectedResultKind) {
    applyDiscoveryToCurrentFeed();
  } else if (["catalog", "map", "calendar"].includes(normalized)) {
    if (state.me && !state.discoveryInFlight) {
      queueMicrotask(() => {
        if (
          ["catalog", "map", "calendar"].includes(state.workspaceMode)
          && state.currentResultKind !== "catalog"
        ) {
          void browseCurrentEvents();
        }
      });
    }
  }
  if (normalized === "map" && state.eventMap) {
    window.requestAnimationFrame(() => state.eventMap.invalidateSize());
  }
}

function navigate(view, focus = true) {
  const workspaceMode = ["catalog", "map", "calendar"].includes(view)
    ? view
    : view === "concierge"
      ? "chat"
      : null;
  const pageView = workspaceMode ? "concierge" : view;
  $$("[data-page]").forEach((page) => {
    page.hidden = page.dataset.page !== pageView;
  });
  $$("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (button.classList.contains("nav-item") || button.classList.contains("mobile-nav-item")) {
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
  });
  if (workspaceMode) setWorkspaceMode(workspaceMode);
  history.replaceState(null, "", `#/${view}`);
  if (focus) {
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    $("#main-content").focus({ preventScroll: true });
  }
}

function stopAutoRefresh() {
  window.clearTimeout(state.autoRefreshTimer);
  state.autoRefreshTimer = null;
}

function hasPendingRequestOutcome() {
  const cutoff = Date.now() - AUTO_REFRESH_WINDOW_MS;
  return state.requests.some((item) => {
    if (item.outcome || !["received", "started"].includes(item.state)) return false;
    const createdAt = validDate(item.created_at);
    return createdAt !== null && createdAt.valueOf() >= cutoff;
  });
}

function requestOutcomeFingerprint() {
  return state.requests
    .map((item) => [
      item.request_id,
      item.state,
      item.outcome?.canonical_event_id || "",
      item.outcome?.state || "",
      item.outcome?.updated_at || "",
    ].join(":"))
    .join("|");
}

function autoRefreshDelay() {
  const backoff = Math.min(
    AUTO_REFRESH_MAX_MS,
    AUTO_REFRESH_BASE_MS * (2 ** Math.min(state.autoRefreshFailures, 4)),
  );
  return Math.round(backoff * (0.8 + (Math.random() * 0.4)));
}

function scheduleAutoRefresh() {
  stopAutoRefresh();
  if (
    !state.me
    || document.visibilityState !== "visible"
    || !hasPendingRequestOutcome()
  ) return;
  state.autoRefreshTimer = window.setTimeout(async () => {
    state.autoRefreshTimer = null;
    await refreshPendingRequestOutcome();
    scheduleAutoRefresh();
  }, autoRefreshDelay());
}

function startAutoRefresh() {
  state.autoRefreshFailures = 0;
  scheduleAutoRefresh();
}

async function refreshPendingRequestOutcome() {
  if (!state.me || document.visibilityState !== "visible" || !hasPendingRequestOutcome()) return;
  const before = requestOutcomeFingerprint();
  try {
    const applied = await loadRequests();
    if (!applied) return;
    state.autoRefreshFailures = 0;
    if (requestOutcomeFingerprint() !== before) {
      await Promise.allSettled([loadRegistrations(), loadTasks()]);
    }
  } catch (error) {
    if (error.status === 401) {
      if (state.config?.local_demo) clearLocalSession();
      else state.me = null;
      showWelcome();
      return;
    }
    state.autoRefreshFailures += 1;
  }
}

async function refreshAll({ showErrors = true } = {}) {
  if (state.refreshInFlight) return state.refreshInFlight;
  const sessionGeneration = state.sessionGeneration;
  const refresh = (async () => {
    const [requests, registrations, tasks] = await Promise.allSettled([
      loadRequests(),
      loadRegistrations(),
      loadTasks(),
    ]);
    if (sessionGeneration !== state.sessionGeneration) return;
    const results = [requests, registrations, tasks];
    const sessionExpired = results.some(
      (result) => result.status === "rejected" && result.reason?.status === 401,
    );
    if (sessionExpired) {
      if (state.config?.local_demo) clearLocalSession();
      else {
        stopAutoRefresh();
        state.me = null;
      }
      showWelcome();
      return;
    }
    if (showErrors) {
      if (requests.status === "rejected") renderRecentRequests(requests.reason);
      if (registrations.status === "rejected") renderPlans(registrations.reason);
      if (tasks.status === "rejected") renderTasks(tasks.reason);
    }
  })();
  state.refreshInFlight = refresh;
  try {
    await refresh;
  } finally {
    if (state.refreshInFlight === refresh) state.refreshInFlight = null;
  }
}

async function loadRequests() {
  const sessionGeneration = state.sessionGeneration;
  const generation = ++state.requestLoadGeneration;
  let payload;
  try {
    payload = await api("/v1/requests?limit=8");
  } catch (error) {
    if (
      sessionGeneration !== state.sessionGeneration
      || generation !== state.requestLoadGeneration
    ) return false;
    throw error;
  }
  if (
    sessionGeneration !== state.sessionGeneration
    || generation !== state.requestLoadGeneration
  ) return false;
  state.requests = payload.items || [];
  renderRecentRequests();
  return true;
}

async function loadCursorCollection(path) {
  const items = [];
  const seenCursors = new Set();
  let cursor = null;
  for (let page = 0; page < COLLECTION_PAGE_CAP; page += 1) {
    const query = new URLSearchParams({ limit: String(COLLECTION_PAGE_SIZE) });
    if (cursor) query.set("cursor", cursor);
    const separator = path.includes("?") ? "&" : "?";
    const payload = await api(`${path}${separator}${query}`);
    const pageItems = Array.isArray(payload.items) ? payload.items : [];
    items.push(...pageItems.slice(0, COLLECTION_ITEM_CAP - items.length));
    const nextCursor = payload.next_cursor || null;
    if (!nextCursor) return { items, capped: false };
    if (seenCursors.has(nextCursor)) {
      throw new Error("The service returned an invalid page sequence. Refresh and try again.");
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }
  return { items, capped: Boolean(cursor) };
}

async function loadRegistrations() {
  const sessionGeneration = state.sessionGeneration;
  const generation = ++state.registrationLoadGeneration;
  let result;
  try {
    result = await loadCursorCollection("/v1/registrations");
  } catch (error) {
    if (
      sessionGeneration !== state.sessionGeneration
      || generation !== state.registrationLoadGeneration
    ) return false;
    throw error;
  }
  if (
    sessionGeneration !== state.sessionGeneration
    || generation !== state.registrationLoadGeneration
  ) return false;
  state.registrations = result.items;
  state.registrationsCapped = result.capped;
  renderPlans();
  return true;
}

async function loadTasks() {
  const sessionGeneration = state.sessionGeneration;
  const generation = ++state.taskLoadGeneration;
  let result;
  try {
    result = await loadCursorCollection("/v1/tasks?state=actionable");
  } catch (error) {
    if (
      sessionGeneration !== state.sessionGeneration
      || generation !== state.taskLoadGeneration
    ) return false;
    throw error;
  }
  if (
    sessionGeneration !== state.sessionGeneration
    || generation !== state.taskLoadGeneration
  ) return false;
  state.tasks = result.items;
  state.tasksCapped = result.capped;
  renderTasks();
  return true;
}

function renderEmpty(container, symbol, title, copy) {
  const empty = node("div", "empty-state");
  const emblem = node("span", "empty-symbol", symbol);
  emblem.setAttribute("aria-hidden", "true");
  append(empty, emblem, node("h3", "", title), node("p", "", copy));
  container.replaceChildren(empty);
}

function formatRelative(value) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "Recently";
  const seconds = Math.round((date.valueOf() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  return formatter.format(Math.round(hours / 24), "day");
}

function expiryLabel(value) {
  const expiry = validDate(value);
  if (!expiry) return "Expiry unavailable";
  return `${expiry.valueOf() <= Date.now() ? "Expired" : "Expires"} ${formatRelative(value)}`;
}

function requestStateLabel(value) {
  return {
    received: "Saved",
    started: "Started",
    failed_no_candidate: "No match",
  }[value] || humanize(value);
}

function renderRecentRequests(error = null) {
  const container = $("#recent-requests");
  if (error) {
    renderEmpty(container, "!", "Activity is temporarily unavailable", error.message);
    return;
  }
  if (!state.requests.length) {
    renderEmpty(container, "✦", "No saved briefs yet", "Durable requests will appear here.");
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of state.requests) {
    const button = node("button", "recent-request");
    button.type = "button";
    button.title = `Reuse: ${item.text}`;
    const title = node("strong", "", item.text);
    const time = node("small", "", formatRelative(item.created_at));
    const displayState = item.outcome?.state || item.state;
    const status = node(
      "span",
      "request-state",
      item.outcome ? lifecycleLabel(displayState) : requestStateLabel(displayState),
    );
    if (displayState === "failed_no_candidate") status.classList.add("no-match");
    const outcome = item.outcome
      ? node(
          "span",
          "request-outcome",
          `${item.outcome.title} · ${eventDateTime(item.outcome.start_at)}`,
        )
      : null;
    if (outcome) outcome.title = outcome.textContent;
    append(button, title, status, outcome, time);
    button.addEventListener("click", () => {
      navigate("concierge");
      $("#ask-input").value = item.text;
      $("#ask-input").focus();
      showToast("Brief restored. Choose preview or durable handling when you’re ready.");
    });
    fragment.append(button);
  }
  container.replaceChildren(fragment);
}

function validDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? null : date;
}

function eventDateTime(value) {
  const date = validDate(value);
  if (!date) return "Time to be confirmed";
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
}

function dateTile(value, className = "pick-date", item = null) {
  const date = validDate(value);
  const calendarUrl = item ? googleCalendarUrl(item) : null;
  const tile = node(calendarUrl ? "a" : "div", className);
  const month = date
    ? new Intl.DateTimeFormat(undefined, { month: "short" }).format(date)
    : "TBD";
  const day = date ? new Intl.DateTimeFormat(undefined, { day: "numeric" }).format(date) : "—";
  append(tile, node("span", "", month), node("strong", "", day));
  if (calendarUrl) {
    tile.href = calendarUrl;
    tile.target = "_blank";
    tile.rel = "noopener noreferrer";
    tile.classList.add("calendar-add-link");
    tile.title = "Add to Google Calendar";
    tile.setAttribute(
      "aria-label",
      `Add ${item.title} to Google Calendar (opens in new tab)`,
    );
  } else {
    tile.setAttribute("aria-hidden", "true");
  }
  return tile;
}

function sourceLabel(source) {
  return {
    public_jsonld: "Event site",
    meetup: "Meetup",
    ticketmaster: "Ticketmaster",
    luma: "Luma",
    eventbrite: "Eventbrite",
    partiful: "Partiful",
    serpapi: "Public listing",
  }[source] || (source ? humanize(source) : "Curated source");
}

function safeUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(value, window.location.origin);
    if (url.username || url.password) return null;
    const isSameOriginHttp = url.protocol === "http:" && url.origin === window.location.origin;
    return url.protocol === "https:" || isSameOriginHttp ? url.href : null;
  } catch (_error) {
    return null;
  }
}

function externalLink(value, label, className = "button button-secondary") {
  const href = safeUrl(value);
  if (!href) return null;
  const link = node("a", className, label);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.setAttribute("aria-label", `${label.replace(/\s*↗\s*$/, "")} (opens in new tab)`);
  return link;
}

function googleCalendarTimestamp(value) {
  const date = validDate(value);
  if (!date) return null;
  return date
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d{3}Z$/, "Z");
}

function googleCalendarUrl(item) {
  const start = googleCalendarTimestamp(item.start_at);
  if (!start) return null;
  const parsedStart = validDate(item.start_at);
  const fallbackEnd = parsedStart
    ? new Date(parsedStart.valueOf() + 60 * 60 * 1000)
    : null;
  const end = googleCalendarTimestamp(item.end_at)
    || googleCalendarTimestamp(fallbackEnd);
  if (!end) return null;
  const sourceUrl = eventRegistrationUrl(item);
  const details = [item.description, sourceUrl ? `Event page: ${sourceUrl}` : null]
    .filter(Boolean)
    .join("\n\n");
  const location = [item.venue_name, item.city].filter(Boolean).join(", ");
  const query = new URLSearchParams({
    action: "TEMPLATE",
    text: item.title || "Event",
    dates: `${start}/${end}`,
  });
  if (details) query.set("details", details);
  if (location) query.set("location", location);
  return `https://calendar.google.com/calendar/render?${query}`;
}

function calendarTimingLink(item, label, className) {
  const link = externalLink(googleCalendarUrl(item), label, className);
  if (!link) return node("span", className, label);
  link.title = "Add to Google Calendar";
  link.setAttribute(
    "aria-label",
    `Add ${item.title} to Google Calendar (opens in new tab)`,
  );
  return link;
}

function priceLabel(value) {
  return { free: "Free", paid: "Paid", unknown: "Price unknown" }[value] || "Price unknown";
}

function discoveryFilters() {
  return {
    when: $("#filter-when").value,
    price: $("#filter-price").value,
    city: $("#filter-city").value,
    source: $("#filter-source").value,
    radius: $("#filter-radius").value,
    sort: $("#filter-sort").value,
  };
}

function normalizeFilterSuggestionText(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase()
    .trim()
    .replace(/\s+/g, " ");
}

function filterSuggestionMatches(query, values) {
  if (!query) return true;
  return values.some((value) => {
    const normalized = normalizeFilterSuggestionText(value);
    if (!normalized) return false;
    return normalized.includes(query)
      || (normalized.length >= 3 && query.includes(normalized));
  });
}

function localDateKey(date) {
  const year = String(date.getFullYear()).padStart(4, "0");
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function parseLocalDateKey(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(year, month - 1, day);
  if (
    date.getFullYear() !== year
    || date.getMonth() !== month - 1
    || date.getDate() !== day
  ) return null;
  return date;
}

function dateRangeFilterValue(startKey, endKey) {
  return `range:${startKey}:${endKey}`;
}

function parseDateRangeFilterValue(value) {
  const match = /^range:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$/.exec(
    String(value || ""),
  );
  if (!match) return null;
  const start = parseLocalDateKey(match[1]);
  const end = parseLocalDateKey(match[2]);
  if (!start || !end || start.valueOf() > end.valueOf()) return null;
  return {
    start,
    end,
    startKey: match[1],
    endKey: match[2],
  };
}

function removeDateRangeFilterOption() {
  $("#filter-when option[data-date-range]")?.remove();
}

function dateRangeLabel(start, end) {
  const sameDay = sameLocalDate(start, end);
  const sameYear = start.getFullYear() === end.getFullYear();
  const sameMonth = sameYear && start.getMonth() === end.getMonth();
  const monthDay = (value) => new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
  }).format(value);
  const fullDate = (value) => new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(value);
  const startCopy = monthDay(start);
  if (sameDay) return startCopy;
  if (sameMonth) return `${startCopy} – ${end.getDate()}, ${end.getFullYear()}`;
  if (sameYear) return `${startCopy} – ${monthDay(end)}, ${end.getFullYear()}`;
  return `${fullDate(start)} – ${fullDate(end)}`;
}

function ensureDateRangeFilterOption(startKey, endKey) {
  const parsed = parseDateRangeFilterValue(dateRangeFilterValue(startKey, endKey));
  if (!parsed) return null;
  removeDateRangeFilterOption();
  const option = node(
    "option",
    "",
    dateRangeLabel(parsed.start, parsed.end),
  );
  option.value = dateRangeFilterValue(parsed.startKey, parsed.endKey);
  option.dataset.dateRange = `${parsed.startKey}:${parsed.endKey}`;
  $("#filter-when").append(option);
  return option.value;
}

function addLocalDays(date, days) {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function datePresetRange(preset) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  if (preset === "today") return [today, today];
  if (preset === "tomorrow") {
    const tomorrow = addLocalDays(today, 1);
    return [tomorrow, tomorrow];
  }
  if (preset === "7d") return [today, addLocalDays(today, 6)];
  if (preset === "weekend") {
    const day = today.getDay();
    const saturday = addLocalDays(today, day === 0 ? 0 : (6 - day + 7) % 7);
    return [saturday, addLocalDays(saturday, day === 0 ? 0 : 1)];
  }
  return [today, today];
}

function currentProviderSuggestionEntries() {
  const entries = new Map();
  const loadedCounts = new Map();
  for (const item of state.currentFeedItems) {
    const seen = new Set();
    for (const identity of itemProviders(item)) {
      if (identity.key === "unknown" || seen.has(identity.key)) continue;
      seen.add(identity.key);
      loadedCounts.set(identity.key, (loadedCounts.get(identity.key) || 0) + 1);
    }
  }
  for (const [key, metadata] of state.catalogProviders) {
    entries.set(key, {
      key,
      label: metadata.label,
      sourceKey: metadata.sourceKey,
      count: Math.max(metadata.eventCount || 0, loadedCounts.get(key) || 0),
    });
  }
  for (const [key, label] of state.availableProviders) {
    if (entries.has(key)) continue;
    entries.set(key, {
      key,
      label,
      sourceKey: null,
      count: loadedCounts.get(key) || 0,
    });
  }
  return [...entries.values()];
}

function currentCitySuggestionEntries() {
  const counts = new Map();
  for (const item of state.currentFeedItems) {
    const value = String(item.city || "").trim();
    if (value) counts.set(value, (counts.get(value) || 0) + 1);
  }
  return [...state.availableCities.entries()].map(([value, label]) => ({
    value,
    label,
    count: counts.get(value) || 0,
  }));
}

const FILTER_KIND_LABELS = {
  when: "Date",
  city: "City",
  price: "Price",
  source: "Provider",
  radius: "Distance",
  sort: "Sort",
};

function filterSuggestionKindAllowed(kind) {
  return !state.catalogFilterEditKind || state.catalogFilterEditKind === kind;
}

function filterSuggestionDescription(kind, value, fallback) {
  if (discoveryFilters()[kind] === value) return "Currently selected";
  if (state.catalogFilterEditKind === kind) return "Replace current filter";
  return fallback;
}

function catalogFilterSuggestionPriority(suggestion, query, index) {
  if (!query) return index;
  const label = normalizeFilterSuggestionText(suggestion.label);
  const qualifiedLabel = normalizeFilterSuggestionText(
    `${suggestion.category} ${suggestion.label}`,
  );
  if (query === label || query === qualifiedLabel) return 0;
  if (label.startsWith(query)) return 100 + index;
  if (label.includes(query)) return 200 + index;
  if (query.includes(label)) return 300 + index;
  return 400 + index;
}

function buildCatalogFilterSuggestions(rawQuery) {
  const query = normalizeFilterSuggestionText(rawQuery);
  const suggestions = [];
  const dateDefinitions = [
    {
      id: "date-today",
      value: "today",
      label: "Today",
      keywords: ["today", "tonight"],
      featured: true,
    },
    {
      id: "date-tomorrow",
      value: "tomorrow",
      label: "Tomorrow",
      keywords: ["tomorrow", "tom"],
      featured: true,
    },
    {
      id: "date-weekend",
      value: "weekend",
      label: "This weekend",
      keywords: ["this weekend", "weekend", "saturday", "sunday"],
      featured: true,
    },
    {
      id: "date-seven-days",
      value: "7d",
      label: "Next 7 days",
      keywords: ["next 7 days", "next week", "seven days"],
    },
    {
      id: "date-thirty-days",
      value: "30d",
      label: "Next 30 days",
      keywords: ["next 30 days", "this month", "thirty days"],
    },
  ];
  const dateCategoryQuery = filterSuggestionMatches(
    query,
    ["date", "day", "calendar", "when"],
  );
  const matchedDates = filterSuggestionKindAllowed("when")
    ? dateDefinitions.filter((definition) => {
      if (state.catalogFilterEditKind === "when") {
        return !query || filterSuggestionMatches(
          query,
          [definition.label, ...definition.keywords],
        );
      }
      if (!query) return definition.featured;
      if (dateCategoryQuery) return true;
      return filterSuggestionMatches(query, [definition.label, ...definition.keywords]);
    })
    : [];
  for (const definition of matchedDates) {
    suggestions.push({
      id: definition.id,
      kind: "when",
      value: definition.value,
      category: "Date",
      label: definition.label,
      description: filterSuggestionDescription(
        "when",
        definition.value,
        "Use the event’s local start date",
      ),
      queryTerms: [definition.label, definition.value, ...definition.keywords],
      ariaLabel: `Date · ${definition.label}`,
    });
  }
  if (
    filterSuggestionKindAllowed("when")
    && (
      !query
      || matchedDates.length
      || filterSuggestionMatches(query, ["date", "day", "calendar", "date range"])
    )
  ) {
    suggestions.push({
      id: "date-range",
      kind: "date-range",
      value: null,
      category: "Date",
      label: state.catalogFilterEditKind === "when"
        ? "Choose another date range…"
        : "Choose date range…",
      description: "Pick a start and end date",
      ariaLabel: "Choose date range…",
    });
  }

  if (filterSuggestionKindAllowed("price")) {
    const priceDefinitions = [
      {
        id: "price-free",
        value: "free",
        label: "Free",
        keywords: ["free", "no cost", "zero cost"],
        featured: true,
        description: "Only events marked free",
      },
      {
        id: "price-paid",
        value: "paid",
        label: "Paid",
        keywords: ["paid", "ticketed"],
        description: "Only events with a paid price",
      },
      {
        id: "price-listed",
        value: "listed",
        label: "Price listed",
        keywords: ["price listed", "known price", "priced"],
        description: "Only events with published pricing",
      },
    ];
    const priceCategoryQuery = filterSuggestionMatches(
      query,
      ["price", "cost", "ticket price"],
    );
    for (const definition of priceDefinitions.filter((item) => {
      if (state.catalogFilterEditKind === "price") {
        return !query || filterSuggestionMatches(query, [item.label, ...item.keywords]);
      }
      return !query ? item.featured : filterSuggestionMatches(
        query,
        [item.label, ...item.keywords],
      ) || priceCategoryQuery;
    })) {
      suggestions.push({
        id: definition.id,
        kind: "price",
        value: definition.value,
        category: "Price",
        label: definition.label,
        description: filterSuggestionDescription(
          "price",
          definition.value,
          definition.description,
        ),
        queryTerms: [definition.label, definition.value, ...definition.keywords],
        ariaLabel: `Price · ${definition.label}`,
      });
    }
  }

  if (filterSuggestionKindAllowed("source")) {
    const providerPriority = new Map(
      ["luma", "meetup", "eventbrite", "partiful", "ticketmaster"].map(
        (key, index) => [key, index],
      ),
    );
    const providerCategoryQuery = filterSuggestionMatches(
      query,
      ["provider", "source", "platform"],
    );
    const providerSuggestions = currentProviderSuggestionEntries()
      .filter((provider) =>
        providerCategoryQuery
        || filterSuggestionMatches(query, [
          provider.key,
          provider.label,
          provider.sourceKey,
          `${provider.label} events`,
          `provider ${provider.label}`,
        ])
      )
      .sort((left, right) => {
        const leftPriority = providerPriority.get(left.key) ?? Number.POSITIVE_INFINITY;
        const rightPriority = providerPriority.get(right.key) ?? Number.POSITIVE_INFINITY;
        return leftPriority - rightPriority
          || right.count - left.count
          || left.label.localeCompare(right.label);
      });
    for (const provider of providerSuggestions) {
      if (query || state.catalogFilterEditKind === "source" || suggestions.length < 7) {
        const countCopy = provider.count
          ? `${provider.count} current event${provider.count === 1 ? "" : "s"}`
          : "Loaded events only";
        suggestions.push({
          id: `provider-${provider.key}`,
          kind: "source",
          value: provider.key,
          category: "Provider",
          label: provider.label,
          description: filterSuggestionDescription("source", provider.key, countCopy),
          queryTerms: [
            provider.label,
            provider.key,
            provider.sourceKey,
            `${provider.label} events`,
          ],
          ariaLabel: `Provider · ${provider.label}${provider.count ? ` · ${provider.count}` : ""}`,
        });
      }
    }
  }

  if (filterSuggestionKindAllowed("city") && (query || state.catalogFilterEditKind === "city")) {
    const cityCategoryQuery = filterSuggestionMatches(
      query,
      ["city", "place", "location"],
    );
    for (const city of currentCitySuggestionEntries()
      .filter((entry) =>
        cityCategoryQuery
        || filterSuggestionMatches(query, [entry.value, entry.label])
      )
      .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label))) {
      suggestions.push({
        id: `city-${city.value}`,
        kind: "city",
        value: city.value,
        category: "City",
        label: city.label,
        description: filterSuggestionDescription(
          "city",
          city.value,
          `${city.count} loaded event${city.count === 1 ? "" : "s"}`,
        ),
        queryTerms: [city.label, city.value],
        ariaLabel: `City · ${city.label} · ${city.count}`,
      });
    }
  }

  const radiusCategoryQuery = filterSuggestionMatches(
    query,
    ["distance", "near me", "nearby", "within", "miles", "radius"],
  );
  const requestedRadius = /\b(5|10|25|50)\s*(?:mi|mile|miles)?\b/.exec(query)?.[1];
  if (
    filterSuggestionKindAllowed("radius")
    && (
      state.catalogFilterEditKind === "radius"
      || (
        state.workspaceMode === "map"
        && radiusCategoryQuery
      )
    )
  ) {
    for (const definition of [
      { value: "5", label: "Within 5 mi", keywords: ["5 miles", "five miles"] },
      { value: "10", label: "Within 10 mi", keywords: ["10 miles", "ten miles"] },
      { value: "25", label: "Within 25 mi", keywords: ["25 miles", "twenty five miles"] },
      { value: "50", label: "Within 50 mi", keywords: ["50 miles", "fifty miles"] },
    ].filter((item) =>
      !query
      || (requestedRadius ? item.value === requestedRadius : radiusCategoryQuery)
      || filterSuggestionMatches(query, [item.label, ...item.keywords])
    )) {
      suggestions.push({
        id: `radius-${definition.value}`,
        kind: "radius",
        value: definition.value,
        category: "Distance",
        label: definition.label,
        description: filterSuggestionDescription(
          "radius",
          definition.value,
          "Measure from the current map center",
        ),
        queryTerms: [definition.label, `${definition.value} miles`, ...definition.keywords],
        ariaLabel: `Distance · ${definition.label}`,
      });
    }
  }

  const sortCategoryQuery = filterSuggestionMatches(query, ["sort", "order", "order by"]);
  const hasRankedEvents = state.currentFeedItems.some(
    (item) => item.score !== null
      && item.score !== undefined
      && String(item.score).trim() !== ""
      && Number.isFinite(Number(item.score)),
  );
  if (
    filterSuggestionKindAllowed("sort")
    && (
      state.catalogFilterEditKind === "sort"
      || sortCategoryQuery
      || filterSuggestionMatches(query, [
        "sort",
        "soonest",
        "recommended",
        "top ranked",
        "free first",
      ])
    )
  ) {
    for (const definition of [
      { value: "soonest", label: "Soonest", keywords: ["soonest", "chronological"] },
      { value: "relevance", label: "Recommended", keywords: ["recommended", "relevance"] },
      ...(hasRankedEvents
        ? [{ value: "score", label: "Top ranked", keywords: ["top ranked", "best score"] }]
        : []),
      { value: "free", label: "Free first", keywords: ["free first"] },
    ].filter((item) =>
      !query
      || sortCategoryQuery
      || filterSuggestionMatches(query, [item.label, ...item.keywords])
    )) {
      suggestions.push({
        id: `sort-${definition.value}`,
        kind: "sort",
        value: definition.value,
        category: "Sort",
        label: definition.label,
        description: filterSuggestionDescription(
          "sort",
          definition.value,
          "Change result order",
        ),
        queryTerms: [definition.label, definition.value, ...definition.keywords],
        ariaLabel: `Sort · ${definition.label}`,
      });
    }
  }
  const ranked = suggestions
    .map((suggestion, index) => ({
      suggestion,
      priority: catalogFilterSuggestionPriority(suggestion, query, index),
    }))
    .sort((left, right) => left.priority - right.priority)
    .slice(0, query && !state.catalogFilterEditKind ? 7 : 8)
    .map(({ suggestion }) => suggestion);
  if (query && !state.catalogFilterEditKind) {
    ranked.push({
      id: "text-search",
      kind: "text-search",
      value: String(rawQuery || "").trim(),
      category: "Search",
      label: `Search for “${String(rawQuery || "").trim()}”`,
      description: "Match event titles, descriptions, places, and providers",
      ariaLabel: `Search events for ${String(rawQuery || "").trim()}`,
    });
  }
  return ranked;
}

function setCatalogFilterSuggestionIndex(index) {
  const input = $("#catalog-search");
  const options = $$("#catalog-filter-suggestions [role='option']");
  if (!options.length || index < 0) {
    state.catalogFilterSuggestionIndex = -1;
    input.removeAttribute("aria-activedescendant");
    for (const option of options) option.setAttribute("aria-selected", "false");
    return;
  }
  const normalized = (index + options.length) % options.length;
  state.catalogFilterSuggestionIndex = normalized;
  options.forEach((option, optionIndex) => {
    option.setAttribute("aria-selected", String(optionIndex === normalized));
  });
  const active = options[normalized];
  input.setAttribute("aria-activedescendant", active.id);
  active.scrollIntoView({ block: "nearest" });
}

function closeCatalogFilterMenu({ returnFocus = false, dismiss = false } = {}) {
  const input = $("#catalog-search");
  if (dismiss && state.catalogFilterEditKind) finishCatalogFilterEdit();
  if (returnFocus) input.focus({ preventScroll: true });
  if (dismiss) state.catalogFilterMenuDismissed = true;
  $("#catalog-filter-menu").hidden = true;
  $("#catalog-date-range-row").hidden = true;
  $("#catalog-filter-suggestions").hidden = false;
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
  state.catalogFilterSuggestionIndex = -1;
}

function announceCatalogFilter(message) {
  $("#catalog-filter-status").textContent = "";
  window.requestAnimationFrame(() => {
    $("#catalog-filter-status").textContent = message;
  });
}

function catalogFilterSearchPlaceholder() {
  const category = FILTER_KIND_LABELS[state.catalogFilterEditKind];
  return category
    ? `Change ${category.toLocaleLowerCase()} filter`
    : "Search events or add a filter";
}

function finishCatalogFilterEdit() {
  const editing = Boolean(state.catalogFilterEditKind);
  state.catalogFilterEditKind = null;
  const input = $("#catalog-search");
  if (editing) {
    // Restore the text the edit displaced. Reopening a filter beside a typed query is
    // not abandoning that query, so it comes back committed instead of being dropped —
    // otherwise the box looked empty and the list silently widened after every edit.
    const restored = state.catalogFilterEditDraft;
    state.catalogFilterEditDraft = "";
    input.value = restored;
    const committed = restored.trim();
    if (committed !== state.catalogSearch) {
      state.catalogSearch = committed;
      if (state.workspaceMode === "calendar") {
        state.calendarMonth = null;
        state.calendarSelectedDateKey = null;
      }
      // Callers that apply or remove a filter refresh again straight after; this keeps
      // the dismissed-edit path (Escape) from leaving a committed search unapplied.
      refreshDiscoveryView();
    }
  } else {
    input.value = state.catalogSearch;
  }
  input.placeholder = catalogFilterSearchPlaceholder();
}

function residualCatalogSearch(rawQuery, suggestion) {
  let residual = String(rawQuery || "").trim();
  const terms = [...new Set((suggestion?.queryTerms || []).filter(Boolean))]
    .sort((left, right) => String(right).length - String(left).length);
  let matched = false;
  for (const term of terms) {
    const escaped = String(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const next = residual.replace(new RegExp(`(^|\\s)${escaped}(?=\\s|$)`, "i"), " ");
    if (next !== residual) {
      residual = next;
      matched = true;
      break;
    }
  }
  if (!matched) return "";
  return residual
    .replace(/\b(?:from|by|via|provider|source)\s*$/i, "")
    .replace(/\s+/g, " ")
    .trim();
}

function consumeCatalogFilterQuery(suggestion = null) {
  const editing = Boolean(state.catalogFilterEditKind);
  const rawQuery = $("#catalog-search").value;
  finishCatalogFilterEdit();
  if (!editing) {
    state.catalogSearch = suggestion
      ? residualCatalogSearch(rawQuery, suggestion)
      : "";
    $("#catalog-search").value = state.catalogSearch;
  }
}

function renderCatalogFilterSuggestions({ force = false } = {}) {
  const input = $("#catalog-search");
  const shell = $(".catalog-search-shell");
  // Suggestions are only honest once the first catalog response has taught us which
  // providers and cities exist. Offering the free-text fallback alone while those
  // facets are still in flight showed a menu that silently changed contents underneath
  // the user the moment the response landed. populateFacetFilters() re-renders this
  // menu when the facets arrive, so the suppression lifts on its own.
  const facetsPending = state.discoveryInFlight && state.catalogEndpointAvailable === null;
  if (
    !["chat", "catalog", "map", "calendar"].includes(state.workspaceMode)
    || facetsPending
    || (!force && (
      state.catalogFilterMenuDismissed
      || !shell.contains(document.activeElement)
    ))
  ) {
    closeCatalogFilterMenu();
    return;
  }
  const suggestions = buildCatalogFilterSuggestions(input.value);
  $(".catalog-filter-menu-heading span").textContent = state.catalogFilterEditKind
    ? `Change ${FILTER_KIND_LABELS[state.catalogFilterEditKind]}`
    : "Suggested filters";
  state.catalogFilterSuggestions = suggestions;
  const list = $("#catalog-filter-suggestions");
  const signature = JSON.stringify(
    suggestions.map(({ id, value, ariaLabel, description }) => ({
      id,
      value,
      ariaLabel,
      description,
    })),
  );
  if (signature !== state.catalogFilterSuggestionSignature) {
    const options = suggestions.map((suggestion, index) => {
      const option = node("button", "catalog-filter-suggestion");
      option.type = "button";
      option.id = `catalog-filter-suggestion-${index}`;
      option.dataset.suggestionIndex = String(index);
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      option.setAttribute("aria-label", suggestion.ariaLabel);
      append(
        option,
        node("span", "catalog-filter-suggestion-kind", suggestion.category),
        node("strong", "", suggestion.label),
        node("small", "", suggestion.description),
      );
      option.addEventListener("pointerdown", (event) => event.preventDefault());
      option.addEventListener("click", () => applyCatalogFilterSuggestion(index));
      return option;
    });
    list.replaceChildren(...options);
    state.catalogFilterSuggestionSignature = signature;
  }
  list.hidden = false;
  $("#catalog-date-range-row").hidden = true;
  if (!suggestions.length) {
    closeCatalogFilterMenu();
    return;
  }
  $("#catalog-filter-menu").hidden = false;
  input.setAttribute("aria-expanded", "true");
  setCatalogFilterSuggestionIndex(-1);
}

function openCatalogDateRange() {
  const startInput = $("#catalog-date-start");
  const endInput = $("#catalog-date-end");
  const selected = parseDateRangeFilterValue($("#filter-when").value);
  const [fallbackStart, fallbackEnd] = datePresetRange(
    ["today", "tomorrow", "weekend", "7d"].includes($("#filter-when").value)
      ? $("#filter-when").value
      : "today",
  );
  const minimum = localDateKey(new Date());
  startInput.min = minimum;
  endInput.min = minimum;
  startInput.value = selected?.startKey || localDateKey(fallbackStart);
  endInput.value = selected?.endKey || localDateKey(fallbackEnd);
  $("#catalog-filter-suggestions").hidden = true;
  $("#catalog-date-range-row").hidden = false;
  $("#catalog-filter-menu").hidden = false;
  $("#catalog-search").setAttribute("aria-expanded", "false");
  $("#catalog-search").removeAttribute("aria-activedescendant");
  state.catalogFilterSuggestionIndex = -1;
  startInput.focus({ preventScroll: true });
  try {
    startInput.showPicker?.();
  } catch (_error) {
    // Some browsers permit the native picker only for a direct trusted activation.
  }
}

function applyDateRangeFilter(startKey, endKey) {
  const value = ensureDateRangeFilterOption(startKey, endKey);
  if (!value) return;
  consumeCatalogFilterQuery();
  setEventFilter("when", value, { focus: false });
  closeCatalogFilterMenu({ returnFocus: true, dismiss: true });
  announceCatalogFilter(`Date range applied: ${selectedOptionLabel("#filter-when")}.`);
}

function applyCatalogFilterSuggestion(index) {
  const suggestion = state.catalogFilterSuggestions[index];
  if (!suggestion) return;
  if (suggestion.kind === "date-range") {
    openCatalogDateRange();
    return;
  }
  if (suggestion.kind === "text-search") {
    state.catalogFilterEditKind = null;
    state.catalogSearch = suggestion.value;
    if (state.workspaceMode === "calendar") {
      state.calendarMonth = null;
      state.calendarSelectedDateKey = null;
    }
    $("#catalog-search").value = state.catalogSearch;
    $("#catalog-search").placeholder = catalogFilterSearchPlaceholder();
    refreshDiscoveryView();
    closeCatalogFilterMenu({ returnFocus: true, dismiss: true });
    announceCatalogFilter(`Search applied: ${state.catalogSearch}.`);
    return;
  }
  consumeCatalogFilterQuery(suggestion);
  setEventFilter(suggestion.kind, suggestion.value, { focus: false });
  closeCatalogFilterMenu({ returnFocus: true, dismiss: true });
  announceCatalogFilter(`${suggestion.category} filter applied: ${suggestion.label}.`);
}

function handleCatalogFilterSuggestionKeydown(event) {
  if (event.isComposing) return;
  const menuOpen = !$("#catalog-filter-menu").hidden;
  const optionCount = state.catalogFilterSuggestions.length;
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    if (!menuOpen) renderCatalogFilterSuggestions();
    if (!optionCount && !state.catalogFilterSuggestions.length) return;
    const movingForward = event.key === "ArrowDown";
    if (event.key === "Home") {
      setCatalogFilterSuggestionIndex(0);
    } else if (event.key === "End") {
      setCatalogFilterSuggestionIndex(state.catalogFilterSuggestions.length - 1);
    } else {
      const direction = movingForward ? 1 : -1;
      const current = state.catalogFilterSuggestionIndex;
      setCatalogFilterSuggestionIndex(
        current < 0
          ? (direction > 0 ? 0 : state.catalogFilterSuggestions.length - 1)
          : current + direction,
      );
    }
    return;
  }
  if (
    event.key === "Enter"
    && menuOpen
    && state.catalogFilterSuggestions.length
  ) {
    event.preventDefault();
    applyCatalogFilterSuggestion(
      state.catalogFilterSuggestionIndex >= 0
        ? state.catalogFilterSuggestionIndex
        : 0,
    );
    return;
  }
  if (event.key === "Escape" && menuOpen) {
    event.preventDefault();
    closeCatalogFilterMenu({ returnFocus: true, dismiss: true });
  } else if (event.key === "Tab") {
    closeCatalogFilterMenu({ dismiss: true });
  }
}

function selectedOptionLabel(selector) {
  const select = $(selector);
  return select.selectedOptions[0]?.textContent?.trim() || "";
}

function activeFilterValueLabel(name) {
  const copy = selectedOptionLabel(`#filter-${name}`);
  if (name === "sort") return copy.replace(/^Sort\s*·\s*/i, "");
  return copy;
}

function beginCatalogFilterEdit(name) {
  if (!FILTER_KIND_LABELS[name]) return;
  state.catalogFilterEditKind = name;
  state.catalogFilterMenuDismissed = false;
  const input = $("#catalog-search");
  state.catalogFilterEditDraft = input.value;
  input.value = "";
  input.placeholder = catalogFilterSearchPlaceholder();
  input.focus({ preventScroll: true });
  // Editing a date filter lists the date presets first and keeps focus in the search
  // box. Jumping straight into the start/end row skipped every preset and moved focus
  // out of the combobox the user just reopened; "Choose another date range…" is still
  // in that list for a custom interval.
  renderCatalogFilterSuggestions({ force: true });
  announceCatalogFilter(`Choose a new ${FILTER_KIND_LABELS[name].toLocaleLowerCase()} filter.`);
}

function removeCatalogFilter(name) {
  if (!FILTER_KIND_LABELS[name]) return;
  const label = activeFilterValueLabel(name);
  finishCatalogFilterEdit();
  setEventFilter(name, DISCOVERY_DEFAULTS[name], { focus: false });
  closeCatalogFilterMenu({ returnFocus: true, dismiss: true });
  announceCatalogFilter(`${FILTER_KIND_LABELS[name]} filter removed: ${label}.`);
}

function renderCatalogActiveFilters(filters = discoveryFilters()) {
  const target = $("#catalog-active-filters");
  if (!target) return;
  const order = ["when", "source", "city", "price", "radius", "sort"];
  const entries = order.filter(
    (name) => filters[name] !== DISCOVERY_DEFAULTS[name],
  );
  const tokens = entries.map((name) => {
    const category = FILTER_KIND_LABELS[name];
    const valueLabel = activeFilterValueLabel(name);
    const token = node("span", "catalog-filter-token");
    token.dataset.activeFilter = name;
    token.setAttribute("role", "group");
    token.setAttribute("aria-label", `${category} filter: ${valueLabel}`);

    const edit = node("button", "catalog-filter-token-edit");
    edit.type = "button";
    edit.setAttribute(
      "aria-label",
      `Edit ${category.toLocaleLowerCase()} filter: ${valueLabel}`,
    );
    edit.append(
      node("span", "catalog-filter-token-label", `${category} · ${valueLabel}`),
    );
    edit.addEventListener("click", () => beginCatalogFilterEdit(name));

    const remove = node("button", "catalog-filter-token-remove", "×");
    remove.type = "button";
    remove.setAttribute(
      "aria-label",
      `Remove ${category.toLocaleLowerCase()} filter: ${valueLabel}`,
    );
    remove.addEventListener("click", () => removeCatalogFilter(name));
    append(token, edit, remove);
    return token;
  });
  if (entries.length > 1) {
    const clear = node("button", "catalog-filter-token catalog-filter-clear", "Clear all");
    clear.type = "button";
    clear.setAttribute("aria-label", "Clear all event filters");
    clear.addEventListener("click", resetDiscoveryFilters);
    tokens.push(clear);
  }
  target.replaceChildren(...tokens);
}

function requestTextWithHints(baseText, filters) {
  const hints = [];
  if (state.catalogSearch) hints.push(`matching “${state.catalogSearch}”`);
  if (filters.when !== DISCOVERY_DEFAULTS.when) {
    hints.push(`date: ${activeFilterValueLabel("when")}`);
  }
  if (filters.city !== DISCOVERY_DEFAULTS.city) {
    hints.push(`city: ${activeFilterValueLabel("city").replace(/\s+·\s+\d+$/, "")}`);
  }
  if (filters.price !== DISCOVERY_DEFAULTS.price) {
    hints.push(`price: ${activeFilterValueLabel("price")}`);
  }
  // The provider scope is deliberately omitted. It selects which catalog page the
  // browser is paging through, not what the person is asking for; /v1/feed has no
  // source parameter, so passing it as a hint constrains the brief with something the
  // recommender cannot honor.
  if (filters.radius !== DISCOVERY_DEFAULTS.radius) {
    const center = state.mapFilterCenter;
    const centerCopy = center
      ? ` from map center ${center[0].toFixed(4)}, ${center[1].toFixed(4)}`
      : "";
    hints.push(`distance: ${activeFilterValueLabel("radius")}${centerCopy}`);
  }
  if (filters.sort !== DISCOVERY_DEFAULTS.sort) {
    hints.push(`order: ${activeFilterValueLabel("sort")}`);
  }
  if (!hints.length) return baseText;
  return `${baseText}\n\nDiscovery context: ${hints.join("; ")}.`;
}

function normalizedHostname(value) {
  const href = safeUrl(value);
  if (!href) return null;
  try {
    return new URL(href).hostname.toLowerCase().replace(/^www\./, "");
  } catch (_error) {
    return null;
  }
}

function providerIdentity(source, registrationUrl) {
  const rawSource = source || "unknown";
  const hostname = normalizedHostname(registrationUrl);
  const registrationHref = safeUrl(registrationUrl);
  if (
    rawSource === "meetup"
    || hostname === "meetup.com"
    || hostname?.endsWith(".meetup.com")
  ) {
    return {
      key: "meetup",
      label: "Meetup",
      rawSource,
      hostname,
      registrationHref,
    };
  }
  if (rawSource === "luma" || hostname === "luma.com" || hostname?.endsWith(".luma.com")) {
    return { key: "luma", label: "Luma", rawSource, hostname, registrationHref };
  }
  if (
    rawSource === "eventbrite"
    || hostname === "eventbrite.com"
    || hostname?.endsWith(".eventbrite.com")
  ) {
    return {
      key: "eventbrite",
      label: "Eventbrite",
      rawSource,
      hostname,
      registrationHref,
    };
  }
  if (
    rawSource === "ticketmaster"
    || hostname === "ticketmaster.com"
    || hostname?.endsWith(".ticketmaster.com")
  ) {
    return {
      key: "ticketmaster",
      label: "Ticketmaster",
      rawSource,
      hostname,
      registrationHref,
    };
  }
  if (
    rawSource === "partiful"
    || hostname === "partiful.com"
    || hostname?.endsWith(".partiful.com")
  ) {
    return {
      key: "partiful",
      label: "Partiful",
      rawSource,
      hostname,
      registrationHref,
    };
  }
  if (["public_jsonld", "unknown"].includes(rawSource) && hostname) {
    return {
      key: `host:${hostname}`,
      label: hostname,
      rawSource,
      hostname,
      registrationHref,
    };
  }
  return {
    key: rawSource,
    label: sourceLabel(rawSource),
    rawSource,
    hostname,
    registrationHref,
  };
}

function itemProviders(item) {
  const identities = [];
  const seen = new Set();
  const sources = Array.isArray(item.sources) ? item.sources : [];
  for (const source of sources) {
    const identity = providerIdentity(source.source, source.registration_url);
    const key = `${identity.key}|${identity.rawSource}|${identity.hostname || ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    identities.push(identity);
  }
  if (!identities.length) {
    for (const registrationUrl of item.registration_urls || []) {
      const identity = providerIdentity(null, registrationUrl);
      const key = `${identity.key}|${identity.rawSource}|${identity.hostname || ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      identities.push(identity);
    }
  }
  if (!identities.length) identities.push(providerIdentity(null, null));
  return identities;
}

function normalizeCatalogProvider(value) {
  const provider = typeof value === "string" ? { source_key: value } : value;
  if (!provider || typeof provider !== "object") return null;
  const sourceKey = String(
    provider.source_key || provider.catalog_source_key || "",
  ).trim();
  if (!sourceKey) return null;
  const providerHint = String(
    provider.provider || provider.platform || provider.key || "",
  ).trim() || null;
  const providerUrl = provider.seed_url || provider.registration_url || null;
  const identity = providerIdentity(providerHint?.toLocaleLowerCase() || null, providerUrl);
  const fallbackLabel = provider.label || provider.display_name || provider.publisher;
  const recognized = !identity.key.startsWith("host:")
    && identity.key !== "unknown"
    && identity.key !== sourceKey;
  const label = recognized
    ? identity.label
    : String(fallbackLabel || identity.label || humanize(sourceKey));
  const key = recognized
    ? identity.key
    : String(provider.filter_key || provider.key || `source:${sourceKey}`);
  const countValue = provider.event_count ?? provider.count ?? provider.current_count;
  const eventCount = Number.isFinite(Number(countValue))
    ? Math.max(0, Number(countValue))
    : null;
  return { key, label, sourceKey, eventCount };
}

function rememberCatalogProviders(values, { reset = false } = {}) {
  if (reset) state.catalogProviders.clear();
  for (const value of Array.isArray(values) ? values : []) {
    const metadata = normalizeCatalogProvider(value);
    if (!metadata) continue;
    const existing = state.catalogProviders.get(metadata.key);
    if (!existing) {
      state.catalogProviders.set(metadata.key, metadata);
      continue;
    }
    state.catalogProviders.set(metadata.key, {
      ...existing,
      eventCount: Math.max(existing.eventCount || 0, metadata.eventCount || 0) || null,
      sourceKey: existing.sourceKey === metadata.sourceKey ? existing.sourceKey : null,
    });
  }
}

function itemMatchesProvider(item, filterKey) {
  if (filterKey === "any") return true;
  if (item.catalog_filter_key === filterKey) return true;
  if (itemProviders(item).some((identity) => identity.key === filterKey)) return true;
  const metadata = state.catalogProviders.get(filterKey);
  if (!metadata?.sourceKey) return false;
  const sourceKeys = [
    item.source_key,
    item.catalog_source_key,
    ...(item.source_keys || []),
    ...(item.sources || []).map((source) => source.source_key),
  ];
  return sourceKeys.some((sourceKey) => sourceKey === metadata.sourceKey);
}

function populateSourceFilter(items, { reset = false } = {}) {
  const select = $("#filter-source");
  const selected = select.value;
  const selectedLabel = select.selectedOptions[0]?.textContent?.replace(/\s+· 0 loaded$/, "")
    || humanize(selected);
  if (reset) state.availableProviders.clear();
  if (state.currentResultKind === "catalog") {
    for (const [key, metadata] of state.catalogProviders) {
      state.availableProviders.set(key, metadata.label);
    }
  }
  for (const item of items) {
    for (const identity of itemProviders(item)) {
      if (identity.key === "unknown") continue;
      state.availableProviders.set(identity.key, identity.label);
    }
  }
  const all = node("option", "", "Any provider");
  all.value = "any";
  const options = [all];
  for (const [key, label] of [...state.availableProviders.entries()].sort((left, right) =>
    left[1].localeCompare(right[1]),
  )) {
    const metadataCount = state.catalogProviders.get(key)?.eventCount;
    const option = node(
      "option",
      "",
      metadataCount === null || metadataCount === undefined
        ? label
        : `${label} · ${metadataCount}`,
    );
    option.value = key;
    options.push(option);
  }
  if (selected !== "any" && !state.availableProviders.has(selected)) {
    const unavailable = node("option", "", `${selectedLabel} · 0 loaded`);
    unavailable.value = selected;
    options.push(unavailable);
  }
  select.replaceChildren(...options);
  select.value = selected;
  renderProviderQuickFilters(items);
}

function cityLabel(value) {
  const aliases = {
    sanfrancisco: "San Francisco",
    sanjose: "San Jose",
    sanramon: "San Ramon",
    paloalto: "Palo Alto",
    redwoodcity: "Redwood City",
    mountainview: "Mountain View",
  };
  const normalized = String(value || "").trim();
  return aliases[normalized.toLocaleLowerCase()] || humanize(normalized);
}

function populateCityFilter(items, { reset = false } = {}) {
  const select = $("#filter-city");
  const selected = select.value;
  const selectedLabel = select.selectedOptions[0]?.textContent?.replace(/\s+· 0 loaded$/, "")
    || cityLabel(selected);
  if (reset) state.availableCities.clear();
  for (const item of items) {
    const value = String(item.city || "").trim();
    if (!value) continue;
    state.availableCities.set(value, cityLabel(value));
  }
  const counts = new Map();
  for (const item of items) {
    const value = String(item.city || "").trim();
    if (value) counts.set(value, (counts.get(value) || 0) + 1);
  }
  const all = node("option", "", "Any city");
  all.value = "any";
  const options = [all];
  for (const [value, label] of [...state.availableCities.entries()].sort((left, right) =>
    left[1].localeCompare(right[1]),
  )) {
    const option = node("option", "", `${label} · ${counts.get(value) || 0}`);
    option.value = value;
    options.push(option);
  }
  if (selected !== "any" && !state.availableCities.has(selected)) {
    const unavailable = node("option", "", `${selectedLabel} · 0 loaded`);
    unavailable.value = selected;
    options.push(unavailable);
  }
  select.replaceChildren(...options);
  select.value = selected;
}

function populateFacetFilters(items, options = {}) {
  populateSourceFilter(items, options);
  populateCityFilter(items, options);
  if (
    $("#catalog-date-range-row").hidden
    && !state.catalogFilterMenuDismissed
    && $(".catalog-search-shell")?.contains(document.activeElement)
  ) {
    renderCatalogFilterSuggestions();
  }
}

function setEventFilter(name, value, { focus = true } = {}) {
  const control = $(`#filter-${name}`);
  if (!control) return;
  if (name === "when" && !String(value).startsWith("range:")) {
    removeDateRangeFilterOption();
    $("#catalog-date-start").value = "";
    $("#catalog-date-end").value = "";
  }
  if (
    name === "radius"
    && value !== DISCOVERY_DEFAULTS.radius
    && state.eventMap
  ) {
    const center = state.eventMap.getCenter();
    state.mapFilterCenter = [center.lat, center.lng];
  }
  if (state.workspaceMode === "calendar") {
    state.calendarMonth = null;
    state.calendarSelectedDateKey = null;
  }
  control.value = value;
  refreshDiscoveryView();
  if (name === "source") {
    state.catalogScopeRefreshPending = true;
    void refreshCatalogProviderScope();
  }
  if (focus) {
    const focusOwner = document.activeElement;
    window.requestAnimationFrame(() => {
      // Do not steal focus if the operator already moved to another visible control.
      if (
        document.activeElement === focusOwner
        || document.activeElement === document.body
      ) {
        $("#catalog-search").focus({ preventScroll: true });
      }
    });
  }
}

async function refreshCatalogProviderScope() {
  if (
    !["catalog", "map", "calendar"].includes(state.workspaceMode)
    || state.currentResultKind !== "catalog"
    || state.catalogEndpointAvailable !== true
  ) return;
  const filterKey = $("#filter-source").value;
  const sourceKey = selectedCatalogSourceKey();
  if (filterKey !== DISCOVERY_DEFAULTS.source && !sourceKey) return;
  if (sourceKey === state.catalogLoadedSourceKey) {
    state.catalogScopeRefreshPending = false;
    return;
  }
  if (state.discoveryInFlight) {
    state.catalogScopeRefreshPending = true;
    return;
  }
  state.catalogScopeRefreshPending = false;
  const trigger = state.workspaceMode === "map"
    ? $("#refresh-map-events")
    : $("#browse-current-events");
  await browseCurrentEvents(trigger);
}

function flushCatalogScopeRefresh() {
  if (!state.catalogScopeRefreshPending || state.discoveryInFlight) return;
  void refreshCatalogProviderScope();
}

function renderProviderQuickFilters(items = state.currentFeedItems) {
  const target = $("#provider-quick-filters");
  if (!target) return;
  const counts = new Map();
  for (const item of items) {
    const seen = new Set();
    for (const identity of itemProviders(item)) {
      if (identity.key === "unknown" || seen.has(identity.key)) continue;
      seen.add(identity.key);
      const current = counts.get(identity.key) || { label: identity.label, count: 0 };
      current.count += 1;
      counts.set(identity.key, current);
    }
  }
  if (state.currentResultKind === "catalog") {
    for (const [key, metadata] of state.catalogProviders) {
      const loaded = counts.get(key);
      counts.set(key, {
        label: metadata.label,
        count: Math.max(loaded?.count || 0, metadata.eventCount || 0),
      });
    }
  }
  const quickProviderPriority = new Map(
    ["luma", "meetup", "eventbrite", "partiful", "ticketmaster"].map(
      (key, index) => [key, index],
    ),
  );
  const buttons = [...counts.entries()]
    .sort((left, right) => {
      const leftPriority = quickProviderPriority.get(left[0]) ?? Number.POSITIVE_INFINITY;
      const rightPriority = quickProviderPriority.get(right[0]) ?? Number.POSITIVE_INFINITY;
      return leftPriority - rightPriority
        || right[1].count - left[1].count
        || left[1].label.localeCompare(right[1].label);
    })
    .slice(0, 5)
    .map(([key, value]) => {
      const button = node("button", "quick-filter quick-provider", `${value.label} · ${value.count}`);
      button.type = "button";
      button.dataset.providerKey = key;
      const active = $("#filter-source").value === key;
      button.setAttribute("aria-pressed", String(active));
      button.addEventListener("click", () => {
        setEventFilter("source", active ? DISCOVERY_DEFAULTS.source : key);
      });
      return button;
    });
  target.replaceChildren(...buttons);
}

function sameLocalDate(left, right) {
  return (
    left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate()
  );
}

function matchesWhen(item, value) {
  if (value === "any") return true;
  const start = validDate(item.start_at);
  if (!start) return false;
  const now = new Date();
  if (value === "today") return sameLocalDate(start, now);
  if (value === "tomorrow") {
    const tomorrow = new Date(now);
    tomorrow.setDate(tomorrow.getDate() + 1);
    return sameLocalDate(start, tomorrow);
  }
  if (value.startsWith("on:")) {
    const exactDate = parseLocalDateKey(value.slice(3));
    return exactDate ? sameLocalDate(start, exactDate) : false;
  }
  if (value.startsWith("range:")) {
    const range = parseDateRangeFilterValue(value);
    if (!range) return false;
    const eventDateKey = localDateKey(start);
    return eventDateKey >= range.startKey && eventDateKey <= range.endKey;
  }
  if (value === "7d" || value === "30d") {
    const days = value === "7d" ? 7 : 30;
    return start.valueOf() >= now.valueOf()
      && start.valueOf() <= now.valueOf() + (days * 24 * 60 * 60 * 1000);
  }
  if (value === "weekend") {
    const weekendStart = new Date(now);
    weekendStart.setHours(0, 0, 0, 0);
    const day = weekendStart.getDay();
    const daysUntilSaturday = day === 0 ? 0 : (6 - day + 7) % 7;
    weekendStart.setDate(weekendStart.getDate() + daysUntilSaturday);
    const weekendEnd = new Date(weekendStart);
    weekendEnd.setDate(weekendEnd.getDate() + (day === 0 ? 1 : 2));
    return start.valueOf() >= weekendStart.valueOf() && start.valueOf() < weekendEnd.valueOf();
  }
  return false;
}

function matchesPrice(item, value) {
  if (value === "any") return true;
  if (value === "listed") return ["free", "paid"].includes(item.price_status);
  return item.price_status === value;
}

function eventCoordinates(item) {
  if (
    item.latitude === null
    || item.latitude === undefined
    || item.latitude === ""
    || item.longitude === null
    || item.longitude === undefined
    || item.longitude === ""
  ) {
    return null;
  }
  const latitude = Number(item.latitude);
  const longitude = Number(item.longitude);
  if (
    !Number.isFinite(latitude)
    || !Number.isFinite(longitude)
    || latitude < -90
    || latitude > 90
    || longitude < -180
    || longitude > 180
  ) {
    return null;
  }
  return [latitude, longitude];
}

function distanceMiles(left, right) {
  if (!left || !right) return null;
  const radians = (degrees) => degrees * Math.PI / 180;
  const latitudeDelta = radians(right[0] - left[0]);
  const longitudeDelta = radians(right[1] - left[1]);
  const startLatitude = radians(left[0]);
  const endLatitude = radians(right[0]);
  const haversine = Math.sin(latitudeDelta / 2) ** 2
    + Math.cos(startLatitude) * Math.cos(endLatitude)
    * Math.sin(longitudeDelta / 2) ** 2;
  return 3958.8 * 2 * Math.atan2(Math.sqrt(haversine), Math.sqrt(1 - haversine));
}

function matchesRadius(item, value) {
  if (value === "any") return true;
  const radius = Number(value);
  const distance = distanceMiles(state.mapFilterCenter, eventCoordinates(item));
  return Number.isFinite(radius) && distance !== null && distance <= radius;
}

function googleMapsUrl(item) {
  const coordinates = eventCoordinates(item);
  const locationCopy = [item.venue_name, item.city].filter(Boolean).join(", ");
  const query = coordinates ? coordinates.join(",") : locationCopy;
  if (!query) return null;
  const url = new URL("https://www.google.com/maps/search/");
  url.searchParams.set("api", "1");
  url.searchParams.set("query", query);
  return url.href;
}

function matchesDiscovery(item, filters) {
  const city = String(item.city || "").toLocaleLowerCase();
  const requestedCity = String(filters.city || "any").toLocaleLowerCase();
  return (
    matchesWhen(item, filters.when)
    && matchesPrice(item, filters.price)
    && (requestedCity === "any" || city === requestedCity)
    && matchesRadius(item, filters.radius)
    && itemMatchesProvider(item, filters.source)
  );
}

function matchesCatalogSearch(item, query = state.catalogSearch) {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  const providers = itemProviders(item).map((identity) => identity.label);
  return [
    item.title,
    item.description,
    item.venue_name,
    item.city,
    ...providers,
  ]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase()
    .includes(needle);
}

function sortDiscoveryItems(items, sort) {
  return items
    .map((item, index) => ({ item, index }))
    .sort((left, right) => {
      if (sort === "soonest") {
        const leftDate = validDate(left.item.start_at)?.valueOf() ?? Number.POSITIVE_INFINITY;
        const rightDate = validDate(right.item.start_at)?.valueOf() ?? Number.POSITIVE_INFINITY;
        return leftDate - rightDate || left.index - right.index;
      }
      if (sort === "score") {
        const leftScore = left.item.score !== null
          && left.item.score !== undefined
          && String(left.item.score).trim() !== ""
          && Number.isFinite(Number(left.item.score))
          ? Number(left.item.score)
          : -1;
        const rightScore = right.item.score !== null
          && right.item.score !== undefined
          && String(right.item.score).trim() !== ""
          && Number.isFinite(Number(right.item.score))
          ? Number(right.item.score)
          : -1;
        return rightScore - leftScore || left.index - right.index;
      }
      if (sort === "free") {
        return Number(right.item.price_status === "free")
          - Number(left.item.price_status === "free")
          || left.index - right.index;
      }
      return left.index - right.index;
    })
    .map(({ item }) => item);
}

function discoveryFilterLabels(filters) {
  const labels = [];
  if (filters.when !== DISCOVERY_DEFAULTS.when) labels.push(selectedOptionLabel("#filter-when"));
  if (filters.city !== DISCOVERY_DEFAULTS.city) labels.push(selectedOptionLabel("#filter-city"));
  if (filters.price !== DISCOVERY_DEFAULTS.price) labels.push(selectedOptionLabel("#filter-price"));
  if (filters.source !== DISCOVERY_DEFAULTS.source) {
    labels.push(selectedOptionLabel("#filter-source"));
  }
  if (filters.radius !== DISCOVERY_DEFAULTS.radius) {
    labels.push(selectedOptionLabel("#filter-radius"));
  }
  if (filters.sort !== DISCOVERY_DEFAULTS.sort) {
    labels.push(selectedOptionLabel("#filter-sort"));
  }
  return labels;
}

function clientOnlyFilterLabels(filters) {
  const labels = [];
  if (filters.when !== "any") labels.push("when");
  if (["paid", "listed"].includes(filters.price)) labels.push("price");
  if (filters.city !== "any") labels.push("city");
  if (filters.source !== "any") labels.push("source");
  if (filters.radius !== "any") labels.push("radius");
  if (filters.sort !== DISCOVERY_DEFAULTS.sort) labels.push("sort");
  return labels;
}

function syncEventFilterRail(filters = discoveryFilters()) {
  for (const name of ["when", "city", "price", "source", "radius"]) {
    const wrapper = $(`[data-filter-control="${name}"]`);
    const active = filters[name] !== DISCOVERY_DEFAULTS[name];
    wrapper?.classList.toggle("active", active);
    const remove = $(`[data-remove-filter="${name}"]`);
    if (remove) remove.hidden = !active;
  }
  const labels = discoveryFilterLabels(filters);
  $("#clear-discovery-filters").hidden = labels.length === 0 && !state.catalogSearch;
  $('[data-quick-filter="weekend"]').setAttribute(
    "aria-pressed",
    String(filters.when === "weekend"),
  );
  $('[data-quick-filter="free"]').setAttribute(
    "aria-pressed",
    String(filters.price === "free"),
  );
  renderProviderQuickFilters();
}

function renderActiveResultFilters(filters = discoveryFilters()) {
  const target = $("#active-result-filters");
  target.replaceChildren();
  renderCatalogActiveFilters(filters);
  syncEventFilterRail(filters);
}

function updateResultsToolbar(batchCount = 0) {
  const count = $("#results-count");
  const scope = $("#results-scope");
  renderActiveResultFilters();
  if (!state.currentRawResultCount) {
    count.textContent = state.currentFeedItems.length
      ? "No events match these filters"
      : "No current catalog matches";
    scope.textContent = state.currentFeedItems.length
      ? `${state.currentFeedItems.length} returned event${state.currentFeedItems.length === 1 ? "" : "s"} remain available when filters are widened.`
      : "The service returned no ranked events for this brief.";
    return;
  }
  count.textContent = `${state.currentRawResultCount} loaded · ${batchCount} shown`;
  const filteredOut = state.currentRawResultCount - state.currentMatchCount;
  const sort = discoveryFilters().sort;
  const order = sort === "soonest"
    ? "soonest first"
    : sort === "relevance"
      ? "recommended order"
      : `${selectedOptionLabel("#filter-sort").toLocaleLowerCase()}`;
  const collectionLabel = state.currentResultKind === "catalog"
    ? "catalog events"
    : "recommendations";
  const capCopy = state.currentResultKind === "catalog" && state.catalogResultCapped
    ? ` Provider loading stopped at the ${CATALOG_PROVIDER_ITEM_CAP}-event safety cap.`
    : "";
  scope.textContent = filteredOut > 0
    ? `${state.currentRawResultCount} loaded; ${filteredOut} hidden by client-side filters. Remaining events are ${order}.${capCopy}`
    : `${state.currentRawResultCount} loaded ${collectionLabel} · ${order}.${capCopy}`;
}

function renderUnderstanding(understood) {
  const container = $("#understanding-chips");
  const values = [];
  for (const category of understood?.categories || []) values.push(humanize(category));
  if (understood?.free_only) values.push("Free only");
  if (understood?.window_start) {
    const start = validDate(understood.window_start);
    const end = validDate(understood.window_end);
    if (start) {
      const startLabel = new Intl.DateTimeFormat(undefined, {
        weekday: "short",
        month: "short",
        day: "numeric",
        hour: "numeric",
      }).format(start);
      const endLabel = end
        ? new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "numeric" }).format(end)
        : null;
      values.push(endLabel ? `${startLabel} – ${endLabel}` : startLabel);
    }
  }
  if (understood?.radius_km) values.push(`Within ${Math.round(understood.radius_km)} km`);
  container.replaceChildren(...values.map((value) => node("span", "understanding-chip", value)));
}

function clearThreadEmpty() {
  $("#recommendation-messages .thread-empty")?.remove();
  $("#concierge-page").classList.add("has-conversation");
}

function resetRecommendationSession() {
  state.discoveryController?.abort();
  state.discoveryController = null;
  state.discoveryGeneration += 1;
  state.discoveryInFlight = false;
  state.currentQuery = "";
  state.pendingPicks = [];
  state.nextCursor = null;
  state.currentFeedItems = [];
  state.currentUnderstood = null;
  state.currentRawResultCount = 0;
  state.currentMatchCount = 0;
  state.hasFeedResponse = false;
  state.currentResultKind = null;
  state.catalogSearch = "";
  state.catalogFilterSuggestions = [];
  state.catalogFilterSuggestionIndex = -1;
  state.catalogFilterSuggestionSignature = "";
  state.catalogFilterMenuDismissed = false;
  state.catalogFilterEditKind = null;
  state.catalogFilterEditDraft = "";
  state.availableProviders.clear();
  state.catalogProviders.clear();
  state.catalogEndpointAvailable = null;
  state.catalogLoadedSourceKey = null;
  state.catalogScopeRefreshPending = false;
  state.catalogResultCapped = false;
  state.availableCities.clear();
  state.dismissedEventIds.clear();
  state.likedEventIds.clear();
  state.mapFilterCenter = null;
  state.mapLastSignature = "";
  state.selectedMapEventId = null;
  state.calendarMonth = null;
  state.calendarSelectedDateKey = null;
  removeDateRangeFilterOption();
  $("#filter-when").value = DISCOVERY_DEFAULTS.when;
  $("#filter-price").value = DISCOVERY_DEFAULTS.price;
  $("#filter-city").value = DISCOVERY_DEFAULTS.city;
  $("#filter-source").value = DISCOVERY_DEFAULTS.source;
  $("#filter-radius").value = DISCOVERY_DEFAULTS.radius;
  $("#filter-sort").value = DISCOVERY_DEFAULTS.sort;
  $("#catalog-search").value = "";
  $("#catalog-search").placeholder = catalogFilterSearchPlaceholder();
  $("#catalog-date-start").value = "";
  $("#catalog-date-end").value = "";
  closeCatalogFilterMenu();
  populateFacetFilters([], { reset: true });
  const empty = node("div", "thread-empty");
  const mark = node("span", "", "✦");
  mark.setAttribute("aria-hidden", "true");
  append(
    empty,
    mark,
    node("p", "", "Start a chat to see recommendations here."),
  );
  $("#recommendation-messages").replaceChildren(empty);
  $("#concierge-page").classList.remove("has-conversation");
  $("#ask-input").value = "";
  setError($("#ask-error"));
  $("#request-progress").hidden = true;
  renderUnderstanding(null);
  renderEmpty(
    $("#picks-list"),
    "✦",
    "No events loaded yet",
    "Open Events to load the latest ranked catalog response.",
  );
  $("#picks-list").removeAttribute("aria-busy");
  $("#results-count").textContent = "No catalog response yet";
  $("#results-scope").textContent = "Submit a brief or browse to load current events.";
  renderActiveResultFilters();
  $("#next-picks").hidden = true;
  $("#map-event-list").replaceChildren();
  $("#map-result-count").textContent = "0 mapped";
  $("#map-status").textContent = "Load events to use the map.";
  state.eventMapMarkers?.clearLayers();
  $("#event-calendar-grid").replaceChildren();
  $("#calendar-current-month").textContent = "Current month";
  $("#calendar-agenda-heading").textContent = "Choose a date";
  $("#calendar-agenda-count").textContent = "0 events";
  renderEmpty(
    $("#calendar-agenda-list"),
    "□",
    "No day selected",
    "Choose a date to see its events here.",
  );
  const ask = $("#ask-submit");
  const browse = $("#browse-current-events");
  const next = $("#next-picks");
  if (ask) setButtonBusy(ask, false, "");
  if (browse) setButtonBusy(browse, false, "");
  if (next) setButtonBusy(next, false, "");
  updateAskButton();
}

function threadMessage(role, label, copy) {
  const message = node("article", `thread-message thread-message-${role}`);
  message.setAttribute("aria-label", label);
  const avatar = node("span", "thread-avatar", role === "user" ? "You" : "EC");
  avatar.setAttribute("aria-hidden", "true");
  const body = node("div", "thread-message-body");
  append(body, node("strong", "thread-speaker", label), node("p", "thread-copy", copy));
  append(message, avatar, body);
  return { message, body };
}

function beginRecommendationTurn({ displayText, sentText, mode, filters }) {
  clearThreadEmpty();
  const messages = $("#recommendation-messages");
  for (const action of $$(".thread-more-results", messages)) {
    action.disabled = true;
    action.dataset.stale = "true";
    action.textContent = "Result set replaced";
  }
  const user = threadMessage(
    "user",
    "You",
    displayText,
  );
  const userMeta = node("div", "thread-meta");
  userMeta.append(
    node("span", "thread-mode", mode === "handle" ? "Durable brief" : "Preview only"),
  );
  for (const label of discoveryFilterLabels(filters)) {
    userMeta.append(node("span", "thread-filter", label));
  }
  if (sentText.includes("\n\nDiscovery context:")) {
    userMeta.append(
      node(
        "small",
        "thread-sent-copy",
        "The shared event filters were appended to this request as discovery context.",
      ),
    );
  }
  user.body.append(userMeta);

  const assistant = threadMessage(
    "assistant",
    "Concierge · catalog summary",
    "Checking the current catalog response…",
  );
  assistant.message.classList.add("pending");
  assistant.body.append(node("span", "thread-pending", "Catalog request in progress"));
  append(messages, user.message, assistant.message);
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  assistant.message.scrollIntoView({
    block: "nearest",
    behavior: reduceMotion ? "auto" : "smooth",
  });
  return assistant;
}

function recommendationProviderLabels(items) {
  const providers = new Map();
  for (const item of items) {
    for (const identity of itemProviders(item)) {
      if (identity.key !== "unknown") providers.set(identity.key, identity.label);
    }
  }
  return [...providers.values()].sort((left, right) => left.localeCompare(right));
}

function completeRecommendationTurn(
  turn,
  {
    feed,
    mode,
    workflowStarted = false,
    rawText,
    error = null,
  },
) {
  turn.message.classList.remove("pending");
  $(".thread-pending", turn.body)?.remove();
  const copy = $(".thread-copy", turn.body);
  if (error) {
    turn.message.classList.add("error");
    copy.textContent = `I couldn’t finish that search: ${error.message}`;
  } else {
    // The summary reports provider truth — what the feed actually returned — while the
    // picks list applies the shared discovery filters locally. Counting the filtered
    // subset here made the assistant under-report results the user could still reveal
    // by relaxing a filter.
    const loadedItems = Array.isArray(feed?.items) ? feed.items : [];
    const rawCount = loadedItems.length;
    const durableCopy = mode === "handle"
      ? workflowStarted
        ? " I also saved your brief and started its durable workflow."
        : " I saved your brief; background handling will resume when its worker is available."
      : " This is only a preview—nothing was registered.";
    copy.textContent = rawCount
      ? `I found ${rawCount} current option${rawCount === 1 ? "" : "s"}.${durableCopy}`
      : `I didn’t find a current match for that brief.${durableCopy}`;
    const providers = recommendationProviderLabels(loadedItems);
    if (providers.length) {
      const grounding = node("details", "thread-grounding-details");
      const summary = node("summary", "", "Where these results came from");
      const sourceLine = node(
        "p",
        "thread-grounding",
        `Loaded providers: ${providers.join(", ")}. Open an event’s details for its source record.`,
      );
      append(grounding, summary, sourceLine);
      turn.body.append(grounding);
    }
  }

  const actions = node("div", "thread-actions");
  const refine = node("button", "text-button", "Edit prompt");
  refine.type = "button";
  refine.addEventListener("click", () => {
    $("#ask-input").value = rawText;
    $("#ask-input").focus();
  });
  actions.append(refine);
  if (!error) {
    const viewAll = node("button", "text-button", "View in Events");
    viewAll.type = "button";
    viewAll.addEventListener("click", () => navigate("catalog"));
    actions.append(viewAll);
  }
  turn.body.append(actions);
}

function mapPopupContent(items) {
  const content = node("div", "map-popup");
  if (items.length > 1) {
    content.append(node("strong", "map-popup-heading", `${items.length} events here`));
  }
  for (const item of items.slice(0, 4)) {
    const event = node("article", "map-popup-event");
    const title = externalLink(
      eventRegistrationUrl(item),
      item.title,
      "map-popup-title",
    ) || node("strong", "map-popup-title", item.title);
    const timing = calendarTimingLink(
      item,
      eventFeedTimingLabel(item.start_at, item.end_at),
      "map-popup-time calendar-add-link",
    );
    const locationCopy = [item.venue_name, item.city].filter(Boolean).join(" · ");
    const location = locationCopy
      ? externalLink(googleMapsUrl(item), locationCopy, "map-popup-location")
      : null;
    append(event, title, timing, location);
    content.append(event);
  }
  return content;
}

function ensureEventMap() {
  if (state.eventMap) return true;
  if (!window.L) {
    $("#map-status").textContent = "The interactive map could not load. Events remain available in the list.";
    return false;
  }
  const map = window.L.map("events-map", {
    scrollWheelZoom: false,
    zoomControl: true,
  });
  window.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  state.eventMapMarkers = window.L.layerGroup().addTo(map);
  state.eventMap = map;
  map.setView([37.78, -122.28], 9, { animate: false });
  map.on("moveend", () => {
    if (state.mapSuppressMoveNotice) return;
    if ($("#filter-radius").value !== DISCOVERY_DEFAULTS.radius) {
      $("#map-search-area").hidden = false;
    }
  });
  window.requestAnimationFrame(() => map.invalidateSize());
  return true;
}

function selectMapEvent(item, { focusCard = false } = {}) {
  state.selectedMapEventId = item.canonical_event_id;
  $$(".map-event-card").forEach((card) => {
    card.classList.toggle("selected", card.dataset.eventId === item.canonical_event_id);
  });
  const marker = state.mapMarkerByEvent.get(item.canonical_event_id);
  const coordinates = eventCoordinates(item);
  if (marker && coordinates && state.eventMap) {
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    state.eventMap.setView(
      coordinates,
      Math.max(state.eventMap.getZoom(), 12),
      { animate: !reduceMotion },
    );
    marker.openPopup();
  }
  if (focusCard) {
    const card = $(`.map-event-card[data-event-id="${item.canonical_event_id}"]`);
    card?.scrollIntoView({ block: "nearest" });
    $(".map-event-select", card)?.focus({ preventScroll: true });
  }
}

function mapEventCard(item) {
  const card = node("article", "map-event-card");
  card.setAttribute("role", "listitem");
  card.dataset.eventId = item.canonical_event_id;
  const primaryProvider = itemProviders(item)[0];
  const coordinates = eventCoordinates(item);
  const select = node(coordinates ? "button" : "div", "map-event-select");
  if (coordinates) {
    select.type = "button";
    select.setAttribute(
      "aria-label",
      `Show ${item.title} on the map, ${eventDateTime(item.start_at)}`,
    );
  } else {
    card.classList.add("unmapped");
  }
  const date = validDate(item.start_at);
  const dateCopy = date
    ? new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date)
    : "Date TBD";
  const copy = node("span", "map-event-copy");
  append(
    copy,
    node("strong", "", item.title),
    node(
      "span",
      "",
      `${eventFeedTimingLabel(item.start_at, item.end_at)} · ${priceLabel(item.price_status)}`,
    ),
  );
  append(select, node("span", "map-event-date", dateCopy), copy);
  if (coordinates) select.addEventListener("click", () => selectMapEvent(item));

  const actions = node("div", "map-event-actions");
  const provider = providerFilterButton(primaryProvider);
  actions.append(provider);
  if (!coordinates) actions.append(node("span", "map-coordinate-note", "No exact pin"));
  const maps = externalLink(googleMapsUrl(item), "Map ↗", "map-mini-link");
  if (maps) {
    maps.setAttribute("aria-label", `Open ${item.title} location in Google Maps (opens in new tab)`);
    actions.append(maps);
  }
  const eventSite = externalLink(eventRegistrationUrl(item), "Event ↗", "map-mini-link");
  if (eventSite) {
    eventSite.setAttribute("aria-label", `Open event site for ${item.title} (opens in new tab)`);
    actions.append(eventSite);
  }
  const calendar = externalLink(
    googleCalendarUrl(item),
    "Calendar ↗",
    "map-mini-link calendar-add-link",
  );
  if (calendar) {
    calendar.setAttribute(
      "aria-label",
      `Add ${item.title} to Google Calendar (opens in new tab)`,
    );
    actions.append(calendar);
  }
  append(card, select, actions);
  return card;
}

function renderEventMap(items) {
  const list = $("#map-event-list");
  const mapped = items.filter((item) => eventCoordinates(item));
  const unmapped = items.filter((item) => !eventCoordinates(item));
  $("#map-result-count").textContent = `${mapped.length} mapped${unmapped.length ? ` · ${unmapped.length} without coordinates` : ""}`;
  const mapCopy = mapped.length
    ? `${mapped.length} event${mapped.length === 1 ? "" : "s"} on the map. ${unmapped.length ? `${unmapped.length} remain in the list without coordinates.` : ""}`
    : "No filtered events have map coordinates. They remain available in the list.";
  $("#map-status").textContent = state.catalogResultCapped
    ? `${mapCopy} Provider loading reached the ${CATALOG_PROVIDER_ITEM_CAP}-event safety cap.`
    : mapCopy;
  list.replaceChildren(...items.map(mapEventCard));
  if (!ensureEventMap()) return;

  state.eventMapMarkers.clearLayers();
  state.mapMarkerByEvent.clear();
  const groups = new Map();
  for (const item of mapped) {
    const coordinates = eventCoordinates(item);
    const key = `${coordinates[0].toFixed(5)},${coordinates[1].toFixed(5)}`;
    const group = groups.get(key) || { coordinates, items: [] };
    group.items.push(item);
    groups.set(key, group);
  }
  let markerIndex = 0;
  for (const group of groups.values()) {
    markerIndex += 1;
    const markerContent = node(
      "span",
      "event-marker-count",
      group.items.length > 1 ? group.items.length : markerIndex,
    );
    const icon = window.L.divIcon({
      className: `event-map-marker${group.items.length > 1 ? " cluster" : ""}`,
      html: markerContent,
      iconSize: [34, 34],
      iconAnchor: [17, 17],
    });
    const label = group.items.length > 1
      ? `${group.items.length} events at this location`
      : `${group.items[0].title}, ${eventDateTime(group.items[0].start_at)}`;
    const marker = window.L.marker(group.coordinates, {
      icon,
      keyboard: true,
      title: label,
      alt: label,
    });
    marker.bindPopup(mapPopupContent(group.items), { maxWidth: 300 });
    marker.on("click", () => selectMapEvent(group.items[0], { focusCard: true }));
    marker.addTo(state.eventMapMarkers);
    marker.getElement()?.setAttribute("aria-label", label);
    for (const item of group.items) state.mapMarkerByEvent.set(item.canonical_event_id, marker);
  }

  const signature = mapped.map((item) => item.canonical_event_id).join("|");
  if (
    mapped.length
    && signature !== state.mapLastSignature
    && $("#filter-radius").value === DISCOVERY_DEFAULTS.radius
  ) {
    const bounds = window.L.latLngBounds(mapped.map((item) => eventCoordinates(item)));
    state.mapSuppressMoveNotice = true;
    state.eventMap.fitBounds(bounds, {
      padding: [36, 36],
      maxZoom: mapped.length === 1 ? 13 : 12,
      animate: false,
    });
    state.mapFilterCenter = [
      state.eventMap.getCenter().lat,
      state.eventMap.getCenter().lng,
    ];
    window.setTimeout(() => {
      state.mapSuppressMoveNotice = false;
    }, 0);
  }
  state.mapLastSignature = signature;
  $("#map-search-area").hidden = true;
}

function calendarItemsByDate(items) {
  const groups = new Map();
  for (const item of items) {
    const start = validDate(item.start_at);
    if (!start) continue;
    const key = localDateKey(start);
    const group = groups.get(key) || [];
    group.push(item);
    groups.set(key, group);
  }
  return groups;
}

function firstDayOfMonth(value) {
  const date = validDate(value) || new Date();
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function calendarMonthLabel(value) {
  return new Intl.DateTimeFormat(undefined, {
    month: "long",
    year: "numeric",
  }).format(value);
}

function calendarAgendaCard(item) {
  const card = node("article", "calendar-agenda-card");
  card.setAttribute("role", "listitem");
  card.dataset.eventId = item.canonical_event_id;
  const start = validDate(item.start_at);
  const timeCopy = start
    ? new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
    }).format(start)
    : "TBD";
  const time = calendarTimingLink(
    item,
    timeCopy,
    "calendar-agenda-time calendar-add-link",
  );
  const copy = node("div", "calendar-agenda-copy");
  const heading = node("h3");
  const eventSite = externalLink(
    eventRegistrationUrl(item),
    item.title,
    "calendar-agenda-title-link",
  );
  heading.append(eventSite || node("span", "", item.title));
  const locationCopy = [item.venue_name, item.city].filter(Boolean).join(" · ")
    || "Location to be confirmed";
  const location = externalLink(
    googleMapsUrl(item),
    locationCopy,
    "calendar-agenda-location",
  );
  const metadata = node("p");
  append(
    metadata,
    location || node("span", "", locationCopy),
    node("span", "", ` · ${priceLabel(item.price_status)}`),
  );
  const descriptionCopy = String(item.description || "").trim();
  const description = node(
    "p",
    "calendar-agenda-description",
    descriptionCopy || "No description was supplied by the provider.",
  );
  const actions = node("div", "calendar-agenda-actions");
  const primaryProvider = itemProviders(item)[0];
  actions.append(providerFilterButton(primaryProvider));
  const open = externalLink(
    eventRegistrationUrl(item),
    "Event ↗",
    "calendar-add-link",
  );
  if (open) actions.append(open);
  append(copy, heading, metadata, description, actions);
  append(card, time, copy);
  return card;
}

function renderCalendarAgenda(itemsByDate, dateKey) {
  const list = $("#calendar-agenda-list");
  const date = parseLocalDateKey(dateKey);
  const items = itemsByDate.get(dateKey) || [];
  $("#calendar-agenda-heading").textContent = date
    ? new Intl.DateTimeFormat(undefined, {
      weekday: "long",
      month: "long",
      day: "numeric",
    }).format(date)
    : "Choose a date";
  $("#calendar-agenda-count").textContent = `${items.length} event${items.length === 1 ? "" : "s"}`;
  if (!date) {
    renderEmpty(
      list,
      "□",
      "No day selected",
      "Choose a date to see its events here.",
    );
    return;
  }
  if (!items.length) {
    renderEmpty(
      list,
      "○",
      "No matching events",
      "This date has no events in the current filtered catalog.",
    );
    return;
  }
  list.replaceChildren(...items.map(calendarAgendaCard));
}

function renderEventCalendar(items) {
  const grid = $("#event-calendar-grid");
  const itemsByDate = calendarItemsByDate(items);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  if (!state.calendarMonth) {
    const todayKey = localDateKey(today);
    const firstEvent = items.find((item) => validDate(item.start_at));
    state.calendarMonth = itemsByDate.has(todayKey)
      ? firstDayOfMonth(today)
      : firstDayOfMonth(firstEvent?.start_at || today);
  } else {
    state.calendarMonth = firstDayOfMonth(state.calendarMonth);
  }

  const visibleMonth = state.calendarMonth;
  const firstVisibleDate = addLocalDays(visibleMonth, -visibleMonth.getDay());
  const lastVisibleDate = addLocalDays(firstVisibleDate, 41);
  const selectedStillVisible = state.calendarSelectedDateKey
    && state.calendarSelectedDateKey >= localDateKey(firstVisibleDate)
    && state.calendarSelectedDateKey <= localDateKey(lastVisibleDate);
  if (!selectedStillVisible) {
    const monthPrefix = `${visibleMonth.getFullYear()}-${String(
      visibleMonth.getMonth() + 1,
    ).padStart(2, "0")}-`;
    state.calendarSelectedDateKey = [...itemsByDate.keys()]
      .sort()
      .find((key) => key.startsWith(monthPrefix))
      || null;
  }

  $("#calendar-current-month").textContent = calendarMonthLabel(visibleMonth);
  const cells = [];
  for (let offset = 0; offset < 42; offset += 1) {
    const date = addLocalDays(firstVisibleDate, offset);
    const key = localDateKey(date);
    const dayItems = itemsByDate.get(key) || [];
    const cell = node("button", "calendar-day");
    cell.type = "button";
    cell.dataset.date = key;
    cell.dataset.dateKey = key;
    cell.dataset.outsideMonth = String(date.getMonth() !== visibleMonth.getMonth());
    cell.classList.toggle("outside-month", date.getMonth() !== visibleMonth.getMonth());
    cell.classList.toggle("is-today", sameLocalDate(date, today));
    cell.classList.toggle("is-selected", state.calendarSelectedDateKey === key);
    cell.setAttribute("role", "gridcell");
    cell.setAttribute("aria-selected", String(state.calendarSelectedDateKey === key));
    if (sameLocalDate(date, today)) cell.setAttribute("aria-current", "date");
    cell.setAttribute(
      "aria-label",
      `${new Intl.DateTimeFormat(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
      }).format(date)}, ${dayItems.length} event${dayItems.length === 1 ? "" : "s"}`,
    );
    const dayNumber = node("span", "calendar-day-number", date.getDate());
    const events = node("span", "calendar-day-events");
    for (const item of dayItems.slice(0, 3)) {
      events.append(node("span", "calendar-event-pill", item.title));
    }
    if (dayItems.length > 3) {
      events.append(
        node("span", "calendar-event-overflow", `+${dayItems.length - 3} more`),
      );
    }
    append(cell, dayNumber, events);
    cell.addEventListener("click", () => {
      state.calendarSelectedDateKey = key;
      if (date.getMonth() !== state.calendarMonth.getMonth()) {
        state.calendarMonth = firstDayOfMonth(date);
      }
      renderEventCalendar(items);
      $("#calendar-agenda-heading").focus({ preventScroll: true });
    });
    cells.push(cell);
  }
  grid.replaceChildren(...cells);
  renderCalendarAgenda(itemsByDate, state.calendarSelectedDateKey);
}

function shiftCalendarMonth(offset) {
  const month = state.calendarMonth || firstDayOfMonth(new Date());
  state.calendarMonth = new Date(month.getFullYear(), month.getMonth() + offset, 1);
  state.calendarSelectedDateKey = null;
  applyDiscoveryToCurrentFeed();
  $("#calendar-current-month").focus({ preventScroll: true });
}

function hasMorePicks() {
  return state.pendingPicks.length > 0 || Boolean(state.nextCursor);
}

function renderPickBatch() {
  const container = $("#picks-list");
  const visibleLimit = state.workspaceMode === "chat" ? 3 : MAX_VISIBLE_PICKS;
  const items = state.pendingPicks.splice(0, visibleLimit);
  $("#next-picks").hidden = !hasMorePicks();
  updateResultsToolbar(items.length);
  if (!items.length) {
    renderEmpty(
      container,
      "○",
      "No confident matches yet",
      "Try widening the timing, location, or price. Nothing was registered.",
    );
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of items) fragment.append(pickCard(item));
  container.replaceChildren(fragment);
}

function applyDiscoveryToCurrentFeed() {
  const filters = discoveryFilters();
  const filtered = state.currentFeedItems.filter(
    (item) => matchesDiscovery(item, filters) && matchesCatalogSearch(item),
  );
  state.pendingPicks = sortDiscoveryItems(
    filtered,
    filters.sort,
  );
  state.currentMatchCount = state.pendingPicks.length;
  if (state.workspaceMode === "map") {
    updateResultsToolbar(state.pendingPicks.length);
    $("#next-picks").hidden = true;
    renderEventMap(state.pendingPicks);
  } else if (state.workspaceMode === "calendar") {
    updateResultsToolbar(state.pendingPicks.length);
    $("#next-picks").hidden = true;
    renderEventCalendar(state.pendingPicks);
  } else {
    renderPickBatch();
  }
}

function renderPicks(
  feed,
  error = null,
  {
    resetProviders = true,
    resultKind = ["catalog", "map", "calendar"].includes(state.workspaceMode) ? "catalog" : "feed",
    resetCatalogProviders = false,
  } = {},
) {
  const container = $("#picks-list");
  state.hasFeedResponse = !error;
  state.currentResultKind = resultKind;
  if (resultKind === "catalog") {
    rememberCatalogProviders(feed?.providers, { reset: resetCatalogProviders });
  }
  if (resetProviders) state.dismissedEventIds.clear();
  state.currentFeedItems = error
    ? []
    : [...(feed?.items || [])].filter(
        (item) => !state.dismissedEventIds.has(item.canonical_event_id),
      );
  state.currentRawResultCount = state.currentFeedItems.length;
  state.currentUnderstood = error ? null : feed?.understood || null;
  state.currentMatchCount = 0;
  populateFacetFilters(state.currentFeedItems, { reset: resetProviders });
  state.nextCursor = error ? null : feed?.next_cursor || null;
  renderUnderstanding(feed?.understood);
  if (error) {
    state.pendingPicks = [];
    $("#next-picks").hidden = true;
    $("#results-count").textContent = "Catalog response unavailable";
    $("#results-scope").textContent = "No result filtering or sorting was applied.";
    renderActiveResultFilters();
    renderEmpty(container, "!", "I couldn’t finish that search", error.message);
    if (state.workspaceMode === "calendar") {
      $("#event-calendar-grid").replaceChildren();
      renderEmpty(
        $("#calendar-agenda-list"),
        "!",
        "Calendar unavailable",
        error.message,
      );
    }
    if (state.workspaceMode === "map") {
      $("#map-event-list").replaceChildren();
      $("#map-status").textContent = error.message;
    }
    return;
  }
  applyDiscoveryToCurrentFeed();
}

function catalogScoreLabel(value) {
  if (value === null || value === undefined || value === "") {
    return state.currentResultKind === "catalog"
      ? "Chronological"
      : "Catalog rank unavailable";
  }
  const score = Number(value);
  return Number.isFinite(score)
    ? `Catalog rank ${new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(score)}`
    : "Catalog rank unavailable";
}

function eventTimingLabel(startValue, endValue) {
  const start = validDate(startValue);
  const end = validDate(endValue);
  if (!start) return "Time to be confirmed";
  const startCopy = eventDateTime(startValue);
  if (!end) return startCopy;
  const endCopy = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(end);
  return `${startCopy} – ${endCopy}`;
}

function eventFeedTimingLabel(startValue, endValue) {
  const start = validDate(startValue);
  const end = validDate(endValue);
  if (!start) return "Time to be confirmed";
  const startCopy = new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(start);
  if (!end) return startCopy;
  const endCopy = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(end);
  return `${startCopy} – ${endCopy}`;
}

function eventRegistrationUrl(item) {
  const candidate = item.sources?.[0]?.registration_url || item.registration_urls?.[0];
  return safeUrl(candidate);
}

function availabilityLabel(item) {
  if (item.event_status === "cancelled") return "Organizer marks this event cancelled";
  if (item.event_status === "rescheduled") return "Organizer marks this event rescheduled";
  if (
    item.conflict === "not_evaluated"
    || item.registerable === null
    || item.registerable === undefined
  ) {
    return eventRegistrationUrl(item)
      ? "Not conflict-checked · event link available"
      : "Not conflict-checked · event link unavailable";
  }
  if (!item.registerable) return "Blocked from handling by the current conflict verdict";
  return eventRegistrationUrl(item)
    ? "Eligible to handle · event link listed"
    : "Eligible to handle · event link unavailable";
}

function handlingEligibilityLabel(item) {
  if (item.event_status === "cancelled") return "Unavailable · cancelled";
  if (
    item.conflict === "not_evaluated"
    || item.registerable === null
    || item.registerable === undefined
  ) {
    return eventRegistrationUrl(item)
      ? "Event link available"
      : "Not conflict-checked";
  }
  return item.registerable ? "Eligible to handle" : "Conflict blocked";
}

function detailFact(term, value) {
  const wrapper = node("div", "pick-detail-fact");
  const description = node("dd");
  if (value instanceof Node) description.append(value);
  else description.textContent = String(value);
  append(wrapper, node("dt", "", term), description);
  return wrapper;
}

function pickDetails(item, identities) {
  const details = node("details", "pick-details");
  const summary = node("summary", "", "Details");
  summary.setAttribute("aria-label", `View event details for ${item.title}`);
  details.append(summary);
  const content = node("div", "pick-details-content");
  const facts = node("dl", "pick-detail-grid");
  const locationCopy = [item.venue_name, item.city].filter(Boolean).join(" · ")
    || "Venue to be confirmed";
  const locationLink = externalLink(
    googleMapsUrl(item),
    locationCopy,
    "pick-detail-map-link",
  );
  if (locationLink) {
    locationLink.setAttribute(
      "aria-label",
      `Open ${item.title} location in Google Maps (opens in new tab)`,
    );
  }
  append(
    facts,
    detailFact(
      "Full timing",
      calendarTimingLink(
        item,
        eventTimingLabel(item.start_at, item.end_at),
        "calendar-add-link pick-detail-calendar-link",
      ),
    ),
    detailFact("Location", locationLink || locationCopy),
    detailFact("Availability", availabilityLabel(item)),
    detailFact("Ranking", catalogScoreLabel(item.score)),
    detailFact("Listing status", humanize(item.event_status)),
    detailFact(
      "Registration lanes",
      item.lanes?.length ? item.lanes.map(humanize).join(", ") : "None reported",
    ),
  );
  content.append(facts);
  const description = node("section", "pick-description");
  description.setAttribute("aria-label", "About this event");
  const descriptionCopy = String(item.description || "").trim();
  append(
    description,
    node("strong", "pick-detail-label", "About this event"),
    node(
      "p",
      `pick-description-copy${descriptionCopy ? "" : " pick-description-empty"}`,
      descriptionCopy || "The provider did not include a description for this event.",
    ),
  );
  const descriptionSource = !descriptionCopy
    ? externalLink(
      eventRegistrationUrl(item),
      "Check the event page ↗",
      "pick-description-source",
    )
    : null;
  if (descriptionSource) {
    descriptionSource.setAttribute(
      "aria-label",
      `Check the event page for ${item.title} (opens in new tab)`,
    );
    description.append(descriptionSource);
  }
  content.append(description);
  const sourceHeading = node("strong", "pick-detail-label", "Source records");
  const sourceList = node("ul", "source-truth-list");
  for (const identity of identities) {
    const hostCopy = identity.hostname && identity.hostname !== identity.label
      ? ` · ${identity.hostname}`
      : "";
    const copy = `${identity.label} · source ${identity.rawSource}${hostCopy}`;
    const item = node("li");
    const link = externalLink(identity.registrationHref, copy, "source-record-link");
    append(item, link || node("span", "", copy));
    sourceList.append(item);
  }
  append(content, sourceHeading, sourceList);
  details.append(content);
  return details;
}

function providerFilterButton(identity) {
  const provider = node("button", "pick-provider provider-filter", identity.label);
  provider.type = "button";
  const selected = $("#filter-source").value === identity.key;
  provider.setAttribute("aria-pressed", String(selected));
  provider.setAttribute(
    "aria-label",
    selected
      ? `Remove ${identity.label} provider filter`
      : `Filter events by ${identity.label}`,
  );
  provider.title = selected
    ? `Show events from every provider`
    : `Show only ${identity.label} events`;
  provider.addEventListener("click", () => {
    setEventFilter(
      "source",
      selected ? DISCOVERY_DEFAULTS.source : identity.key,
    );
  });
  return provider;
}

function pickCard(item) {
  const card = node("article", "pick-card event-row");
  card.setAttribute("role", "listitem");
  card.dataset.eventId = item.canonical_event_id;
  const main = node("div", "pick-main");
  const identities = itemProviders(item);
  const primaryProvider = identities[0];
  const sourceCopy = node("span", "pick-source");
  const provider = providerFilterButton(primaryProvider);
  const sourceTruth = node(
    "span",
    "pick-source-truth",
    primaryProvider.label === sourceLabel(primaryProvider.rawSource)
      ? `Source record · ${primaryProvider.rawSource}`
      : `via ${sourceLabel(primaryProvider.rawSource)} · ${primaryProvider.rawSource}`,
  );
  append(sourceCopy, provider, sourceTruth);
  const details = pickDetails(item, identities);
  details.id = `event-details-${String(item.canonical_event_id).replace(/[^a-z0-9_-]/gi, "")}`;
  const title = node("h3");
  const titleButton = node("button", "pick-title-button", item.title);
  titleButton.type = "button";
  titleButton.setAttribute("aria-expanded", "false");
  titleButton.setAttribute("aria-controls", details.id);
  titleButton.addEventListener("click", () => {
    details.open = !details.open;
  });
  details.addEventListener("toggle", () => {
    titleButton.setAttribute("aria-expanded", String(details.open));
  });
  title.append(titleButton);
  const meta = node("p", "pick-meta");
  const locationCopy = [item.venue_name, item.city].filter(Boolean).join(" · ")
    || "Venue to be confirmed";
  const location = externalLink(
    googleMapsUrl(item),
    locationCopy,
    "pick-location pick-location-link",
  ) || node("span", "pick-location", locationCopy);
  if (location instanceof HTMLAnchorElement) {
    location.setAttribute(
      "aria-label",
      `Open ${item.title} location in Google Maps (opens in new tab)`,
    );
  }
  append(
    meta,
    sourceCopy,
    calendarTimingLink(
      item,
      eventFeedTimingLabel(item.start_at, item.end_at),
      "pick-time calendar-add-link",
    ),
    location,
    node("span", "pick-price", priceLabel(item.price_status)),
  );
  append(main, title, meta);

  const tags = node("div", "pick-tags");
  append(
    tags,
    node("span", "tag tag-score", catalogScoreLabel(item.score)),
    node(
      "span",
      `tag ${item.registerable && item.event_status !== "cancelled" ? "tag-available" : ""}`,
      handlingEligibilityLabel(item),
    ),
  );
  if (item.event_status === "rescheduled") tags.append(node("span", "tag tag-warning", "Organizer rescheduled"));
  if (item.event_status === "cancelled") tags.append(node("span", "tag tag-warning", "Organizer cancelled"));
  if (item.conflict === "demote") tags.append(node("span", "tag tag-warning", "Close to another plan"));
  if (item.conflict === "blocked") tags.append(node("span", "tag tag-warning", "Calendar conflict"));
  if ($(".tag-warning", tags)) tags.classList.add("has-warning");
  main.append(tags);

  if (item.rationale) {
    const rationale = node("p", "pick-rationale");
    const sparkle = node("span", "", "✦");
    sparkle.setAttribute("aria-hidden", "true");
    append(rationale, sparkle, node("span", "", item.rationale));
    main.append(rationale);
  }

  const actions = node("div", "pick-actions");
  const sourceUrl = eventRegistrationUrl(item);
  const visit = externalLink(sourceUrl, "↗", "event-open-link");
  if (visit) {
    visit.setAttribute("aria-label", `Open event site for ${item.title} (opens in new tab)`);
    visit.addEventListener("click", () => {
      void recordFeedback(item.canonical_event_id, "click").catch(() => {});
    });
    actions.append(visit);
  }
  append(main, details, actions);
  append(card, dateTile(item.start_at, "pick-date", item), main);
  return card;
}

async function recordFeedback(eventId, kind, signalId = crypto.randomUUID()) {
  return api("/v1/feed-feedback", {
    method: "POST",
    body: {
      signal_id: signalId,
      canonical_event_id: eventId,
      kind,
    },
    keepalive: kind === "click",
  });
}

function showPickSkeletons() {
  const container = $("#picks-list");
  container.replaceChildren(
    node("div", "loading-card"),
    node("div", "loading-card"),
    node("div", "loading-card"),
  );
  container.setAttribute("aria-busy", "true");
  $("#next-picks").hidden = true;
  $("#results-count").textContent = "Loading current catalog…";
  $("#results-scope").textContent = "Results will be filtered and sorted in this tab after they return.";
  if (state.workspaceMode === "map") {
    $("#map-status").textContent = "Loading events for the map…";
    $("#map-event-list").replaceChildren(
      node("div", "map-loading-card"),
      node("div", "map-loading-card"),
      node("div", "map-loading-card"),
    );
  } else if (state.workspaceMode === "calendar") {
    $("#event-calendar-grid").replaceChildren();
    $("#calendar-current-month").textContent = "Loading calendar…";
    $("#calendar-agenda-count").textContent = "Loading";
    $("#calendar-agenda-list").replaceChildren(
      node("div", "loading-card"),
      node("div", "loading-card"),
    );
  }
  renderActiveResultFilters();
}

async function runDiscovery({
  baseText,
  displayText,
  mode,
  trigger,
  showInChat = true,
  useCatalogFilters = true,
}) {
  if (state.discoveryInFlight) return;
  const error = $("#ask-error");
  const filters = useCatalogFilters
    ? discoveryFilters()
    : { ...DISCOVERY_DEFAULTS };
  const text = requestTextWithHints(baseText, filters);
  if (text.length > 2000) {
    setError(error, "The brief plus selected hints must stay within 2,000 characters.");
    $("#ask-input").focus();
    return;
  }
  setError(error);
  state.currentQuery = text;
  state.pendingPicks = [];
  state.nextCursor = null;
  showPickSkeletons();
  $("#request-progress").hidden = true;
  const turn = showInChat
    ? beginRecommendationTurn({ displayText, sentText: text, mode, filters })
    : null;
  state.discoveryInFlight = true;
  const generation = ++state.discoveryGeneration;
  const controller = new AbortController();
  state.discoveryController = controller;
  const otherTrigger = trigger === $("#ask-submit")
    ? $("#browse-current-events")
    : $("#ask-submit");
  otherTrigger.disabled = true;
  setButtonBusy(
    trigger,
    true,
    mode === "handle" ? "Saving the brief…" : "Finding current events…",
  );
  try {
    const payload = await api(mode === "handle" ? "/v1/requests" : "/v1/feed", {
      method: "POST",
      body: { text },
      signal: controller.signal,
    });
    if (generation !== state.discoveryGeneration || controller.signal.aborted) return;
    const feed = mode === "handle" ? payload.feed : payload;
    renderPicks(feed, null, { resultKind: showInChat ? "feed" : "catalog" });
    $("#picks-list").removeAttribute("aria-busy");
    if (turn) {
      completeRecommendationTurn(turn, {
        feed,
        mode,
        workflowStarted: Boolean(payload.workflow_started),
        rawText: baseText,
      });
    }
    if (mode === "handle") {
      $("#request-progress-copy").textContent = payload.workflow_started
        ? "The durable workflow is running. I’ll create a clear handoff whenever an event site needs you."
        : "Your request is saved and will continue automatically when the registration engine recovers.";
      $("#request-progress").hidden = false;
      showToast("Your brief is saved. Registration is never claimed until it is verified.");
      await loadRequests().catch((refreshError) => renderRecentRequests(refreshError));
      scheduleAutoRefresh();
      void checkReadiness();
    }
  } catch (requestError) {
    if (generation !== state.discoveryGeneration || controller.signal.aborted) return;
    $("#picks-list").removeAttribute("aria-busy");
    renderPicks(null, requestError, {
      resultKind: showInChat ? "feed" : "catalog",
    });
    setError(error, requestError.message);
    if (turn) {
      completeRecommendationTurn(turn, {
        feed: null,
        mode,
        rawText: baseText,
        error: requestError,
      });
    }
  } finally {
    if (generation !== state.discoveryGeneration) return;
    state.discoveryController = null;
    state.discoveryInFlight = false;
    setButtonBusy(trigger, false, "");
    otherTrigger.disabled = false;
    updateAskButton();
    flushCatalogScopeRefresh();
  }
}

async function submitBrief(event) {
  event.preventDefault();
  const input = $("#ask-input");
  const text = input.value.trim();
  if (!text) {
    setError($("#ask-error"), "Give me a sentence about what sounds good.");
    input.focus();
    return;
  }
  await runDiscovery({
    baseText: text,
    displayText: text,
    mode: $("input[name='ask-mode']:checked").value,
    trigger: $("#ask-submit"),
    useCatalogFilters: true,
  });
}

function catalogEventsPath({ sourceKey = null, cursor = null } = {}) {
  const query = new URLSearchParams({ limit: String(CATALOG_PAGE_SIZE) });
  if (sourceKey) query.set("source_key", sourceKey);
  if (cursor) query.set("cursor", cursor);
  return `/v1/catalog/events?${query}`;
}

function catalogEndpointUnavailable(error) {
  return [404, 405, 501].includes(error?.status);
}

function selectedCatalogSourceKey() {
  const filterKey = $("#filter-source").value;
  if (filterKey === DISCOVERY_DEFAULTS.source) return null;
  return state.catalogProviders.get(filterKey)?.sourceKey || null;
}

async function loadCatalogPages({ sourceKey = null, signal } = {}) {
  const items = [];
  const providers = [];
  const seenEvents = new Set();
  const seenCursors = new Set();
  let cursor = null;
  let pages = 0;
  do {
    const payload = await api(catalogEventsPath({ sourceKey, cursor }), { signal });
    if (!Array.isArray(payload?.items)) {
      throw new Error("The catalog returned an invalid event collection.");
    }
    providers.push(...(Array.isArray(payload.providers) ? payload.providers : []));
    for (const item of payload.items) {
      if (!item?.canonical_event_id || seenEvents.has(item.canonical_event_id)) continue;
      seenEvents.add(item.canonical_event_id);
      items.push(item);
      if (items.length >= CATALOG_PROVIDER_ITEM_CAP) break;
    }
    pages += 1;
    const nextCursor = typeof payload.next_cursor === "string" && payload.next_cursor
      ? payload.next_cursor
      : null;
    if (
      !sourceKey
      || !nextCursor
      || seenCursors.has(nextCursor)
      || pages >= CATALOG_PROVIDER_PAGE_CAP
      || items.length >= CATALOG_PROVIDER_ITEM_CAP
    ) {
      cursor = nextCursor;
      break;
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  } while (cursor);
  const capped = Boolean(
    sourceKey
    && cursor
    && (pages >= CATALOG_PROVIDER_PAGE_CAP || items.length >= CATALOG_PROVIDER_ITEM_CAP),
  );
  return {
    items,
    providers,
    next_cursor: sourceKey && !capped ? null : cursor,
    understood: null,
    capped,
  };
}

async function browseCurrentEvents(trigger = $("#browse-current-events")) {
  if (state.discoveryInFlight) return;
  const errorTarget = $("#ask-error");
  const filterKey = $("#filter-source").value;
  const sourceKey = selectedCatalogSourceKey();
  setError(errorTarget);
  showPickSkeletons();
  state.discoveryInFlight = true;
  const generation = ++state.discoveryGeneration;
  const controller = new AbortController();
  state.discoveryController = controller;
  const ask = $("#ask-submit");
  ask.disabled = true;
  setButtonBusy(
    trigger,
    true,
    sourceKey ? "Loading every current provider event…" : "Loading current events…",
  );
  try {
    let payload;
    let usedCatalogEndpoint = true;
    try {
      payload = await loadCatalogPages({ sourceKey, signal: controller.signal });
    } catch (catalogError) {
      if (!catalogEndpointUnavailable(catalogError)) throw catalogError;
      usedCatalogEndpoint = false;
      payload = await api("/v1/feed", {
        method: "POST",
        body: { text: "Show me current events" },
        signal: controller.signal,
      });
    }
    if (generation !== state.discoveryGeneration || controller.signal.aborted) return;
    if (usedCatalogEndpoint) {
      state.catalogEndpointAvailable = true;
      state.catalogLoadedSourceKey = sourceKey;
      state.catalogResultCapped = payload.capped;
      if (sourceKey && filterKey !== DISCOVERY_DEFAULTS.source) {
        payload.items = payload.items.map((item) => ({
          ...item,
          catalog_filter_key: filterKey,
        }));
      }
      renderPicks(payload, null, {
        resultKind: "catalog",
        resetProviders: true,
        resetCatalogProviders: !sourceKey,
      });
      if (payload.capped) {
        showToast(
          `Loaded the first ${CATALOG_PROVIDER_ITEM_CAP} events for this provider. Narrow the date or city to continue.`,
          "error",
        );
      }
    } else {
      state.catalogEndpointAvailable = false;
      state.catalogLoadedSourceKey = null;
      state.catalogResultCapped = false;
      renderPicks(payload, null, {
        resultKind: "catalog",
        resetProviders: true,
        resetCatalogProviders: true,
      });
    }
    $("#picks-list").removeAttribute("aria-busy");
  } catch (browseError) {
    if (generation !== state.discoveryGeneration || controller.signal.aborted) return;
    $("#picks-list").removeAttribute("aria-busy");
    renderPicks(null, browseError, { resultKind: "catalog" });
    setError(errorTarget, browseError.message);
  } finally {
    if (generation !== state.discoveryGeneration) return;
    state.discoveryController = null;
    state.discoveryInFlight = false;
    setButtonBusy(trigger, false, "");
    ask.disabled = false;
    updateAskButton();
    flushCatalogScopeRefresh();
  }
}

async function nextCatalogEvents() {
  if (!state.nextCursor || state.discoveryInFlight) return;
  const button = $("#next-picks");
  const browse = $("#browse-current-events");
  const previousCursor = state.nextCursor;
  const previousItems = [...state.currentFeedItems];
  state.discoveryInFlight = true;
  const generation = ++state.discoveryGeneration;
  const controller = new AbortController();
  state.discoveryController = controller;
  browse.disabled = true;
  setButtonBusy(button, true, "Loading…");
  try {
    const payload = await api(
      catalogEventsPath({
        sourceKey: state.catalogLoadedSourceKey,
        cursor: state.nextCursor,
      }),
      { signal: controller.signal },
    );
    if (!Array.isArray(payload?.items)) {
      throw new Error("The catalog returned an invalid event collection.");
    }
    if (generation !== state.discoveryGeneration || controller.signal.aborted) return;
    rememberCatalogProviders(payload.providers);
    const seen = new Set(previousItems.map((item) => item.canonical_event_id));
    const newItems = payload.items.filter(
      (item) => item?.canonical_event_id && !seen.has(item.canonical_event_id),
    );
    state.currentFeedItems = [...previousItems, ...newItems];
    state.currentRawResultCount = state.currentFeedItems.length;
    state.nextCursor = typeof payload.next_cursor === "string" && payload.next_cursor
      ? payload.next_cursor
      : null;
    populateFacetFilters(state.currentFeedItems);
    const filters = discoveryFilters();
    state.pendingPicks = sortDiscoveryItems(
      newItems.filter(
        (item) => matchesDiscovery(item, filters) && matchesCatalogSearch(item),
      ),
      filters.sort,
    );
    state.currentMatchCount = state.currentFeedItems.filter(
      (item) => matchesDiscovery(item, filters) && matchesCatalogSearch(item),
    ).length;
    renderPickBatch();
  } catch (error) {
    if (generation !== state.discoveryGeneration || controller.signal.aborted) return;
    state.nextCursor = previousCursor;
    showToast(`Couldn’t load more catalog events: ${error.message}`, "error");
  } finally {
    if (generation !== state.discoveryGeneration) return;
    state.discoveryController = null;
    state.discoveryInFlight = false;
    setButtonBusy(button, false, "");
    browse.disabled = false;
    updateAskButton();
    flushCatalogScopeRefresh();
  }
}

async function nextPicks() {
  if (
    state.discoveryInFlight
    || (state.currentResultKind !== "catalog" && !state.currentQuery)
  ) return;
  if (state.pendingPicks.length) {
    renderPickBatch();
    $("#picks-title").focus();
    if (!hasMorePicks()) {
      const action = $(".thread-more-results:not([data-stale='true'])");
      if (action) {
        action.disabled = true;
        action.textContent = "No more in this result set";
      }
    }
    return;
  }
  if (!state.nextCursor) return;
  if (state.currentResultKind === "catalog" && state.catalogEndpointAvailable) {
    await nextCatalogEvents();
    return;
  }
  const button = $("#next-picks");
  const ask = $("#ask-submit");
  const browse = $("#browse-current-events");
  const currentThreadAction = $(".thread-more-results:not([data-stale='true'])");
  const previousItems = [...state.currentFeedItems];
  const previousCursor = state.nextCursor;
  const previousUnderstood = state.currentUnderstood;
  state.discoveryInFlight = true;
  const generation = ++state.discoveryGeneration;
  const controller = new AbortController();
  state.discoveryController = controller;
  ask.disabled = true;
  browse.disabled = true;
  if (currentThreadAction) currentThreadAction.disabled = true;
  setButtonBusy(button, true, "Looking…");
  showPickSkeletons();
  try {
    const feed = await api("/v1/feed", {
      method: "POST",
      body: { text: state.currentQuery, cursor: state.nextCursor },
      signal: controller.signal,
    });
    if (generation !== state.discoveryGeneration || controller.signal.aborted) return;
    const seen = new Set(previousItems.map((item) => item.canonical_event_id));
    const appended = [
      ...previousItems,
      ...(feed.items || []).filter((item) => !seen.has(item.canonical_event_id)),
    ];
    renderPicks(
      { ...feed, items: appended },
      null,
      { resetProviders: false },
    );
    $("#picks-title").focus();
  } catch (error) {
    if (generation !== state.discoveryGeneration || controller.signal.aborted) return;
    renderPicks(
      {
        items: previousItems,
        next_cursor: previousCursor,
        understood: previousUnderstood,
      },
      null,
      { resetProviders: false },
    );
    showToast(`Couldn’t load more events: ${error.message}`, "error");
  } finally {
    if (generation !== state.discoveryGeneration) return;
    state.discoveryController = null;
    state.discoveryInFlight = false;
    $("#picks-list").removeAttribute("aria-busy");
    setButtonBusy(button, false, "");
    browse.disabled = false;
    updateAskButton();
    flushCatalogScopeRefresh();
    if (currentThreadAction) {
      currentThreadAction.disabled = !hasMorePicks();
      if (!hasMorePicks()) currentThreadAction.textContent = "No more in this result set";
    }
  }
}

function refreshDiscoveryView() {
  renderActiveResultFilters();
  if (state.hasFeedResponse) applyDiscoveryToCurrentFeed();
  updateAskButton();
}

function refreshCatalogSearch() {
  state.catalogFilterMenuDismissed = false;
  if (state.catalogFilterEditKind) {
    renderCatalogFilterSuggestions({ force: true });
    return;
  }
  const draft = $("#catalog-search").value.trim();
  if (!draft && state.catalogSearch) {
    state.catalogSearch = "";
    if (state.workspaceMode === "calendar") {
      state.calendarMonth = null;
      state.calendarSelectedDateKey = null;
    }
    refreshDiscoveryView();
  }
  // Treat typed text as a draft while the palette can recognize semantic filters.
  // A literal search is committed only through its explicit Search suggestion or Enter.
  renderCatalogFilterSuggestions({ force: true });
}

function resetDiscoveryFilters() {
  state.catalogFilterEditKind = null;
  state.catalogFilterEditDraft = "";
  removeDateRangeFilterOption();
  $("#filter-when").value = DISCOVERY_DEFAULTS.when;
  $("#filter-price").value = DISCOVERY_DEFAULTS.price;
  $("#filter-city").value = DISCOVERY_DEFAULTS.city;
  $("#filter-source").value = DISCOVERY_DEFAULTS.source;
  $("#filter-radius").value = DISCOVERY_DEFAULTS.radius;
  $("#filter-sort").value = DISCOVERY_DEFAULTS.sort;
  state.catalogScopeRefreshPending = true;
  state.mapFilterCenter = state.eventMap
    ? [state.eventMap.getCenter().lat, state.eventMap.getCenter().lng]
    : null;
  state.catalogSearch = "";
  state.calendarMonth = null;
  state.calendarSelectedDateKey = null;
  $("#catalog-search").value = "";
  $("#catalog-search").placeholder = catalogFilterSearchPlaceholder();
  $("#catalog-date-start").value = "";
  $("#catalog-date-end").value = "";
  refreshDiscoveryView();
  void refreshCatalogProviderScope();
  closeCatalogFilterMenu({ returnFocus: true, dismiss: true });
  announceCatalogFilter("All event filters cleared.");
}

function updateAskButton() {
  const mode = $("input[name='ask-mode']:checked").value;
  const label = $("#ask-submit span:first-child");
  label.textContent = mode === "handle"
    ? "Save and take action"
    : "Send";
  $("#ask-submit").disabled = state.discoveryInFlight;
  $("#ask-hint").textContent = mode === "handle"
    ? "This saves a durable brief and may take action in permitted lanes."
    : "Preview is read only. Every chat message is a standalone catalog search.";
  $$(".mode-option").forEach((option) => {
    option.classList.toggle("selected", Boolean($("input", option)?.checked));
  });
}

function lifecycleLabel(value) {
  return {
    found: "Finding a safe route",
    handoff: "Needs your tap",
    awaiting_confirmation: "Checking registration",
    registered: "Registration confirmed",
    scheduled: "On your calendar",
    reconciled: "Confirmed",
    withdrawing: "Leaving event",
    completed: "Completed",
    cancelled: "Cancelled",
    expired: "Expired",
    failed_no_candidate: "No match",
  }[value] || humanize(value);
}

function agendaGroup(value) {
  const date = validDate(value);
  if (!date) return "Later";
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const startOfDate = new Date(date);
  startOfDate.setHours(0, 0, 0, 0);
  const days = Math.round((startOfDate - startOfToday) / 86_400_000);
  if (days < 0) return "Recently";
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days >= 0 && days < 7) return "This week";
  return "Later";
}

function renderPlans(error = null) {
  const container = $("#plans-list");
  const count = state.registrations.filter((item) => !["cancelled", "expired", "completed"].includes(item.state)).length;
  $("#plans-count").textContent = state.registrationsCapped ? `${count}+` : String(count);
  $("#plans-count").hidden = count === 0;
  const summary = $("#plans-summary");
  const confirmed = state.registrations.filter((item) => ["scheduled", "reconciled"].includes(item.state)).length;
  const inProgress = state.registrations.filter((item) => ["found", "registered", "awaiting_confirmation", "withdrawing"].includes(item.state)).length;
  const needsTap = state.registrations.filter((item) => item.state === "handoff").length;
  summary.replaceChildren(
    metric(confirmed, "Confirmed plans"),
    metric(inProgress, "In progress"),
    metric(needsTap, "Need your tap"),
  );
  if (error) {
    renderEmpty(container, "!", "Plans are temporarily unavailable", error.message);
    return;
  }
  if (!state.registrations.length) {
    renderEmpty(
      container,
      "◌",
      "No upcoming plans yet",
      "When a durable request reaches a registration lifecycle, it will appear here.",
    );
    return;
  }
  const groups = new Map();
  for (const registration of state.registrations) {
    const label = agendaGroup(registration.start_at);
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(registration);
  }
  const fragment = document.createDocumentFragment();
  for (const label of ["Recently", "Today", "Tomorrow", "This week", "Later"]) {
    const items = groups.get(label);
    if (!items?.length) continue;
    const group = node("section", "agenda-group");
    const heading = node("h2", "agenda-group-label", label);
    const list = node("div", "agenda-group-items");
    for (const item of items) list.append(planCard(item));
    append(group, heading, list);
    fragment.append(group);
  }
  if (state.registrationsCapped) {
    fragment.append(
      collectionLimitNotice(`Showing the first ${COLLECTION_ITEM_CAP} plans, ordered by event time.`),
    );
  }
  container.replaceChildren(fragment);
}

function metric(value, label) {
  const item = node("div", "metric");
  append(item, node("strong", "", value), node("span", "", label));
  return item;
}

function collectionLimitNotice(copy) {
  return node("p", "collection-limit-notice", copy);
}

function planCard(item) {
  const card = node("article", "agenda-card");
  const copy = node("div", "agenda-copy");
  const title = node("h3", "", item.title);
  const meta = node("p", "agenda-meta");
  append(
    meta,
    node("span", "", eventDateTime(item.start_at)),
    node("span", "", [item.venue_name, item.city].filter(Boolean).join(" · ") || "Venue to be confirmed"),
    node("span", "", priceLabel(item.price_status)),
  );
  append(copy, title, meta);
  const stateBox = node("div", "agenda-state");
  const status = node("span", `state-pill ${item.state}`, lifecycleLabel(item.state));
  stateBox.append(status);
  const actions = node("div", "agenda-actions");
  const visit = externalLink(item.registration_url, "Event site", "button button-secondary");
  if (visit) actions.append(visit);
  if (item.can_withdraw) {
    const leave = node("button", "button button-quiet-danger", "Leave event");
    leave.type = "button";
    leave.addEventListener("click", () => void withdrawPlan(item, leave));
    actions.append(leave);
  }
  if (actions.childElementCount) stateBox.append(actions);
  if (item.conflict_warning || item.event_status !== "scheduled") {
    const warning = item.event_status === "cancelled"
      ? "Organizer cancelled"
      : item.event_status === "rescheduled"
        ? "Organizer changed the time"
        : "Calendar conflict warning";
    stateBox.append(node("span", "tag tag-warning", warning));
  }
  append(card, dateTile(item.start_at, "agenda-date"), copy, stateBox);
  return card;
}

async function withdrawPlan(item, button) {
  const confirmed = window.confirm(
    `Leave “${item.title}”? The concierge will ask the source to withdraw and then reconcile your calendar.`,
  );
  if (!confirmed) return;
  const sessionGeneration = state.sessionGeneration;
  setButtonBusy(button, true, "Requesting…");
  try {
    await api("/v1/unrsvp", {
      method: "POST",
      body: { canonical_event_id: item.canonical_event_id, request_id: crypto.randomUUID() },
    });
    if (sessionGeneration !== state.sessionGeneration) return;
    item.state = "withdrawing";
    item.can_withdraw = false;
    renderPlans();
    showToast("Withdrawal accepted. I’ll reconcile the source and calendar.");
  } catch (error) {
    if (sessionGeneration !== state.sessionGeneration) return;
    showToast(`Your plan was not changed: ${error.message}`, "error");
    setButtonBusy(button, false, "");
  }
}

function taskReason(value) {
  return {
    browser_fail: "The event site needs you",
    captcha: "A quick human check",
    identity_wall: "Sign-in required",
    approval_gated: "Membership approval",
    dues: "Payment or dues required",
    deferred_register: "Final registration step",
    saturation: "Automation was at capacity",
    unexpected_paywall: "Unexpected payment step",
    no_autonomous_lane: "This source requires you",
    calendar_write_failed: "Calendar needs attention",
    withdrawal_required: "Withdrawal needs you",
  }[value] || humanize(value);
}

function renderTasks(error = null) {
  const container = $("#tasks-list");
  const actionable = state.tasks.filter((item) => ["open", "notified"].includes(item.state)).length;
  for (const target of [$("#tasks-count"), $("#mobile-task-count"), $("#topbar-task-count")]) {
    target.textContent = state.tasksCapped ? `${actionable}+` : String(actionable);
    target.hidden = actionable === 0;
  }
  if (error) {
    renderEmpty(container, "!", "To-dos are temporarily unavailable", error.message);
    return;
  }
  if (!state.tasks.length) {
    renderEmpty(container, "✓", "Nothing needs you right now", "I’ll only ask for a tap when an event source requires one.");
    return;
  }
  const fragment = document.createDocumentFragment();
  state.tasks.forEach((item, index) => fragment.append(taskCard(item, index)));
  if (state.tasksCapped) {
    fragment.append(
      collectionLimitNotice(
        `Showing the first ${COLLECTION_ITEM_CAP} actionable to-dos, ordered by urgency.`,
      ),
    );
  }
  container.replaceChildren(fragment);
}

function taskCard(item, index) {
  const card = node("article", "task-card");
  const number = node("span", "task-number", String(index + 1).padStart(2, "0"));
  number.setAttribute("aria-hidden", "true");
  const copy = node("div", "task-copy");
  append(
    copy,
    node("p", "task-reason", taskReason(item.reason)),
    node("h3", "", item.title || item.event_summary),
  );
  const meta = node("p", "task-meta");
  append(
    meta,
    node("span", "", eventDateTime(item.start_at)),
    node("span", "", [item.venue_name, item.city].filter(Boolean).join(" · ") || "Event site"),
  );
  copy.append(meta);

  const actions = node("div", "task-actions");
  const visit = externalLink(item.deep_link, "Open event site ↗", "button button-primary");
  if (visit) actions.append(visit);
  const verifying = state.verifyingTasks.has(item.task_id);
  const done = node("button", "button button-secondary", verifying ? "Verification requested" : "I finished registration");
  done.type = "button";
  done.disabled = verifying;
  done.addEventListener("click", () => openCompletionDialog(item));
  actions.append(done);
  const expiry = node(
    "p",
    "task-expiry",
    verifying ? "Checking the source before updating your calendar" : expiryLabel(item.expires_at),
  );
  append(actions, expiry);
  append(card, number, copy, actions);
  return card;
}

function openCompletionDialog(item) {
  state.currentTaskId = item.task_id;
  $("#completion-copy").textContent = `I’ll verify “${item.title || item.event_summary}” at the source before changing your calendar.`;
  $("#completion-dialog").showModal();
}

async function completeCurrentTask() {
  const taskId = state.currentTaskId;
  state.currentTaskId = null;
  if (!taskId) return;
  const sessionGeneration = state.sessionGeneration;
  state.verifyingTasks.add(taskId);
  renderTasks();
  $("#tasks-title").focus({ preventScroll: true });
  try {
    const result = await api(`/v1/me/tasks/${encodeURIComponent(taskId)}/done`, { method: "POST" });
    if (sessionGeneration !== state.sessionGeneration) return;
    showToast(
      result.status === "already_accepted"
        ? "Verification was already requested. I’ll update the plan after the source check."
        : "Verification started. I’ll update your plan only after the source check passes.",
    );
  } catch (error) {
    if (sessionGeneration !== state.sessionGeneration) return;
    state.verifyingTasks.delete(taskId);
    renderTasks();
    showToast(`Verification was not started: ${error.message}`, "error");
  }
}

async function savePreferences() {
  const button = $("#save-preferences");
  const errorTarget = $("#preferences-error");
  const sessionGeneration = state.sessionGeneration;
  const me = state.me;
  if (!me) return;
  const interests = $$("#settings-interests [data-interest][aria-pressed='true']").map(
    (item) => item.dataset.interest,
  );
  const revision = Math.max(Date.now(), Number(me.preference_revision || 0) + 1);
  setError(errorTarget);
  setButtonBusy(button, true, "Saving…");
  try {
    const result = await api("/v1/preferences", {
      method: "PUT",
      body: { interests, revision },
    });
    if (sessionGeneration !== state.sessionGeneration || state.me !== me) return;
    me.interests = result.interests;
    me.preference_revision = result.revision;
    syncIdentity();
    showToast("Your taste signals are saved.");
  } catch (error) {
    if (sessionGeneration !== state.sessionGeneration || state.me !== me) return;
    setError(errorTarget, error.message);
    if (error.status === 409) {
      try {
        const refreshed = await api("/v1/me");
        if (sessionGeneration !== state.sessionGeneration || state.me !== me) return;
        state.me = refreshed;
        syncIdentity();
      } catch (_refreshError) {
        // Keep the actionable conflict message visible if the refresh also fails.
      }
    }
  } finally {
    if (sessionGeneration === state.sessionGeneration && state.me === me) {
      setButtonBusy(button, false, "");
    }
  }
}

async function onboard(event) {
  event.preventDefault();
  if (!state.config.local_demo) {
    if (state.config.auth_start_url) window.location.assign(state.config.auth_start_url);
    return;
  }
  const email = $("#onboarding-email");
  if (!email.reportValidity()) return;
  const button = $("#onboarding-submit");
  const errorTarget = $("#welcome-error");
  const interests = $$("#onboarding-interests [data-interest][aria-pressed='true']").map(
    (item) => item.dataset.interest,
  );
  setError(errorTarget);
  setButtonBusy(button, true, "Preparing your concierge…");
  try {
    const account = await api("/v1/onboard", {
      method: "POST",
      body: { notify_email: email.value.trim() },
    });
    state.tenantId = account.tenant_id;
    const persisted = writeLocalSession(state.tenantId);
    if (interests.length) {
      try {
        await api("/v1/preferences", {
          method: "PUT",
          body: { interests, revision: Date.now() },
        });
      } catch (_preferenceError) {
        // Account creation is already durable; preferences can be retried from Settings.
      }
    }
    await enterProduct(await api("/v1/me"));
    $("#main-content").focus({ preventScroll: true });
    showToast(
      persisted
        ? "Your local concierge is ready."
        : "Your concierge is ready for this tab; private browsing prevented session storage.",
    );
  } catch (error) {
    if (state.tenantId && [401, 404].includes(error.status)) clearLocalSession();
    setError(errorTarget, error.message);
  } finally {
    setButtonBusy(button, false, "");
  }
}

async function signOut() {
  const button = $("#sign-out");
  setButtonBusy(button, true, "Signing out…");
  try {
    if (state.config.local_demo) {
      clearLocalSession();
    } else if (state.config.logout_url) {
      await api(state.config.logout_url, { method: "POST" });
      stopAutoRefresh();
      invalidateSessionLoads();
      state.me = null;
      clearPrivateClientState();
    }
    showWelcome();
    $("#welcome-content").focus({ preventScroll: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setButtonBusy(button, false, "");
  }
}

function resetErasureDialog() {
  const input = $("#account-erasure-confirmation");
  input.value = "";
  $("#account-erasure-confirm").disabled = true;
  $("#account-erasure-reauth").hidden = true;
  setError($("#account-erasure-error"));
}

function openAccountErasureDialog() {
  resetErasureDialog();
  erasureRequestId();
  $("#account-erasure-dialog").showModal();
  $("#account-erasure-confirmation").focus();
}

function closeAccountErasureDialog() {
  $("#account-erasure-dialog").close();
  resetErasureDialog();
  $("#open-erasure-dialog").focus({ preventScroll: true });
}

function showErasureAccepted() {
  stopAutoRefresh();
  clearLocalSession();
  clearErasureRequestId();
  if ($("#account-erasure-dialog").open) $("#account-erasure-dialog").close();
  window.history.replaceState({}, "", "/app#account-erasure");
  setScreen("erasure");
  $("#erasure-accepted-title").focus({ preventScroll: true });
}

async function beginAccountErasure(event) {
  event.preventDefault();
  const input = $("#account-erasure-confirmation");
  const button = $("#account-erasure-confirm");
  const errorTarget = $("#account-erasure-error");
  if (input.value !== ERASURE_CONFIRMATION) {
    setError(errorTarget, `Type ${ERASURE_CONFIRMATION} exactly to continue.`);
    input.focus();
    return;
  }
  setError(errorTarget);
  $("#account-erasure-reauth").hidden = true;
  setButtonBusy(button, true, "Accepting erasure…");
  try {
    await api("/v1/me/erasure-requests", {
      method: "POST",
      body: {
        request_id: erasureRequestId(),
        confirmation: ERASURE_CONFIRMATION,
      },
    });
    showErasureAccepted();
  } catch (error) {
    if (error.status === 428 && state.config?.reauth_url) {
      setError(errorTarget, "Sign in again, then return here and repeat the exact confirmation.");
      $("#account-erasure-reauth").hidden = false;
      $("#account-erasure-reauth").focus();
    } else {
      setError(errorTarget, error.message);
    }
  } finally {
    setButtonBusy(button, false, "");
  }
}

async function startAccountErasureReauthentication() {
  const button = $("#account-erasure-reauth");
  const errorTarget = $("#account-erasure-error");
  setButtonBusy(button, true, "Opening secure sign-in…");
  try {
    const result = await api(state.config.reauth_url, {
      method: "POST",
      body: { return_to: "/app#/settings" },
    });
    if (typeof result?.authorization_url !== "string") {
      throw new Error("Secure sign-in returned an invalid destination.");
    }
    window.location.assign(result.authorization_url);
  } catch (error) {
    setError(errorTarget, error.message);
    setButtonBusy(button, false, "");
  }
}

function bindEvents() {
  $("#onboarding-form").addEventListener("submit", onboard);
  $("#ask-form").addEventListener("submit", submitBrief);
  $("#browse-current-events").addEventListener("click", () => void browseCurrentEvents());
  $("#refresh-map-events").addEventListener(
    "click",
    () => void browseCurrentEvents($("#refresh-map-events")),
  );
  $("#calendar-prev-month").addEventListener("click", () => shiftCalendarMonth(-1));
  $("#calendar-next-month").addEventListener("click", () => shiftCalendarMonth(1));
  $("#calendar-today").addEventListener("click", () => {
    const today = new Date();
    state.calendarMonth = firstDayOfMonth(today);
    state.calendarSelectedDateKey = localDateKey(today);
    applyDiscoveryToCurrentFeed();
    $("#calendar-agenda-heading").focus({ preventScroll: true });
  });
  const startNewChat = () => {
    resetRecommendationSession();
    navigate("concierge");
    $("#ask-input").focus();
  };
  $("#new-chat").addEventListener("click", startNewChat);
  $(".mobile-new-chat").addEventListener("click", startNewChat);
  $$("[data-workspace-view]").forEach((button) => {
    button.addEventListener("click", () => {
      const workspace = button.dataset.workspaceView;
      navigate(workspace === "chat" ? "concierge" : workspace);
    });
  });
  $("#clear-discovery-filters").addEventListener("click", resetDiscoveryFilters);
  $$("[data-remove-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      const name = button.dataset.removeFilter;
      setEventFilter(name, DISCOVERY_DEFAULTS[name]);
    });
  });
  $('[data-quick-filter="weekend"]').addEventListener("click", () => {
    const active = $("#filter-when").value === "weekend";
    setEventFilter("when", active ? DISCOVERY_DEFAULTS.when : "weekend");
  });
  $('[data-quick-filter="free"]').addEventListener("click", () => {
    const active = $("#filter-price").value === "free";
    setEventFilter("price", active ? DISCOVERY_DEFAULTS.price : "free");
  });
  $("#map-search-area").addEventListener("click", () => {
    if (!state.eventMap) return;
    const center = state.eventMap.getCenter();
    state.mapFilterCenter = [center.lat, center.lng];
    $("#map-search-area").hidden = true;
    refreshDiscoveryView();
  });
  $("#next-picks").addEventListener("click", nextPicks);
  $("#save-preferences").addEventListener("click", savePreferences);
  $("#refresh-activity").addEventListener("click", () => {
    void loadRequests()
      .catch((error) => renderRecentRequests(error))
      .finally(scheduleAutoRefresh);
  });
  $("#refresh-plans").addEventListener("click", () => void loadRegistrations().catch((error) => renderPlans(error)));
  $("#refresh-tasks").addEventListener("click", () => void loadTasks().catch((error) => renderTasks(error)));
  $("#toast-close").addEventListener("click", () => {
    $("#toast").hidden = true;
    window.clearTimeout(state.toastTimer);
  });
  $("#sign-out").addEventListener("click", () => void signOut());
  $("#open-erasure-dialog").addEventListener("click", openAccountErasureDialog);
  $("#account-erasure-close").addEventListener("click", closeAccountErasureDialog);
  $("#account-erasure-cancel").addEventListener("click", closeAccountErasureDialog);
  $("#account-erasure-form").addEventListener("submit", (event) => void beginAccountErasure(event));
  $("#account-erasure-reauth").addEventListener("click", () => void startAccountErasureReauthentication());
  $("#account-erasure-confirmation").addEventListener("input", (event) => {
    $("#account-erasure-confirm").disabled = event.target.value !== ERASURE_CONFIRMATION;
    $("#account-erasure-reauth").hidden = true;
    setError($("#account-erasure-error"));
  });
  $$("[data-view]").forEach((button) => {
    button.addEventListener("click", () => {
      const view = button.dataset.view;
      navigate(view);
      if (view === "plans") {
        void loadRegistrations().catch((error) => renderPlans(error));
      } else if (view === "tasks") {
        void loadTasks().catch((error) => renderTasks(error));
      } else if (view === "concierge") {
        void loadRequests()
          .catch((error) => renderRecentRequests(error))
          .finally(scheduleAutoRefresh);
      }
    });
  });
  $$("[data-interest]").forEach((button) => {
    button.addEventListener("click", () => {
      const pressed = button.getAttribute("aria-pressed") === "true";
      button.setAttribute("aria-pressed", String(!pressed));
    });
  });
  $$("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      $("#ask-input").value = button.dataset.prompt;
      $("#ask-input").focus();
    });
  });
  for (const selector of [
    "#filter-when",
    "#filter-city",
    "#filter-price",
    "#filter-source",
    "#filter-radius",
    "#filter-sort",
  ]) {
    $(selector).addEventListener("change", () => {
      closeCatalogFilterMenu();
      if (
        selector === "#filter-when"
        && !$("#filter-when").value.startsWith("range:")
      ) {
        removeDateRangeFilterOption();
        $("#catalog-date-start").value = "";
        $("#catalog-date-end").value = "";
      }
      if (
        selector === "#filter-radius"
        && $("#filter-radius").value !== DISCOVERY_DEFAULTS.radius
        && state.eventMap
      ) {
        const center = state.eventMap.getCenter();
        state.mapFilterCenter = [center.lat, center.lng];
      }
      refreshDiscoveryView();
      if (selector === "#filter-source") void refreshCatalogProviderScope();
    });
  }
  $("#catalog-search").addEventListener("input", refreshCatalogSearch);
  $("#catalog-search").addEventListener("focus", () => {
    state.catalogFilterMenuDismissed = false;
    renderCatalogFilterSuggestions();
  });
  $("#catalog-search").addEventListener("click", () => {
    state.catalogFilterMenuDismissed = false;
    renderCatalogFilterSuggestions();
  });
  $("#catalog-search").addEventListener("keydown", handleCatalogFilterSuggestionKeydown);
  for (const input of [$("#catalog-date-start"), $("#catalog-date-end")]) {
    input.addEventListener("change", () => {
      const start = $("#catalog-date-start").value;
      $("#catalog-date-end").min = start || localDateKey(new Date());
      if ($("#catalog-date-end").value && $("#catalog-date-end").value < start) {
        $("#catalog-date-end").value = start;
      }
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        $("#catalog-date-apply").click();
      }
    });
  }
  $("#catalog-date-range-row").addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeCatalogFilterMenu({ returnFocus: true, dismiss: true });
    }
  });
  $$("[data-date-preset]").forEach((button) => {
    button.addEventListener("click", () => {
      const [start, end] = datePresetRange(button.dataset.datePreset);
      $("#catalog-date-start").value = localDateKey(start);
      $("#catalog-date-end").value = localDateKey(end);
      $("#catalog-date-end").min = localDateKey(start);
    });
  });
  $("#catalog-date-apply").addEventListener("click", () => {
    const start = $("#catalog-date-start").value;
    const end = $("#catalog-date-end").value;
    if (!start || !end || start > end) {
      announceCatalogFilter("Choose a valid start and end date.");
      $("#catalog-date-start").focus();
      return;
    }
    applyDateRangeFilter(start, end);
  });
  $("#catalog-date-cancel").addEventListener("click", () => {
    closeCatalogFilterMenu({ returnFocus: true, dismiss: true });
  });
  document.addEventListener("focusin", (event) => {
    if (!$(".catalog-search-shell").contains(event.target)) {
      closeCatalogFilterMenu({ dismiss: true });
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!$(".catalog-search-shell").contains(event.target)) {
      closeCatalogFilterMenu({ dismiss: true });
    }
  });
  $("#ask-input").addEventListener("input", (event) => {
    event.target.style.height = "auto";
    event.target.style.height = `${Math.min(event.target.scrollHeight, 160)}px`;
  });
  $("#ask-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("#ask-form").requestSubmit();
    }
  });
  $$("input[name='ask-mode']").forEach((radio) => radio.addEventListener("change", updateAskButton));
  $("#completion-dialog").addEventListener("close", (event) => {
    if (event.target.returnValue === "confirm") void completeCurrentTask();
    else state.currentTaskId = null;
  });
  $("#account-erasure-dialog").addEventListener("close", () => {
    resetErasureDialog();
    if ($("#erasure-view").hidden) {
      $("#open-erasure-dialog").focus({ preventScroll: true });
    }
  });
  window.addEventListener("hashchange", () => {
    const view = window.location.hash.replace(/^#\/?/, "");
    if (["concierge", "catalog", "map", "calendar", "plans", "tasks", "settings"].includes(view)) {
      navigate(view, false);
    }
  });
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const editing = target instanceof HTMLInputElement
      || target instanceof HTMLTextAreaElement
      || target instanceof HTMLSelectElement
      || target?.isContentEditable;
    if (
      event.key === "/"
      && ["chat", "catalog", "map", "calendar"].includes(state.workspaceMode)
      && !editing
    ) {
      event.preventDefault();
      $("#catalog-search").focus();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") {
      stopAutoRefresh();
      return;
    }
    if (state.me) {
      void refreshPendingRequestOutcome().finally(scheduleAutoRefresh);
    }
  });
}

bindEvents();
updateAskButton();
void boot();
