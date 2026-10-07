"""Built discovery UI filter transport, date boundaries, and cursor regressions."""

from __future__ import annotations

import os
import re
from datetime import datetime
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
NOW = "2026-10-07T16:30:00Z"  # Wednesday, 9:30 AM in San Francisco.
LATER = "2026-10-07T17:45:00Z"
SEARCH_NAME = "Search events, organizers, hosts, speakers, partners, or add a filter"


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
def catalog_filters_page(page_factory):
    harness = page_factory(authenticated=False)
    page = harness.page
    page.set_default_timeout(5_000)
    page.clock.set_fixed_time(_instant(NOW))
    api = ConsumerIdentityApi(harness)
    api.install(page)
    queries: list[tuple[str, dict[str, list[str]]]] = []
    state = {"paginate": False}
    events = [
        catalog_event() | {
            "canonical_event_id": f"93000000-0000-4000-8000-{index:012d}",
            "title": title,
            "start_at": start,
            "end_at": end,
            "city": "sanfrancisco",
            "topics": ["ai", "networking"],
            "source_keys": ["tech-week-sf-2026"],
        }
        for index, (title, start, end) in enumerate([
            ("Monday multi-day event", "2026-10-05T07:00:00Z", "2026-10-12T06:45:00Z"),
            ("Wednesday earlier event", "2026-10-07T15:00:00Z", "2026-10-07T17:00:00Z"),
            ("Wednesday current event", NOW, "2026-10-07T18:00:00Z"),
            ("Sunday last event", "2026-10-12T06:30:00Z", "2026-10-12T06:45:00Z"),
            ("Next Monday event", "2026-10-12T07:00:00Z", None),
        ])
    ]

    def catalog(route: Route) -> None:
        path = urlsplit(route.request.url).path
        query = parse_qs(urlsplit(route.request.url).query)
        queries.append((path, query))
        api.calls.append((route.request.method, path))
        if path.endswith("/summary"):
            api.respond(route, {
                "days": [{"start_day": "2026-10-07", "event_count": 1,
                          "topics": [{"topic": "ai", "label": "AI", "event_count": 1}]}],
                "total_event_count": 1, "time_zone": "America/Los_Angeles",
            })
            return
        ranges = query.get("date_range", [])
        if ranges:
            bounds = [tuple(_instant(value) for value in interval.split("..")) for interval in ranges]
        else:
            bounds = [(
                _instant(query.get("starts_after", [NOW])[0]),
                _instant(query.get("starts_before", ["2027-01-01T00:00:00Z"])[0]),
            )]
        # Fixtures apply start-time semantics; service-backed tests own SQL eligibility.
        items = [event for event in events
                 if any(start <= _instant(event["start_at"]) < end for start, end in bounds)]
        next_cursor = None
        if state["paginate"] and len(items) > 1 and query.get("limit") != ["1"]:
            if "cursor" in query:
                items = items[1:]
            else:
                items = items[:1]
                next_cursor = "fixture-next"
        api.respond(route, {
            "items": items, "next_cursor": next_cursor,
            "providers": [{"source_key": "tech-week-sf-2026", "label": "SF Tech Week 2026",
                           "display_name": "SF Tech Week 2026", "publisher": "Tech Week",
                           "provider": "www.tech-week.com", "seed_url": "https://www.tech-week.com/calendar/sf",
                           "event_count": len(items)}],
            "topic_facets": [{"topic": "ai", "label": "AI", "event_count": len(items)},
                             {"topic": "networking", "label": "Networking", "event_count": len(items)}],
            "city_facets": [{"city": "sanfrancisco", "event_count": len(items)}],
        })

    page.route(re.compile(r"/v1/catalog/events(?:/summary)?(?:\?.*)?$"), catalog)
    yield harness, queries, state
    assert not api.unexpected
    assert all(method == "GET" for method, _ in api.calls)
    assert not any(path.startswith(("/auth/", "/v1/preferences", "/v1/me/")) for _, path in api.calls)


