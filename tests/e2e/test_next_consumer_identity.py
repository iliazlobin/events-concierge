"""Production guest browsing and managed signup UI; no real OAuth account is created."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Route, expect
from tests.e2e.test_next_release_profile import ReleaseApi

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(not BASE, reason="Set EC_CONSUMER_WEB_URL or EC_ADMIN_WEB_URL"),
]


class ConsumerIdentityApi(ReleaseApi):
    def __init__(self, harness):
        super().__init__(local_demo=False)
        self.harness = harness
        self.account_status = 401
        self.challenge_status = 200
        self.providers = ["google.com", "apple.com"]
        self.legal_mode = "required"
        self.legal_available = True
        self.catalog_name_suggestions_enabled: bool | None = False

    def config(self, route: Route) -> None:
        self.respond(route, {
            "product_name": "Events Concierge", "release_profile": "discovery",
            **({"catalog_name_suggestions_enabled": self.catalog_name_suggestions_enabled}
               if self.catalog_name_suggestions_enabled is not None else {}),
            "local_demo": False, "auth_mode": "deployment_session",
            "auth_provider": "identity_platform", "anonymous_browsing": True,
            "auth_start_url": "/sign-in", "reauth_url": "/auth/reauth",
            "logout_url": "/auth/logout", "csrf_cookie_name": "__Host-ec_csrf",
            "csrf_header_name": "X-EC-CSRF",
            "identity_platform": {
                "project_id": "events-identity-test",
                "api_key": "restricted-browser-key-fixture",
                "auth_domain": "events-identity-test.firebaseapp.com",
                "providers": self.providers,
            },
            "consumer_legal_mode": self.legal_mode,
            "legal_policy": {
                "terms_version": "2026-10-05", "privacy_version": "2026-10-05",
                "terms_url": "https://events.example.test/terms/2026-10-05",
                "privacy_url": "https://events.example.test/privacy/2026-10-05",
            } if self.legal_available and self.legal_mode != "deferred" else None,
        })

    def handle(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        if path == "/v1/me":
            self.calls.append((route.request.method, path))
            self.harness.allowed_console_error_fragments.append(str(self.account_status))
            self.respond(route, {"detail": "fixture account unavailable"}, self.account_status)
        elif path == "/auth/identity/start":
            self.calls.append((route.request.method, path))
            if self.challenge_status != 200:
                self.harness.allowed_console_error_fragments.append(str(self.challenge_status))
            self.respond(route, {"state": "s" * 43}, self.challenge_status)
        elif path.startswith("/auth/"):
            self.unexpected.append(path)
            self.respond(route, {"detail": "unexpected OAuth mutation"}, 500)
        else:
            super().handle(route)


@pytest.fixture
def identity_page(page_factory):
    harness = page_factory(authenticated=False)
    api = ConsumerIdentityApi(harness)
    api.install(harness.page)
    harness.page.route("**/auth/**", api.handle)
    yield harness, api
    assert not api.unexpected
    assert all(method == "GET" for method, _ in api.calls)


def test_guest_browses_catalog_and_graph_without_personal_data_calls(identity_page):
    harness, api = identity_page
    page = harness.page
    page.goto(f"{BASE}/?view=events")
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Sign in to save filters")).to_be_visible()
    expect(page.get_by_role("link", name="Sign in", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Account menu", exact=False)).to_have_count(0)
    expect(page.get_by_role("menuitem")).to_have_count(0)
    expect(page.get_by_role("button", name="Save filters", exact=True)).to_have_count(0)
    for width in (1440, 390, 320):
        page.set_viewport_size({"width": width, "height": 800})
        expect(page.get_by_role("link", name="Sign in", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 1440, "height": 800})
    page.locator(".site-nav").get_by_role("button", name="Entities", exact=True).click()
    expect(page.locator(".site-nav").get_by_role("button", name="Entities", exact=True)).to_have_attribute("aria-current", "page")
    assert not any(path.startswith(("/v1/onboard", "/v1/me/", "/v1/preferences", "/auth/")) for _, path in api.calls)


def test_guest_signin_keeps_the_selected_view_and_filters(identity_page):
    harness, _ = identity_page
    page = harness.page
    page.goto(f"{BASE}/?view=events&when=week&price=free&city=oakland")
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    page.get_by_role("link", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    destination = parse_qs(urlsplit(page.url).query)["return_to"][0]
    filters = parse_qs(urlsplit(destination).query)
    assert filters["view"] == ["events"]
    assert filters["when"] == ["week"]
    assert filters["price"] == ["free"]
    assert filters["city"] == ["oakland"]
    expect(page.get_by_role("link", name="Browse events")).to_have_attribute("href", destination)
    page.get_by_role("link", name="Browse events").click()
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["price"] == ["free"]


def test_google_apple_actions_require_unchecked_legal_consent_without_token_storage(identity_page):
    harness, api = identity_page
    page = harness.page
    response = page.goto(f"{BASE}/sign-in?return_to=%2Fsettings%2Fsaved-filters")
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    google = page.get_by_role("button", name="Continue with Google", exact=True)
    apple = page.get_by_role("button", name="Continue with Apple", exact=True)
    consent = page.get_by_role("checkbox")
    expect(page.get_by_role("heading", name="Sign in", exact=True)).to_be_visible()
    expect(google).to_be_disabled()
    expect(apple).to_be_disabled()
    expect(consent).not_to_be_checked()
    expect(page.get_by_role("link", name="Terms of Service")).to_have_attribute("href", "https://events.example.test/terms/2026-10-05")
    expect(page.get_by_role("link", name="Privacy Policy")).to_have_attribute("href", "https://events.example.test/privacy/2026-10-05")
    consent.check()
    expect(google).to_be_enabled()
    expect(apple).to_be_enabled()
    for width in (1440, 390, 320):
        page.set_viewport_size({"width": width, "height": 800})
        expect(google).to_be_visible()
        expect(apple).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    consent.uncheck()
    expect(google).to_be_disabled()
    assert page.evaluate("Object.keys(localStorage).filter(key => key.startsWith('firebase:'))") == []
    assert page.evaluate("Object.keys(sessionStorage).filter(key => key.startsWith('firebase:'))") == []
    assert api.calls.count(("GET", "/auth/identity/start")) == 1
    assert not any(path == "/v1/me" for _, path in api.calls)
    # Browser-key checks receive an origin only, never the sign-in URL's state/destination.
    referrers = []
    def provider_request(route):
        referrers.append(route.request.all_headers().get("referer"))
        route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*"}, body="{}")
    page.route("https://identitytoolkit.googleapis.com/v1/fixture", provider_request)
    page.evaluate("fetch('https://identitytoolkit.googleapis.com/v1/fixture')")
    assert referrers == [f"{BASE}/"]


@pytest.mark.parametrize("account_status", [401, 428])
def test_settings_require_identity_and_preserve_destination(identity_page, account_status):
    harness, api = identity_page
    api.account_status = account_status
    page = harness.page
    page.goto(f"{BASE}/settings/saved-filters")
    expect(page.get_by_role("button", name="Continue with Apple", exact=True)).to_be_visible()
    query = parse_qs(urlsplit(page.url).query)
    assert urlsplit(page.url).path == "/sign-in"
    assert query["return_to"] == ["/settings/saved-filters"]
    if account_status == 428:
        expect(page.get_by_role("heading", name="Review the updated terms", exact=True)).to_be_visible()
    assert not any(path == "/v1/me/saved-filters" for _, path in api.calls)


def test_failed_signin_challenge_cannot_enable_provider_actions(identity_page):
    harness, api = identity_page
    api.challenge_status = 503
    page = harness.page
    page.goto(f"{BASE}/sign-in")
    expect(page.get_by_role("main").get_by_role("alert")).to_contain_text("We couldn\u2019t load sign-in options")
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Browse events")).to_be_visible()


def test_google_only_pilot_shows_only_its_enabled_provider(identity_page):
    harness, api = identity_page
    api.providers = ["google.com"]
    page = harness.page
    page.goto(f"{BASE}/sign-in")
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Continue with Apple", exact=True)).to_have_count(0)
    page.get_by_role("checkbox").check()
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_enabled()


def test_explicit_legal_deferral_enables_signup_without_claiming_acceptance(identity_page):
    harness, api = identity_page
    api.legal_mode = "deferred"
    api.providers = ["google.com"]
    page = harness.page
    page.goto(f"{BASE}/sign-in?return_to=%2Fsettings%2Fsaved-filters")
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_enabled()
    expect(page.get_by_role("checkbox")).to_have_count(0)
    expect(page.get_by_role("link", name="Terms of Service")).to_have_count(0)
    expect(page.get_by_role("link", name="Privacy Policy")).to_have_count(0)
    assert api.calls.count(("GET", "/auth/identity/start")) == 1
    assert (
        page.evaluate("Object.keys(localStorage).filter(key => key.startsWith('firebase:'))") == []
    )
    assert (
        page.evaluate("Object.keys(sessionStorage).filter(key => key.startsWith('firebase:'))")
        == []
    )


def test_missing_legal_documents_do_not_implicitly_defer_signup(identity_page):
    harness, api = identity_page
    api.legal_available = False
    page = harness.page
    page.goto(f"{BASE}/sign-in")
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_disabled()
    expect(page.get_by_text("Sign-in is temporarily unavailable.", exact=True)).to_be_visible()
    assert not any(path == "/auth/identity/start" for _, path in api.calls)


def test_account_confirmation_does_not_offer_signup_or_start_a_new_challenge(identity_page):
    harness, api = identity_page
    page = harness.page
    page.goto(f"{BASE}/sign-in?reauth=1&state={'r' * 43}&return_to=%2Fsettings%2Faccount")
    expect(page.get_by_role("heading", name="Confirm your account", exact=True)).to_be_visible()
    expect(page.get_by_text("Sign in with the same account to confirm this change.", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Continue with Google", exact=True)).to_be_enabled()
    expect(page.get_by_role("checkbox")).to_have_count(0)
    assert not any(path == "/auth/identity/start" for _, path in api.calls)
