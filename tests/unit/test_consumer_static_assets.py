"""Focused contracts for consumer assets and the scanner-safe handoff page."""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree

from events_concierge.api.app import _completion_page

_STATIC_DIR = Path(__file__).parents[2] / "src" / "events_concierge" / "api" / "static"


def test_consumer_mark_and_manifest_are_self_contained() -> None:
    mark = _STATIC_DIR / "mark.svg"
    manifest = json.loads((_STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))

    root = ElementTree.parse(mark).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert manifest["name"] == "Events Concierge"
    assert manifest["start_url"] == "/app"
    assert manifest["theme_color"] == "#f4f6f7"
    assert manifest["icons"] == [
        {
            "src": "/assets/mark.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any maskable",
        }
    ]


def test_active_handoff_page_is_styled_but_remains_an_inert_get() -> None:
    response = _completion_page("Confirm completed registration", show_form=True)
    body = response.body.decode("utf-8")

    assert 'href="/assets/app.css"' in body
    assert 'src="/assets/mark.svg"' in body
    assert '<form method="post" class="onboarding-form"' in body
    assert "Mark registration done" in body
    assert "<script" not in body
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )


def test_used_handoff_page_has_no_resubmission_form() -> None:
    response = _completion_page("Completion already submitted", show_form=False)
    body = response.body.decode("utf-8")

    assert "Completion already submitted" in body
    assert "<form" not in body
    assert 'href="/app"' in body


def test_consumer_app_restores_focus_and_always_clears_pick_busy_state() -> None:
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

    onboard = app.split("async function onboard(event)", maxsplit=1)[1].split(
        "function bindEvents()", maxsplit=1
    )[0]
    bind_events = app.split("function bindEvents()", maxsplit=1)[1]
    assert '$("#main-content").focus({ preventScroll: true });' in onboard
    sign_out = app.split("async function signOut()", maxsplit=1)[1].split(
        "function bindEvents()", maxsplit=1
    )[0]
    assert '$("#welcome-content").focus({ preventScroll: true });' in sign_out
    assert '$("#sign-out").addEventListener("click", () => void signOut());' in bind_events

    navigate = app.split("function navigate(view, focus = true)", maxsplit=1)[1].split(
        "function stopAutoRefresh()", maxsplit=1
    )[0]
    assert 'matchMedia?.("(prefers-reduced-motion: reduce)").matches' in navigate
    assert 'behavior: reduceMotion ? "auto" : "smooth"' in navigate

    next_picks = app.split("async function nextPicks()", maxsplit=1)[1].split(
        "function updateAskButton()", maxsplit=1
    )[0]
    finally_block = next_picks.split("} finally {", maxsplit=1)[1]
    assert '$("#picks-list").removeAttribute("aria-busy");' in finally_block

    run_discovery = app.split("async function runDiscovery({", maxsplit=1)[1].split(
        "async function submitBrief(event)", maxsplit=1
    )[0]
    assert (
        "await loadRequests().catch((refreshError) => renderRecentRequests(refreshError));"
        in run_discovery
    )

    calendar = app.split("function renderEventCalendar(items)", maxsplit=1)[1].split(
        "function shiftCalendarMonth", maxsplit=1
    )[0]
    assert '$("#calendar-agenda-heading").focus({ preventScroll: true });' in calendar

    completion = app.split("async function completeCurrentTask()", maxsplit=1)[1].split(
        "async function savePreferences()", maxsplit=1
    )[0]
    assert '$("#tasks-title").focus({ preventScroll: true });' in completion
    assert '<h1 id="tasks-title" tabindex="-1">' in html


