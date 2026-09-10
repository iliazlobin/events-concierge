"""Bounded, read-only work previews on the Overview landing page."""

import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Route, expect
from tests.e2e import test_admin_work_errors as work_fixtures

work_errors_page = work_fixtures.work_errors_page

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]


def _block(page, name):
    return page.locator(f'[aria-label="{name} overview"]')


def _range(block):
    return (
        block.get_by_role("navigation", name="Record pages", exact=True)
        .locator("..")
        .get_by_role("status")
    )


def _many_entity_records(scenario, monkeypatch):
    original = scenario.respond_errors

    def respond(route: Route):
        params = parse_qs(urlsplit(route.request.url).query)
        if params.get("queue") != ["entity_refresh"] or scenario.error_status != 200:
            original(route)
            return
        records = [
            {
                **work_fixtures._entity_error(),
                "record_id": f"019a7137-8b68-7bf4-b75c-00010000{number:04x}",
                "label": f"Fixture ensemble profile {number:02}",
            }
            for number in range(1, 8)
        ]
        if "entity_refresh" in scenario.empty_queues:
            records = []
        record_id = params.get("record_id", [""])[0]
        if record_id:
            records = [record for record in records if record["record_id"] == record_id]
        offset = int(params.get("offset", ["0"])[0])
        limit = int(params.get("limit", ["10"])[0])
        scenario.respond(
            route,
            {
                "generated_at": work_fixtures._STAMP,
                "queue": "entity_refresh",
                "scope": "errors",
                "total": len(records),
                "offset": offset,
                "limit": limit,
                "items": records[offset : offset + limit],
            },
        )

    monkeypatch.setattr(scenario, "respond_errors", respond)


def test_overview_loads_three_bounded_record_previews_without_expanding(work_errors_page):
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    notifications = _block(page, "Notifications")
    entities = _block(page, "Entity refresh")
    expect(requests.get_by_role("button", name=re.compile(r"^Open Request"))).to_have_count(3)
    expect(
        notifications.get_by_role("button", name=re.compile(r"^Open Notification"))
    ).to_have_count(3)
    expect(entities.get_by_role("button", name=re.compile(r"^Open Fixture"))).to_have_count(1)
    expect(requests.get_by_text("Nothing is ready to claim", exact=False)).to_be_visible()
    expect(requests.get_by_text("2099-01-02 00:00 UTC", exact=True)).to_have_count(3)
    expect(entities.get_by_text("0 profiles are due", exact=False)).to_be_visible()
    expect(page.locator("#operations-errors")).to_have_count(0)
    assert {call["queue"][0] for call in scenario.error_calls} == {
        "request_start",
        "notifications",
        "entity_refresh",
    }
    assert all(call["limit"] == ["3"] for call in scenario.error_calls)
    expect(page.get_by_role("navigation", name="Background work pages")).to_have_count(0)


def test_preview_opens_exact_signed_reference_and_restores_deep_link(work_errors_page):
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin?store_query=music")
    notifications = _block(page, "Notifications")
    record_id = "-9222999999738606380"
    notifications.get_by_role(
        "button", name=re.compile(rf"^Open Notification reference {record_id}")
    ).click()
    expect(page.locator(f'[id="error-{record_id}"]')).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["ops_record"] == [record_id]
    assert params["store_query"] == ["music"]
    page.reload()
    expect(page.locator(f'[id="error-{record_id}"]')).to_be_visible()
    assert any(
        call.get("record_id") == [record_id] and call["limit"] == ["10"]
        for call in scenario.error_calls
    )
    page.get_by_role("button", name="Collapse Notifications records", exact=True).click()
    expect(
        notifications.get_by_role("button", name=re.compile(r"^Open Notification"))
    ).to_have_count(3)
    expect(page.locator("#operations-errors")).to_have_count(0)


