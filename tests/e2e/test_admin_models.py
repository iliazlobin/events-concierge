"""Models workspace behavior in the real Next UI, with isolated accounting fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, Route, expect
from tests.e2e.test_admin_operations import OperationsApi

pytestmark = pytest.mark.browser_e2e


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/v1/models/usage",
        "/admin/v1/models/budget",
        "/admin/v1/models/key",
        "/admin/v1/models/unknown",
    ],
)
def test_model_admin_http_responses_disallow_storage(page_factory, path: str) -> None:
    base = os.environ.get("EC_ADMIN_WEB_URL")
    if not base:
        pytest.skip("EC_ADMIN_WEB_URL is not set")
    harness = page_factory()
    # Real HTTP catches Next header overrides that route-level fixtures bypass.
    response = harness.context.request.get(f"{base}{path}")
    directives = {value.strip().lower() for value in response.headers["cache-control"].split(",")}
    assert "no-store" in directives


def budget() -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    return {
        "revision": 1,
        "mode": "enforce",
        "daily_limit_usd": "2",
        "monthly_limit_usd": "20",
        "daily_used_usd": ".80",
        "monthly_used_usd": "1.20",
        "alert_percent": 80,
        "updated_at": now,
        "generated_at": now,
        "day_start": now,
        "month_start": now,
        "unknown_calls": 0,
        "pending_calls": 0,
    }


@dataclass
class ModelsApi(OperationsApi):
    reviewer: bool = True
    usage_unavailable: bool = False
    key_unavailable: bool = False
    conflict: bool = False
    sparse_latency: bool = False
    settings: dict[str, object] | None = None

    def handle(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        params = parse_qs(urlsplit(route.request.url).query)
        if path == "/admin/v1/operator/session":
            self.respond(
                route,
                {
                    "subject": "fixture",
                    "role": "reviewer" if self.reviewer else "viewer",
                    "capabilities": [
                        "ingestion.read",
                        *(["models.budget.configure"] if self.reviewer else []),
                    ],
                    "environment": "local",
                    "authentication": "local",
                },
            )
        elif path == "/admin/v1/models/budget":
            self.settings = self.settings or budget()
            if route.request.method == "PATCH":
                if not self.reviewer:
                    self.respond(route, {"detail": "not authorized"}, 403)
                elif self.conflict:
                    self.respond(route, {"detail": "budget changed; refresh before editing"}, 409)
                else:
                    self.settings.update(route.request.post_data_json)
                    self.settings["revision"] = int(self.settings["revision"]) + 1
                    self.respond(route, self.settings)
            else:
                self.respond(route, self.settings)
        elif path == "/admin/v1/models/key":
            self.respond(
                route,
                {
                    "status": "unavailable" if self.key_unavailable else "ok",
                    "checked_at": datetime.now(UTC).isoformat(),
                    "usage": "42",
                    "usage_daily": "4",
                    "usage_monthly": "10",
                    "limit": "100",
                    "limit_state": "configured",
                    "limit_remaining": "90",
                    "limit_reset": "monthly",
                },
            )
        elif path == "/admin/v1/models/usage":
            if self.usage_unavailable:
                self.respond(route, {"detail": "unavailable"}, 503)
                return
            start = datetime.fromisoformat(params["start_at"][0])
            end = datetime.fromisoformat(params["end_at"][0])
            selected = params.get("model", [None])[0]
            filtered = selected is not None
            totals = {
                "calls": 2 if filtered else 3,
                "failed": 0 if filtered else 1,
                "cost_usd": ".80",
                "unknown_cost_calls": 0 if filtered else 1,
                "input_tokens": 200,
                "output_tokens": 40,
                "latency_ms": 400,
                "cached_tokens": 50,
                "reasoning_tokens": 5,
                "pending": 0,
                "unknown_token_calls": 0 if filtered else 1,
            }
            points = []
            for index in range(3):
                at = start + (end - start) * index / 3
                until = start + (end - start) * (index + 1) / 3
                points.append(
                    {
                        **totals,
                        "at": at.isoformat(),
                        "until": until.isoformat(),
                        "cost_usd": ".80" if index == 1 else "0",
                        "calls": 2 if index == 1 else (0 if filtered or index == 0 else 1),
                        "failed": 1 if index == 2 and not filtered else 0,
                        "unknown_cost_calls": 1 if index == 2 and not filtered else 0,
                        "input_tokens": 200 if index == 1 else 0,
                        "output_tokens": 40 if index == 1 else 0,
                        "latency_ms": None if self.sparse_latency and index != 1 else 400,
                    }
                )
            self.respond(
                route,
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                    "bucket_hours": int(params["bucket_hours"][0]),
                    "model": selected,
                    "tracked_since": (end - timedelta(days=30)).isoformat(),
                    "model_options": ["z-ai/fallback", "unreported"],
                    "totals": totals,
                    "series": points,
                    "models": [{**totals, "model": selected or "z-ai/fallback"}],
                    "recent": [
                        {
                            "call_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                            "started_at": start.isoformat(),
                            "completed_at": (start + timedelta(seconds=1)).isoformat(),
                            "requested_model": "deepseek/primary",
                            "actual_model": "z-ai/fallback",
                            "status": "ok",
                            "input_tokens": 200,
                            "output_tokens": 40,
                            "cost_usd": ".8",
                            "latency_ms": 1000,
                            "error_code": None,
                        }
                    ],
                },
            )
        else:
            super().handle(route)


@pytest.fixture
def models_page(browser: Browser) -> Iterator[tuple[Page, ModelsApi, str]]:
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    page = context.new_page()
    scenario = ModelsApi()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/v1/**", scenario.handle)
    try:
        yield page, scenario, os.environ["EC_ADMIN_WEB_URL"].rstrip("/")
        assert not errors
        assert not scenario.unexpected
    finally:
        context.close()


def test_usage_filters_metrics_history_chart_focus_and_unknown_billing(
    models_page: tuple[Page, ModelsApi, str],
) -> None:
    page, _, url = models_page
    page.goto(f"{url}/admin?tab=models")
    expect(page.get_by_role("heading", name="Models", exact=True)).to_be_visible()
    trend = page.get_by_role("region", name="Model usage trend")
    expect(page.get_by_text("1 calls with unknown cost", exact=True)).to_be_visible()
    metric = trend.get_by_role("group", name="Chart metric")
    metric.get_by_role("button", name="Tokens", exact=True).click()
    expect(metric.get_by_role("button", name="Tokens", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(trend.get_by_label("Chart series")).to_contain_text("TotalInputOutput")
    expect(trend.locator("svg path")).to_have_count(3)
    intervals = trend.get_by_role("group", name="Model usage intervals").get_by_role("button")
    intervals.nth(1).focus()
    expect(trend.get_by_text("200 input · 40 output", exact=True)).to_be_visible()
    expect(intervals.nth(1)).to_have_attribute("aria-pressed", "true")
    intervals.nth(1).press("ArrowRight")
    expect(intervals.nth(2)).to_be_focused()
    expect(trend).to_contain_text("calls with unknown cost; reported spend excludes them")
    intervals.nth(2).press("Home")
    expect(intervals.nth(0)).to_be_focused()
    intervals.nth(0).press("End")
    expect(intervals.nth(2)).to_be_focused()
    page.get_by_role("combobox", name="Usage model").select_option("z-ai/fallback")
    expect(page.get_by_text("1 calls with unknown cost", exact=True)).to_have_count(0)
    page.get_by_role("combobox", name="Model usage time range").select_option("30d")
    page.reload()
    expect(page.get_by_role("combobox", name="Usage model")).to_have_value("z-ai/fallback")
    expect(page.get_by_role("combobox", name="Model usage time range")).to_have_value("30d")
    expect(metric.get_by_role("button", name="Tokens", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.get_by_role("combobox", name="Model usage time range").select_option("24h")
    page.go_back()
    expect(page.get_by_role("combobox", name="Model usage time range")).to_have_value("30d")
    page.get_by_text("Recent calls · latest 1", exact=True).click()
    expect(page.get_by_text("deepseek/primary → z-ai/fallback", exact=True)).to_be_visible()
    expect(page.get_by_role("region", name="OpenRouter key usage")).to_contain_text("$4.00")


def test_line_chart_missing_latency_is_a_gap_not_zero(
    models_page: tuple[Page, ModelsApi, str],
) -> None:
    page, scenario, url = models_page
    scenario.sparse_latency = True
    page.goto(f"{url}/admin?tab=models&usage_metric=latency")
    trend = page.get_by_role("region", name="Model usage trend")
    expect(trend).to_be_visible()
    path = trend.locator("svg path").get_attribute("d")
    assert path and path.count("M") == 1 and "L" not in path
    intervals = trend.get_by_role("group", name="Model usage intervals").get_by_role("button")
    intervals.nth(0).focus()
    expect(trend.get_by_text("No measurement", exact=True)).to_be_visible()
    intervals.nth(0).press("ArrowRight")
    expect(trend.get_by_text("200 input · 40 output", exact=True)).to_be_visible()


def test_budget_edit_survives_refresh_uses_exact_values_and_reports_conflicts(
    models_page: tuple[Page, ModelsApi, str],
) -> None:
    page, scenario, url = models_page
    page.goto(f"{url}/admin?tab=models")
    panel = page.get_by_role("region", name="Application model budget")
    panel.get_by_role("button", name="Edit budget", exact=True).click()
    panel.get_by_role("textbox", name="Daily limit (USD)", exact=True).fill("0")
    panel.get_by_role("textbox", name="Monthly limit (USD)", exact=True).fill("12.50")
    page.get_by_role("button", name="Refresh usage", exact=True).click()
    expect(panel.get_by_role("textbox", name="Daily limit (USD)", exact=True)).to_have_value("0")
    scenario.conflict = True
    panel.get_by_role("button", name="Save budget", exact=True).click()
    expect(panel.get_by_role("alert")).to_contain_text("budget changed")
    scenario.conflict = False
    panel.get_by_role("button", name="Save budget", exact=True).click()
    expect(panel).to_contain_text("Limit reached")
    assert scenario.settings is not None
    assert (
        scenario.settings["daily_limit_usd"] == "0"
        and scenario.settings["monthly_limit_usd"] == "12.50"
    )


def test_failed_reads_hide_old_charts_viewer_is_read_only_and_mobile_stays_bounded(
    models_page: tuple[Page, ModelsApi, str],
) -> None:
    page, scenario, url = models_page
    scenario.reviewer = False
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{url}/admin?tab=models")
    expect(page.get_by_role("region", name="Model usage trend")).to_be_visible()
    expect(page.get_by_role("button", name="Edit budget", exact=True)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    scenario.usage_unavailable = True
    scenario.key_unavailable = True
    page.get_by_role("button", name="Refresh usage", exact=True).click()
    expect(page.get_by_role("region", name="Model usage trend")).to_have_count(0)
    expect(page.get_by_role("region", name="OpenRouter key usage")).to_contain_text(
        "Provider totals unavailable."
    )
    scenario.usage_unavailable = False
    page.get_by_role("button", name="Retry usage", exact=True).click()
    expect(page.get_by_role("region", name="Model usage trend")).to_be_visible()