def test_consumer_auto_refresh_is_pending_only_jittered_and_race_safe() -> None:
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "const AUTO_REFRESH_BASE_MS = 30_000;" in app
    assert "const AUTO_REFRESH_MAX_MS = 5 * 60_000;" in app
    assert "const AUTO_REFRESH_WINDOW_MS = 6 * 60 * 60_000;" in app
    scheduler = app.split("function scheduleAutoRefresh()", maxsplit=1)[1].split(
        "function startAutoRefresh()", maxsplit=1
    )[0]
    assert 'document.visibilityState !== "visible"' in scheduler
    assert "!hasPendingRequestOutcome()" in scheduler
    assert "window.setTimeout" in scheduler
    assert "scheduleAutoRefresh();" in scheduler
    assert "await refreshPendingRequestOutcome();" in scheduler
    assert "refreshAll" not in scheduler

    delay = app.split("function autoRefreshDelay()", maxsplit=1)[1].split(
        "function scheduleAutoRefresh()", maxsplit=1
    )[0]
    assert "AUTO_REFRESH_MAX_MS" in delay
    assert "state.autoRefreshFailures" in delay
    assert "Math.random()" in delay

    pending = app.split("async function refreshPendingRequestOutcome()", maxsplit=1)[1].split(
        "async function refreshAll", maxsplit=1
    )[0]
    assert "const applied = await loadRequests();" in pending
    assert "Promise.allSettled([loadRegistrations(), loadTasks()])" in pending
    assert "state.autoRefreshFailures += 1;" in pending

    refresh = app.split("async function refreshAll({ showErrors = true } = {})", maxsplit=1)[
        1
    ].split("async function loadRequests()", maxsplit=1)[0]
    assert "if (state.refreshInFlight) return state.refreshInFlight;" in refresh
    assert "Promise.allSettled" in refresh
    assert "if (showErrors)" in refresh
    assert "state.refreshInFlight = null;" in refresh

    visibility = app.split('document.addEventListener("visibilitychange"', maxsplit=1)[1]
    assert "stopAutoRefresh();" in visibility
    assert "void refreshPendingRequestOutcome().finally(scheduleAutoRefresh);" in visibility

    for resource in ("request", "registration", "task"):
        assert f"++state.{resource}LoadGeneration" in app
        assert f"generation !== state.{resource}LoadGeneration" in app


def test_recent_requests_render_explicit_outcomes_without_guessing() -> None:
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")

    recent = app.split("function renderRecentRequests(error = null)", maxsplit=1)[1].split(
        "function validDate(value)", maxsplit=1
    )[0]
    assert "const displayState = item.outcome?.state || item.state;" in recent
    assert "item.outcome ? lifecycleLabel(displayState)" in recent
    assert "item.outcome.title" in recent
    assert "eventDateTime(item.outcome.start_at)" in recent


def test_deployment_mutations_forward_the_session_bound_csrf_cookie() -> None:
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")

    cookie_reader = app.split("function cookieValue(name)", maxsplit=1)[1].split(
        "async function api", maxsplit=1
    )[0]
    assert "document.cookie" in cookie_reader
    assert "if (matches.length !== 1) return null;" in cookie_reader

    api = app.split("async function api", maxsplit=1)[1].split(
        "function readLocalSession", maxsplit=1
    )[0]
    assert '["GET", "HEAD", "OPTIONS"]' in api
    assert "state.config?.csrf_cookie_name" in api
    assert "headers[state.config.csrf_header_name] = csrf;" in api
    assert 'credentials: "same-origin"' in api

    sign_out = app.split("async function signOut()", maxsplit=1)[1].split(
        "function bindEvents()", maxsplit=1
    )[0]
    assert 'await api(state.config.logout_url, { method: "POST" });' in sign_out


def test_account_erasure_ui_requires_exact_confirmation_and_shows_only_acceptance() -> None:
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'const ERASURE_CONFIRMATION = "DELETE MY ACCOUNT";' in app
    assert "confirmation: ERASURE_CONFIRMATION" in app
    assert 'method: "POST"' in app
    assert 'api("/v1/me/erasure-requests"' in app
    assert "localStorage.setItem(ERASURE_REQUEST_KEY" in app
    assert "localStorage.removeItem(ERASURE_REQUEST_KEY" in app
    assert "error.status === 428 && state.config?.reauth_url" in app
    assert 'body: { return_to: "/app#/settings" }' in app
    accepted = app.split("function showErasureAccepted()", maxsplit=1)[1].split(
        "async function beginAccountErasure", maxsplit=1
    )[0]
    assert 'setScreen("erasure")' in accepted
    assert "setTimeout" not in accepted
    assert "poll" not in accepted.casefold()

    assert 'id="account-erasure-dialog"' in html
    assert 'id="account-erasure-confirm"' in html
    assert 'id="account-erasure-confirmation"' in html
    assert "This is an acceptance receipt, not a claim" in html
    assert "Production accounts require a fresh sign-in" in html


def test_consumer_focus_rings_contrast_with_light_and_dark_surfaces() -> None:
    css = (_STATIC_DIR / "app.css").read_text(encoding="utf-8")

    assert "outline: 3px solid var(--coral);" not in css
    assert css.count("outline: 3px solid var(--coral-ink);") == 3
    assert ".welcome .skip-link:focus-visible" in css
    assert ".request-progress .text-button:focus-visible" in css
    assert ".toast button:focus-visible" in css


