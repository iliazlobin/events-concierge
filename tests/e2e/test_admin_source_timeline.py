"""Hermetic checks of the global retained-source registration timeline."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, Route, expect
from tests.e2e.test_admin_workspaces import WorkspaceApi, source_record

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]

ENDPOINT = "/admin/v1/ingestion/source-registration-history"
NOW = datetime(2026, 9, 8, 16, 10, 0, 123456, tzinfo=UTC)


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def registration_history(days: int, fixtures: bool = False, *, empty: bool = False) -> dict[str, Any]:
    """One retained cohort, regrouped into rolling days, not fabricated historical health."""
    start = NOW - timedelta(days=days)
    registrations = [] if empty else [
        (NOW - timedelta(days=60, hours=3), 2),
        (NOW - timedelta(days=15, hours=3), 3),
        (NOW - timedelta(days=2, hours=3), 5),
    ]
    if fixtures and not empty:
        registrations.append((NOW - timedelta(days=1, hours=3), 2))
    original = 0 if empty else 37 + (3 if fixtures else 0)
    baseline = original + sum(count for at, count in registrations if at < start)
    current = baseline
    items = []
    for index in range(days):
        bucket_start = start + timedelta(days=index)
        bucket_end = bucket_start + timedelta(days=1)
        added = sum(count for at, count in registrations if bucket_start <= at < bucket_end)
        current += added
        items.append({
            "bucket_start": _stamp(bucket_start),
            "bucket_end": _stamp(bucket_end),
            "registered_sources": current,
            "added_sources": added,
        })
    return {
        "generated_at": _stamp(NOW),
        "window_days": days,
        "bucket_hours": 24,
        "window_start": _stamp(start),
        "baseline_sources": baseline,
        "total_sources": current,
        "added_sources": current - baseline,
        "items": items,
        "include_fixtures": fixtures,
        "history_scope": "retained_registry",
    }


@dataclass
class SourceTimelineApi(WorkspaceApi):
    timeline_calls: list[dict[str, list[str]]] = field(default_factory=list)
    timeline_status: int = 200
    timeline_empty: bool = False
    held_days: set[int] = field(default_factory=set)
    held_routes: list[tuple[Route, dict[str, Any]]] = field(default_factory=list)
    global_rows: list[dict[str, Any]] | None = None
    global_status: int = 200
    global_calls: list[dict[str, list[str]]] = field(default_factory=list)
    hold_global_offset: int | None = None
    held_global_routes: list[tuple[Route, dict[str, Any]]] = field(default_factory=list)

    def handle(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        params = parse_qs(urlsplit(route.request.url).query)
        if path == "/admin/v1/ingestion/sources" and self.global_rows is not None and not params.get("query"):
            assert route.request.method == "GET", "Source fixtures forbid mutations"
            self.calls.append((path, params))
            self.global_calls.append(params)
            if self.global_status != 200:
                self.respond(route, {"detail": "Fixture global registry unavailable"}, self.global_status)
                return
            offset = int(params.get("offset", ["0"])[0])
            limit = int(params.get("limit", ["100"])[0])
            body = {"items": self.global_rows[offset:offset + limit], "total": len(self.global_rows), "limit": limit, "offset": offset}
            if offset == self.hold_global_offset:
                self.held_global_routes.append((route, body))
            else:
                self.respond(route, body)
            return
        if path != ENDPOINT:
            super().handle(route)
            return
        assert route.request.method == "GET", "Timeline fixtures forbid mutations"
        self.calls.append((path, params))
        self.timeline_calls.append(params)
        if self.timeline_status != 200:
            self.respond(route, {"detail": "Fixture registration history unavailable"}, self.timeline_status)
            return
        days = int(params.get("window_days", ["90"])[0])
        fixtures = params.get("include_fixtures", ["false"])[0] == "true"
        body = registration_history(days, fixtures, empty=self.timeline_empty)
        if days in self.held_days:
            self.held_routes.append((route, body))
            return
        self.respond(route, body)

    def release_history(self) -> None:
        pending, self.held_routes = self.held_routes, []
        for route, body in pending:
            self.respond(route, body)

    def release_global_roster(self) -> None:
        self.hold_global_offset = None
        pending, self.held_global_routes = self.held_global_routes, []
        for route, body in pending:
            self.respond(route, body)


@pytest.fixture
def timeline_page(browser: Browser) -> Iterator[tuple[Page, SourceTimelineApi, str]]:
    context = browser.new_context(viewport={"width": 1440, "height": 1050}, reduced_motion="reduce")
    page = context.new_page()
    page.set_default_timeout(7000)
    scenario = SourceTimelineApi()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/v1/**", scenario.handle)
    try:
        yield page, scenario, os.environ["EC_ADMIN_WEB_URL"].rstrip("/")
        assert not errors, errors
        assert not scenario.unexpected, scenario.unexpected
    finally:
        page.unroute_all(behavior="ignoreErrors")
        context.close()


def _history(page: Page):
    return page.get_by_role("region", name="Source registration history", exact=True)


def _timeline(page: Page):
    return _history(page).get_by_role("group", name="Registered sources timeline", exact=True)


def _window(page: Page, days: int):
    return _history(page).get_by_role("group", name="Source history window", exact=True).get_by_role(
        "button", name=f"{days}d", exact=True
    )


def _metric(page: Page, label: str):
    return _history(page).locator("dt", has_text=re.compile(f"^{re.escape(label)}$")).locator("+ dd")


def test_source_timeline_uses_global_registration_counts_and_quiet_utc_intervals(timeline_page) -> None:
    page, scenario, base = timeline_page
    page.goto(f"{base}/admin?tab=sources")
    expect(_history(page).get_by_role("heading", name="Sources over time", exact=True)).to_be_visible()
    expect(_window(page, 90)).to_have_attribute("aria-pressed", "true")
    expect(_metric(page, "Registered sources")).to_have_text("47")
    expect(_metric(page, "Added in 90d")).to_have_text("+10")
    assert scenario.timeline_calls[-1] == {"window_days": ["90"], "include_fixtures": ["false"]}
    # Two loaded registry rows cannot stand in for the aggregate's retained cohort.
    expect(page.get_by_role("table", name="Source registry", exact=True).locator("tbody tr")).to_have_count(2)
    bucket = registration_history(90)["items"][0]
    timestamp = datetime.fromisoformat(bucket["bucket_end"]).strftime("%Y-%m-%d %H:%M UTC")
    point = _timeline(page).get_by_role("img", name=f"{timestamp}: 37 registered sources; 0 added in previous 24h", exact=True)
    expect(point).to_be_visible()
    point.hover()
    expect(_history(page).get_by_text(timestamp, exact=True)).to_be_visible()
    expect(_history(page).get_by_role("status").filter(has_text="37 registered")).to_contain_text("37 registered · +0 in previous 24h")
    expect(_timeline(page).get_by_role("img")).to_have_count(91)
    expect(_timeline(page).get_by_role("img").last).to_have_attribute("aria-label", "2026-09-08 16:10 UTC: 47 registered sources; 0 added in previous 24h")
    baseline = _timeline(page).get_by_role("img").first
    expect(baseline).to_have_attribute("aria-label", re.compile(r": 37 registered sources; start of period$"))
    baseline.focus()
    expect(_history(page).get_by_role("status").filter(has_text="37 registered")).to_contain_text("37 registered · start of period")


def test_source_timeline_window_reload_back_and_forward_preserve_other_scope(timeline_page) -> None:
    page, scenario, base = timeline_page
    page.goto(f"{base}/admin?tab=sources&registry_query=Bay&source_window=24&store_query=music")
    expect(_metric(page, "Added in 90d")).to_have_text("+10")
    _window(page, 7).click()
    expect(_metric(page, "Added in 7d")).to_have_text("+5")
    first_url = page.url
    assert parse_qs(urlsplit(first_url).query)["source_trend"] == ["7"]
    _window(page, 30).click()
    expect(_metric(page, "Added in 30d")).to_have_text("+8")
    second_url = page.url
    page.reload()
    expect(_window(page, 30)).to_have_attribute("aria-pressed", "true")
    expect(_metric(page, "Registered sources")).to_have_text("47")
    assert scenario.timeline_calls[-1]["window_days"] == ["30"]
    page.go_back()
    expect(_window(page, 7)).to_have_attribute("aria-pressed", "true")
    expect(_metric(page, "Added in 7d")).to_have_text("+5")
    assert page.url == first_url
    page.go_forward()
    expect(_metric(page, "Added in 30d")).to_have_text("+8")
    assert page.url == second_url
    _window(page, 90).click()
    expect(_metric(page, "Added in 90d")).to_have_text("+10")
    params = parse_qs(urlsplit(page.url).query)
    assert "source_trend" not in params
    assert params["registry_query"] == ["Bay"]
    assert params["source_window"] == ["24"]
    assert params["store_query"] == ["music"]


def test_source_timeline_only_fixture_toggle_changes_registry_scope(timeline_page) -> None:
    page, scenario, base = timeline_page
    page.goto(
        f"{base}/admin?tab=sources&registry_query=Bay&registry_state=failed"
        "&registry_mode=public_jsonld&registry_publisher=Bay%20Arts%20Council&registry_region=Bay%20Area"
    )
    expect(_metric(page, "Registered sources")).to_have_text("47")
    calls = len(scenario.timeline_calls)
    page.get_by_role("textbox", name=re.compile(r"^Search sources")).fill("arts")
    page.get_by_role("region", name="Source registry charts", exact=True).get_by_role(
        "button", name="Filter sources: Latest succeeded (1)", exact=True
    ).click()
    expect(page.locator("#source-registry-list").get_by_role("button", name="Bay Arts 02", exact=True)).to_be_visible()
    expect(_metric(page, "Registered sources")).to_have_text("47")
    assert len(scenario.timeline_calls) == calls
    page.get_by_role("checkbox", name=re.compile(r"^Include .*fixtures$")).check()
    expect(_metric(page, "Registered sources")).to_have_text("52")
    expect(_metric(page, "Added in 90d")).to_have_text("+12")
    assert scenario.timeline_calls[-1] == {"window_days": ["90"], "include_fixtures": ["true"]}
    page.go_back()
    expect(_metric(page, "Registered sources")).to_have_text("47")
    assert scenario.timeline_calls[-1]["include_fixtures"] == ["false"]


def test_source_timeline_failure_zero_and_retry_are_independent_of_registry(timeline_page) -> None:
    page, scenario, base = timeline_page
    page.goto(f"{base}/admin?tab=sources")
    expect(_metric(page, "Registered sources")).to_have_text("47")
    scenario.timeline_status = 503
    _window(page, 7).click()
    expect(_history(page).get_by_text("Registration history unavailable.", exact=True)).to_be_visible()
    expect(_timeline(page)).to_have_count(0)
    expect(_metric(page, "Registered sources")).not_to_have_text("47")
    expect(_metric(page, "Registered sources")).not_to_have_text("0")
    expect(page.locator("#source-registry-list").get_by_role("button", name="Bay Arts 01", exact=True)).to_be_visible()
    scenario.timeline_status = 200
    scenario.timeline_empty = True
    _history(page).get_by_role("button", name="Retry history", exact=True).click()
    expect(_metric(page, "Registered sources")).to_have_text("0")
    expect(_metric(page, "Added in 7d")).to_have_text("+0")
    expect(_timeline(page)).to_be_visible()
    expect(_history(page).get_by_text("Registration history unavailable.", exact=True)).to_have_count(0)
    scenario.timeline_empty = False
    _window(page, 30).click()
    expect(_metric(page, "Registered sources")).to_have_text("47")


def test_source_timeline_late_obsolete_window_cannot_replace_current_counts(timeline_page) -> None:
    page, scenario, base = timeline_page
    page.goto(f"{base}/admin?tab=sources")
    expect(_metric(page, "Added in 90d")).to_have_text("+10")
    scenario.held_days.add(7)
    with page.expect_request(lambda request: urlsplit(request.url).path == ENDPOINT and "window_days=7" in request.url):
        _window(page, 7).click()
    expect(_history(page).get_by_text("Loading registration history…", exact=True)).to_be_visible()
    expect(_timeline(page)).to_have_count(0)
    _window(page, 30).click()
    expect(_metric(page, "Added in 30d")).to_have_text("+8")
    scenario.release_history()
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    expect(_window(page, 30)).to_have_attribute("aria-pressed", "true")
    expect(_metric(page, "Added in 30d")).to_have_text("+8")
    expect(_metric(page, "Added in 7d")).to_have_count(0)


def test_source_timeline_mobile_keyboard_is_read_only_and_fits_viewport(timeline_page) -> None:
    page, _, base = timeline_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/admin?tab=sources&source_trend=7")
    points = _timeline(page).get_by_role("img")
    expect(points.last).to_be_visible()
    original = page.url
    expect(page.get_by_role("textbox", name="Search sources", exact=True)).to_have_value("Bay")
    points.first.focus()
    points.first.press("End")
    expect(points.last).to_be_focused()
    points.last.press("Home")
    expect(points.first).to_be_focused()
    points.first.press("ArrowRight")
    expect(points.nth(1)).to_be_focused()
    points.nth(1).press("ArrowLeft")
    expect(points.first).to_be_focused()
    points.first.press("Enter")
    assert page.url == original
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.evaluate("document.body.scrollWidth <= innerWidth")


def current_source_roster() -> list[dict[str, Any]]:
    rows = [source_record(number) for number in range(1, 8)]
    rows[3].update(enabled=False, effective_status="disabled")
    rows[4].update(retired_at="2026-09-01T00:00:00Z", effective_status="retired")
    rows[5].update(review_status="unreviewed", effective_status="unreviewed")
    rows[6].update(latest_run=None)
    return rows


def test_source_current_state_is_global_nonzero_and_resets_filters_in_one_history_step(timeline_page) -> None:
    page, scenario, base = timeline_page
    scenario.global_rows = current_source_roster()
    page.goto(
        f"{base}/admin?tab=sources&source_trend=7&registry_query=Bay&registry_state=failed"
        "&registry_mode=public_jsonld&registry_publisher=Bay%20Arts%20Council&registry_region=Bay%20Area"
        "&registry_fixtures=true&store_query=music"
    )
    history = _history(page)
    expected = ["Healthy (2)", "Failed (1)", "Blocked (1)", "No runs (1)", "Paused (1)", "Retired (1)"]
    for label in expected:
        name, count = label.rsplit(" (", 1)
        expect(history.get_by_role("button", name=f"Show {name} sources ({count}", exact=True)).to_be_visible()
    expect(history.get_by_role("button", name=re.compile(r"^Show .* sources "))).to_have_count(6)
    expect(page.get_by_role("region", name="Source registry charts", exact=True).get_by_text("Current collection state", exact=True)).to_have_count(0)
    assert all(call.get("include_fixtures") == ["true"] for call in scenario.global_calls)
    assert all(not call.get(key) for call in scenario.global_calls for key in ("query", "mode", "publisher", "region"))
    assert all(call.get("state") == ["all"] for call in scenario.global_calls)
    original = page.url
    history.get_by_role("button", name="Show Paused sources (1)", exact=True).click()
    registry = page.locator("#source-registry-list")
    expect(registry.get_by_role("button", name="Bay Arts 04", exact=True)).to_be_visible()
    expect(registry.get_by_role("button", name="Bay Arts 01", exact=True)).to_have_count(0)
    params = parse_qs(urlsplit(page.url).query)
    for key in ("registry_query", "registry_state", "registry_mode", "registry_publisher", "registry_region"):
        assert key not in params
    assert params["registry_lens"] == ["paused"]
    assert params["registry_fixtures"] == ["true"]
    assert params["source_trend"] == ["7"]
    assert params["store_query"] == ["music"]
    filtered = page.url
    page.go_back()
    expect(page).to_have_url(original)
    expect(page.get_by_role("textbox", name="Search sources", exact=True)).to_have_value("Bay")
    expect(registry.get_by_role("button", name="Bay Arts 01", exact=True)).to_be_visible()
    page.go_forward()
    expect(page).to_have_url(filtered)
    expect(registry.get_by_role("button", name="Bay Arts 04", exact=True)).to_be_visible()
    page.reload()
    expect(registry.get_by_role("button", name="Bay Arts 04", exact=True)).to_be_visible()


def test_source_current_state_waits_for_complete_global_roster(timeline_page) -> None:
    page, scenario, base = timeline_page
    scenario.global_rows = [source_record(number) for number in range(2, 103)]
    scenario.hold_global_offset = 100
    with page.expect_request(lambda request: urlsplit(request.url).path == "/admin/v1/ingestion/sources" and parse_qs(urlsplit(request.url).query).get("offset") == ["100"]):
        page.goto(f"{base}/admin?tab=sources&registry_query=Bay")
    expect(_metric(page, "Registered sources")).to_have_text("47")
    expect(page.locator("#source-registry-list").get_by_role("button", name="Bay Arts 01", exact=True)).to_be_visible()
    expect(_history(page).get_by_role("button", name=re.compile(r"^Show .* sources "))).to_have_count(0)
    expect(_history(page).get_by_role("button", name="Show Healthy sources (100)", exact=True)).to_have_count(0)
    # Wait for the independent reader's second page before releasing its response.
    expect(_history(page).get_by_text("Loading current collection state…", exact=True)).to_be_visible()
    assert scenario.held_global_routes
    scenario.release_global_roster()
    expect(_history(page).get_by_role("button", name="Show Healthy sources (101)", exact=True)).to_be_visible()


@pytest.mark.parametrize("status", [503, 403])
def test_source_current_state_failed_read_hides_prior_counts_without_zeroing_history(timeline_page, status: int) -> None:
    page, scenario, base = timeline_page
    scenario.global_rows = current_source_roster()
    page.goto(f"{base}/admin?tab=sources&registry_query=Bay")
    expect(_history(page).get_by_role("button", name="Show Healthy sources (2)", exact=True)).to_be_visible()
    scenario.global_status = status
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(_history(page).get_by_text("Current collection state unavailable.", exact=True)).to_be_visible()
    expect(_history(page).get_by_role("button", name=re.compile(r"^Show .* sources "))).to_have_count(0)
    expect(_metric(page, "Registered sources")).to_have_text("47")
    expect(page.locator("#source-registry-list").get_by_role("button", name="Bay Arts 01", exact=True)).to_be_visible()
    scenario.global_status = 200
    _history(page).get_by_role("button", name="Retry collection state", exact=True).click()
    expect(_history(page).get_by_role("button", name="Show Healthy sources (2)", exact=True)).to_be_visible()
