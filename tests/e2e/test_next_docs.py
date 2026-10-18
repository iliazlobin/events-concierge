"""Public guides render without API access, sign-in or registration execution."""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(not BASE, reason="Set EC_CONSUMER_WEB_URL"),
]


class DocumentIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, _tag, attrs):
        self.ids.extend(value for name, value in attrs if name == "id" and value)


def check_reading_links(page):
    links = (
        page.get_by_role("main")
        .locator("a[href]")
        .evaluate_all(
            "elements => [...new Set(elements.map(element => element.getAttribute('href')))]"
        )
    )
    for href in links:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            continue
        if parts.path == "/v1/muse/openapi.json":
            # The schema link is explicit user navigation; static reading must not fetch it.
            continue
        response = page.request.get(
            BASE
            + (parts.path or urlsplit(page.url).path)
            + ("?" + parts.query if parts.query else "")
        )
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
    expect(page.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
    breadcrumb = page.get_by_role("navigation", name="Breadcrumb", exact=True)
    expect(breadcrumb.locator('[aria-current="page"]')).to_have_text("Overview")
    nav = page.get_by_role("navigation", name="Documentation", exact=True)
    expect(nav.get_by_role("link", name="Overview", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    guides = [
        ("Find events", "browsing"),
        ("Registrations", "muse"),
        ("MCP discovery", "mcp"),
        ("Account", "account"),
        ("How it works", "how-it-works"),
    ]
    for title, slug in guides:
        nav.get_by_role("link", name=title, exact=True).click()
        expect(page).to_have_url(re.compile(rf"/docs/{slug}$"))
        expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
        expect(breadcrumb.locator('[aria-current="page"]')).to_have_text(title)
        expect(breadcrumb.get_by_role("link", name="Docs", exact=True)).to_have_attribute(
            "href", "/docs"
        )
        expect(nav.get_by_role("link", name=title, exact=True)).to_have_attribute(
            "aria-current", "page"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        article = page.get_by_role("article")
        article_box = article.bounding_box()
        assert article_box and article_box["width"] <= 620
        disclosure = page.get_by_role("main").locator("details")
        if width == 320:
            expect(disclosure).not_to_have_attribute("open", "")
            disclosure.locator("summary").click()
        contents = page.get_by_role("navigation", name="On this page", exact=True)
        contents_box = contents.bounding_box()
        assert contents_box
        if width == 1440:
            assert contents_box["x"] >= article_box["x"] + article_box["width"]
            assert contents_box["width"] <= 180
            rail = page.get_by_role("complementary")
            assert rail.evaluate("element => getComputedStyle(element).position") == "sticky"
        else:
            assert contents_box["x"] >= article_box["x"]
            assert (
                contents_box["x"] + contents_box["width"] <= article_box["x"] + article_box["width"]
            )
        for link in contents.get_by_role("link").all():
            href = link.get_attribute("href")
            heading = page.locator(href)
            expect(heading).to_have_count(1)
            link.click()
            expect(heading).to_be_focused()
        check_reading_links(page)
    expect(page.get_by_role("link", name="Back to events", exact=True)).to_have_attribute(
        "href", "/"
    )
    assert api_calls == []


def test_guides_remain_readable_and_crosslinked_without_javascript(browser):
    context = browser.new_context(
        java_script_enabled=False, viewport={"width": 320, "height": 1000}
    )
    try:
        page = context.new_page()
        page.goto(BASE + "/docs/browsing")
        breadcrumb = page.get_by_role("navigation", name="Breadcrumb", exact=True)
        expect(breadcrumb.locator('[aria-current="page"]')).to_have_text("Find events")
        page.get_by_role("main").locator("details summary").click()
        contents = page.get_by_role("navigation", name="On this page", exact=True)
        contents.get_by_role("link", name="Views and calendar", exact=True).click()
        expect(
            page.get_by_role("heading", name="Events, map and calendar", exact=True)
        ).to_be_focused()
        page.get_by_role("navigation", name="Continue reading").get_by_role(
            "link", name="Register with Muse", exact=True
        ).click()
        expect(page.get_by_role("heading", name="Queue and start", exact=True)).to_be_visible()
        expect(page.get_by_role("textbox", name="Muse signup instruction")).to_contain_text(
            "Register only the free events I queued"
        )
        expect(
            page.get_by_text("Select and copy the instruction above.", exact=True)
        ).to_be_visible()
        assert page.locator("pre code").count() > 0
        expect(page.get_by_role("link", name="connector schema", exact=True)).to_have_attribute(
            "href", "/v1/muse/openapi.json"
        )
    finally:
        context.close()


def test_muse_instruction_can_be_copied_without_account_or_api_access(page_factory):
    harness = page_factory()
    page = harness.page
    api_calls = []

    def refuse_api(route):
        api_calls.append(route.request.url)
        route.fulfill(status=503, content_type="application/json", body='{"detail":"offline"}')

    page.route("**/v1/**", refuse_api)
    page.route("**/auth/**", refuse_api)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.goto(BASE + "/docs/muse")
    instruction = page.get_by_role("textbox", name="Muse signup instruction").input_value()
    assert "Register only the free events I queued" in instruction
    page.get_by_role("button", name="Copy Muse instruction", exact=True).click()
    expect(page.get_by_role("status")).to_have_text("Instruction copied.")
    assert page.evaluate("navigator.clipboard.readText()") == instruction
    assert api_calls == []