def test_consumer_collections_follow_bounded_cursors_and_label_recent_items() -> None:
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "const COLLECTION_PAGE_SIZE = 50;" in app
    assert "const COLLECTION_PAGE_CAP = 4;" in app
    loader = app.split("async function loadCursorCollection(path)", maxsplit=1)[1].split(
        "async function loadRegistrations()", maxsplit=1
    )[0]
    assert "page < COLLECTION_PAGE_CAP" in loader
    assert 'query.set("cursor", cursor);' in loader
    assert "seenCursors.has(nextCursor)" in loader
    assert "return { items, capped: Boolean(cursor) };" in loader

    assert 'loadCursorCollection("/v1/registrations")' in app
    assert 'loadCursorCollection("/v1/tasks?state=actionable")' in app
    assert 'if (days < 0) return "Recently";' in app
    assert '["Recently", "Today", "Tomorrow", "This week", "Later"]' in app
    assert "Showing the first ${COLLECTION_ITEM_CAP} plans" in app
    assert "Showing the first ${COLLECTION_ITEM_CAP} actionable to-dos" in app


def test_chat_and_catalog_list_preserve_truth_and_csp_safe_dom() -> None:  # noqa: PLR0915
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (_STATIC_DIR / "app.css").read_text(encoding="utf-8")

    for element_id in (
        "filter-when",
        "filter-price",
        "filter-city",
        "filter-source",
        "filter-radius",
        "filter-sort",
        "browse-current-events",
        "recommendation-messages",
        "results-count",
        "catalog-search",
        "catalog-active-filters",
        "catalog-filter-state",
        "chat-workspace-tab",
        "catalog-workspace-tab",
        "map-workspace-tab",
        "calendar-workspace-tab",
        "workspace-context-rail",
        "catalog-date-range-row",
        "catalog-date-start",
        "catalog-date-end",
        "catalog-date-apply",
        "events-map",
        "map-event-list",
        "calendar-workspace",
        "calendar-current-month",
        "event-calendar-grid",
        "calendar-agenda-list",
        "new-chat",
    ):
        assert f'id="{element_id}"' in html

    assert "Each brief is standalone" in html
    assert "prior turns are not sent with it" in html
    assert "from durable Saved briefs" in html
    assert "not claims of autonomous or LLM reasoning" in html
    assert "Choosing a provider loads its complete current catalog within a safety cap" in html
    assert 'id="filter-category"' not in html
    assert 'id="toggle-discovery-filters"' not in html
    assert 'data-workspace-mode="chat"' in html
    assert 'role="tablist"' not in html
    assert 'role="tab"' not in html
    assert 'aria-current="page"' in html
    assert 'class="picks-list" role="list"' in html
    assert 'href="/assets/leaflet.css"' in html
    assert 'src="/assets/leaflet.js"' in html

    hints = app.split("function requestTextWithHints", maxsplit=1)[1].split(
        "function normalizedHostname", maxsplit=1
    )[0]
    assert "state.catalogSearch" in hints
    for field in ("when", "city", "price", "radius", "sort"):
        assert f"filters.{field} !== DISCOVERY_DEFAULTS.{field}" in hints
    # The provider scope is deliberately excluded. It selects which catalog page the
    # browser is paging through rather than what the person asked for, and /v1/feed has
    # no source parameter, so appending it would constrain the brief with something the
    # recommender cannot honor. test_consumer_ui asserts the resulting request body.
    assert "filters.source !== DISCOVERY_DEFAULTS.source" not in hints

    discovery = app.split("async function runDiscovery({", maxsplit=1)[1].split(
        "async function submitBrief(event)", maxsplit=1
    )[0]
    assert "const text = requestTextWithHints(baseText, filters);" in discovery
    assert 'mode === "handle" ? "/v1/requests" : "/v1/feed"' in discovery

    assert 'hostname?.endsWith(".luma.com")' in app
    assert 'hostname?.endsWith(".meetup.com")' in app
    assert 'hostname?.endsWith(".partiful.com")' in app
    assert 'rawSource === "meetup"' in app
    assert "· source ${identity.rawSource}" in app
    assert "Eligible to handle" in app
    assert '"Chronological"' in app
    assert '"Not conflict-checked · event link available"' in app
    assert 'item.conflict === "not_evaluated"' in app
    assert "Registration link listed" not in app
    assert "eventRegistrationUrl(item)" in app
    assert 'node("article", "pick-card event-row")' in app
    assert "function matchesCatalogSearch" in app
    assert 'navigate("catalog")' in app

    assert "new AbortController()" in app
    assert "state.discoveryGeneration" in app
    assert "state.discoveryController?.abort();" in app
    assert "generation !== state.discoveryGeneration" in app
    assert "Result set replaced" in app

    assert "innerHTML" not in app
    assert "insertAdjacentHTML" not in app
    assert "eval(" not in app
    assert "View event details for ${item.title}" in app
    assert "More like this:" not in app
    assert "Not interested:" not in app
    assert "feedback-button" not in app
    assert 'recordFeedback(item.canonical_event_id, "click")' in app
    assert 'unknown: "Price unknown"' in app
    assert 'node("section", "pick-description")' in app
    assert "pick-description-copy" in app
    assert '"About this event"' in app
    assert "function googleCalendarUrl(item)" in app
    assert "https://calendar.google.com/calendar/render?" in app
    assert "Add ${item.title} to Google Calendar" in app
    assert "matchesCatalogSearch(item)" in app
    assert 'marker.getElement()?.setAttribute("aria-label", label)' in app
    assert '"No exact pin"' in app
    assert "https://www.google.com/maps/search/" in app
    assert "https://tile.openstreetmap.org/{z}/{x}/{y}.png" in app
    assert "function renderEventMap(items)" in app
    assert "const CATALOG_PAGE_SIZE = 50;" in app
    assert "const CATALOG_PROVIDER_PAGE_CAP = 20;" in app
    assert "return `/v1/catalog/events?${query}`;" in app
    assert 'query.set("source_key", sourceKey);' in app
    assert "async function loadCatalogPages" in app
    assert "pages >= CATALOG_PROVIDER_PAGE_CAP" in app
    assert "rememberCatalogProviders(feed?.providers" in app
    assert "catalogEndpointUnavailable(catalogError)" in app
    assert 'payload = await api("/v1/feed"' in app

    assert "@media (max-width: 1080px)" in css
    assert "@media (max-width: 420px)" in css
    assert "scroll-padding-bottom: calc(112px + env(safe-area-inset-bottom));" in css
    assert ".catalog-active-filters" in css
    assert ".catalog-filter-token" in css
    assert ".catalog-filter-state-shell" in css
    assert ".recommendation-messages" in css
    assert ".pick-detail-grid" in css
    assert ".pick-title-button" in css
    assert ".map-workspace" in css
    assert ".workspace-context-rail" in css
    assert ".calendar-workspace" in css
    assert ".event-calendar-grid" in css
    assert ".calendar-day" in css
    assert ".calendar-agenda-card" in css
    assert ".event-map-marker" in css
    assert ".map-coordinate-note" in css
    assert '.provider-filter[aria-pressed="true"]' in css
    assert ".workspace-topbar" in css
    assert ".catalog-search" in css
    assert ".chat-composer" in css


