"""Hermetic, product-level browser coverage for the consumer experience."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.browser_e2e

_TENANT_ID = "11111111-1111-4111-8111-111111111111"
_EVENT_ID = "22222222-2222-4222-8222-222222222222"
_REQUEST_ID = "33333333-3333-4333-8333-333333333333"
_DURABLE_REQUEST_ID = "44444444-4444-4444-8444-444444444444"
_TRANSPARENT_MAP_TILE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _feed(request_id: str = _REQUEST_ID) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "items": [
            {
                "canonical_event_id": _EVENT_ID,
                "title": "Friday Night Jazz",
                "start_at": "2030-06-14T19:30:00-07:00",
                "end_at": "2030-06-14T21:30:00-07:00",
                "venue_name": "The Lakehouse",
                "city": "Oakland",
                "description": "A free lakeside quartet with an early-evening set.",
                "price_status": "free",
                "event_status": "scheduled",
                "score": 0.94,
                "rationale": "Free live music at the time you asked for.",
                "conflict": "clear",
                "lanes": ["api"],
                "latitude": 37.8044,
                "longitude": -122.2712,
                "registerable": True,
                "registration_urls": ["https://events.example.test/friday-jazz"],
                "sources": [
                    {
                        "source": "public_jsonld",
                        "registration_url": "https://events.example.test/friday-jazz",
                    }
                ],
            }
        ],
        "next_cursor": None,
        "understood": {
            "categories": ["live music"],
            "free_only": True,
            "window_start": "2030-06-14T17:00:00-07:00",
            "window_end": "2030-06-14T23:00:00-07:00",
            "radius_km": 16,
        },
    }


def _rich_feed() -> dict[str, Any]:
    payload = _feed()
    base = payload["items"][0]
    payload["items"] = [
        {
            **base,
            "canonical_event_id": "55555555-5555-4555-8555-555555555555",
            "title": "Luma Design Salon",
            "start_at": "2030-06-10T18:00:00-07:00",
            "end_at": "2030-06-10T20:00:00-07:00",
            "venue_name": "Design District Studio",
            "city": "San Francisco",
            "description": "A design salon listed on Luma.",
            "price_status": "paid",
            "score": 0.81,
            "rationale": "A design gathering from the current catalog.",
            "latitude": 37.7749,
            "longitude": -122.4194,
            "registration_urls": ["https://events.luma.com/design-salon"],
            "sources": [
                {
                    "source": "public_jsonld",
                    "registration_url": "https://events.luma.com/design-salon",
                }
            ],
        },
        {
            **base,
            "canonical_event_id": "66666666-6666-4666-8666-666666666666",
            "title": "Oakland Builders Meetup",
            "start_at": "2030-06-12T18:30:00-07:00",
            "end_at": "2030-06-12T20:30:00-07:00",
            "venue_name": "Jack London Workspace",
            "city": "Oakland",
            "description": "A community technology meetup.",
            "price_status": "unknown",
            "score": 0.88,
            "rationale": "A local technology gathering.",
            "latitude": 37.7955,
            "longitude": -122.278,
            "registration_urls": ["https://www.meetup.com/oakland-builders/events/123"],
            "sources": [
                {
                    "source": "meetup",
                    "registration_url": "https://www.meetup.com/oakland-builders/events/123",
                }
            ],
        },
        base,
    ]
    return payload


def _luma_catalog_event(
    event_id: str,
    title: str,
    start_at: str,
    *,
    latitude: float | None,
    longitude: float | None,
) -> dict[str, Any]:
    event = json.loads(json.dumps(_rich_feed()["items"][0]))
    event.update(
        {
            "canonical_event_id": event_id,
            "title": title,
            "start_at": start_at,
            "end_at": None,
            "latitude": latitude,
            "longitude": longitude,
            "score": None,
            "rationale": "",
            "conflict": "not_evaluated",
            "registerable": None,
            "registration_urls": [f"https://luma.com/{event_id[:8]}"],
            "sources": [
                {
                    "source": "public_jsonld",
                    "source_key": "luma-genai-sf",
                    "registration_url": f"https://luma.com/{event_id[:8]}",
                }
            ],
        }
    )
    return event


def _registration() -> dict[str, Any]:
    return {
        "canonical_event_id": _EVENT_ID,
        "title": "Friday Night Jazz",
        "start_at": "2030-06-14T19:30:00-07:00",
        "end_at": "2030-06-14T21:30:00-07:00",
        "venue_name": "The Lakehouse",
        "city": "Oakland",
        "description": "A free lakeside quartet.",
        "price_status": "free",
        "event_status": "scheduled",
        "state": "scheduled",
        "lane": "autonomous_sla",
        "source": "public_jsonld",
        "conflict_warning": False,
        "registration_url": "https://events.example.test/friday-jazz",
        "updated_at": "2030-06-01T12:00:00Z",
        "can_withdraw": True,
    }


def _task() -> dict[str, Any]:
    return {
        "task_id": "handoff:friday-jazz",
        "canonical_event_id": _EVENT_ID,
        "event_summary": "Friday Night Jazz",
        "title": "Friday Night Jazz",
        "start_at": "2030-06-14T19:30:00-07:00",
        "venue_name": "The Lakehouse",
        "city": "Oakland",
        "reason": "identity_wall",
        "state": "open",
        "deep_link": "https://events.example.test/friday-jazz/finish",
        "expires_at": "2030-06-10T12:00:00Z",
        "created_at": "2030-06-01T12:00:00Z",
    }


@dataclass(slots=True)
class ApiCall:
    method: str
    path: str
    body: Any
    headers: dict[str, str]
    query: str = ""


@dataclass(slots=True)
class ApiScenario:
    calls: list[ApiCall] = field(default_factory=list)
    interests: list[str] = field(default_factory=lambda: ["live music"])
    preference_revision: int = 7
    requests: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "request_id": _REQUEST_ID,
                "text": "A free concert this month",
                "state": "started",
                "created_at": "2030-06-01T12:00:00Z",
                "categories": ["live music"],
                "free_only": True,
                "window_start": None,
                "window_end": None,
                "outcome": {
                    "canonical_event_id": _EVENT_ID,
                    "title": "Friday Night Jazz",
                    "start_at": "2030-06-14T19:30:00-07:00",
                    "state": "scheduled",
                    "lane": "autonomous_sla",
                    "source": "public_jsonld",
                    "updated_at": "2030-06-02T12:00:00Z",
                },
            }
        ]
    )
    registrations: list[dict[str, Any]] = field(default_factory=lambda: [_registration()])
    tasks: list[dict[str, Any]] = field(default_factory=lambda: [_task()])
    feed_failures_remaining: int = 0
    task_failures_remaining: int = 0
    request_failures_remaining: int = 0
    request_successes: int = 0
    resolve_request_after_successes: int | None = None
    feed_payload: dict[str, Any] | None = None
    catalog_payloads: dict[tuple[str | None, str | None], dict[str, Any]] | None = None
    catalog_unavailable: bool = False
    hold_next_paths: set[str] = field(default_factory=set)
    held_responses: list[tuple[Route, str, dict[str, Any]]] = field(default_factory=list)
    hold_next_preference_update: bool = False
    held_preference_responses: list[tuple[Route, dict[str, Any]]] = field(
        default_factory=list
    )

    def install(self, page: Page) -> None:
        def handler(route: Route) -> None:
            self._handle(route)

        page.route("**/readyz", handler)
        page.route("**/v1/**", handler)

    def count(self, method: str, path: str) -> int:
        return sum(call.method == method and call.path == path for call in self.calls)

    def last(self, method: str, path: str) -> ApiCall:
        return next(
            call for call in reversed(self.calls) if call.method == method and call.path == path
        )

    def release_held_responses(
        self, *, status_by_path: dict[str, int] | None = None
    ) -> None:
        held = list(self.held_responses)
        self.held_responses.clear()
        for route, path, payload in held:
            self._json(
                route,
                payload,
                status=(status_by_path or {}).get(path, 200),
            )

    def release_held_preference_responses(self) -> None:
        held = list(self.held_preference_responses)
        self.held_preference_responses.clear()
        for route, payload in held:
            self._json(route, payload)

    def _handle(self, route: Route) -> None:  # noqa: PLR0911, PLR0912, PLR0915
        request = route.request
        parsed = urlsplit(request.url)
        method = request.method
        body = request.post_data_json if request.post_data else None
        self.calls.append(ApiCall(method, parsed.path, body, request.headers, parsed.query))

        if method == "GET" and parsed.path in self.hold_next_paths:
            self.hold_next_paths.remove(parsed.path)
            payloads = {
                "/v1/requests": {"items": self.requests, "next_cursor": None},
                "/v1/registrations": {"items": self.registrations, "next_cursor": None},
                "/v1/tasks": {"items": self.tasks, "next_cursor": None},
            }
            payload = json.loads(json.dumps(payloads[parsed.path]))
            self.held_responses.append((route, parsed.path, payload))
            return

        if method == "GET" and parsed.path == "/readyz":
            self._json(
                route,
                {
                    "status": "ready",
                    "components": {"database": "ready", "temporal": "degraded"},
                },
            )
            return
        if method == "GET" and parsed.path == "/v1/ui-config":
            self._json(
                route,
                {
                    "product_name": "Events Concierge",
                    "local_demo": True,
                    "auth_mode": "local_demo",
                    "auth_start_url": None,
                    "reauth_url": None,
                    "logout_url": None,
                    "csrf_cookie_name": None,
                    "csrf_header_name": None,
                },
            )
            return
        if method == "POST" and parsed.path == "/v1/onboard":
            self._json(
                route,
                {"tenant_id": _TENANT_ID, "relay_inbox": "fixture@u.concierge.test"},
                status=201,
            )
            return
        if method == "GET" and parsed.path == "/v1/me":
            self._json(
                route,
                {
                    "notify_email": "alex@example.test",
                    "interests": self.interests,
                    "preference_revision": self.preference_revision,
                    "local_demo": True,
                },
            )
            return
        if method == "PUT" and parsed.path == "/v1/preferences":
            requested_interests = list(body["interests"])
            requested_revision = int(body["revision"])
            payload = {
                "status": "updated",
                "interests": requested_interests,
                "revision": requested_revision,
            }
            if self.hold_next_preference_update:
                self.hold_next_preference_update = False
                self.held_preference_responses.append((route, payload))
                return
            self.interests = requested_interests
            self.preference_revision = requested_revision
            self._json(
                route,
                payload,
            )
            return
        if method == "GET" and parsed.path == "/v1/requests":
            if self.request_failures_remaining:
                self.request_failures_remaining -= 1
                self._json(route, {"detail": "Request status is briefly unavailable."}, status=503)
                return
            self.request_successes += 1
            if (
                self.resolve_request_after_successes is not None
                and self.request_successes >= self.resolve_request_after_successes
                and self.requests
                and self.requests[0].get("outcome") is None
            ):
                self.requests[0]["outcome"] = {
                    "canonical_event_id": _EVENT_ID,
                    "title": "Friday Night Jazz",
                    "start_at": "2030-06-14T19:30:00-07:00",
                    "state": "scheduled",
                    "lane": "autonomous_sla",
                    "source": "public_jsonld",
                    "updated_at": "2030-06-02T12:05:00Z",
                }
            self._json(route, {"items": self.requests, "next_cursor": None})
            return
        if method == "GET" and parsed.path == "/v1/registrations":
            self._json(route, {"items": self.registrations, "next_cursor": None})
            return
        if method == "GET" and parsed.path == "/v1/tasks":
            self._json(route, {"items": self.tasks, "next_cursor": None})
            return
        if method == "GET" and parsed.path == "/v1/catalog/events":
            if self.catalog_unavailable:
                self._json(route, {"detail": "Catalog browser endpoint is unavailable."}, status=404)
                return
            query = parse_qs(parsed.query)
            source_key = query.get("source_key", [None])[0]
            cursor = query.get("cursor", [None])[0]
            if self.catalog_payloads is None:
                feed = self.feed_payload if self.feed_payload is not None else _feed()
                self._json(
                    route,
                    {
                        "items": feed["items"],
                        "next_cursor": feed["next_cursor"],
                        "providers": [],
                    },
                )
                return
            payload = self.catalog_payloads.get((source_key, cursor))
            if payload is None:
                self._json(
                    route,
                    {
                        "detail": (
                            "unexpected catalog page: "
                            f"source_key={source_key!r}, cursor={cursor!r}"
                        )
                    },
                    status=500,
                )
                return
            self._json(route, payload)
            return
        if method == "POST" and parsed.path == "/v1/feed":
            if self.feed_failures_remaining:
                self.feed_failures_remaining -= 1
                self._json(route, {"detail": "Fixture catalog is briefly unavailable."}, status=503)
            else:
                self._json(route, self.feed_payload if self.feed_payload is not None else _feed())
            return
        if method == "POST" and parsed.path == "/v1/requests":
            durable = {
                "request_id": _DURABLE_REQUEST_ID,
                "text": body["text"],
                "state": "received",
                "created_at": "2030-06-02T12:00:00Z",
                "categories": ["live music"],
                "free_only": False,
                "window_start": None,
                "window_end": None,
                "outcome": None,
            }
            self.requests.insert(0, durable)
            self._json(
                route,
                {
                    "request_id": _DURABLE_REQUEST_ID,
                    "workflow_started": False,
                    "feed": (
                        self.feed_payload
                        if self.feed_payload is not None
                        else _feed(_DURABLE_REQUEST_ID)
                    ),
                },
            )
            return
        if method == "POST" and parsed.path.startswith("/v1/me/tasks/"):
            if self.task_failures_remaining:
                self.task_failures_remaining -= 1
                self._json(route, {"detail": "Lifecycle verifier is recovering."}, status=503)
            else:
                self._json(route, {"status": "accepted"}, status=202)
            return
        if method == "POST" and parsed.path == "/v1/feed-feedback":
            self._json(route, {"status": "accepted"}, status=202)
            return
        if method == "POST" and parsed.path == "/v1/unrsvp":
            self._json(route, {"status": "accepted", "request_id": body["request_id"]}, status=202)
            return
        if method == "POST" and parsed.path == "/v1/me/erasure-requests":
            self._json(
                route,
                {
                    "request_id": body["request_id"],
                    "status": "pending",
                    "workflow_targets": 1,
                    "workflows_cancelled": 0,
                    "calendar_targets": 1,
                    "calendar_deleted": 0,
                    "browser_sessions_revoked": False,
                    "credential_vault_purged": False,
                    "object_store_purged": False,
                    "retained_audit_rows": 0,
                    "failed_stage": None,
                },
                status=202,
            )
            return
        self._json(
            route, {"detail": f"unexpected fixture request: {method} {parsed.path}"}, status=500
        )

    @staticmethod
    def _json(route: Route, payload: Any, *, status: int = 200) -> None:
        route.fulfill(
            status=status,
            content_type="application/json; charset=utf-8",
            body=json.dumps(payload),
        )


def _open(page: Page, scenario: ApiScenario, product_server: str):
    scenario.install(page)
    return page.goto(product_server, wait_until="load")


def _open_authenticated(page: Page, scenario: ApiScenario, product_server: str):
    response = _open(page, scenario, product_server)
    expect(page.locator("#product-shell")).to_be_visible()
    expect(page.locator("#recent-requests .recent-request")).to_have_count(len(scenario.requests))
    return response


def _stub_map_tiles(page: Page) -> None:
    page.route(
        "https://tile.openstreetmap.org/**",
        lambda route: route.fulfill(
            status=200,
            content_type="image/png",
            body=_TRANSPARENT_MAP_TILE,
        ),
    )


def _assert_no_horizontal_overflow(page: Page) -> None:
    measurements = page.evaluate(
        """
        () => {
          const root = document.documentElement;
          const main = document.querySelector("#main-content");
          const active = document.querySelector("[data-page]:not([hidden])");
          return {
            root: root.scrollWidth - root.clientWidth,
            main: main.scrollWidth - main.clientWidth,
            active: active.scrollWidth - active.clientWidth,
          };
        }
        """
    )
    assert measurements == {"root": 0, "main": 0, "active": 0}


def _assert_map_workspace_is_responsive(page: Page, navigation) -> None:
    navigation.click()
    expect(page.locator("#map-workspace")).to_be_visible()
    expect(page.locator("#map-event-list .map-event-card")).to_have_count(3)
    expect(page.locator(".event-map-marker")).to_have_count(3)
    _assert_no_horizontal_overflow(page)
    measurements = page.evaluate(
        """
        () => {
          const workspace = document.querySelector("#map-workspace");
          const map = document.querySelector("#events-map");
          const shell = document.querySelector(".map-canvas-shell");
          const mapBox = map.getBoundingClientRect();
          const shellBox = shell.getBoundingClientRect();
          return {
            workspaceOverflow: workspace.scrollWidth - workspace.clientWidth,
            clipped: getComputedStyle(shell).overflow === "hidden",
            mapContained:
              mapBox.left >= shellBox.left - 1
              && mapBox.right <= shellBox.right + 1,
            mapHeight: mapBox.height,
          };
        }
        """
    )
    assert measurements["workspaceOverflow"] <= 1
    assert measurements["clipped"]
    assert measurements["mapContained"]
    assert measurements["mapHeight"] >= 300


def test_shell_onboarding_and_preview_vs_durable_boundary(  # noqa: PLR0915
    page_factory, product_server: str
) -> None:
    harness = page_factory()
    page = harness.page
    scenario = ApiScenario()

    response = _open(page, scenario, product_server)
    assert response is not None
    assert response.status == 200
    headers = response.all_headers()
    assert headers["cache-control"] == "no-cache, max-age=0"
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "connect-src 'self'" in headers["content-security-policy"]
    assert "object-src 'none'" in headers["content-security-policy"]

    script_response = page.context.request.get(f"{product_server}/assets/app.js")
    assert script_response.ok
    assert script_response.headers["x-content-type-options"] == "nosniff"
    assert script_response.headers["content-type"].startswith("text/javascript")

    expect(page.locator("#welcome-view")).to_be_visible()
    expect(page.get_by_role("heading", name="One brief. Something worth going to.")).to_be_visible()
    page.locator("#onboarding-email").fill("alex@example.test")
    page.locator("#onboarding-interests [data-interest='live music']").click()
    page.locator("#onboarding-form").get_by_role("button", name="Meet my concierge").click()

    expect(page.locator("#product-shell")).to_be_visible()
    expect(page.locator("#main-content")).to_be_focused()
    expect(page.locator("#service-status")).to_contain_text("Requests safely queued")
    expect(page.locator("#recent-requests")).to_contain_text("Friday Night Jazz")
    expect(page.locator("#recent-requests")).to_contain_text("On your calendar")
    assert scenario.count("POST", "/v1/onboard") == 1
    assert scenario.count("PUT", "/v1/preferences") == 1
    assert scenario.last("GET", "/v1/me").headers["x-ec-tenant-id"] == _TENANT_ID

    page.locator("#ask-input").fill("Free live music Friday after work")
    page.locator("#ask-submit").click()
    expect(
        page.locator("#picks-list").get_by_role("heading", name="Friday Night Jazz")
    ).to_be_visible()
    expect(page.locator("#request-progress")).to_be_hidden()
    assert scenario.count("POST", "/v1/feed") == 1
    assert scenario.last("POST", "/v1/feed").body == {
        "text": "Free live music Friday after work"
    }
    assert scenario.count("POST", "/v1/requests") == 0
    assert len(scenario.requests) == 1

    page.locator("input[name='ask-mode'][value='handle']").check()
    expect(page.locator("#ask-submit")).to_contain_text("Save and take action")
    page.locator("#ask-input").fill("Handle a jazz event for Friday")
    page.locator("#ask-submit").click()
    expect(page.locator("#request-progress")).to_be_visible()
    expect(page.locator("#request-progress-copy")).to_contain_text(
        "saved and will continue automatically"
    )
    expect(page.locator("#toast")).to_have_attribute("role", "status")
    expect(page.locator("#toast")).to_contain_text("Your brief is saved")
    expect(page.locator("#recent-requests")).to_contain_text("Handle a jazz event for Friday")
    assert scenario.count("POST", "/v1/requests") == 1
    assert scenario.last("POST", "/v1/requests").body == {"text": "Handle a jazz event for Friday"}

    page.reload(wait_until="load")
    expect(page.locator("#product-shell")).to_be_visible()
    assert scenario.count("POST", "/v1/onboard") == 1


def test_chat_and_catalog_list_preserve_provider_truth_and_filter_locally(  # noqa: PLR0915
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    page.clock.install(time="2030-06-02T12:00:00-07:00")
    scenario = ApiScenario(feed_payload=_rich_feed())
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='catalog']").click()
    expect(page).to_have_url(f"{product_server}/#/catalog")
    expect(page.locator("#catalog-workspace-tab")).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.locator("#browse-current-events")).to_be_visible()
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)
    expect(
        page.locator("#catalog-active-filters [data-active-filter='sort']")
    ).to_have_count(0)
    initial_headings = page.locator("#picks-list .pick-card h3")
    expect(initial_headings.nth(0)).to_have_text("Luma Design Salon")
    expect(initial_headings.nth(1)).to_have_text("Oakland Builders Meetup")
    expect(initial_headings.nth(2)).to_have_text("Friday Night Jazz")
    assert scenario.count("GET", "/v1/catalog/events") == 1
    assert parse_qs(scenario.last("GET", "/v1/catalog/events").query) == {"limit": ["50"]}
    assert scenario.count("POST", "/v1/feed") == 0
    assert scenario.count("POST", "/v1/requests") == 0
    expect(page.locator("#recommendation-messages .thread-message")).to_have_count(0)
    expect(page.locator("#recent-requests .recent-request")).to_have_count(1)
    expect(page.locator("#discovery-console")).to_be_hidden()
    expect(page.locator("#catalog-active-filters")).to_be_hidden()
    luma = page.locator(".pick-card").filter(has_text="Luma Design Salon")
    meetup = page.locator(".pick-card").filter(has_text="Oakland Builders Meetup")
    expect(luma).to_contain_text("Luma")
    expect(luma).to_contain_text("via Event site · public_jsonld")
    expect(meetup).to_contain_text("Meetup")
    expect(meetup).to_contain_text("Source record · meetup")
    expect(meetup.locator(".pick-price")).to_have_text("Price unknown")
    friday = page.locator(".pick-card").filter(has_text="Friday Night Jazz")
    expect(friday.locator(".pick-price")).to_have_text("Free")
    expect(page.locator("#picks-list")).not_to_contain_text("Price not listed")
    calendar_link = luma.locator(".pick-date.calendar-add-link")
    expect(calendar_link).to_have_attribute(
        "aria-label",
        "Add Luma Design Salon to Google Calendar (opens in new tab)",
    )
    expect(calendar_link).to_have_attribute(
        "href", re.compile(r"^https://calendar\.google\.com/calendar/render\?")
    )
    expect(luma).to_contain_text("Catalog rank 0.81")
    expect(luma).to_contain_text("Eligible to handle")
    expect(luma.locator("summary")).to_have_attribute(
        "aria-label", "View event details for Luma Design Salon"
    )
    expect(luma.get_by_role("link", name="Open event site for Luma Design Salon")).to_be_visible()
    location = luma.get_by_role(
        "link", name="Open Luma Design Salon location in Google Maps"
    )
    expect(location).to_be_visible()
    expect(location).to_have_attribute(
        "href",
        "https://www.google.com/maps/search/?api=1&query=37.7749%2C-122.4194",
    )

    luma.get_by_role("button", name=re.compile(r"^Luma Design Salon")).click()
    expect(luma.locator(".pick-details")).to_have_attribute("open", "")
    expect(luma.locator(".pick-description-copy")).to_have_text(
        "A design salon listed on Luma."
    )
    expect(
        luma.get_by_role("button", name="More like this: Luma Design Salon")
    ).to_have_count(0)
    expect(
        luma.get_by_role("button", name="Not interested: Luma Design Salon")
    ).to_have_count(0)
    expect(luma).to_contain_text("Source records")
    expect(luma).to_contain_text(
        "Luma · source public_jsonld · events.luma.com"
    )
    expect(luma).to_contain_text("Registration lanes")
    assert scenario.count("POST", "/v1/feed-feedback") == 0

    provider = luma.get_by_role("button", name="Filter events by Luma")
    expect(provider).to_have_attribute("aria-pressed", "false")
    provider.click()
    provider_token = page.locator(
        "#catalog-active-filters [data-active-filter='source']"
    )
    expect(provider_token).to_contain_text("Provider · Luma")
    expect(page.locator("#catalog-search")).to_be_focused()
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    expect(
        page.get_by_role("button", name="Remove Luma provider filter")
    ).to_have_attribute("aria-pressed", "true")
    provider_token.get_by_role(
        "button", name="Remove provider filter: Luma", exact=True
    ).click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)

    page.locator("#catalog-search").fill("luma")
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)
    page.get_by_role(
        "option", name="Search events for luma", exact=True
    ).click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    expect(page.locator("#picks-list")).to_contain_text("Luma Design Salon")
    assert scenario.count("GET", "/v1/catalog/events") == 1
    assert scenario.count("POST", "/v1/feed") == 0

    page.locator("#catalog-search").fill("")
    page.locator("#catalog-search").fill("luma")
    page.get_by_role("option", name="Provider · Luma · 1", exact=True).click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    expect(page.locator("#results-count")).to_have_text("3 loaded · 1 shown")
    expect(page.locator("#picks-list")).to_contain_text("Luma Design Salon")
    assert scenario.count("GET", "/v1/catalog/events") == 1
    assert scenario.count("POST", "/v1/feed") == 0

    provider_token.get_by_role(
        "button", name="Remove provider filter: Luma", exact=True
    ).click()
    page.locator("#catalog-search").fill("oakland")
    page.get_by_role("option", name="City · Oakland · 2", exact=True).click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(2)
    expect(page.locator("#results-scope")).to_contain_text(
        "1 hidden by client-side filters"
    )
    headings = page.locator("#picks-list .pick-card h3")
    expect(headings.nth(0)).to_have_text("Oakland Builders Meetup")
    page.locator("#catalog-search").fill("top ranked")
    page.get_by_role("option", name="Sort · Top ranked", exact=True).click()
    expect(
        page.locator("#catalog-active-filters [data-active-filter='sort']")
    ).to_contain_text("Sort · Top ranked")
    expect(headings.nth(0)).to_have_text("Friday Night Jazz")

    page.get_by_role("button", name="Clear all event filters", exact=True).click()
    page.locator("#catalog-search").fill("free")
    page.get_by_role("option", name="Price · Free", exact=True).click()
    expect(
        page.locator("#catalog-active-filters [data-active-filter='price']")
    ).to_contain_text("Price · Free")
    page.locator("#browse-current-events").click()
    assert scenario.count("GET", "/v1/catalog/events") == 2
    assert scenario.count("POST", "/v1/feed") == 0

    page.locator(".desktop-nav [data-view='concierge']").click()
    expect(page).to_have_url(f"{product_server}/#/concierge")
    expect(page.locator("#chat-workspace-tab")).to_have_attribute(
        "aria-current", "page"
    )
    expect(
        page.locator("#catalog-active-filters [data-active-filter='price']")
    ).to_contain_text("Price · Free")
    page.locator("#ask-input").fill("Something social")
    page.locator("#ask-submit").click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    assert scenario.last("POST", "/v1/feed").body == {
        "text": "Something social\n\nDiscovery context: price: Free."
    }
    expect(page.locator("#recommendation-messages .thread-message-user")).to_have_count(1)
    expect(page.locator("#recommendation-messages")).to_contain_text(
        "I found 3 current options"
    )
    expect(page.locator("#picks-list")).to_contain_text("Friday Night Jazz")
    expect(page.locator("#picks-list")).not_to_contain_text("Luma Design Salon")
    expect(page.locator("#picks-list .pick-card h3").first).to_have_text(
        "Friday Night Jazz"
    )
    assert scenario.count("POST", "/v1/requests") == 0

    page.locator(".desktop-nav [data-view='settings']").click()
    page.locator("#sign-out").click()
    expect(page.locator("#welcome-view")).to_be_visible()
    page.locator("#onboarding-email").fill("next@example.test")
    page.locator("#onboarding-submit").click()
    expect(page.locator("#product-shell")).to_be_visible()
    expect(page.locator("#ask-input")).to_have_value("")
    expect(page.locator("#recommendation-messages")).to_contain_text(
        "Start a chat"
    )
    expect(page.locator("#picks-list .pick-card")).to_have_count(0)
    expect(page.locator("#picks-list")).not_to_have_attribute("aria-busy", "true")


def test_map_reuses_loaded_events_and_applies_local_provider_and_radius_filters(  # noqa: PLR0915
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True, reduced_motion="reduce")
    page = harness.page
    _stub_map_tiles(page)
    page.clock.install(time="2030-06-02T12:00:00-07:00")
    scenario = ApiScenario(feed_payload=_rich_feed())
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='catalog']").click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)
    assert scenario.count("GET", "/v1/catalog/events") == 1
    assert scenario.count("POST", "/v1/feed") == 0

    page.locator(".desktop-nav [data-view='map']").click()
    expect(page).to_have_url(f"{product_server}/#/map")
    expect(page.locator("#map-workspace-tab")).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.locator("#map-workspace")).to_be_visible()
    expect(page.locator("#map-event-list .map-event-card")).to_have_count(3)
    expect(page.locator(".event-map-marker")).to_have_count(3)
    expect(
        page.locator('.event-map-marker[aria-label^="Luma Design Salon"]')
    ).to_have_count(1)
    expect(page.locator("#map-result-count")).to_have_text("3 mapped")
    assert scenario.count("GET", "/v1/catalog/events") == 1
    assert scenario.count("POST", "/v1/feed") == 0

    luma = page.locator(".map-event-card").filter(has_text="Luma Design Salon")
    expect(luma).to_contain_text("Luma")
    meetup = page.locator(".map-event-card").filter(
        has_text="Oakland Builders Meetup"
    )
    expect(meetup).to_contain_text("Price unknown")
    friday = page.locator(".map-event-card").filter(has_text="Friday Night Jazz")
    expect(friday).to_contain_text("Free")
    expect(page.locator("#map-event-list")).not_to_contain_text("Price not listed")
    expect(
        luma.get_by_role(
            "link",
            name="Add Luma Design Salon to Google Calendar (opens in new tab)",
        )
    ).to_have_attribute(
        "href", re.compile(r"^https://calendar\.google\.com/calendar/render\?")
    )
    expect(
        luma.get_by_role(
            "link", name="Open Luma Design Salon location in Google Maps"
        )
    ).to_have_attribute(
        "href",
        "https://www.google.com/maps/search/?api=1&query=37.7749%2C-122.4194",
    )
    luma.get_by_role("button", name="Show Luma Design Salon on the map").click()
    expect(luma).to_have_class(re.compile(r"\bselected\b"))

    search = page.locator("#catalog-search")
    search.fill("10 miles")
    page.get_by_role(
        "option", name="Distance · Within 10 mi", exact=True
    ).click()
    distance_token = page.locator(
        "#catalog-active-filters [data-active-filter='radius']"
    )
    expect(distance_token).to_have_count(1)
    expect(distance_token).to_contain_text("Distance · Within 10 mi")
    page.locator(".leaflet-control-zoom-in").click()
    expect(page.locator("#map-search-area")).to_be_visible()
    page.locator("#map-search-area").click()
    expect(page.locator("#map-search-area")).to_be_hidden()
    assert scenario.count("GET", "/v1/catalog/events") == 1
    assert scenario.count("POST", "/v1/feed") == 0

    distance_token.get_by_role(
        "button", name="Remove distance filter: Within 10 mi", exact=True
    ).click()
    expect(distance_token).to_have_count(0)
    luma = page.locator(".map-event-card").filter(has_text="Luma Design Salon")
    provider = luma.get_by_role("button", name="Filter events by Luma")
    expect(provider).to_have_attribute("aria-pressed", "false")
    provider.click()
    provider_token = page.locator(
        "#catalog-active-filters [data-active-filter='source']"
    )
    expect(provider_token).to_contain_text("Provider · Luma")
    expect(page.locator("#map-event-list .map-event-card")).to_have_count(1)
    expect(
        page.get_by_role("button", name="Remove Luma provider filter")
    ).to_have_attribute("aria-pressed", "true")

    page.locator(".desktop-nav [data-view='catalog']").click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    expect(page.locator("#picks-list")).to_contain_text("Luma Design Salon")
    assert scenario.count("GET", "/v1/catalog/events") == 1
    assert scenario.count("POST", "/v1/feed") == 0
    _assert_no_horizontal_overflow(page)


def test_catalog_provider_facets_load_every_luma_page_and_render_all_map_results(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True, reduced_motion="reduce")
    page = harness.page
    _stub_map_tiles(page)
    page.clock.install(time="2030-06-02T12:00:00-07:00")
    rich = _rich_feed()
    luma_one = _luma_catalog_event(
        "a1111111-1111-4111-8111-111111111111",
        "Luma AI Builders",
        "2030-06-08T18:00:00-07:00",
        latitude=37.7749,
        longitude=-122.4194,
    )
    luma_two = _luma_catalog_event(
        "a2222222-2222-4222-8222-222222222222",
        "Luma Product Salon",
        "2030-06-09T18:30:00-07:00",
        latitude=37.7858,
        longitude=-122.4064,
    )
    luma_three = _luma_catalog_event(
        "a3333333-3333-4333-8333-333333333333",
        "Luma Founder Dinner",
        "2030-06-10T19:00:00-07:00",
        latitude=None,
        longitude=None,
    )
    providers = [
        {
            "source_key": "luma-genai-sf",
            "label": "Generative AI SF",
            "provider": "Luma",
            "event_count": 3,
        }
    ]
    scenario = ApiScenario(
        feed_payload=rich,
        catalog_payloads={
            (None, None): {
                "items": [rich["items"][1], rich["items"][2]],
                "next_cursor": None,
                "providers": providers,
            },
            ("luma-genai-sf", None): {
                "items": [luma_one, luma_two],
                "next_cursor": "luma-page-2",
                "providers": providers,
            },
            ("luma-genai-sf", "luma-page-2"): {
                "items": [luma_three],
                "next_cursor": None,
                "providers": providers,
            },
        },
    )
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='map']").click()
    expect(page.locator("#map-event-list")).not_to_contain_text("Luma AI Builders")
    assert scenario.count("GET", "/v1/catalog/events") == 1

    page.locator("#catalog-search").fill("luma")
    page.get_by_role("option", name="Provider · Luma · 3", exact=True).click()
    expect(
        page.locator("#catalog-active-filters [data-active-filter='source']")
    ).to_contain_text("Provider · Luma")
    expect(page.locator("#map-event-list .map-event-card")).to_have_count(3)
    expect(page.locator(".event-map-marker")).to_have_count(2)
    expect(page.locator("#map-result-count")).to_have_text(
        "2 mapped · 1 without coordinates"
    )
    expect(page.locator("#map-status")).to_contain_text(
        "1 remain in the list without coordinates"
    )
    for title in ("Luma AI Builders", "Luma Product Salon", "Luma Founder Dinner"):
        expect(page.locator("#map-event-list")).to_contain_text(title)

    assert scenario.count("GET", "/v1/catalog/events") == 3
    provider_calls = [
        call
        for call in scenario.calls
        if call.method == "GET"
        and call.path == "/v1/catalog/events"
        and "source_key=luma-genai-sf" in call.query
    ]
    assert len(provider_calls) == 2
    assert parse_qs(provider_calls[0].query) == {
        "limit": ["50"],
        "source_key": ["luma-genai-sf"],
    }
    assert parse_qs(provider_calls[1].query) == {
        "limit": ["50"],
        "source_key": ["luma-genai-sf"],
        "cursor": ["luma-page-2"],
    }
    assert scenario.count("POST", "/v1/feed") == 0

    page.locator(".desktop-nav [data-view='catalog']").click()
    luma_card = page.locator(".pick-card").filter(has_text="Luma AI Builders")
    expect(luma_card).to_contain_text("Chronological")
    expect(luma_card).to_contain_text("Event link available")
    expect(luma_card).not_to_contain_text("Catalog rank 0")
    expect(luma_card).not_to_contain_text("Conflict blocked")
    luma_card.get_by_role("button", name=re.compile(r"^Luma AI Builders")).click()
    expect(luma_card).to_contain_text("Not conflict-checked · event link available")

    page.locator(".desktop-nav [data-view='concierge']").click()
    page.locator("#ask-input").fill("Recommend an AI event")
    page.locator("#ask-submit").click()
    assert scenario.last("POST", "/v1/feed").body == {"text": "Recommend an AI event"}


def test_provider_scope_selection_survives_an_unscoped_catalog_refresh_race(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    rich = _rich_feed()
    luma_events = [
        _luma_catalog_event(
            "b1111111-1111-4111-8111-111111111111",
            "Luma Systems Night",
            "2030-06-08T18:00:00-07:00",
            latitude=37.7749,
            longitude=-122.4194,
        ),
        _luma_catalog_event(
            "b2222222-2222-4222-8222-222222222222",
            "Luma Product Forum",
            "2030-06-09T18:30:00-07:00",
            latitude=37.7858,
            longitude=-122.4064,
        ),
        _luma_catalog_event(
            "b3333333-3333-4333-8333-333333333333",
            "Luma Founder Roundtable",
            "2030-06-10T19:00:00-07:00",
            latitude=37.7955,
            longitude=-122.278,
        ),
    ]
    providers = [
        {
            "source_key": "luma-genai-sf",
            "label": "Generative AI SF",
            "provider": "Luma",
            "event_count": 3,
        }
    ]
    scenario = ApiScenario(
        catalog_payloads={
            (None, None): {
                "items": [rich["items"][1], rich["items"][2]],
                "next_cursor": None,
                "providers": providers,
            },
            ("luma-genai-sf", None): {
                "items": luma_events[:2],
                "next_cursor": "luma-race-page-2",
                "providers": providers,
            },
            ("luma-genai-sf", "luma-race-page-2"): {
                "items": luma_events[2:],
                "next_cursor": None,
                "providers": providers,
            },
        }
    )
    _open_authenticated(page, scenario, product_server)
    page.locator(".desktop-nav [data-view='catalog']").click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(2)
    assert scenario.count("GET", "/v1/catalog/events") == 1

    held_unscoped: list[Route] = []

    def hold_next_unscoped_catalog(route: Route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        if not held_unscoped and "source_key" not in query:
            held_unscoped.append(route)
            return
        route.fallback()

    page.route("**/v1/catalog/events?*", hold_next_unscoped_catalog)
    page.locator("#browse-current-events").click()
    for _ in range(100):
        if held_unscoped:
            break
        page.wait_for_timeout(10)
    assert held_unscoped

    search = page.locator("#catalog-search")
    search.fill("luma")
    page.get_by_role("option", name="Provider · Luma · 3", exact=True).click()
    expect(
        page.locator("#catalog-active-filters [data-active-filter='source']")
    ).to_contain_text("Provider · Luma")
    assert not [
        call
        for call in scenario.calls
        if call.method == "GET" and "source_key=luma-genai-sf" in call.query
    ]

    scenario._handle(held_unscoped.pop())
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)
    for event in luma_events:
        expect(page.locator("#picks-list")).to_contain_text(event["title"])

    scoped_calls = [
        call
        for call in scenario.calls
        if call.method == "GET"
        and call.path == "/v1/catalog/events"
        and "source_key=luma-genai-sf" in call.query
    ]
    assert len(scoped_calls) == 2
    assert parse_qs(scoped_calls[0].query) == {
        "limit": ["50"],
        "source_key": ["luma-genai-sf"],
    }
    assert parse_qs(scoped_calls[1].query) == {
        "limit": ["50"],
        "source_key": ["luma-genai-sf"],
        "cursor": ["luma-race-page-2"],
    }


def test_catalog_browser_falls_back_to_ranked_feed_while_endpoint_is_unavailable(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    harness.allowed_console_error_fragments.append("status of 404")
    page = harness.page
    scenario = ApiScenario(feed_payload=_rich_feed(), catalog_unavailable=True)
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='catalog']").click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)
    assert scenario.count("GET", "/v1/catalog/events") == 1
    assert scenario.last("POST", "/v1/feed").body == {"text": "Show me current events"}


def test_catalog_search_suggestions_apply_mouse_filters_and_provider_scope(  # noqa: PLR0915
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    page.clock.install(time="2030-06-02T12:00:00-07:00")
    rich = _rich_feed()
    providers = [
        {
            "source_key": "luma-genai-sf",
            "label": "Generative AI SF",
            "provider": "luma",
            "event_count": 1,
        }
    ]
    scenario = ApiScenario(
        catalog_payloads={
            (None, None): {
                "items": [rich["items"][1], rich["items"][2]],
                "next_cursor": None,
                "providers": providers,
            },
            ("luma-genai-sf", None): {
                "items": [rich["items"][0]],
                "next_cursor": None,
                "providers": providers,
            },
        }
    )
    _open_authenticated(page, scenario, product_server)
    page.locator(".desktop-nav [data-view='catalog']").click()

    search = page.locator("#catalog-search")
    suggestions = page.locator("#catalog-filter-suggestions")
    expect(search).to_have_attribute("role", "combobox")
    expect(search).to_have_attribute("aria-autocomplete", "list")
    expect(search).to_have_attribute("aria-controls", "catalog-filter-suggestions")
    expect(search).to_have_attribute("aria-expanded", "false")

    search.fill("today")
    expect(suggestions).to_be_visible()
    expect(suggestions).to_have_attribute("role", "listbox")
    expect(search).to_have_attribute("aria-expanded", "true")
    page.get_by_role("option", name="Date · Today", exact=True).click()
    date_token = page.locator(
        "#catalog-active-filters [data-active-filter='when']"
    )
    expect(date_token).to_have_count(1)
    expect(date_token).to_contain_text("Date · Today")
    edit_date = date_token.get_by_role(
        "button", name="Edit date filter: Today", exact=True
    )
    expect(edit_date).to_be_visible()
    expect(search).to_have_value("")
    expect(suggestions).to_be_hidden()
    expect(search).to_have_attribute("aria-expanded", "false")

    search.fill("builders")
    expect(page.locator("#picks-list .pick-card")).to_have_count(0)
    edit_date.click()
    expect(search).to_be_focused()
    expect(search).to_have_value("")
    expect(search).to_have_attribute("placeholder", "Change date filter")
    page.get_by_role("option", name="Date · Tomorrow", exact=True).click()
    expect(date_token).to_have_count(1)
    expect(date_token).to_contain_text("Date · Tomorrow")
    expect(search).to_have_value("builders")
    date_token.get_by_role(
        "button", name="Remove date filter: Tomorrow", exact=True
    ).click()
    expect(date_token).to_have_count(0)
    expect(search).to_have_value("builders")
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    expect(page.locator("#picks-list")).to_contain_text("Oakland Builders Meetup")
    search.fill("")

    search.fill("luma")
    luma_suggestion = page.get_by_role(
        "option", name="Provider · Luma · 1", exact=True
    )
    expect(luma_suggestion).to_be_visible()
    luma_suggestion.click()
    provider_token = page.locator(
        "#catalog-active-filters [data-active-filter='source']"
    )
    expect(provider_token).to_have_count(1)
    expect(provider_token).to_contain_text("Provider · Luma")
    expect(
        provider_token.get_by_role(
            "button", name="Edit provider filter: Luma · 1", exact=True
        )
    ).to_be_visible()
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    expect(page.locator("#picks-list")).to_contain_text("Luma Design Salon")
    provider_call = scenario.last("GET", "/v1/catalog/events")
    assert parse_qs(provider_call.query) == {
        "limit": ["50"],
        "source_key": ["luma-genai-sf"],
    }

    provider_token.get_by_role(
        "button", name="Remove provider filter: Luma · 1", exact=True
    ).click()
    expect(provider_token).to_have_count(0)
    expect(page.locator("#picks-list .pick-card")).to_have_count(2)
    assert parse_qs(scenario.last("GET", "/v1/catalog/events").query) == {
        "limit": ["50"]
    }

    search.fill("recommended")
    page.get_by_role("option", name="Sort · Recommended", exact=True).click()
    sort_token = page.locator(
        "#catalog-active-filters [data-active-filter='sort']"
    )
    expect(sort_token).to_have_count(1)
    expect(sort_token).to_contain_text("Sort · Recommended")


def test_catalog_search_suggestions_support_keyboard_date_ranges_and_escape(  # noqa: PLR0915
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    page.clock.install(time="2030-06-02T12:00:00-07:00")
    rich = _rich_feed()
    scenario = ApiScenario(
        catalog_payloads={
            (None, None): {
                "items": rich["items"],
                "next_cursor": None,
                "providers": [],
            }
        }
    )
    _open_authenticated(page, scenario, product_server)
    page.locator(".desktop-nav [data-view='catalog']").click()

    search = page.locator("#catalog-search")
    suggestions = page.locator("#catalog-filter-suggestions")
    search.fill("tomorrow")
    tomorrow = page.get_by_role("option", name="Date · Tomorrow", exact=True)
    expect(tomorrow).to_be_visible()
    search.press("ArrowDown")
    tomorrow_id = tomorrow.get_attribute("id")
    assert tomorrow_id
    expect(search).to_have_attribute("aria-activedescendant", tomorrow_id)
    search.press("Enter")
    date_token = page.locator(
        "#catalog-active-filters [data-active-filter='when']"
    )
    expect(date_token).to_have_count(1)
    expect(date_token).to_contain_text("Date · Tomorrow")
    expect(search).to_have_value("")
    expect(suggestions).to_be_hidden()

    search.fill("weekend")
    expect(
        page.get_by_role("option", name="Date · This weekend", exact=True)
    ).to_be_visible()
    search.press("Escape")
    expect(suggestions).to_be_hidden()
    expect(search).to_have_attribute("aria-expanded", "false")
    expect(search).to_be_focused()
    expect(search).to_have_value("weekend")
    expect(date_token).to_contain_text("Date · Tomorrow")

    search.fill("date range")
    page.get_by_role("option", name="Choose date range…", exact=True).click()
    range_start = page.get_by_label("From", exact=True)
    range_end = page.get_by_label("To", exact=True)
    expect(range_start).to_be_visible()
    expect(range_start).to_have_attribute("type", "date")
    expect(range_start).to_be_focused()
    range_start.fill("2030-06-12")
    range_end.fill("2030-06-12")
    page.get_by_role("button", name="Apply dates", exact=True).click()
    expect(date_token).to_contain_text("Date · Jun 12")
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    expect(page.locator("#picks-list")).to_contain_text("Oakland Builders Meetup")
    expect(page.locator("#picks-list")).not_to_contain_text("Luma Design Salon")

    search.fill("today")
    search.press("ArrowDown")
    search.press("Enter")
    expect(date_token).to_have_count(1)
    expect(date_token).to_contain_text("Date · Today")
    expect(range_start).to_have_value("")
    expect(range_end).to_have_value("")
    date_token.get_by_role(
        "button", name="Remove date filter: Today", exact=True
    ).click()
    expect(date_token).to_have_count(0)
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)


def test_date_range_filter_persists_across_events_map_and_calendar(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True, reduced_motion="reduce")
    page = harness.page
    _stub_map_tiles(page)
    page.clock.install(time="2030-06-02T12:00:00-07:00")
    scenario = ApiScenario(feed_payload=_rich_feed())
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='catalog']").click()
    context_rail = page.locator("#workspace-context-rail")
    expect(context_rail).to_be_visible()
    search = context_rail.locator("#catalog-search")
    search.fill("date range")
    page.get_by_role("option", name="Choose date range…", exact=True).click()
    page.get_by_label("From", exact=True).fill("2030-06-09")
    page.get_by_label("To", exact=True).fill("2030-06-12")
    page.get_by_role("button", name="Apply dates", exact=True).click()

    date_token = page.locator(
        "#catalog-active-filters [data-active-filter='when']"
    )
    expected_date_token = "Date · Jun 9 \N{EN DASH} 12, 2030"
    expect(date_token).to_contain_text(expected_date_token)
    expect(page.locator("#picks-list .pick-card")).to_have_count(2)
    expect(page.locator("#picks-list")).to_contain_text("Luma Design Salon")
    expect(page.locator("#picks-list")).to_contain_text("Oakland Builders Meetup")
    expect(page.locator("#picks-list")).not_to_contain_text("Friday Night Jazz")

    page.locator("#map-workspace-tab").click()
    expect(page).to_have_url(f"{product_server}/#/map")
    expect(date_token).to_contain_text(expected_date_token)
    expect(page.locator("#map-event-list .map-event-card")).to_have_count(2)
    expect(page.locator(".event-map-marker")).to_have_count(2)

    page.locator("#calendar-workspace-tab").click()
    expect(page).to_have_url(f"{product_server}/#/calendar")
    expect(page.locator("#calendar-workspace")).to_be_visible()
    expect(context_rail).to_be_visible()
    expect(date_token).to_contain_text(expected_date_token)
    expect(page.locator("#event-calendar-grid .calendar-event-pill")).to_have_count(2)
    expect(page.locator("#event-calendar-grid")).not_to_contain_text(
        "Friday Night Jazz"
    )

    page.locator("#catalog-workspace-tab").click()
    expect(page).to_have_url(f"{product_server}/#/catalog")
    expect(date_token).to_contain_text(expected_date_token)
    expect(page.locator("#picks-list .pick-card")).to_have_count(2)


def test_calendar_month_navigation_and_selected_day_agenda(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True, reduced_motion="reduce")
    page = harness.page
    page.clock.install(time="2030-06-02T12:00:00-07:00")
    scenario = ApiScenario(feed_payload=_rich_feed())
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='calendar']").click()
    expect(page).to_have_url(f"{product_server}/#/calendar")
    expect(page.locator("#calendar-workspace-tab")).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.locator("#calendar-current-month")).to_have_text("June 2030")
    expect(page.locator("#event-calendar-grid .calendar-day")).to_have_count(42)

    june_twelve = page.locator(
        ".calendar-day[data-date-key='2030-06-12']"
    )
    expect(june_twelve).to_contain_text("12")
    june_twelve.click()
    expect(page.locator("#calendar-agenda-heading")).to_contain_text("June 12")
    expect(page.locator("#calendar-agenda-count")).to_have_text("1 event")
    agenda = page.locator("#calendar-agenda-list .calendar-agenda-card")
    expect(agenda).to_have_count(1)
    expect(agenda).to_contain_text("Oakland Builders Meetup")
    expect(agenda).to_contain_text("A community technology meetup.")
    expect(agenda).to_contain_text("Price unknown")
    calendar_link = agenda.get_by_role(
        "link",
        name="Add Oakland Builders Meetup to Google Calendar (opens in new tab)",
    )
    href = calendar_link.get_attribute("href")
    assert href is not None
    calendar_query = parse_qs(urlsplit(href).query)
    assert calendar_query["action"] == ["TEMPLATE"]
    assert calendar_query["text"] == ["Oakland Builders Meetup"]
    assert "A community technology meetup." in calendar_query["details"][0]
    assert calendar_query["location"][0] == "Jack London Workspace, Oakland"

    page.get_by_role("button", name="Next month", exact=True).click()
    expect(page.locator("#calendar-current-month")).to_have_text("July 2030")
    expect(page.locator("#event-calendar-grid .calendar-event-pill")).to_have_count(0)
    page.get_by_role("button", name="Previous month", exact=True).click()
    expect(page.locator("#calendar-current-month")).to_have_text("June 2030")

    page.locator(".calendar-day[data-date-key='2030-06-14']").click()
    expect(page.locator("#calendar-agenda-heading")).to_contain_text("June 14")
    expect(page.locator("#calendar-agenda-list")).to_contain_text("Friday Night Jazz")
    expect(page.locator("#calendar-agenda-list")).to_contain_text(
        "A free lakeside quartet with an early-evening set."
    )
    expect(page.locator("#calendar-agenda-list")).to_contain_text("Free")

    page.get_by_role("button", name="Today", exact=True).click()
    expect(page.locator("#calendar-agenda-heading")).to_contain_text("June 2")
    expect(page.locator("#calendar-agenda-count")).to_have_text("0 events")


def test_catalog_search_provider_suggestion_arrives_with_delayed_facets(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    rich = _rich_feed()
    scenario = ApiScenario(
        catalog_payloads={
            (None, None): {
                "items": rich["items"],
                "next_cursor": None,
                "providers": [
                    {
                        "source_key": "luma-genai-sf",
                        "label": "Generative AI SF",
                        "provider": "luma",
                        "event_count": 1,
                    }
                ],
            }
        }
    )
    held_catalog_routes: list[Route] = []

    def hold_catalog(route: Route) -> None:
        held_catalog_routes.append(route)

    scenario.install(page)
    page.route("**/v1/catalog/events?*", hold_catalog)
    page.goto(product_server, wait_until="load")
    expect(page.locator("#product-shell")).to_be_visible()
    page.locator(".desktop-nav [data-view='catalog']").click()
    for _ in range(100):
        if held_catalog_routes:
            break
        page.wait_for_timeout(10)
    assert held_catalog_routes

    search = page.locator("#catalog-search")
    search.fill("luma")
    expect(page.locator("#catalog-filter-suggestions")).to_be_hidden()
    expect(search).to_have_value("luma")

    page.unroute("**/v1/catalog/events?*", hold_catalog)
    scenario._handle(held_catalog_routes.pop())
    expect(
        page.get_by_role("option", name="Provider · Luma · 1", exact=True)
    ).to_be_visible()
    expect(search).to_have_attribute("aria-expanded", "true")


def test_map_enter_applies_the_exact_ten_mile_distance_suggestion(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True, reduced_motion="reduce")
    page = harness.page
    _stub_map_tiles(page)
    scenario = ApiScenario(feed_payload=_rich_feed())
    _open_authenticated(page, scenario, product_server)
    page.locator(".desktop-nav [data-view='map']").click()
    expect(page.locator("#map-event-list .map-event-card")).to_have_count(3)

    search = page.locator("#catalog-search")
    search.fill("10 miles")
    expect(
        page.get_by_role("option", name="Distance · Within 10 mi", exact=True)
    ).to_be_visible()
    search.press("Enter")

    distance_token = page.locator(
        "#catalog-active-filters [data-active-filter='radius']"
    )
    expect(distance_token).to_have_count(1)
    expect(distance_token).to_contain_text("Distance · Within 10 mi")
    expect(distance_token).not_to_contain_text("Within 5 mi")


def test_enter_prefers_free_first_sort_over_the_free_price_filter(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    scenario = ApiScenario(feed_payload=_rich_feed())
    _open_authenticated(page, scenario, product_server)
    page.locator(".desktop-nav [data-view='catalog']").click()

    search = page.locator("#catalog-search")
    search.fill("free first")
    expect(page.get_by_role("option", name="Price · Free", exact=True)).to_be_visible()
    expect(
        page.get_by_role("option", name="Sort · Free first", exact=True)
    ).to_be_visible()
    search.press("Enter")

    expect(
        page.locator("#catalog-active-filters [data-active-filter='sort']")
    ).to_contain_text("Sort · Free first")
    expect(
        page.locator("#catalog-active-filters [data-active-filter='price']")
    ).to_have_count(0)


def test_top_ranked_sort_is_not_suggested_without_numeric_scores(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    feed = _rich_feed()
    for item in feed["items"]:
        item["score"] = None
    scenario = ApiScenario(feed_payload=feed)
    _open_authenticated(page, scenario, product_server)
    page.locator(".desktop-nav [data-view='catalog']").click()

    page.locator("#catalog-search").fill("sort")
    expect(
        page.get_by_role("option", name="Sort · Recommended", exact=True)
    ).to_be_visible()
    expect(
        page.get_by_role("option", name="Sort · Top ranked", exact=True)
    ).to_have_count(0)


def test_map_keeps_events_without_coordinates_in_the_parallel_list(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    _stub_map_tiles(page)
    payload = _rich_feed()
    payload["items"][0]["latitude"] = None
    payload["items"][0]["longitude"] = None
    scenario = ApiScenario(feed_payload=payload)
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='map']").click()
    expect(page.locator("#map-event-list .map-event-card")).to_have_count(3)
    expect(page.locator(".event-map-marker")).to_have_count(2)
    expect(page.locator("#map-result-count")).to_have_text(
        "2 mapped · 1 without coordinates"
    )
    expect(page.locator("#map-status")).to_contain_text(
        "1 remain in the list without coordinates"
    )
    luma = page.locator(".map-event-card").filter(has_text="Luma Design Salon")
    expect(luma).to_contain_text("No exact pin")
    expect(
        luma.get_by_role("button", name="Show Luma Design Salon on the map")
    ).to_have_count(0)


def test_catalog_search_keeps_loaded_and_shown_counts_aligned_without_feedback_actions(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    scenario = ApiScenario(feed_payload=_rich_feed())
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='catalog']").click()
    page.locator("#catalog-search").fill("luma")
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)
    page.get_by_role(
        "option", name="Search events for luma", exact=True
    ).click()
    luma = page.locator(".pick-card").filter(has_text="Luma Design Salon")
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    luma.get_by_role("button", name=re.compile(r"^Luma Design Salon")).click()
    expect(luma.locator(".pick-details")).to_have_attribute("open", "")
    expect(
        luma.get_by_role("button", name="More like this: Luma Design Salon")
    ).to_have_count(0)
    expect(
        luma.get_by_role("button", name="Not interested: Luma Design Salon")
    ).to_have_count(0)
    expect(page.locator("#results-count")).to_have_text("3 loaded · 1 shown")
    expect(page.locator("#results-scope")).to_contain_text(
        "2 hidden by client-side filters"
    )
    assert scenario.count("POST", "/v1/feed-feedback") == 0


def test_sign_out_invalidates_delayed_private_refreshes_before_reonboarding(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    harness.allowed_console_error_fragments.append("status of 401")
    page = harness.page
    scenario = ApiScenario(
        hold_next_paths={"/v1/requests", "/v1/registrations", "/v1/tasks"}
    )
    _open(page, scenario, product_server)
    expect(page.locator("#product-shell")).to_be_visible()

    for _ in range(50):
        if len(scenario.held_responses) == 3:
            break
        page.wait_for_timeout(20)
    assert len(scenario.held_responses) == 3

    page.locator(".desktop-nav [data-view='settings']").click()
    page.locator("#sign-out").click()
    expect(page.locator("#welcome-view")).to_be_visible()
    scenario.requests = [
        {
            "request_id": "77777777-7777-4777-8777-777777777777",
            "text": "Account B private brief",
            "state": "received",
            "created_at": "2030-06-03T12:00:00Z",
            "categories": ["art"],
            "free_only": False,
            "window_start": None,
            "window_end": None,
            "outcome": None,
        }
    ]
    scenario.registrations = []
    scenario.tasks = []

    page.locator("#onboarding-email").fill("account-b@example.test")
    page.locator("#onboarding-submit").click()
    expect(page.locator("#product-shell")).to_be_visible()
    expect(page.locator("#recent-requests")).to_contain_text("Account B private brief")
    expect(page.locator("#recent-requests")).not_to_contain_text("A free concert this month")

    scenario.release_held_responses(status_by_path={"/v1/requests": 401})
    page.wait_for_timeout(250)
    expect(page.locator("#product-shell")).to_be_visible()
    expect(page.locator("#welcome-view")).to_be_hidden()
    expect(page.locator("#recent-requests")).to_contain_text("Account B private brief")
    expect(page.locator("#recent-requests")).not_to_contain_text("A free concert this month")

    page.locator(".desktop-nav [data-view='plans']").click()
    expect(page.locator("#plans-list")).not_to_contain_text("Friday Night Jazz")
    page.locator(".desktop-nav [data-view='tasks']").click()
    expect(page.locator("#tasks-list")).not_to_contain_text("Friday Night Jazz")


def test_delayed_preferences_from_signed_out_account_cannot_mutate_next_account(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    scenario = ApiScenario(hold_next_preference_update=True)
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='settings']").click()
    page.locator("#settings-interests [data-interest='outdoors']").click()
    page.locator("#save-preferences").click()
    expect(page.locator("#save-preferences")).to_have_attribute("aria-busy", "true")
    for _ in range(50):
        if scenario.held_preference_responses:
            break
        page.wait_for_timeout(20)
    assert len(scenario.held_preference_responses) == 1

    page.locator("#sign-out").click()
    expect(page.locator("#welcome-view")).to_be_visible()
    scenario.interests = ["art"]
    scenario.preference_revision = 50
    page.locator("#onboarding-email").fill("account-b@example.test")
    page.locator("#onboarding-submit").click()
    expect(page.locator("#product-shell")).to_be_visible()
    page.locator(".desktop-nav [data-view='settings']").click()
    expect(
        page.locator("#settings-interests [data-interest='art']")
    ).to_have_attribute("aria-pressed", "true")
    expect(
        page.locator("#settings-interests [data-interest='outdoors']")
    ).to_have_attribute("aria-pressed", "false")
    page.locator("#toast-close").click()

    scenario.release_held_preference_responses()
    page.wait_for_timeout(250)
    expect(
        page.locator("#settings-interests [data-interest='art']")
    ).to_have_attribute("aria-pressed", "true")
    expect(
        page.locator("#settings-interests [data-interest='outdoors']")
    ).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#save-preferences")).to_be_enabled()
    expect(page.locator("#save-preferences")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#toast")).to_be_hidden()


@pytest.mark.parametrize(
    ("width", "height"),
    [(1280, 900), (1000, 900), (760, 900), (390, 844)],
)
def test_chat_and_event_list_are_compact_and_overflow_safe_at_key_widths(
    page_factory,
    product_server: str,
    width: int,
    height: int,
) -> None:
    harness = page_factory(
        width=width,
        height=height,
        authenticated=True,
        reduced_motion="reduce",
    )
    page = harness.page
    _stub_map_tiles(page)
    scenario = ApiScenario(feed_payload=_rich_feed())
    _open_authenticated(page, scenario, product_server)

    navigation = (
        page.locator(".mobile-nav [data-view='catalog']")
        if width <= 900
        else page.locator(".desktop-nav [data-view='catalog']")
    )
    navigation.click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)
    page.locator("#catalog-search").fill("free")
    page.get_by_role("option", name="Price · Free", exact=True).click()
    expect(
        page.locator("#catalog-active-filters [data-active-filter='price']")
    ).to_contain_text("Price · Free")
    expect(page.locator("#picks-list .pick-card")).to_have_count(1)
    _assert_no_horizontal_overflow(page)

    measurements = page.evaluate(
        """
        () => {
          const rect = (selector) => {
            const element = document.querySelector(selector);
            const box = element.getBoundingClientRect();
            return {
              x: box.x,
              y: box.y,
              right: box.right,
              bottom: box.bottom,
              width: box.width,
              height: box.height,
              overflow: element.scrollWidth - element.clientWidth,
            };
          };
          return {
            primary: rect(".concierge-primary"),
            workspace: rect(".workspace-topbar"),
            search: rect(".catalog-search"),
            tokens: rect("#catalog-active-filters"),
            row: rect(".pick-card"),
            browse: rect("#browse-current-events"),
          };
        }
        """
    )
    assert measurements["primary"]["overflow"] <= 1
    assert measurements["workspace"]["overflow"] <= 1
    assert measurements["search"]["overflow"] <= 1
    assert measurements["tokens"]["overflow"] <= 1
    assert measurements["row"]["overflow"] <= 1
    assert measurements["search"]["height"] <= 48
    assert measurements["tokens"]["height"] <= 44
    assert measurements["row"]["height"] <= 90
    assert measurements["browse"]["height"] <= 52
    page.get_by_role(
        "button", name="Remove price filter: Free", exact=True
    ).click()
    expect(page.locator("#picks-list .pick-card")).to_have_count(3)

    mobile_nav = page.locator(".mobile-nav")
    if width <= 900:
        expect(mobile_nav).to_be_visible()
        first_event = page.locator("#picks-list .pick-card").first
        first_event.scroll_into_view_if_needed()
        visibility = page.evaluate(
            """
            () => {
              const event = document.querySelector("#picks-list .pick-card").getBoundingClientRect();
              const nav = document.querySelector(".mobile-nav").getBoundingClientRect();
              return { eventBottom: event.bottom, navTop: nav.top };
            }
            """
        )
        assert visibility["eventBottom"] <= visibility["navTop"] - 4
    else:
        expect(mobile_nav).to_be_hidden()

    map_navigation = (
        page.locator(".mobile-nav [data-view='map']")
        if width <= 900
        else page.locator(".desktop-nav [data-view='map']")
    )
    _assert_map_workspace_is_responsive(page, map_navigation)

    chat_navigation = (
        page.locator(".mobile-nav [data-view='concierge']")
        if width <= 900
        else page.locator(".desktop-nav [data-view='concierge']")
    )
    chat_navigation.click()
    expect(page.locator("#ask-submit")).to_be_visible()
    _assert_no_horizontal_overflow(page)
    composer = page.locator("#ask-form").bounding_box()
    assert composer is not None
    assert composer["height"] <= 210
    if width <= 900:
        nav_box = mobile_nav.bounding_box()
        assert nav_box is not None
        assert composer["y"] + composer["height"] <= nav_box["y"] - 4
    else:
        assert composer["y"] + composer["height"] <= height - 8


def test_plans_tasks_and_settings_are_actionable_and_keyboard_safe(  # noqa: PLR0915
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    harness.allowed_console_error_fragments.append("status of 503")
    scenario = ApiScenario(task_failures_remaining=1)
    _open_authenticated(page, scenario, product_server)

    plans_nav = page.locator(".desktop-nav [data-view='plans']")
    plans_nav.focus()
    page.keyboard.press("Enter")
    expect(page).to_have_url(f"{product_server}/#/plans")
    expect(page.locator("#main-content")).to_be_focused()
    expect(page.locator("#plans-page")).to_be_visible()
    expect(page.locator("#plans-summary")).to_contain_text("1Confirmed plan")
    expect(page.locator("#plans-list")).to_contain_text("Friday Night Jazz")
    plan_link = page.locator("#plans-list a")
    expect(plan_link).to_have_attribute("target", "_blank")
    expect(plan_link).to_have_attribute("rel", "noopener noreferrer")
    expect(plan_link).to_have_attribute("href", "https://events.example.test/friday-jazz")

    tasks_nav = page.locator(".desktop-nav [data-view='tasks']")
    tasks_nav.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#tasks-page")).to_be_visible()
    expect(page.locator("#main-content")).to_be_focused()
    expect(page.locator("#tasks-list")).to_contain_text("Sign-in required")
    task_link = page.locator("#tasks-list a")
    expect(task_link).to_have_attribute("target", "_blank")
    expect(task_link).to_have_attribute("rel", "noopener noreferrer")

    done = page.get_by_role("button", name="I finished registration")
    done.focus()
    page.keyboard.press("Enter")
    dialog = page.locator("#completion-dialog")
    expect(dialog).to_be_visible()
    assert page.evaluate(
        "document.querySelector('#completion-dialog').contains(document.activeElement)"
    )
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(done).to_be_focused()

    done.click()
    page.locator("#completion-confirm").click()
    expect(page.locator("#tasks-title")).to_be_focused()
    expect(page.locator("#toast")).to_have_attribute("role", "alert")
    expect(page.locator("#toast")).to_contain_text(
        "Verification was not started: Lifecycle verifier is recovering."
    )
    expect(done).to_be_enabled()

    done.click()
    page.locator("#completion-confirm").click()
    expect(page.locator("#toast")).to_have_attribute("role", "status")
    expect(page.locator("#toast")).to_contain_text("Verification started")
    expect(page.get_by_role("button", name="Verification requested")).to_be_disabled()
    assert scenario.count("POST", "/v1/me/tasks/handoff%3Afriday-jazz/done") == 2

    settings_nav = page.locator(".desktop-nav [data-view='settings']")
    settings_nav.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#settings-page")).to_be_visible()
    expect(page.locator("#main-content")).to_be_focused()
    expect(page.locator("#settings-email")).to_have_text("alex@example.test")
    page.locator("#settings-interests [data-interest='outdoors']").click()
    page.locator("#save-preferences").click()
    expect(page.locator("#toast")).to_have_attribute("role", "status")
    expect(page.locator("#toast")).to_contain_text("Your taste signals are saved")
    assert scenario.last("PUT", "/v1/preferences").body["interests"] == [
        "live music",
        "outdoors",
    ]


def test_search_errors_are_announced_and_controls_recover(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    harness.allowed_console_error_fragments.append("status of 503")
    scenario = ApiScenario(feed_failures_remaining=1)
    _open_authenticated(page, scenario, product_server)

    page.locator("#ask-input").fill("A temporary catalog error, please")
    page.locator("#ask-submit").click()
    error = page.locator("#ask-error")
    expect(error).to_be_visible()
    expect(error).to_have_attribute("role", "alert")
    expect(error).to_have_text("Fixture catalog is briefly unavailable.")
    expect(page.locator("#picks-list")).to_contain_text("finish that search")
    expect(page.locator("#ask-submit")).to_be_enabled()
    expect(page.locator("#ask-submit")).to_have_attribute("aria-busy", "false")
    assert page.locator("#picks-list").get_attribute("aria-busy") is None

    page.locator("#ask-submit").click()
    expect(error).to_be_hidden()
    expect(page.locator("#picks-list")).to_contain_text("Friday Night Jazz")
    assert scenario.count("POST", "/v1/feed") == 2


def test_account_erasure_requires_exact_confirmation_and_ends_the_browser_session(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    page = harness.page
    scenario = ApiScenario()
    _open_authenticated(page, scenario, product_server)

    page.locator(".desktop-nav [data-view='settings']").click()
    page.locator("#open-erasure-dialog").click()
    dialog = page.locator("#account-erasure-dialog")
    confirmation = page.locator("#account-erasure-confirmation")
    submit = page.locator("#account-erasure-confirm")
    expect(dialog).to_be_visible()
    expect(confirmation).to_be_focused()
    confirmation.fill("delete my account")
    expect(submit).to_be_disabled()

    confirmation.fill("DELETE MY ACCOUNT")
    expect(submit).to_be_enabled()
    submit.click()

    expect(dialog).to_be_hidden()
    expect(page.locator("#product-shell")).to_be_hidden()
    expect(page.locator("#erasure-view")).to_be_visible()
    expect(page.locator("#erasure-accepted-title")).to_be_focused()
    expect(page.locator("#erasure-view")).to_contain_text(
        "acceptance receipt, not a claim that every provider has finished"
    )
    call = scenario.last("POST", "/v1/me/erasure-requests")
    assert call.body["confirmation"] == "DELETE MY ACCOUNT"
    assert len(call.body["request_id"]) == 36
    assert page.evaluate(
        "localStorage.getItem('events-concierge.local-session.v1')"
    ) is None
    assert page.evaluate(
        "localStorage.getItem('events-concierge.erasure-request.v1')"
    ) is None


def test_390px_layout_reduced_motion_focus_and_overflow(page_factory, product_server: str) -> None:
    harness = page_factory(
        width=390,
        height=844,
        authenticated=True,
        reduced_motion="reduce",
        probe_scroll=True,
    )
    page = harness.page
    scenario = ApiScenario()
    _open_authenticated(page, scenario, product_server)

    expect(page.locator(".mobile-header")).to_be_visible()
    expect(page.locator("#mobile-status")).to_have_text("Queued")
    expect(page.locator("#mobile-status")).to_have_attribute(
        "aria-label", "Registration engine degraded; requests are safely queued"
    )
    expect(page.locator(".mobile-nav")).to_be_visible()
    expect(page.locator(".side-rail")).to_be_hidden()
    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches") is True
    _assert_no_horizontal_overflow(page)

    for view in ("plans", "tasks", "settings", "concierge"):
        navigation = page.locator(f".mobile-nav-item[data-view='{view}']")
        navigation.focus()
        page.keyboard.press("Enter")
        expect(page.locator(f"[data-page='{view}']")).to_be_visible()
        expect(page.locator("#main-content")).to_be_focused()
        assert page.evaluate("window.__e2eScrollCalls.at(-1).behavior") == "auto"
        _assert_no_horizontal_overflow(page)

    page.locator(".mobile-nav-item[data-view='catalog']").click()
    expect(page.locator("#concierge-page")).to_be_visible()
    expect(page.locator("#concierge-page")).to_have_attribute(
        "data-workspace-mode", "catalog"
    )
    expect(page.locator("#catalog-search")).to_be_visible()
    _assert_no_horizontal_overflow(page)


def test_pending_refresh_pauses_when_hidden_backs_off_and_preserves_stale_truth(
    page_factory, product_server: str
) -> None:
    harness = page_factory(authenticated=True)
    harness.allowed_console_error_fragments.append("status of 503")
    page = harness.page
    page.clock.install(time="2030-06-02T12:00:00Z")
    scenario = ApiScenario(
        requests=[
            {
                "request_id": _REQUEST_ID,
                "text": "Handle a free Friday concert",
                "state": "started",
                "created_at": "2030-06-02T12:00:00Z",
                "categories": ["live music"],
                "free_only": True,
                "window_start": None,
                "window_end": None,
                "outcome": None,
            }
        ],
        resolve_request_after_successes=2,
    )
    _open_authenticated(page, scenario, product_server)
    expect(page.locator("#recent-requests")).to_contain_text("Handle a free Friday concert")
    assert scenario.count("GET", "/v1/requests") == 1

    page.evaluate(
        """
        () => {
          Object.defineProperty(document, "visibilityState", {
            configurable: true,
            get: () => "hidden",
          });
          document.dispatchEvent(new Event("visibilitychange"));
        }
        """
    )
    page.clock.fast_forward(10 * 60_000)
    assert scenario.count("GET", "/v1/requests") == 1

    scenario.request_failures_remaining = 1
    page.evaluate(
        """
        () => {
          Object.defineProperty(document, "visibilityState", {
            configurable: true,
            get: () => "visible",
          });
          document.dispatchEvent(new Event("visibilitychange"));
        }
        """
    )
    expect(page.locator("#recent-requests")).to_contain_text("Handle a free Friday concert")
    assert scenario.count("GET", "/v1/requests") == 2
    assert scenario.count("GET", "/v1/registrations") == 1
    assert scenario.count("GET", "/v1/tasks") == 1

    # One failure doubles the 30-second jittered delay to at most 72 seconds.
    page.clock.fast_forward(80_000)
    expect(page.locator("#recent-requests")).to_contain_text("Friday Night Jazz")
    expect(page.locator("#recent-requests")).to_contain_text("On your calendar")
    assert scenario.count("GET", "/v1/requests") == 3
    assert scenario.count("GET", "/v1/registrations") == 2
    assert scenario.count("GET", "/v1/tasks") == 2
