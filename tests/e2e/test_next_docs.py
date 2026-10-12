"""Public guides render without API access, sign-in or registration execution."""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [pytest.mark.browser_e2e, pytest.mark.skipif(not BASE, reason="Set EC_CONSUMER_WEB_URL")]


class DocumentIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, _tag, attrs):
        self.ids.extend(value for name, value in attrs if name == "id" and value)


def check_reading_links(page):
    links = page.get_by_role("main").locator("a[href]").evaluate_all("elements => [...new Set(elements.map(element => element.getAttribute('href')))]")
    for href in links:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            continue
        response = page.request.get(BASE + (parts.path or urlsplit(page.url).path) + ("?" + parts.query if parts.query else ""))
        assert response.status == 200, href
        if parts.fragment:
            ids = DocumentIds()
            ids.feed(response.text())
            assert ids.ids.count(parts.fragment) == 1, href


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
        contents = page.get_by_role("navigation", name="On this page", exact=True)
        for link in contents.get_by_role("link").all():
            href = link.get_attribute("href")
            heading = page.locator(href)
            expect(heading).to_have_count(1)
            link.click()
            expect(heading).to_be_focused()
        check_reading_links(page)
    expect(page.get_by_role("link", name="Back to events", exact=True)).to_have_attribute("href", "/")
    assert api_calls == []


def test_guides_remain_readable_and_crosslinked_without_javascript(browser):
    context = browser.new_context(java_script_enabled=False, viewport={"width": 320, "height": 1000})
    try:
        page = context.new_page()
        page.goto(BASE + "/docs/browsing")
        contents = page.get_by_role("navigation", name="On this page", exact=True)
        contents.get_by_role("link", name="Views and calendar", exact=True).click()
        expect(page.get_by_role("heading", name="Events, map and calendar", exact=True)).to_be_focused()
        page.get_by_role("navigation", name="Continue reading").get_by_role("link", name="Register with Muse", exact=True).click()
        expect(page.get_by_role("heading", name="Queue and start", exact=True)).to_be_visible()
        expect(page.get_by_role("textbox", name="Muse signup instruction")).to_contain_text("Register only the free events I queued")
        expect(page.get_by_text("Select and copy the instruction above.", exact=True)).to_be_visible()
        assert page.locator("pre code").count() > 0
    finally:
        context.close()