def test_catalog_filtering_uses_search_tokens_and_hidden_native_state_only() -> None:
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (_STATIC_DIR / "app.css").read_text(encoding="utf-8")

    context_start = html.index('id="workspace-context-rail"')
    shell_start = html.index('<div class="catalog-search-shell">', context_start)
    shell_end = html.index("</aside>", shell_start)
    shell = html[shell_start:shell_end]
    assert re.search(
        r'</label>\s*<div\s+id="catalog-active-filters"',
        shell,
    )
    active_open = shell.split('id="catalog-active-filters"', maxsplit=1)[1].split(
        ">",
        maxsplit=1,
    )[0]
    assert 'class="catalog-active-filters"' in active_open
    assert 'role="group"' in active_open
    assert 'aria-label="Active event filters"' in active_open
    assert "aria-live" not in active_open

    legacy_id = html.index('id="discovery-console"')
    legacy_start = html.rfind("<section", 0, legacy_id)
    legacy_open_end = html.index(">", legacy_id)
    legacy_end = html.index("</section>", legacy_open_end) + len("</section>")
    legacy_open = html[legacy_start:legacy_open_end]
    legacy = html[legacy_start:legacy_end]
    assert re.search(r"\bhidden\b", legacy_open)
    assert 'class="discovery-console event-filter-bar catalog-filter-state-shell"' in legacy_open
    assert 'id="catalog-filter-state"' in legacy
    for element_id in (
        "filter-when",
        "filter-city",
        "filter-price",
        "filter-source",
        "filter-radius",
        "filter-sort",
        "clear-discovery-filters",
        "provider-quick-filters",
    ):
        assert f'id="{element_id}"' in legacy
    assert "<select" not in f"{html[:legacy_start]}{html[legacy_end:]}"

    compact_css = " ".join(css.split())
    assert (
        ".catalog-filter-state-shell, #catalog-filter-state { display: none !important; }"
        in compact_css
    )
    assert ".catalog-active-filters:empty { display: none; }" in compact_css
    for selector in (
        ".catalog-filter-token",
        ".catalog-filter-token-label",
        ".catalog-filter-token-edit",
        ".catalog-filter-token-remove",
    ):
        assert selector in css
    responsive = next(
        section
        for section in css.split("@media (max-width: 640px)")[1:]
        if ".catalog-active-filters {" in section
    )
    assert ".catalog-active-filters" in responsive
    assert "flex-wrap: nowrap;" in responsive
    assert "overflow-x: auto;" in responsive
    assert ".catalog-filter-token" in responsive

    renderer = app.split(
        "function renderCatalogActiveFilters(filters = discoveryFilters())",
        maxsplit=1,
    )[1].split("function requestTextWithHints", maxsplit=1)[0]
    assert 'const target = $("#catalog-active-filters");' in renderer
    assert 'node("span", "catalog-filter-token")' in renderer
    assert 'node("button", "catalog-filter-token-edit")' in renderer
    assert 'node("button", "catalog-filter-token-remove"' in renderer
    assert 'remove.addEventListener("click", () => removeCatalogFilter(name));' in renderer
    assert '"Clear all"' in renderer
    result_filter_sync = app.split(
        "function renderActiveResultFilters(filters = discoveryFilters())",
        maxsplit=1,
    )[1].split("function updateResultsToolbar", maxsplit=1)[0]
    assert "renderCatalogActiveFilters(filters);" in result_filter_sync
    assert "syncEventFilterRail(filters);" in result_filter_sync