@pytest.mark.parametrize("width", [1440, 390])
def test_this_week_uses_today_through_sunday_without_prior_starts(catalog_filters_page, tmp_path, width):
    harness, queries, _ = catalog_filters_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=events&source=tech-week-sf-2026&when=week")
    expect(page.locator(".event-card")).to_have_count(2)
    expect(page.get_by_role("heading", name="Wednesday current event", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Sunday last event", exact=True)).to_be_visible()
    query = queries[-1][1]
    assert _instant(query["starts_after"][0]) == _instant(NOW)
    assert _instant(query["starts_before"][0]) == _instant("2026-10-12T07:00:00Z")
    page.locator(".active-filter-chip--date").get_by_role("button", name="Date This week").click()
    dialog = page.get_by_role("dialog", name="Edit date range")
    expect(dialog).to_contain_text("Oct 7, 2026")
    expect(dialog).to_contain_text("Oct 11, 2026")
    expect(dialog.get_by_role("button", name="This week", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(dialog).to_have_css("opacity", "1")
    page.screenshot(path=str(tmp_path / f"this-week-filter-{width}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_typed_topic_and_this_week_keeps_a_rolling_preset(catalog_filters_page):
    harness, queries, _ = catalog_filters_page
    page = harness.page
    page.goto(f"{BASE}/?view=events&source=tech-week-sf-2026&when=all")
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    search.fill("AI events this week")
    search.press("Enter")
    expect(page.locator(".active-filter-chip--date")).to_contain_text("This week")
    expect(page.locator(".event-card")).to_have_count(2)
    query = queries[-1][1]
    assert query["topic"] == ["ai"]
    assert query["source_key"] == ["tech-week-sf-2026"]
    assert _instant(query["starts_after"][0]) == _instant(NOW)
    assert not set(query) & {"q", "date_range"}
    assert parse_qs(urlsplit(page.url).query)["when"] == ["week"]
    page.clock.set_fixed_time(_instant(LATER))
    page.reload()
    expect(page.locator(".active-filter-chip--date")).to_contain_text("This week")
    expect(page.locator(".event-card")).to_have_count(1)
    assert _instant(queries[-1][1]["starts_after"][0]) == _instant(LATER)


def test_pagination_reuses_first_window_and_new_selection_refreshes_it(catalog_filters_page):
    harness, queries, state = catalog_filters_page
    state["paginate"] = True
    page = harness.page
    page.goto(f"{BASE}/?view=events&source=tech-week-sf-2026&when=week")
    expect(page.locator(".event-card")).to_have_count(1)
    first = queries[-1][1]
    page.clock.set_fixed_time(_instant(LATER))
    page.get_by_role("button", name="More events", exact=True).click()
    expect(page.locator(".event-card")).to_have_count(2)
    continuation = queries[-1][1]
    assert continuation["cursor"] == ["fixture-next"]
    assert continuation["starts_after"] == first["starts_after"]
    assert continuation["starts_before"] == first["starts_before"]
    page.locator(".active-filter-chip--date").get_by_role("button", name="Date This week").click()
    page.get_by_role("dialog", name="Edit date range").get_by_role("button", name="Next week", exact=True).click()
    expect(page.get_by_role("heading", name="Next Monday event", exact=True)).to_be_visible()
    assert "cursor" not in queries[-1][1]
    assert _instant(queries[-1][1]["starts_after"][0]) == _instant("2026-10-12T07:00:00Z")
    page.locator(".active-filter-chip--date").get_by_role("button", name="Date Next week").click()
    page.get_by_role("dialog", name="Edit date range").get_by_role("button", name="This week", exact=True).click()
    expect(page.locator(".event-card")).to_have_count(1)
    assert _instant(queries[-1][1]["starts_after"][0]) == _instant(LATER)
    assert "cursor" not in queries[-1][1]


def test_combined_filters_survive_views_reload_and_calendar_day_reads(catalog_filters_page):
    harness, queries, _ = catalog_filters_page
    page = harness.page
    page.goto(f"{BASE}/?view=events&when=custom&date_range=2026-10-07..2026-10-11"
              "&source=tech-week-sf-2026&source=luma-sf&city=sanfrancisco&city=oakland"
              "&area=bay_area&topic=ai&topic=networking&q=Alex&price=paid"
              "&cmp=between&min=10.25&max=25.50&availability=available")
    expect(page.locator(".event-card")).to_have_count(3)
    expected = {"source_key": ["tech-week-sf-2026", "luma-sf"],
                "city": ["sanfrancisco", "oakland"], "location_scope": ["bay_area"],
                "topic": ["ai", "networking"], "q": ["Alex"], "price": ["paid"],
                "price_min_cents": ["1025"], "price_max_cents": ["2550"],
                "availability": ["available"]}
    nav = page.locator(".site-nav")
    nav.get_by_role("button", name="Map", exact=True).click()
    expect(page.get_by_role("heading", name="Event map", exact=True)).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name="Event map", exact=True)).to_be_visible()
    nav.get_by_role("button", name="Calendar", exact=True).click()
    day = page.get_by_role("gridcell", name=re.compile(r"Wednesday, October 7: 1 event"))
    expect(day).to_be_visible()
    day.click()
    expect(page.locator(".calendar-agenda").get_by_role("heading", name="Wednesday current event", exact=True)).to_be_visible()
    assert any(path.endswith("/summary") for path, _ in queries)
    day_queries = [query for _, query in queries if query.get("include_facets") == ["false"]]
    assert day_queries
    assert day_queries[-1]["date_range"] == ["2026-10-07T07:00:00.000Z..2026-10-08T07:00:00.000Z"]
    for _, query in queries:
        assert {key: query.get(key) for key in expected} == expected


@pytest.mark.parametrize("width", [1440, 390])
def test_zero_price_suggestion_and_price_editor_maximum(catalog_filters_page, width):
    harness, queries, _ = catalog_filters_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=events&when=week")
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    search.fill("$0")
    page.get_by_role("option", name="Free", exact=False).click()
    chip = page.locator(".active-filter-chip").filter(has_text="Price")
    expect(chip).to_contain_text("Free")
    assert queries[-1][1]["price"] == ["free"]
    assert "price_max_cents" not in queries[-1][1]
    chip.get_by_role("button", name="Price Free").click()
    dialog = page.get_by_role("dialog", name="Edit price")
    dialog.get_by_role("group", name="Ticket type").get_by_role("button", name="Any price", exact=True).click()
    dialog.get_by_role("button", name="Up to", exact=True).click()
    dialog.get_by_role("textbox", name="Price", exact=True).fill("1000001")
    dialog.get_by_role("textbox", name="Price", exact=True).press("Escape")
    expect(dialog).not_to_be_visible()
    expect(chip).to_contain_text("Free")
    assert queries[-1][1]["price"] == ["free"]
    chip.get_by_role("button", name="Price Free").click()
    dialog.get_by_role("group", name="Ticket type").get_by_role("button", name="Any price", exact=True).click()
    dialog.get_by_role("button", name="Up to", exact=True).click()
    dialog.get_by_role("textbox", name="Price", exact=True).fill("1000000")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    dialog.get_by_role("textbox", name="Price", exact=True).press("Escape")
    expect(chip).to_contain_text("≤ $1000000")
    assert queries[-1][1]["price_max_cents"] == ["100000000"]
    assert "price" not in queries[-1][1]
