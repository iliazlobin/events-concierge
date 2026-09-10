"""Read-only drill-downs and snapshot semantics for the visual Overview summary."""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, expect
from tests.e2e.test_admin_operating_overview import operating_page as _operating_page

operating_page = _operating_page

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]


def _freshness(page: Page):
    return page.get_by_role("article", name="Source freshness", exact=True)


def _coverage(page: Page):
    return page.get_by_role("article", name="Catalog coverage", exact=True)


def test_freshness_categories_reveal_bounded_evidence_and_open_source(operating_page) -> None:
    page, scenario, base = operating_page
    scenario.source_health_overrides.update(
        {
            3: {"freshness_state": "warn"},
            4: {"freshness_state": "never", "last_success_at": None},
            5: {"enabled": False},
            6: {"retired_at": "2026-09-01T00:00:00Z"},
        }
    )
    page.goto(f"{base}/admin")
    freshness = _freshness(page)
    expect(freshness.get_by_text("8 / 12", exact=True)).to_be_visible()
    expect(
        freshness.get_by_role(
            "img",
            name=(
                "Freshness of 12 scheduled sources: 8 fresh, 1 watch, 2 overdue, 1 never succeeded"
            ),
            exact=True,
        )
    ).to_be_visible()
    expect(freshness.get_by_role("listitem")).to_have_count(3)
    expect(freshness.get_by_text("Showing 3 of 4 sources", exact=True)).to_be_visible()
    reads_before = scenario.calls.count(("GET", "/admin/v1/ingestion/source-health"))
    categories = freshness.get_by_role("group", name="Inspect sources by freshness", exact=True)
    categories.get_by_role("button", name="Watch 1", exact=True).click()
    expect(categories.get_by_role("button", name="Watch 1", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(freshness.get_by_role("listitem")).to_have_count(1)
    expect(freshness.get_by_role("link", name=re.compile(r"^Bay Arts 03"))).to_be_visible()
    categories.get_by_role("button", name="Fresh 8", exact=True).click()
    expect(freshness.get_by_role("listitem")).to_have_count(3)
    expect(freshness.get_by_text("Showing 3 of 8 sources", exact=True)).to_be_visible()
    assert scenario.calls.count(("GET", "/admin/v1/ingestion/source-health")) == reads_before
    assert not scenario.source_history_calls
    freshness.get_by_role("button", name="Show attention", exact=True).click()
    expect(freshness.get_by_text("No successful run recorded", exact=False)).to_be_visible()
    freshness.get_by_role("link", name=re.compile(r"^Bay Arts 01")).click()
    expect(page).to_have_url(re.compile(r"[?&]source_selection=bay-arts-01(?:&|$)"))
    expect(
        page.get_by_role("region", name="Source details for bay-arts-01", exact=True)
    ).to_be_visible()


def test_source_volume_bars_open_upcoming_catalog_and_fit_mobile(operating_page) -> None:
    page, scenario, base = operating_page
    # These two source associations overlap; their counts must not replace the unique 105 total.
    scenario.source_health_overrides.update(
        {
            14: {"upcoming_events": 2000, "enabled": False},
            13: {"upcoming_events": 2000},
            12: {"upcoming_events": 0},
        }
    )
    page.set_viewport_size({"width": 390, "height": 1000})
    page.goto(f"{base}/admin")
    coverage = _coverage(page)
    expect(coverage.get_by_text("105", exact=True)).to_be_visible()
    expect(
        coverage.get_by_role(
            "img",
            name="13 sources with upcoming events; 1 without upcoming events",
            exact=True,
        )
    ).to_be_visible()
    expect(coverage.get_by_role("listitem")).to_have_count(4)
    expect(coverage.get_by_role("listitem").first).to_contain_text("Bay Arts 13")
    summary = page.get_by_role("region", name="System summary", exact=True)
    assert summary.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    assert summary.evaluate(
        "element => element.getBoundingClientRect().right <= document.documentElement.clientWidth + 1"
    )
    coverage.get_by_role(
        "link", name="Bay Arts 14: 2,000 upcoming events. Open catalog", exact=True
    ).click()
    expect(page.get_by_role("heading", name="Catalog", exact=True, level=1)).to_be_visible()
    expect(page.get_by_label("Catalog source", exact=True)).to_have_value("bay-arts-14")
    expect(page.get_by_label("Event dates", exact=True)).to_have_value("upcoming")
    params = parse_qs(urlsplit(page.url).query)
    assert params["store_source"] == ["bay-arts-14"]
    assert params["store_dates"] == ["upcoming"]
    expect(page.get_by_role("region", name="Catalog investigation", exact=True)).to_be_visible()
    assert scenario.event_calls[-1][1]["source_key"] == ["bay-arts-14"]
    assert scenario.event_calls[-1][1]["date_scope"] == ["upcoming"]


def test_independent_snapshot_failures_remove_only_unavailable_visuals(operating_page) -> None:
    page, scenario, base = operating_page
    page.goto(f"{base}/admin")
    expect(_freshness(page).get_by_role("listitem")).to_have_count(2)
    expect(_coverage(page).get_by_role("listitem")).to_have_count(4)
    scenario.sources_failed = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(_freshness(page).get_by_text("Unknown", exact=True)).to_be_visible()
    expect(_freshness(page).get_by_role("img")).to_have_count(0)
    expect(_freshness(page).get_by_role("listitem")).to_have_count(0)
    expect(_coverage(page).get_by_text("105", exact=True)).to_be_visible()
    expect(_coverage(page).get_by_role("img")).to_have_count(0)
    expect(_coverage(page).get_by_role("listitem")).to_have_count(0)
    expect(
        _coverage(page).get_by_text("Source coverage is unavailable.", exact=False)
    ).to_be_visible()

    scenario.sources_failed = False
    scenario.catalog_failed = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(_freshness(page).get_by_text("12 / 14", exact=True)).to_be_visible()
    expect(_freshness(page).get_by_role("img")).to_have_count(1)
    expect(_coverage(page).get_by_text("Unknown", exact=True)).to_be_visible()
    expect(_coverage(page).get_by_text("105", exact=True)).to_have_count(0)
    expect(_coverage(page).get_by_role("img")).to_have_count(1)
    expect(_coverage(page).get_by_role("listitem")).to_have_count(4)
    expect(
        _coverage(page).get_by_text(
            "Unique catalog total unavailable; source counts use their own snapshot.", exact=True
        )
    ).to_be_visible()
