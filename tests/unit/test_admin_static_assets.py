"""Static contracts for the same-origin ingestion administration surface."""

from __future__ import annotations

from pathlib import Path

_STATIC_DIR = Path(__file__).parents[2] / "src" / "events_concierge" / "api" / "static"


def _asset(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


def test_admin_shell_declares_separate_semantic_operational_surface() -> None:
    html = _asset("admin.html")

    assert "<title>Ingestion admin · Events Concierge</title>" in html
    assert 'href="/assets/app.css"' in html
    assert 'href="/assets/admin.css"' in html
    assert '<script type="module" src="/assets/admin.js"></script>' in html
    assert 'href="/app"' in html
    assert 'id="admin-main"' in html
    assert 'id="policy-banner"' in html
    assert 'id="overview-metrics"' in html
    assert 'id="overview-source-search"' in html
    assert 'id="overview-source-results"' in html
    assert 'id="attention-source-list"' in html
    assert 'id="pipeline-chart"' in html
    assert 'id="overview-stage-detail"' in html
    assert 'id="operational-diagnostics"' in html
    assert 'id="source-filters"' in html
    assert 'id="source-rows"' in html
    assert 'id="command-list"' in html
    assert 'id="run-list"' in html
    assert 'id="refresh-due-dialog"' in html
    assert 'id="admin-live-status"' in html
    assert 'aria-live="polite"' in html
    assert 'role="alert"' in html

    # Fixture data is an explicit opt-in and potentially broad cadence work has a confirmation.
    fixture_input = html.split('id="include-fixtures"', maxsplit=1)[1].split(">", maxsplit=1)[0]
    assert "checked" not in fixture_input
    assert 'id="open-refresh-due"' in html
    assert 'id="confirm-refresh-due"' in html
    assert "Each source still passes its reviewed registry" in html


def test_admin_overview_prioritizes_named_sources_actions_and_progressive_disclosure() -> None:
    html = _asset("admin.html")
    app = _asset("admin.js")
    css = _asset("admin.css")

    assert "Run the catalog" in html
    assert "Find source by name, URL, publisher, or key" in html
    assert 'data-overview-route="failed-runs"' in html
    assert 'data-overview-route="due-sources"' in html
    assert 'data-overview-route="running-runs"' in html
    assert 'data-overview-route="pending-commands"' in html
    assert "Adapter families are implementation details" in html
    assert "appears here as Public JSON-LD" in html

    assert "async function loadOverviewSources()" in app
    assert "async function loadOverviewRuns()" in app
    assert "function overviewRunCollection()" in app
    assert 'source?.seed_url' in app
    assert "function sourceSearchCopy(source)" in app
    assert "function renderAttentionSources()" in app
    assert "function renderOverviewStageDetail(stages, facts)" in app
    assert 'button.setAttribute("aria-controls", "overview-stage-detail");' in app
    assert 'button.setAttribute("aria-pressed"' in app
    assert "These counts describe the bounded run ledger" in app

    assert ".operations-flow-list" in css
    assert ".flow-connector" in css
    assert ".operational-diagnostics" in css
    assert "@keyframes flow-travel" in css
    assert ".operations-flow-list.has-live-work .flow-connector i" in css
    assert ".operational-diagnostics .panel-tools" in css


def test_admin_operator_token_is_password_input_and_never_browser_persistent() -> None:
    html = _asset("admin.html")
    app = _asset("admin.js")

    token_input = html.split('id="operator-token"', maxsplit=1)[1].split(">", maxsplit=1)[0]
    assert 'type="password"' in token_input
    assert 'autocomplete="off"' in token_input
    assert "Kept only in this page" in html
    assert "never written to browser storage" in html

    assert 'headers.Authorization = `Bearer ${state.operatorToken}`;' in app
    assert 'state.operatorToken = "";' in app
    assert 'window.addEventListener("pagehide"' in app
    for persistent_sink in (
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "indexedDB",
    ):
        assert persistent_sink not in app


def test_admin_client_uses_exact_bounded_ingestion_api_contract() -> None:
    app = _asset("admin.js")

    assert 'api("/admin/v1/ingestion/overview")' in app
    assert '`/admin/v1/ingestion/sources?${sourceQueryString()}`' in app
    assert '`/admin/v1/ingestion/runs?${runQueryString()}`' in app
    assert '`/admin/v1/ingestion/commands?limit=${COMMAND_PAGE_SIZE}`' in app
    assert 'api("/admin/v1/ingestion/commands", {' in app
    assert 'method: "POST"' in app
    assert "const body = { command_id: commandId, action };" in app
    assert 'if (sourceKey) body.source_key = sourceKey;' in app
    assert 'crypto.randomUUID()' in app

    assert "const SOURCE_PAGE_SIZE = 50;" in app
    assert "const RUN_PAGE_SIZE = 50;" in app
    assert "const COMMAND_PAGE_SIZE = 30;" in app
    assert 'state: state.sourceState' in app
    assert 'include_fixtures: String(state.includeFixtures)' in app
    assert 'state.includeFixtures = $("#include-fixtures").checked;' in app
    assert 'query.set("status", state.runStatus);' in app
    assert 'query.set("source_key", state.runSourceKey);' in app


def test_admin_commands_are_idempotent_poll_only_while_pending_and_pause_when_hidden() -> None:
    app = _asset("admin.js")

    assert "retryCommandIds: new Map()" in app
    assert "state.retryCommandIds.get(retryKey) || crypto.randomUUID()" in app
    assert "state.retryCommandIds.set(retryKey, commandId);" in app
    assert "state.retryCommandIds.delete(retryKey);" in app
    assert "PENDING_STATUSES.has" in app
    assert "pendingCommandCount() > 0" in app
    assert 'document.visibilityState !== "visible"' in app
    assert "window.setTimeout" in app
    assert "POLL_MAX_MS" in app
    assert 'document.addEventListener("visibilitychange"' in app
    assert "stopPolling();" in app
    assert "if (shouldPoll()) void pollPendingWork();" in app

    due_dialog = app.split('const dueDialog = $("#refresh-due-dialog");', maxsplit=1)[1]
    assert 'dueDialog.addEventListener("close"' in due_dialog
    assert 'dueDialog.returnValue === "confirm"' in due_dialog
    assert 'submitCommand("refresh_due"' in due_dialog


def test_admin_dynamic_rendering_uses_safe_dom_and_safe_external_links() -> None:
    app = _asset("admin.js")

    assert "document.createElement(tag)" in app
    assert "element.textContent = String(content)" in app
    assert "replaceChildren()" in app
    assert "parsed.protocol !== \"https:\"" in app
    assert 'title.target = "_blank";' in app
    assert 'title.rel = "noopener noreferrer";' in app
    for unsafe_sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert unsafe_sink not in app


def test_admin_client_supports_deep_linked_observability_and_revision_provenance() -> None:
    app = _asset("admin.js")

    assert 'new Set(["overview", "sources", "runs", "commands", "logic"])' in app
    assert 'url.searchParams.get("source")' in app
    assert 'setQueryValue(url.searchParams, "source", state.selectedSourceKey);' in app
    assert "/admin/v1/ingestion/filters?include_fixtures=" in app
    assert "/admin/v1/ingestion/sources/${encodeURIComponent(sourceKey)}" in app
    assert "include_fixtures: String(state.includeFixtures)" in app
    assert 'runWindow: "24h"' in app
    assert 'url.searchParams.get("run_window") || "24h"' in app
    assert 'setQueryValue(url.searchParams, "run_window", state.runWindow, "24h");' in app
    assert 'query.set("window_hours", String(windowHours));' in app
    assert 'api("/versionz")' in app
    assert "requestInFlight: new Map()" in app
    assert "adapters/crawl/source.py" in app
    assert "application/catalog_refresh.py" in app
    assert "adapters/postgres/catalog_refresh_commit.py" in app
    assert "proof of causation" in app
    assert 'run.provenance_status === "claim_recorded"' in app
    assert "Worker claim ·" in app
    assert "Source revision recorded ·" in app
    assert "worker claim unavailable" in app
    assert "Legacy run · worker claim unavailable" in app
    assert 'node("strong", "", "Accepted by")' in app
    assert 'node("strong", "", "Claimed by")' in app
    assert "command.executor_source_revision" in app
    assert "command.executor_release_revision" in app
    assert "command.executor_image_digest" in app
    assert '? "Not claimed yet"' in app
    assert "does not prove the worker performed the fetch" in app


def test_admin_styles_match_product_and_cover_responsive_accessibility_modes() -> None:
    css = _asset("admin.css")

    for product_token in (
        "var(--paper)",
        "var(--surface)",
        "var(--forest)",
        "var(--mint-light)",
        "var(--coral-ink)",
        "var(--serif)",
        "var(--sans)",
        "var(--radius)",
    ):
        assert product_token in css

    assert "@media (max-width: 760px)" in css
    assert ".admin-table td::before" in css
    assert "content: attr(data-label);" in css
    assert "@media (max-width: 520px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "animation: none;" in css
    assert "@media (forced-colors: active)" in css
    assert ".visually-hidden" in css
