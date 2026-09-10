"""Current Catalog collection status stays independent of event and publication scopes."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, Route, expect
from tests.e2e.test_admin_operations import OperationsApi
from tests.e2e.test_admin_workspaces import source_record

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]

_NOW = "2026-09-08T12:00:00Z"


def _due(hours: float) -> str:
    return (
        (datetime.fromisoformat(_NOW.replace("Z", "+00:00")) + timedelta(hours=hours))
        .isoformat()
        .replace("+00:00", "Z")
    )


def collection_roster() -> list[dict[str, Any]]:
    rows = [source_record(number) for number in range(1, 10)]
    for number, row in enumerate(rows, 1):
        row.update(
            last_succeeded_at=f"2026-09-{number:02}T12:00:00Z",
            next_due_at=_due((-2, -1, 0.5, 2, 18, 30, 1, 1, 1)[number - 1]),
        )
        row["latest_run"].update(started_at=_due(-1), completed_at=_due(-0.9))
    rows[0].update(due=True, effective_status="due")
    rows[1].update(due=True, effective_status="running")
    rows[1]["latest_run"].update(status="running", completed_at=None)
    rows[2].update(last_succeeded_at=None)
    rows[6].update(
        enabled=False, effective_status="disabled", last_succeeded_at="2000-01-01T00:00:00Z"
    )
    rows[7].update(retired_at="2026-09-01T00:00:00Z", effective_status="retired")
    rows[8].update(review_status="unreviewed", effective_status="unreviewed")
    return rows


@dataclass
class CatalogCollectionApi(OperationsApi):
    catalog_history_available: bool = True
    registry_status: int = 200
    registry_calls: list[dict[str, list[str]]] = field(default_factory=list)
    registry_rows: list[dict[str, Any]] = field(default_factory=collection_roster)

    def handle(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        if path != "/admin/v1/ingestion/sources":
            super().handle(route)
            return
        assert route.request.method == "GET", "Collection status fixtures forbid writes"
        self.calls.append(("GET", path))
        params = parse_qs(urlsplit(route.request.url).query)
        self.registry_calls.append(params)
        if self.registry_status != 200:
            self.respond(
                route, {"detail": "Fixture current registry unavailable"}, self.registry_status
            )
            return
        rows = self.registry_rows
        if query := params.get("query", [""])[0].casefold():
            rows = [
                row
                for row in rows
                if query
                in f"{row['source_key']} {row['display_name']} {row['publisher']}".casefold()
            ]
        offset = int(params.get("offset", ["0"])[0])
        limit = int(params.get("limit", ["100"])[0])
        self.respond(
            route,
            {
                "items": rows[offset : offset + limit],
                "total": len(rows),
                "limit": limit,
                "offset": offset,
            },
        )


@pytest.fixture
def collection_page(browser: Browser) -> Iterator[tuple[Page, CatalogCollectionApi, str]]:
    context = browser.new_context(viewport={"width": 1440, "height": 1050}, reduced_motion="reduce")
    page = context.new_page()
    page.clock.set_fixed_time(datetime.fromisoformat(_NOW.replace("Z", "+00:00")))
    page.set_default_timeout(7000)
    scenario = CatalogCollectionApi()
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


def _status(page: Page):
    return page.get_by_role("region", name="Catalog collection status", exact=True)


def _metric(page: Page, label: str):
    return _status(page).locator("dt", has_text=re.compile(f"^{re.escape(label)}$")).locator("+ dd")


def _schedule(page: Page):
    return _status(page).get_by_role("region", name="Upcoming collection schedule", exact=True)


def _schedule_rows(page: Page):
    return (
        _schedule(page)
        .get_by_role("table", name="Upcoming source collections", exact=True)
        .locator("tbody tr")
    )


def test_catalog_current_collection_counts_and_schedule_ignore_event_and_history_filters(
    collection_page,
) -> None:
    page, scenario, base = collection_page
    page.goto(f"{base}/admin?tab=catalog")
    status = _status(page)
    expect(_metric(page, "Due for refresh")).to_have_text("1")
    expect(_metric(page, "Collecting")).to_have_text("1")
    expect(_metric(page, "Awaiting first success")).to_have_text("1")
    oldest = _metric(page, "Oldest successful collection").locator("time")
    expect(oldest).to_have_attribute("datetime", "2026-09-01T12:00:00Z")
    expect(oldest).to_have_attribute("title", "2026-09-01 12:00 UTC")
    expect(status.get_by_text("6 enabled, reviewed sources.", exact=False)).to_be_visible()
    expect(_schedule_rows(page)).to_have_count(4)
    for name in ("Bay Arts 01", "Bay Arts 03", "Bay Arts 04", "Bay Arts 05"):
        expect(
            status.get_by_role("button", name=f"Manage source for {name}", exact=True)
        ).to_be_visible()
    expect(
        status.get_by_role("button", name="Manage source for Bay Arts 06", exact=True)
    ).to_have_count(0)
    expect(_schedule(page).locator('time[datetime="2026-09-08T12:30:00Z"]')).to_be_visible()
    calls = len(scenario.registry_calls)
    page.get_by_role("textbox", name="Search catalog events", exact=True).fill(
        "no matching fixture"
    )
    page.get_by_role("combobox", name="Event dates", exact=True).select_option("past")
    page.get_by_role("combobox", name="Event price", exact=True).select_option("paid")
    page.get_by_role("group", name="Catalog trend window", exact=True).get_by_role(
        "button", name="7d", exact=True
    ).click()
    expect(page.get_by_text("No published events match these filters.", exact=True)).to_be_visible()
    expect(_metric(page, "Due for refresh")).to_have_text("1")
    expect(_schedule_rows(page)).to_have_count(4)
    assert len(scenario.registry_calls) == calls
    assert scenario.registry_calls[-1] == {
        "state": ["all"],
        "include_fixtures": ["false"],
        "sort_by": ["source"],
        "sort_direction": ["asc"],
        "limit": ["100"],
        "offset": ["0"],
    }


def test_catalog_schedule_window_and_bucket_drilldown_keep_exact_sources(
    collection_page,
) -> None:
    page, scenario, base = collection_page
    page.goto(f"{base}/admin?tab=catalog")
    schedule = _schedule(page)
    window = schedule.get_by_role("group", name="Collection schedule window", exact=True)
    expect(_schedule_rows(page)).to_have_count(4)
    expect(
        schedule.get_by_role("button", name="Manage source for Bay Arts 06", exact=True)
    ).to_have_count(0)
    window.get_by_role("button", name="48h", exact=True).click()
    expect(_schedule_rows(page)).to_have_count(5)
    expect(
        schedule.get_by_role("button", name="Manage source for Bay Arts 06", exact=True)
    ).to_be_visible()
    window.get_by_role("button", name="7d", exact=True).click()
    expect(_schedule_rows(page)).to_have_count(5)
    window.get_by_role("button", name="24h", exact=True).click()
    timeline = schedule.get_by_role("group", name="Collection schedule timeline", exact=True)
    bucket = timeline.get_by_role(
        "button",
        name="Inspect sources due 2026-09-08 12:00 UTC to 2026-09-08 13:00 UTC",
        exact=True,
    )
    bucket.focus()
    bucket.press("Enter")
    expect(_schedule_rows(page)).to_have_count(1)
    expect(
        schedule.get_by_role("button", name="Manage source for Bay Arts 03", exact=True)
    ).to_be_visible()
    expect(bucket).to_have_attribute("aria-pressed", "true")
    schedule.get_by_role("button", name="Show entire window", exact=True).click()
    expect(_schedule_rows(page)).to_have_count(4)
    expect(
        timeline.get_by_role(
            "button",
            name="Inspect sources due 2026-09-08 13:00 UTC to 2026-09-08 14:00 UTC",
            exact=True,
        )
    ).to_be_disabled()
    calls = len(scenario.registry_calls)
    schedule.get_by_role("textbox", name="Find scheduled source", exact=True).fill("Bay Arts 04")
    expect(_schedule_rows(page)).to_have_count(1)
    expect(
        schedule.get_by_role("button", name="Manage source for Bay Arts 04", exact=True)
    ).to_be_visible()
    schedule.get_by_role("textbox", name="Find scheduled source", exact=True).clear()
    expect(_schedule_rows(page)).to_have_count(4)
    assert len(scenario.registry_calls) == calls
    event_calls = len(scenario.event_calls)
    scenario.registry_rows[5]["next_due_at"] = _due(3)
    schedule.get_by_role("button", name="Refresh collection schedule", exact=True).click()
    expect(_schedule_rows(page)).to_have_count(5)
    expect(
        schedule.get_by_role("button", name="Manage source for Bay Arts 06", exact=True)
    ).to_be_visible()
    assert len(scenario.registry_calls) == calls + 1
    assert len(scenario.event_calls) == event_calls


def test_catalog_schedule_distinguishes_due_collecting_and_missing_dates(
    collection_page,
) -> None:
    page, scenario, base = collection_page
    missing = source_record(10)
    missing.update(next_due_at=None, last_succeeded_at=_due(-1))
    scenario.registry_rows.append(missing)
    page.goto(f"{base}/admin?tab=catalog")
    schedule = _schedule(page)
    states = schedule.get_by_role("group", name="Collection schedule state", exact=True)
    for name, number in (
        ("Show due sources", 1),
        ("Show collecting sources", 2),
        ("Show sources without a due date", 10),
    ):
        states.get_by_role("button", name=name, exact=True).click()
        expect(_schedule_rows(page)).to_have_count(1)
        expect(
            schedule.get_by_role(
                "button", name=f"Manage source for Bay Arts {number:02}", exact=True
            )
        ).to_be_visible()
        for excluded in (7, 8, 9):
            expect(
                schedule.get_by_role(
                    "button", name=f"Manage source for Bay Arts {excluded:02}", exact=True
                )
            ).to_have_count(0)
    states.get_by_role("button", name="Show next sources", exact=True).click()
    expect(_schedule_rows(page)).to_have_count(4)
    expect(
        schedule.get_by_role("button", name="Manage source for Bay Arts 02", exact=True)
    ).to_have_count(0)
    expect(
        schedule.get_by_role("button", name="Manage source for Bay Arts 10", exact=True)
    ).to_have_count(0)


def test_catalog_schedule_show_more_reaches_every_source_in_window(collection_page) -> None:
    page, scenario, base = collection_page
    for number in range(10, 20):
        row = source_record(number)
        row.update(next_due_at=_due(number - 9), last_succeeded_at=_due(-1))
        scenario.registry_rows.append(row)
    page.goto(f"{base}/admin?tab=catalog")
    schedule = _schedule(page)
    expect(_schedule_rows(page)).to_have_count(8)
    schedule.get_by_role("button", name="Show more sources", exact=True).click()
    expect(_schedule_rows(page)).to_have_count(14)
    for number in (1, 3, 4, 5, *range(10, 20)):
        expect(
            schedule.get_by_role(
                "button", name=f"Manage source for Bay Arts {number:02}", exact=True
            )
        ).to_be_visible()
    expect(schedule.get_by_role("button", name="Show more sources", exact=True)).to_have_count(0)


def test_catalog_schedule_keeps_selected_source_when_event_filters_match_nothing(
    collection_page,
) -> None:
    page, _, base = collection_page
    page.goto(
        f"{base}/admin?tab=catalog&store_source=bay-arts-03&store_query=absent&store_dates=past"
        "&store_price=paid&store_run=fixture-run"
    )
    expect(
        page.get_by_text("No records attributed to this run match this search.", exact=True)
    ).to_be_visible()
    expect(_schedule_rows(page)).to_have_count(1)
    expect(
        _schedule(page).get_by_role("button", name="Manage source for Bay Arts 03", exact=True)
    ).to_be_visible()
    expect(_metric(page, "Due for refresh")).to_have_text("0")


@pytest.mark.parametrize(
    "source_key,lifecycle",
    [("bay-arts-07", "Paused"), ("bay-arts-08", "Retired"), ("bay-arts-09", "Blocked")],
)
def test_catalog_selected_ineligible_source_has_no_schedule_or_zero_health_claim(
    collection_page, source_key: str, lifecycle: str
) -> None:
    page, _, base = collection_page
    page.goto(
        f"{base}/admin?tab=catalog&store_source={source_key}&store_run=fixture-run&store_dates=past"
    )
    status = _status(page)
    expect(status.get_by_text(lifecycle, exact=True)).to_be_visible()
    expect(status.get_by_text("No refresh eligibility is shown.", exact=False)).to_be_visible()
    expect(_metric(page, "Due for refresh")).to_have_count(0)
    expect(_metric(page, "Collecting")).to_have_count(0)
    expect(_metric(page, "Last successful collection")).to_be_visible()
    expect(_schedule(page)).to_have_count(0)
    expect(
        status.get_by_role(
            "button", name=f"Manage source for Bay Arts {source_key[-2:]}", exact=True
        )
    ).to_be_visible()


def test_catalog_missing_source_is_unknown_without_substituting_global_counts(
    collection_page,
) -> None:
    page, _, base = collection_page
    page.goto(f"{base}/admin?tab=catalog&store_source=bay-arts-99")
    status = _status(page)
    expect(
        status.get_by_text(
            "Selected source is unavailable in the current registry snapshot.", exact=True
        )
    ).to_be_visible()
    expect(status.locator("dl")).to_have_count(0)
    expect(_schedule(page)).to_have_count(0)


def test_catalog_schedule_drills_to_exact_source_and_back_preserves_catalog_scope(
    collection_page,
) -> None:
    page, _, base = collection_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/admin?tab=catalog&store_query=music&store_price=free&catalog_trend=168")
    status = _status(page)
    entry = status.get_by_role("button", name="Manage source for Bay Arts 03", exact=True)
    expect(entry).to_be_visible()
    expect(_schedule(page).locator('time[datetime="2026-09-08T12:30:00Z"]')).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    original = page.url
    entry.focus()
    entry.press("Enter")
    expect(page.get_by_role("heading", name="Sources", exact=True, level=1)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["source_selection"] == ["bay-arts-03"]
    page.go_back()
    expect(entry).to_be_visible()
    assert page.url == original
    assert page.evaluate("document.body.scrollWidth <= innerWidth")


@pytest.mark.parametrize("code", [503, 403])
def test_catalog_status_failed_read_hides_old_dates_and_recovers_independently(
    collection_page, code: int
) -> None:
    page, scenario, base = collection_page
    page.goto(f"{base}/admin?tab=catalog")
    status = _status(page)
    expect(_metric(page, "Due for refresh")).to_have_text("1")
    schedule = _schedule(page)
    horizon = schedule.get_by_role("group", name="Collection schedule window", exact=True)
    horizon.get_by_role("button", name="48h", exact=True).click()
    search = schedule.get_by_role("textbox", name="Find scheduled source", exact=True)
    search.fill("Bay Arts 03")
    expect(_schedule_rows(page)).to_have_count(1)
    scenario.registry_status = code
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(status.get_by_text("Collection status unavailable.", exact=True)).to_be_visible()
    expect(status.locator("dl")).to_have_count(0)
    expect(_schedule(page)).to_have_count(0)
    expect(page.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)).to_be_visible()
    expect(page.get_by_role("group", name="Published records timeline", exact=True)).to_be_visible()
    scenario.registry_status = 200
    status.get_by_role("button", name="Retry collection status", exact=True).click()
    expect(_metric(page, "Due for refresh")).to_have_text("1")
    expect(horizon.get_by_role("button", name="48h", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(search).to_have_value("Bay Arts 03")
    expect(_schedule_rows(page)).to_have_count(1)
    scenario.catalog_status = 503
    scenario.throughput_failed = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_text("Collection history unavailable.", exact=True)).to_be_visible()
    expect(_metric(page, "Due for refresh")).to_have_text("1")
    expect(_schedule_rows(page)).to_have_count(1)
    expect(search).to_have_value("Bay Arts 03")


@pytest.mark.parametrize("dates,count", [("all", 4), ("upcoming", 3)])
def test_source_catalog_counts_open_exact_dates_and_restore_source_context(
    collection_page, dates: str, count: int
) -> None:
    page, scenario, base = collection_page
    # Source 02 has four distinct linked events, including one past event.
    scenario.registry_rows[1].update(total_event_count=4, upcoming_event_count=3)
    page.goto(
        f"{base}/admin?tab=sources&registry_query=Bay%20Arts%2002&registry_sort=catalog_total&registry_direction=desc"
        "&store_source=bay-arts-01&store_dates=past&store_query=unrelated&store_price=paid"
        "&store_run=fixture-source-01-published-run&store_event=019a7137-8b68-7bf4-b75c-000100000001"
        "&store_after_start=2026-10-01T19%3A00%3A00.123456Z&store_after_id=019a7137-8b68-7bf4-b75c-000100000001"
    )
    registry = page.locator("#source-registry-list")
    all_events = registry.get_by_role(
        "button", name="Browse all events for Bay Arts 02", exact=True
    )
    upcoming_events = registry.get_by_role(
        "button", name="Browse upcoming events for Bay Arts 02", exact=True
    )
    expect(all_events).to_have_text("4")
    expect(upcoming_events).to_have_text("3")
    original = page.url
    (all_events if dates == "all" else upcoming_events).click()
    expect(page.get_by_role("heading", name="Catalog", exact=True, level=1)).to_be_visible()
    expect(page.get_by_role("combobox", name="Catalog source", exact=True)).to_have_value(
        "bay-arts-02"
    )
    expect(page.get_by_role("combobox", name="Event dates", exact=True)).to_have_value(dates)
    expect(page.get_by_role("combobox", name="Event price", exact=True)).to_have_value("all")
    expect(page.get_by_role("textbox", name="Search catalog events", exact=True)).to_have_value("")
    expect(
        page.locator("dt").filter(has_text=re.compile("^Matching events$")).locator("+ dd")
    ).to_have_text(str(count))
    params = parse_qs(urlsplit(page.url).query)
    assert params["store_source"] == ["bay-arts-02"]
    assert params.get("store_dates", ["all"]) == [dates]
    assert not set(params).intersection(
        {
            "store_query",
            "store_price",
            "store_run",
            "store_event",
            "store_after_start",
            "store_after_id",
        }
    )
    assert scenario.event_calls[-1][1]["source_key"] == ["bay-arts-02"]
    assert scenario.event_calls[-1][1]["date_scope"] == [dates]
    assert scenario.event_calls[-1][1]["price_status"] == ["all"]
    assert not set(scenario.event_calls[-1][1]).intersection(
        {"q", "run_key", "after_start_at", "after_canonical_event_id"}
    )
    catalog_url = page.url
    page.reload()
    expect(page.get_by_role("combobox", name="Event dates", exact=True)).to_have_value(dates)
    expect(
        page.locator("dt").filter(has_text=re.compile("^Matching events$")).locator("+ dd")
    ).to_have_text(str(count))
    page.go_back()
    expect(all_events).to_be_visible()
    expect(page.get_by_role("textbox", name="Search sources", exact=True)).to_have_value(
        "Bay Arts 02"
    )
    expect(page.get_by_role("combobox", name="Sort sources", exact=True)).to_have_value(
        "catalog_total"
    )
    expect(page.get_by_role("button", name="Reverse source sort order", exact=True)).to_have_text(
        "Descending"
    )
    assert page.url == original
    page.go_forward()
    expect(page.get_by_role("combobox", name="Event dates", exact=True)).to_have_value(dates)
    expect(page.get_by_role("combobox", name="Catalog source", exact=True)).to_have_value(
        "bay-arts-02"
    )
    assert page.url == catalog_url


@pytest.mark.parametrize("missing", [False, True])
def test_source_catalog_counts_distinguish_unavailable_and_zero(
    collection_page, missing: bool
) -> None:
    page, scenario, base = collection_page
    if not missing:
        page.set_viewport_size({"width": 390, "height": 844})
    for count_field in ("total_event_count", "upcoming_event_count"):
        if missing:
            scenario.registry_rows[0].pop(count_field, None)
        else:
            scenario.registry_rows[0][count_field] = None
        scenario.registry_rows[1][count_field] = 0
    page.goto(f"{base}/admin?tab=sources")
    registry = page.locator("#source-registry-list")
    expect(registry.get_by_role("columnheader", name=re.compile("^All events"))).to_be_visible()
    expect(
        registry.get_by_role("columnheader", name=re.compile("^Upcoming events"))
    ).to_be_visible()
    for scope in ("all", "upcoming"):
        unavailable = registry.get_by_role(
            "button", name=f"Browse {scope} events for Bay Arts 01", exact=True
        )
        expect(unavailable).to_have_text("\u2014")
        expect(unavailable).to_be_enabled()
        expect(
            registry.get_by_role(
                "button", name=f"Browse {scope} events for Bay Arts 02", exact=True
            )
        ).to_have_text("0")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
