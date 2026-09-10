"""Exact-run failure explanations never borrow another run's error or a denied snapshot."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, Route, expect
from tests.e2e.test_admin_workspaces import RUN, SOURCE, STAMP, START, WorkspaceApi

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]
COMMAND = "e952bcd1-0564-5457-93a8-4eaf93fba9fd"
PATH = f"/admin/v1/ingestion/commands/{COMMAND}/investigation"


def event(number: int, **changes: Any) -> dict[str, Any]:
    return {
        "event_id": str(number),
        "observed_at": f"2026-09-08T16:09:{number:02}Z",
        "event_code": "error",
        "command_attempt": 1,
        "source_key": SOURCE,
        "run_key": RUN,
        "task_attempt": 1,
        "stage": "collect",
        "outcome_code": "failed",
        "duration_ms": 102670,
        "candidate_count": None,
        "canonical_count": None,
        "request_count": None,
        "page_count": None,
        "error_type": "validation",
        "worker_id": "fixture-worker",
        "release_revision": "fixture-dirty",
        "image_digest": None,
    } | changes


@dataclass
class FailureApi(WorkspaceApi):
    events: list[dict[str, Any]] = field(default_factory=lambda: [event(n) for n in range(1, 8)])
    evidence_status: int = 200
    hold_evidence: bool = False
    pending: list[Route] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.run_rows[0]["command"] = {
            "command_id": COMMAND,
            "action": "refresh_due",
            "requested_at": START,
            "started_at": START,
            "completed_at": STAMP,
        }

    def payload(self) -> dict[str, Any]:
        return {
            "generated_at": STAMP,
            "command_id": COMMAND,
            "plan": {"status": "fixed", "created_at": START, "tasks": []},
            "attempts": [],
            "events": self.events,
            "next_event_id": None,
            "has_more": False,
            "evidence": {
                "events_since": START,
                "history_complete": False,
                "logs": "structured_events_only",
            },
        }

    def handle(self, route: Route) -> None:
        if urlsplit(route.request.url).path != PATH:
            super().handle(route)
            return
        assert route.request.method == "GET"
        params = parse_qs(urlsplit(route.request.url).query)
        assert params["source_key"] == [SOURCE] and params["limit"] == ["100"]
        self.calls.append((PATH, params))
        if self.hold_evidence:
            self.pending.append(route)
        else:
            self.respond(
                route,
                self.payload() if self.evidence_status == 200 else {"detail": "Unavailable"},
                self.evidence_status,
            )


@pytest.fixture
def failure_page(browser: Browser) -> Iterator[tuple[Page, FailureApi, str]]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 1100}, permissions=["clipboard-read", "clipboard-write"]
    )
    page = context.new_page()
    page.set_default_timeout(7000)
    scenario = FailureApi()
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


def open_run(page: Page, base: str) -> None:
    page.goto(f"{base}/admin?tab=runs&run_selection={SOURCE}%7C{RUN}")


def test_failure_details_filter_command_tail_and_copy_precise_context(failure_page) -> None:
    page, scenario, base = failure_page
    scenario.events[-1].update(
        observed_at="2026-09-08T16:09:07.000123+00:00", request_count=69, page_count=4
    )
    scenario.events += [
        event(8, source_key="other-source", error_type="network"),
        event(9, run_key="other-run", error_type="internal"),
    ]
    open_run(page, base)
    details = page.get_by_role("region", name="Failure details", exact=True)
    expect(
        details.get_by_text("Recorded validation error during collection", exact=True)
    ).to_be_visible()
    expect(details).to_contain_text("task attempt 1")
    expect(details).not_to_contain_text("network")
    expect(details).not_to_contain_text("internal")
    details.locator("summary").click()
    expect(details.get_by_role("listitem")).to_have_count(5)
    expect(details.get_by_text("69 requests · 4 pages", exact=True)).to_be_visible()
    expect(details.get_by_text("2026-09-08 16:09:07 UTC", exact=True).first).to_be_visible()
    expect(details).to_contain_text("1\u20135 of 7")
    details.get_by_role("button", name="Next worker events", exact=True).click()
    expect(details.get_by_role("listitem")).to_have_count(2)
    details.get_by_role("button", name="Copy log context", exact=True).click()
    expect(details.get_by_role("button", name="Copied", exact=True)).to_be_visible()
    copied = page.evaluate("navigator.clipboard.readText()")
    assert f"source_key={SOURCE}" in copied and f"run_key={RUN}" in copied
    assert "task_attempt=1" in copied and "error_type=validation" in copied
    expect(
        page.locator("#run-inspector").get_by_role("link", name="Command receipt", exact=True)
    ).to_have_attribute("href", re.compile(f"command={COMMAND}"))


@pytest.mark.parametrize("status", [401, 403, 503])
def test_failed_refresh_hides_previous_worker_error(failure_page, status: int) -> None:
    page, scenario, base = failure_page
    open_run(page, base)
    details = page.get_by_role("region", name="Failure details", exact=True)
    expect(
        details.get_by_text("Recorded validation error during collection", exact=True)
    ).to_be_visible()
    scenario.evidence_status = status
    details.get_by_role("button", name="Refresh failure details", exact=True).click()
    expect(details).to_contain_text(
        "Access to worker events was denied."
        if status in (401, 403)
        else "Worker events could not be read."
    )
    expect(
        details.get_by_text("Recorded validation error during collection", exact=True)
    ).to_have_count(0)
    expect(details.locator("summary")).to_have_count(0)


def test_missing_and_old_error_evidence_does_not_claim_latest_attempt_reason(failure_page) -> None:
    page, scenario, base = failure_page
    scenario.events = [event(1, observed_at="2026-09-07T16:00:00Z")]
    open_run(page, base)
    details = page.get_by_role("region", name="Failure details", exact=True)
    expect(details).to_contain_text("Earlier attempt error")
    expect(details).to_contain_text("precise failure reason is unavailable")
    expect(details.get_by_text("transport timeout", exact=True)).to_be_visible()
    scenario.events = [event(1, run_key="other-run")]
    details.get_by_role("button", name="Refresh failure details", exact=True).click()
    expect(details).to_contain_text("No error event for this run is available")
    scenario.run_rows[0]["command"] = None
    page.reload()
    expect(details).to_contain_text("No command is linked to this run.")


def test_late_error_response_never_appears_on_another_selected_run(failure_page) -> None:
    page, scenario, base = failure_page
    scenario.hold_evidence = True
    open_run(page, base)
    expect(page.get_by_text("Reading worker events…", exact=True)).to_be_visible()
    page.wait_for_function(
        "document.querySelector('#run-inspector h2')?.textContent === 'Bay Arts 01'"
    )
    page.locator("#run-inspector").get_by_role(
        "button", name="Inspect next run", exact=True
    ).click()
    expect(page.locator("#run-inspector h2")).to_have_text("Bay Arts 02")
    for route in scenario.pending:
        scenario.respond(route, scenario.payload())
    expect(page.get_by_role("region", name="Failure details", exact=True)).to_have_count(0)
    expect(page.locator("#run-inspector")).not_to_contain_text("validation")
