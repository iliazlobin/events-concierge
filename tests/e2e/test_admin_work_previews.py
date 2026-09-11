"""Scrollable, read-only work previews retain bounded reads and exact investigation links."""

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


def _viewport(block):
    return block.get_by_role("region", name="Work records", exact=True)


def _rows(block):
    return _viewport(block).get_by_role("button", name=re.compile(r"^Open "))


def _scroll_bottom(block):
    _viewport(block).evaluate("element => element.scrollTo(0, element.scrollHeight)")


def _many_records(scenario, monkeypatch):
    """Extend every queue locally, including delayed append responses for cancellation checks."""
    original = scenario.respond_errors
    control = {"hold": set(), "held": []}

    def respond(route: Route, *, release=False):
        params = parse_qs(urlsplit(route.request.url).query)
        queue = params.get("queue", [""])[0]
        scope = params.get("scope", ["errors" if queue == "entity_refresh" else "pending"])[0]
        if scenario.error_status != 200:
            original(route)
            return
        if queue == "request_start":
            records = [work_fixtures._pending_request(number) for number in range(1, 48)]
            if scope == "errors":
                records = [record for record in records if record["error_code"]]
        elif queue == "notifications":
            records = [work_fixtures._notification(number) for number in range(1, 48)]
            if scope == "failed":
                records = [
                    {
                        **work_fixtures._notification(number, failed=True),
                        "record_id": str(9223372036854775806 - number),
                        "label": f"Retained notification {number:02}",
                    }
                    for number in range(1, 4)
                ]
        elif queue == "entity_refresh":
            records = [
                {
                    **work_fixtures._entity_error(),
                    "record_id": f"019a7137-8b68-7bf4-b75c-00010000{number:04x}",
                    "label": f"Fixture ensemble profile {number:02}",
                }
                for number in range(1, 48)
            ]
        else:
            original(route)
            return
        if queue in scenario.empty_queues:
            records = []
        record_id = params.get("record_id", [""])[0]
        if record_id:
            records = [record for record in records if record["record_id"] == record_id]
        offset = int(params.get("offset", ["0"])[0])
        limit = int(params.get("limit", ["10"])[0])
        if not release and (queue, scope, offset) in control["hold"]:
            control["held"].append(route)
            return
        scenario.respond(
            route,
            {
                "generated_at": work_fixtures._STAMP,
                "queue": queue,
                **({"scope": scope} if queue != "entity_refresh" else {}),
                "total": len(records),
                "offset": offset,
                "limit": limit,
                "items": records[offset : offset + limit],
            },
        )

    monkeypatch.setattr(scenario, "respond_errors", respond)
    control["release"] = lambda route: respond(route, release=True)
    return control


def _visible_geometry(block):
    return _viewport(block).evaluate(
        """element => {
            const view = element.getBoundingClientRect();
            const bounds = [...element.querySelectorAll('li')].map(row => row.getBoundingClientRect());
            return {
                fullyVisible: bounds.filter(row => row.top >= view.top - 1 && row.bottom <= view.bottom + 1).length,
                intersecting: bounds.filter(row => Math.min(row.bottom, view.bottom) - Math.max(row.top, view.top) > 1).length,
                overflows: element.scrollHeight > element.clientHeight,
            };
        }"""
    )


def test_overview_loads_twenty_records_but_only_three_visible_per_panel(
    work_errors_page, monkeypatch
):
    page, scenario, base = work_errors_page
    _many_records(scenario, monkeypatch)
    page.goto(f"{base}/admin")
    for name in ("Request starts", "Notifications", "Entity refresh"):
        block = _block(page, name)
        expect(_rows(block)).to_have_count(20)
        expect(block.get_by_text("20 of 47 loaded", exact=False)).to_be_visible()
        geometry = _visible_geometry(block)
        assert geometry["fullyVisible"] == 3, (name, geometry)
        assert geometry["intersecting"] == 3, (name, geometry)
        assert geometry["overflows"]
    expect(page.locator("#operations-errors")).to_have_count(0)
    assert {call["queue"][0] for call in scenario.error_calls} == {
        "request_start",
        "notifications",
        "entity_refresh",
    }
    assert all(call["limit"] == ["20"] and call["offset"] == ["0"] for call in scenario.error_calls)
    expect(page.get_by_role("navigation", name="Record pages")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile(r"^(Previous|Next) records$"))).to_have_count(
        0
    )
    expect(
        page.get_by_role("button", name=re.compile(r"^View all (records|errors)$"))
    ).to_have_count(0)