def test_catalog_search_filter_suggestions_have_an_accessible_combobox_contract() -> None:
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'role="combobox"' in html
    assert 'aria-autocomplete="list"' in html
    assert 'aria-controls="catalog-filter-suggestions"' in html
    assert 'id="catalog-filter-suggestions"' in html
    assert 'role="listbox"' in html
    assert 'id="catalog-date-range-row"' in html
    assert 'aria-label="Choose date range"' in html
    assert 'id="catalog-date-start"' in html
    assert 'id="catalog-date-end"' in html
    assert 'type="date"' in html
    assert '<label for="catalog-date-start">' in html
    assert '<label for="catalog-date-end">' in html
    assert "<span>From</span>" in html
    assert "<span>To</span>" in html

    for label in ("Today", "Tomorrow", "This weekend"):
        assert f'label: "{label}"' in app
    assert "ariaLabel: `Date · ${definition.label}`" in app
    assert '"Choose date range…"' in app
    assert '"aria-activedescendant"' in app
    assert 'event.key === "ArrowDown"' in app
    assert 'event.key === "Enter"' in app
    assert 'event.key === "Escape"' in app
    assert "range:" in app
    assert "function applyDateRangeFilter" in app
    assert 'setEventFilter("source"' in app
    assert 'setEventFilter("when"' in app


def test_session_termination_invalidates_reads_mutations_and_hidden_private_state() -> None:
    app = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "function invalidateSessionLoads()" in app
    assert "state.sessionGeneration += 1;" in app
    assert "sessionGeneration !== state.sessionGeneration" in app
    assert "if (state.refreshInFlight === refresh) state.refreshInFlight = null;" in app
    assert "function clearPrivateClientState()" in app
    assert "state.verifyingTasks.clear();" in app
    assert "clearErasureRequestId();" in app
    assert '$("#completion-copy").textContent =' in app
    assert '$("#toast-message").textContent = "";' in app

    for start, end in (
        ("async function withdrawPlan(item, button)", "function taskReason(value)"),
        ("async function completeCurrentTask()", "async function savePreferences()"),
        ("async function savePreferences()", "async function onboard(event)"),
    ):
        action = app.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]
        assert "const sessionGeneration = state.sessionGeneration;" in action
        assert "sessionGeneration !== state.sessionGeneration" in action
    save_preferences = app.split("async function savePreferences()", maxsplit=1)[1].split(
        "async function onboard(event)", maxsplit=1
    )[0]
    assert "state.me !== me" in save_preferences
    assert "sessionGeneration === state.sessionGeneration && state.me === me" in save_preferences
