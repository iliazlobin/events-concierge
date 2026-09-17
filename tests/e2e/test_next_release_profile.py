"""Exercise runtime release profiles in the built Next.js consumer, using isolated API fixtures."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect
from tests.e2e.test_consumer_ui import _TRANSPARENT_MAP_TILE, _feed

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(not BASE, reason="EC_CONSUMER_WEB_URL or EC_ADMIN_WEB_URL is not set"),
]


@dataclass
class ReleaseApi:
    profile: str | None = "discovery"
    local_demo: bool = True
    config_status: int = 200
    hold_config: bool = False
    held: list[Route] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    def config(self, route: Route) -> None:
        payload = {
            "product_name": "Events Concierge",
            "local_demo": self.local_demo,
            "auth_mode": "local_demo" if self.local_demo else "deployment_session",
            "auth_start_url": None,
            "reauth_url": None,
            "logout_url": None,
            "csrf_cookie_name": None,
            "csrf_header_name": None,
        }
        if self.profile is not None:
            payload["release_profile"] = self.profile
        self.respond(route, payload if self.config_status == 200 else {"detail": "Config unavailable"}, self.config_status)

    @staticmethod
    def respond(route: Route, payload, status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    def install(self, page: Page) -> None:
        page.route("**/v1/**", self.handle)
        page.route("https://tile.openstreetmap.org/**", lambda route: route.fulfill(content_type="image/png", body=_TRANSPARENT_MAP_TILE))

    def handle(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        self.calls.append((route.request.method, path))
        if path == "/v1/ui-config":
            if self.hold_config:
                self.held.append(route)
            else:
                self.config(route)
        elif path == "/v1/me":
            self.respond(route, {
                "notify_email": "browser@example.test", "interests": [],
                "preference_revision": 1, "local_demo": True, "is_admin": False,
                "relay_inbox": "fixture@relay.example.test",
            })
        elif path in {"/v1/me/saved-filters", "/v1/me/api-keys"}:
            self.respond(route, [])
        elif path == "/v1/catalog/events":
            event = _feed()["items"][0] | {
                "organizer_name": "Lakehouse Music", "topics": ["jazz"],
                "source_keys": ["fixture-jazz"], "providers": ["Fixture Jazz"],
                "sources": [{"source_key": "fixture-jazz", "source": "public_jsonld", "label": "Fixture Jazz", "registration_url": "https://events.example.test/friday-jazz"}],
            }
            self.respond(route, {
                "items": [event], "next_cursor": None,
                "providers": [{"source_key": "fixture-jazz", "label": "Fixture Jazz", "display_name": "Fixture Jazz", "publisher": "Fixture Jazz", "provider": "public_jsonld", "seed_url": "https://events.example.test", "event_count": 1}],
                "topic_facets": [{"topic": "jazz", "label": "Jazz", "event_count": 1}],
                "city_facets": [{"city": "Oakland", "event_count": 1}],
            })
        elif path == "/v1/catalog/events/summary":
            self.respond(route, {"days": [{"start_day": "2030-06-14", "event_count": 1, "topics": [{"topic": "jazz", "label": "Jazz", "event_count": 1}]}], "total_event_count": 1, "time_zone": "America/Los_Angeles"})
        else:
            self.unexpected.append(path)
            self.respond(route, {"detail": f"Unexpected fixture request: {path}"}, 500)


@pytest.fixture
def release_page(page_factory):
    harness = page_factory(authenticated=True)
    api = ReleaseApi()
    api.install(harness.page)
    yield harness, api
    assert not api.unexpected
    assert all(method == "GET" for method, _ in api.calls)


@pytest.mark.parametrize("view", ["chat", "entities"])
def test_discovery_rejects_deep_links_and_history_but_keeps_provider_actions(release_page, view):
    harness, api = release_page
    page = harness.page
    page.goto(f"{BASE}/?view={view}&entity=hidden&release_profile=full")
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Chat", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Entities", exact=True)).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query)["view"] == ["events"]
    assert "entity" not in parse_qs(urlsplit(page.url).query)
    page.get_by_role("button", name="Show details for Friday Night Jazz", exact=True).click()
    expect(page.get_by_role("button", name=re.compile(r"^Explore organizer"))).to_have_count(0)
    expect(page.locator('a[href="https://events.example.test/friday-jazz"]')).to_be_visible()
    expect(page.get_by_role("link", name=re.compile(r"^Add Friday Night Jazz .* to Google Calendar$"))).to_be_visible()
    page.get_by_role("button", name="Add jazz topic filter", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]topic=jazz(?:&|$)"))
    assert parse_qs(urlsplit(page.url).query)["view"] == ["events"]
    page.evaluate("""() => {
        const state = structuredClone(history.state);
        state.eventsConciergeConsumer.view = 'chat';
        state.eventsConciergeConsumer.selectedEntityId = 'hidden';
        history.pushState(state, '', '?view=chat');
        dispatchEvent(new PopStateEvent('popstate', {state}));
    }""")
    expect(page).to_have_url(re.compile(r"[?&]view=events(?:&|$)"))
    expect(page.locator(".chat-view")).to_have_count(0)
    page.get_by_role("button", name="Events Concierge", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]view=events(?:&|$)"))
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator(".mobile-nav button")).to_have_count(3)
    assert all("agent" not in path and "entities" not in path for _, path in api.calls)


@pytest.mark.parametrize("profile", ["full", None])
def test_full_and_legacy_runtime_configs_preserve_chat_and_entity_navigation(release_page, profile):
    harness, api = release_page
    api.profile = profile
    page = harness.page
    page.goto(f"{BASE}/")
    expect(page.locator(".chat-view")).to_be_visible()
    expect(page.locator(".site-nav").get_by_role("button", name="Chat", exact=True)).to_be_visible()
    expect(page.locator(".site-nav").get_by_role("button", name="Entities", exact=True)).to_be_visible()
    page.locator(".site-nav").get_by_role("button", name="Events", exact=True).click()
    page.get_by_role("button", name="Show details for Friday Night Jazz", exact=True).click()
    expect(page.get_by_role("button", name="Explore organizer Lakehouse Music", exact=True)).to_be_visible()
    page.get_by_role("button", name="Events Concierge", exact=True).click()
    expect(page.locator(".chat-view")).to_be_visible()


def test_unknown_runtime_profile_fails_closed(release_page):
    harness, api = release_page
    api.profile = "future-profile"
    harness.page.goto(f"{BASE}/?view=chat")
    expect(harness.page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(harness.page.get_by_role("button", name="Chat", exact=True)).to_have_count(0)


def test_unresolved_config_never_renders_deferred_navigation(release_page):
    harness, api = release_page
    api.hold_config = True
    page = harness.page
    page.goto(f"{BASE}/?view=chat")
    expect(page.locator(".app-loading")).to_be_visible()
    expect(page.get_by_role("navigation")).to_have_count(0)
    expect(page.get_by_role("button", name="Continue", exact=True)).to_have_count(0)
    assert api.calls == [("GET", "/v1/ui-config")]
    assert api.held
    api.config(api.held.pop())
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()


def test_config_failure_can_retry_without_exposing_onboarding_or_chat(release_page):
    harness, api = release_page
    api.config_status = 503
    harness.allowed_console_error_fragments.append("503")
    page = harness.page
    page.goto(f"{BASE}/?view=chat")
    expect(page.get_by_role("main").get_by_role("alert")).to_contain_text("Sign-in is temporarily unavailable. Please try again.")
    expect(page.get_by_role("button", name="Continue", exact=True)).to_have_count(0)
    expect(page.get_by_role("navigation")).to_have_count(0)
    assert api.calls == [("GET", "/v1/ui-config")]
    api.config_status = 200
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()


def test_discovery_settings_suppress_automated_registration_and_calendar_claims(release_page):
    harness, api = release_page
    page = harness.page
    page.goto(f"{BASE}/settings/account")
    expect(page.get_by_role("heading", name="Event discovery", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Activity", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Security", exact=True)).to_have_count(0)
    expect(page.get_by_text("Find and handle it.", exact=False)).to_have_count(0)
    expect(page.get_by_text("Your calendar stays true.", exact=False)).to_have_count(0)
    page.goto(f"{BASE}/settings/activity")
    expect(page.get_by_role("heading", name="Event discovery", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Withdraw", exact=True)).to_have_count(0)
    assert not any(path in {"/v1/me/requests", "/v1/me/registrations", "/v1/me/tasks"} for _, path in api.calls)
    page.goto(f"{BASE}/settings")
    expect(page.get_by_role("heading", name="Profile", exact=True)).to_be_visible()
    expect(page.get_by_text("Relay inbox", exact=True)).to_have_count(0)
    expect(page.get_by_text("sends confirmations", exact=False)).to_have_count(0)
    page.goto(f"{BASE}/settings/taste")
    expect(page.get_by_text("Use topic filters", exact=False)).to_be_visible()
    expect(page.get_by_text("keeps refining the order", exact=False)).to_have_count(0)
    page.goto(f"{BASE}/settings/security")
    expect(page.get_by_role("heading", name="Account security", exact=True)).to_be_visible()
    expect(page.locator(".preview-banner")).to_have_count(0)
    expect(page.get_by_role("button")).to_have_count(0)


def test_full_profile_retains_explicitly_inert_security_preview(release_page):
    harness, api = release_page
    api.profile = "full"
    page = harness.page
    page.goto(f"{BASE}/settings/security")
    expect(page.get_by_role("heading", name="Security", exact=True)).to_be_visible()
    expect(page.get_by_role("note")).to_contain_text("Design preview")
    expect(page.get_by_role("link", name="Security", exact=True)).to_be_visible()
    buttons = page.get_by_role("button")
    assert buttons.count() > 0
    for button in buttons.all():
        expect(button).to_be_disabled()


@pytest.mark.parametrize("profile", ["discovery", "full"])
def test_profiles_keep_map_and_calendar_browsing(release_page, profile):
    harness, api = release_page
    api.profile = profile
    page = harness.page
    page.goto(f"{BASE}/?view=map&when=custom&start=2030-06-01&end=2030-06-30")
    expect(page.get_by_role("button", name="Focus Friday Night Jazz on map", exact=True)).to_be_visible()
    graph = page.get_by_role("button", name="View Friday Night Jazz in Lakehouse Music's graph", exact=True)
    if profile == "discovery":
        expect(graph).to_have_count(0)
    else:
        expect(graph).to_be_visible()
    expect(page.get_by_role("link", name="View Friday Night Jazz event page (opens in new tab)", exact=True)).to_be_visible()
    page.locator(".site-nav").get_by_role("button", name="Calendar", exact=True).click()
    expect(page.get_by_role("grid", name="June 2030", exact=True)).to_be_visible()
    expect(page.get_by_role("gridcell", name=re.compile(r"June 14.*1 event"))).to_be_visible()
    assert ("GET", "/v1/catalog/events/summary") in api.calls


@pytest.mark.parametrize("local_demo", [True, False])
def test_profile_avatar_reads_use_the_current_auth_contract(release_page, local_demo):
    harness, api = release_page
    api.local_demo = local_demo
    page = harness.page
    avatar_reads = []

    def identity(route):
        route.fulfill(content_type="application/json", body=json.dumps({
            "notify_email": "browser@example.test", "interests": [],
            "preference_revision": 1, "local_demo": local_demo, "is_admin": False,
            "profile": {"display_name": "Avatar reader", "time_zone": None, "revision": 1,
                        "avatar_url": "/v1/me/avatar?v=fixture"},
        }))

    def avatar(route):
        tenant = route.request.headers.get("x-ec-tenant-id")
        avatar_reads.append(tenant)
        authorized = bool(tenant) if local_demo else tenant is None
        route.fulfill(status=200 if authorized else 401, content_type="image/png", body=_TRANSPARENT_MAP_TILE)

    page.route("**/v1/me", identity)
    page.route("**/v1/me/avatar?*", avatar)
    page.goto(f"{BASE}/settings")
    expect(page.get_by_role("img", name="Your profile photo", exact=True)).to_be_visible()
    page.wait_for_function("document.querySelector('.profile-photo__image')?.naturalWidth > 0", timeout=5000)
    page.goto(f"{BASE}/?view=events")
    expect(page.locator(".account-menu__avatar")).to_be_visible()
    page.wait_for_function("document.querySelector('.account-menu__avatar')?.naturalWidth > 0", timeout=5000)
    assert len(avatar_reads) == 2
    assert all(avatar_reads) if local_demo else not any(avatar_reads)
    assert len(set(avatar_reads)) == 1


def test_discovery_defers_api_keys_in_navigation_deep_links_and_history(release_page):
    harness, api = release_page
    page = harness.page
    page.goto(f"{BASE}/?view=events")
    page.get_by_role("button", name=re.compile(r"^Account menu")).click()
    expect(page.get_by_role("menuitem", name=re.compile(r"^API keys"))).to_have_count(0)
    page.goto(f"{BASE}/settings/api-keys")
    expect(page.get_by_role("heading", name="Account access", exact=True)).to_be_visible()
    expect(page.get_by_text("API keys are not available in this release.", exact=False)).to_be_visible()
    expect(page.get_by_role("navigation").get_by_role("link", name="API keys", exact=True)).to_have_count(0)
    expect(page.get_by_role("textbox", name="Key name", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Create key", exact=True)).to_have_count(0)
    page.get_by_role("main").get_by_role("link", name="Account and data", exact=True).click()
    expect(page.get_by_role("heading", name="Account and data", exact=True)).to_be_visible()
    page.go_back()
    expect(page.get_by_role("heading", name="Account access", exact=True)).to_be_visible()
    page.go_forward()
    expect(page.get_by_role("heading", name="Account and data", exact=True)).to_be_visible()
    assert not any(path.startswith("/v1/me/api-keys") for _, path in api.calls)


def test_full_profile_retains_api_key_records_with_accurate_capability_text(release_page):
    harness, api = release_page
    api.profile = "full"
    page = harness.page
    page.goto(f"{BASE}/?view=events")
    page.get_by_role("button", name=re.compile(r"^Account menu")).click()
    page.get_by_role("menuitem", name="API keys Development key records", exact=True).click()
    expect(page.get_by_role("heading", name="API keys", exact=True)).to_be_visible()
    expect(page.get_by_text("Authentication with these keys is not available yet.", exact=False)).to_be_visible()
    expect(page.get_by_role("textbox", name="Key name", exact=True)).to_be_visible()
    expect(page.get_by_text("No keys yet.", exact=True)).to_be_visible()
    assert ("GET", "/v1/me/api-keys") in api.calls
