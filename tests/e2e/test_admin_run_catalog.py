"""Hermetic browser checks for exact run-attributed published Catalog records."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import Browser, Page, Route, expect
from tests.e2e.test_admin_operations import _event, catalog_response
from tests.e2e.test_admin_workspaces import RUN, SOURCE, WorkspaceApi, run_record

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]

OTHER_RUN = "catalog:bay-arts-01:other-20260907"
EMPTY_RUN = "catalog:bay-arts-01:empty-20260906"


def published_event(number: int) -> dict[str, Any]:
    return _event(1, number) | {
        "refresh_run_key": RUN if number <= 25 else OTHER_RUN,
        "source_key": SOURCE,
        "source_display_name": "Bay Arts 01",
        "title": f"Bay Arts 01 Event {number:02}"
        if number <= 25
        else f"Other-run event {number:02}",
    }


@dataclass
class RunCatalogApi(WorkspaceApi):
    event_calls: list[dict[str, list[str]]] = field(default_factory=list)
    corruption: str | None = None
    event_status: int = 200
    held_run: str | None = None
    held_routes: list[Route] = field(default_factory=list)

    def handle(self, route: Route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        if path != "/admin/v1/ingestion/events":
            super().handle(route)
            return
        query = parse_qs(urlsplit(request.url).query)
        self.calls.append((path, query))
        self.event_calls.append(query)
        if request.method != "GET":
            self.unexpected.append(f"{request.method} {path}")
            self.respond(route, {"detail": "Fixture forbids writes"}, 405)
            return
        if self.held_run and query.get("run_key") == [self.held_run]:
            self.held_routes.append(route)
            return
        self.respond_events(route)

    def respond_events(self, route: Route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        if self.event_status != 200:
            self.respond(
                route, {"detail": "Fixture published records unavailable"}, self.event_status
            )
            return
        key = query.get("run_key", [None])[0]
        response = catalog_response([published_event(number) for number in range(1, 28)], query)
        shown = response["items"]
        if key and self.corruption == "echo":
            response["run_key"] = OTHER_RUN
        elif key and self.corruption == "missing_echo":
            response.pop("run_key")
        elif key and self.corruption == "row" and shown:
            response["items"] = [shown[0] | {"refresh_run_key": OTHER_RUN}, *shown[1:]]
        self.respond(route, response)


@pytest.fixture
def run_catalog_page(browser: Browser) -> Iterator[tuple[Page, RunCatalogApi, str]]:
    context = browser.new_context(viewport={"width": 1600, "height": 1000}, reduced_motion="reduce")
    context.add_init_script("""Object.defineProperty(navigator, 'clipboard', {value: {
        writeText: async text => { window.__copiedCatalogText = text; }
    }});""")
    page = context.new_page()
    scenario = RunCatalogApi()
    scenario.run_rows = [
        run_record(1) | {"status": "succeeded", "error": None, "canonical_count": 25},
        run_record(2),
        run_record(1)
        | {"run_key": EMPTY_RUN, "status": "succeeded", "error": None, "canonical_count": 0},
    ]
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/admin/v1/**", scenario.handle)
    try:
        yield page, scenario, os.environ["EC_ADMIN_WEB_URL"].rstrip("/")
        assert not errors, errors
        assert not scenario.unexpected, scenario.unexpected
        assert scenario.event_calls
    finally:
        page.unroute_all(behavior="ignoreErrors")
        context.close()


def open_run_records(page: Page, base: str, key: str = RUN) -> None:
    page.goto(
        f"{base}/admin?{urlencode({'tab': 'runs', 'run_selection': f'{SOURCE}|{key}', 'run_inspector': 'summary'})}"
    )
    inspector = page.locator("#run-inspector")
    expect(inspector.get_by_role("heading", name="Bay Arts 01", exact=True)).to_be_visible()
    inspector.get_by_role("button", name="View published records", exact=True).click()
    expect(page.get_by_role("heading", name="Catalog", exact=True, level=1)).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["store_source"] == [SOURCE]
    assert params["store_run"] == [key]


def catalog(page: Page):
    return page.get_by_role("region", name="Catalog investigation", exact=True)


def event_button(page: Page, number: int):
    return catalog(page).get_by_role("button", name=published_event(number)["title"], exact=True)


def test_run_records_preserve_exact_scope_through_paging_search_copy_reload_and_history(
    run_catalog_page,
) -> None:
    page, scenario, base = run_catalog_page
    open_run_records(page, base)
    expect(event_button(page, 1)).to_be_visible()
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_contain_text("25")
    assert scenario.event_calls[-1]["run_key"] == [RUN]
    catalog(page).get_by_role("button", name="Next parsed events page", exact=True).click()
    expect(event_button(page, 21)).to_be_visible()
    expect(event_button(page, 1)).to_have_count(0)
    expect(event_button(page, 26)).to_have_count(0)
    assert scenario.event_calls[-1]["after_start_at"] == [published_event(20)["start_at"]]
    assert scenario.event_calls[-1]["run_key"] == [RUN]
    event_button(page, 21).click()
    expect(event_button(page, 21)).to_have_attribute("aria-expanded", "true")
    selected_page = page.url
    page.reload()
    expect(event_button(page, 21)).to_have_attribute("aria-expanded", "true")
    assert page.url == selected_page
    assert parse_qs(urlsplit(page.url).query)["store_after_start"] == [
        published_event(20)["start_at"]
    ]
    search = catalog(page).get_by_role("textbox", name="Search catalog events")
    search.fill("Event 2")
    expect(event_button(page, 20)).to_be_visible()
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("6")
    params = parse_qs(urlsplit(page.url).query)
    assert params["store_run"] == [RUN]
    assert params["store_query"] == ["Event 2"]
    assert "store_after_start" not in params and "store_event" not in params
    assert scenario.event_calls[-1]["q"] == ["Event 2"]
    event_button(page, 21).click()
    selected_search = page.url
    page.get_by_role("button", name="Copy investigation link", exact=True).click()
    expect(page.get_by_role("button", name="Copied", exact=True)).to_be_visible()
    copied = page.evaluate("window.__copiedCatalogText")
    assert parse_qs(urlsplit(copied).query)["store_run"] == [RUN]
    assert parse_qs(urlsplit(copied).query)["store_event"] == [
        published_event(21)["canonical_event_id"]
    ]
    page.go_back()
    expect(event_button(page, 21)).to_have_attribute("aria-expanded", "false")
    expect(search).to_have_value("Event 2")
    page.go_forward()
    expect(event_button(page, 21)).to_have_attribute("aria-expanded", "true")
    assert page.url == selected_search
    page.goto(copied)
    expect(event_button(page, 21)).to_have_attribute("aria-expanded", "true")
    expect(search).to_have_value("Event 2")
    assert scenario.event_calls[-1]["run_key"] == [RUN]


def test_publishing_run_backlink_and_clear_actions_keep_views_separate(run_catalog_page) -> None:
    page, scenario, base = run_catalog_page
    open_run_records(page, base)
    expect(event_button(page, 1)).to_be_visible()
    original = page.url
    catalog(page).get_by_role("button", name="Publishing run", exact=True).click()
    expect(
        page.locator("#run-inspector").get_by_role(
            "button", name="View published records", exact=True
        )
    ).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["tab"] == ["runs"]
    assert params["run_selection"] == [f"{SOURCE}|{RUN}"]
    page.go_back()
    expect(event_button(page, 1)).to_be_visible()
    assert page.url == original
    catalog(page).get_by_role("button", name="Clear run filter", exact=True).click()
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("27")
    assert "store_run" not in parse_qs(urlsplit(page.url).query)
    assert "run_key" not in scenario.event_calls[-1]
    catalog(page).get_by_role("button", name="Next parsed events page", exact=True).click()
    expect(event_button(page, 26)).to_be_visible()
    expect(catalog(page).get_by_role("button", name="Publishing run", exact=True)).to_have_count(0)
    catalog(page).get_by_role("button", name="Clear filters", exact=True).click()
    expect(catalog(page).get_by_role("combobox", name="Catalog source", exact=True)).to_have_value(
        ""
    )
    expect(event_button(page, 1)).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    for key in ("store_source", "store_run", "store_query", "store_after_start", "store_event"):
        assert key not in params
    page.go_back()
    expect(event_button(page, 26)).to_be_visible()
    assert "store_run" not in parse_qs(urlsplit(page.url).query)


@pytest.mark.parametrize("corruption", ["echo", "missing_echo", "row"])
def test_unconfirmed_run_or_row_provenance_hides_previous_records(
    run_catalog_page, corruption: str
) -> None:
    page, scenario, base = run_catalog_page
    open_run_records(page, base)
    expect(event_button(page, 1)).to_be_visible()
    event_button(page, 1).click()
    expect(catalog(page).get_by_role("button", name="Copy event ID", exact=True)).to_be_visible()
    scenario.corruption = corruption
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(
        catalog(page).get_by_text(
            "The returned events do not match the selected catalog filters. Refresh to retry.",
            exact=True,
        )
    ).to_be_visible()
    expect(event_button(page, 1)).to_have_count(0)
    expect(catalog(page).get_by_role("button", name="Copy event ID", exact=True)).to_have_count(0)
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("—")
    scenario.corruption = None
    catalog(page).get_by_role("button", name="Retry", exact=True).click()
    expect(event_button(page, 1)).to_be_visible()
    expect(event_button(page, 1)).to_have_attribute("aria-expanded", "true")
    assert scenario.event_calls[-1]["run_key"] == [RUN]


def test_failed_unfiltered_read_cannot_reuse_previous_run_scoped_results(run_catalog_page) -> None:
    page, scenario, base = run_catalog_page
    open_run_records(page, base)
    expect(event_button(page, 1)).to_be_visible()
    scenario.event_status = 503
    catalog(page).get_by_role("button", name="Clear run filter", exact=True).click()
    expect(
        catalog(page).get_by_text("Fixture published records unavailable", exact=True)
    ).to_be_visible()
    expect(event_button(page, 1)).to_have_count(0)
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("—")
    assert "run_key" not in scenario.event_calls[-1]
    assert "store_run" not in parse_qs(urlsplit(page.url).query)
    scenario.event_status = 200
    catalog(page).get_by_role("button", name="Retry", exact=True).click()
    expect(event_button(page, 1)).to_be_visible()
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("27")
    page.go_back()
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("25")
    assert parse_qs(urlsplit(page.url).query)["store_run"] == [RUN]


def test_empty_run_does_not_claim_the_whole_source_has_no_events(run_catalog_page) -> None:
    page, scenario, base = run_catalog_page
    open_run_records(page, base, EMPTY_RUN)
    expect(
        catalog(page).get_by_text("No current records remain attributed to this run.", exact=True)
    ).to_be_visible()
    expect(
        catalog(page).get_by_text("This source has no current browseable events.", exact=True)
    ).to_have_count(0)
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("0")
    assert scenario.event_calls[-1]["run_key"] == [EMPTY_RUN]
    catalog(page).get_by_role("textbox", name="Search catalog events").fill("music")
    expect(
        catalog(page).get_by_text(
            "No records attributed to this run match this search.", exact=True
        )
    ).to_be_visible()
    catalog(page).get_by_role("button", name="Clear run filter", exact=True).click()
    expect(catalog(page).get_by_role("textbox", name="Search catalog events")).to_have_value(
        "music"
    )
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("0")
    assert "store_run" not in parse_qs(urlsplit(page.url).query)
    assert parse_qs(urlsplit(page.url).query)["store_query"] == ["music"]
    catalog(page).get_by_role("button", name="Clear event search", exact=True).click()
    expect(event_button(page, 1)).to_be_visible()
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("27")


def test_late_run_response_cannot_replace_current_source_wide_records(run_catalog_page) -> None:
    page, scenario, base = run_catalog_page
    open_run_records(page, base)
    expect(event_button(page, 1)).to_be_visible()
    scenario.held_run = RUN
    with page.expect_request(
        lambda request: (
            urlsplit(request.url).path == "/admin/v1/ingestion/events"
            and parse_qs(urlsplit(request.url).query).get("run_key") == [RUN]
        )
    ):
        page.get_by_role("button", name="Refresh", exact=True).click()
    expect(catalog(page).get_by_text("Loading parsed events…", exact=True)).to_be_visible()
    assert scenario.held_routes
    catalog(page).get_by_role("button", name="Clear run filter", exact=True).click()
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("27")
    scenario.held_run = None
    for route in scenario.held_routes:
        scenario.respond_events(route)
    scenario.held_routes.clear()
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(
        catalog(page)
        .get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("27")
    assert "store_run" not in parse_qs(urlsplit(page.url).query)
    catalog(page).get_by_role("button", name="Next parsed events page", exact=True).click()
    expect(event_button(page, 26)).to_be_visible()


def test_run_filters_preserve_run_until_source_or_run_is_explicitly_cleared(
    run_catalog_page,
) -> None:
    page, scenario, base = run_catalog_page
    open_run_records(page, base)
    store = catalog(page)
    store.get_by_role("combobox", name="Event dates", exact=True).select_option("upcoming")
    store.get_by_role("combobox", name="Event price", exact=True).select_option("free")
    store.get_by_role("textbox", name="Search catalog events").fill("Event 2")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("6")
    event_button(page, 21).click()
    original = page.url
    assert scenario.event_calls[-1]["run_key"] == [RUN]
    assert scenario.event_calls[-1]["date_scope"] == ["upcoming"]
    assert scenario.event_calls[-1]["price_status"] == ["free"]
    store.get_by_role("button", name="Clear run filter", exact=True).click()
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("8")
    params = parse_qs(urlsplit(page.url).query)
    assert params["store_source"] == [SOURCE]
    assert params["store_dates"] == ["upcoming"]
    assert params["store_price"] == ["free"]
    assert params["store_query"] == ["Event 2"]
    assert "store_run" not in params and "store_event" not in params
    page.go_back()
    expect(event_button(page, 21)).to_have_attribute("aria-expanded", "true")
    assert page.url == original
    store.get_by_role("combobox", name="Catalog source", exact=True).select_option("")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("8")
    params = parse_qs(urlsplit(page.url).query)
    assert "store_source" not in params and "store_run" not in params
    assert params["store_dates"] == ["upcoming"]
    assert params["store_price"] == ["free"]
    assert params["store_query"] == ["Event 2"]
    assert "store_event" not in params and "store_after_start" not in params
    page.reload()
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("8")
    assert "source_key" not in scenario.event_calls[-1]
    assert "run_key" not in scenario.event_calls[-1]
