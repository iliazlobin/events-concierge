"""Canonical Sources configuration stays scoped, permission-aware and revision-checked.

Only the real Next.js UI is exercised. All API requests use browser fixtures; the
single permitted PATCH updates fixture state and never reaches a live backend.
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Locator, Page, Route, expect
from tests.e2e.test_admin_command_investigation import ACCEPTED, CommandApi
from tests.e2e.test_admin_workspaces import STAMP, START, WorkspaceApi, source_detail

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]
SOURCE = "bay-arts-01"
SECOND_SOURCE = "bay-arts-02"
FIRST_SEED = "https://example.test/events/one"
SECOND_SEED = "https://second.example.test/events/two"


def configuration(source_key: str) -> dict[str, Any]:
    detail = source_detail(source_key, 24)
    detail["window"]["starts_at"] = (
        datetime.fromisoformat(STAMP) - timedelta(hours=24)
    ).isoformat()
    second = source_key == SECOND_SOURCE
    detail["source"].update(
        source_revision=8 if second else 4,
        seed_url=SECOND_SEED if second else FIRST_SEED,
        seed_host="second.example.test" if second else "example.test",
        approved_origins=["https://second.example.test" if second else "https://example.test"],
        page_limit=20 if second else 10,
        collection_horizon_days=90,
    )
    return detail


@dataclass
class SourceConfigurationApi(WorkspaceApi):
    role: str = "viewer"
    hold_save: bool = False
    held_save: Route | None = None
    allow_refresh: bool = False
    refresh_writes: list[dict[str, Any]] = field(default_factory=list)
    command_api: CommandApi = field(default_factory=CommandApi)
    hidden_source_keys: set[str] = field(default_factory=set)
    writes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    config: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            SOURCE: configuration(SOURCE),
            SECOND_SOURCE: configuration(SECOND_SOURCE),
        }
    )

    def handle(self, route: Route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        query = parse_qs(urlsplit(request.url).query)
        key = path.rsplit("/", 1)[-1]
        if path == "/admin/v1/operator/session" and request.method == "GET":
            self.calls.append((path, query))
            self.respond(
                route,
                {
                    "subject": "source-config-fixture",
                    "role": self.role,
                    "environment": "local",
                    "authentication": "local",
                    "capabilities": ["ingestion.sources.configure", "ingestion.refresh"]
                    if self.role == "reviewer"
                    else ["ingestion.refresh"]
                    if self.role == "operator"
                    else [],
                },
            )
        elif path == "/admin/v1/ingestion/source-health" and request.method == "GET":
            self.calls.append((path, query))
            self.respond_health(route)
        elif path.startswith("/admin/v1/ingestion/commands") and self.allow_refresh:
            self.handle_command(route)
        elif path == f"/admin/v1/ingestion/sources/{key}" and key in self.config:
            self.calls.append((path, query))
            if request.method == "PATCH":
                if self.hold_save:
                    assert self.held_save is None
                    self.held_save = route
                else:
                    self.save(route, key)
            elif request.method == "GET":
                if status := self.status_overrides.get(path):
                    self.respond(route, {"detail": "Configuration fixture read failed"}, status)
                else:
                    detail = copy.deepcopy(self.config[key])
                    hours = int(query.get("window_hours", ["24"])[0])
                    detail["window"].update(
                        hours=hours,
                        starts_at=(
                            datetime.fromisoformat(STAMP) - timedelta(hours=hours)
                        ).isoformat(),
                    )
                    self.respond(route, detail)
            else:
                self.unexpected.append(f"{request.method} {path}")
                self.respond(route, {"detail": "Fixture forbids this method"}, 405)
        else:
            super().handle(route)

    def handle_command(self, route: Route) -> None:
        if route.request.method == "POST":
            payload = route.request.post_data_json
            assert self.role in {"operator", "reviewer"}
            assert payload["action"] == "refresh_source"
            assert payload["source_key"] == SOURCE
            self.refresh_writes.append(payload)
        if (
            route.request.method == "GET"
            and not self.command_api.submitted
            and urlsplit(route.request.url).path.endswith("/commands")
        ):
            self.respond(route, {"items": []})
        else:
            self.command_api.handle(route)

    def respond_source_page(self, route: Route, page_key: tuple[str, int]) -> None:
        if page_key in self.source_page_responses:
            super().respond_source_page(route, page_key)
            return
        query = parse_qs(urlsplit(route.request.url).query)
        rows = [
            copy.deepcopy(item["source"])
            for key, item in self.config.items()
            if key not in self.hidden_source_keys
        ]
        needle, offset = page_key
        if needle:
            rows = [
                row
                for row in rows
                if needle.lower()
                in f"{row['source_key']} {row['display_name']} {row['publisher']}".lower()
            ]
        limit = int(query.get("limit", ["100"])[0])
        self.respond(
            route,
            {
                "items": rows[offset : offset + limit],
                "total": len(rows),
                "limit": limit,
                "offset": offset,
            },
        )

    def respond_health(self, route: Route) -> None:
        if status := self.status_overrides.get("/admin/v1/ingestion/source-health"):
            self.respond(route, {"detail": "Source roster fixture read failed"}, status)
            return
        sources = []
        for item in (
            item for key, item in self.config.items() if key not in self.hidden_source_keys
        ):
            source = item["source"]
            sources.append(
                {
                    **source,
                    "health": "healthy",
                    "run_state": "ok",
                    "freshness_state": "ok",
                    "retry_state": "ok",
                    "yield_state": "ok",
                    "last_attempt_at": START,
                    "last_success_at": START,
                    "last_catalog_change_at": None,
                    "latest_run_status": "succeeded",
                    "latest_run_error": None,
                    "latest_attempt_count": 1,
                    "upcoming_events": source["event_count"],
                    "hours_since_success": 0,
                }
            )
        self.respond(route, {"generated_at": STAMP, "total": len(sources), "sources": sources})

    def save(self, route: Route, key: str) -> None:
        payload = route.request.post_data_json
        self.writes.append((key, payload))
        if self.role != "reviewer":
            self.unexpected.append(f"unauthorized fixture PATCH {key}")
            self.respond(route, {"detail": "Reviewer required"}, 403)
            return
        source = self.config[key]["source"]
        if payload.get("expected_revision") != source["source_revision"]:
            self.respond(route, {"detail": "Revision conflict"}, 409)
            return
        assert payload["review_acknowledged"] is True
        assert payload["handoff_only"] is True
        assert payload["mode"] == source["mode"]
        if "collection_horizon_days" in payload:
            assert isinstance(payload["collection_horizon_days"], int)
            assert 1 <= payload["collection_horizon_days"] <= 90
        source.update(
            {
                name: value
                for name, value in payload.items()
                if name not in {"expected_revision", "review_acknowledged"}
            }
        )
        source["source_revision"] += 1
        self.respond(
            route,
            {
                "source_key": key,
                "source_revision": source["source_revision"],
                "reviewed_at": STAMP,
                "updated_at": STAMP,
            },
        )


@pytest.fixture
def source_page(browser: Browser) -> Iterator[tuple[Page, SourceConfigurationApi, str]]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        reduced_motion="reduce",
        permissions=["clipboard-read", "clipboard-write"],
    )
    page = context.new_page()
    page.set_default_timeout(7000)
    scenario = SourceConfigurationApi()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/admin/v1/**", scenario.handle)
    try:
        yield page, scenario, os.environ["EC_ADMIN_WEB_URL"].rstrip("/")
        assert not errors, errors
        assert not scenario.unexpected, scenario.unexpected
        assert not scenario.command_api.unexpected, scenario.command_api.unexpected
    finally:
        page.unroute_all(behavior="ignoreErrors")
        context.close()


def inspector(page: Page) -> Locator:
    return page.get_by_role("region", name="Source configuration", exact=True)


def open_configuration(page: Page, name: str = "Bay Arts 01") -> Locator:
    page.get_by_role("button", name=f"Configuration for {name}", exact=True).click()
    panel = inspector(page)
    expect(panel).to_be_visible()
    expect(page.get_by_role("heading", name="Source configuration", exact=True)).to_be_visible()
    return panel


def start_edit(panel: Locator, limit: str = "12") -> None:
    panel.get_by_role("spinbutton", name="Page limit", exact=True).fill(limit)


def test_source_configuration_preserves_roster_and_viewer_history(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    expect(panel.get_by_text(FIRST_SEED, exact=True)).to_be_visible()
    expect(panel.get_by_text("Read-only · reviewer access required", exact=True)).to_be_visible()
    expect(panel.get_by_role("button", name=re.compile(r"^Edit "))).to_have_count(0)
    expect(panel.get_by_role("link", name=FIRST_SEED, exact=True)).to_have_attribute(
        "href", FIRST_SEED
    )
    expect(
        page.get_by_role("button", name="Configuration for Bay Arts 02", exact=True)
    ).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["source_selection"] == [SOURCE]
    assert params["source_inspector"] == ["configuration"]
    assert "source" not in params and "catalog_config" not in params
    page.reload()
    expect(panel.get_by_text(FIRST_SEED, exact=True)).to_be_visible()
    page.go_back()
    expect(panel).to_have_count(0)
    page.go_forward()
    expect(panel.get_by_text(FIRST_SEED, exact=True)).to_be_visible()
    expect(
        page.get_by_role("button", name=re.compile(r"^(Open full|Full) source workspace$"))
    ).to_have_count(0)
    assert not scenario.writes


def test_source_configuration_is_directly_editable_and_cancel_restores_verified_values(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    expect(panel.get_by_role("button", name=re.compile(r"^Edit "))).to_have_count(0)
    for role, name in [
        ("textbox", "Seed URL"),
        ("spinbutton", "Page limit"),
        ("textbox", "Approved HTTPS origins · one per line"),
    ]:
        expect(panel.get_by_role(role, name=name, exact=True)).to_be_enabled()
    save = panel.get_by_role("button", name="Save", exact=True)
    cancel = panel.get_by_role("button", name="Cancel", exact=True)
    expect(save).to_be_disabled()
    expect(cancel).to_be_disabled()
    expect(panel.get_by_role("checkbox")).to_have_count(0)
    expect(panel.get_by_role("button", name="Reset", exact=True)).to_have_count(0)
    panel.get_by_role("spinbutton", name="Page limit", exact=True).fill("17")
    expect(save).to_be_enabled()
    expect(cancel).to_be_enabled()
    cancel.click()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("10")
    expect(save).to_be_disabled()
    expect(panel.get_by_role("textbox", name="Seed URL", exact=True)).to_be_enabled()
    managed = panel.locator("details").filter(has_text="Identity, adapter, and policy")
    expect(managed.locator("dl")).to_be_hidden()
    managed.locator("summary").click()
    expect(managed.get_by_text("Adapter contract", exact=True)).to_be_visible()
    expect(managed.get_by_role("textbox")).to_have_count(0)
    assert not scenario.writes


def test_source_daily_cadence_preset_keeps_revision_and_review_contract(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    minutes = panel.get_by_role("spinbutton", name="Refresh interval · min", exact=True)
    expect(minutes).to_be_enabled()
    presets = panel.get_by_role("group", name="Refresh cadence presets", exact=True)
    expect(presets.get_by_role("button", name="Every 6h", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    for label, value in [("Every 12h", "720"), ("Every 6h", "360"), ("Daily", "1440")]:
        presets.get_by_role("button", name=label, exact=True).click()
        expect(minutes).to_have_value(value)
        expect(presets.get_by_role("button", name=label, exact=True)).to_have_attribute(
            "aria-pressed", "true"
        )
    minutes.fill("90")
    expect(presets.locator('button[aria-pressed="true"]')).to_have_count(0)
    presets.get_by_role("button", name="Daily", exact=True).click()
    save = panel.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_enabled()
    save.click()
    expect(panel.get_by_text("registry.rev/5", exact=True)).to_be_visible()
    expect(
        panel.get_by_role("spinbutton", name="Refresh interval · min", exact=True)
    ).to_have_value("1440")
    assert len(scenario.writes) == 1
    source_key, payload = scenario.writes[0]
    assert source_key == SOURCE
    assert payload["expected_revision"] == 4
    assert payload["refresh_interval_minutes"] == 1440
    assert payload["review_acknowledged"] is True
    assert payload["seed_url"] == FIRST_SEED
    assert payload["page_limit"] == 10


def test_source_save_requires_valid_bounds_endpoint_and_approved_origins(source_page) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    start_edit(panel, "12")
    save = panel.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_enabled()
    for label, invalid, valid in [
        ("Page limit", ["", "0", "501", "1.5"], "12"),
        ("Refresh interval · min", ["4", "1441", "5.5"], "360"),
        ("Minimum pacing · ms", ["249", "60001", "250.5"], "1000"),
    ]:
        field = panel.get_by_role("spinbutton", name=label, exact=True)
        for value in invalid:
            field.fill(value)
            expect(save).to_be_disabled()
        field.fill(valid)
        expect(save).to_be_enabled()
    endpoint = panel.get_by_role("textbox", name="Seed URL", exact=True)
    for value in ["", "http://example.test/events", "https://unapproved.example.test/events"]:
        endpoint.fill(value)
        expect(save).to_be_disabled()
    endpoint.fill(FIRST_SEED)
    origins = panel.get_by_role("textbox", name="Approved HTTPS origins · one per line", exact=True)
    for value in [
        "",
        "http://example.test",
        "https://unapproved.example.test",
        "https://example.test\nhttps://example.test",
    ]:
        origins.fill(value)
        expect(save).to_be_disabled()
    origins.fill("https://example.test")
    expect(save).to_be_enabled()
    panel.get_by_role("button", name="Cancel", exact=True).click()
    expect(save).to_be_disabled()
    assert not scenario.writes


@pytest.mark.parametrize("revision", [0, None])
def test_source_configuration_cannot_edit_without_verified_revision(source_page, revision) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    scenario.config[SOURCE]["source"]["source_revision"] = revision
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    expect(
        panel.get_by_text("A verified registry revision is required before saving.", exact=True)
    ).to_be_visible()
    expect(panel.get_by_role("textbox", name="Seed URL", exact=True)).to_be_disabled()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_be_disabled()
    expect(panel.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    assert not scenario.writes


def test_source_event_window_presets_enforce_bounds_and_save_reviewed_value(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    expect(panel.get_by_role("spinbutton", name="Event window · days", exact=True)).to_have_value(
        "90"
    )
    days = panel.get_by_role("spinbutton", name="Event window · days", exact=True)
    expect(days).to_be_enabled()
    expect(days).to_have_value("90")
    presets = panel.get_by_role("group", name="Event window presets", exact=True)
    presets.get_by_role("button", name="60 days", exact=True).click()
    expect(days).to_have_value("60")
    save = panel.get_by_role("button", name="Save", exact=True)
    for invalid in ["0", "91", "1.5", ""]:
        days.fill(invalid)
        expect(save).to_be_disabled()
    days.fill("1")
    expect(save).to_be_enabled()
    presets.get_by_role("button", name="90 days", exact=True).click()
    expect(save).to_be_disabled()  # Restoring the recorded value creates no revision.
    presets.get_by_role("button", name="30 days", exact=True).click()
    expect(save).to_be_enabled()
    save.click()
    expect(panel.get_by_text("registry.rev/5", exact=True)).to_be_visible()
    expect(panel.get_by_role("spinbutton", name="Event window · days", exact=True)).to_have_value(
        "30"
    )
    assert len(scenario.writes) == 1
    assert scenario.writes[0][1]["collection_horizon_days"] == 30
    assert scenario.writes[0][1]["expected_revision"] == 4


def test_source_legacy_window_stays_unknown_and_is_not_silently_sent(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    scenario.config[SOURCE]["source"].pop("collection_horizon_days")
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    expect(panel.get_by_role("button", name="Edit event window", exact=True)).to_have_count(0)
    days = panel.get_by_role("spinbutton", name="Event window · days", exact=True)
    expect(days).to_be_disabled()
    expect(days).to_have_value("")
    presets = panel.get_by_role("group", name="Event window presets", exact=True)
    for label in ["30 days", "60 days", "90 days"]:
        expect(presets.get_by_role("button", name=label, exact=True)).to_be_disabled()
    panel.get_by_role("spinbutton", name="Page limit", exact=True).fill("12")
    panel.get_by_role("button", name="Save", exact=True).click()
    expect(panel.get_by_text("registry.rev/5", exact=True)).to_be_visible()
    expect(days).to_have_value("")
    expect(days).to_be_disabled()
    assert len(scenario.writes) == 1
    assert "collection_horizon_days" not in scenario.writes[0][1]
    assert "collection_horizon_days" not in scenario.config[SOURCE]["source"]


def test_source_reviewer_save_confirms_review_and_uses_verified_revision(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    queue = page.get_by_role("region", name=f"Source details for {SOURCE}", exact=True).get_by_role(
        "button", name="Queue refresh", exact=True
    )
    expect(queue).to_be_enabled()
    save = panel.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_disabled()
    expect(
        panel.get_by_text(
            "Save confirms review of the endpoint, origins and collection settings.", exact=True
        )
    ).to_be_visible()
    start_edit(panel, "12")
    expect(save).to_be_enabled()
    expect(queue).to_be_disabled()
    save.click()
    expect(panel.get_by_text("registry.rev/5", exact=True)).to_be_visible()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("12")
    expect(save).to_be_disabled()
    assert len(scenario.writes) == 1
    source_key, payload = scenario.writes[0]
    assert source_key == SOURCE
    assert payload["expected_revision"] == 4
    assert payload["page_limit"] == 12
    assert payload["review_acknowledged"] is True
    assert payload["mode"] == "public_jsonld"
    assert payload["handoff_only"] is True
    start_edit(panel, "13")
    expect(save).to_be_enabled()
    panel.get_by_role("button", name="Cancel", exact=True).click()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("12")
    assert len(scenario.writes) == 1


def test_source_revision_conflict_preserves_draft_without_false_success(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    start_edit(panel, "17")
    scenario.config[SOURCE]["source"]["source_revision"] = 5
    panel.get_by_role("button", name="Save", exact=True).click()
    expect(
        panel.get_by_text(re.compile("This source changed while you were editing"))
    ).to_be_visible()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("17")
    assert scenario.writes[0][1]["expected_revision"] == 4
    assert scenario.config[SOURCE]["source"]["page_limit"] == 10
    page.get_by_role("button", name="Refresh source details", exact=True).click()
    expect(panel.get_by_text(re.compile("Your draft still uses revision 4"))).to_be_visible()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("17")
    expect(panel.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    panel.get_by_role("button", name="Cancel", exact=True).click()
    expect(panel.get_by_text("registry.rev/5", exact=True)).to_be_visible()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("10")


def test_source_late_save_response_does_not_replace_second_source_draft(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    scenario.hold_save = True
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    start_edit(panel, "12")
    with page.expect_request(
        lambda request: (
            request.method == "PATCH"
            and urlsplit(request.url).path == f"/admin/v1/ingestion/sources/{SOURCE}"
        )
    ):
        panel.get_by_role("button", name="Save", exact=True).click()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_be_disabled()
    expect(panel.get_by_role("textbox", name="Seed URL", exact=True)).to_be_disabled()

    details = page.get_by_role("region", name=f"Source details for {SOURCE}", exact=True)
    expect(details.get_by_role("button", name="Close source details", exact=True)).to_be_disabled()
    expect(details.get_by_role("button", name="Overview", exact=True)).to_be_disabled()
    expect(page.get_by_role("textbox", name="Search sources", exact=True)).to_be_disabled()
    # Native history remains available while a request is pending. Its late receipt
    # must not overwrite a draft in the newly selected source.
    page.go_back()
    expect(panel).to_have_count(0)
    open_configuration(page, "Bay Arts 02")
    expect(panel.get_by_role("textbox", name="Seed URL", exact=True)).to_have_value(SECOND_SEED)
    start_edit(panel, "23")
    assert scenario.held_save is not None
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlsplit(response.url).path == f"/admin/v1/ingestion/sources/{SOURCE}"
        )
    ):
        scenario.save(scenario.held_save, SOURCE)
    assert scenario.writes[0][0] == SOURCE
    assert scenario.config[SOURCE]["source"]["page_limit"] == 12
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("23")
    expect(panel.get_by_role("textbox", name="Seed URL", exact=True)).to_have_value(SECOND_SEED)
    expect(panel.get_by_role("button", name="Save", exact=True)).to_be_enabled()
    assert parse_qs(urlsplit(page.url).query)["source_selection"] == [SECOND_SOURCE]
    assert scenario.config[SECOND_SOURCE]["source"]["page_limit"] == 20


def test_source_switch_failed_read_never_reuses_editable_configuration(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    start_edit(panel, "17")
    scenario.status_overrides[f"/admin/v1/ingestion/sources/{SECOND_SOURCE}"] = 503
    open_configuration(page, "Bay Arts 02")
    expect(panel.get_by_role("button", name="Retry", exact=True)).to_be_visible()
    expect(panel.get_by_role("button", name="Save", exact=True)).to_have_count(0)
    expect(panel.get_by_text(FIRST_SEED, exact=True)).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query)["source_selection"] == [SECOND_SOURCE]
    scenario.status_overrides.clear()
    page.get_by_role("region", name=f"Source details for {SECOND_SOURCE}", exact=True).get_by_role(
        "button", name="Retry source details", exact=True
    ).click()
    expect(panel.get_by_role("textbox", name="Seed URL", exact=True)).to_have_value(SECOND_SEED)
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("20")
    assert not scenario.writes


def test_pending_save_finishes_when_a_late_roster_read_moves_its_source_row(source_page) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    start_edit(panel, "12")
    scenario.hold_source_pages.add(("", 0))
    with page.expect_request(
        lambda request: urlsplit(request.url).path == "/admin/v1/ingestion/sources"
    ):
        page.get_by_role("button", name="Refresh", exact=True).click()
    save = panel.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_enabled()
    scenario.hold_save = True
    # The source table dims pointer targets during its independent roster read;
    # the verified editor remains available to keyboard users.
    save.focus()
    with page.expect_request(lambda request: request.method == "PATCH"):
        save.press("Enter")
    scenario.hidden_source_keys.add(SOURCE)
    scenario.release_source_page(("", 0))
    expect(page.get_by_role("button", name="Bay Arts 01", exact=True)).to_have_count(0)
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("12")
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_be_disabled()
    expect(panel.get_by_role("button", name="Cancel", exact=True)).to_be_disabled()
    assert scenario.held_save is not None
    scenario.save(scenario.held_save, SOURCE)
    expect(panel.get_by_text("registry.rev/5", exact=True)).to_be_visible()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("12")
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_be_enabled()
    expect(save).to_be_disabled()
    start_edit(panel, "13")
    expect(save).to_be_enabled()
    assert len(scenario.writes) == 1


@pytest.mark.parametrize("status", [503, 403])
def test_source_failed_refresh_has_no_stale_editable_config(
    source_page: tuple[Page, SourceConfigurationApi, str], status: int
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    start_edit(panel, "17")
    scenario.status_overrides[f"/admin/v1/ingestion/sources/{SOURCE}"] = status
    page.get_by_role("button", name="Refresh source details", exact=True).click()
    expect(panel.get_by_role("button", name="Edit reviewed config", exact=True)).to_have_count(0)
    if status == 403:
        expect(panel).to_have_count(0)
        expect(page.get_by_role("button", name="Retry source details", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Save", exact=True)).to_have_count(0)
        expect(page.get_by_text(FIRST_SEED, exact=True)).to_have_count(0)
    else:
        expect(panel.get_by_role("button", name="Retry", exact=True)).to_be_visible()
        expect(panel.get_by_role("button", name="Save", exact=True)).to_be_disabled()
        expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("17")
    expect(page.get_by_role("button", name="Configuration for Bay Arts 01")).to_be_visible()
    assert not scenario.writes


@pytest.mark.parametrize("width", [390, 1024, 1440])
def test_source_configuration_fits_expanded_row_and_restores_history(
    source_page: tuple[Page, SourceConfigurationApi, str],
    width: int,
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    expect(page.get_by_role("complementary", name="Source configuration")).to_have_count(0)
    expanded = panel.locator("xpath=ancestor::tr[1]")
    expect(expanded).to_have_attribute("data-testid", "source-expanded-row")
    expect(
        expanded.locator("xpath=preceding-sibling::tr[1]").get_by_role(
            "button", name="Bay Arts 01", exact=True
        )
    ).to_be_visible()
    assert expanded.evaluate(
        "row => row.firstElementChild.colSpan === row.closest('table').querySelectorAll('thead th').length"
    )
    start_edit(panel)
    assert page.evaluate(
        "Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) <= document.documentElement.clientWidth"
    )
    if width == 390:
        assert panel.evaluate("""panel => {
            const bounds = panel.getBoundingClientRect();
            return bounds.left >= 0 && bounds.right <= document.documentElement.clientWidth
                && [...panel.querySelectorAll('input, select, textarea')].filter(field => field.getClientRects().length)
                    .every(field => field.getBoundingClientRect().right <= bounds.right + 1);
        }"""), "The canonical editor and fields must fit the visible mobile column."
    page.go_back()
    expect(panel).to_have_count(0)
    page.go_forward()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("10")
    assert not scenario.writes


@pytest.mark.parametrize("legacy", ["source", "catalog_config"])
def test_legacy_source_bookmarks_reveal_the_canonical_source_and_clear_conflicting_filters(
    source_page: tuple[Page, SourceConfigurationApi, str],
    legacy: str,
) -> None:
    page, _, base = source_page
    page.goto(
        f"{base}/admin?tab=catalog&{legacy}={SOURCE}&registry_query=unrelated&registry_state=blocked&registry_publisher=Other&registry_lens=paused"
    )
    details = page.get_by_role("region", name=f"Source details for {SOURCE}", exact=True)
    expect(details).to_be_visible()
    expect(page.get_by_role("textbox", name="Search sources", exact=True)).to_have_value("")
    expect(page.get_by_role("button", name="Bay Arts 01", exact=True)).to_have_attribute(
        "aria-expanded", "true"
    )
    params = parse_qs(urlsplit(page.url).query)
    assert params["tab"] == ["sources"]
    assert params["source_selection"] == [SOURCE]
    assert all(
        key not in params
        for key in [
            "source",
            "catalog_config",
            "registry_query",
            "registry_state",
            "registry_publisher",
            "registry_lens",
        ]
    )
    perspective = "Configuration" if legacy == "catalog_config" else "Overview"
    expect(
        details.get_by_role("group", name="Source detail perspective").get_by_role(
            "button", name=perspective, exact=True
        )
    ).to_have_attribute("aria-pressed", "true")
    page.reload()
    expect(details).to_be_visible()
    expect(
        page.get_by_role("button", name=re.compile(r"^(Open full|Full) source workspace$"))
    ).to_have_count(0)


def test_source_missing_roster_bookmark_keeps_exact_configuration_retry_visible(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    missing = "bay-arts-99"
    scenario.config[missing] = configuration(missing)
    scenario.hidden_source_keys.add(missing)
    scenario.status_overrides[f"/admin/v1/ingestion/sources/{missing}"] = 404
    page.goto(f"{base}/admin?tab=sources&source_selection={missing}&source_inspector=configuration")
    panel = inspector(page)
    expect(panel.get_by_role("button", name="Retry", exact=True)).to_be_visible()
    expect(panel.get_by_role("button", name="Edit reviewed config", exact=True)).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query)["source_selection"] == [missing]
    page.reload()
    expect(panel.get_by_role("button", name="Retry", exact=True)).to_be_visible()
    scenario.status_overrides.clear()
    page.get_by_role("region", name=f"Source details for {missing}", exact=True).get_by_role(
        "button", name="Refresh source details", exact=True
    ).click()
    expect(page.get_by_role("region", name=f"Source details for {missing}", exact=True).get_by_text(missing, exact=True)).to_be_visible()
    expect(panel.get_by_text(FIRST_SEED, exact=True)).to_be_visible()
    assert not scenario.writes


@pytest.mark.parametrize("from_records", [False, True])
def test_catalog_manage_source_uses_the_same_sources_editor_and_back_keeps_catalog_scope(
    source_page: tuple[Page, SourceConfigurationApi, str],
    from_records: bool,
) -> None:
    page, _, base = source_page
    suffix = (
        f"&store_source={SOURCE}&store_query=music" if from_records else "&store_source_query=Bay"
    )
    page.goto(f"{base}/admin?tab=catalog{suffix}")
    expect(inspector(page)).to_have_count(0)
    page.get_by_role(
        "button",
        name="Manage source" if from_records else "Manage source for Bay Arts 01",
        exact=True,
    ).click()
    panel = inspector(page)
    expect(panel.get_by_text(FIRST_SEED, exact=True)).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["tab"] == ["sources"]
    assert params["source_selection"] == [SOURCE]
    assert params["source_inspector"] == ["configuration"]
    page.go_back()
    expect(page.get_by_role("heading", name="Catalog", exact=True)).to_be_visible()
    expect(inspector(page)).to_have_count(0)
    if from_records:
        expect(
            page.get_by_role("textbox", name=re.compile(r"^Search parsed source events\b"))
        ).to_have_value("music")
    else:
        expect(page.get_by_role("textbox", name="Find a catalog source", exact=True)).to_have_value(
            "Bay"
        )


def test_source_bookmark_editor_does_not_move_when_the_initial_roster_arrives(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    scenario.hold_source_pages.add(("", 0))
    with page.expect_request(
        lambda request: urlsplit(request.url).path == "/admin/v1/ingestion/sources"
    ):
        page.goto(
            f"{base}/admin?tab=sources&source_selection={SOURCE}&source_inspector=configuration"
        )
    expect(
        page.get_by_role("region", name="Source registry charts", exact=True).get_by_text(
            "Reading matching sources…", exact=True
        )
    ).to_be_visible()
    expect(page.get_by_role("button", name="Edit reviewed config", exact=True)).to_have_count(0)
    scenario.release_source_page(("", 0))
    panel = inspector(page)
    expect(panel.get_by_role("textbox", name="Seed URL", exact=True)).to_have_value(FIRST_SEED)
    start_edit(panel, "17")
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/sources" and response.status == 200
        )
    ):
        page.get_by_role("button", name="Refresh", exact=True).click()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("17")
    expect(panel.get_by_role("button", name="Save", exact=True)).to_be_enabled()
    assert not scenario.writes


def test_source_roster_recovery_preserves_the_bookmark_fallback_draft(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    scenario.status_overrides["/admin/v1/ingestion/sources"] = 503
    page.goto(f"{base}/admin?tab=sources&source_selection={SOURCE}&source_inspector=configuration")
    panel = inspector(page)
    expect(panel.get_by_role("textbox", name="Seed URL", exact=True)).to_have_value(FIRST_SEED)
    start_edit(panel, "17")
    scenario.status_overrides.clear()
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/sources" and response.status == 200
        )
    ):
        page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("button", name="Bay Arts 01", exact=True)).to_be_visible()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("17")
    expect(panel.get_by_role("button", name="Save", exact=True)).to_be_enabled()
    assert not scenario.writes


def test_source_draft_survives_a_refresh_that_omits_its_roster_row(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    panel = open_configuration(page)
    start_edit(panel, "17")
    scenario.hidden_source_keys.add(SOURCE)
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/sources" and response.status == 200
        )
    ):
        page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("button", name="Bay Arts 01", exact=True)).to_have_count(0)
    expect(panel.locator("xpath=ancestor::tr[1]")).to_have_attribute(
        "data-testid", "source-expanded-row"
    )
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("17")
    expect(panel.get_by_role("button", name="Save", exact=True)).to_be_enabled()
    scenario.hidden_source_keys.clear()
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/sources" and response.status == 200
        )
    ):
        page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("button", name="Bay Arts 01", exact=True)).to_be_visible()
    expect(panel.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("17")
    assert not scenario.writes


def test_source_refresh_receipt_tracks_the_exact_accepted_command(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, scenario, base = source_page
    scenario.role = "operator"
    scenario.allow_refresh = True
    page.goto(f"{base}/admin?tab=sources&source_selection={SOURCE}")
    details = page.get_by_role("region", name=f"Source details for {SOURCE}", exact=True)
    queue = details.get_by_role("button", name="Queue refresh", exact=True)
    expect(queue).to_be_enabled()
    queue.click()
    expect(
        page.get_by_text("Bay Arts 01 refresh accepted into the durable queue.", exact=True)
    ).to_be_visible()
    assert len(scenario.refresh_writes) == 1
    page.get_by_role("button", name="Track command", exact=True).click()
    expect(page.get_by_role("region", name="Command investigation", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["command"] == [ACCEPTED]
    assert any(path.endswith(f"/commands/{ACCEPTED}") for _, path, _ in scenario.command_api.calls)


@pytest.mark.parametrize(
    "restriction", ["viewer", "paused", "unreviewed", "running", "retired", "policy"]
)
def test_source_refresh_honors_eligibility_without_sending_a_command(
    source_page: tuple[Page, SourceConfigurationApi, str],
    restriction: str,
) -> None:
    page, scenario, base = source_page
    scenario.role = "viewer" if restriction == "viewer" else "reviewer"
    source = scenario.config[SOURCE]["source"]
    if restriction == "paused":
        source["enabled"] = False
    elif restriction == "unreviewed":
        source["review_status"] = "unreviewed"
    elif restriction == "running":
        source["latest_run"]["status"] = "running"
    elif restriction == "retired":
        source["retired_at"] = STAMP
    elif restriction == "policy":
        source["policy_blocked"] = True
    page.goto(f"{base}/admin?tab=sources&source_selection={SOURCE}")
    details = page.get_by_role("region", name=f"Source details for {SOURCE}", exact=True)
    expect(
        details.get_by_role("region", name="Source collection schedule", exact=True)
    ).to_be_visible()
    queue = details.get_by_role("button", name="Queue refresh", exact=True)
    if restriction == "retired":
        expect(queue).to_have_count(0)
    else:
        expect(queue).to_be_disabled()
    assert not scenario.refresh_writes


def test_retired_review_preview_url_opens_the_current_pipeline(
    source_page: tuple[Page, SourceConfigurationApi, str],
) -> None:
    page, _, base = source_page
    page.goto(f"{base}/admin/review")
    expect(page.get_by_role("heading", name="Collection pipeline", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["tab"] == ["pipeline"]