@pytest.mark.parametrize(
    "name,queue,record_id",
    [
        ("Request starts", "request_start", "019a7137-8b68-7bf4-b75c-000000000015"),
        ("Notifications", "notifications", "9007199254741013"),
        ("Entity refresh", "entity_refresh", "019a7137-8b68-7bf4-b75c-000100000015"),
    ],
)
def test_scrolling_appends_each_queue_and_restores_position_after_record(
    work_errors_page, monkeypatch, name, queue, record_id
):
    page, scenario, base = work_errors_page
    _many_records(scenario, monkeypatch)
    page.goto(f"{base}/admin?store_query=music")
    block = _block(page, name)
    expect(_rows(block)).to_have_count(20)
    original_url = page.url
    _scroll_bottom(block)
    expect(_rows(block)).to_have_count(40)
    expect(block.get_by_text("40 of 47 loaded", exact=False)).to_be_visible()
    expect(page.locator("#operations-errors")).to_have_count(0)
    assert page.url == original_url
    assert any(
        call["queue"] == [queue] and call["offset"] == ["20"] for call in scenario.error_calls
    )
    assert all(call["limit"] == ["20"] for call in scenario.error_calls)
    _rows(block).nth(20).scroll_into_view_if_needed()
    scroll_top = _viewport(block).evaluate("element => element.scrollTop")
    assert scroll_top > 0
    _rows(block).nth(20).click()
    expect(page.locator(f'[id="error-{record_id}"]')).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["ops_record"] == [record_id]
    kind = "errors" if queue == "entity_refresh" else "records"
    page.get_by_role("button", name=f"Collapse {name} {kind}", exact=True).click()
    expect(_rows(block)).to_have_count(40)
    expect(page.locator("#operations-errors")).to_have_count(0)
    page.wait_for_function(
        """({label, position}) => {
            const panel = document.querySelector(`[aria-label="${label} overview"]`);
            const view = panel?.querySelector('[role="region"][aria-label="Work records"]');
            return view && Math.abs(view.scrollTop - position) < 2;
        }""",
        arg={"label": name, "position": scroll_top},
    )
    assert parse_qs(urlsplit(page.url).query)["store_query"] == ["music"]
    _scroll_bottom(block)
    expect(_rows(block)).to_have_count(47)
    expect(block.get_by_text("47 of 47 loaded", exact=False)).to_be_visible()
    expect(block.get_by_role("button", name="Load more records", exact=True)).to_have_count(0)
    assert any(
        call["queue"] == [queue] and call["offset"] == ["40"] for call in scenario.error_calls
    )


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
    expect(_rows(notifications)).to_have_count(5)
    expect(page.locator("#operations-errors")).to_have_count(0)


def test_scope_changes_reset_scroll_keep_panels_independent_and_preserve_exact_scope(
    work_errors_page, monkeypatch
):
    page, scenario, base = work_errors_page
    _many_records(scenario, monkeypatch)
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    notifications = _block(page, "Notifications")
    expect(_rows(requests)).to_have_count(20)
    expect(_rows(notifications)).to_have_count(20)
    _scroll_bottom(requests)
    _scroll_bottom(notifications)
    expect(_rows(requests)).to_have_count(40)
    expect(_rows(notifications)).to_have_count(40)
    notification_scroll = _viewport(notifications).evaluate("element => element.scrollTop")
    requests.get_by_role("button", name="With errors", exact=True).click()
    expect(_rows(requests)).to_have_count(10)
    assert _viewport(requests).evaluate("element => element.scrollTop") == 0
    expect(_rows(notifications)).to_have_count(40)
    assert _viewport(notifications).evaluate("element => element.scrollTop") == notification_scroll
    notifications.get_by_role("button", name="Failed history", exact=True).click()
    expect(_rows(notifications)).to_have_count(3)
    assert _viewport(notifications).evaluate("element => element.scrollTop") == 0
    _rows(notifications).first.click()
    expect(page.locator('[id="error-9223372036854775805"]')).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["ops_scope"] == ["failed"]
    page.get_by_role("button", name="Collapse Notifications records", exact=True).click()
    expect(
        notifications.get_by_role("button", name="Failed history", exact=True)
    ).to_have_attribute("aria-pressed", "true")
    expect(_rows(notifications)).to_have_count(3)
    notifications.get_by_role("button", name="Pending", exact=True).click()
    expect(_rows(notifications)).to_have_count(20)
    assert _viewport(notifications).evaluate("element => element.scrollTop") == 0
    assert any(
        call["queue"] == ["request_start"]
        and call["scope"] == ["errors"]
        and call["offset"] == ["0"]
        and call["limit"] == ["20"]
        for call in scenario.error_calls
    )


