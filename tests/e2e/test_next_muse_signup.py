"""Muse selection and credential UI in the production bundle; all writes are fixtures."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Route, expect
from tests.e2e.test_next_release_profile import ReleaseApi, catalog_event, second_catalog_event

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(not BASE, reason="Set EC_CONSUMER_WEB_URL"),
]
BATCH_ID = "99999999-9999-4999-8999-999999999999"
FIXTURE_KEY = "ec_muse_" + "1" * 32 + "." + "x" * 43


class MuseApi(ReleaseApi):
    def __init__(self, harness):
        super().__init__(local_demo=False)
        self.harness = harness
        self.signed_in = True
        self.connected = False
        self.batches = []
        self.prepared = []
        self.writes = []
        self.events = [
            catalog_event()
            | {
                "registration_urls": ["https://lu.ma/friday-jazz"],
                "sources": [
                    {"registration_url": "https://lu.ma/friday-jazz", "source": "public_jsonld"}
                ],
            },
            second_catalog_event()
            | {
                "registration_urls": ["https://www.meetup.com/jazz/events/123/"],
                "sources": [
                    {
                        "registration_url": "https://www.meetup.com/jazz/events/123/",
                        "source": "meetup",
                    }
                ],
            },
        ]

    def config(self, route: Route):
        self.respond(
            route,
            {
                "product_name": "Events Concierge",
                "release_profile": "discovery",
                "local_demo": False,
                "auth_mode": "deployment_session",
                "anonymous_browsing": True,
                "auth_provider": "identity_platform",
                "auth_start_url": "/sign-in",
                "csrf_cookie_name": "fixture_csrf",
                "csrf_header_name": "X-Fixture-CSRF",
                "consumer_legal_mode": "deferred",
                "legal_policy": None,
                "identity_platform": {
                    "project_id": "events-fixture",
                    "api_key": "browser-key-fixture",
                    "auth_domain": "events-fixture.firebaseapp.com",
                    "providers": ["google.com"],
                },
            },
        )

    def handle(self, route: Route):
        path, method = urlsplit(route.request.url).path, route.request.method
        if method not in {"GET", "HEAD"}:
            self.writes.append((method, path, route.request.headers))
        if path == "/v1/me" and not self.signed_in:
            self.harness.allowed_console_error_fragments.append("401")
            self.respond(route, {"detail": "Sign in required"}, 401)
        elif path == "/v1/catalog/events":
            self.respond(
                route,
                {
                    "items": self.events,
                    "next_cursor": None,
                    "providers": [],
                    "topic_facets": [],
                    "city_facets": [],
                },
            )
        elif path.startswith("/v1/catalog/events/") and not path.endswith("/summary"):
            event_id = path.rsplit("/", 1)[1]
            self.respond(
                route,
                next(event for event in self.events if event["canonical_event_id"] == event_id),
            )
        elif path == "/v1/me/muse/connection":
            self.connection(route)
        elif path == "/v1/me/muse/batches":
            if method == "POST":
                body = route.request.post_data_json
                self.prepared.append(body)
                batch = {
                    "batch_id": BATCH_ID,
                    "request_id": body["request_id"],
                    "created_at": "2030-06-01T00:00:00Z",
                    "items": [
                        self.item(event)
                        for event in self.events
                        if event["canonical_event_id"] in body["event_ids"]
                    ],
                }
                self.batches = [batch]
                self.respond(route, batch, 201)
            else:
                self.respond(route, self.batches)
        elif path == "/auth/identity/start":
            self.respond(route, {"state": "s" * 43})
        else:
            super().handle(route)

    def connection(self, route):
        if route.request.method == "POST":
            self.connected = True
            self.respond(route, {"token": FIXTURE_KEY, "expires_at": "2030-07-01T00:00:00Z"}, 201)
        elif route.request.method == "DELETE":
            self.connected = False
            route.fulfill(status=204)
        else:
            self.respond(
                route,
                {
                    "connected": self.connected,
                    "expires_at": "2030-07-01T00:00:00Z" if self.connected else None,
                },
            )

    @staticmethod
    def item(event, status="queued"):
        return {
            "event": {
                key: event.get(key)
                for key in (
                    "canonical_event_id",
                    "title",
                    "start_at",
                    "end_at",
                    "venue_name",
                    "city",
                )
            }
            | {
                "registration_url": event["registration_urls"][0],
                "price_status": "free",
                "observed_at": "2030-06-01T00:00:00Z",
            },
            "status": status,
            "attempt_id": None,
            "outcome": None,
            "updated_at": "2030-06-01T00:00:00Z",
        }


def install(page_factory, width=1440):
    harness = page_factory(width=width)
    api = MuseApi(harness)
    api.install(harness.page)
    harness.page.route("**/auth/**", api.handle)
    harness.context.add_cookies([{"name": "fixture_csrf", "value": "bound-fixture", "url": BASE}])
    harness.context.add_init_script("""
        Object.defineProperty(navigator, "clipboard", { value: {
          writeText: async value => { window.__fixtureCopied = value; }
        }});
    """)
    return harness, api


@pytest.mark.parametrize("width", [1440, 320])
def test_select_two_events_review_and_prepare_without_automatic_dispatch(
    page_factory, width, tmp_path
):
    harness, api = install(page_factory, width)
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=")
    page.get_by_role("checkbox", name="Select Friday Night Jazz for Muse").check()
    page.get_by_role("checkbox", name="Select Saturday Night Jazz for Muse").check()
    tray = page.get_by_role("complementary", name="Selected Muse events")
    expect(tray).to_contain_text("2 selected")
    tray.get_by_role("button", name="Sign up with Muse", exact=True).click()
    dialog = page.get_by_role("dialog", name="Sign up with Muse", exact=True)
    expect(dialog).to_contain_text("Friday Night Jazz")
    expect(dialog).to_contain_text("Saturday Night Jazz")
    dialog.get_by_role("button", name="Prepare signup batch").click()
    expect(dialog.get_by_role("textbox", name="Muse signup instruction")).to_contain_text(BATCH_ID)
    assert api.prepared[0]["event_ids"] == [event["canonical_event_id"] for event in api.events]
    assert api.writes[0][2]["x-fixture-csrf"] == "bound-fixture"
    expect(dialog.get_by_role("link", name="Open Muse", exact=True)).to_have_attribute(
        "href", "https://muse.ai/"
    )
    dialog.get_by_role("button", name="Copy instruction").click()
    expect(dialog.get_by_role("button", name="Instruction copied")).to_be_visible()
    assert FIXTURE_KEY not in page.evaluate("window.__fixtureCopied")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"muse-batch-{width}.png"))
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(tray.get_by_role("button", name="Sign up with Muse", exact=True)).to_be_focused()
    assert api.unexpected == []


def test_guest_selection_survives_explicit_sign_in_without_anonymous_writes(page_factory):
    harness, api = install(page_factory)
    api.signed_in = False
    page = harness.page
    return_path = "/?view=events&when=all&city="
    page.goto(BASE + return_path)
    page.get_by_role("button", name="Sign up with Muse", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Sign up with Muse")
    expect(dialog.get_by_role("link", name="Sign in to use Muse")).to_be_visible()
    assert api.writes == []
    dialog.get_by_role("link", name="Sign in to use Muse").click()
    page.wait_for_url(re.compile(r".*/sign-in\?return_to="))
    stored = page.evaluate("JSON.parse(sessionStorage.getItem('ec:muse:selection'))")
    assert stored["eventIds"] == [api.events[0]["canonical_event_id"]]
    assert "title" not in stored and "token" not in stored
    # The identity authority is exercised in its own suite; this test resumes the verified return.
    api.signed_in = True
    page.goto(BASE + return_path)
    dialog = page.get_by_role("dialog", name="Sign up with Muse")
    expect(dialog).to_contain_text("Friday Night Jazz")
    expect(dialog.get_by_role("button", name="Prepare signup batch")).to_be_visible()
    assert page.evaluate("sessionStorage.getItem('ec:muse:selection')") is None
    assert api.unexpected == []


@pytest.mark.parametrize("width", [1440, 320])
def test_settings_secret_is_one_time_and_results_are_not_promoted(page_factory, width, tmp_path):
    harness, api = install(page_factory, width)
    api.events[1]["title"] = api.events[0]["title"]
    api.batches = [
        {
            "batch_id": BATCH_ID,
            "request_id": BATCH_ID,
            "created_at": "2030-06-01T00:00:00Z",
            "items": [api.item(api.events[0], "waitlisted"), api.item(api.events[1], "uncertain")],
        }
    ]
    page = harness.page
    page.goto(BASE + "/settings/muse")
    expect(page.get_by_role("heading", name="Muse signups", exact=True)).to_be_visible()
    expect(page.get_by_role("textbox", name="Muse API description")).to_have_value(
        BASE + "/v1/muse/openapi.json"
    )
    page.get_by_role("button", name="Create connection key").click()
    secret = page.get_by_label("Muse connection key", exact=True)
    expect(secret).to_have_attribute("type", "password")
    assert secret.input_value() == FIXTURE_KEY
    assert FIXTURE_KEY not in page.evaluate("JSON.stringify([localStorage, sessionStorage])")
    page.get_by_role("button", name="Copy connection key", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("secure credential setup")
    expect(page.get_by_text("Waitlisted", exact=True)).to_be_visible()
    expect(page.get_by_text("Needs verification", exact=True)).to_be_visible()
    results = page.locator("article li")
    expect(results.nth(0)).to_contain_text("Jun 14, 2030")
    expect(results.nth(0)).to_contain_text("PDT")
    expect(results.nth(0)).to_contain_text("Waitlisted")
    expect(results.nth(1)).to_contain_text("Jun 15, 2030")
    expect(results.nth(1)).to_contain_text("PDT")
    expect(results.nth(1)).to_contain_text("Needs verification")
    expect(page.get_by_text("Registered · reported by Muse", exact=True)).not_to_be_visible()
    page.locator("article").screenshot(path=str(tmp_path / f"muse-results-{width}.png"))
    page.screenshot(path=str(tmp_path / f"muse-settings-{width}.png"))
    page.get_by_role("button", name="Hide key", exact=True).click()
    expect(secret).not_to_be_visible()
    page.get_by_role("button", name="Disconnect Muse").click()
    expect(page.get_by_role("status")).to_contain_text("access revoked")
    page.reload()
    expect(page.get_by_text("No active connection key.", exact=True)).to_be_visible()
    expect(secret).not_to_be_visible()
    assert [item[:2] for item in api.writes] == [
        ("POST", "/v1/me/muse/connection"),
        ("DELETE", "/v1/me/muse/connection"),
    ]
    assert api.unexpected == []
