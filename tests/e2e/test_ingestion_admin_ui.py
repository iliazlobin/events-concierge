"""Hermetic, product-level browser coverage for the local ingestion admin."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.browser_e2e

_SOURCE_KEY = "bay-area-arts"


def _source(
    *,
    source_key: str = _SOURCE_KEY,
    display_name: str = "Bay Area Arts",
    publisher: str = "Bay Arts Council",
    seed_url: str = "https://events.example.test/arts",
    effective_status: str = "due",
) -> dict[str, Any]:
    return {
        "source_key": source_key,
        "display_name": display_name,
        "publisher": publisher,
        "mode": "public_jsonld",
        "region": "Bay Area",
        "seed_url": seed_url,
        "enabled": True,
        "review_status": "reviewed",
        "effective_status": effective_status,
        "due": effective_status == "due",
        "last_succeeded_at": "2030-05-31T12:00:00Z",
        "next_due_at": "2030-06-01T11:00:00Z",
        "event_count": 23,
        "latest_run": {
            "run_key": "cadence:2030-05-31",
            "status": "succeeded",
            "started_at": "2030-05-31T11:59:00Z",
            "completed_at": "2030-05-31T12:00:00Z",
            "candidate_count": 25,
            "canonical_count": 23,
            "error": None,
            "attempt_count": 1,
        },
    }


def _run(
    run_key: str,
    *,
    release_revision: str | None,
    image_digest: str | None,
    provenance_status: str,
    completed_at: str,
    duration_ms: int,
    status: str = "succeeded",
) -> dict[str, Any]:
    return {
        "run_key": run_key,
        "source_key": _SOURCE_KEY,
        "display_name": "Bay Area Arts",
        "status": status,
        "started_at": "2030-05-31T11:58:00Z",
        "completed_at": completed_at,
        "candidate_count": 25,
        "canonical_count": 23,
        "duration_ms": duration_ms,
        "error": None,
        "attempt_count": 1,
        "source_revision": 4,
        "release_revision": release_revision,
        "image_digest": image_digest,
        "provenance_status": provenance_status,
        "trigger": "admin_source",
    }


@dataclass(slots=True)
class ApiCall:
    method: str
    path: str
    query: dict[str, list[str]]
    body: Any


@dataclass(slots=True)
class AdminApiScenario:
    policy_allowed: bool = True
    due_sources: int = 2
    sources: list[dict[str, Any]] = field(default_factory=lambda: [_source()])
    runs: list[dict[str, Any]] = field(default_factory=list)
    calls: list[ApiCall] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    complete_command_on_get: int | None = None
    command_gets: int = 0

    def install(self, page: Page) -> None:
        def handler(route: Route) -> None:
            self._handle(route)

        page.route("**/admin/v1/ingestion/**", handler)

    def count(self, method: str, path: str) -> int:
        return sum(call.method == method and call.path == path for call in self.calls)

    def last(self, method: str, path: str) -> ApiCall:
        return next(
            call for call in reversed(self.calls) if call.method == method and call.path == path
        )

    def _handle(self, route: Route) -> None:  # noqa: PLR0911 - explicit API fixture router
        request = route.request
        parsed = urlsplit(request.url)
        method = request.method
        body = request.post_data_json if request.post_data else None
        self.calls.append(ApiCall(method, parsed.path, parse_qs(parsed.query), body))

        if method == "GET" and parsed.path == "/admin/v1/ingestion/overview":
            self._json(route, self._overview())
            return
        if method == "GET" and parsed.path == "/admin/v1/ingestion/filters":
            self._json(
                route,
                {
                    "modes": [{"value": "public_jsonld", "count": len(self.sources)}],
                    "publishers": [{"value": "Bay Arts Council", "count": len(self.sources)}],
                    "regions": [{"value": "Bay Area", "count": len(self.sources)}],
                    "source_states": [
                        {"value": "active", "label": "Active"},
                        {"value": "due", "label": "Due now"},
                        {"value": "failed", "label": "Latest run failed"},
                    ],
                    "run_statuses": [
                        {"value": "running", "label": "Running"},
                        {"value": "succeeded", "label": "Succeeded"},
                        {"value": "failed", "label": "Failed"},
                    ],
                    "window_hours": [24, 168, 720],
                },
            )
            return
        if method == "GET" and parsed.path == "/admin/v1/ingestion/sources":
            self._json(
                route,
                {
                    "items": self.sources,
                    "total": len(self.sources),
                    "limit": 50,
                    "offset": 0,
                },
            )
            return
        if method == "GET" and parsed.path.startswith("/admin/v1/ingestion/sources/"):
            source_key = parsed.path.rsplit("/", maxsplit=1)[-1]
            if any(source["source_key"] == source_key for source in self.sources):
                self._json(route, self._source_detail(source_key))
                return
        if method == "GET" and parsed.path == "/admin/v1/ingestion/runs":
            runs = self.runs
            status_filter = parse_qs(parsed.query).get("status", [""])[0]
            if status_filter:
                runs = [run for run in runs if run["status"] == status_filter]
            self._json(
                route,
                {
                    "items": runs,
                    "total": len(runs),
                    "limit": 50,
                    "offset": 0,
                },
            )
            return
        if method == "GET" and parsed.path == "/admin/v1/ingestion/commands":
            self.command_gets += 1
            if (
                self.complete_command_on_get is not None
                and self.command_gets >= self.complete_command_on_get
                and self.commands
            ):
                self.commands[0] = {
                    **self.commands[0],
                    "status": "completed",
                    "started_at": "2030-06-01T12:00:01Z",
                    "completed_at": "2030-06-01T12:00:02Z",
                    "executor_source_revision": 5,
                    "executor_release_revision": "worker-def5678",
                    "executor_image_digest": f"sha256:{'3' * 64}",
                    "result": {
                        "outcome": "succeeded",
                        "canonical_count": 23,
                    },
                }
            self._json(route, {"items": self.commands})
            return
        if method == "POST" and parsed.path == "/admin/v1/ingestion/commands":
            command = {
                "command_id": body["command_id"],
                "action": body["action"],
                "source_key": body.get("source_key"),
                "status": "queued",
                "requested_at": "2030-06-01T12:00:00Z",
                "started_at": None,
                "completed_at": None,
                "result": None,
                "error_code": None,
                "source_revision": 4,
                "release_revision": "api-abc1234",
                "image_digest": f"sha256:{'2' * 64}",
                "executor_source_revision": None,
                "executor_release_revision": None,
                "executor_image_digest": None,
            }
            self.commands = [command]
            self._json(route, command, status=202)
            return
        self._json(
            route,
            {"detail": f"unexpected fixture request: {method} {parsed.path}"},
            status=500,
        )

    def _overview(self) -> dict[str, Any]:
        pending = sum(command["status"] in {"queued", "running"} for command in self.commands)
        return {
            "generated_at": "2030-06-01T12:00:00Z",
            "policy": {
                "allowed": self.policy_allowed,
                "reason": (
                    "Reviewed browser collection is admitted."
                    if self.policy_allowed
                    else "Browser collection is disabled by source policy."
                ),
                "code": "allowed" if self.policy_allowed else "modality_disabled",
            },
            "summary": {
                "sources": len(self.sources),
                "active_sources": len(self.sources) if self.policy_allowed else 0,
                "due_sources": self.due_sources,
                "running_runs": 0,
                "failed_runs_24h": 1,
                "catalog_events": 1_284,
                "pending_commands": pending,
                "fixture_sources": 497,
            },
            "latest_success_at": "2030-05-31T12:00:00Z",
        }

    def _source_detail(self, source_key: str = _SOURCE_KEY) -> dict[str, Any]:
        selected = next(source for source in self.sources if source["source_key"] == source_key)
        source = {
            **selected,
            "seed_host": "events.example.test",
            "handoff_only": True,
            "approved_origins": ["https://events.example.test"],
            "reviewed_at": "2030-01-01T00:00:00Z",
            "review_expires_at": None,
            "refresh_interval_minutes": 360,
            "min_interval_ms": 1500,
            "page_limit": 10,
            "source_revision": 4,
            "policy_blocked": False,
        }
        run = {
            "source_key": source_key,
            "display_name": source["display_name"],
            **source["latest_run"],
            "duration_ms": 60_000,
            "source_revision": 4,
            "release_revision": "abc1234",
            "image_digest": f"sha256:{'1' * 64}",
            "provenance_status": "claim_recorded",
            "trigger": "admin_source",
        }
        return {
            "generated_at": "2030-06-01T12:00:00Z",
            "source": source,
            "window": {
                "hours": 168,
                "bucket_hours": 24,
                "starts_at": "2030-05-25T12:00:00Z",
                "ends_at": "2030-06-01T12:00:00Z",
            },
            "summary": {
                "total_runs": 1,
                "succeeded_runs": 1,
                "failed_runs": 0,
                "running_runs": 0,
                "success_rate": 1.0,
                "candidate_count": 25,
                "canonical_count": 23,
                "yield_rate": 0.92,
                "average_duration_ms": 60_000,
                "p95_duration_ms": 60_000,
                "latest_success_at": "2030-05-31T12:00:00Z",
                "latest_failure_at": None,
            },
            "history": [
                {
                    "bucket_start": "2030-05-31T00:00:00Z",
                    "total_runs": 1,
                    "succeeded_runs": 1,
                    "failed_runs": 0,
                    "candidate_count": 25,
                    "canonical_count": 23,
                    "average_duration_ms": 60_000,
                }
            ],
            "recent_runs": [run],
            "current_build": {
                "release_revision": "abc1234",
                "image_digest": f"sha256:{'1' * 64}",
            },
        }

    @staticmethod
    def _json(route: Route, payload: Any, *, status: int = 200) -> None:
        route.fulfill(
            status=status,
            content_type="application/json; charset=utf-8",
            body=json.dumps(payload),
        )


def _open_admin(
    page: Page,
    scenario: AdminApiScenario,
    product_server: str,
    suffix: str = "",
) -> None:
    scenario.install(page)
    response = page.goto(f"{product_server}/admin{suffix}", wait_until="load")
    assert response is not None
    assert response.status == 200
    expect(page.locator("#admin-shell")).to_be_visible()
    expect(page.locator("#connection-status")).to_contain_text("Control plane connected")


def test_admin_renders_overview_sources_and_untrusted_copy_as_text(
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    page.clock.install(time="2030-06-01T12:00:00Z")
    unsafe_name = '<img src=x onerror="window.__adminInjected = true">'
    scenario = AdminApiScenario(
        sources=[
            _source(
                display_name=unsafe_name,
                publisher="<script>window.__adminInjected = true</script>",
                seed_url="javascript:window.__adminInjected=true",
            )
        ]
    )

    _open_admin(page, scenario, product_server)

    expect(page.locator("#policy-title")).to_have_text("Refresh network access is admitted")
    expect(page.locator("#metric-sources")).to_have_text("1")
    expect(page.locator("#metric-due")).to_have_text("2")
    expect(page.locator("#metric-events")).to_have_text("1,284")
    expect(page.locator("#fixture-summary")).to_contain_text("497 fixture sources hidden")
    row = page.locator("#source-rows tr")
    expect(row).to_have_count(1)
    expect(row).to_contain_text(unsafe_name)
    expect(row).to_contain_text(_SOURCE_KEY)
    expect(row).to_contain_text("23")
    expect(row.locator("img, script, a")).to_have_count(0)
    assert page.evaluate("window.__adminInjected") is None

    source_call = scenario.last("GET", "/admin/v1/ingestion/sources")
    assert source_call.query["include_fixtures"] == ["false"]
    assert source_call.query["limit"] == ["50"]

    headers = page.context.request.get(f"{product_server}/admin").headers
    assert headers["x-frame-options"] == "DENY"
    assert "connect-src 'self'" in headers["content-security-policy"]


def test_overview_source_launcher_finds_luma_and_opens_its_workspace(
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    scenario = AdminApiScenario(
        sources=[
            _source(),
            _source(
                source_key="luma-genai-sf",
                display_name="Generative AI SF",
                publisher="Generative AI SF",
                seed_url="https://luma.com/genai-sf",
                effective_status="active",
            ),
        ]
    )

    _open_admin(page, scenario, product_server)

    page.locator("#overview-source-query").fill("luma")
    result = page.locator("#overview-source-results .source-launcher-result")
    expect(result).to_have_count(1)
    expect(result).to_contain_text("Generative AI SF")
    expect(result).to_contain_text("luma.com")
    expect(page.locator("#overview-source-index-summary")).to_contain_text(
        "search also checks source URLs and keys"
    )

    result.click()
    expect(page.locator("#source-detail-dialog")).to_be_visible()
    expect(page.locator("#source-detail-title")).to_have_text("Generative AI SF")
    expect(page.locator("#source-detail-key")).to_have_text("luma-genai-sf")
    assert "source=luma-genai-sf" in page.url


def test_overview_prioritizes_actions_connected_pipeline_and_progressive_disclosure(
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    scenario = AdminApiScenario()

    _open_admin(page, scenario, product_server)

    expect(page.locator("#overview-title")).to_have_text("Run the catalog")
    expect(page.locator("#operational-diagnostics")).not_to_have_attribute("open", "")
    expect(page.locator("#overview-metrics")).to_be_hidden()
    expect(page.locator("#pipeline-chart .flow-stage")).to_have_count(5)
    expect(page.locator("#pipeline-chart .flow-connector")).to_have_count(4)

    normalize = page.locator('[data-overview-stage="normalize"]')
    normalize.click()
    expect(normalize).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#overview-stage-detail")).to_contain_text("Extraction & normalization")
    expect(page.locator("#overview-stage-detail")).to_contain_text("loaded page")

    page.locator("#operational-diagnostics > summary").click()
    expect(page.locator("#overview-metrics")).to_be_visible()
    expect(page.locator("#source-type-breakdown")).to_be_visible()
    expect(page.locator(".diagnostic-note")).to_contain_text(
        "Luma source, for example, appears here as Public JSON-LD"
    )

    page.locator('[data-overview-route="due-sources"]').first.click()
    expect(page.locator("#sources-panel")).to_be_visible()
    expect(page.locator("#source-state")).to_have_value("due")
    page.wait_for_function(
        "() => new URL(window.location.href).searchParams.get('source_state') === 'due'"
    )
    source_call = scenario.last("GET", "/admin/v1/ingestion/sources")
    assert source_call.query["state"] == ["due"]


def test_overview_pipeline_remains_unfiltered_after_run_ledger_drilldown(
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    scenario = AdminApiScenario(
        runs=[
            _run(
                "cadence:succeeded",
                release_revision="worker-current",
                image_digest=f"sha256:{'4' * 64}",
                provenance_status="claim_recorded",
                completed_at="2030-05-31T12:00:00Z",
                duration_ms=4_000,
            ),
            _run(
                "cadence:failed",
                release_revision="worker-current",
                image_digest=f"sha256:{'4' * 64}",
                provenance_status="claim_recorded",
                completed_at="2030-05-31T11:00:00Z",
                duration_ms=5_000,
                status="failed",
            ),
        ]
    )

    _open_admin(page, scenario, product_server)

    normalize_value = page.locator(
        '[data-overview-stage="normalize"] .flow-stage-value'
    )
    expect(normalize_value).to_have_text("50")

    page.locator('[data-overview-route="failed-runs"]').first.click()
    expect(page.locator("#runs-panel")).to_be_visible()
    expect(page.locator("#run-status")).to_have_value("failed")
    expect(page.locator("#run-list .activity-item")).to_have_count(1)

    page.locator('[data-admin-tab][href="#overview"]').click()
    expect(normalize_value).to_have_text("50")
    expect(page.locator('[data-overview-stage="collect"] .flow-stage-value')).to_have_text("2")


def test_overview_small_screen_keeps_navigation_and_diagnostic_controls_available(
    page_factory, product_server: str
) -> None:
    harness = page_factory(width=390, height=896)
    page = harness.page
    scenario = AdminApiScenario()

    _open_admin(page, scenario, product_server)

    nav_geometry = page.locator(".console-tabs").evaluate(
        "nav => ({ client: nav.clientWidth, scroll: nav.scrollWidth })"
    )
    assert nav_geometry["scroll"] <= nav_geometry["client"] + 2
    expect(page.locator('[data-admin-tab][href="#logic"]')).to_have_text("Changes")

    page.locator("#operational-diagnostics > summary").click()
    controls = page.locator("#operational-diagnostics .panel-tools .segmented-button")
    expect(controls).to_have_count(3)
    assert all(
        box is not None and box["width"] > 0 and box["height"] > 0
        for box in [controls.nth(index).bounding_box() for index in range(3)]
    )


def test_overview_tablet_pipeline_fits_without_hidden_stage_overflow(
    page_factory, product_server: str
) -> None:
    harness = page_factory(width=760, height=896)
    page = harness.page
    scenario = AdminApiScenario()

    _open_admin(page, scenario, product_server)

    flow_geometry = page.locator("#pipeline-chart").evaluate(
        "flow => ({ client: flow.clientWidth, scroll: flow.scrollWidth })"
    )
    assert flow_geometry["scroll"] <= flow_geometry["client"] + 2
    expect(page.locator("#pipeline-chart .flow-stage")).to_have_count(5)


def test_policy_blocked_state_disables_all_refresh_controls(
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    scenario = AdminApiScenario(
        policy_allowed=False,
        sources=[_source(effective_status="policy_blocked")],
    )

    _open_admin(page, scenario, product_server)

    expect(page.locator("#policy-title")).to_have_text("Refresh network access is blocked")
    expect(page.locator("#policy-code")).to_have_text("modality_disabled")
    expect(page.locator("#open-refresh-due")).to_be_disabled()
    expect(page.locator("#source-rows .source-refresh")).to_be_disabled()
    page.locator("#source-rows .source-refresh").evaluate("(button) => button.click()")
    assert scenario.count("POST", "/admin/v1/ingestion/commands") == 0


def test_source_refresh_posts_once_then_polls_to_terminal_completion(
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    page.clock.install(time="2030-06-01T12:00:00Z")
    # Keep the command queued through the post-acceptance refresh so the polling indicator is
    # observable, then complete it on the second timer-driven poll.
    scenario = AdminApiScenario(complete_command_on_get=4)
    _open_admin(page, scenario, product_server)
    page.locator('[data-admin-tab][href="#sources"]').click()

    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and urlsplit(response.url).path == "/admin/v1/ingestion/commands"
        )
    ) as accepted:
        page.locator("#source-rows .source-refresh").click()
    assert accepted.value.status == 202

    expect(page.locator("#admin-toast")).to_contain_text(
        f"Refresh command accepted for {_SOURCE_KEY}."
    )
    expect(page.locator("#command-list")).to_contain_text("Queued")
    expect(page.locator("#command-list")).to_contain_text("Accepted by")
    expect(page.locator("#command-list")).to_contain_text("api-abc1234")
    expect(page.locator("#command-list")).to_contain_text("Claimed by")
    expect(page.locator("#command-list")).to_contain_text("Not claimed yet")
    page.locator('[data-admin-tab][href="#commands"]').click()
    expect(page.locator("#command-polling")).to_be_visible()
    command_call = scenario.last("POST", "/admin/v1/ingestion/commands")
    assert command_call.body["action"] == "refresh_source"
    assert command_call.body["source_key"] == _SOURCE_KEY
    assert UUID(command_call.body["command_id"]).version == 4
    assert scenario.count("POST", "/admin/v1/ingestion/commands") == 1

    page.clock.fast_forward(6_200)
    expect(page.locator("#command-list")).to_contain_text("Completed")
    expect(page.locator("#command-list")).to_contain_text("Outcome: succeeded")
    expect(page.locator("#command-list")).to_contain_text("Canonical Count: 23")
    expect(page.locator("#command-list")).to_contain_text("worker-def5678")
    expect(page.locator("#command-list")).to_contain_text("source r5")
    expect(page.locator("#command-polling")).to_be_hidden()
    assert scenario.command_gets >= 3


def test_source_deep_link_opens_end_to_end_history_and_revision_ownership(
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    page.clock.install(time="2030-06-01T12:00:00Z")
    scenario = AdminApiScenario()

    _open_admin(
        page,
        scenario,
        product_server,
        f"?tab=sources&source={_SOURCE_KEY}&fixtures=true",
    )

    expect(page.locator("#source-detail-dialog")).to_be_visible()
    expect(page.locator("#source-detail-title")).to_have_text("Bay Area Arts")
    expect(page.locator("#source-detail-summary")).to_contain_text("92%")
    expect(page.locator("#source-detail-pipeline")).to_contain_text("Normalize & dedupe")
    expect(page.locator("#source-detail-history svg")).to_have_count(1)
    expect(page.locator("#source-detail-logic")).to_contain_text("adapters/crawl/source.py")
    expect(page.locator("#source-detail-logic")).to_contain_text("application/catalog_refresh.py")
    expect(page.locator("#source-detail-logic")).to_contain_text("abc1234")
    expect(page.locator("#source-detail-logic")).to_contain_text("Worker claim")
    detail_call = scenario.last("GET", f"/admin/v1/ingestion/sources/{_SOURCE_KEY}")
    assert detail_call.query["window_hours"] == ["168"]
    assert detail_call.query["include_fixtures"] == ["true"]
    assert f"source={_SOURCE_KEY}" in page.url


@pytest.mark.parametrize("viewport_width", [1406, 760])
def test_admin_detail_and_logic_layouts_contain_dynamic_content(
    page_factory, product_server: str, viewport_width: int
) -> None:
    harness = page_factory(width=viewport_width, height=896)
    page = harness.page
    scenario = AdminApiScenario(
        runs=[
            _run(
                "admin:new-worker",
                release_revision="worker-new",
                image_digest=f"sha256:{'2' * 64}",
                provenance_status="claim_recorded",
                completed_at="2030-05-31T12:00:00Z",
                duration_ms=4_400,
            ),
            _run(
                "admin:legacy-worker",
                release_revision=None,
                image_digest=None,
                provenance_status="legacy_unavailable",
                completed_at="2030-05-30T12:00:00Z",
                duration_ms=7_800,
            ),
        ]
    )

    _open_admin(
        page,
        scenario,
        product_server,
        f"?tab=sources&source={_SOURCE_KEY}&fixtures=true",
    )

    section_geometry = page.locator(".source-detail-body > .detail-section").evaluate_all(
        """sections => sections.map(section => {
          const box = section.getBoundingClientRect();
          return {
            id: section.id,
            top: box.top,
            bottom: box.bottom,
            verticalOverflow: Math.max(0, section.scrollHeight - section.clientHeight),
          };
        })"""
    )
    assert all(item["verticalOverflow"] <= 2 for item in section_geometry)
    assert all(
        current["bottom"] <= following["top"] + 2
        for current, following in pairwise(section_geometry)
    )

    painted_geometry = page.evaluate(
        """() => {
          const inspect = (sectionSelector, childSelector) => {
            const section = document.querySelector(sectionSelector);
            const sectionBox = section.getBoundingClientRect();
            return [...section.querySelectorAll(childSelector)]
              .filter(child => child.getClientRects().length)
              .map(child => {
                const childBox = child.getBoundingClientRect();
                return {
                  topOverflow: Math.max(0, sectionBox.top - childBox.top),
                  bottomOverflow: Math.max(0, childBox.bottom - sectionBox.bottom),
                };
              });
          };
          const tableRegions = [...document.querySelectorAll(".detail-table-scroll")]
            .map(region => {
              const regionBox = region.getBoundingClientRect();
              const tableBox = region.querySelector("table").getBoundingClientRect();
              return {
                topOverflow: Math.max(0, regionBox.top - tableBox.top),
                bottomOverflow: Math.max(0, tableBox.bottom - regionBox.bottom),
              };
            });
          return [
            ...inspect("#source-detail-history", ":scope > *"),
            ...inspect("#source-detail-logic", ":scope > *"),
            ...tableRegions,
          ];
        }"""
    )
    assert painted_geometry
    assert all(
        item["topOverflow"] <= 2 and item["bottomOverflow"] <= 2 for item in painted_geometry
    )

    header_top = page.locator(".source-detail-header").bounding_box()
    assert header_top is not None
    page.locator('.source-detail-nav a[href="#source-detail-logic"]').click()
    page.wait_for_function("() => document.querySelector('.source-detail-body').scrollTop > 0")
    scroll_geometry = page.evaluate(
        """() => ({
          bodyScroll: document.querySelector(".source-detail-body").scrollTop,
          dialogScroll: document.querySelector("#source-detail-dialog").scrollTop,
          headerTop: document.querySelector(".source-detail-header")
            .getBoundingClientRect().top,
        })"""
    )
    assert scroll_geometry["bodyScroll"] > 0
    assert scroll_geometry["dialogScroll"] == 0
    assert abs(scroll_geometry["headerTop"] - header_top["y"]) <= 2
    expect(page.locator(".source-detail-header")).to_be_visible()
    expect(page.locator(".source-detail-nav")).to_be_visible()

    page.locator("#source-detail-close").click()
    if viewport_width == 760:
        tablet_table = page.locator(".admin-table-frame").evaluate(
            """frame => ({
              tableDisplay: getComputedStyle(frame.querySelector("table")).display,
              rowHeight: frame.querySelector("tbody tr").getBoundingClientRect().height,
              clientWidth: frame.clientWidth,
              scrollWidth: frame.scrollWidth,
            })"""
        )
        assert tablet_table["tableDisplay"] == "table"
        assert tablet_table["rowHeight"] < 100
        assert tablet_table["scrollWidth"] > tablet_table["clientWidth"]

    page.locator('[data-admin-tab][href="#logic"]').click()
    expect(page.locator("#logic-revision-timeline .revision-group")).to_have_count(2)
    expect(page.locator("#logic-revision-timeline .revision-heading")).to_have_count(2)
    expect(
        page.locator("#logic-revision-timeline .revision-group").first.locator(":scope > span")
    ).to_have_count(3)

    logic_geometry = page.locator("#logic-panel").evaluate(
        """panel => {
          const panelBox = panel.getBoundingClientRect();
          const grid = panel.querySelector(".logic-grid");
          const gridBox = grid.getBoundingClientRect();
          const cards = [...grid.children].map(card => {
            const box = card.getBoundingClientRect();
            return { top: box.top, bottom: box.bottom, height: box.height };
          });
          const groups = [...panel.querySelectorAll(".revision-timeline .revision-group")]
            .map(group => {
              const box = group.getBoundingClientRect();
              return {
                height: box.height,
                verticalOverflow: Math.max(0, group.scrollHeight - group.clientHeight),
              };
            });
          const contained = [
            grid,
            ...panel.querySelectorAll(":scope > .ops-card"),
          ].every(item => {
            const box = item.getBoundingClientRect();
            return (
              box.left >= panelBox.left - 2 &&
              box.right <= panelBox.right + 2 &&
              box.top >= panelBox.top - 2 &&
              box.bottom <= panelBox.bottom + 2
            );
          });
          return {
            cards,
            groups,
            contained,
            gridOverflow: Math.max(0, grid.scrollWidth - grid.clientWidth),
            panelOverflow: Math.max(0, panel.scrollWidth - panel.clientWidth),
            pageOverflow: Math.max(
              0,
              document.documentElement.scrollWidth -
                document.documentElement.clientWidth,
            ),
            timelineHeight: panel
              .querySelector(".revision-timeline")
              .getBoundingClientRect().height,
            gridTop: gridBox.top,
          };
        }"""
    )
    assert logic_geometry["contained"]
    assert logic_geometry["gridOverflow"] <= 2
    assert logic_geometry["panelOverflow"] <= 2
    assert logic_geometry["pageOverflow"] <= 2
    assert logic_geometry["timelineHeight"] < 180
    assert all(item["height"] < 120 for item in logic_geometry["groups"])
    assert all(item["verticalOverflow"] <= 2 for item in logic_geometry["groups"])
    assert all(item["height"] < 260 for item in logic_geometry["cards"]), logic_geometry
    first_card, second_card = logic_geometry["cards"]
    if viewport_width > 760:
        assert abs(first_card["top"] - second_card["top"]) <= 2
    else:
        assert first_card["bottom"] <= second_card["top"] + 2


def test_run_window_restores_from_url_and_queries_server_side(
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    scenario = AdminApiScenario()

    _open_admin(page, scenario, product_server, "?tab=runs&run_window=7d")

    expect(page.locator("#runs-panel")).to_be_visible()
    expect(page.locator("#run-window")).to_have_value("7d")
    first_run_call = scenario.last("GET", "/admin/v1/ingestion/runs")
    assert first_run_call.query["window_hours"] == ["168"]

    page.locator("#run-window").select_option("30d")
    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/admin/v1/ingestion/runs"
    ):
        page.locator("#run-filters").get_by_role("button", name="Apply").click()
    latest_run_call = scenario.last("GET", "/admin/v1/ingestion/runs")
    assert latest_run_call.query["window_hours"] == ["720"]
    assert "run_window=30d" in page.url


def test_due_refresh_requires_confirmation_and_posts_no_source_key(
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    scenario = AdminApiScenario()
    _open_admin(page, scenario, product_server)

    page.locator("#open-refresh-due").click()
    dialog = page.locator("#refresh-due-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("2 sources are currently due.")
    dialog.get_by_role("button", name="Not now").click()
    expect(dialog).to_be_hidden()
    assert scenario.count("POST", "/admin/v1/ingestion/commands") == 0

    page.locator("#open-refresh-due").click()
    page.locator("#confirm-refresh-due").click()
    expect(page.locator("#admin-toast")).to_contain_text("Due-source refresh command accepted.")
    command_call = scenario.last("POST", "/admin/v1/ingestion/commands")
    assert command_call.body["action"] == "refresh_due"
    assert "source_key" not in command_call.body
    assert scenario.count("POST", "/admin/v1/ingestion/commands") == 1
