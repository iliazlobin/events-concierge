"""Exercise Overview and independent run statistics with read-only operator snapshots."""

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
from tests.e2e.test_admin_operations import OperationsApi
from tests.e2e.test_admin_work_errors import _entity_error, _notification, _request_error
from tests.e2e.test_admin_workspaces import run_record, source_detail

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]


_TREND_NOW = datetime(2026, 9, 8, 16, 37, tzinfo=UTC)


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _trend_buckets(hours: int, bucket_hours: int, *, empty: bool = False) -> list[dict[str, Any]]:
    """Model aligned start-time buckets, including the partial first/current interval."""
    width = bucket_hours * 3600
    first = int((_TREND_NOW - timedelta(hours=hours)).timestamp()) // width * width
    last = int(_TREND_NOW.timestamp()) // width * width
    result = []
    for index, stamp in enumerate(range(first, last + 1, width)):
        runs = 0 if empty else (index % 4) + 2
        failed = 1 if runs and index % 3 == 1 else 0
        deferred = 1 if runs and index % 3 == 2 else 0
        # Some runs remain running/paused: total runs must not be presented as completed runs.
        succeeded = max(0, runs - failed - deferred - 1)
        collected = succeeded * 11
        published = succeeded * 7
        result.append(
            {
                "bucket_start": _stamp(datetime.fromtimestamp(stamp, UTC)),
                "runs": runs,
                "succeeded": succeeded,
                "failed": failed,
                "deferred": deferred,
                "collected": collected,
                "published": published,
                "yield_pct": published / collected * 100 if collected else None,
                "median_duration_ms": 3500 if succeeded else None,
            }
        )
    return result


@dataclass
class OverviewApi(OperationsApi):
    """Independent aggregate/source chart reads; no tenant or provider operations."""

    catalog_failed: bool = False
    session_failed: bool = False
    throughput_empty: bool = False
    throughput_sparse: bool = False
    throughput_failure_status: int = 503
    source_history_failed: bool = False
    source_history_empty: bool = False
    source_history_calls: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)
    run_calls: list[dict[str, list[str]]] = field(default_factory=list)
    run_rows: list[dict[str, Any]] | None = None
    held_run_after: str | None = None
    held_run_routes: list[Route] = field(default_factory=list)
    work_rows: dict[str, list[dict[str, Any]]] | None = None

    def handle(self, route: Route) -> None:  # noqa: PLR0911 - explicit fixture endpoints
        request = route.request
        path = urlsplit(request.url).path
        params = parse_qs(urlsplit(request.url).query)
        source_match = re.fullmatch(r"/admin/v1/ingestion/sources/(bay-arts-\d{2})", path)
        if request.method == "GET" and path == "/admin/v1/operator/session" and self.session_failed:
            self.calls.append((request.method, path))
            self.respond(route, {"detail": "Fixture operator session unavailable"}, 503)
            return
        if request.method == "GET" and path == "/admin/v1/ingestion/throughput":
            self.calls.append((request.method, path))
            self.throughput_calls.append(params)
            if self.throughput_failed:
                self.respond(
                    route,
                    {"detail": "Fixture collection activity unavailable"},
                    self.throughput_failure_status,
                )
                return
            hours = int(params.get("window_hours", ["24"])[0])
            bucket_hours = int(params.get("bucket_hours", ["1"])[0])
            buckets = _trend_buckets(hours, bucket_hours, empty=self.throughput_empty)
            if self.throughput_sparse:
                for index in (0, 2):
                    for metric in (
                        "runs",
                        "succeeded",
                        "failed",
                        "deferred",
                        "collected",
                        "published",
                    ):
                        buckets[index][metric] = 0
                buckets[1]["collected"] = buckets[1]["published"] = 0
            self.respond(
                route,
                {
                    "generated_at": _stamp(_TREND_NOW),
                    "window_hours": hours,
                    "bucket_hours": bucket_hours,
                    "buckets": buckets,
                },
            )
            return
        if request.method == "GET" and source_match:
            source_key = source_match[1]
            self.calls.append((request.method, path))
            self.source_history_calls.append((source_key, params))
            if self.source_history_failed:
                self.respond(route, {"detail": "Fixture source history unavailable"}, 503)
                return
            hours = int(params.get("window_hours", ["24"])[0])
            bucket_hours = int(params.get("bucket_hours", ["4"])[0])
            # Source history uses full intervals anchored to its own statement timestamp,
            # not fleet wall-clock boundaries or the slightly earlier summary timestamp.
            history_end = _TREND_NOW + timedelta(microseconds=987654)
            buckets = _trend_buckets(hours, bucket_hours, empty=self.source_history_empty)[
                : hours // bucket_hours
            ]
            for index, bucket in enumerate(buckets):
                bucket["bucket_start"] = _stamp(
                    history_end - timedelta(hours=hours - index * bucket_hours)
                )
            body = source_detail(source_key, hours)
            body["generated_at"] = _stamp(_TREND_NOW)
            body["window"] = {
                "hours": hours,
                "bucket_hours": bucket_hours,
                "starts_at": _stamp(_TREND_NOW - timedelta(hours=hours)),
                "ends_at": _stamp(_TREND_NOW),
            }
            body["history"] = [
                {
                    "bucket_start": row["bucket_start"],
                    "total_runs": row["runs"],
                    "succeeded_runs": row["succeeded"],
                    "failed_runs": row["failed"],
                    "candidate_count": row["collected"],
                    "canonical_count": row["published"],
                    "average_duration_ms": row["median_duration_ms"],
                }
                for row in buckets
            ]
            totals = {
                key: sum(row[key] for row in buckets)
                for key in ("runs", "succeeded", "failed", "collected", "published")
            }
            completed = totals["succeeded"] + totals["failed"]
            body["summary"].update(
                {
                    "total_runs": totals["runs"],
                    "succeeded_runs": totals["succeeded"],
                    "failed_runs": totals["failed"],
                    "running_runs": totals["runs"] - completed,
                    "candidate_count": totals["collected"],
                    "canonical_count": totals["published"],
                    "success_rate": totals["succeeded"] / completed if completed else None,
                    "yield_rate": totals["published"] / totals["collected"]
                    if totals["collected"]
                    else None,
                }
            )
            body["recent_runs"] = []
            self.respond(route, body)
            return
        if (
            request.method == "GET"
            and path == "/admin/v1/ingestion/overview"
            and self.catalog_failed
        ):
            self.calls.append((request.method, path))
            self.respond(route, {"detail": "Fixture catalog summary unavailable"}, 503)
            return
        if self.handle_work_records(route, path, params) or self.handle_run_records(
            route, path, params
        ):
            return
        super().handle(route)

    def handle_work_records(self, route: Route, path: str, params: dict[str, list[str]]) -> bool:
        if (
            path not in ("/admin/v1/operations/records", "/admin/v1/operations/errors")
            or self.work_rows is None
        ):
            return False
        self.calls.append((route.request.method, path))
        self.record_calls.append(params)
        queue = params.get("queue", ["request_start"])[0]
        rows = self.work_rows.get(queue, [])
        record_id = params.get("record_id", [None])[0]
        if record_id:
            rows = [row for row in rows if row["record_id"] == record_id]
        offset = int(params.get("offset", ["0"])[0])
        limit = int(params.get("limit", ["10"])[0])
        self.respond(
            route,
            {
                "generated_at": _stamp(_TREND_NOW),
                "queue": queue,
                "scope": params.get(
                    "scope", ["errors" if queue == "entity_refresh" else "pending"]
                )[0],
                "items": rows[offset : offset + limit],
                "total": len(rows),
                "offset": offset,
                "limit": limit,
            },
        )
        return True

    def handle_run_records(self, route: Route, path: str, params: dict[str, list[str]]) -> bool:
        if path == "/admin/v1/ingestion/runs":
            self.run_calls.append(params)
            if self.held_run_after and params.get("started_after") == [self.held_run_after]:
                self.calls.append((route.request.method, path))
                self.held_run_routes.append(route)
                return True
            if self.run_rows is not None:
                self.calls.append((route.request.method, path))
                limit = int(params.get("limit", ["50"])[0])
                offset = int(params.get("offset", ["0"])[0])
                self.respond(
                    route,
                    {
                        "items": self.run_rows[offset : offset + limit],
                        "total": len(self.run_rows),
                        "limit": limit,
                        "offset": offset,
                    },
                )
                return True
        if path == "/admin/v1/ingestion/runs/lookup" and self.run_rows is not None:
            self.calls.append((route.request.method, path))
            match = next(
                (
                    row
                    for row in self.run_rows
                    if row["source_key"] == params.get("source_key", [None])[0]
                    and row["run_key"] == params.get("run_key", [None])[0]
                ),
                None,
            )
            self.respond(
                route,
                match if match else {"detail": "Fixture run not found"},
                200 if match else 404,
            )
            return True
        return False