def test_failed_history_preview_resets_page_and_keeps_scope_for_exact_record(work_errors_page):
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin")
    notifications = _block(page, "Notifications")
    notifications.get_by_role("button", name="Next records", exact=True).click()
    expect(_range(notifications)).to_contain_text("4–5 of 5")
    notifications.get_by_role("button", name="Failed history", exact=True).click()
    expect(
        notifications.get_by_role("button", name=re.compile(r"^Open Retained notification"))
    ).to_have_count(1)
    expect(notifications.get_by_text("separate from pending work", exact=False)).to_be_visible()
    expect(_range(notifications)).to_contain_text("1–1 of 1")
    expect(
        notifications.get_by_role("button", name="Previous records", exact=True)
    ).to_be_disabled()
    expect(notifications.get_by_role("button", name="Next records", exact=True)).to_be_disabled()
    notifications.get_by_role("button", name=re.compile(r"^Open Retained notification")).click()
    records = page.locator("#operations-errors")
    expect(records.get_by_role("button", name="Failed history", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert parse_qs(urlsplit(page.url).query)["ops_scope"] == ["failed"]
    assert any(
        call.get("scope") == ["failed"] and call["limit"] == ["10"] for call in scenario.error_calls
    )
    page.get_by_role("button", name="Collapse Notifications records", exact=True).click()
    expect(
        notifications.get_by_role("button", name="Failed history", exact=True)
    ).to_have_attribute("aria-pressed", "true")
    expect(_range(notifications)).to_contain_text("1–1 of 1")
    notifications.get_by_role("button", name="Pending", exact=True).click()
    expect(_range(notifications)).to_contain_text("1–3 of 5")


def test_preview_read_failure_hides_old_records_and_retry_recovers(work_errors_page):
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    expect(requests.get_by_role("button", name=re.compile(r"^Open Request"))).to_have_count(3)
    scenario.error_status = 503
    requests.get_by_role("button", name="Refresh preview", exact=True).click()
    expect(requests.get_by_role("alert")).to_contain_text("could not be loaded")
    expect(requests.get_by_role("button", name=re.compile(r"^Open Request"))).to_have_count(0)
    scenario.error_status = 200
    requests.get_by_role("button", name="Retry preview", exact=True).click()
    expect(requests.get_by_role("button", name=re.compile(r"^Open Request"))).to_have_count(3)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
    expect(requests.get_by_role("navigation", name="Record pages", exact=True)).to_be_visible()
    expect(requests.get_by_role("button", name="Next records", exact=True)).to_be_enabled()
    expect(
        page.get_by_role("button", name=re.compile(r"^View all (records|errors)$"))
    ).to_have_count(0)


@pytest.mark.parametrize(
    "name,queue,total,page_two_record",
    [
        ("Request starts", "request_start", 13, "019a7137-8b68-7bf4-b75c-000000000004"),
        ("Notifications", "notifications", 5, "-9223372036854775808"),
        ("Entity refresh", "entity_refresh", 7, "019a7137-8b68-7bf4-b75c-000100000004"),
    ],
)
def test_each_preview_pages_inline_opens_exact_record_and_restores_page(
    work_errors_page, monkeypatch, name, queue, total, page_two_record
):
    page, scenario, base = work_errors_page
    _many_entity_records(scenario, monkeypatch)
    page.goto(f"{base}/admin?store_query=music")
    block = _block(page, name)
    expect(_range(block)).to_contain_text(f"1–3 of {total}")
    previous = block.get_by_role("button", name="Previous records", exact=True)
    next_button = block.get_by_role("button", name="Next records", exact=True)
    expect(previous).to_be_disabled()
    original_url = page.url
    next_button.click()
    expect(_range(block)).to_contain_text(f"4–{min(6, total)} of {total}")
    expect(previous).to_be_enabled()
    expect(block.get_by_role("button", name=re.compile(r"^Open "))).to_have_count(min(3, total - 3))
    expect(page.locator("#operations-errors")).to_have_count(0)
    assert page.url == original_url
    assert all(call["limit"] == ["3"] for call in scenario.error_calls)
    assert any(
        call["queue"] == [queue] and call["offset"] == ["3"] for call in scenario.error_calls
    )

    block.get_by_role("button", name=re.compile(r"^Open ")).first.click()
    expect(page.locator(f'[id="error-{page_two_record}"]')).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["ops_record"] == [page_two_record]
    kind = "errors" if queue == "entity_refresh" else "records"
    page.get_by_role("button", name=f"Collapse {name} {kind}", exact=True).click()
    expect(page.locator("#operations-errors")).to_have_count(0)
    expect(_range(block)).to_contain_text(f"4–{min(6, total)} of {total}")
    assert "ops_record" not in parse_qs(urlsplit(page.url).query)
    assert parse_qs(urlsplit(page.url).query)["store_query"] == ["music"]

    last_offset = 3
    while last_offset + 3 < total:
        next_button.click()
        last_offset += 3
        expect(_range(block)).to_contain_text(
            f"{last_offset + 1}–{min(last_offset + 3, total)} of {total}"
        )
    expect(next_button).to_be_disabled()
    expect(previous).to_be_enabled()
    previous.click()
    expected_offset = last_offset - 3
    expect(_range(block)).to_contain_text(
        f"{expected_offset + 1}–{min(expected_offset + 3, total)} of {total}"
    )
    expect(next_button).to_be_enabled()


def test_panels_keep_independent_pages_and_scope_change_resets_to_first(work_errors_page):
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    notifications = _block(page, "Notifications")
    requests.get_by_role("button", name="Next records", exact=True).click()
    notifications.get_by_role("button", name="Next records", exact=True).click()
    expect(_range(requests)).to_contain_text("4–6 of 13")
    expect(_range(notifications)).to_contain_text("4–5 of 5")
    requests.get_by_role("button", name="With errors", exact=True).click()
    expect(_range(requests)).to_contain_text("1–3 of 10")
    expect(requests.get_by_role("button", name="Previous records", exact=True)).to_be_disabled()
    expect(_range(notifications)).to_contain_text("4–5 of 5")
    assert any(
        call["queue"] == ["request_start"]
        and call["scope"] == ["errors"]
        and call["offset"] == ["0"]
        and call["limit"] == ["3"]
        for call in scenario.error_calls
    )
    expect(page.locator("#operations-errors")).to_have_count(0)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
    previous = notifications.get_by_role("button", name="Previous records", exact=True)
    previous.focus()
    previous.press("Enter")
    expect(_range(notifications)).to_contain_text("1–3 of 5")
    expect(_range(requests)).to_contain_text("1–3 of 10")


def test_preview_refresh_clamps_page_after_records_disappear(work_errors_page):
    page, scenario, base = work_errors_page
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    requests.get_by_role("button", name="Next records", exact=True).click()
    expect(_range(requests)).to_contain_text("4–6 of 13")
    scenario.empty_queues.add("request_start")
    requests.get_by_role("button", name="Refresh preview", exact=True).click()
    expect(_range(requests)).to_contain_text("0 of 0")
    expect(requests.get_by_role("button", name="Previous records", exact=True)).to_be_disabled()
    expect(requests.get_by_role("button", name="Next records", exact=True)).to_be_disabled()
    scenario.empty_queues.remove("request_start")
    requests.get_by_role("button", name="Refresh preview", exact=True).click()
    expect(_range(requests)).to_contain_text("1–3 of 13")