def test_refresh_failure_hides_stale_records_and_retry_and_empty_recover(
    work_errors_page, monkeypatch
):
    page, scenario, base = work_errors_page
    _many_records(scenario, monkeypatch)
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    expect(_rows(requests)).to_have_count(20)
    scenario.error_status = 503
    requests.get_by_role("button", name="Refresh preview", exact=True).click()
    expect(requests.get_by_role("alert")).to_contain_text("could not be loaded")
    expect(requests.get_by_role("button", name=re.compile(r"^Open Request"))).to_have_count(0)
    scenario.error_status = 200
    requests.get_by_role("button", name="Retry preview", exact=True).click()
    expect(_rows(requests)).to_have_count(20)
    scenario.empty_queues.add("request_start")
    requests.get_by_role("button", name="Refresh preview", exact=True).click()
    expect(requests.get_by_text("No pending records in this snapshot.", exact=True)).to_be_visible()
    expect(requests.get_by_text("0 of 0 loaded", exact=False)).to_be_visible()
    expect(requests.get_by_role("button", name="Load more records", exact=True)).to_have_count(0)
    scenario.empty_queues.clear()
    requests.get_by_role("button", name="Refresh preview", exact=True).click()
    expect(_rows(requests)).to_have_count(20)


def test_append_failure_retains_loaded_records_and_retries_same_offset(
    work_errors_page, monkeypatch
):
    page, scenario, base = work_errors_page
    _many_records(scenario, monkeypatch)
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    expect(_rows(requests)).to_have_count(20)
    scenario.error_status = 503
    _scroll_bottom(requests)
    expect(requests.get_by_role("alert")).to_be_visible()
    expect(_rows(requests)).to_have_count(20)
    scenario.error_status = 200
    requests.get_by_role("button", name="Retry loading more", exact=True).click()
    expect(_rows(requests)).to_have_count(40)
    reads = [call for call in scenario.error_calls if call["queue"] == ["request_start"]]
    assert [call["offset"] for call in reads[-2:]] == [["20"], ["20"]]


def test_shrinking_queue_distinguishes_loaded_records_from_latest_count(
    work_errors_page, monkeypatch
):
    page, scenario, base = work_errors_page
    _many_records(scenario, monkeypatch)
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    expect(_rows(requests)).to_have_count(20)
    scenario.empty_queues.add("request_start")
    _scroll_bottom(requests)
    expect(requests.get_by_text("20 loaded · latest count 0", exact=False)).to_be_visible()
    expect(_rows(requests)).to_have_count(20)
    expect(requests.get_by_role("button", name="Load more records", exact=True)).to_have_count(0)
    requests.get_by_role("button", name="Refresh preview", exact=True).click()
    expect(_rows(requests)).to_have_count(0)
    expect(requests.get_by_text("0 of 0 loaded", exact=False)).to_be_visible()


def test_late_pending_append_cannot_replace_changed_scope(work_errors_page, monkeypatch):
    page, scenario, base = work_errors_page
    control = _many_records(scenario, monkeypatch)
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    expect(_rows(requests)).to_have_count(20)
    control["hold"].add(("request_start", "pending", 20))
    with page.expect_request(
        lambda request: "queue=request_start" in request.url and "offset=20" in request.url
    ):
        _scroll_bottom(requests)
    requests.get_by_role("button", name="With errors", exact=True).click()
    expect(_rows(requests)).to_have_count(10)
    assert control["held"]
    for route in control["held"]:
        control["release"](route)
    expect(_rows(requests)).to_have_count(10)
    expect(
        requests.get_by_role("button", name=re.compile(r"^Open Request retry 21"))
    ).to_have_count(0)
    expect(requests.get_by_text("10 of 10 loaded", exact=False)).to_be_visible()
    assert _viewport(requests).evaluate("element => element.scrollTop") == 0
    expect(requests.get_by_role("alert")).to_have_count(0)


def test_native_keyboard_scroll_and_load_more_fit_mobile(work_errors_page, monkeypatch):
    page, scenario, base = work_errors_page
    _many_records(scenario, monkeypatch)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/admin")
    requests = _block(page, "Request starts")
    expect(_rows(requests)).to_have_count(20)
    view = _viewport(requests)
    view.focus()
    view.press("PageDown")
    page.wait_for_function(
        'document.querySelector(\'[aria-label="Request starts overview"] [role="region"]\').scrollTop > 0'
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert _visible_geometry(requests)["intersecting"] <= 4
    more = requests.get_by_role("button", name="Load more records", exact=True)
    more.focus()
    more.press("Enter")
    # Focusing the fallback scrolls it into view and can trigger the automatic batch
    # before Enter requests another one. Both paths must retain bounded reads.
    expect(_rows(requests).nth(39)).to_be_attached()
    assert _rows(requests).count() in {40, 47}
    assert all(call["limit"] == ["20"] for call in scenario.error_calls)
    assert any(
        call["queue"] == ["request_start"] and call["offset"] == ["20"]
        for call in scenario.error_calls
    )
    expect(page.locator("#operations-errors")).to_have_count(0)