@pytest.fixture
def operating_page(browser: Browser) -> Iterator[tuple[Page, OverviewApi, str]]:
    context = browser.new_context(viewport={"width": 1600, "height": 1000}, reduced_motion="reduce")
    page = context.new_page()
    scenario = OverviewApi()
    scenario.queue_overrides["request_start"] = {
        "pending": 4,
        "ready": 0,
        "leased": 0,
        "failed": 4,
    }
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/v1/**", scenario.handle)
    try:
        yield page, scenario, os.environ["EC_ADMIN_WEB_URL"].rstrip("/")
        assert not errors, errors
        assert not scenario.unexpected, scenario.unexpected
        assert all(
            method == "GET" and path.startswith("/admin/v1/") for method, path in scenario.calls
        )
    finally:
        context.close()


def _summary(page: Page):
    return page.get_by_role("region", name="System summary", exact=True)


def _trends(page: Page):
    return page.get_by_role("region", name="Collection trends", exact=True)


def _bars(page: Page):
    return (
        _trends(page)
        .get_by_role("group", name="Collection timeline", exact=True)
        .get_by_role("button", name=re.compile(r"^Inspect runs started "))
    )


def _background(page: Page):
    return page.get_by_role("region", name="Background work", exact=True)


def _background_metric(page: Page, queue: str, metric: str):
    return (
        _background(page)
        .get_by_role("listitem", name=f"{queue} overview", exact=True)
        .get_by_role("term")
        .filter(has_text=re.compile(f"^{re.escape(metric)}$"))
        .locator("..")
        .get_by_role("definition")
    )


def _background_page_status(page: Page):
    return (
        _background(page)
        .get_by_role("navigation", name="Background work pages", exact=True)
        .locator("..")
        .get_by_role("status")
    )


def test_overview_landing_has_visual_summary_bounded_previews_and_five_destinations(
    operating_page,
) -> None:
    page, scenario, base = operating_page
    page.goto(f"{base}/admin")
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    summary = _summary(page)
    for label in ("Fresh sources", "Catalog events", "12 / 14", "105"):
        expect(summary.get_by_text(label, exact=True)).to_be_visible()
    expect(_trends(page)).to_have_count(0)
    expect(page.get_by_role("button", name="Inspect request starts", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="All work", exact=True)).to_have_count(0)
    expect(page.get_by_role("group", name="Operational perspective", exact=True)).to_have_count(0)
    navigation = page.get_by_role("navigation", name="Administration navigation", exact=True)
    expect(navigation.get_by_role("button")).to_have_text(
        ["Overview", "Sources", "Catalog", "Runs", "Commands"]
    )
    original = page.url
    summary.get_by_text("Fresh sources", exact=True).click()
    assert page.url == original
    assert not scenario.summary_calls
    assert {call["queue"][0] for call in scenario.record_calls} == {
        "request_start",
        "notifications",
    }
    assert all(
        call["limit"] == ["20"] and call["offset"] == ["0"] and call["scope"] == ["pending"]
        for call in scenario.record_calls
    )
    assert not scenario.throughput_calls
    assert not scenario.source_history_calls
    expect(page.get_by_role("button", name="View run statistics", exact=True)).to_be_visible()


def test_primary_runs_opens_independent_statistics_and_overview_link_matches(
    operating_page,
) -> None:
    page, scenario, base = operating_page
    page.goto(f"{base}/admin?trend_window=168&trend_source=bay-arts-02&trend_metric=records")
    expect(_summary(page)).to_be_visible()
    original = page.url
    navigation = page.get_by_role("navigation", name="Administration navigation", exact=True)
    navigation.get_by_role("button", name="Runs", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True, level=1)).to_be_visible()
    expect(_trends(page)).to_be_visible()
    expect(_summary(page)).to_have_count(0)
    expect(page.get_by_label("Search runs", exact=True)).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query)["tab"] == ["run-stats"]
    expect(_trends(page).get_by_label("Collection source", exact=True)).to_have_value("bay-arts-02")
    statistics_url = page.url
    page.reload()
    expect(_bars(page).first).to_be_visible()
    assert scenario.source_history_calls[-1][1]["window_hours"] == ["168"]
    page.go_back()
    expect(_summary(page)).to_be_visible()
    assert page.url == original
    page.get_by_role("button", name="View run statistics", exact=True).click()
    expect(_bars(page).first).to_be_visible()
    assert page.url == statistics_url


def test_trend_filters_survive_reload_and_browser_back_forward(operating_page) -> None:
    page, scenario, base = operating_page
    page.goto(f"{base}/admin?tab=run-stats&registry_query=arts&store_query=music")
    trends = _trends(page)
    expect(_bars(page).first).to_be_visible()
    original = page.url
    trends.get_by_role("button", name="7d", exact=True).click()
    expect(trends.get_by_role("button", name="7d", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    week_url = page.url
    trends.get_by_role("button", name="Records", exact=True).click()
    records_url = page.url
    trends.get_by_label("Collection source", exact=True).select_option("bay-arts-02")
    expect(_bars(page).first).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["trend_window"] == ["168"]
    assert params["trend_metric"] == ["records"]
    assert params["trend_source"] == ["bay-arts-02"]
    assert params["registry_query"] == ["arts"]
    assert params["store_query"] == ["music"]
    selected_url = page.url
    page.reload()
    expect(trends.get_by_label("Collection source", exact=True)).to_have_value("bay-arts-02")
    expect(trends.get_by_role("button", name="Records", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(_bars(page).first).to_be_visible()
    assert scenario.source_history_calls[-1] == (
        "bay-arts-02",
        {"window_hours": ["168"], "bucket_hours": ["24"], "include_fixtures": ["false"]},
    )
    page.go_back()
    expect(trends.get_by_label("Collection source", exact=True)).to_have_value("")
    assert page.url == records_url
    page.go_back()
    expect(trends.get_by_role("button", name="Runs", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert page.url == week_url
    page.go_back()
    expect(trends.get_by_role("button", name="24h", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert page.url == original
    page.go_forward()
    page.go_forward()
    page.go_forward()
    expect(trends.get_by_label("Collection source", exact=True)).to_have_value("bay-arts-02")
    expect(_bars(page).first).to_be_visible()
    assert page.url == selected_url
    trends.get_by_role("button", name="30d", exact=True).click()
    expect(_bars(page).first).to_be_visible()
    assert scenario.source_history_calls[-1][1]["window_hours"] == ["720"]
    assert scenario.source_history_calls[-1][1]["bucket_hours"] == ["24"]


def test_chart_bucket_expands_exact_source_runs_with_selection_and_history(
    operating_page,
) -> None:
    page, scenario, base = operating_page
    scenario.run_rows = [
        run_record(2)
        | {"started_at": "2026-09-02T17:00:00Z", "completed_at": "2026-09-02T17:00:12Z"}
    ]
    page.goto(
        f"{base}/admin?tab=run-stats&trend_window=168&trend_source=bay-arts-02&trend_metric=records"
        "&run_query=unrelated-search&run_selection=bay-arts-01%7Cold-run"
        "&run_inspector=execution&run_page=2&run_sort=attempts&run_direction=asc"
    )
    bars = _bars(page)
    expect(bars.nth(1)).to_be_visible()
    original = page.url
    bars.nth(1).click()
    expanded = page.get_by_role("region", name="Runs in selected interval", exact=True)
    expect(expanded).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_have_count(1)
    expect(
        page.get_by_role("navigation", name="Administration breadcrumb", exact=True)
    ).to_have_count(0)
    expect(expanded.get_by_label("Search runs", exact=True)).to_have_value("")
    params = parse_qs(urlsplit(page.url).query)
    assert params["tab"] == ["run-stats"]
    assert params["trend_source"] == ["bay-arts-02"]
    assert params["trend_window"] == ["168"]
    assert params["trend_after"] == ["2026-09-02T16:37:00.987654Z"]
    assert params["trend_before"] == ["2026-09-03T16:37:00.987654Z"]
    for key in (
        "run_query",
        "run_selection",
        "run_inspector",
        "run_page",
        "run_sort",
        "run_direction",
        "status",
    ):
        assert key not in params
    assert scenario.run_calls[-1]["source_key"] == ["bay-arts-02"]
    assert scenario.run_calls[-1]["started_after"] == params["trend_after"]
    assert scenario.run_calls[-1]["started_before"] == params["trend_before"]
    # The interval remains in its table row while an exact run is inspected beneath it.
    expect(expanded.locator("xpath=ancestor::td[1]")).to_have_attribute("colspan", "6")
    expanded.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 02")).click()
    expect(
        expanded.locator("#run-inspector").get_by_role("heading", name="Bay Arts 02", exact=True)
    ).to_be_visible()
    selected_url = page.url
    assert parse_qs(urlsplit(selected_url).query)["run_selection"] == [
        "bay-arts-02|catalog:bay-arts-02:fixed-20260908"
    ]
    page.reload()
    expect(
        expanded.locator("#run-inspector").get_by_role("heading", name="Bay Arts 02", exact=True)
    ).to_be_visible()
    assert page.url == selected_url
    page.go_back()
    expect(
        expanded.locator("#run-inspector").get_by_role("heading", name="Bay Arts 02", exact=True)
    ).to_have_count(0)
    page.go_back()
    expect(expanded).to_have_count(0)
    expect(_trends(page).get_by_label("Collection source", exact=True)).to_have_value("bay-arts-02")
    assert page.url == original
    page.go_forward()
    expect(expanded).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["trend_after"] == params["trend_after"]
    bars.nth(1).click()
    expect(expanded).to_have_count(0)
    assert "trend_after" not in parse_qs(urlsplit(page.url).query)
    page.go_back()
    expect(expanded).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["tab"] == ["run-stats"]


@pytest.mark.parametrize("source", [False, True])
def test_browse_runs_expands_all_displayed_intervals_and_collapses_with_history(
    operating_page, source: bool
) -> None:
    page, scenario, base = operating_page
    suffix = "&trend_source=bay-arts-02&trend_window=168" if source else ""
    page.goto(f"{base}/admin?tab=run-stats{suffix}")
    browse = _trends(page).get_by_role("button", name="Browse runs", exact=True)
    expect(browse).to_be_enabled()
    browse.click()
    expanded = page.get_by_role("region", name="Runs in selected range", exact=True)
    expect(expanded.get_by_label("Search runs", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_have_count(1)
    params = parse_qs(urlsplit(page.url).query)
    assert params["tab"] == ["run-stats"]
    assert params["trend_scope"] == ["range"]
    assert params["trend_after"] == [
        "2026-09-01T16:37:00.987654Z" if source else "2026-09-07T16:00:00Z"
    ]
    assert params["trend_before"] == [
        "2026-09-08T16:37:00.987654Z" if source else "2026-09-08T16:37:00Z"
    ]
    assert scenario.run_calls[-1].get("source_key", []) == (["bay-arts-02"] if source else [])
    assert scenario.run_calls[-1]["started_after"] == params["trend_after"]
    assert scenario.run_calls[-1]["started_before"] == params["trend_before"]
    assert "status" not in params
    assert "fixtures" not in params
    expect(expanded.locator("xpath=ancestor::td")).to_have_count(0)
    page.go_back()
    expect(expanded).to_have_count(0)
    expect(_bars(page).first).to_be_visible()
    page.go_forward()
    expect(expanded).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["trend_after"] == params["trend_after"]
    page.reload()
    expect(expanded).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["tab"] == ["run-stats"]


def test_inline_runs_pages_ten_rows_and_preserves_expansion(operating_page) -> None:
    page, scenario, base = operating_page
    scenario.run_rows = [run_record(number) for number in range(2, 25)]
    page.goto(f"{base}/admin?tab=run-stats")
    expect(_bars(page).first).to_be_visible()
    _bars(page).first.click()
    expanded = page.get_by_role("region", name="Runs in selected interval", exact=True)
    ledger = expanded.get_by_role("region", name="Source run ledger", exact=True)
    rows = ledger.locator("tbody > tr")
    pagination = ledger.get_by_label("Runs pagination", exact=True)
    expect(rows).to_have_count(10)
    expect(pagination).to_contain_text("1\u201310 of 23")
    expect(pagination.get_by_role("button", name="Previous", exact=True)).to_be_disabled()
    assert scenario.run_calls[-1]["limit"] == ["10"]
    assert scenario.run_calls[-1].get("offset", ["0"]) == ["0"]
    original = parse_qs(urlsplit(page.url).query)
    pagination.get_by_role("button", name="Next", exact=True).click()
    expect(rows).to_have_count(10)
    expect(rows.first).to_contain_text("Bay Arts 12")
    expect(pagination).to_contain_text("11\u201320 of 23")
    assert scenario.run_calls[-1]["limit"] == ["10"]
    assert scenario.run_calls[-1]["offset"] == ["10"]
    page.reload()
    expect(rows.first).to_contain_text("Bay Arts 12")
    expect(pagination).to_contain_text("11\u201320 of 23")
    pagination.get_by_role("button", name="Previous", exact=True).click()
    expect(rows).to_have_count(10)
    expect(rows.first).to_contain_text("Bay Arts 02")
    expect(pagination).to_contain_text("1\u201310 of 23")
    assert scenario.run_calls[-1].get("offset", ["0"]) == ["0"]
    current = parse_qs(urlsplit(page.url).query)
    assert current["tab"] == ["run-stats"]
    assert current["trend_after"] == original["trend_after"]
    assert current["trend_before"] == original["trend_before"]
    expect(expanded).to_have_count(1)


def test_inline_runs_keep_metric_changes_but_reset_changed_interval_and_source(
    operating_page,
) -> None:
    page, scenario, base = operating_page
    scenario.run_rows = [run_record(2)]
    page.goto(f"{base}/admin?tab=run-stats")
    bars = _bars(page)
    expect(bars.nth(1)).to_be_visible()
    bars.first.click()
    expanded = page.get_by_role("region", name="Runs in selected interval", exact=True)
    expect(expanded).to_be_visible()
    first_after = parse_qs(urlsplit(page.url).query)["trend_after"]
    expanded.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 02")).click()
    expect(expanded.locator("#run-inspector")).to_be_visible()
    _trends(page).get_by_role("group", name="Trend metric", exact=True).get_by_role(
        "button", name="Records", exact=True
    ).click()
    expect(expanded.locator("#run-inspector")).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["trend_after"] == first_after
    bars.nth(1).click()
    expect(expanded).to_have_count(1)
    expect(expanded.locator("#run-inspector")).to_have_count(0)
    changed = parse_qs(urlsplit(page.url).query)
    assert changed["trend_after"] != first_after
    assert "run_selection" not in changed
    _trends(page).get_by_label("Collection source", exact=True).select_option("bay-arts-02")
    expect(expanded).to_have_count(0)
    assert "trend_after" not in parse_qs(urlsplit(page.url).query)
    expect(bars.first).to_be_visible()
    bars.first.click()
    expect(expanded).to_be_visible()
    assert scenario.run_calls[-1]["source_key"] == ["bay-arts-02"]
    _trends(page).get_by_role("group", name="Trend window", exact=True).get_by_role(
        "button", name="7d", exact=True
    ).click()
    expect(expanded).to_have_count(0)
    assert "trend_after" not in parse_qs(urlsplit(page.url).query)


def test_late_inline_run_read_cannot_replace_new_interval(operating_page) -> None:
    page, scenario, base = operating_page
    scenario.held_run_after = "2026-09-07T16:00:00Z"
    scenario.run_rows = [run_record(2)]
    page.goto(f"{base}/admin?tab=run-stats")
    expect(_bars(page).nth(1)).to_be_visible()
    with page.expect_request(
        lambda request: (
            urlsplit(request.url).path == "/admin/v1/ingestion/runs"
            and parse_qs(urlsplit(request.url).query).get("started_after")
            == [scenario.held_run_after]
        )
    ):
        _bars(page).first.click()
    expanded = page.get_by_role("region", name="Runs in selected interval", exact=True)
    expect(expanded).to_be_visible()
    _bars(page).nth(1).click()
    expect(
        expanded.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 02"))
    ).to_be_visible()
    assert scenario.held_run_routes
    for route in scenario.held_run_routes:
        scenario.respond(route, {"items": [run_record(3)], "total": 1, "limit": 50, "offset": 0})
    expect(
        expanded.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 02"))
    ).to_be_visible()
    expect(expanded.get_by_text("Bay Arts 03", exact=True)).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query)["trend_after"] == ["2026-09-07T17:00:00Z"]


@pytest.mark.parametrize(
    "failure,previous", [("sources_failed", "12 / 14"), ("catalog_failed", "105")]
)
def test_failed_summary_reader_hides_prior_metric_without_zero_substitution(
    operating_page,
    failure: str,
    previous: str,
) -> None:
    page, scenario, base = operating_page
    page.goto(f"{base}/admin")
    expect(_summary(page).get_by_text(previous, exact=True)).to_be_visible()
    setattr(scenario, failure, True)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(_summary(page).get_by_text("Unknown", exact=True)).to_have_count(1)
    expect(_summary(page).get_by_text(previous, exact=True)).to_have_count(0)
    failed_card = _summary(page).get_by_role(
        "article",
        name="Source freshness" if failure == "sources_failed" else "Catalog coverage",
        exact=True,
    )
    expect(failed_card.get_by_text("Unknown", exact=True)).to_be_visible()
    expect(failed_card.get_by_text("0", exact=True)).to_have_count(0)
    expect(_trends(page)).to_have_count(0)
    setattr(scenario, failure, False)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(_summary(page).get_by_text(previous, exact=True)).to_be_visible()


@pytest.mark.parametrize("status", [503, 403])
def test_failed_trend_read_hides_previous_buckets_and_retry_recovers(
    operating_page, status: int
) -> None:
    page, scenario, base = operating_page
    page.goto(f"{base}/admin?tab=run-stats")
    expect(_bars(page).first).to_be_visible()
    scenario.throughput_failed = True
    scenario.throughput_failure_status = status
    _trends(page).get_by_role("button", name="7d", exact=True).click()
    expect(_trends(page).get_by_role("alert")).to_be_visible()
    expect(_bars(page)).to_have_count(0)
    expect(_trends(page).get_by_role("button", name="Browse runs", exact=True)).to_be_disabled()
    expect(_trends(page).get_by_text("No runs in these intervals.", exact=True)).to_have_count(0)
    expect(_summary(page)).to_have_count(0)
    scenario.throughput_failed = False
    _trends(page).get_by_role("button", name="Retry trends", exact=True).click()
    expect(_bars(page).first).to_be_visible()
    expect(_trends(page).get_by_role("alert")).to_have_count(0)
    assert scenario.throughput_calls[-1]["window_hours"] == ["168"]


def test_saved_source_chart_remains_available_when_source_choices_fail(operating_page) -> None:
    page, scenario, base = operating_page
    scenario.sources_failed = True
    page.goto(f"{base}/admin?tab=run-stats&trend_source=bay-arts-02&trend_window=720")
    expect(_trends(page).get_by_label("Collection source", exact=True)).to_have_value("bay-arts-02")
    expect(_bars(page).first).to_be_visible()
    expect(_summary(page)).to_have_count(0)
    choices_error = page.get_by_role("alert").filter(has_text="Source choices could not be loaded.")
    expect(choices_error).to_be_visible()
    expect(choices_error).to_contain_text("saved source selections remain available")
    assert scenario.source_history_calls[-1][0] == "bay-arts-02"
    assert scenario.source_history_calls[-1][1]["window_hours"] == ["720"]


def test_source_history_failure_never_displays_prior_all_source_data(operating_page) -> None:
    page, scenario, base = operating_page
    page.goto(f"{base}/admin?tab=run-stats")
    expect(_bars(page).first).to_be_visible()
    scenario.source_history_failed = True
    _trends(page).get_by_label("Collection source", exact=True).select_option("bay-arts-01")
    expect(_trends(page).get_by_role("alert")).to_be_visible()
    expect(_bars(page)).to_have_count(0)
    expect(_trends(page).get_by_role("button", name="Browse runs", exact=True)).to_be_disabled()
    expect(_trends(page).get_by_text("No runs in these intervals.", exact=True)).to_have_count(0)
    scenario.source_history_failed = False
    _trends(page).get_by_role("button", name="Retry trends", exact=True).click()
    expect(_bars(page).first).to_be_visible()
    assert scenario.source_history_calls[-1][0] == "bay-arts-01"
    assert scenario.source_history_calls[-1][1]["include_fixtures"] == ["false"]


@pytest.mark.parametrize("source", [False, True])
def test_successful_empty_trend_is_distinct_from_missing_evidence(
    operating_page, source: bool
) -> None:
    page, scenario, base = operating_page
    scenario.throughput_empty = True
    scenario.source_history_empty = True
    suffix = "&trend_source=bay-arts-01" if source else ""
    page.goto(f"{base}/admin?tab=run-stats{suffix}")
    expect(_trends(page).get_by_text("No runs in these intervals.", exact=True)).to_be_visible()
    expect(_trends(page).get_by_role("alert")).to_have_count(0)
    expect(_trends(page).get_by_text("Unknown", exact=True)).to_have_count(0)
    expect(_summary(page)).to_have_count(0)


@pytest.mark.parametrize(
    "button,queue,title",
    [
        ("Inspect request starts", "request_start", "Request starts"),
        ("Inspect notifications", "notifications", "Notifications"),
    ],
)
def test_background_records_expand_inline_and_history_restores_visible_list(
    operating_page,
    button: str,
    queue: str,
    title: str,
) -> None:
    page, _, base = operating_page
    page.goto(f"{base}/admin?trend_window=168&trend_metric=records")
    expect(_background(page)).to_be_visible()
    entry = page.get_by_role("button", name=button, exact=True)
    entry.click()
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    expect(_summary(page)).to_be_visible()
    expect(_background(page)).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_have_count(1)
    expect(entry).to_have_attribute("aria-expanded", "true")
    expect(
        entry.locator("xpath=ancestor::li[1]").get_by_role("region", name="Records", exact=True)
    ).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["ops_queue"] == [queue]
    selected_url = page.url
    page.reload()
    expect(page.get_by_role("region", name="Records", exact=True)).to_be_visible()
    page.get_by_role("button", name=f"Collapse {title} records", exact=True).click()
    expect(_summary(page)).to_be_visible()
    expect(entry).to_be_visible()
    expect(entry).to_be_focused()
    for key in ("ops_queue", "ops_record", "ops_offset", "ops_scope"):
        assert key not in parse_qs(urlsplit(page.url).query)
    assert parse_qs(urlsplit(page.url).query)["trend_window"] == ["168"]
    page.go_back()
    expect(page.get_by_role("region", name="Records", exact=True)).to_be_visible()
    assert page.url == selected_url
    page.go_forward()
    expect(_summary(page)).to_be_visible()


def test_collapse_background_clears_record_and_history_restores_it(operating_page) -> None:
    page, _, base = operating_page
    record = "019a7137-8b68-7bf4-b75c-000000000001"
    page.goto(
        f"{base}/admin?ops_queue=request_start&ops_record={record}&ops_offset=10&ops_scope=errors"
    )
    expect(page.get_by_role("region", name="Records", exact=True)).to_be_visible()
    page.get_by_role("button", name="Collapse Request starts records", exact=True).click()
    expect(_summary(page)).to_be_visible()
    expect(page.get_by_role("button", name="Inspect request starts", exact=True)).to_be_visible()
    for key in ("ops_queue", "ops_record", "ops_offset", "ops_scope"):
        assert key not in parse_qs(urlsplit(page.url).query)
    page.go_back()
    expect(page.get_by_role("region", name="Records", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["ops_record"] == [record]
    assert parse_qs(urlsplit(page.url).query)["ops_offset"] == ["10"]


def test_background_switches_queues_with_nested_records_and_keeps_overview(operating_page) -> None:
    page, scenario, base = operating_page
    scenario.queue_overrides["entity_refresh"] = {"pending": 1, "failed": 1}
    scenario.work_rows = {
        "request_start": [_request_error(number) for number in range(1, 13)],
        "notifications": [_notification(1)],
        "entity_refresh": [_entity_error()],
    }
    page.goto(f"{base}/admin?registry_query=arts")
    background = _background(page)
    request_entry = background.get_by_role("button", name="Inspect request starts", exact=True)
    request_entry.click()
    records = background.get_by_role("region", name="Records", exact=True)
    expect(records.get_by_role("button", name=re.compile("^Inspect record"))).to_have_count(10)
    records.get_by_role("button", name="Next records", exact=True).click()
    expect(records.get_by_role("button", name=re.compile("^Inspect record"))).to_have_count(2)
    assert parse_qs(urlsplit(page.url).query)["ops_offset"] == ["10"]
    records.get_by_role("button", name=re.compile("^Inspect record Request retry 12")).click()
    expect(background.locator(f"#error-{_request_error(12)['record_id']}")).to_be_visible()
    expect(_summary(page)).to_be_visible()
    notification_entry = background.get_by_role("button", name="Inspect notifications", exact=True)
    notification_entry.click()
    expect(request_entry).to_have_attribute("aria-expanded", "false")
    expect(notification_entry).to_have_attribute("aria-expanded", "true")
    expect(background.get_by_role("region", name="Records", exact=True)).to_have_count(1)
    query = parse_qs(urlsplit(page.url).query)
    assert query["ops_queue"] == ["notifications"]
    assert "ops_offset" not in query
    assert "ops_record" not in query
    records.get_by_role("button", name=re.compile("^Inspect record Notification 01")).click()
    notification_detail = background.locator(f"#error-{_notification(1)['record_id']}")
    expect(notification_detail).to_be_visible()
    notification_url = page.url
    entity_entry = background.get_by_role("button", name="Inspect Entity refresh", exact=True)
    entity_entry.click()
    expect(records).to_have_count(0)
    errors = background.get_by_role("region", name="Errors", exact=True)
    errors.get_by_role("button", name=re.compile("^Inspect error Fixture ensemble profile")).click()
    expect(background.locator(f"#error-{_entity_error()['record_id']}")).to_be_visible()
    expect(_summary(page)).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_have_count(1)
    page.go_back()
    page.go_back()
    expect(notification_detail).to_be_visible()
    assert page.url == notification_url
    page.reload()
    expect(notification_detail).to_be_visible()
    expect(_summary(page)).to_be_visible()
    background.get_by_role("button", name="Collapse Notifications records", exact=True).click()
    expect(records).to_have_count(0)
    expect(notification_entry).to_be_focused()
    assert parse_qs(urlsplit(page.url).query)["registry_query"] == ["arts"]


@pytest.mark.parametrize(
    "tab,title,breadcrumb,parent",
    [
        ("runs", "Runs", "Run history", "Runs"),
        ("pipeline", "Collection pipeline", "Pipeline", "Runs"),
    ],
)
def test_legacy_secondary_route_has_contextual_navigation_and_back(
    operating_page, tab: str, title: str, breadcrumb: str, parent: str
) -> None:
    page, _, base = operating_page
    page.goto(f"{base}/admin?tab={tab}&registry_query=arts")
    expect(page.get_by_role("heading", name=title, exact=True, level=1)).to_be_visible()
    navigation = page.get_by_role("navigation", name="Administration navigation", exact=True)
    expect(navigation.get_by_role("button")).to_have_text(
        ["Overview", "Sources", "Catalog", "Runs", "Commands"]
    )
    context = page.get_by_role("navigation", name="Administration breadcrumb", exact=True)
    expect(context.get_by_text(breadcrumb, exact=True)).to_be_visible()
    original = page.url
    context.get_by_role("button", name=f"Back to {parent}", exact=True).click()
    expect(page.get_by_role("heading", name=parent, exact=True, level=1)).to_be_visible()
    if parent == "Runs":
        expect(_trends(page)).to_be_visible()
        assert parse_qs(urlsplit(page.url).query)["tab"] == ["run-stats"]
    else:
        assert parse_qs(urlsplit(page.url).query)["tab"] == ["sources"]
    page.go_back()
    expect(page.get_by_role("heading", name=title, exact=True, level=1)).to_be_visible()
    assert page.url == original
    page.reload()
    expect(context).to_be_visible()


@pytest.mark.parametrize("width", [390, 1600])
def test_commands_tab_preserves_registry_filter_on_browser_back(operating_page, width: int) -> None:
    page, _, base = operating_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{base}/admin?tab=sources&registry_query=arts&registry_state=active")
    expect(page.get_by_role("heading", name="Sources", exact=True, level=1)).to_be_visible()
    original = page.url
    expect(page.get_by_role("button", name="Command history", exact=True)).to_have_count(0)
    navigation = page.get_by_role("navigation", name="Administration navigation", exact=True)
    navigation.get_by_role("button", name="Commands", exact=True).click()
    expect(page.get_by_role("heading", name="Commands", exact=True, level=1)).to_be_visible()
    expect(navigation.get_by_role("button")).to_have_text(
        ["Overview", "Sources", "Catalog", "Runs", "Commands"]
    )
    command_tab = navigation.get_by_role("button", name="Commands", exact=True)
    expect(command_tab).to_have_attribute("aria-current", "page")
    expect(
        page.get_by_role("navigation", name="Administration breadcrumb", exact=True)
    ).to_have_count(0)
    expect(page.get_by_role("button", name="Back to Sources", exact=True)).to_have_count(0)
    commands_url = page.url
    page.reload()
    expect(command_tab).to_have_attribute("aria-current", "page")
    assert page.url == commands_url
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.go_back()
    expect(page.get_by_role("heading", name="Sources", exact=True, level=1)).to_be_visible()
    expect(navigation.get_by_role("button", name="Sources", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    assert page.url == original
    page.go_forward()
    expect(command_tab).to_have_attribute("aria-current", "page")
    assert page.url == commands_url


def test_mobile_keyboard_trends_and_background_records_fit_viewport(operating_page) -> None:
    page, scenario, base = operating_page
    scenario.run_rows = [run_record(2)]
    scenario.work_rows = {"request_start": [_request_error(1)]}
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/admin?tab=run-stats")
    expect(_bars(page).first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    bars = _bars(page)
    bars.first.focus()
    bars.first.press("End")
    expect(bars.last).to_be_focused()
    bars.last.press("Home")
    expect(bars.first).to_be_focused()
    bars.first.press("ArrowRight")
    expect(bars.nth(1)).to_be_focused()
    expect(_trends(page).get_by_role("table")).to_be_visible()
    bars.nth(1).press("Enter")
    expanded = page.get_by_role("region", name="Runs in selected interval", exact=True)
    expect(expanded).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_have_count(1)
    assert page.evaluate("document.body.scrollWidth <= innerWidth")
    expanded.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 02")).click()
    expect(expanded.locator("#run-inspector")).to_be_visible()
    assert page.evaluate("document.body.scrollWidth <= innerWidth")
    expanded.get_by_role("button", name="Close run details", exact=True).click()
    expect(expanded.locator("#run-inspector")).to_have_count(0)
    expanded.get_by_role("button", name="Collapse runs", exact=True).click()
    expect(expanded).to_have_count(0)
    assert page.evaluate("document.body.scrollWidth <= innerWidth")
    page.get_by_role("navigation", name="Administration navigation", exact=True).get_by_role(
        "button", name="Overview", exact=True
    ).press("Enter")
    expect(_summary(page)).to_be_visible()
    expect(_background(page)).to_be_visible()
    entry = page.get_by_role("button", name="Inspect request starts", exact=True)
    entry.focus()
    entry.press("Enter")
    records = _background(page).get_by_role("region", name="Records", exact=True)
    expect(records).to_be_visible()
    records.get_by_role("button", name=re.compile("^Inspect record Request retry 01")).click()
    expect(records.locator(f"#error-{_request_error(1)['record_id']}")).to_be_visible()
    expect(_summary(page)).to_be_visible()
    assert page.evaluate("document.body.scrollWidth <= innerWidth")
    page.get_by_role("button", name="Collapse Request starts records", exact=True).click()
    expect(entry).to_be_focused()
    assert page.evaluate("document.body.scrollWidth <= innerWidth")


def test_activity_table_is_open_hides_empty_intervals_and_keeps_runs_without_output(
    operating_page,
) -> None:
    page, scenario, base = operating_page
    scenario.throughput_sparse = True
    page.goto(f"{base}/admin?tab=run-stats")
    table = _trends(page).get_by_role("table")
    expect(table).to_be_visible()
    expect(_trends(page).get_by_text("View data table", exact=True)).to_have_count(0)
    rows = table.locator("tbody tr")
    expect(rows).to_have_count(23)
    expect(_bars(page)).to_have_count(23)
    expect(rows.first.locator("td").nth(1)).to_have_text("3")
    expect(rows.first.locator("td").nth(4)).to_have_text("0")
    expect(rows.first.locator("td").nth(5)).to_have_text("0")
    expect(table.get_by_role("button", name="2026-09-07 16:00 UTC", exact=False)).to_have_count(0)
    expect(table.get_by_role("button", name="2026-09-07 18:00 UTC", exact=False)).to_have_count(0)
    rows.first.get_by_role("button").click()
    expect(page.get_by_label("Search runs", exact=True)).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["tab"] == ["run-stats"]
    assert params["trend_after"] == ["2026-09-07T17:00:00Z"]
    assert params["trend_before"] == ["2026-09-07T18:00:00Z"]
    expect(rows.first.get_by_role("button")).to_have_attribute("aria-expanded", "true")
    expect(
        table.locator("tbody > tr")
        .nth(1)
        .get_by_role("region", name="Runs in selected interval", exact=True)
    ).to_be_visible()
    page.go_back()
    expect(table).to_be_visible()


def test_catalog_is_primary_and_browser_history_preserves_source_context(operating_page) -> None:
    page, _, base = operating_page
    page.goto(f"{base}/admin?tab=sources&registry_query=arts")
    navigation = page.get_by_role("navigation", name="Administration navigation", exact=True)
    navigation.get_by_role("button", name="Catalog", exact=True).click()
    expect(page.get_by_role("heading", name="Catalog", exact=True, level=1)).to_be_visible()
    expect(navigation.get_by_role("button", name="Catalog", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    expect(
        page.get_by_role("navigation", name="Administration breadcrumb", exact=True)
    ).to_have_count(0)
    catalog_url = page.url
    page.reload()
    expect(navigation.get_by_role("button", name="Catalog", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    page.go_back()
    expect(page.get_by_role("heading", name="Sources", exact=True, level=1)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["registry_query"] == ["arts"]
    page.go_forward()
    expect(page.get_by_role("heading", name="Catalog", exact=True, level=1)).to_be_visible()
    assert page.url == catalog_url


def _populate_background_failures(scenario: OverviewApi) -> None:
    for queue in (
        "account_erasure",
        "change_delivery",
        "calendar_repair",
        "handoff_expiry",
        "watch_projection",
        "entity_refresh",
    ):
        scenario.queue_overrides[queue] = {"pending": 2, "failed": 2}
    # Duplicated backend responsibilities must not create another row or page.
    scenario.extra_queues.append(
        {
            "queue": "request_start",
            "pending": 4,
            "ready": 0,
            "leased": 0,
            "failed": 4,
            "oldest_pending_at": None,
            "last_progress_at": None,
        }
    )


def test_background_work_is_visible_deduplicated_and_paged_with_history(operating_page) -> None:
    page, scenario, base = operating_page
    _populate_background_failures(scenario)
    page.goto(f"{base}/admin?registry_query=arts&trend_window=168")
    background = _background(page)
    rows = background.get_by_role("list", name="Work records", exact=True).get_by_role("listitem")
    expect(_background_page_status(page)).to_have_text("1\u20135 of 8")
    expect(rows).to_have_count(5)
    expect(background.get_by_role("button", name="Previous", exact=True)).to_be_disabled()
    expect(
        background.get_by_role("button", name="Inspect request starts", exact=True)
    ).to_have_count(1)
    expect(background.locator("summary")).to_have_count(0)
    first_url = page.url
    background.get_by_role("button", name="Next", exact=True).click()
    expect(_background_page_status(page)).to_have_text("6\u20138 of 8")
    expect(rows).to_have_count(3)
    expect(background.get_by_role("button", name="Next", exact=True)).to_be_disabled()
    second_url = page.url
    assert parse_qs(urlsplit(second_url).query)["ops_work_page"] == ["2"]
    page.reload()
    expect(_background_page_status(page)).to_have_text("6\u20138 of 8")
    page.go_back()
    expect(_background_page_status(page)).to_have_text("1\u20135 of 8")
    assert page.url == first_url
    page.go_forward()
    expect(_background_page_status(page)).to_have_text("6\u20138 of 8")
    assert page.url == second_url


def test_paged_background_expansion_restores_page_and_focus(operating_page) -> None:
    page, scenario, base = operating_page
    _populate_background_failures(scenario)
    page.goto(f"{base}/admin?registry_query=arts&trend_window=168&ops_work_page=2")
    background = _background(page)
    expect(_background_page_status(page)).to_have_text("6\u20138 of 8")
    entry = background.get_by_role("button", name="Inspect Entity refresh", exact=True)
    entry.click()
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    expect(_summary(page)).to_be_visible()
    expect(background).to_be_visible()
    expect(entry).to_have_attribute("aria-expanded", "true")
    expect(
        entry.locator("xpath=ancestor::li[1]").get_by_role("region", name="Errors", exact=True)
    ).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["ops_work_page"] == ["2"]
    selected_url = page.url
    page.reload()
    expect(page.get_by_role("region", name="Errors", exact=True)).to_be_visible()
    page.go_back()
    expect(_background_page_status(page)).to_have_text("6\u20138 of 8")
    expect(entry).to_be_focused()
    page.go_forward()
    expect(page.get_by_role("region", name="Errors", exact=True)).to_be_visible()
    assert page.url == selected_url
    background.get_by_role("button", name="Previous", exact=True).click()
    expect(_background_page_status(page)).to_have_text("1\u20135 of 8")
    expect(background.get_by_role("region", name="Errors", exact=True)).to_have_count(0)
    assert "ops_queue" not in parse_qs(urlsplit(page.url).query)
    page.go_back()
    expect(_background_page_status(page)).to_have_text("6\u20138 of 8")
    expect(background.get_by_role("region", name="Errors", exact=True)).to_be_visible()
    page.get_by_role("button", name="Collapse Entity refresh errors", exact=True).click()
    expect(_background_page_status(page)).to_have_text("6\u20138 of 8")
    expect(entry).to_be_focused()
    assert parse_qs(urlsplit(page.url).query)["registry_query"] == ["arts"]
    assert parse_qs(urlsplit(page.url).query)["trend_window"] == ["168"]


def test_background_work_single_page_unknown_failure_and_recovery(operating_page) -> None:
    page, scenario, base = operating_page
    page.goto(f"{base}/admin")
    background = _background(page)
    rows = background.get_by_role("list", name="Work records", exact=True).locator(":scope > li")
    expect(rows).to_have_count(2)
    expect(
        background.get_by_role("navigation", name="Background work pages", exact=True)
    ).to_have_count(0)
    for metric, value in (("Pending", "4"), ("Ready to claim", "0"), ("Leases", "0")):
        expect(_background_metric(page, "Request starts", metric)).to_have_text(value)
    _populate_background_failures(scenario)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(_background_page_status(page)).to_have_text("1\u20135 of 8")
    scenario.backend_failed = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    alert = page.get_by_role("alert", name="Overview data status", exact=True)
    expect(alert).to_contain_text("Work could not be loaded.")
    expect(rows).to_have_count(2)
    expect(background.get_by_text("Unknown", exact=True)).to_have_count(8)
    for metric in ("Pending", "Ready to claim", "Leases", "Pending with errors"):
        expect(_background_metric(page, "Request starts", metric)).to_have_text("Unknown")
    expect(background.get_by_text("Account cleanup", exact=True)).to_have_count(0)
    expect(
        background.get_by_role("navigation", name="Background work pages", exact=True)
    ).to_have_count(0)
    scenario.backend_failed = False
    alert.get_by_role("button", name="Retry", exact=True).click()
    expect(_background_page_status(page)).to_have_text("1\u20135 of 8")
    expect(background.get_by_text("Unknown", exact=True)).to_have_count(0)


def test_overview_outage_has_one_alert_and_retry_recovers_only_failed_reads(operating_page) -> None:
    page, scenario, base = operating_page
    scenario.backend_failed = scenario.sources_failed = scenario.catalog_failed = True
    scenario.session_failed = True
    page.goto(f"{base}/admin")
    alert = page.get_by_role("alert", name="Overview data status", exact=True)
    expect(alert).to_contain_text("Overview data unavailable.")
    expect(alert).to_contain_text("Work, Sources, Catalog could not be loaded.")
    expect(page.locator("main").get_by_role("alert")).to_have_count(1)
    expect(page.get_by_text("Admin overview is unavailable.", exact=True)).to_have_count(0)
    expect(_summary(page).get_by_text("Unknown", exact=True)).to_have_count(2)
    expect(_background(page).get_by_text("Unknown", exact=True)).to_have_count(8)
    expect(page.locator(".admin-operator-menu strong")).to_have_text("Operator")

    scenario.sources_failed = False
    alert.get_by_role("button", name="Retry", exact=True).click()
    expect(_summary(page).get_by_text("12 / 14", exact=True)).to_be_visible()
    expect(alert).to_contain_text("Some overview data unavailable.")
    expect(alert).to_contain_text("Work, Catalog could not be loaded.")
    expect(page.locator("main").get_by_role("alert")).to_have_count(1)
    source_reads = scenario.calls.count(("GET", "/admin/v1/ingestion/source-health"))
    overview_reads = scenario.calls.count(("GET", "/admin/v1/ingestion/overview"))

    scenario.backend_failed = scenario.catalog_failed = False
    scenario.session_failed = False
    alert.get_by_role("button", name="Retry", exact=True).click()
    expect(alert).to_have_count(0)
    expect(_summary(page).get_by_text("105", exact=True)).to_be_visible()
    for metric, value in (("Pending", "4"), ("Ready to claim", "0"), ("Leases", "0")):
        expect(_background_metric(page, "Request starts", metric)).to_have_text(value)
    expect(page.locator(".admin-operator-menu strong")).to_have_text("fixture-viewer")
    assert scenario.calls.count(("GET", "/admin/v1/ingestion/source-health")) == source_reads
    assert scenario.calls.count(("GET", "/admin/v1/ingestion/overview")) == overview_reads + 1


def test_background_work_mobile_keyboard_paging_fits_and_clamps_saved_page(operating_page) -> None:
    page, scenario, base = operating_page
    _populate_background_failures(scenario)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/admin?ops_work_page=999")
    background = _background(page)
    expect(_background_page_status(page)).to_have_text("6\u20138 of 8")
    previous = background.get_by_role("button", name="Previous", exact=True)
    previous.focus()
    previous.press("Enter")
    expect(_background_page_status(page)).to_have_text("1\u20135 of 8")
    assert "ops_work_page" not in parse_qs(urlsplit(page.url).query)
    next_button = background.get_by_role("button", name="Next", exact=True)
    next_button.focus()
    next_button.press("Enter")
    expect(_background_page_status(page)).to_have_text("6\u20138 of 8")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(background.get_by_role("listitem")).to_have_count(3)
