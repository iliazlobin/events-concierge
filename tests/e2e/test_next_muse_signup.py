"""Muse queue, unread progress and credentials in the real bundle; writes are fixtures."""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlsplit

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
        self.muse_enabled = True
        self.muse_calls = []
        self.event_detail_calls = []
        self.connected = False
        self.batches = []
        self.queued = []
        self.registrations = []
        self.acknowledged = []
        self.queue_error = False
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
                **({"muse_enabled": self.muse_enabled} if self.muse_enabled is not None else {}),
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
        if path.startswith(("/v1/me/muse/", "/v1/muse/")):
            self.muse_calls.append((method, path))
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
            self.event_detail_calls.append(path)
            event_id = path.rsplit("/", 1)[1]
            self.respond(
                route,
                next(event for event in self.events if event["canonical_event_id"] == event_id),
            )
        elif path == "/v1/me/muse/connection":
            self.connection(route)
        elif path == "/v1/me/muse/registrations":
            self.queue(route)
        elif path == "/v1/me/muse/registrations/seen":
            self.seen(route)
        elif path == "/v1/me/muse/batches":
            self.respond(route, self.batches)
        elif path == "/auth/identity/start":
            self.respond(route, {"state": "s" * 43})
        else:
            super().handle(route)

    def queue(self, route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            self.queued.append(body)
            if self.queue_error:
                self.harness.allowed_console_error_fragments.append("503")
                self.respond(route, {"detail": "Registration queue temporarily unavailable"}, 503)
                return
            item = next((task for task in self.registrations
                         if task["event"]["canonical_event_id"] == body["event_id"]), None)
            if item is None:
                event = next(event for event in self.events
                             if event["canonical_event_id"] == body["event_id"])
                item = self.registration(event)
                self.registrations.insert(0, item)
            self.respond(route, item, 201)
        else:
            query = parse_qs(urlsplit(route.request.url).query)
            cursor = query.get("cursor", [None])[0]
            start = next((index + 1 for index, item in enumerate(self.registrations)
                          if item["event"]["canonical_event_id"] == cursor), 0)
            limit = int(query.get("limit", ["50"])[0])
            items = self.registrations[start:start + limit]
            self.respond(route, {"items": items, "total": len(self.registrations),
                                 "unread_count": sum(item["unread"] for item in self.registrations),
                                 "next_cursor": items[-1]["event"]["canonical_event_id"]
                                 if items and start + limit < len(self.registrations) else None})

    def seen(self, route):
        body = route.request.post_data_json
        self.acknowledged.extend(body["items"])
        for observed in body["items"]:
            for item in self.registrations:
                if (item["event"]["canonical_event_id"] == observed["event_id"]
                        and item["version"] <= observed["version"]):
                    item["unread"] = False
        route.fulfill(status=204)

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

    @classmethod
    def registration(cls, event, status="queued", version=1, unread=False):
        return cls.item(event, status) | {
            "batch_id": BATCH_ID, "created_at": "2030-06-01T00:00:00Z",
            "version": version, "unread": unread,
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


@pytest.mark.parametrize("muse_enabled", [False, None])
def test_disabled_muse_has_no_controls_restore_or_settings_requests(page_factory, muse_enabled):
    harness, api = install(page_factory)
    api.muse_enabled = muse_enabled
    event_id = api.events[0]["canonical_event_id"]
    harness.context.add_init_script(
        f"sessionStorage.setItem('ec:muse:intent', JSON.stringify({{"
        f"eventId: '{event_id}', savedAt: Date.now() }}));"
    )
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=&registrations=1")
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("Sign up with Muse for"))).to_have_count(0)
    expect(page.get_by_role("checkbox", name=re.compile("Muse"))).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Registrations"))).to_have_count(0)
    expect(page.get_by_role("dialog", name="Registrations", exact=True)).to_have_count(0)
    page.get_by_role("button", name=re.compile(r"^Account menu")).click()
    expect(page.get_by_role("menuitem", name=re.compile("Muse signups"))).to_have_count(0)
    assert page.evaluate("sessionStorage.getItem('ec:muse:intent')") is not None
    assert api.event_detail_calls == []
    page.goto(BASE + "/settings/muse")
    expect(page.get_by_text("Muse signups are not enabled for this release.", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Create connection key", exact=True)).to_have_count(0)
    assert api.muse_calls == []
    assert api.writes == []
    assert api.unexpected == []


@pytest.mark.parametrize("width", [1440, 320])
def test_one_icon_queues_each_event_and_progress_panel_retains_filters(page_factory, width, tmp_path):
    harness, api = install(page_factory, width)
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=")
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(page.get_by_role("checkbox", name=re.compile("Muse"))).to_have_count(0)
    expect(page.locator(".event-card__muse")).to_have_count(0)
    first = page.get_by_role("button", name="Sign up with Muse for Friday Night Jazz", exact=True)
    expect(first).to_have_attribute("title", "Sign up with Muse")
    first.click()
    expect(page.get_by_role("button", name="View registration for Friday Night Jazz")).to_be_visible()
    page.get_by_role("button", name="Sign up with Muse for Saturday Night Jazz").click()
    expect(page.get_by_role("button", name="View registration for Saturday Night Jazz")).to_be_visible()
    assert [item["event_id"] for item in api.queued] == [event["canonical_event_id"] for event in api.events]
    assert all(set(item) == {"request_id", "event_id"} for item in api.queued)
    assert all(write[2]["x-fixture-csrf"] == "bound-fixture" for write in api.writes)
    page.get_by_role("button", name="View registration for Friday Night Jazz").click()
    dialog = page.get_by_role("dialog", name="Registrations", exact=True)
    expect(dialog).to_contain_text("Friday Night Jazz")
    expect(dialog).to_contain_text("Saturday Night Jazz")
    expect(dialog).to_contain_text("Muse")
    assert "view=events" in page.url and "when=all" in page.url and "registrations=1" in page.url
    dialog.get_by_role("button", name="Copy Muse instruction").click()
    dialog.get_by_role("button", name="View instruction").click()
    expect(dialog.get_by_role("textbox", name="Muse signup instruction")).to_be_visible()
    instruction = page.evaluate("window.__fixtureCopied")
    assert "queued" in instruction.lower() and FIXTURE_KEY not in instruction
    expect(dialog.get_by_role("link", name="Open Muse", exact=True)).to_have_attribute("href", "https://muse.ai/")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"muse-queue-{width}.png"))
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(page.get_by_role("button", name="View registration for Friday Night Jazz")).to_be_focused()
    assert "registrations=1" not in page.url
    assert len(api.queued) == 2
    assert api.unexpected == []


