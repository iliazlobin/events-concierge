"""Muse queue, unread progress and credentials in the real bundle; writes are fixtures."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Route, expect
from tests.e2e.test_next_release_profile import (
    ENTITY_ID,
    ReleaseApi,
    catalog_event,
    second_catalog_event,
)

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
        self.registration_account = {"executor": "muse", "expected_email": None, "provider_overrides": {}, "revision": 0, "updated_at": None}
        self.account_saves = []
        self.account_error = None
        self.batches = []
        self.queued = []
        self.registrations = []
        self.acknowledged = []
        self.queue_error = False
        self.writes = []
        self.providers = []
        self.catalog_reads = []
        observed_at = datetime.now(UTC).isoformat()
        self.events = [
            catalog_event()
            | {
                "registration_urls": ["https://lu.ma/friday-jazz"],
                "sources": [
                    {"registration_url": "https://lu.ma/friday-jazz", "source": "public_jsonld",
                     "last_seen_at": observed_at}
                ],
            },
            second_catalog_event()
            | {
                "registration_urls": ["https://www.meetup.com/jazz/events/123/"],
                "sources": [
                    {
                        "registration_url": "https://www.meetup.com/jazz/events/123/",
                        "source": "meetup",
                        "last_seen_at": observed_at,
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

    def record_request(self, route, path, method):
        if path.startswith(("/v1/me/muse/", "/v1/muse/")):
            self.muse_calls.append((method, path))
        if method not in {"GET", "HEAD"}:
            self.writes.append((method, path, route.request.headers))

    def handle(self, route: Route):
        path, method = urlsplit(route.request.url).path, route.request.method
        self.record_request(route, path, method)
        if path == "/v1/me" and not self.signed_in:
            self.harness.allowed_console_error_fragments.append("401")
            self.respond(route, {"detail": "Sign in required"}, 401)
        elif path == "/v1/catalog/events":
            self.catalog_reads.append(route.request.url)
            self.respond(
                route,
                {
                    "items": self.events,
                    "next_cursor": None,
                    "providers": self.providers,
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
        elif path == "/v1/me/registration-accounts/muse":
            self.account_preferences(route)
        elif path == "/v1/me/muse/connection":
            self.connection(route)
        elif path == "/v1/me/muse/registrations/by-event":
            ids = parse_qs(urlsplit(route.request.url).query).get("event_id", [])
            self.respond(route, [item for item in self.registrations if item["event"]["canonical_event_id"] in ids])
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

    def account_preferences(self, route):
        if route.request.method != "PUT":
            self.respond(route, self.registration_account)
            return
        body = route.request.post_data_json
        self.account_saves.append(body)
        if self.account_error:
            self.harness.allowed_console_error_fragments.append(str(self.account_error))
            self.respond(route, {"detail": "Account settings unavailable"}, self.account_error)
        elif body["revision"] != self.registration_account["revision"] + 1:
            self.harness.allowed_console_error_fragments.append("409")
            self.respond(route, {"detail": "Registration account changed"}, 409)
        else:
            self.registration_account = body | {"executor": "muse", "updated_at": "2030-06-01T00:00:00Z"}
            self.respond(route, self.registration_account)

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
                                 "status_counts": {status: sum(item["status"] == status for item in self.registrations)
                                                   for status in ("queued", "in_progress", "needs_input", "awaiting_approval",
                                                                  "waitlisted", "registered", "failed", "uncertain")},
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


def install(page_factory, width=1440, *, has_touch=False):
    harness = page_factory(width=width, has_touch=has_touch)
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
    expect(page.get_by_role("menuitem", name=re.compile("Registrations"))).to_have_count(0)
    assert page.evaluate("sessionStorage.getItem('ec:muse:intent')") is not None
    assert api.event_detail_calls == []
    page.goto(BASE + "/settings/muse")
    expect(page.get_by_text("Not enabled.", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Create key", exact=True)).to_have_count(0)
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
    expect(page.get_by_role("link", name="Documentation", exact=True)).to_be_visible()
    expect(page.locator(".event-card__muse")).to_have_count(0)
    first = page.get_by_role("button", name="Sign up with Muse for Friday Night Jazz", exact=True)
    expect(first).to_have_attribute("title", "Sign up with Muse")
    expect(first).to_have_attribute("data-registration-state", "available")
    expect(first.locator(".event-muse-action__badge")).to_have_count(0)
    first.click()
    dialog = page.get_by_role("dialog", name="Registrations", exact=True)
    expect(dialog).to_contain_text("Friday Night Jazz")
    dialog.get_by_role("button", name="Close registrations").click()
    queued = page.get_by_role("button", name="View registration for Friday Night Jazz", exact=True)
    expect(queued).to_have_attribute("data-registration-state", "queued")
    expect(queued.locator(".event-muse-action__badge")).to_be_visible()
    expect(queued.locator(".sr-only")).to_have_text("Queued")
    queued.hover()
    assert queued.locator(".event-muse-action__badge").evaluate("element => getComputedStyle(element).color") == "rgb(184, 162, 255)"
    page.get_by_role("button", name="Sign up with Muse for Saturday Night Jazz").click()
    expect(dialog).to_contain_text("Saturday Night Jazz")
    dialog.get_by_role("button", name="Close registrations").click()
    expect(page.get_by_role("button", name="View registration for Saturday Night Jazz")).to_be_visible()
    assert queued.get_attribute("aria-describedby") != page.get_by_role("button", name="View registration for Saturday Night Jazz", exact=True).get_attribute("aria-describedby")
    assert [item["event_id"] for item in api.queued] == [event["canonical_event_id"] for event in api.events]
    assert all(set(item) == {"request_id", "event_id"} for item in api.queued)
    assert all(write[2]["x-fixture-csrf"] == "bound-fixture" for write in api.writes)
    page.get_by_role("button", name="View registration for Friday Night Jazz").click()
    dialog = page.get_by_role("dialog", name="Registrations", exact=True)
    expect(dialog).to_contain_text("Friday Night Jazz")
    expect(dialog).to_contain_text("Saturday Night Jazz")
    expect(dialog).to_contain_text("Muse")
    assert "view=events" in page.url and "when=all" in page.url and "registrations=1" in page.url
    expect(dialog.get_by_role("button", name="Copy Muse instruction")).to_have_count(0)
    expect(dialog.get_by_role("textbox", name="Muse signup instruction")).to_have_count(0)
    expect(dialog.get_by_role("link", name="Muse settings", exact=True)).to_have_attribute("href", "/settings/registrations")
    expect(dialog.locator('[data-status="queued"]').first).to_have_text("Queued")
    expect(dialog.get_by_role("link", name="Open Muse", exact=True)).to_have_attribute("href", "https://muse.ai/")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"muse-queue-{width}.png"))
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(page.get_by_role("button", name="View registration for Friday Night Jazz")).to_be_focused()
    assert "registrations=1" not in page.url
    assert len(api.queued) == 2
    assert api.unexpected == []


def activate_unavailable_help(page, action, *, touch):
    """Use actual input; Playwright's locator actionability treats aria-disabled as native disabled."""
    action.scroll_into_view_if_needed()
    box = action.bounding_box()
    assert box is not None
    point = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}
    assert action.evaluate("(element, point) => element.contains(document.elementFromPoint(point.x, point.y))", point)
    if touch:
        page.touchscreen.tap(**point)
    else:
        page.mouse.click(**point)


