"""The deployed neighborhood-only shape stays useful without inventing venue coordinates."""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Route, expect
from tests.e2e.test_next_consumer_identity import ConsumerIdentityApi
from tests.e2e.test_next_release_profile import catalog_event

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(not BASE, reason="Set EC_CONSUMER_WEB_URL or EC_ADMIN_WEB_URL"),
]


def _event(index: int, venue: str, *, exact: bool = False) -> dict:
    return catalog_event() | {
        "canonical_event_id": f"90000000-0000-4000-8000-{index:012d}",
        "title": f"Area fixture event {index}",
        "city": "sanfrancisco", "venue_name": venue,
        "start_at": f"2026-10-{8 + index % 2:02d}T18:00:00-07:00", "end_at": None,
        "latitude": 37.781 if exact else None, "longitude": -122.408 if exact else None,
        "source_keys": ["tech-week-sf-2026"], "calendar_labels": ["SF Tech Week 2026"],
        "sources": [{"source_key": "tech-week-sf-2026", "source": "public_jsonld",
                     "label": "SF Tech Week 2026", "registration_url": "https://www.tech-week.com/calendar/sf"}],
    }


@pytest.fixture
def area_map_page(page_factory):
    harness = page_factory(authenticated=False)
    api = ConsumerIdentityApi(harness)
    api.install(harness.page)
    yield harness, api
    assert not api.unexpected
    assert all(method == "GET" for method, _ in api.calls)


def _install_area_pages(page, api, pages):
    queries = []
    total = sum(map(len, pages))

    def catalog(route: Route):
        query = parse_qs(urlsplit(route.request.url).query)
        queries.append(query)
        api.calls.append((route.request.method, "/v1/catalog/events"))
        index = int(query.get("cursor", ["0"])[0])
        api.respond(route, {
            "items": pages[index], "next_cursor": str(index + 1) if index + 1 < len(pages) else None,
            "providers": [{"source_key": "tech-week-sf-2026", "label": "SF Tech Week 2026",
                           "display_name": "SF Tech Week 2026", "publisher": "Tech Week",
                           "provider": "www.tech-week.com", "seed_url": "https://www.tech-week.com/calendar/sf", "event_count": total}],
            "topic_facets": [], "city_facets": [{"city": "sanfrancisco", "event_count": total}],
        })

    page.route(re.compile(r"/v1/catalog/events(?:\?.*)?$"), catalog)
    page.route(re.compile(r"/v1/catalog/events/summary(?:\?.*)?$"), lambda route: api.respond(route, {
        "days": [{"start_day": "2026-10-08", "event_count": total, "topics": []}],
        "total_event_count": total, "time_zone": "America/Los_Angeles",
    }))
    return queries


@pytest.mark.parametrize("width", [1440, 390])
def test_guest_map_groups_areas_retains_exact_and_unlocated_results_and_paginates(area_map_page, tmp_path, width):
    harness, api = area_map_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    queries = _install_area_pages(page, api, [
        [_event(0, "SOMA"), _event(1, "SOMA"), _event(2, "FiDi (SF)"),
         _event(3, "SOMA", exact=True), _event(4, "Virtual (SF)"), _event(5, "Other")],
        [_event(6, "SOMA")],
        [_event(7, "SOMA")],
    ])
    page.goto(f"{BASE}/?view=map&source=tech-week-sf-2026&city=sanfrancisco&when=custom&start=2026-10-01&end=2026-10-31&price=any")
    rail = page.locator(".map-preview-rail")
    expect(page.locator(".map-heading")).to_contain_text("4 mapped (3 approximate) · 2 without map locations")
    expect(page.locator(".map-heading")).to_contain_text("6 of 8 loaded")
    expect(page.locator(".map-marker--area")).to_have_count(2)
    expect(page.locator(".map-marker:not(.map-marker--area)")).to_have_count(1)
    expect(rail.locator(".map-preview-rail__notice")).to_have_count(0)
    expect(rail.locator(".map-preview")).to_have_count(4)
    soma = page.get_by_role("button", name="Show 2 events near South of Market (approximate area)", exact=True)
    expect(soma).to_be_visible()
    marker_element = soma.element_handle()
    soma.click()
    expect(rail.locator(".map-preview")).to_have_count(2)
    expect(rail.get_by_role("button", name="Show all areas")).to_be_visible()
    rail.get_by_role("button", name="Focus Area fixture event 1 on map", exact=True).click()
    expect(page.locator(".map-selection")).to_contain_text("Approximate area only")
    expect(page.locator(".map-selection .event-card")).to_contain_text("Area fixture event 1")
    expect(soma).to_have_attribute("aria-pressed", "true")
    assert marker_element and marker_element.get_attribute("aria-pressed") == "true"

    page.get_by_role("button", name="Load remaining events", exact=True).click()
    expect(rail.locator(".map-preview")).to_have_count(4)
    expect(page.get_by_role("button", name="Show 4 events near South of Market (approximate area)", exact=True)).to_contain_text("4")
    expect(rail.get_by_role("button", name="Focus Area fixture event 7 on map", exact=True)).to_be_visible()
    expect(page.locator(".map-heading")).to_contain_text("8 loaded")
    expect(page.get_by_role("button", name="Load remaining events", exact=True)).to_have_count(0)
    expect(page.locator(".map-marker")).to_have_count(3)
    assert len(queries) == 3
    assert queries[0]["limit"] == ["72"]
    for query in queries[1:]:
        assert query["limit"] == ["100"]
        assert query["include_facets"] == ["false"]
        assert {key: value for key, value in query.items() if key not in {"cursor", "limit", "include_facets"}} == {
            key: value for key, value in queries[0].items() if key != "limit"
        }
    page.screenshot(path=str(tmp_path / f"map-area-selection-{width}.png"))

    rail.get_by_role("button", name="Show all areas").click()
    expect(rail.locator(".map-preview")).to_have_count(6)
    rail.get_by_role("button", name="Focus Area fixture event 3 on map", exact=True).click()
    expect(page.locator(".map-selection__location-note")).to_have_count(0)
    expect(page.locator(".map-selection .event-card")).to_contain_text("Area fixture event 3")
    rail.get_by_role("button", name=re.compile(r"^Without map locations")).click()
    expect(rail.locator(".map-preview")).to_have_count(2)
    expect(rail).to_contain_text("Virtual (SF)")
    expect(rail.get_by_role("button", name="Show details for Area fixture event 5", exact=True)).to_be_visible()
    expect(page.locator(".map-marker")).to_have_count(3)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not any(path.startswith(("/auth/", "/v1/me/", "/v1/preferences")) for _, path in api.calls)
    page.screenshot(path=str(tmp_path / f"map-unlocated-{width}.png"))


