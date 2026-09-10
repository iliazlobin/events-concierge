"""Read-only browser coverage for inline Overview work and error investigations."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, Route, expect
from tests.e2e.test_admin_operations import OperationsApi

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]

_STAMP = "2026-09-08T16:00:00Z"
_RETRY = "2099-01-02T00:00:00Z"
_SIGNED_NOTIFICATION_IDS = ("-9222999999738606380", "-9223372036854775808", "0")


def _request_error(number: int) -> dict[str, Any]:
    return {
        "record_id": f"019a7137-8b68-7bf4-b75c-00000000{number:04x}",
        "label": f"Request retry {number:02}",
        "state": "scheduled",
        "error_code": "test_retry_fixture",
        "error_summary": "fresh request-start retry after reclaim",
        "attempt_count": number + 3,
        "attempt_kind": "failed_start_attempts",
        "failed_at": None,
        "created_at": "2026-07-18T10:11:12Z",
        "next_attempt_at": _RETRY,
        "lease_expires_at": None,
        "last_observed_at": None,
        "sources": [],
    }


def _pending_request(number: int) -> dict[str, Any]:
    record = _request_error(number)
    if number > 10:
        record.update(error_code=None, error_summary=None, attempt_count=0)
    return record


def _notification(number: int, *, failed: bool = False) -> dict[str, Any]:
    return {
        "record_id": "9223372036854775806" if failed else str(9007199254740992 + number),
        "label": "Retained notification" if failed else f"Notification {number:02}",
        "state": "failed" if failed else "ready",
        "error_code": "unclassified" if failed else None,
        "error_summary": "An error was recorded. Raw error text is unavailable in this view."
        if failed
        else None,
        "attempt_count": 0,
        "attempt_kind": "failed_delivery_attempts",
        "created_at": "2026-07-18T10:11:12Z",
        "next_attempt_at": "2026-09-01T10:00:00Z",
        "failed_at": "2026-09-07T14:15:16Z" if failed else None,
        "lease_expires_at": None,
        "last_observed_at": None,
        "sources": [],
    }


def _signed_notification(record_id: str) -> dict[str, Any]:
    return {
        **_notification(3),
        "record_id": record_id,
        "label": f"Notification reference {record_id}",
    }


def _entity_error() -> dict[str, Any]:
    return {
        "record_id": "019a7137-8b68-7bf4-b75c-000100000001",
        "label": "Fixture ensemble profile",
        "state": "scheduled",
        "error_code": "source_errors",
        "error_summary": "Entity profile has recorded source errors.",
        "attempt_count": None,
        "created_at": "2026-06-01T10:00:00Z",
        "next_attempt_at": "2026-09-10T12:00:00Z",
        "lease_expires_at": None,
        "last_observed_at": "2026-09-07T11:22:33Z",
        "sources": [
            {
                "source_id": "019a7137-8b68-7bf4-b75c-000200000001",
                "provider_key": "official_site",
                "status": "failed",
                "error_code": "unavailable",
                "error_summary": "Provider unavailable.",
                "observed_at": "2026-09-07T11:22:33Z",
                "next_refresh_at": "2026-09-10T12:00:00Z",
            },
            {
                "source_id": "019a7137-8b68-7bf4-b75c-000200000002",
                "provider_key": "community_profile",
                "status": "blocked",
                "error_code": "unsupported_profile",
                "error_summary": "Profile is not supported.",
                "observed_at": "2026-09-06T13:14:15Z",
                "next_refresh_at": None,
            },
        ],
    }


@dataclass
class WorkErrorsApi(OperationsApi):
    error_status: int = 200
    empty_queues: set[str] = field(default_factory=set)
    held_queues: set[str] = field(default_factory=set)
    pending_error_routes: list[Route] = field(default_factory=list)
    error_calls: list[dict[str, list[str]]] = field(default_factory=list)

    def handle(self, route: Route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        if path not in {"/admin/v1/operations/errors", "/admin/v1/operations/records"}:
            super().handle(route)
            return
        self.calls.append((request.method, path))
        if request.method != "GET":
            self.unexpected.append(f"{request.method} {path}")
            self.respond(route, {"detail": "Fixture forbids mutations"}, 405)
            return
        params = parse_qs(urlsplit(request.url).query)
        self.error_calls.append(params)
        if params.get("queue", [""])[0] in self.held_queues:
            self.pending_error_routes.append(route)
            return
        self.respond_errors(route)

    def respond_errors(self, route: Route) -> None:
        params = parse_qs(urlsplit(route.request.url).query)
        queue = params.get("queue", [""])[0]
        scope = params.get("scope", ["pending"])[0]
        endpoint = urlsplit(route.request.url).path
        valid = (queue == "entity_refresh" and endpoint.endswith("/errors")) or (
            endpoint.endswith("/records")
            and (
                (queue == "request_start" and scope in {"pending", "errors"})
                or (queue == "notifications" and scope in {"pending", "failed"})
            )
        )
        if not valid:
            self.unexpected.append(f"Unexpected error queue {queue}")
            self.respond(route, {"detail": "Unknown error queue"}, 400)
            return
        if self.error_status != 200:
            self.respond(
                route,
                {"detail": f"Fixture error records unavailable ({self.error_status})"},
                self.error_status,
            )
            return
        offset = int(params.get("offset", ["0"])[0])
        limit = int(params.get("limit", ["10"])[0])
        if queue == "request_start":
            records = [_pending_request(number) for number in range(1, 14)]
            if scope == "errors":
                records = [record for record in records if record["error_code"]]
        elif queue == "notifications":
            records = (
                [_notification(1, failed=True)]
                if scope == "failed"
                else [
                    _notification(1),
                    _notification(2),
                    *(_signed_notification(record_id) for record_id in _SIGNED_NOTIFICATION_IDS),
                ]
            )
        else:
            records = [_entity_error()]
        if queue in self.empty_queues:
            records = []
        record_id = params.get("record_id", [""])[0]
        if record_id:
            records = [record for record in records if record["record_id"] == record_id]
        self.respond(
            route,
            {
                "generated_at": _STAMP,
                "queue": queue,
                **({"scope": scope} if queue != "entity_refresh" else {}),
                "total": len(records),
                "offset": offset,
                "limit": limit,
                "items": records[offset : offset + limit],
            },
        )


@pytest.fixture
def work_errors_page(browser: Browser) -> Iterator[tuple[Page, WorkErrorsApi, str]]:
    base_url = os.environ["EC_ADMIN_WEB_URL"].rstrip("/")
    context = browser.new_context(viewport={"width": 1600, "height": 1000}, reduced_motion="reduce")
    context.add_init_script("""Object.defineProperty(navigator, 'clipboard', {value: {
        writeText: async text => { window.__copiedWorkText = text; }
    }});""")
    page = context.new_page()
    scenario = WorkErrorsApi()
    scenario.queue_overrides["request_start"] = {
        "pending": 13,
        "ready": 0,
        "leased": 0,
        "failed": 10,
    }
    scenario.queue_overrides["notifications"] = {"pending": 5, "ready": 5, "leased": 0, "failed": 1}
    scenario.queue_overrides["entity_refresh"] = {
        "pending": 0,
        "ready": 0,
        "leased": 0,
        "failed": 1,
    }
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/v1/**", scenario.handle)
    try:
        yield page, scenario, base_url
        assert not errors, errors
        assert not scenario.unexpected, scenario.unexpected
        assert scenario.error_calls
        assert all(
            method == "GET" and path.startswith("/admin/v1/") for method, path in scenario.calls
        )
    finally:
        context.close()


def _error_row(page: Page, label: str):
    return page.locator("#operations-errors").get_by_role(
        "button", name=re.compile(rf"^Inspect (?:record|error) {re.escape(label)}(?: |$)")
    )


def _last_inspector_call(scenario: WorkErrorsApi, queue: str) -> dict[str, list[str]]:
    # Other queue previews read concurrently with the full, ten-record inspector.
    return next(
        call
        for call in reversed(scenario.error_calls)
        if call.get("queue") == [queue] and call.get("limit") == ["10"]
    )


def test_request_reason_retry_time_reference_and_copy_link(work_errors_page) -> None:
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin?ops_queue=request_start")
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    expect(page.get_by_role("region", name="Background work", exact=True)).to_be_visible()
    errors = page.locator("#operations-errors")
    expect(_error_row(page, "Request retry 01")).to_be_visible()
    expect(errors.get_by_role("button", name="Previous records")).to_be_disabled()
    _error_row(page, "Request retry 01").click()
    record = _request_error(1)
    detail = page.locator(f"#error-{record['record_id']}")
    expect(
        detail.get_by_text("fresh request-start retry after reclaim", exact=True)
    ).to_be_visible()
    expect(detail.locator("dt", has_text="Recorded failed attempts").locator("+ dd")).to_have_text(
        "4"
    )
    expect(detail.locator("dt", has_text="Next retry").locator("+ dd")).to_have_text(
        "2099-01-02 00:00 UTC"
    )
    expect(detail.locator("time").filter(has_text="2099-01-02")).to_have_attribute(
        "datetime", _RETRY
    )
    expect(detail.locator("dt", has_text="Created").locator("+ dd")).to_have_text(
        "2026-07-18 10:11 UTC"
    )
    expect(detail.get_by_text("test_retry_fixture", exact=True)).to_be_visible()
    expect(
        detail.get_by_text("The recorded retry time is in the future", exact=False)
    ).to_be_visible()
    detail.get_by_role("button", name="Copy reference", exact=True).click()
    expect(detail.get_by_role("button", name="Reference copied", exact=True)).to_be_visible()
    assert page.evaluate("window.__copiedWorkText") == record["record_id"]

    page.get_by_role("button", name="Copy inspection link", exact=True).click()
    expect(page.get_by_role("button", name="Copied", exact=True)).to_be_visible()
    copied = page.evaluate("window.__copiedWorkText")
    assert parse_qs(urlsplit(copied).query)["ops_record"] == [record["record_id"]]
    assert parse_qs(urlsplit(copied).query)["ops_queue"] == ["request_start"]
    page.goto(copied)
    expect(detail).to_be_visible()
    expect(errors.get_by_role("button", name="All records", exact=True)).to_be_visible()
    assert _last_inspector_call(scenario, "request_start")["record_id"] == [record["record_id"]]
    inline = page.get_by_role("region", name="Request starts records", exact=True)
    expect(inline).to_be_visible()
    toggle = page.get_by_role("button", name="Inspect request starts", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "true")
    page.get_by_role("button", name="Collapse Request starts records", exact=True).click()
    expect(inline).to_have_count(0)
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    assert "ops_queue" not in parse_qs(urlsplit(page.url).query)
    page.go_back()
    expect(detail).to_be_visible()
    expect(inline).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    assert parse_qs(urlsplit(page.url).query)["ops_record"] == [record["record_id"]]
    page.go_forward()
    expect(inline).to_have_count(0)
    expect(toggle).to_have_attribute("aria-expanded", "false")


def test_error_pagination_history_and_later_record_reload(work_errors_page) -> None:
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin?ops_queue=request_start")
    errors = page.locator("#operations-errors")
    expect(_error_row(page, "Request retry 01")).to_be_visible()
    errors.get_by_role("button", name="Next records", exact=True).click()
    expect(_error_row(page, "Request retry 11")).to_be_visible()
    expect(_error_row(page, "Request retry 01")).to_have_count(0)
    expect(errors.get_by_text("11\u201313 of 13", exact=True)).to_be_visible()
    expect(errors.get_by_role("button", name="Next records", exact=True)).to_be_disabled()
    assert parse_qs(urlsplit(page.url).query)["ops_offset"] == ["10"]
    _error_row(page, "Request retry 12").click()
    record_id = _request_error(12)["record_id"]
    detail = page.locator(f"#error-{record_id}")
    expect(detail).to_be_visible()

    page.go_back()
    expect(_error_row(page, "Request retry 12")).to_have_attribute("aria-expanded", "false")
    assert "ops_record" not in parse_qs(urlsplit(page.url).query)
    page.go_forward()
    expect(detail).to_be_visible()
    page.reload()
    expect(detail).to_be_visible()
    assert _last_inspector_call(scenario, "request_start")["record_id"] == [record_id]
    assert _last_inspector_call(scenario, "request_start")["offset"] == ["0"]
    assert parse_qs(urlsplit(page.url).query)["ops_offset"] == ["10"]
    errors.get_by_role("button", name="All records", exact=True).click()
    expect(_error_row(page, "Request retry 11")).to_be_visible()
    expect(errors.get_by_text("11\u201313 of 13", exact=True)).to_be_visible()
    assert "ops_record" not in parse_qs(urlsplit(page.url).query)
    errors.get_by_role("button", name="Previous records", exact=True).click()
    expect(_error_row(page, "Request retry 01")).to_be_visible()
    assert "ops_offset" not in parse_qs(urlsplit(page.url).query)


def test_queue_switch_hides_request_evidence_and_reads_entity_sources(work_errors_page) -> None:
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin?ops_queue=request_start")
    _error_row(page, "Request retry 01").click()
    request_detail = page.locator(f"#error-{_request_error(1)['record_id']}")
    expect(request_detail).to_be_visible()
    scenario.held_queues.add("entity_refresh")
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    with page.expect_request("**/admin/v1/operations/errors?queue=entity_refresh&*"):
        page.get_by_role("button", name="Inspect Entity refresh", exact=True).click()
    expect(page.locator("#operations-errors").get_by_role("status")).to_have_text(
        "Loading error records…"
    )
    expect(_error_row(page, "Request retry 01")).to_have_count(0)
    expect(request_detail).to_have_count(0)
    assert "ops_record" not in parse_qs(urlsplit(page.url).query)
    assert scenario.pending_error_routes
    scenario.held_queues.clear()
    for route in scenario.pending_error_routes:
        scenario.respond_errors(route)
    scenario.pending_error_routes.clear()
    _error_row(page, "Fixture ensemble profile").click()
    detail = page.locator(f"#error-{_entity_error()['record_id']}")
    expect(detail.get_by_text("source_errors", exact=True)).to_be_visible()
    expect(detail.locator("dt", has_text="Recorded failed attempts")).to_have_count(0)
    expect(detail.locator("dt", has_text="Lease expires")).to_have_count(0)
    expect(detail.locator("dt", has_text="Last observed").locator("+ dd")).to_have_text(
        "2026-09-07 11:22 UTC"
    )
    official = detail.get_by_role("heading", name="official_site", exact=False).locator("..")
    expect(official.get_by_text("failed", exact=True)).to_be_visible()
    expect(official.get_by_text("unavailable", exact=True)).to_be_visible()
    expect(official.get_by_text("Provider unavailable.", exact=True)).to_be_visible()
    expect(official.locator("dt", has_text="Observed").locator("+ dd")).to_have_text(
        "2026-09-07 11:22 UTC"
    )
    expect(official.locator("dt", has_text="Next refresh").locator("+ dd")).to_have_text(
        "2026-09-10 12:00 UTC"
    )
    community = detail.get_by_role("heading", name="community_profile", exact=False).locator("..")
    expect(community.get_by_text("blocked", exact=True)).to_be_visible()
    expect(community.get_by_text("unsupported_profile", exact=True)).to_be_visible()
    expect(community.locator("dt", has_text="Next refresh").locator("+ dd")).to_have_text(
        "Not recorded"
    )


def test_error_read_failure_hides_old_records_and_retry_recovers(work_errors_page) -> None:
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin?ops_queue=request_start")
    errors = page.locator("#operations-errors")
    _error_row(page, "Request retry 01").click()
    expect(errors.get_by_role("button", name="Copy reference", exact=True)).to_be_visible()
    scenario.error_status = 503
    errors.get_by_role("button", name="Refresh records", exact=True).click()
    expect(errors.get_by_role("alert")).to_contain_text(
        "Records could not be loaded. Previous results are hidden."
    )
    expect(_error_row(page, "Request retry 01")).to_have_count(0)
    expect(errors.get_by_role("button", name="Copy reference", exact=True)).to_have_count(0)
    expect(errors.get_by_text("No records in this view.", exact=True)).to_have_count(0)
    scenario.error_status = 200
    errors.get_by_role("button", name="Retry records", exact=True).click()
    expect(_error_row(page, "Request retry 01")).to_be_visible()
    expect(errors.get_by_role("button", name="Copy reference", exact=True)).to_be_visible()
    expect(errors.get_by_role("alert")).to_have_count(0)


def test_late_previous_queue_response_cannot_replace_current_evidence(work_errors_page) -> None:
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin?ops_queue=request_start")
    errors = page.locator("#operations-errors")
    _error_row(page, "Request retry 01").click()
    scenario.held_queues.add("request_start")
    with page.expect_request("**/admin/v1/operations/records?queue=request_start&*"):
        errors.get_by_role("button", name="Refresh records", exact=True).click()
    expect(errors.get_by_role("status")).to_have_text("Loading records…")
    assert scenario.pending_error_routes
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    page.get_by_role("button", name="Inspect Entity refresh", exact=True).click()
    _error_row(page, "Fixture ensemble profile").click()
    entity_detail = page.locator(f"#error-{_entity_error()['record_id']}")
    expect(entity_detail).to_be_visible()
    scenario.held_queues.clear()
    for route in scenario.pending_error_routes:
        scenario.respond_errors(route)
    scenario.pending_error_routes.clear()
    # Give the obsolete response a render turn after the current queue has settled.
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(entity_detail).to_be_visible()
    expect(_error_row(page, "Request retry 01")).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query)["ops_queue"] == ["entity_refresh"]
    assert parse_qs(urlsplit(page.url).query)["ops_record"] == [_entity_error()["record_id"]]


@pytest.mark.parametrize("status", [401, 403])
def test_error_authorization_failure_clears_previous_details(work_errors_page, status: int) -> None:
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin?ops_queue=request_start")
    errors = page.locator("#operations-errors")
    _error_row(page, "Request retry 01").click()
    expect(errors.get_by_role("button", name="Copy reference", exact=True)).to_be_visible()
    scenario.error_status = status
    errors.get_by_role("button", name="Refresh records", exact=True).click()
    expect(errors.get_by_role("alert")).to_contain_text(
        "Records are unavailable for this operator session."
    )
    expect(errors.get_by_role("button", name=re.compile("^Inspect record"))).to_have_count(0)
    expect(errors.get_by_text(_request_error(1)["record_id"], exact=True)).to_have_count(0)
    expect(errors.get_by_role("button", name="Copy reference", exact=True)).to_have_count(0)


def test_successful_empty_error_snapshot_is_distinct_from_failure(work_errors_page) -> None:
    page, scenario, base = work_errors_page
    scenario.empty_queues.add("request_start")
    page.goto(f"{base}/admin?ops_queue=request_start")
    errors = page.locator("#operations-errors")
    expect(errors.get_by_text("No records in this view.", exact=True)).to_be_visible()
    expect(errors.get_by_role("alert")).to_have_count(0)
    expect(errors.get_by_role("button", name=re.compile("^Inspect record"))).to_have_count(0)
    expect(errors.get_by_role("button", name="Next records", exact=True)).to_have_count(0)


def test_older_backend_error_endpoint_has_specific_compatibility_message(work_errors_page) -> None:
    page, scenario, base = work_errors_page
    scenario.error_status = 404
    page.goto(f"{base}/admin?ops_queue=request_start")
    errors = page.locator("#operations-errors")
    expect(errors.get_by_role("alert")).to_contain_text(
        "This backend does not expose individual records yet."
    )
    expect(errors.get_by_text("No records in this view.", exact=True)).to_have_count(0)
    scenario.error_status = 503
    errors.get_by_role("button", name="Retry records", exact=True).click()
    expect(errors.get_by_role("alert")).to_contain_text("Records could not be loaded.")
    expect(errors.get_by_role("alert")).not_to_contain_text("does not expose")
    scenario.error_status = 200
    errors.get_by_role("button", name="Retry records", exact=True).click()
    expect(_error_row(page, "Request retry 01")).to_be_visible()
    expect(errors.get_by_role("alert")).to_have_count(0)


def test_pending_and_error_filters_clear_selection_and_restore_history(work_errors_page) -> None:
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin?ops_queue=request_start")
    records = page.get_by_role("region", name="Records", exact=True)
    expect(records.get_by_role("button", name="Pending", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    records.get_by_role("button", name="Next records", exact=True).click()
    _error_row(page, "Request retry 12").click()
    detail = page.locator(f"#error-{_request_error(12)['record_id']}")
    expect(detail).to_be_visible()
    expect(detail.locator("dt", has_text="Reason")).to_have_count(0)
    expect(detail.locator("dt", has_text="Available after")).to_be_visible()
    original = page.url
    records.get_by_role("button", name="With errors", exact=True).click()
    expect(_error_row(page, "Request retry 01")).to_be_visible()
    expect(_error_row(page, "Request retry 12")).to_have_count(0)
    expect(records.get_by_text("1\u201310 of 10", exact=True)).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["ops_scope"] == ["errors"]
    assert "ops_record" not in params and "ops_offset" not in params
    assert _last_inspector_call(scenario, "request_start")["scope"] == ["errors"]
    page.reload()
    expect(records.get_by_role("button", name="With errors", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(records.get_by_text("1\u201310 of 10", exact=True)).to_be_visible()
    page.go_back()
    expect(detail).to_be_visible()
    assert page.url == original
    assert _last_inspector_call(scenario, "request_start")["scope"] == ["pending"]
    page.go_forward()
    expect(records.get_by_role("button", name="With errors", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(detail).to_have_count(0)


def test_notifications_pending_and_failed_history_keep_exact_decimal_references(
    work_errors_page,
) -> None:
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin")
    expect(page.get_by_role("region", name="Background work", exact=True)).to_be_visible()
    page.get_by_role("button", name="Inspect notifications", exact=True).click()
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    records = page.get_by_role("region", name="Records", exact=True)
    _error_row(page, "Notification 01").click()
    record_id = _notification(1)["record_id"]
    detail = page.locator(f"#error-{record_id}")
    expect(
        detail.locator("dt", has_text="Recorded delivery failures").locator("+ dd")
    ).to_have_text("0")
    expect(detail.locator("dt", has_text="Reason")).to_have_count(0)
    detail.get_by_role("button", name="Copy reference", exact=True).click()
    expect(detail.get_by_role("button", name="Reference copied", exact=True)).to_be_visible()
    assert page.evaluate("window.__copiedWorkText") == "9007199254740993"
    assert parse_qs(urlsplit(page.url).query)["ops_record"] == [record_id]
    page.reload()
    expect(detail).to_be_visible()
    assert _last_inspector_call(scenario, "notifications")["record_id"] == [record_id]
    pending_url = page.url
    records.get_by_role("button", name="Failed history", exact=True).click()
    expect(_error_row(page, "Notification 01")).to_have_count(0)
    expect(records).to_contain_text("These records are no longer pending.")
    _error_row(page, "Retained notification").click()
    failed_id = _notification(1, failed=True)["record_id"]
    failed = page.locator(f"#error-{failed_id}")
    expect(failed.locator("dt", has_text="Recorded state").locator("+ dd")).to_have_text("Failed")
    expect(
        failed.locator("dt").filter(has_text=re.compile("^Failed$")).locator("+ dd")
    ).to_have_text("2026-09-07 14:15 UTC")
    expect(failed.locator("dt", has_text="Next retry")).to_have_count(0)
    failed.get_by_text("Attempt measurement", exact=True).click()
    expect(failed).to_contain_text("Contention and quarantine may not consume an attempt.")
    page.get_by_role("button", name="Copy inspection link", exact=True).click()
    expect(page.get_by_role("button", name="Copied", exact=True)).to_be_visible()
    copied = page.evaluate("window.__copiedWorkText")
    params = parse_qs(urlsplit(copied).query)
    assert params["ops_scope"] == ["failed"]
    assert params["ops_record"] == [failed_id]
    page.reload()
    expect(failed).to_be_visible()
    assert _last_inspector_call(scenario, "notifications")["record_id"] == [failed_id]
    page.go_back()
    expect(failed).to_have_count(0)
    page.go_back()
    expect(detail).to_be_visible()
    assert page.url == pending_url
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    page.get_by_role("button", name="Inspect request starts", exact=True).click()
    expect(_error_row(page, "Request retry 01")).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert "ops_scope" not in params and "ops_record" not in params
    assert _last_inspector_call(scenario, "request_start")["scope"] == ["pending"]


@pytest.mark.parametrize("record_id", _SIGNED_NOTIFICATION_IDS)
def test_signed_notification_references_preserve_copy_reload_and_history(
    work_errors_page,
    record_id: str,
) -> None:
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin?ops_queue=notifications&store_query=music")
    row = _error_row(page, f"Notification reference {record_id}")
    row.click()
    detail = page.locator(f"#error-{record_id}")
    expect(detail).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["ops_record"] == [record_id]
    detail.get_by_role("button", name="Copy reference", exact=True).click()
    expect(detail.get_by_role("button", name="Reference copied", exact=True)).to_be_visible()
    assert page.evaluate("window.__copiedWorkText") == record_id
    page.get_by_role("button", name="Copy inspection link", exact=True).click()
    expect(page.get_by_role("button", name="Copied", exact=True)).to_be_visible()
    copied = page.evaluate("window.__copiedWorkText")
    assert parse_qs(urlsplit(copied).query)["ops_record"] == [record_id]
    assert parse_qs(urlsplit(copied).query)["store_query"] == ["music"]
    page.go_back()
    expect(row).to_have_attribute("aria-expanded", "false")
    assert "ops_record" not in parse_qs(urlsplit(page.url).query)
    page.go_forward()
    expect(detail).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["ops_record"] == [record_id]
    page.reload()
    expect(detail).to_be_visible()
    assert _last_inspector_call(scenario, "notifications")["record_id"] == [record_id]
    page.goto(copied)
    expect(detail).to_be_visible()
    assert _last_inspector_call(scenario, "notifications")["record_id"] == [record_id]
    expect(page.get_by_role("button", name="All records", exact=True)).to_be_visible()