@pytest.mark.parametrize("width", [1440, 320])
def test_unavailable_muse_explains_blockers_without_queue_or_card_actions(page_factory, width, tmp_path):
    harness, api = install(page_factory, width, has_touch=width == 320)
    event = api.events[0]
    event["price_status"] = "unknown"
    event["registration_urls"] = ["https://events.example.test/friday-jazz"]
    event["sources"][0]["registration_url"] = event["registration_urls"][0]
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=")
    card = page.locator("article").filter(has=page.get_by_role("heading", name=event["title"], exact=True))
    action = card.locator(".event-muse-action")
    expect(action).to_have_attribute("aria-disabled", "true")
    assert action.evaluate("element => !element.disabled && element.tabIndex >= 0")
    expanded = card.locator(".event-card__summary-trigger").get_attribute("aria-expanded")
    initial_tint = action.evaluate("element => getComputedStyle(element).color")
    expect(page.get_by_role("tooltip")).to_have_count(0)

    action.hover()
    tooltip = page.get_by_role("tooltip")
    expect(tooltip).to_contain_text(re.compile("free", re.I))
    expect(tooltip).to_contain_text("Luma and Meetup")
    expect(action).to_have_attribute("aria-describedby", tooltip.get_attribute("id"))
    expect(action).not_to_have_css("color", initial_tint)
    assert tooltip.evaluate("element => { const box = element.getBoundingClientRect(); return box.left >= 0 && box.right <= innerWidth; }")
    page.screenshot(path=str(tmp_path / f"muse-unavailable-hover-{width}.png"))
    tooltip.hover()
    page.wait_for_timeout(200)  # A reader can move onto the help text without losing it.
    expect(tooltip).to_be_visible()
    page.keyboard.press("Escape")
    expect(tooltip).to_have_count(0)
    page.mouse.move(0, 0)

    action.focus()
    expect(tooltip).to_be_visible()
    action.press("Enter")
    expect(tooltip).to_be_visible()
    action.press("Escape")
    expect(tooltip).to_have_count(0)
    action.press("Space")
    expect(tooltip).to_be_visible()
    page.get_by_role("heading", name="Events", exact=True).click()
    expect(tooltip).to_have_count(0)
    activate_unavailable_help(page, action, touch=width == 320)
    expect(tooltip).to_be_visible()
    page.screenshot(path=str(tmp_path / f"muse-unavailable-tap-{width}.png"))
    expect(card.locator(".event-card__summary-trigger")).to_have_attribute("aria-expanded", expanded)
    expect(page.get_by_role("dialog", name="Registrations", exact=True)).to_have_count(0)
    assert api.queued == [] and api.writes == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert api.unexpected == []


