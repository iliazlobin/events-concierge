"""Guest filter suggestions in the built frontend, without provider or account writes."""

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
SEARCH_NAME = "Search events, organizers, hosts, speakers, partners, or add a filter"


@pytest.fixture
def discovery_filters_page(page_factory, request):
    signed_in = getattr(request, "param", False)
    harness = page_factory(authenticated=False)
    api = ConsumerIdentityApi(harness)
    api.install(harness.page)
    queries = []
    events = [
        catalog_event() | {
            "canonical_event_id": f"91000000-0000-4000-8000-{index:012d}",
            "title": f"{city} Tech Week {status}",
            "city": "sanfrancisco" if city == "SF" else "losangeles",
            "start_at": "2030-10-08T18:00:00-07:00", "end_at": None,
            "source_keys": [f"tech-week-{city.lower()}-2026"],
            "registration_status": status,
        }
        for index, (city, status) in enumerate([
            ("SF", "open"), ("SF", "waitlist"), ("SF", "sold_out"),
            ("SF", "unknown"), ("LA", "open"),
        ])
    ]

    def catalog(route: Route):
        query = parse_qs(urlsplit(route.request.url).query)
        queries.append(query)
        api.calls.append((route.request.method, "/v1/catalog/events"))
        source = query.get("source_key", [])
        availability = query.get("availability", [None])[0]
        status = "open" if availability == "available" else "sold_out"
        items = [event for event in events
                 if (not source or any(key in source for key in event["source_keys"]))
                 and (not availability or event["registration_status"] == status)]
        api.respond(route, {
            "items": items, "next_cursor": None,
            # An SF-scoped facet need not contain LA; its starter still has a readable chip.
            "providers": [{"source_key": "tech-week-sf-2026", "label": "SF Tech Week 2026",
                           "display_name": "SF Tech Week 2026", "publisher": "Tech Week",
                           "provider": "www.tech-week.com", "seed_url": "https://www.tech-week.com/calendar/sf",
                           "event_count": 4}],
            "topic_facets": [], "city_facets": [],
        })

    harness.page.route(re.compile(r"/v1/catalog/events(?:\?.*)?$"), catalog)
    if signed_in:
        def account(route: Route):
            api.calls.append((route.request.method, "/v1/me"))
            api.respond(route, {"notify_email": "browser@example.test", "interests": [],
                                "preference_revision": 1, "local_demo": False, "is_admin": False,
                                "relay_inbox": None})

        entries = [{
                "saved_filter_id": f"92000000-0000-4000-8000-{index:012d}",
                "name": f"My saved selection {index}",
                "filters": {"query": "Mira AI in San Francisco this week" if index == 2 else "",
                            "sort": "soonest", "datePreset": "custom" if index == 2 else "all",
                            "customStart": "2030-10-01" if index == 2 else "",
                            "customEnd": "2030-10-31" if index == 2 else "", "dateRanges": [],
                            "sourceKeys": ["tech-week-sf-2026"] if index == 2 else [],
                            "city": "", "cities": [], "locationScopes": [],
                            "topics": [], "price": "any", "priceComparison": "any",
                            "priceMinDollars": "", "priceMaxDollars": "", "availability": "any"},
                "created_at": None, "updated_at": None, "last_used_at": None,
            } for index in range(3)]

        def saved_filters(route: Route):
            api.calls.append((route.request.method, "/v1/me/saved-filters"))
            api.respond(route, entries)

        def applied(route: Route):
            path = urlsplit(route.request.url).path
            assert route.request.method == "POST"
            assert path == f"/v1/me/saved-filters/{entries[2]['saved_filter_id']}/applied"
            api.calls.append(("POST", path))
            api.respond(route, entries[2])

        harness.page.route("**/v1/me", account)
        harness.page.route("**/v1/me/saved-filters", saved_filters)
        harness.page.route("**/v1/me/saved-filters/*/applied", applied)
    yield harness, api, queries
    assert not api.unexpected
    assert all(method == "GET" or (
        signed_in and method == "POST"
        and path == "/v1/me/saved-filters/92000000-0000-4000-8000-000000000002/applied"
    ) for method, path in api.calls)
    assert not any(path.startswith(("/auth/", "/v1/preferences")) for _, path in api.calls)
    if not signed_in:
        assert not any(path.startswith("/v1/me/") for _, path in api.calls)


