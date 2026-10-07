"""Built Next.js sign-in flows with isolated JSON/auth fixtures, never a real identity provider.

Run with EC_CONSUMER_WEB_URL or EC_ADMIN_WEB_URL pointing at a loopback Next.js server and the repository's Python
Playwright dependencies: python -m pytest tests/e2e/test_next_sign_in.py.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Route, expect
from tests.e2e.test_next_release_profile import ReleaseApi

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(not BASE, reason="Set EC_CONSUMER_WEB_URL or EC_ADMIN_WEB_URL"),
]


@dataclass
class SignInApi(ReleaseApi):
    provider: str | None = "google"
    local_demo: bool = False
    authenticated: bool = True
    logout_status: int = 204

    def config(self, route: Route) -> None:
        self.respond(
            route,
            {
                "product_name": "Events Concierge",
                "release_profile": "discovery",
                "local_demo": self.local_demo,
                "auth_mode": "local_demo" if self.local_demo else "deployment_session",
                "auth_provider": self.provider,
                "auth_start_url": "/auth/login" if self.provider else None,
                "reauth_url": "/auth/reauth" if self.provider == "custom_claim" else None,
                "logout_url": "/auth/logout",
                "csrf_cookie_name": "fixture-csrf",
                "csrf_header_name": "X-Fixture-CSRF",
            }
            if self.config_status == 200
            else {"detail": "PRIVATE_PROVIDER_ERROR"},
            self.config_status,
        )

    def handle(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        if path == "/v1/me" and not self.authenticated:
            self.calls.append((route.request.method, path))
            self.respond(route, {"detail": "PRIVATE_PROVIDER_ERROR"}, 401)
        elif path.startswith("/auth/"):
            self.calls.append((route.request.method, path))
            if path == "/auth/login":
                route.fulfill(status=303, headers={"Location": "/sign-in?reason=cancelled"})
            elif path == "/auth/logout":
                if self.logout_status == 204:
                    self.authenticated = False
                    route.fulfill(status=204)
                else:
                    self.respond(route, {"detail": "PRIVATE_PROVIDER_ERROR"}, self.logout_status)
            else:
                self.unexpected.append(path)
                self.respond(route, {"detail": "Unexpected authentication request"}, 500)
        else:
            super().handle(route)


@pytest.fixture
def sign_in_page(page_factory):
    harness = page_factory(authenticated=False)
    api = SignInApi()
    api.install(harness.page)
    harness.page.route("**/auth/**", api.handle)
    yield harness, api
    assert not api.unexpected


@pytest.mark.parametrize(
    "reason,title",
    [
        ("", "Sign in"),
        ("cancelled", "Sign-in cancelled"),
        ("not_authorized", "This account can\u2019t sign in"),
        ("unavailable", "Sign-in is temporarily unavailable"),
        ("signed_out", "You\u2019re signed out"),
        ("PRIVATE_PROVIDER_ERROR", "Sign-in wasn\u2019t completed"),
    ],
)
def test_public_landing_is_terminal_and_safe(sign_in_page, reason, title):
    harness, api = sign_in_page
    page = harness.page
    response = page.goto(f"{BASE}/sign-in?reason={reason}" if reason else f"{BASE}/sign-in")
    assert response.status == 200
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
    link = page.get_by_role("link", name="Continue with Google", exact=True)
    expect(link).to_have_attribute("href", "/auth/login")
    expect(page.get_by_role("main")).not_to_contain_text("PRIVATE_PROVIDER_ERROR")
    page.wait_for_timeout(150)
    assert api.calls and all(path == "/v1/ui-config" for _, path in api.calls)
    for width in [1440, 390, 320]:
        page.set_viewport_size({"width": width, "height": 700})
        expect(link).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.keyboard.press("Tab")
    expect(link).to_be_focused()
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") != "none"
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Sign-in cancelled", exact=True)).to_be_visible()
    page.wait_for_timeout(150)
    assert api.calls.count(("GET", "/auth/login")) == 1
    assert not any(path == "/v1/me" for _, path in api.calls)


def test_configuration_failure_is_safe_and_retry_only_loads_config(sign_in_page):
    harness, api = sign_in_page
    api.config_status = 503
    harness.allowed_console_error_fragments.append("503")
    page = harness.page
    page.goto(f"{BASE}/sign-in")
    expect(page.get_by_role("main").get_by_role("alert")).to_have_text(
        "We couldn\u2019t load sign-in options. Please try again."
    )
    expect(page.get_by_role("main")).not_to_contain_text("PRIVATE_PROVIDER_ERROR")
    assert all(path == "/v1/ui-config" for _, path in api.calls)
    api.config_status = 200
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.get_by_role("link", name="Continue with Google", exact=True)).to_be_visible()
    assert all(path == "/v1/ui-config" for _, path in api.calls)


@pytest.mark.parametrize("provider", ["custom_claim", None])
def test_generic_or_unconfigured_authentication_is_not_labelled_google(sign_in_page, provider):
    harness, api = sign_in_page
    api.provider = provider
    page = harness.page
    page.goto(f"{BASE}/sign-in")
    if provider:
        expect(page.get_by_role("link", name="Continue to sign in", exact=True)).to_have_attribute(
            "href", "/auth/login"
        )
    else:
        expect(page.get_by_role("button", name="Check again", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="Browse events")).to_have_attribute("href", "/")
        expect(page.get_by_role("link", name="Continue to sign in", exact=True)).to_have_count(0)
    expect(page.get_by_role("main")).not_to_contain_text("Google")


def test_local_demo_preserves_onboarding(sign_in_page):
    harness, api = sign_in_page
    api.local_demo = True
    api.provider = None
    api.authenticated = False
    harness.allowed_console_error_fragments.append("401")
    page = harness.page
    page.goto(f"{BASE}/sign-in")
    page.get_by_role("link", name="Continue to local demo", exact=True).click()
    expect(page.get_by_role("textbox", name="Email address", exact=True)).to_be_visible()
    assert not any(path.startswith("/auth/") for _, path in api.calls)


@pytest.mark.parametrize("path", ["/", "/settings/account"])
def test_expired_deployment_session_lands_without_starting_google(sign_in_page, path):
    harness, api = sign_in_page
    api.authenticated = False
    harness.allowed_console_error_fragments.append("401")
    page = harness.page
    page.goto(f"{BASE}{path}")
    expected = f"{BASE}/sign-in" if path == "/" else f"{BASE}/sign-in?return_to=%2Fsettings%2Faccount"
    expect(page).to_have_url(expected)
    expect(page.get_by_role("link", name="Continue with Google", exact=True)).to_be_visible()
    assert api.calls.count(("GET", "/v1/me")) == 1
    assert not any(path.startswith("/auth/") for _, path in api.calls)


@pytest.mark.parametrize("provider", ["google", "custom_claim"])
def test_google_deletion_gate_preserves_custom_claim_confirmation(sign_in_page, provider):
    harness, api = sign_in_page
    api.provider = provider
    page = harness.page
    page.goto(f"{BASE}/settings/account")
    button = page.get_by_role("button", name="Erase my account…", exact=True)
    if provider == "google":
        expect(button).to_be_disabled()
        expect(page.locator("#deletion-unavailable")).to_contain_text(
            "Signing in again will not enable deletion."
        )
        expect(page.get_by_text("Google", exact=True)).to_be_visible()
        expect(page.get_by_text("Destructive actions ask you to sign in again first.", exact=False)).to_have_count(0)
    else:
        expect(button).to_be_enabled()
        expect(page.get_by_text("Single sign-on", exact=True)).to_be_visible()
        button.click()
        page.get_by_role("button", name="Cancel", exact=True).click()
        expect(button).to_be_visible()
    assert all(method == "GET" for method, _ in api.calls)


@pytest.mark.parametrize("logout_status", [204, 503])
def test_logout_waits_for_revocation_and_never_restarts_login(sign_in_page, logout_status):
    harness, api = sign_in_page
    api.logout_status = logout_status
    if logout_status == 503:
        harness.allowed_console_error_fragments.append("503")
    page = harness.page
    page.goto(f"{BASE}/")
    expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
    page.evaluate("localStorage.setItem('events-concierge.local-session.v1', 'fixture-session')")
    page.get_by_role("button", name=re.compile("^Account menu")).click()
    page.get_by_role("menuitem", name="Sign out", exact=False).click()
    if logout_status == 204:
        expect(page).to_have_url(f"{BASE}/sign-in?reason=signed_out")
        expect(page.get_by_role("heading", name="You\u2019re signed out", exact=True)).to_be_visible()
        assert page.evaluate("localStorage.getItem('events-concierge.local-session.v1')") is None
    else:
        expect(
            page.get_by_text("We couldn\u2019t sign you out. Please try again.", exact=True)
        ).to_be_visible()
        expect(page.get_by_role("heading", name="Friday Night Jazz", exact=True)).to_be_visible()
        assert (
            page.evaluate("localStorage.getItem('events-concierge.local-session.v1')") is not None
        )
    page.wait_for_timeout(150)
    assert api.calls.count(("POST", "/auth/logout")) == 1
    assert not any(path == "/auth/login" for _, path in api.calls)