@pytest.mark.parametrize("width", [1440, 320])
def test_stale_supported_event_explains_freshness_but_existing_task_stays_accessible(page_factory, width):
    harness, api = install(page_factory, width)
    event = api.events[0]
    event["sources"][0]["last_seen_at"] = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    page = harness.page
    path = BASE + "/?view=events&when=all&city="
    page.goto(path)
    action = page.get_by_role("button", name="Muse unavailable for Friday Night Jazz", exact=True)
    expect(action).to_have_attribute("data-registration-state", "unavailable")
    expect(action).to_have_attribute("aria-disabled", "true")
    action.focus()
    tooltip = page.get_by_role("tooltip")
    expect(tooltip).to_contain_text("Event details need a refresh.")
    expect(tooltip).not_to_contain_text("Luma and Meetup")
    expect(tooltip).not_to_contain_text(re.compile("free", re.I))
    action.press("Enter")
    assert api.queued == [] and api.writes == []
    # Eligibility gates new work; it must never hide a task already being handled.
    api.registrations = [api.registration(event, "needs_input", version=2)]
    api.registrations[0]["outcome"] = {"status": "needs_input", "note": "Provider needs your reply"}
    page.goto(path)
    existing = page.get_by_role("button", name="View registration for Friday Night Jazz", exact=True)
    expect(existing).to_have_attribute("data-registration-state", "needs_input")
    expect(existing).not_to_have_attribute("aria-disabled", "true")
    existing.click()
    expect(page.get_by_role("dialog", name="Registrations", exact=True)).to_contain_text("Provider needs your reply")
    assert api.queued == []
    assert api.unexpected == []


def test_pending_queue_is_natively_disabled_and_cannot_submit_twice(page_factory):
    harness, api = install(page_factory)
    page = harness.page
    held = []

    def hold_queue(route):
        if route.request.method == "POST":
            held.append(route)
        else:
            api.handle(route)

    page.route("**/v1/me/muse/registrations", hold_queue)
    page.goto(BASE + "/?view=events&when=all&city=")
    action = page.get_by_role("button", name="Sign up with Muse for Friday Night Jazz", exact=True)
    action.click()
    expect(action).to_be_disabled()
    expect(action).to_have_attribute("disabled", "")
    expect(action).to_have_attribute("aria-busy", "true")
    expect(action).to_have_attribute("data-registration-state", "saving")
    assert len(held) == 1
    action.evaluate("element => element.click()")  # Native disabled semantics suppress activation.
    assert len(held) == 1
    api.handle(held.pop())
    queued = page.get_by_role("button", name="View registration for Friday Night Jazz", exact=True)
    expect(queued).to_have_attribute("data-registration-state", "queued")
    expect(queued).to_be_enabled()
    assert len(api.queued) == 1
    page.get_by_role("button", name="Close registrations", exact=True).click()
    queued.click()
    expect(page.get_by_role("dialog", name="Registrations", exact=True)).to_be_visible()
    assert len(api.queued) == 1
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
    expect(button.locator('[aria-label="2 unread registration updates"]')).to_be_visible()
    needs_input = page.get_by_role("button", name="View registration for Friday Night Jazz", exact=True)
    expect(needs_input).to_have_attribute("data-registration-state", "needs_input")
    assert needs_input.locator(".event-muse-action__badge").evaluate("element => getComputedStyle(element).color") == "rgb(230, 180, 92)"
    assert api.acknowledged == []
    button.click()
    dialog = page.get_by_role("dialog", name="Registrations", exact=True)
    expect(dialog).to_contain_text("Needs your input")
    expect(dialog).to_contain_text("Needs verification")
    expect(dialog).to_contain_text("Login needed in Muse")
    expect(dialog.locator('[aria-label="Unread update"]')).to_have_count(0)
    expect(button.locator('[aria-label$="unread registration updates"]')).to_have_count(0)
    assert {item["version"] for item in api.acknowledged} == {2, 3}
    assert not any(item["unread"] for item in api.registrations)
    # A subsequent connector report is an update, even if a prior version was viewed.
    api.registrations[0].update(status="registered", version=3, unread=True,
                                outcome={"status": "registered", "note": "Confirmed by provider",
                                         "confirmation_reference": "fixture-confirmation",
                                         "evidence_url": "https://lu.ma/friday-jazz"})
    dialog.get_by_role("button", name="Refresh registrations").click()
    expect(dialog.locator('[data-status="registered"]')).to_have_text("Registered · reported by Muse")
    expect(dialog).to_contain_text("fixture-confirmation")
    expect(dialog.get_by_role("link", name=re.compile("evidence", re.I))).to_have_attribute("href", "https://lu.ma/friday-jazz")
    expect(dialog.locator('[aria-label="Unread update"]')).to_have_count(0)
    assert api.acknowledged[-1] == {"event_id": api.events[0]["canonical_event_id"], "version": 3}
    dialog.get_by_role("button", name="Close registrations", exact=True).click()
    registered = page.get_by_role("button", name="View registration for Friday Night Jazz", exact=True)
    expect(registered).to_have_attribute("data-registration-state", "registered")
    expect(registered.locator(".sr-only")).to_have_text("Registered · reported by Muse")
    expect(registered.locator(".event-muse-action__badge")).to_be_visible()
    assert registered.locator(".event-muse-action__badge").evaluate("element => getComputedStyle(element).color") == "rgb(151, 217, 163)"
    registered.click()
    expect(dialog).to_contain_text("fixture-confirmation")
    assert api.queued == []
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