def test_map_load_remaining_preserves_successful_pages_and_retries_failed_cursor(area_map_page):
    harness, api = area_map_page
    page = harness.page
    cursors = []

    def catalog(route: Route):
        cursor = parse_qs(urlsplit(route.request.url).query).get("cursor", [None])[0]
        cursors.append(cursor)
        if cursor == "2" and cursors.count("2") == 1:
            harness.allowed_console_error_fragments.append("503")
            api.respond(route, {"detail": "Page temporarily unavailable"}, 503)
            return
        index = int(cursor or "0")
        api.respond(route, {"items": [_event(index, "SOMA")], "providers": [],
                            "next_cursor": str(index + 1) if index < 2 else None})

    page.route(re.compile(r"/v1/catalog/events(?:\?.*)?$"), catalog)
    page.goto(f"{BASE}/?view=map&when=custom&start=2026-10-01&end=2026-10-31&price=any")
    expect(page.locator(".map-heading")).to_contain_text("1 loaded")
    page.get_by_role("button", name="Load remaining events", exact=True).click()
    expect(page.locator(".map-view").get_by_role("alert")).to_contain_text("Page temporarily unavailable")
    expect(page.locator(".map-heading")).to_contain_text("2 loaded")
    page.get_by_role("button", name="Load remaining events", exact=True).click()
    expect(page.locator(".map-heading")).to_contain_text("3 loaded")
    expect(page.locator(".map-view").get_by_role("alert")).to_have_count(0)
    expect(page.get_by_role("button", name="Load remaining events", exact=True)).to_have_count(0)
    assert cursors == [None, "1", "2", "2"]


def test_map_discards_pending_batch_and_count_when_view_changes(area_map_page):
    harness, api = area_map_page
    page = harness.page
    held_pages = []
    held_counts = []
    cursors = []

    def catalog(route: Route):
        cursor = parse_qs(urlsplit(route.request.url).query).get("cursor", [None])[0]
        cursors.append(cursor)
        if cursor:
            held_pages.append(route)
        else:
            api.respond(route, {"items": [_event(0, "SOMA")], "next_cursor": "1", "providers": []})

    def hold_count(route: Route):
        held_counts.append(route)

    page.route(re.compile(r"/v1/catalog/events(?:\?.*)?$"), catalog)
    page.route(re.compile(r"/v1/catalog/events/summary(?:\?.*)?$"), hold_count)
    page.goto(f"{BASE}/?view=map&when=custom&start=2026-10-01&end=2026-10-31&price=any")
    expect(page.locator(".map-heading")).to_contain_text("1 loaded")
    page.get_by_role("button", name="Load remaining events", exact=True).click()
    expect(page.get_by_role("button", name="Loading remaining events", exact=True)).to_be_disabled()
    assert held_pages and held_counts
    page.locator(".site-nav").get_by_role("button", name="Events", exact=True).click()
    expect(page.get_by_role("heading", name="Area fixture event 0", exact=True)).to_be_visible()
    with page.expect_response(lambda response: "cursor=1" in response.url) as pending_page:
        api.respond(held_pages[0], {"items": [_event(1, "SOMA")], "next_cursor": "2", "providers": []})
    pending_page.value.finished()
    for route in held_counts:
        api.respond(route, {"days": [], "total_event_count": 500, "time_zone": "America/Los_Angeles"})
    # Allow the fulfilled fetch and React render to settle without depending on map tile traffic.
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    expect(page.locator(".filter-bar__actions [aria-live]")).to_have_text("1 loaded · more available")
    expect(page.get_by_role("heading", name="Area fixture event 1", exact=True)).to_have_count(0)
    assert "2" not in cursors