def test_unread_badge_and_results_acknowledge_observed_versions_only(page_factory):
    harness, api = install(page_factory)
    api.registrations = [api.registration(api.events[0], "needs_input", 2, True),
                         api.registration(api.events[1], "uncertain", 3, True)]
    api.registrations[0]["outcome"] = {"status": "needs_input", "note": "Login needed in Muse",
                                     "confirmation_reference": None, "evidence_url": None}
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=")
    button = page.get_by_role("button", name=re.compile("^Registrations"))
    expect(button).to_contain_text("2")
    assert api.acknowledged == []
    button.click()
    dialog = page.get_by_role("dialog", name="Registrations", exact=True)
    expect(dialog).to_contain_text("Needs your input")
    expect(dialog).to_contain_text("Needs verification")
    expect(dialog).to_contain_text("Login needed in Muse")
    expect(dialog.locator('[aria-label="Unread update"]')).to_have_count(0)
    expect(button).not_to_contain_text("2")
    assert {item["version"] for item in api.acknowledged} == {2, 3}
    assert not any(item["unread"] for item in api.registrations)
    # A subsequent connector report is an update, even if a prior version was viewed.
    api.registrations[0].update(status="registered", version=3, unread=True,
                                outcome={"status": "registered", "note": "Confirmed by provider",
                                         "confirmation_reference": "fixture-confirmation",
                                         "evidence_url": "https://lu.ma/friday-jazz"})
    dialog.get_by_role("button", name="Refresh registrations").click()
    expect(dialog).to_contain_text("Registered · reported by Muse")
    expect(dialog).to_contain_text("fixture-confirmation")
    expect(dialog.get_by_role("link", name=re.compile("evidence", re.I))).to_have_attribute("href", "https://lu.ma/friday-jazz")
    expect(dialog.locator('[aria-label="Unread update"]')).to_have_count(0)
    assert api.acknowledged[-1] == {"event_id": api.events[0]["canonical_event_id"], "version": 3}
    assert api.unexpected == []


def test_queue_failure_is_recoverable_without_duplicate_intent(page_factory):
    harness, api = install(page_factory)
    api.queue_error = True
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=")
    button = page.get_by_role("button", name="Sign up with Muse for Friday Night Jazz")
    button.click()
    expect(page.get_by_role("alert").filter(has_text="Registration queue")).to_contain_text("unavailable")
    expect(button).to_be_enabled()
    api.queue_error = False
    button.click()
    expect(page.get_by_role("button", name="View registration for Friday Night Jazz")).to_be_visible()
    assert len(api.queued) == 2 and api.queued[0] == api.queued[1]
    assert len(api.registrations) == 1
    assert api.unexpected == []