@pytest.mark.parametrize("width", [1440, 320])
def test_guest_has_no_muse_controls_or_automatic_queue_after_sign_in(page_factory, width):
    harness, api = install(page_factory, width)
    api.signed_in = False
    page = harness.page
    return_path = "/?view=events&when=all&city="
    page.goto(BASE + return_path)
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("Muse for"))).to_have_count(0)
    expect(page.get_by_role("button", name="Registrations", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Documentation", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="Map", exact=True).click()
    expect(page.get_by_role("heading", name="Event map", exact=True)).to_be_visible()
    expect(page.locator(".event-muse-action")).to_have_count(0)
    page.get_by_role("button", name="Calendar", exact=True).click()
    expect(page.locator(".calendar-agenda")).to_be_visible()
    expect(page.locator(".event-muse-action")).to_have_count(0)
    assert api.muse_calls == [] and api.writes == []
    api.signed_in = True
    page.goto(BASE + return_path)
    expect(page.get_by_role("button", name="Sign up with Muse for Friday Night Jazz")).to_be_visible()
    assert api.queued == []
    page.get_by_role("button", name="Sign up with Muse for Friday Night Jazz").click()
    expect(page.get_by_role("button", name="View registration for Friday Night Jazz")).to_be_visible()
    assert len(api.queued) == 1
    assert api.unexpected == []


def test_older_registrations_remain_accessible_beyond_first_page(page_factory):
    harness, api = install(page_factory)
    api.registrations = [api.registration(api.events[0] | {
        "canonical_event_id": f"{index:08x}-1111-4111-8111-111111111111", "title": f"Queued event {index}"
    }) for index in reversed(range(55))]
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=&registrations=1")
    dialog = page.get_by_role("dialog", name="Registrations", exact=True)
    expect(dialog).to_contain_text("Queued event 54")
    expect(dialog).not_to_contain_text("Queued event 0")
    dialog.get_by_role("button", name="Load more registrations").click()
    expect(dialog).to_contain_text("Queued event 0")
    expect(dialog.get_by_role("button", name="Load more registrations")).to_have_count(0)
    dialog.locator("li").filter(has=page.get_by_role("link", name="Queued event 54", exact=True)).scroll_into_view_if_needed()
    older = api.registrations[-1]
    older.update(status="needs_input", version=2, unread=True,
                 outcome={"status": "needs_input", "note": "Answer needed for older event",
                          "confirmation_reference": None, "evidence_url": None})
    dialog.get_by_role("button", name="Refresh registrations").click()
    expect(dialog).to_contain_text("Answer needed for older event")
    assert older["unread"]  # Loading an offscreen result does not mark it as viewed.
    old_card = dialog.locator("li").filter(has=page.get_by_role("link", name="Queued event 0", exact=True))
    old_card.scroll_into_view_if_needed()
    expect(old_card.locator('[aria-label="Unread update"]')).to_have_count(0)
    expect(page.get_by_role("button", name="Registrations", exact=True).locator('[aria-label$="unread registration updates"]')).to_have_count(0)
    assert not older["unread"]
    older.update(status="registered", version=3, unread=True,
                 outcome={"status": "registered", "note": "Older event confirmed",
                          "confirmation_reference": "older-confirmation",
                          "evidence_url": "https://lu.ma/friday-jazz"})
    dialog.get_by_role("button", name="Refresh registrations").click()
    expect(old_card).to_contain_text("older-confirmation")
    old_card.scroll_into_view_if_needed()
    expect(old_card.locator('[aria-label="Unread update"]')).to_have_count(0)
    expect(page.get_by_role("button", name="Registrations", exact=True).locator('[aria-label$="unread registration updates"]')).to_have_count(0)
    assert api.acknowledged[-1] == {"event_id": older["event"]["canonical_event_id"], "version": 3}
    assert api.unexpected == []


@pytest.mark.parametrize("width", [1440, 320])
def test_settings_secret_is_one_time_with_instructions_in_docs(page_factory, width):
    harness, api = install(page_factory, width)
    page = harness.page
    page.goto(BASE + "/settings/registrations")
    expect(page.get_by_role("heading", name="Registrations", exact=True)).to_be_visible()
    expect(page.locator('.settings-panel > [role="status"]')).to_have_class("sr-only")
    selector = page.get_by_role("combobox", name="Provider", exact=True)
    expect(selector).to_have_value("muse")
    assert selector.locator("option").all_text_contents() == ["Muse"]
    expect(page.get_by_role("textbox", name="Connector URL", exact=True)).to_have_value(BASE + "/v1/muse/openapi.json")
    page.get_by_role("button", name="Create key", exact=True).click()
    secret = page.get_by_label("Muse connection key", exact=True)
    expect(secret).to_have_attribute("type", "password")
    expect(page.get_by_text("Shown once", exact=True)).to_be_visible()
    assert secret.input_value() == FIXTURE_KEY
    assert FIXTURE_KEY not in page.evaluate("JSON.stringify([localStorage, sessionStorage])")
    copy_key = page.get_by_role("button", name="Copy connection key", exact=True)
    bounds = (secret.bounding_box(), copy_key.bounding_box())
    copy_key.click()
    expect(page.locator('.settings-panel > [role="status"]')).to_contain_text("Key copied.")
    expect(page.locator('.settings-panel > [role="status"]')).to_have_class("sr-only")
    assert (secret.bounding_box(), copy_key.bounding_box()) == bounds
    assert page.evaluate("window.__fixtureCopied") == FIXTURE_KEY
    expect(page.get_by_text("Active", exact=True)).to_be_visible()
    expect(page.get_by_text("Expires", exact=True)).to_be_visible()
    expect(page.get_by_role("navigation", name="Muse links").get_by_role("link", name="Registrations", exact=True)).to_be_visible()
    page.get_by_role("button", name="Hide key", exact=True).click()
    expect(secret).not_to_be_visible()
    page.get_by_role("button", name="Revoke key", exact=True).click()
    expect(page.locator('.settings-panel > [role="status"]')).to_contain_text("Key revoked.")
    page.reload()
    expect(page.get_by_text("No active key", exact=True)).to_be_visible()
    instructions = page.get_by_role("textbox", name="Muse signup instruction")
    expect(instructions).not_to_be_visible()
    expect(page.get_by_role("button", name="Copy Muse instruction")).not_to_be_visible()
    expect(page.locator("details")).to_have_count(0)
    page.get_by_role("link", name="Setup guide", exact=True).click()
    expect(instructions).to_be_visible()
    expect(page.get_by_text("Opening Muse does not start registrations.", exact=False)).to_be_visible()
    page.get_by_role("button", name="Copy Muse instruction").click()
    expect(page.get_by_role("status")).to_contain_text("Instruction copied.")
    instruction = page.evaluate("window.__fixtureCopied")
    assert instruction == instructions.input_value()
    assert "queued" in instruction.lower() and FIXTURE_KEY not in instruction
    page.evaluate("() => { navigator.clipboard.writeText = async () => { throw new Error('clipboard disabled'); }; }")
    page.get_by_role("button", name="Copy Muse instruction").click()
    expect(page.get_by_role("main").get_by_role("alert")).to_contain_text("Select and copy the instruction below.")
    expect(instructions).to_be_visible()
    assert [item[:2] for item in api.writes] == [("POST", "/v1/me/muse/connection"),
                                                ("DELETE", "/v1/me/muse/connection")]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert api.unexpected == []


@pytest.mark.parametrize("width", [1440, 320])
def test_settings_help_supports_hover_keyboard_touch_and_copy_without_writes(page_factory, width):
    harness, api = install(page_factory, width)
    page = harness.page
    page.goto(BASE + "/settings/registrations")
    expect(page.get_by_role("heading", name="Registrations", exact=True)).to_be_visible()
    connector = page.get_by_role("textbox", name="Connector URL", exact=True)
    expect(connector).to_have_value(BASE + "/v1/muse/openapi.json")
    expect(page.get_by_role("tooltip")).to_have_count(0)
    help_button = page.get_by_role("button", name="Connector URL help", exact=True)
    help_button.hover()
    tooltip = page.get_by_role("tooltip")
    expect(tooltip).to_contain_text("public HTTPS")
    assert help_button.evaluate("element => element !== document.activeElement")
    page.keyboard.press("Escape")
    expect(tooltip).to_have_count(0)
    help_button.focus()
    expect(tooltip).to_be_visible()
    expect(help_button).to_have_attribute("aria-describedby", tooltip.get_attribute("id"))
    help_button.press("Escape")
    expect(tooltip).to_have_count(0)
    help_button.click()
    expect(tooltip).to_be_visible()
    assert tooltip.evaluate("element => { const box = element.getBoundingClientRect(); return box.left >= 0 && box.right <= innerWidth; }")
    page.get_by_role("button", name="Copy URL", exact=True).click()
    expect(tooltip).to_have_count(0)
    expect(page.locator('.settings-panel > [role="status"]')).to_contain_text("URL copied.")
    expect(page.locator('.settings-panel > [role="status"]')).to_have_class("sr-only")
    assert page.evaluate("window.__fixtureCopied") == connector.input_value()
    assert api.writes == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_registration_settings_preserve_bookmarks_and_sign_in_destination(page_factory):
    harness, api = install(page_factory)
    page = harness.page
    page.goto(BASE + "/settings/muse")
    expect(page).to_have_url(BASE + "/settings/registrations")
    expect(page.get_by_role("heading", name="Registrations", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="Provider", exact=True)).to_have_value("muse")
    api.muse_calls.clear()
    api.signed_in = False
    page.reload()
    expect(page).to_have_url(BASE + "/sign-in?return_to=%2Fsettings%2Fregistrations")
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_visible()
    assert api.muse_calls == []
    assert api.writes == []
    assert api.unexpected == []


def assert_title_action_geometry(row):
    expect(row.locator(".event-muse-action")).to_be_visible()
    dimensions = row.evaluate("""row => {
        const text = row.querySelector('.event-title-text');
        const button = row.querySelector('.event-muse-action');
        const icon = button.querySelector('svg');
        const title = text.getBoundingClientRect(), action = button.getBoundingClientRect();
        const glyph = icon.getBoundingClientRect(), style = getComputedStyle(text);
        return { font: parseFloat(style.fontSize), line: parseFloat(style.lineHeight),
                 titleTop: title.top, titleRight: title.right, actionLeft: action.left,
                 actionCenter: action.top + action.height / 2, width: action.width,
                 height: action.height, iconSize: glyph.height };
    }""")
    assert abs(dimensions["iconSize"] - dimensions["font"]) < 0.5
    # Rectangles may lose a fraction of a pixel when their coordinates are
    # subtracted (for example, a 24px control reads as 23.99994px in Chromium).
    minimum_size = min(dimensions["width"], dimensions["height"])
    assert minimum_size >= 24 or minimum_size == pytest.approx(24, abs=0.001)
    assert 0 <= dimensions["actionLeft"] - dimensions["titleRight"] <= dimensions["font"]
    assert abs(dimensions["actionCenter"] - dimensions["titleTop"] - dimensions["line"] / 2) < 1.5


@pytest.mark.parametrize("width", [1440, 320])
def test_source_view_and_map_calendar_card_actions_use_shared_controls(page_factory, width):
    harness, api = install(page_factory, width)
    key = "fixture-techweek"
    api.providers = [{"source_key": key, "label": "SF Tech Week 2026", "display_name": "SF Tech Week 2026",
                      "publisher": "Tech Week", "provider": "luma", "seed_url": "https://www.tech-week.com/", "event_count": 2}]
    api.events = [event | {"source_keys": [key], "calendar_labels": ["SF Tech Week 2026"],
                           "latitude": 37.7749, "longitude": -122.4194,
                           "sources": [{"source": "public_jsonld", "source_key": key, "label": "SF Tech Week 2026",
                                        "registration_url": event["registration_urls"][0],
                                        "last_seen_at": event["sources"][0]["last_seen_at"]}]} for event in api.events]
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=")
    first_card = page.locator("article").filter(has=page.get_by_role("heading", name="Friday Night Jazz", exact=True))
    expect(first_card.locator(".event-card__title-row").get_by_role("button", name="Sign up with Muse for Friday Night Jazz")).to_be_visible()
    assert_title_action_geometry(first_card.locator(".event-title-row"))
    first_card.get_by_role("button", name="View source SF Tech Week 2026", exact=True).click()
    expect(page.get_by_role("heading", name="SF Tech Week 2026", exact=True)).to_be_visible()
    assert "view=source" in page.url and "source=" + key in page.url
    expect(page.get_by_text("Events in range", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Website")).to_have_attribute("href", "https://www.tech-week.com/")
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(api.catalog_reads[-1]).query)["source_key"] == [key]
    page.get_by_role("button", name="Back", exact=True).click()
    expect(page.get_by_role("heading", name="Events", exact=True)).to_be_visible()
    assert "view=events" in page.url and "when=all" in page.url
    first_card.get_by_role("button", name="View source SF Tech Week 2026", exact=True).click()
    expect(page.get_by_role("heading", name="SF Tech Week 2026", exact=True)).to_be_visible()
    page.get_by_role("button", name="Map", exact=True).last.click()
    expect(page.get_by_role("heading", name="Event map", exact=True)).to_be_visible()
    mini = page.locator(".map-preview").filter(has=page.get_by_role("link", name="Friday Night Jazz", exact=True))
    date_link = mini.locator(".map-preview__date-link")
    expect(date_link).to_have_attribute("href", re.compile("^https://calendar.google.com/calendar/render"))
    expect(mini.locator(".map-preview__calendar-link")).to_have_attribute("href", date_link.get_attribute("href"))
    expect(mini.get_by_role("button", name="Sign up with Muse for Friday Night Jazz", exact=True)).to_be_visible()
    assert_title_action_geometry(mini.locator(".event-title-row"))
    expect(mini.get_by_role("button", name="View source SF Tech Week 2026", exact=True)).to_be_visible()
    mini.get_by_role("button", name="View source SF Tech Week 2026", exact=True).click()
    expect(page.get_by_role("heading", name="SF Tech Week 2026", exact=True)).to_be_visible()
    page.get_by_role("button", name="Calendar", exact=True).last.click()
    expect(page.get_by_role("button", name="Sign up with Muse for Friday Night Jazz", exact=True)).to_be_visible()
    assert_title_action_geometry(page.locator(".event-title-row").filter(has=page.get_by_role("button", name="Sign up with Muse for Friday Night Jazz", exact=True)))
    assert "source=" + key in page.url
    api.events[0]["title"] = "Friday Night Jazz with a very long title that wraps across several lines on a narrow screen"
    page.goto(BASE + "/?view=events&when=all&city=")
    long_card = page.locator("article").filter(has=page.get_by_role("heading", name=api.events[0]["title"], exact=True))
    assert_title_action_geometry(long_card.locator(".event-title-row"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert api.queued == [] and api.unexpected == []


@pytest.mark.parametrize("width", [1440, 320])
def test_map_muse_hint_remains_readable_at_rail_edge(page_factory, width, tmp_path):
    harness, api = install(page_factory, width)
    page = harness.page
    api.events[0]["price_status"] = "unknown"
    api.events[0]["registration_urls"] = ["https://events.example.test/friday-jazz"]
    api.events[0]["sources"][0]["registration_url"] = api.events[0]["registration_urls"][0]
    page.goto(BASE + "/?view=map&when=all&city=")
    blocked_mini = page.locator(".map-preview").filter(has=page.get_by_role("link", name=api.events[0]["title"], exact=True))
    blocked_action = blocked_mini.get_by_role("button", name=f"Muse unavailable for {api.events[0]['title']}", exact=True)
    blocked_action.hover()
    tooltip = page.get_by_role("tooltip")
    expect(tooltip).to_contain_text(re.compile("Free admission.*confirmed"))
    expect(tooltip).to_contain_text("Luma and Meetup")
    assert tooltip.evaluate("""element => {
        const box = element.getBoundingClientRect();
        return box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight
            && [box.left + 2, box.right - 2].every(x => element.contains(document.elementFromPoint(x, box.top + box.height / 2)));
    }""")
    page.screenshot(path=str(tmp_path / f"muse-unavailable-map-edge-{width}.png"))
    page.keyboard.press("Escape")
    expect(tooltip).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert api.queued == [] and api.unexpected == []


@pytest.mark.parametrize("width", [1440, 320])
@pytest.mark.parametrize("signed_in", [True, False], ids=["signed-in", "guest"])
def test_graph_event_actions_keep_selection_and_require_sign_in(page_factory, width, signed_in):
    harness, api = install(page_factory, width)
    api.signed_in = signed_in
    page = harness.page
    page.goto(BASE + f"/?view=entities&entity={ENTITY_ID}&when=custom&start=2030-06-01&end=2030-06-30")
    event_id = api.events[0]["canonical_event_id"]
    page.locator(f'button[data-node-id="event:{event_id}"]').click()
    card = page.get_by_role("complementary", name="Event detail", exact=True).locator(".event-card")
    expect(card.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    action = card.get_by_role("button", name="Sign up with Muse for Friday Night Jazz", exact=True)
    if not signed_in:
        expect(action).to_have_count(0)
        assert api.muse_calls == [] and api.writes == []
    else:
        assert_title_action_geometry(card.locator(".event-title-row"))
        expect(card.get_by_role("link", name=re.compile("^Add Friday Night Jazz .* to Google Calendar$"))).to_have_attribute(
            "href", re.compile("^https://calendar.google.com/calendar/render")
        )
        action.click()
        dialog = page.get_by_role("dialog", name="Registrations", exact=True)
        expect(dialog).to_contain_text("Friday Night Jazz")
        expect(dialog.get_by_role("link", name="Muse settings", exact=True)).to_have_attribute("href", "/settings/registrations")
        dialog.get_by_role("button", name="Close registrations", exact=True).click()
        saved = card.get_by_role("button", name="View registration for Friday Night Jazz", exact=True)
        expect(saved).to_have_attribute("data-registration-state", "queued")
        saved.click()
        expect(dialog).to_be_visible()
        assert len(api.queued) == 1
        assert parse_qs(urlsplit(page.url).query)["entity"] == [ENTITY_ID]
        assert parse_qs(urlsplit(page.url).query)["view"] == ["entities"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert api.unexpected == []


def test_registration_summary_counts_full_queue_and_filter_keeps_unseen_tasks_unread(page_factory):
    harness, api = install(page_factory)
    api.registrations = [api.registration(api.events[0] | {
        "canonical_event_id": f"{index:08x}-1111-4111-8111-111111111111", "title": f"Queued event {index}"
    }, "registered" if index < 5 else "queued", unread=True) for index in reversed(range(55))]
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=")
    widget = page.get_by_role("button", name="Registrations", exact=True)
    expect(widget).to_have_attribute("title", "50 queued, 0 in progress, 5 registered, 0 needs attention")
    widget.click()
    dialog = page.get_by_role("dialog", name="Registrations", exact=True)
    expect(dialog.get_by_role("combobox", name="Registration provider", exact=True)).to_have_value("muse")
    dialog.get_by_role("button", name=re.compile("^Registered")).click()
    expect(dialog).to_contain_text("More registrations available.")
    assert all(item["unread"] for item in api.registrations[-5:])
    dialog.get_by_role("button", name="Load more registrations").click()
    expect(dialog).to_contain_text("Queued event 0")
    assert api.unexpected == []


def test_visible_past_event_opens_existing_registration_beyond_first_queue_page(page_factory):
    harness, api = install(page_factory)
    api.events[0]["start_at"] = "2020-06-14T19:30:00-07:00"
    older = api.registration(api.events[0], "needs_input", version=3, unread=True)
    older["outcome"] = {"status": "needs_input", "note": "Provider needs a profile response"}
    api.registrations = [api.registration(api.events[1] | {
        "canonical_event_id": f"{index:08x}-1111-4111-8111-111111111111", "title": f"Recent event {index}"
    }) for index in reversed(range(50))] + [older]
    page = harness.page
    page.goto(BASE + "/?view=events&when=source&source=fixture-jazz&city=")
    control = page.get_by_role("button", name="View registration for Friday Night Jazz", exact=True)
    expect(control).to_be_enabled()
    control.click()
    dialog = page.get_by_role("dialog", name="Registrations", exact=True)
    expect(dialog).to_contain_text("Provider needs a profile response")
    assert any(path == "/v1/me/muse/registrations/by-event" for _, path in api.muse_calls)
    assert api.queued == [] and not any(method == "POST" and path == "/v1/me/muse/registrations" for method, path, _ in api.writes)

    # The panel must refresh this cached task even after its catalog card unmounts.
    dialog.get_by_role("button", name="Close registrations", exact=True).click()
    api.events = [api.events[1]]
    page.get_by_role("button", name="Map", exact=True).click()
    expect(page.get_by_role("heading", name="Event map", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="View registration for Friday Night Jazz", exact=True)).not_to_be_visible()
    older["version"] = 4
    older["status"] = "registered"
    older["outcome"] = {"status": "registered", "note": "Provider confirmed this past registration",
                        "confirmation_reference": "older-rsvp", "evidence_url": "https://lu.ma/friday-jazz"}
    older["unread"] = True
    page.get_by_role("button", name="Registrations", exact=True).click()
    expect(dialog).to_contain_text("older-rsvp")
    assert api.queued == []
    assert api.unexpected == []


@pytest.mark.parametrize("width", [1440, 320])
def test_registration_account_is_independent_saved_and_clearable(page_factory, width, tmp_path):
    harness, api = install(page_factory, width)
    page = harness.page
    page.goto(BASE + "/settings/registrations")
    form = page.get_by_role("form", name="Registration account", exact=True)
    email = form.get_by_role("textbox", name="Default email", exact=True)
    expect(email).to_be_enabled()
    expect(email).to_have_value("")
    expect(email).to_have_attribute("placeholder", "Current browser account")
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    assert api.account_saves == []
    form.get_by_role("button", name="Registration account help", exact=True).focus()
    expect(page.get_by_role("tooltip")).to_contain_text("Can differ from your Events Concierge sign-in")
    page.keyboard.press("Escape")
    email.fill("Provider+events@EXAMPLE.TEST")
    form.get_by_role("button", name="Per site", exact=True).click()
    form.get_by_role("textbox", name="Luma email", exact=True).fill("luma@example.test")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".settings-actions").get_by_text("Saved.", exact=True)).to_be_visible()
    assert api.account_saves == [{"expected_email": "Provider+events@example.test", "provider_overrides": {"luma": "luma@example.test"}, "revision": 1}]
    account_write = next(item for item in api.writes if item[1] == "/v1/me/registration-accounts/muse")
    assert account_write[2]["x-fixture-csrf"] == "bound-fixture"
    assert "authorization" not in account_write[2]
    expect(page.locator(".settings-header__who")).to_have_text("browser@example.test")
    expect(page.get_by_role("link", name="Account guide", exact=True)).to_have_attribute("href", "/docs/muse#account")
    page.reload()
    expect(email).to_have_value("Provider+events@example.test")
    expect(form.get_by_role("textbox", name="Luma email", exact=True)).to_have_value("luma@example.test")
    page.screenshot(path=str(tmp_path / f"registration-account-{width}.png"))
    email.fill("")
    form.get_by_role("textbox", name="Luma email", exact=True).fill("")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".settings-actions").get_by_text("Saved.", exact=True)).to_be_visible()
    assert api.account_saves[-1] == {"expected_email": None, "provider_overrides": {}, "revision": 2}
    page.reload()
    expect(email).to_have_value("")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert api.unexpected == []


def test_registration_account_validation_and_failed_save_keep_the_draft(page_factory):
    harness, api = install(page_factory)
    page = harness.page
    page.goto(BASE + "/settings/registrations")
    form = page.get_by_role("form", name="Registration account", exact=True)
    email = form.get_by_role("textbox", name="Default email", exact=True)
    expect(email).to_be_enabled()
    email.fill("person@example")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(form.get_by_role("alert")).to_contain_text("Enter a valid email address.")
    assert api.account_saves == []
    email.fill("provider@example.test")
    api.account_error = 503
    page.get_by_role("button", name="Save", exact=True).click()
    expect(form.get_by_role("alert")).to_contain_text("Account settings unavailable")
    expect(email).to_have_value("provider@example.test")
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()
    assert api.registration_account["expected_email"] is None
    api.account_error = None
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".settings-actions").get_by_text("Saved.", exact=True)).to_be_visible()
    assert len(api.account_saves) == 2
    assert api.account_saves[0] == api.account_saves[1]
    assert api.unexpected == []


def test_registration_account_conflict_requires_reload_and_preserves_unsaved_identity(page_factory):
    harness, api = install(page_factory)
    page = harness.page
    page.goto(BASE + "/settings/registrations")
    form = page.get_by_role("form", name="Registration account", exact=True)
    email = form.get_by_role("textbox", name="Default email", exact=True)
    expect(email).to_be_enabled()
    email.fill("draft@example.test")
    api.registration_account = api.registration_account | {"expected_email": "other-tab@example.test", "revision": 1}
    page.get_by_role("button", name="Save", exact=True).click()
    expect(form.get_by_role("alert")).to_contain_text("Settings changed. Reload and review before saving.")
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    expect(email).to_have_value("draft@example.test")
    assert len(api.account_saves) == 1
    page.get_by_role("button", name="Reload", exact=True).click()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_enabled()
    expect(email).to_have_value("draft@example.test")
    assert len(api.account_saves) == 1
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".settings-actions").get_by_text("Saved.", exact=True)).to_be_visible()
    assert api.account_saves[-1] == {"expected_email": "draft@example.test", "provider_overrides": {}, "revision": 2}
    assert api.unexpected == []


def test_registration_drawer_uses_the_attempt_account_snapshot(page_factory):
    harness, api = install(page_factory)
    event = api.events[0]
    api.registration_account = api.registration_account | {"expected_email": "new-setting@example.test", "revision": 1}
    api.registrations = [api.registration(event, status="in_progress") | {
        "registration_account": {"executor": "muse", "provider": "luma", "expected_email": "claimed@example.test"}
    }]
    page = harness.page
    page.goto(BASE + "/?view=events&when=all&city=")
    page.get_by_role("button", name="Registrations", exact=True).click()
    status = page.get_by_role("dialog").locator('[data-status="in_progress"]')
    expect(status).to_have_attribute("title", "Registration account: claimed@example.test")
    assert api.account_saves == []
