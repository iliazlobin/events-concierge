"""Compact settings retain validation, saved values and explicit deletion using fixture writes."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Route, expect
from tests.e2e.test_next_release_profile import ReleaseApi

BASE = os.environ.get("EC_CONSUMER_WEB_URL") or os.environ.get("EC_ADMIN_WEB_URL")
pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(not BASE, reason="Set EC_CONSUMER_WEB_URL"),
]
FILTER_ID = "22222222-2222-4222-8222-222222222222"


class SettingsApi(ReleaseApi):
    def __init__(self):
        super().__init__()
        self.account_profile = {"display_name": None, "time_zone": None, "avatar_url": None,
                                "revision": 1, "updated_at": None}
        self.interests = []
        self.preference_revision = 1
        self.filters = {"city": "sanfrancisco", "when": "all", "sourceKeys": [], "topics": []}
        self.saved = {"saved_filter_id": FILTER_ID, "name": "SF events", "filters": self.filters,
                      "created_at": "2030-06-01T00:00:00Z", "updated_at": None,
                      "last_used_at": None}
        self.deleted = False
        self.writes = []
        self.hold_writes = False
        self.held_writes = []
        self.registration_account = {"executor": "muse", "expected_email": None,
                                     "provider_overrides": {}, "revision": 0, "updated_at": None}

    def config(self, route: Route) -> None:
        self.respond(route, {"product_name": "Events Concierge", "release_profile": "discovery",
                             "local_demo": True, "auth_mode": "local_demo", "muse_enabled": True})

    def handle(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        method = route.request.method
        handled = {"/v1/me", "/v1/me/profile", "/v1/preferences", "/v1/me/saved-filters",
                   f"/v1/me/saved-filters/{FILTER_ID}", "/v1/me/registration-accounts/muse",
                   "/v1/me/muse/connection"}
        if path not in handled:
            super().handle(route)
            return
        if method != "GET" and self.hold_writes:
            self.held_writes.append(route)
            return
        self.calls.append((method, path))
        if method != "GET":
            self.writes.append((method, path, route.request.post_data_json))
        if path == "/v1/me":
            self.respond(route, {"notify_email": "preview@example.test", "profile": self.account_profile,
                                 "interests": self.interests, "preference_revision": self.preference_revision,
                                 "local_demo": True, "is_admin": False})
        elif path == "/v1/me/profile":
            self.account_profile.update(route.request.post_data_json)
            self.respond(route, self.account_profile)
        elif path == "/v1/preferences":
            payload = route.request.post_data_json
            self.interests = payload["interests"]
            self.preference_revision = payload["revision"]
            self.respond(route, payload)
        elif path == "/v1/me/saved-filters":
            self.respond(route, [] if self.deleted else [self.saved])
        elif path == "/v1/me/muse/connection":
            self.respond(route, {"connected": False, "expires_at": None})
        elif path == "/v1/me/registration-accounts/muse":
            if method == "PUT":
                self.registration_account.update(route.request.post_data_json)
            self.respond(route, self.registration_account)
        elif method == "PUT":
            self.saved.update(route.request.post_data_json)
            self.respond(route, self.saved)
        elif method == "DELETE":
            self.deleted = True
            route.fulfill(status=204, body="")


@pytest.fixture(params=[1280, 375, 320], ids=["desktop", "mobile", "narrow-mobile"])
def settings_page(page_factory, request):
    harness = page_factory(width=request.param, authenticated=True)
    api = SettingsApi()
    api.install(harness.page)
    return harness.page, api


def test_corrected_profile_name_saves_on_the_first_click(settings_page):
    page, api = settings_page
    page.goto(f"{BASE}/settings")
    nav = page.get_by_role("navigation", name="Settings sections", exact=True)
    expect(nav.get_by_role("link", name="Documentation", exact=True)).to_have_count(0)
    expect(page.get_by_role("main").get_by_role("link", name="Guide")).to_have_attribute(
        "href", "/docs/account#profile"
    )
    name = page.get_by_role("textbox", name="Display name", exact=True)
    name.fill("!!!")
    page.get_by_label("Time zone", exact=True).focus()
    expect(page.locator("#display-name-error")).to_contain_text("letter or number")
    expect(name).to_have_attribute("aria-describedby", "display-name-error")
    assert not api.writes
    name.fill("Preview Person")
    # Clearing the old error only on blur moves Save during the click and cancels submission.
    expect(page.locator("#display-name-error")).to_have_count(0)
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Profile saved.")
    assert api.writes == [("PUT", "/v1/me/profile", {
        "display_name": "Preview Person", "time_zone": None, "revision": 2,
    })]


def test_compact_interests_keep_chip_selection_normalization_and_save(settings_page):
    page, api = settings_page
    page.goto(f"{BASE}/settings/taste")
    page.get_by_role("button", name="Interests help", exact=True).hover()
    expect(page.get_by_role("tooltip")).to_contain_text("Use topic filters")
    page.keyboard.press("Escape")
    art = page.get_by_role("button", name="Art & design", exact=True)
    art.click()
    expect(art).to_have_attribute("aria-pressed", "true")
    page.get_by_role("textbox", name="Add an interest", exact=True).fill("  Board Games  ")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.get_by_role("button", name="Remove board games", exact=True)).to_be_visible()
    assert not api.writes
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Interests saved.")
    assert api.writes == [("PUT", "/v1/preferences", {
        "interests": ["art", "board games"], "revision": 2,
    })]


def test_compact_saved_filters_keep_the_selection_and_require_delete_confirmation(settings_page):
    page, api = settings_page
    page.goto(f"{BASE}/settings/saved-filters")
    expect(page.get_by_text("SF events", exact=True)).to_be_visible()
    page.get_by_role("button", name="Rename", exact=True).click()
    page.get_by_role("textbox", name="Rename SF events", exact=True).fill("SF favorites")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_text("SF favorites", exact=True)).to_be_visible()
    assert api.writes == [("PUT", f"/v1/me/saved-filters/{FILTER_ID}", {
        "name": "SF favorites", "filters": api.filters,
    })]
    page.get_by_role("button", name="Delete SF favorites", exact=True).click()
    expect(page.get_by_text("Delete for good?", exact=True)).to_be_visible()
    assert len(api.writes) == 1
    page.get_by_role("button", name="Keep", exact=True).click()
    expect(page.get_by_text("Delete for good?", exact=True)).to_have_count(0)
    assert len(api.writes) == 1
    page.get_by_role("button", name="Delete SF favorites", exact=True).click()
    page.get_by_role("button", name="Delete", exact=True).click()
    expect(page.get_by_text("No saved filters.", exact=True)).to_be_visible()
    assert api.writes[-1][0:2] == ("DELETE", f"/v1/me/saved-filters/{FILTER_ID}")
    assert len(api.writes) == 2


def test_save_controls_match_and_stay_in_place_while_saving(settings_page):
    page, api = settings_page
    appearance = None
    for index, (path, field, value) in enumerate([
        ("/settings", "Display name", "Preview Person"),
        ("/settings/taste", None, None),
        ("/settings/registrations", "Default email", "provider@example.test"),
        ("/settings/saved-filters", "Rename SF events", "SF favorites"),
    ]):
        page.goto(BASE + path)
        if index == 3:
            page.get_by_role("button", name="Rename", exact=True).click()
        save = page.get_by_role("button", name="Save", exact=True)
        expect(save).to_be_visible()
        if index < 3:
            expect(save).to_be_disabled()
        original_box = save.bounding_box()
        if field:
            page.get_by_role("textbox", name=field, exact=True).fill(value)
        else:
            page.get_by_role("button", name="Art & design", exact=True).click()
        expect(save).to_be_enabled()
        # Empty and dirty feedback must not push Save to another row.
        assert save.bounding_box() == original_box
        current = save.evaluate("""element => {
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            return [box.width, box.height, style.fontSize, style.padding,
                    style.backgroundColor, style.borderRadius];
        }""")
        if appearance is None:
            appearance = current
        assert current == appearance
        save.press("Shift+Tab")
        page.keyboard.press("Tab")
        expect(save).to_be_focused()
        assert save.evaluate("element => getComputedStyle(element).outlineWidth") == "2px"
        if index < 3:
            footer_box = page.locator(".settings-actions").bounding_box()
            assert abs(original_box["x"] + original_box["width"]
                       - footer_box["x"] - footer_box["width"]) < 1
        api.hold_writes = True
        save.click()
        expect(save).to_have_attribute("aria-busy", "true")
        expect(save).to_be_disabled()
        assert save.bounding_box() == original_box
        assert len(api.held_writes) == 1
        api.hold_writes = False
        api.handle(api.held_writes.pop())
        if index < 3:
            expect(save).to_have_attribute("aria-busy", "false")
            expect(save).to_be_disabled()
            expect(page.locator(".settings-actions").get_by_role("status")).to_contain_text("saved", ignore_case=True)
            assert save.bounding_box() == original_box
        else:
            expect(page.get_by_text("SF favorites", exact=True)).to_be_visible()
        assert len(api.writes) == index + 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert api.unexpected == []
