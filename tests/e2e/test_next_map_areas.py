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


@pytest.mark.parametrize("width", [1440, 390])
def test_guest_map_groups_areas_retains_exact_and_unlocated_results_and_paginates(area_map_page, tmp_path, width):
    harness, api = area_map_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    pages = [
        [_event(0, "SOMA"), _event(1, "SOMA"), _event(2, "FiDi (SF)"),
         _event(3, "SOMA", exact=True), _event(4, "Virtual (SF)"), _event(5, "Other")],
        [_event(6, "SOMA")],
    ]
    queries = []

    def catalog(route: Route):
        query = parse_qs(urlsplit(route.request.url).query)
        queries.append(query)
        api.calls.append((route.request.method, "/v1/catalog/events"))
        index = int(query.get("cursor", ["0"])[0])
        api.respond(route, {
            "items": pages[index], "next_cursor": "1" if index == 0 else None,
            "providers": [{"source_key": "tech-week-sf-2026", "label": "SF Tech Week 2026",
                           "display_name": "SF Tech Week 2026", "publisher": "Tech Week",
                           "provider": "www.tech-week.com", "seed_url": "https://www.tech-week.com/calendar/sf", "event_count": 7}],
            "topic_facets": [], "city_facets": [{"city": "sanfrancisco", "event_count": 7}],
        })

    page.route(re.compile(r"/v1/catalog/events(?:\?.*)?$"), catalog)
    page.goto(f"{BASE}/?view=map&source=tech-week-sf-2026&city=sanfrancisco&when=custom&start=2026-10-01&end=2026-10-31&price=any")
    rail = page.locator(".map-preview-rail")
    expect(page.locator(".map-heading")).to_contain_text("4 mapped (3 approximate) · 2 without map locations")
    expect(page.locator(".map-marker--area")).to_have_count(2)
    expect(page.locator(".map-marker:not(.map-marker--area)")).to_have_count(1)
    expect(rail).to_contain_text("Outlined markers show approximate areas, not venues")
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

    page.get_by_role("button", name="Load more", exact=True).click()
    expect(rail.locator(".map-preview")).to_have_count(3)
    expect(page.get_by_role("button", name="Show 3 events near South of Market (approximate area)", exact=True)).to_contain_text("3")
    expect(page.locator(".map-marker")).to_have_count(3)
    assert {key: value for key, value in queries[1].items() if key != "cursor"} == queries[0]
    page.screenshot(path=str(tmp_path / f"map-area-selection-{width}.png"))

    rail.get_by_role("button", name="Show all areas").click()
    expect(rail.locator(".map-preview")).to_have_count(5)
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