def test_guest_click_resumes_one_event_after_sign_in_without_anonymous_writes(page_factory):
    harness, api = install(page_factory)
    api.signed_in = False
    page = harness.page
    return_path = "/?view=events&when=all&city="
    page.goto(BASE + return_path)
    page.get_by_role("button", name="Sign up with Muse for Friday Night Jazz").click()
    page.wait_for_url(re.compile(r".*/sign-in\?return_to="))
    stored = page.evaluate("JSON.parse(sessionStorage.getItem('ec:muse:intent'))")
    assert stored["eventId"] == api.events[0]["canonical_event_id"]
    assert set(stored) == {"eventId", "savedAt"}
    assert api.queued == [] and not any(path.startswith("/v1/me/muse/") for _, path, _ in api.writes)
    # Identity provider behavior has its own suite; resume only after a verified session.
    api.signed_in = True
    page.goto(BASE + return_path)
    expect(page.get_by_role("button", name="View registration for Friday Night Jazz")).to_be_visible()
    assert len(api.queued) == 1
    assert page.evaluate("sessionStorage.getItem('ec:muse:intent')") is None
    page.reload()
    expect(page.get_by_role("button", name="View registration for Friday Night Jazz")).to_be_visible()
    assert len(api.queued) == 1
    assert api.unexpected == []


def test_older_registrations_remain_accessible_beyond_first_page(page_factory):
    harness, api = install(page_factory)
    api.registrations = [api.registration(api.events[0] | {
        "canonical_event_id": f"{index:08x}-1111-4111-8111-111111111111", "title": f"Queued event {index}"
    }) for index in range(55)]
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=&registrations=1")
    dialog = page.get_by_role("dialog", name="Registrations", exact=True)
    expect(dialog).to_contain_text("Queued event 0")
    expect(dialog).not_to_contain_text("Queued event 54")
    dialog.get_by_role("button", name="Load more registrations").click()
    expect(dialog).to_contain_text("Queued event 54")
    expect(dialog.get_by_role("button", name="Load more registrations")).to_have_count(0)
    older = api.registrations[-1]
    older.update(status="needs_input", version=2, unread=True,
                 outcome={"status": "needs_input", "note": "Answer needed for older event",
                          "confirmation_reference": None, "evidence_url": None})
    dialog.get_by_role("button", name="Refresh registrations").click()
    expect(dialog).to_contain_text("Answer needed for older event")
    assert older["unread"]  # Loading an offscreen result does not mark it as viewed.
    old_card = dialog.locator("li").filter(has_text="Queued event 54")
    old_card.scroll_into_view_if_needed()
    expect(old_card.locator('[aria-label="Unread update"]')).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Registrations"))).not_to_contain_text("1")
    assert not older["unread"]
    older.update(status="registered", version=3, unread=True,
                 outcome={"status": "registered", "note": "Older event confirmed",
                          "confirmation_reference": "older-confirmation",
                          "evidence_url": "https://lu.ma/friday-jazz"})
    dialog.get_by_role("button", name="Refresh registrations").click()
    expect(old_card).to_contain_text("older-confirmation")
    old_card.scroll_into_view_if_needed()
    expect(old_card.locator('[aria-label="Unread update"]')).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Registrations"))).not_to_contain_text("1")
    assert api.acknowledged[-1] == {"event_id": older["event"]["canonical_event_id"], "version": 3}
    assert api.unexpected == []


@pytest.mark.parametrize("width", [1440, 320])
def test_settings_secret_is_one_time_with_progress_in_registrations(page_factory, width):
    harness, api = install(page_factory, width)
    page = harness.page
    page.goto(BASE + "/settings/muse")
    expect(page.get_by_role("heading", name="Muse signups", exact=True)).to_be_visible()
    expect(page.get_by_role("textbox", name="Muse API description")).to_have_value(BASE + "/v1/muse/openapi.json")
    page.get_by_role("button", name="Create connection key").click()
    secret = page.get_by_label("Muse connection key", exact=True)
    expect(secret).to_have_attribute("type", "password")
    assert secret.input_value() == FIXTURE_KEY
    assert FIXTURE_KEY not in page.evaluate("JSON.stringify([localStorage, sessionStorage])")
    page.get_by_role("button", name="Copy connection key", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("secure credential setup")
    expect(page.get_by_role("link", name="Registrations", exact=True)).to_be_visible()
    page.get_by_role("button", name="Hide key", exact=True).click()
    expect(secret).not_to_be_visible()
    page.get_by_role("button", name="Disconnect Muse").click()
    expect(page.get_by_role("status")).to_contain_text("access revoked")
    page.reload()
    expect(page.get_by_text("No active connection key.", exact=True)).to_be_visible()
    assert [item[:2] for item in api.writes] == [("POST", "/v1/me/muse/connection"),
                                                ("DELETE", "/v1/me/muse/connection")]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert api.unexpected == []
