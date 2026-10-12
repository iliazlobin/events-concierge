"""Public guides render without API access, sign-in or registration execution."""

from __future__ import annotations

import os
import re

import pytest
from playwright.sync_api import expect

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [pytest.mark.browser_e2e, pytest.mark.skipif(not BASE, reason="Set EC_CONSUMER_WEB_URL")]


@pytest.mark.parametrize("width", [1440, 320])
def test_docs_are_public_and_all_guides_work_without_backend(page_factory, width):
    harness = page_factory(width=width)
    page = harness.page
    api_calls = []

    def refuse_api(route):
        api_calls.append(route.request.url)
        route.fulfill(status=503, content_type="application/json", body='{"detail":"offline"}')

    page.route("**/v1/**", refuse_api)
    page.route("**/auth/**", refuse_api)
    response = page.goto(BASE + "/docs")
    assert response and response.status == 200
    assert "Documentation" in response.text()
    expect(page.get_by_role("heading", name="Documentation", exact=True)).to_be_visible()
    nav = page.get_by_role("navigation", name="Documentation", exact=True)
    expect(nav.get_by_role("link", name="Overview", exact=True)).to_have_attribute("aria-current", "page")
    guides = [("Find events", "browsing"), ("Muse signups", "muse"),
              ("Your account", "account"), ("How it works", "how-it-works")]
    for title, slug in guides:
        nav.get_by_role("link", name=title, exact=True).click()
        expect(page).to_have_url(re.compile(rf"/docs/{slug}$"))
        expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
        expect(nav.get_by_role("link", name=title, exact=True)).to_have_attribute("aria-current", "page")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.get_by_role("link", name="Back to events", exact=True)).to_have_attribute("href", "/")
    assert api_calls == []
