"""Exercise the built command investigation against bounded browser-only API fixtures.

EC_ADMIN_WEB_URL points at a separate Next.js build. No live backend call, worker,
provider, database, or operator write is involved. The one POST is intercepted here.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, Route, expect

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]
COMMAND = "926b8762-6d9f-5ade-b85a-b024ceea89cb"
ACCEPTED = "826b8762-6d9f-5ade-b85a-b024ceea89cc"
STAMP = "2026-09-08T16:10:00Z"
START = "2026-09-08T16:00:00Z"
SOURCE = "bay-arts-01"
RUN = "catalog:bay-arts-01:fixed-20260908"


def command(command_id: str = COMMAND) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "action": "refresh_source",
        "source_key": SOURCE,
        "status": "running",
        "requested_at": START,
        "started_at": START,
        "completed_at": None,
        "result": None,
        "error_code": None,
        "source_revision": 4,
        "release_revision": "fixture-dirty",
        "image_digest": None,
        "executor_release_revision": "fixture-dirty",
        "executor_source_revision": 4,
        "executor_image_digest": None,
        "requested_by": "fixture-operator",
        "attempt_count": 7,
        "available_at": START,
        "lease_expires_at": "2026-09-08T16:12:00Z",
        "worker_state": "heartbeat_live",
    }


def activity(event_id: int, **values: Any) -> dict[str, Any]:
    result = {
        "event_id": str(event_id),
        "observed_at": STAMP,
        "event_code": "progress",
        "command_attempt": 7,
        "source_key": SOURCE,
        "run_key": RUN,
        "task_attempt": 2,
        "stage": "collect",
        "outcome_code": "progressed",
        "duration_ms": 12000,
        "candidate_count": 24,
        "canonical_count": None,
        "request_count": 4,
        "page_count": 3,
        "error_type": None,
        "worker_id": "fixture-catalog-executor",
        "release_revision": "fixture-dirty",
        "image_digest": None,
    }
    return result | values


def source_run() -> dict[str, Any]:
    return {
        "status": "running",
        "attempt_count": 2,
        "source_revision": 4,
        "stage_trace": [
            {
                "stage": "collect",
                "label": "Collect",
                "evidence_status": "measured",
                "duration_ms": 12000,
                "last_observed_at": STAMP,
                "last_outcome_code": "progressed",
            }
        ],
        "resources": {
            "execution_count": 1,
            "wall_time_ms": 12000,
            "process_cpu_time_ms": 230,
            "boundary_observed_peak_rss_bytes": 33554432,
            "measurement_scope": "worker_process_boundary_samples",
            "measurement_quality": "best_effort_process_delta_sequential_worker",
        },
        "execution": {
            "execution_path": "guarded_direct",
            "worker_service": "ingestion-command-worker",
            "task_queue": None,
            "adapter_module": "events_concierge.adapters.bibliocommons.source",
            "adapter_symbol": "BiblioCommonsCatalogFetcher.fetch",
            "orchestration_module": "events_concierge.application.catalog_refresh",
            "orchestration_symbol": "CatalogRefreshService.refresh",
            "worker_module": "events_concierge.workers.ingestion_commands",
            "worker_symbol": "run_ingestion_commands",
        },
    }


@dataclass
class CommandApi:
    failed: bool = False
    submitted: bool = False
    calls: list[tuple[str, str, dict[str, list[str]]]] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(
        default_factory=lambda: [
            activity(
                1,
                command_attempt=6,
                task_attempt=1,
                event_code="error",
                error_type="timeout",
                outcome_code="failed",
                request_count=1,
                page_count=None,
            ),
            activity(2, event_code="stage_started", request_count=None, page_count=None),
            activity(3),
        ]
    )

    def handle(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        query = parse_qs(urlsplit(route.request.url).query)
        self.calls.append((route.request.method, path, query))
        if path == "/admin/v1/operator/session":
            self.respond(
                route,
                {
                    "subject": "fixture-operator",
                    "role": "operator",
                    "environment": "local",
                    "authentication": "local",
                    "capabilities": ["ingestion.refresh"],
                },
            )
        elif path == "/admin/v1/ingestion/overview":
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "policy": {"allowed": True, "reason": "Fixture", "code": "allowed"},
                    "summary": {
                        "sources": 2,
                        "active_sources": 2,
                        "due_sources": 1,
                        "running_runs": 1,
                        "failed_runs_24h": 0,
                        "catalog_events": 12,
                        "pending_commands": 1,
                        "fixture_sources": 0,
                    },
                    "latest_success_at": STAMP,
                },
            )
        elif path == "/admin/v1/ingestion/source-health":
            self.respond(route, {"generated_at": STAMP, "total": 0, "sources": []})
        elif path == "/admin/v1/operations/overview":
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "environment": "local",
                    "release_revision": "fixture-dirty",
                    "image_digest": None,
                    "schema_revisions": ["0183"],
                    "measurement_scope": "durable_queue_state",
                    "worker_liveness": "not_measured",
                    "queues": [],
                },
            )
        elif path == "/admin/v1/ingestion/commands":
            if route.request.method == "POST":
                self.submitted = True
                self.respond(route, command(ACCEPTED))
            else:
                self.respond(
                    route, {"items": [command(ACCEPTED)] if self.submitted else [command()]}
                )
        elif re.fullmatch(r"/admin/v1/ingestion/commands/[0-9a-f-]+", path):
            current = command(path.rsplit("/", 1)[-1])
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "command": current,
                    "progress": {
                        "total": 2,
                        "pending": 1,
                        "running": 1,
                        "succeeded": 0,
                        "failed": 0,
                        "paused": 0,
                        "completed": 0,
                        "active_source_key": SOURCE,
                        "active_display_name": "Bay Arts",
                        "active_phase": "collecting",
                        "updated_at": START,
                    },
                    "runs": [
                        {
                            "position": 1,
                            "source_key": SOURCE,
                            "display_name": "Bay Arts",
                            "run_key": RUN,
                            "status": "running",
                            "phase": "collecting",
                            "linked_at": START,
                            "started_at": START,
                            "completed_at": None,
                            "candidate_count": 24,
                            "canonical_count": None,
                            "error_code": None,
                            "attempt_count": 2,
                            "duration_ms": None,
                            "updated_at": STAMP,
                        }
                    ],
                },
            )
        elif path.endswith("/investigation"):
            if self.failed:
                self.respond(route, {"detail": "Fixture evidence unavailable"}, 503)
                return
            task = {
                "position": 1,
                "source_key": SOURCE,
                "run_key": RUN,
                "status": "running",
                "attempt_count": 2,
                "available_at": START,
                "started_at": START,
                "completed_at": None,
                "last_outcome_code": None,
                "candidate_count": 24,
                "canonical_count": None,
                "lease_state": "live",
                "last_progress_at": STAMP,
                "run": source_run() if query.get("source_key", [SOURCE])[0] == SOURCE else None,
            }
            other = dict(
                task,
                position=2,
                source_key="bay-arts-02",
                run_key="second-fixed-run",
                status="pending",
                attempt_count=0,
                lease_state="not_running",
                run=None,
                candidate_count=None,
                last_progress_at=None,
            )
            after = int(query.get("after_event_id", ["0"])[0])
            events = [
                copy.deepcopy(event) for event in self.events if int(event["event_id"]) > after
            ]
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "command_id": path.split("/")[-2],
                    "plan": {"status": "fixed", "created_at": START, "tasks": [task, other]},
                    "attempts": [
                        {
                            "attempt_count": n,
                            "kind": "reclaim" if n == 7 else "initial",
                            "observed_at": START,
                            "claimed_at": START,
                            "last_heartbeat_at": STAMP,
                            "last_progress_at": STAMP,
                            "ended_at": STAMP if n == 6 else None,
                            "outcome_code": "timeout" if n == 6 else None,
                            "worker_id": "fixture-catalog-executor",
                            "release_revision": "fixture-dirty",
                            "image_digest": None,
                        }
                        for n in [7, 6]
                    ],
                    "events": events,
                    "next_event_id": events[-1]["event_id"] if events else None,
                    "has_more": False,
                    "evidence": {
                        "events_since": START,
                        "history_complete": False,
                        "logs": "structured_events_only",
                    },
                },
            )
        else:
            self.unexpected.append(f"{route.request.method} {path}")
            self.respond(route, {"detail": "Unexpected fixture endpoint"}, 404)

    @staticmethod
    def respond(route: Route, body: dict[str, Any], status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def investigation_page(browser: Browser) -> Iterator[tuple[Page, CommandApi, str]]:
    context = browser.new_context(
        viewport={"width": 1600, "height": 1100},
        reduced_motion="reduce",
        permissions=["clipboard-read", "clipboard-write"],
    )
    page = context.new_page()
    page.set_default_timeout(7000)
    scenario = CommandApi()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/admin/v1/**", scenario.handle)
    try:
        yield page, scenario, os.environ["EC_ADMIN_WEB_URL"].rstrip("/")
        assert not errors, errors
        assert not scenario.unexpected, scenario.unexpected
    finally:
        page.unroute_all(behavior="ignoreErrors")
        context.close()


def open_command(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/admin?tab=commands&command={COMMAND}")
    expect(page.get_by_role("heading", name="Bay Arts · collect", exact=True)).to_be_visible()


def test_exact_command_reload_back_and_source_attempt_selection(
    investigation_page: tuple[Page, CommandApi, str],
) -> None:
    page, scenario, base_url = investigation_page
    open_command(page, base_url)
    page.get_by_label("Command attempt", exact=True).select_option("6")
    page.get_by_role("tab", name="Activity", exact=True).click()
    expect(page.get_by_label("Command activity tail")).to_contain_text("timeout")
    page.get_by_role("tab", name="Overview", exact=True).click()
    page.get_by_label("Command source executions").get_by_role(
        "button", name=re.compile("^Bay Arts ")
    ).click()
    query = parse_qs(urlsplit(page.url).query)
    assert query["command"] == [COMMAND]
    assert query["command_attempt"] == ["6"]
    assert query["command_source"] == [SOURCE]
    assert query["command_run"] == [RUN]
    page.reload()
    expect(page.get_by_label("Command attempt", exact=True)).to_have_value("6")
    expect(page.get_by_role("tab", name="Execution", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.get_by_role("region", name="Selected source run evidence")).to_be_visible()
    page.go_back()
    expect(page.get_by_role("region", name="Selected source run evidence")).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query)["command_attempt"] == ["6"]
    assert any(
        query.get("source_key") == [SOURCE]
        for _, path, query in scenario.calls
        if path.endswith("investigation")
    )


def test_progress_metrics_stage_code_copy_and_severity(
    investigation_page: tuple[Page, CommandApi, str],
) -> None:
    page, _, base_url = investigation_page
    open_command(page, base_url)
    page.get_by_label("Command attempt", exact=True).select_option("7")
    page.get_by_label("Command source executions").get_by_role(
        "button", name=re.compile("^Bay Arts ")
    ).click()
    evidence = page.get_by_role("region", name="Selected source run evidence")
    expect(evidence.get_by_role("cell", name="measured", exact=True)).to_be_visible()
    expect(evidence).to_contain_text("CPU 230 ms")
    evidence.get_by_text("Code ownership and execution route", exact=True).click()
    evidence.get_by_role("button", name="Copy path", exact=True).first.click()
    expect(evidence.get_by_role("status")).to_have_text("Path copied")
    assert (
        page.evaluate("navigator.clipboard.readText()")
        == "src/events_concierge/adapters/bibliocommons/source.py"
    )
    page.get_by_text("Dispatch lease, timing and build evidence", exact=True).click()
    expect(
        page.get_by_role("tabpanel", name="Execution", exact=True)
        .get_by_text(re.compile("fixture-dirty · local or unpublished"))
        .first
    ).to_be_visible()
    page.get_by_role("tab", name="Activity", exact=True).click()
    activity_panel = page.get_by_role("tabpanel", name="Activity", exact=True)
    expect(
        activity_panel.locator("dt", has_text="Requests completed").locator("+ dd")
    ).to_have_text("4")
    expect(activity_panel.locator("dt", has_text="Pages completed").locator("+ dd")).to_have_text(
        "3"
    )
    page.get_by_role("button", name="Errors", exact=True).click()
    expect(page.get_by_label("Command activity tail")).to_contain_text(
        "No recorded activity matches"
    )
    page.get_by_label("Command attempt", exact=True).select_option("6")
    expect(page.get_by_label("Command activity tail")).to_contain_text("timeout")
    if preview := os.environ.get("EC_ADMIN_PREVIEW_PATH"):
        page.get_by_role("button", name="All activity", exact=True).click()
        page.get_by_label("Command attempt", exact=True).select_option("7")
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=preview, full_page=True)


def test_pause_cursor_resume_and_failed_refresh_retains_snapshot(
    investigation_page: tuple[Page, CommandApi, str],
) -> None:
    page, scenario, base_url = investigation_page
    open_command(page, base_url)
    page.get_by_role("tab", name="Activity", exact=True).click()
    page.get_by_role("button", name="Pause following", exact=True).click()
    expect(page.get_by_text("Following paused", exact=True)).to_be_visible()
    before = len([path for _, path, _ in scenario.calls if path.endswith("investigation")])
    scenario.events.append(activity(4, request_count=8, page_count=6))
    page.wait_for_timeout(2800)
    assert len([path for _, path, _ in scenario.calls if path.endswith("investigation")]) == before
    expect(page.get_by_label("Command activity tail")).not_to_contain_text("8 requests completed")
    page.get_by_role("button", name="Follow live", exact=True).click()
    expect(page.get_by_label("Command activity tail")).to_contain_text("8 requests completed")
    assert any(
        query.get("after_event_id") == ["3"]
        for _, path, query in scenario.calls
        if path.endswith("investigation")
    )
    scenario.failed = True
    page.get_by_role("button", name="Pause following", exact=True).click()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_text("Stale snapshot", exact=True)).to_be_visible()
    expect(page.get_by_label("Command activity tail")).to_contain_text("8 requests completed")
    scenario.failed = False
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_text("Stale snapshot", exact=True)).to_have_count(0)


def test_track_command_opens_exact_accepted_receipt(
    investigation_page: tuple[Page, CommandApi, str],
) -> None:
    page, scenario, base_url = investigation_page
    page.goto(f"{base_url}/admin?tab=commands")
    page.get_by_role("button", name="Run due sources", exact=True).click()
    page.get_by_role("button", name="Track command", exact=True).click()
    assert scenario.submitted
    assert parse_qs(urlsplit(page.url).query)["command"] == [ACCEPTED]
    expect(page.get_by_role("heading", name="Refresh source", exact=True, level=2)).to_be_visible()


@pytest.mark.parametrize("width", [390, 1024])
def test_command_investigation_has_no_body_overflow(
    investigation_page: tuple[Page, CommandApi, str], width: int
) -> None:
    page, _, base_url = investigation_page
    page.set_viewport_size({"width": width, "height": 844})
    open_command(page, base_url)
    expect(page.get_by_role("button", name="Show queue", exact=True)).to_be_visible()
    page.get_by_label("Command source executions").get_by_role(
        "button", name=re.compile("^Bay Arts ")
    ).click()
    page.get_by_text("Code ownership and execution route", exact=True).click()
    dimensions = page.evaluate("""() => ({viewport: document.documentElement.clientWidth,
        html: document.documentElement.scrollWidth, body: document.body.scrollWidth})""")
    assert dimensions["html"] <= dimensions["viewport"], dimensions
    assert dimensions["body"] <= dimensions["viewport"], dimensions


def test_investigation_tabs_preserve_context_and_keyboard_focus(
    investigation_page: tuple[Page, CommandApi, str],
) -> None:
    page, _, base_url = investigation_page
    open_command(page, base_url)
    initial_url = page.url
    overview = page.get_by_role("tab", name="Overview", exact=True)
    overview.focus()
    overview.press("ArrowRight")
    activity_tab = page.get_by_role("tab", name="Activity", exact=True)
    expect(activity_tab).to_be_focused()
    expect(page.get_by_role("tabpanel", name="Activity", exact=True)).to_be_visible()
    expect(page.get_by_role("tabpanel", name="Overview", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Pause following", exact=True).click()
    activity_tab.press("End")
    expect(page.get_by_role("tab", name="Execution", exact=True)).to_be_focused()
    expect(page.get_by_text("Following paused", exact=True)).to_be_visible()
    assert page.url == initial_url


def test_queue_filters_keep_selected_command_open(
    investigation_page: tuple[Page, CommandApi, str],
) -> None:
    page, _, base_url = investigation_page
    open_command(page, base_url)
    page.get_by_role("textbox", name="Find a command", exact=True).fill("no-match")
    expect(page.get_by_text("No recent commands match.", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Refresh source", exact=True, level=2)).to_be_visible()
    page.get_by_role("textbox", name="Find a command", exact=True).fill(COMMAND)
    page.get_by_label("Command status", exact=True).select_option("failed")
    expect(page.get_by_text("No recent commands match.", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["command"] == [COMMAND]