@pytest.fixture
def catalog_names_page(discovery_filters_page):
    discovery_filters_page[1].catalog_name_suggestions_enabled = True
    return discovery_filters_page


@pytest.mark.parametrize("enabled", [False, None])
def test_disabled_or_older_catalog_names_keep_filters_and_text_search_without_name_requests(
    discovery_filters_page, enabled,
):
    harness, api, queries = discovery_filters_page
    api.catalog_name_suggestions_enabled = enabled
    page = harness.page
    name_requests = []

    def unexpected_names(route: Route):
        name_requests.append(route.request.url)
        api.respond(route, {"detail": "name schema is held"}, 500)

    page.route("**/v1/catalog/name-suggestions?*", unexpected_names)
    page.goto(f"{BASE}/?view=events&source=tech-week-sf-2026")
    expect(page.locator(".event-card")).to_have_count(4)
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    search.fill("SF Tech")
    expect(page.get_by_role("option").filter(has_text="SF Tech Week").first).to_be_visible()
    page.wait_for_timeout(300)
    with page.expect_response(lambda response: (
        urlsplit(response.url).path == "/v1/catalog/events"
        and parse_qs(urlsplit(response.url).query).get("q") == ["Nebius"]
    )):
        search.fill("Nebius")
    search.press("Enter")
    page.wait_for_function("new URL(location.href).searchParams.get('q') === 'Nebius'")
    assert queries[-1]["q"] == ["Nebius"]
    assert queries[-1]["source_key"] == ["tech-week-sf-2026"]
    # Leave the composer active beyond its debounce to detect an accidental names request.
    page.wait_for_timeout(300)
    assert name_requests == []


@pytest.mark.parametrize("width", [1440, 390])
def test_catalog_names_autocomplete_select_and_reload_preserve_filters(catalog_names_page, tmp_path, width):
    harness, api, queries = catalog_names_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    name_queries = []

    def names(route: Route):
        query = parse_qs(urlsplit(route.request.url).query)
        name_queries.append(query)
        api.calls.append((route.request.method, "/v1/catalog/name-suggestions"))
        term = query["q"][0].lower()
        items = [{"name": "Nebius", "kinds": ["host", "organization"], "event_count": 3},
                 {"name": "Nebius Studio", "kinds": ["venue"], "event_count": 1},
                 {"name": "Nebius Builders Night", "kinds": ["event"], "event_count": 1}]
        if term == "mira":
            items = [{"name": "Mira AI in San Francisco this week", "kinds": ["event"], "event_count": 1}]
        api.respond(route, items)

    page.route("**/v1/catalog/name-suggestions?*", names)
    page.goto(f"{BASE}/?view=events&source=tech-week-sf-2026&city=sanfrancisco&when=custom&start=2030-10-01&end=2030-10-31&availability=available")
    # None of the initially loaded event fixtures contains any of the requested names.
    expect(page.locator(".event-card").filter(has_text="Nebius")).to_have_count(0)
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    search.fill("nebius")
    options = page.get_by_role("listbox", name="Suggested filters").get_by_role("option")
    expect(options).to_have_count(3)
    expect(options.first).to_contain_text("Organization")
    expect(options.nth(1)).to_contain_text("Venue")
    expect(options.nth(2)).to_contain_text("Event")
    page.screenshot(path=str(tmp_path / f"catalog-name-suggestions-{width}.png"), animations="disabled")
    search.press("ArrowDown")
    search.press("Enter")
    expect(search).to_have_value("Nebius")
    expect(page.get_by_role("listbox", name="Suggested filters")).not_to_be_visible()
    expect(page.locator(".active-filter-chip").filter(has_text="SF Tech Week 2026")).to_be_visible()
    page.wait_for_function("new URL(location.href).searchParams.get('q') === 'Nebius'")
    for query in (name_queries[-1], queries[-1]):
        assert query["source_key"] == ["tech-week-sf-2026"]
        assert query["city"] == ["sanfrancisco"]
        assert query["availability"] == ["available"]
        assert "starts_after" in query and "starts_before" in query
    page.reload()
    expect(page.get_by_role("combobox", name=SEARCH_NAME)).to_have_value("Nebius")
    assert parse_qs(urlsplit(page.url).query)["source"] == ["tech-week-sf-2026"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    # An event name can contain filter words. Selecting/reloading it must keep the literal name.
    search.fill("mira")
    option = page.get_by_role("option").filter(has_text="Mira AI in San Francisco this week")
    expect(option).to_be_visible()
    option.click()
    expect(search).to_have_value("Mira AI in San Francisco this week")
    page.reload()
    expect(page.get_by_role("combobox", name=SEARCH_NAME)).to_have_value("Mira AI in San Francisco this week")
    page.get_by_role("combobox", name=SEARCH_NAME).press("Enter")
    query = parse_qs(urlsplit(page.url).query)
    assert query["when"] == ["custom"] and query["start"] == ["2030-10-01"]
    assert query["q"] == ["Mira AI in San Francisco this week"]
    assert "topic" not in query


@pytest.mark.parametrize("discovery_filters_page", [True], indirect=True)
def test_restored_saved_name_stays_literal_after_history_and_enter(discovery_filters_page):
    harness, _, _ = discovery_filters_page
    page = harness.page
    page.goto(f"{BASE}/?view=events")
    expect(page.get_by_role("button", name="Saved · 3", exact=True)).to_be_visible()
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    search.fill("My saved selection 2")
    page.get_by_role("option").filter(has_text="My saved selection 2").click()
    expect(search).to_have_value("Mira AI in San Francisco this week")
    search.press("Enter")
    query = parse_qs(urlsplit(page.url).query)
    assert query["q"] == ["Mira AI in San Francisco this week"]
    assert query["source"] == ["tech-week-sf-2026"] and query["when"] == ["custom"]
    assert query["start"] == ["2030-10-01"] and "topic" not in query
    page.go_back()
    page.go_forward()
    expect(page.get_by_role("combobox", name=SEARCH_NAME)).to_have_value("Mira AI in San Francisco this week")
    page.get_by_role("combobox", name=SEARCH_NAME).press("Enter")
    query = parse_qs(urlsplit(page.url).query)
    assert query["q"] == ["Mira AI in San Francisco this week"]
    assert query["start"] == ["2030-10-01"] and "topic" not in query


def test_catalog_name_failure_and_old_response_do_not_replace_current_suggestions(catalog_names_page):
    harness, api, _ = catalog_names_page
    page = harness.page
    held = []

    def names(route: Route):
        term = parse_qs(urlsplit(route.request.url).query)["q"][0]
        api.calls.append((route.request.method, "/v1/catalog/name-suggestions"))
        if term == "nebius":
            held.append(route)
        elif term == "mira":
            api.respond(route, [{"name": "Mira Developer", "kinds": ["speaker", "person"], "event_count": 2}])
        else:
            api.respond(route, {"detail": "temporarily unavailable"}, 503)

    page.route("**/v1/catalog/name-suggestions?*", names)
    page.goto(f"{BASE}/?view=events")
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    with page.expect_request("**/v1/catalog/name-suggestions?*q=nebius*"):
        search.fill("nebius")
    search.fill("mira")
    expect(page.get_by_role("option").filter(has_text="Mira Developer")).to_be_visible()
    api.respond(held[0], [{"name": "Nebius", "kinds": ["organization"], "event_count": 3}])
    expect(page.get_by_role("option").filter(has_text="Nebius")).to_have_count(0)
    search.fill("SF Tech")
    # A failed names request leaves existing source suggestions and normal text search usable.
    expect(page.get_by_role("option").filter(has_text="SF Tech Week").first).to_be_visible()
    expect(page.get_by_role("option").filter(has_text="Mira Developer")).to_have_count(0)
    search.fill("")
    options = page.get_by_role("listbox", name="Useful starting points").get_by_role("option")
    expect(options.nth(0)).to_contain_text("SF Tech Week 2026")
    expect(options.nth(1)).to_contain_text("LA Tech Week 2026")


@pytest.mark.parametrize("width", [1440, 390])
def test_companies_outside_filters_remain_discoverable_without_changing_event_filters(
    catalog_names_page, tmp_path, width,
):
    harness, api, queries = catalog_names_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    name_queries = []

    def names(route: Route):
        query = parse_qs(urlsplit(route.request.url).query)
        name_queries.append(query)
        api.calls.append((route.request.method, "/v1/catalog/name-suggestions"))
        filtered = "starts_after" in query
        api.respond(route, [] if filtered else [
            {"name": "Nebius", "kinds": ["organization"], "event_count": 7},
        ])

    page.route("**/v1/catalog/name-suggestions?*", names)
    page.goto(f"{BASE}/?view=events&source=tech-week-sf-2026&city=sanfrancisco&when=custom"
              "&start=2030-10-09&end=2030-10-11&price=free&topic=ai&availability=available")
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    search.fill("nebi")
    option = page.get_by_role("option").filter(has_text="Nebius")
    expect(option).to_be_visible()
    expect(page.get_by_role("status").filter(has_text="Showing catalog matches")).to_be_visible()
    expect(option).to_contain_text("7 catalog events")
    assert name_queries[-1] == {"q": ["nebi"], "limit": ["8"]}
    page.screenshot(path=str(tmp_path / f"company-outside-filters-{width}.png"), animations="disabled")
    search.press("ArrowDown")
    search.press("Enter")
    expect(search).to_have_value("Nebius")
    page.wait_for_function("new URL(location.href).searchParams.get('q') === 'Nebius'")
    for query in (name_queries[0], queries[-1]):
        assert query["source_key"] == ["tech-week-sf-2026"]
        assert query["city"] == ["sanfrancisco"]
        assert query["price"] == ["free"]
        assert query["topic"] == ["ai"]
        assert query["availability"] == ["available"]
        assert "starts_after" in query and "starts_before" in query
    page.reload()
    expect(page.get_by_role("combobox", name=SEARCH_NAME)).to_have_value("Nebius")
    assert parse_qs(urlsplit(page.url).query)["start"] == ["2030-10-09"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_empty_loading_and_failed_name_reads_are_visible(catalog_names_page):
    harness, api, _ = catalog_names_page
    page = harness.page
    held = []

    def names(route: Route):
        query = parse_qs(urlsplit(route.request.url).query)
        api.calls.append((route.request.method, "/v1/catalog/name-suggestions"))
        if query["q"] == ["nebi"]:
            held.append(route)
        elif query["q"] == ["unknown-company"]:
            api.respond(route, [])
        else:
            api.respond(route, {"detail": "unavailable"}, 503)

    page.route("**/v1/catalog/name-suggestions?*", names)
    page.goto(f"{BASE}/?view=events")
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    with page.expect_request("**/v1/catalog/name-suggestions?*q=nebi*"):
        search.fill("nebi")
        expect(page.get_by_role("status").filter(has_text="Searching names")).to_be_visible()
    search.fill("unknown-company")
    expect(page.get_by_role("status").filter(has_text="No matching names")).to_be_visible()
    for route in held:
        api.respond(route, [{"name": "Nebius", "kinds": ["organization"], "event_count": 7}])
    expect(page.get_by_role("option").filter(has_text="Nebius")).to_have_count(0)
    harness.allowed_console_error_fragments.append("the server responded with a status of 503")
    search.fill("missing-service")
    expect(page.get_by_role("status").filter(has_text="Name suggestions unavailable")).to_be_visible()
    # Failure is distinct from empty results; normal free-text search still works.
    search.press("Enter")
    page.wait_for_function("new URL(location.href).searchParams.get('q') === 'missing-service'")


@pytest.mark.parametrize("width", [1440, 390])
def test_tech_week_starters_lead_and_replace_inherited_filters(discovery_filters_page, tmp_path, width):
    harness, _, queries = discovery_filters_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=events&city=sanfrancisco&price=free&topic=ai&when=custom&start=2030-10-01&end=2030-10-31")
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    search.click()
    options = page.get_by_role("listbox", name="Useful starting points").get_by_role("option")
    expect(options).to_have_count(8)
    expect(options.nth(0)).to_contain_text("SF Tech Week 2026")
    expect(options.nth(1)).to_contain_text("LA Tech Week 2026")
    page.screenshot(path=str(tmp_path / f"tech-week-starters-{width}.png"), animations="disabled")

    for city in ("SF", "LA"):
        source_key = f"tech-week-{city.lower()}-2026"
        page.get_by_role("option", name=f"{city} Tech Week 2026", exact=False).click()
        chip = page.locator(".active-filter-chip").filter(has_text=f"{city} Tech Week 2026")
        expect(chip).to_be_visible()
        expect(page.locator(".event-card")).to_have_count(4 if city == "SF" else 1)
        assert queries[-1]["source_key"] == [source_key]
        assert not set(queries[-1]) & {"city", "topic", "starts_after", "starts_before", "availability", "price"}
        query = parse_qs(urlsplit(page.url).query)
        assert query["source"] == [source_key]
        assert query["when"] == ["all"]
        assert query.get("price", ["any"]) == ["any"]
        assert not set(query) & {"city", "topic", "start", "end"}
        search.press("Tab")
        search.click()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.reload()
    expect(page.locator(".active-filter-chip").filter(has_text="LA Tech Week 2026")).to_be_visible()
    expect(page.locator(".event-card")).to_have_count(1)


@pytest.mark.parametrize("discovery_filters_page", [True], indirect=True)
@pytest.mark.parametrize("width", [1440, 390])
def test_tech_week_leads_saved_selections_without_hiding_typed_matches(discovery_filters_page, tmp_path, width):
    harness, _, _ = discovery_filters_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=events")
    expect(page.get_by_role("button", name="Saved · 3", exact=True)).to_be_visible()
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    search.click()
    options = page.get_by_role("listbox", name="Useful starting points").get_by_role("option")
    expect(options.nth(2)).to_contain_text("My saved selection 0")
    expect(options.nth(0)).to_contain_text("SF Tech Week 2026")
    expect(options.nth(1)).to_contain_text("LA Tech Week 2026")
    page.screenshot(path=str(tmp_path / f"tech-week-with-saved-{width}.png"), animations="disabled")
    search.fill("My saved selection 1")
    expect(page.get_by_role("listbox", name="Suggested filters").get_by_role("option").first).to_contain_text("My saved selection 1")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [1440, 390])
def test_registration_open_is_searchable_editable_and_retained(discovery_filters_page, tmp_path, width):
    harness, _, queries = discovery_filters_page
    page = harness.page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{BASE}/?view=events&source=tech-week-sf-2026&when=all&price=any")
    expect(page.locator(".event-card")).to_have_count(4)
    search = page.get_by_role("combobox", name=SEARCH_NAME)
    search.fill("registration still open")
    page.get_by_role("option", name="Registration open", exact=False).click()
    chip = page.locator(".active-filter-chip").filter(has_text="Registration")
    expect(chip).to_contain_text("Open")
    expect(page.locator(".event-card")).to_have_count(1)
    expect(page.locator(".event-card")).to_contain_text("SF Tech Week open")
    assert queries[-1]["availability"] == ["available"]
    assert queries[-1]["source_key"] == ["tech-week-sf-2026"]
    assert "q" not in queries[-1]
    assert parse_qs(urlsplit(page.url).query)["availability"] == ["available"]
    page.get_by_role("button", name="Map", exact=True).click()
    expect(page.get_by_role("heading", name="Event map", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["availability"] == ["available"]
    page.reload()
    expect(chip).to_contain_text("Open")
    page.get_by_role("button", name="Events", exact=True).click()
    expect(page.locator(".event-card")).to_have_count(1)
    page.screenshot(path=str(tmp_path / f"registration-open-{width}.png"), animations="disabled")

    chip.get_by_role("button", name=re.compile("Registration.*Open")).click()
    dialog = page.get_by_role("dialog", name="Edit registration", exact=True)
    dialog.get_by_role("option", name="Sold out", exact=True).click()
    expect(chip).to_contain_text("Sold out")
    expect(page.locator(".event-card")).to_contain_text("SF Tech Week sold_out")
    assert queries[-1]["availability"] == ["sold_out"]
    page.get_by_role("button", name="Remove registration filter", exact=True).click()
    expect(page.locator(".event-card")).to_have_count(4)
    assert "availability" not in queries[-1]

    page.get_by_role("button", name="Add registration", exact=True).click()
    page.get_by_role("dialog", name="Add registration", exact=True).get_by_role("option", name="Registration open", exact=True).click()
    expect(chip).to_contain_text("Open")
    expect(page.locator(".event-card")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
