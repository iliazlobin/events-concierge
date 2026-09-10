"""Exercise the built admin UI with hermetic, read-only operator API evidence.

Set EC_ADMIN_WEB_URL to an already-running Next.js build. No backend database, provider,
operator mutation, or screenshot artifact is used by these tests.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, Route, expect
from tests.e2e.test_admin_workspaces import source_detail, source_registration_history

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]

_STAMP = "2026-09-08T16:00:00Z"


def _source(number: int) -> dict[str, Any]:
    return {
        "source_key": f"bay-arts-{number:02}",
        "display_name": f"Bay Arts {number:02}",
        "publisher": "Arts Council",
        "mode": "public_jsonld",
        "region": "Bay Area",
        "enabled": True,
        "retired_at": None,
        "refresh_interval_minutes": 360,
        "page_limit": 10,
        "health": "late" if number <= 2 else "healthy",
        "run_state": "failed" if number <= 2 else "ok",
        "freshness_state": "late" if number <= 2 else "ok",
        "retry_state": "ok",
        "yield_state": "ok",
        "last_attempt_at": _STAMP,
        "last_success_at": _STAMP,
        "last_catalog_change_at": _STAMP,
        "latest_run_status": "failed" if number <= 2 else "succeeded",
        "latest_run_error": "transport_error" if number <= 2 else None,
        "latest_attempt_count": 1,
        "upcoming_events": number,
        "hours_since_success": 0,
    }


def _event(source_number: int, number: int) -> dict[str, Any]:
    description = f"Published fixture description for source {source_number}, event {number}."
    return {
        "canonical_event_id": f"019a7137-8b68-7bf4-b75c-{source_number:04x}{number:08x}",
        "title": f"Bay Arts {source_number:02} Event {number:02}",
        "start_at": f"2026-10-{number:02}T19:00:00.123456Z",
        "end_at": f"2026-10-{number:02}T20:00:00.123456Z",
        "venue_name": "Fixture Arts Hall",
        "city": "Oakland",
        "latitude": 37.8,
        "longitude": -122.27,
        "description": description,
        "description_length": len(description),
        "price_status": "free",
        "price_min_cents": None,
        "price_max_cents": None,
        "price_currency": "USD",
        "event_status": "scheduled",
        "normalizer_version": 1,
        "merge_version": 1,
        "source_event_id": f"https://events.example.test/source-{source_number}/event-{number}",
        "registration_url": f"https://events.example.test/register/{source_number}/{number}",
        "last_seen_at": _STAMP,
        "refresh_run_key": f"fixture-source-{source_number:02}-published-run",
        "quality_issues": [],
        "organizer_name": "Arts Council",
        "host_names": [],
        "speaker_names": [],
        "partner_names": [],
        "entity_profiles": [],
        "attendance_count": None,
        "registration_status": "open",
    }


def catalog_response(
    observations: list[dict[str, Any]], params: dict[str, list[str]]
) -> dict[str, Any]:
    """Mirror the public query shape, including source-scoped attribution before dedup."""
    source = params.get("source_key", [None])[0]
    run = params.get("run_key", [None])[0]
    query = params.get("q", [""])[0]
    dates = params.get("date_scope", ["all"])[0]
    price = params.get("price_status", ["all"])[0]
    now = datetime.fromisoformat(_STAMP)
    matches: dict[str, dict[str, Any]] = {}
    contributions: dict[str, set[str]] = {}
    source_names: dict[str, str] = {}
    for row in sorted(observations, key=lambda event: event["last_seen_at"], reverse=True):
        if source and row["source_key"] != source:
            continue
        if run and row["refresh_run_key"] != run:
            continue
        if query.casefold() not in row["title"].casefold():
            continue
        if price != "all" and row["price_status"] != price:
            continue
        future = datetime.fromisoformat(row["end_at"] or row["start_at"]) > now
        if (dates == "upcoming" and not future) or (dates == "past" and future):
            continue
        matches.setdefault(row["canonical_event_id"], row)
        contributions.setdefault(row["source_key"], set()).add(row["canonical_event_id"])
        source_names[row["source_key"]] = row["source_display_name"]
    rows = sorted(
        matches.values(), key=lambda event: (event["start_at"], event["canonical_event_id"])
    )
    total = len(rows)
    upcoming_total = sum(
        datetime.fromisoformat(row["end_at"] or row["start_at"]) > now for row in rows
    )
    source_counts = sorted(
        [
            {"source_key": key, "source_display_name": source_names[key], "events": len(events)}
            for key, events in contributions.items()
        ],
        key=lambda source: (-source["events"], source["source_display_name"]),
    )
    if after := params.get("after_start_at", [None])[0]:
        identity = params.get("after_canonical_event_id", [""])[0]
        rows = [
            row for row in rows if (row["start_at"], row["canonical_event_id"]) > (after, identity)
        ]
    limit = int(params.get("limit", ["20"])[0])
    shown = rows[:limit]
    more = len(rows) > limit
    return {
        "generated_at": _STAMP,
        "items": shown,
        "total": total,
        "upcoming_total": upcoming_total,
        "source_counts": source_counts[:500],
        "source_count": len(source_counts),
        "source_counts_truncated": len(source_counts) > 500,
        "limit": limit,
        "has_more": more,
        "next_start_at": shown[-1]["start_at"] if more else None,
        "next_canonical_event_id": shown[-1]["canonical_event_id"] if more else None,
        "query": query or None,
        "source_key": source,
        "run_key": run,
        "date_scope": dates,
        "price_status": price,
    }


def catalog_observations() -> list[dict[str, Any]]:
    primary = [
        _event(1, number) | {"source_key": "bay-arts-01", "source_display_name": "Bay Arts 01"}
        for number in range(1, 26)
    ]
    second = [
        _event(2, number) | {"source_key": "bay-arts-02", "source_display_name": "Bay Arts 02"}
        for number in range(1, 4)
    ]
    second[0].update(start_at="2026-09-01T19:00:00Z", end_at="2026-09-01T20:00:00Z")
    second[1].update(price_status="paid", price_min_cents=1500)
    second[2].update(price_status="unknown")
    shared = primary[0] | {
        "source_key": "bay-arts-02",
        "source_display_name": "Bay Arts 02",
        "last_seen_at": "2026-09-08T15:00:00Z",
        "refresh_run_key": "fixture-source-02-shared-run",
        "source_event_id": "https://events.example.test/source-2/shared-event",
        "registration_url": "https://events.example.test/register/2/shared-event",
    }
    return [*primary, *second, shared]


@dataclass
class OperationsApi:
    backend_failed: bool = False
    sources_failed: bool = False
    summary_failed: bool = False
    throughput_failed: bool = False
    pending: int = 7
    queue_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_health_overrides: dict[int, dict[str, Any]] = field(default_factory=dict)
    extra_queues: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    event_calls: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)
    failed_event_queries: set[str] = field(default_factory=set)
    failed_event_sources: set[str] = field(default_factory=set)
    summary_calls: list[dict[str, list[str]]] = field(default_factory=list)
    throughput_calls: list[dict[str, list[str]]] = field(default_factory=list)
    record_calls: list[dict[str, list[str]]] = field(default_factory=list)
    catalog_status: int = 200
    catalog_corruption: str | None = None
    held_catalog_source: str | None = None
    held_catalog_routes: list[Route] = field(default_factory=list)
    catalog_history_available: bool = False
    catalog_history_calls: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)

    def handle(self, route: Route) -> None:  # noqa: PLR0911, PLR0912, PLR0915 - explicit fixture endpoints
        request = route.request
        path = urlsplit(request.url).path
        self.calls.append((request.method, path))
        if request.method != "GET":
            self.unexpected.append(f"{request.method} {path}")
            self.respond(route, {"detail": "Fixture forbids mutations"}, 405)
            return
        if path == "/admin/v1/operator/session":
            self.respond(
                route,
                {
                    "subject": "fixture-viewer",
                    "role": "viewer",
                    "capabilities": ["ingestion.read"],
                    "environment": "local",
                    "authentication": "local",
                },
            )
            return
        if path == "/admin/v1/operations/overview":
            if self.backend_failed:
                self.respond(route, {"detail": "Fixture backend read unavailable"}, 503)
                return
            queues = []
            for name in (
                "ingestion_commands",
                "request_start",
                "notifications",
                "account_erasure",
                "change_delivery",
                "calendar_repair",
                "handoff_expiry",
                "watch_projection",
                "entity_refresh",
            ):
                queues.append(
                    {
                        "queue": name,
                        "pending": self.pending if name == "ingestion_commands" else 0,
                        "ready": self.pending - 1 if name == "ingestion_commands" else 0,
                        "leased": 1 if name == "ingestion_commands" else 0,
                        "failed": 19 if name == "notifications" else 0,
                        "oldest_pending_at": _STAMP if name == "ingestion_commands" else None,
                        "last_progress_at": _STAMP,
                    }
                )
            for queue in queues:
                queue.update(self.queue_overrides.get(queue["queue"], {}))
            queues.extend(self.extra_queues)
            self.respond(
                route,
                {
                    "generated_at": _STAMP,
                    "environment": "local",
                    "release_revision": "fixture-revision",
                    "image_digest": None,
                    "schema_revisions": ["0182"],
                    "measurement_scope": "durable_queue_state",
                    "worker_liveness": "not_measured",
                    "queues": queues,
                },
            )
            return
        if path == "/admin/v1/operations/errors":
            params = parse_qs(urlsplit(request.url).query)
            self.respond(
                route,
                {
                    "generated_at": _STAMP,
                    "queue": params.get("queue", ["request_start"])[0],
                    "offset": int(params.get("offset", ["0"])[0]),
                    "limit": int(params.get("limit", ["10"])[0]),
                    "total": 0,
                    "items": [],
                },
            )
            return
        if path == "/admin/v1/operations/records":
            params = parse_qs(urlsplit(request.url).query)
            self.record_calls.append(params)
            self.respond(
                route,
                {
                    "generated_at": _STAMP,
                    "queue": params.get("queue", ["request_start"])[0],
                    "scope": params.get("scope", ["pending"])[0],
                    "offset": int(params.get("offset", ["0"])[0]),
                    "limit": int(params.get("limit", ["10"])[0]),
                    "total": 0,
                    "items": [],
                },
            )
            return
        if path == "/admin/v1/ingestion/source-registration-history":
            params = parse_qs(urlsplit(request.url).query)
            self.respond(
                route,
                source_registration_history(
                    int(params.get("window_days", ["90"])[0]),
                    params.get("include_fixtures", ["false"])[0] == "true",
                    total=14,
                ),
            )
            return
        if path == "/admin/v1/ingestion/source-health":
            if self.sources_failed:
                self.respond(route, {"detail": "Fixture source read unavailable"}, 503)
                return
            self.respond(
                route,
                {
                    "generated_at": _STAMP,
                    "total": 14,
                    "sources": [
                        _source(number) | self.source_health_overrides.get(number, {})
                        for number in range(1, 15)
                    ],
                },
            )
            return
        if path == "/admin/v1/ingestion/summary":
            self.summary_calls.append(parse_qs(urlsplit(request.url).query))
            if self.summary_failed:
                self.respond(route, {"detail": "Fixture ingestion summary unavailable"}, 503)
                return
            self.respond(
                route,
                {
                    "generated_at": _STAMP,
                    "window_start": "2026-09-07T16:00:00Z",
                    "window_hours": 24,
                    "runs": 40,
                    "succeeded": 34,
                    "failed": 3,
                    "running": 2,
                    "paused": 1,
                    "sources_run": 12,
                    "sources_failed": 2,
                    "attempts": 58,
                    "max_attempts": 8,
                    "retrying_runs": 6,
                    "candidates": 220,
                    "canonicals": 180,
                    "zero_yield_runs": 4,
                    "wall_ms": 360000,
                    "duration_p50_ms": 3500,
                    "duration_p95_ms": 8200,
                    "duration_p99_ms": 12000,
                },
            )
            return
        if path == "/admin/v1/ingestion/throughput":
            params = parse_qs(urlsplit(request.url).query)
            self.throughput_calls.append(params)
            if self.throughput_failed:
                self.respond(route, {"detail": "Fixture collection activity unavailable"}, 503)
                return
            self.respond(
                route,
                {
                    "generated_at": _STAMP,
                    "window_hours": int(params.get("window_hours", ["24"])[0]),
                    "bucket_hours": int(params.get("bucket_hours", ["1"])[0]),
                    "buckets": [
                        {
                            "bucket_start": (
                                datetime.fromisoformat(_STAMP)
                                - timedelta(
                                    hours=offset * int(params.get("bucket_hours", ["1"])[0])
                                )
                            ).isoformat(),
                            "runs": runs,
                            "succeeded": runs - failed,
                            "failed": failed,
                            "deferred": 0,
                            "collected": collected,
                            "published": published,
                            "yield_pct": published / collected * 100 if collected else None,
                            "median_duration_ms": 3500 if runs else None,
                        }
                        for offset, runs, failed, collected, published in (
                            (3, 0, 0, 0, 0),
                            (2, 4, 1, 28, 19),
                            (1, 6, 0, 47, 32),
                        )
                    ],
                },
            )
            return
        if path == "/admin/v1/ingestion/overview":
            self.respond(
                route,
                {
                    "generated_at": _STAMP,
                    "policy": {
                        "allowed": True,
                        "reason": "Fixture source policy",
                        "code": "allowed",
                    },
                    "summary": {
                        "sources": 14,
                        "active_sources": 14,
                        "due_sources": 2,
                        "running_runs": 0,
                        "failed_runs_24h": 2,
                        "catalog_events": 105,
                        "pending_commands": 0,
                        "fixture_sources": 0,
                    },
                    "latest_success_at": _STAMP,
                },
            )
            return
        if path == "/admin/v1/ingestion/runs/lookup":
            self.respond(route, {"detail": "Run detail is outside this operations fixture"}, 404)
            return
        if path == "/admin/v1/ingestion/runs":
            self.respond(route, {"items": [], "total": 0, "limit": 50, "offset": 0})
            return
        if path == "/admin/v1/ingestion/commands":
            self.respond(route, {"items": []})
            return
        if path == "/admin/v1/ingestion/sources":
            self.respond(route, {"items": [], "total": 0, "limit": 100, "offset": 0})
            return
        if path == "/admin/v1/ingestion/stages":
            self.respond(route, {"generated_at": _STAMP, "window_hours": 24, "stages": []})
            return
        if path == "/admin/v1/ingestion/filters":
            self.respond(
                route,
                {
                    "modes": [],
                    "publishers": [],
                    "regions": [],
                    "source_states": [],
                    "run_statuses": [],
                    "window_hours": [24, 168, 720],
                },
            )
            return
        if path == "/admin/v1/ingestion/events":
            params = parse_qs(urlsplit(request.url).query)
            source = params.get("source_key", [""])[0]
            self.event_calls.append((source, params))
            if self.held_catalog_source == source:
                self.held_catalog_routes.append(route)
                return
            self.handle_catalog_events(route)
            return
        event_source = re.fullmatch(r"/admin/v1/ingestion/sources/(bay-arts-(\d{2}))/events", path)
        if event_source:
            source_key, source_number = event_source.groups()
            self.handle_events(route, source_key, int(source_number))
            return
        if path.startswith("/admin/v1/ingestion/sources/bay-arts-"):
            if self.catalog_history_available:
                params = parse_qs(urlsplit(request.url).query)
                source_key = path.rsplit("/", 1)[-1]
                self.catalog_history_calls.append((source_key, params))
                hours = int(params.get("window_hours", ["720"])[0])
                bucket = int(params.get("bucket_hours", ["24"])[0])
                body = source_detail(source_key, hours)
                end = datetime.fromisoformat(_STAMP)
                start = end - timedelta(hours=hours)
                body["generated_at"] = _STAMP
                body["window"] = {
                    "hours": hours,
                    "bucket_hours": bucket,
                    "starts_at": start.isoformat(),
                    "ends_at": _STAMP,
                }
                body["history"] = [
                    {
                        "bucket_start": (end - timedelta(hours=bucket * offset)).isoformat(),
                        "total_runs": 2,
                        "succeeded_runs": 2,
                        "failed_runs": 0,
                        "candidate_count": 14,
                        "canonical_count": 7,
                        "average_duration_ms": 3500,
                    }
                    for offset in range(hours // bucket, 0, -1)
                ]
                self.respond(route, body)
                return
            # Source rendering has separate coverage; this suite verifies its deep-link boundary.
            self.respond(route, {"detail": "Source detail is outside this operations fixture"}, 404)
            return
        self.unexpected.append(path)
        self.respond(route, {"detail": "Unexpected fixture endpoint"}, 404)

    def handle_catalog_events(self, route: Route) -> None:
        params = parse_qs(urlsplit(route.request.url).query)
        source = params.get("source_key", [""])[0]
        query = params.get("q", [""])[0]
        if self.catalog_status != 200:
            self.respond(
                route, {"detail": "Fixture published event read unavailable"}, self.catalog_status
            )
            return
        if query in self.failed_event_queries or source in self.failed_event_sources:
            self.respond(route, {"detail": "Fixture published event read unavailable"}, 503)
            return
        result = catalog_response(catalog_observations(), params)
        if self.catalog_corruption == "source":
            result["source_key"] = "bay-arts-02"
        elif self.catalog_corruption == "dates":
            result["date_scope"] = "past"
        elif self.catalog_corruption == "price":
            result["price_status"] = "paid"
        elif self.catalog_corruption == "row" and result["items"]:
            result["items"] = [
                result["items"][0] | {"source_key": "bay-arts-02"},
                *result["items"][1:],
            ]
        self.respond(route, result)

    def handle_events(self, route: Route, source_key: str, source_number: int) -> None:
        params = parse_qs(urlsplit(route.request.url).query)
        self.event_calls.append((source_key, params))
        query = params.get("q", [""])[0]
        if query in self.failed_event_queries or source_key in self.failed_event_sources:
            self.respond(route, {"detail": "Fixture published event read unavailable"}, 503)
            return
        events = [_event(source_number, number) for number in range(1, 26)]
        filtered = [event for event in events if query.casefold() in event["title"].casefold()]
        after_start = params.get("after_start_at", [""])[0]
        after_id = params.get("after_canonical_event_id", [""])[0]
        if after_start and after_id:
            filtered = [
                event
                for event in filtered
                if (event["start_at"], event["canonical_event_id"]) > (after_start, after_id)
            ]
        limit = int(params.get("limit", ["20"])[0])
        items = filtered[:limit]
        has_more = len(filtered) > limit
        self.respond(
            route,
            {
                "items": items,
                "source_total": len(events),
                "limit": limit,
                "has_more": has_more,
                "next_start_at": items[-1]["start_at"] if has_more else None,
                "next_canonical_event_id": items[-1]["canonical_event_id"] if has_more else None,
                "query": query or None,
            },
        )

    @staticmethod
    def respond(route: Route, body: dict[str, Any], status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


@pytest.fixture
def operations_page(browser: Browser) -> Iterator[tuple[Page, OperationsApi, str]]:
    base_url = os.environ["EC_ADMIN_WEB_URL"].rstrip("/")
    context = browser.new_context(
        viewport={"width": 1600, "height": 1000},
        reduced_motion="reduce",
    )
    page = context.new_page()
    scenario = OperationsApi()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # Intercept both operator and consumer APIs; only the explicit operator GETs
    # above are permitted. A fallback to tenant catalog/auth APIs fails the test.
    page.route("**/v1/**", scenario.handle)
    try:
        yield page, scenario, base_url
        assert not errors, errors
        assert not scenario.unexpected, scenario.unexpected
        assert scenario.calls and all(
            method == "GET" and path.startswith("/admin/v1/") for method, path in scenario.calls
        )
    finally:
        context.close()


def test_operations_preserves_unknown_queue_bookmarks_without_inventory(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, _, base_url = operations_page
    page.goto(f"{base_url}/admin?ops_queue=future_reconciliation&registry_query=arts")
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    unavailable = page.get_by_role("region", name="Investigation unavailable", exact=True)
    expect(unavailable).to_contain_text(
        "This saved link has no record investigation in the console."
    )
    expect(
        unavailable.get_by_role("link", name="Open operations runbook", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("region", name="Work", exact=True)).to_have_count(0)
    original = page.url
    page.reload()
    expect(unavailable).to_be_visible()
    page.get_by_role("button", name="Collapse investigation", exact=True).click()
    expect(page.get_by_role("region", name="System summary", exact=True)).to_be_visible()
    assert "ops_queue" not in parse_qs(urlsplit(page.url).query)
    assert parse_qs(urlsplit(page.url).query)["registry_query"] == ["arts"]
    page.go_back()
    expect(unavailable).to_be_visible()
    assert page.url == original


def test_legacy_ingestion_bookmark_links_commands_and_restores_selection(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, _, base_url = operations_page
    page.goto(f"{base_url}/admin?ops_queue=ingestion_commands")
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    page.get_by_role("region", name="Investigation unavailable", exact=True).get_by_role(
        "button", name="Open commands", exact=True
    ).click()
    expect(page.get_by_role("heading", name="Commands", exact=True)).to_be_visible()
    page.go_back()
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    expect(page.get_by_role("region", name="Investigation unavailable", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="All work", exact=True)).to_have_count(0)


def test_operations_failed_snapshot_keeps_independent_record_read_available(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    page.goto(f"{base_url}/admin?ops_queue=request_start")
    records = page.get_by_role("region", name="Records", exact=True)
    expect(records).to_be_visible()
    scenario.backend_failed = True
    page.get_by_role("button", name="Refresh", exact=True).click()
    alert = page.get_by_role("alert", name="Overview data status", exact=True)
    expect(alert).to_contain_text("Work could not be loaded.")
    expect(records).to_be_visible()
    expect(records.get_by_role("alert")).to_have_count(0)
    scenario.backend_failed = False
    alert.get_by_role("button", name="Retry", exact=True).click()
    expect(alert).to_have_count(0)
    expect(records).to_be_visible()


def test_catalog_page_record_survive_reload_and_navigation_back(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    page.goto(f"{base_url}/admin")
    expect(page.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
    page.get_by_role("region", name="System summary", exact=True).get_by_role(
        "button", name="Open catalog", exact=True
    ).click()
    expect(page.get_by_role("heading", name="Catalog", exact=True)).to_be_visible()
    store = page.get_by_role("region", name="Catalog investigation")
    store.get_by_role("combobox", name="Catalog source", exact=True).select_option("bay-arts-01")
    search = store.get_by_role("textbox", name="Search catalog events")
    expect(store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)).to_be_visible()
    search.fill("Event")
    expect(store.get_by_text("Page 1 · 20 loaded", exact=True)).to_be_visible()
    store.get_by_role("button", name="Next parsed events page", exact=True).click()
    event = _event(1, 21)
    store.get_by_role("button", name=event["title"], exact=True).click()
    expect(store.get_by_role("button", name="Copy event ID", exact=True)).to_be_visible()
    expect(store.get_by_role("button", name="Copy run key", exact=True)).to_be_visible()
    store.get_by_text("Published record fields", exact=True).click()
    expect(store.locator("pre")).to_contain_text(event["canonical_event_id"])
    expect(store.locator("pre")).to_contain_text(event["refresh_run_key"])
    saved_query = parse_qs(urlsplit(page.url).query)
    assert saved_query["store_source"] == ["bay-arts-01"]
    assert saved_query["store_query"] == ["Event"]
    assert saved_query["store_event"] == [event["canonical_event_id"]]
    assert saved_query["store_after_start"] == [_event(1, 20)["start_at"]]
    assert saved_query["store_after_id"] == [_event(1, 20)["canonical_event_id"]]
    page.reload()
    expect(search).to_have_value("Event")
    expect(store.get_by_role("button", name=event["title"], exact=True)).to_have_attribute(
        "aria-expanded", "true"
    )
    expect(store.get_by_text("Later page · 5 loaded", exact=True)).to_be_visible()
    expect(store.get_by_role("button", name="First parsed events page", exact=True)).to_be_enabled()
    # A mounted search must not clear URL-provided cursor or record after its debounce interval.
    page.wait_for_timeout(350)
    assert parse_qs(urlsplit(page.url).query) == saved_query
    assert {key: scenario.event_calls[-1][1][key] for key in ["after_start_at", "q"]} == {
        "after_start_at": [_event(1, 20)["start_at"]],
        "q": ["Event"],
    }
    page.get_by_role("navigation", name="Administration navigation").get_by_role(
        "button", name="Overview", exact=True
    ).click()
    expect(page.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
    page.go_back()
    expect(store.get_by_role("button", name=event["title"], exact=True)).to_have_attribute(
        "aria-expanded", "true"
    )
    assert parse_qs(urlsplit(page.url).query) == saved_query
    store.get_by_role("button", name="First parsed events page", exact=True).click()
    expect(store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)).to_be_visible()
    assert "store_after_start" not in parse_qs(urlsplit(page.url).query)
    assert "store_event" not in parse_qs(urlsplit(page.url).query)


def test_global_catalog_deduplicates_events_and_preserves_filter_navigation(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    page.goto(f"{base_url}/admin?tab=catalog")
    store = page.get_by_role("region", name="Catalog investigation")
    source = store.get_by_role("combobox", name="Catalog source", exact=True)
    dates = store.get_by_role("combobox", name="Event dates", exact=True)
    price = store.get_by_role("combobox", name="Event price", exact=True)
    expect(source).to_have_value("")
    expect(dates).to_have_value("all")
    expect(price).to_have_value("all")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("28")
    shared = store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)
    expect(shared).to_have_count(1)
    expect(store.get_by_role("button", name="Manage source for Bay Arts 01 Event 01")).to_have_text(
        "Bay Arts 01"
    )
    assert scenario.event_calls[-1][0] == ""
    assert "source_key" not in scenario.event_calls[-1][1]
    expect(store.get_by_role("textbox", name="Find a catalog source")).to_have_count(0)

    dates.select_option("upcoming")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("27")
    expect(store.get_by_role("button", name="Bay Arts 02 Event 01", exact=True)).to_have_count(0)
    price.select_option("paid")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("1")
    expect(store.get_by_role("button", name="Bay Arts 02 Event 02", exact=True)).to_be_visible()
    selected = page.url
    page.reload()
    expect(dates).to_have_value("upcoming")
    expect(price).to_have_value("paid")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("1")
    assert parse_qs(urlsplit(page.url).query)["store_dates"] == ["upcoming"]
    assert parse_qs(urlsplit(page.url).query)["store_price"] == ["paid"]
    price.select_option("unknown")
    expect(store.get_by_role("button", name="Bay Arts 02 Event 03", exact=True)).to_be_visible()
    page.go_back()
    expect(price).to_have_value("paid")
    expect(store.get_by_role("button", name="Bay Arts 02 Event 02", exact=True)).to_be_visible()
    assert page.url == selected
    page.go_forward()
    expect(price).to_have_value("unknown")
    expect(store.get_by_role("button", name="Bay Arts 02 Event 03", exact=True)).to_be_visible()
    store.get_by_role("button", name="Clear filters", exact=True).click()
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("28")
    expect(dates).to_have_value("all")
    expect(price).to_have_value("all")
    dates.select_option("past")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("1")
    expect(store.get_by_role("button", name="Bay Arts 02 Event 01", exact=True)).to_be_visible()
    assert scenario.event_calls[-1][1]["date_scope"] == ["past"]


def test_catalog_source_filter_resets_cursor_and_selection_but_retains_other_filters(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    page.goto(
        f"{base_url}/admin?tab=catalog&store_source=bay-arts-01&store_dates=upcoming&store_price=free&store_query=Event"
    )
    store = page.get_by_role("region", name="Catalog investigation")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("25")
    store.get_by_role("button", name="Next parsed events page", exact=True).click()
    selected = store.get_by_role("button", name="Bay Arts 01 Event 21", exact=True)
    selected.click()
    expect(selected).to_have_attribute("aria-expanded", "true")
    previous = page.url
    store.get_by_role("combobox", name="Catalog source", exact=True).select_option("bay-arts-02")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("1")
    expect(selected).to_have_count(0)
    expect(store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)).to_be_visible()
    expect(store.get_by_role("button", name="Manage source for Bay Arts 01 Event 01")).to_have_text(
        "Bay Arts 02"
    )
    params = parse_qs(urlsplit(page.url).query)
    assert params["store_source"] == ["bay-arts-02"]
    assert params["store_dates"] == ["upcoming"]
    assert params["store_price"] == ["free"]
    assert params["store_query"] == ["Event"]
    for key in ("store_after_start", "store_after_id", "store_event", "store_run"):
        assert key not in params
    assert scenario.event_calls[-1][1]["price_status"] == ["free"]
    page.go_back()
    expect(selected).to_have_attribute("aria-expanded", "true")
    assert page.url == previous
    page.reload()
    expect(selected).to_have_attribute("aria-expanded", "true")
    assert page.url == previous


def test_catalog_failed_search_and_source_read_hide_previous_rows_and_recover(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    page.goto(f"{base_url}/admin?tab=catalog&store_source=bay-arts-01")
    store = page.get_by_role("region", name="Catalog investigation")
    old_record = store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)
    expect(old_record).to_be_visible()
    scenario.failed_event_queries.add("Event")
    store.get_by_role("textbox", name="Search catalog events").fill("Event")
    failure = store.get_by_role("alert").filter(has_text="Fixture published event read unavailable")
    expect(failure).to_be_visible()
    expect(old_record).to_have_count(0)
    expect(store.get_by_text("No published events match these filters.", exact=True)).to_have_count(
        0
    )
    scenario.failed_event_queries.clear()
    store.get_by_role("button", name="Retry", exact=True).click()
    expect(old_record).to_be_visible()
    expect(failure).to_have_count(0)
    scenario.failed_event_sources.add("bay-arts-02")
    store.get_by_role("combobox", name="Catalog source", exact=True).select_option("bay-arts-02")
    expect(failure).to_be_visible()
    expect(old_record).to_have_count(0)
    expect(
        store.get_by_text("This source has no current browseable events.", exact=True)
    ).to_have_count(0)
    scenario.failed_event_sources.clear()
    store.get_by_role("button", name="Retry", exact=True).click()
    expect(store.get_by_role("button", name="Bay Arts 02 Event 01", exact=True)).to_be_visible()
    expect(failure).to_have_count(0)


def test_catalog_browser_back_discards_committed_and_pending_search_drafts(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, _, base_url = operations_page
    page.goto(f"{base_url}/admin?tab=catalog&store_source=bay-arts-01")
    store = page.get_by_role("region", name="Catalog investigation")
    search = store.get_by_role("textbox", name="Search catalog events")
    original = store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)
    original.click()
    original_query = parse_qs(urlsplit(page.url).query)
    for committed in [True, False]:
        store.get_by_role("button", name="Bay Arts 01 Event 02", exact=True).click()
        search.fill("Event 21")
        if committed:
            expect(
                store.get_by_role("button", name="Bay Arts 01 Event 21", exact=True)
            ).to_be_visible()
            expect(original).to_have_count(0)
            assert parse_qs(urlsplit(page.url).query)["store_query"] == ["Event 21"]
        page.go_back()
        expect(search).to_have_value("")
        expect(original).to_have_attribute("aria-expanded", "true")
        # A stale draft must not reapply itself after Back has restored another URL state.
        page.wait_for_timeout(350)
        expect(search).to_have_value("")
        expect(original).to_have_attribute("aria-expanded", "true")
        assert parse_qs(urlsplit(page.url).query) == original_query


def test_catalog_links_existing_source_and_run_investigations(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    # Old record links still resolve after the architecture view is retired.
    page.goto(f"{base_url}/admin?store=catalog&store_source=bay-arts-01")
    expect(page.get_by_role("heading", name="Catalog", exact=True)).to_be_visible()
    store = page.get_by_role("region", name="Catalog investigation")
    record = store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)
    expect(record).to_be_visible()
    # The record total comes from this API, not the roster's one upcoming event.
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_contain_text("25")
    store.get_by_role("button", name="Manage source for Bay Arts 01 Event 01", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]source_selection=bay-arts-01(?:&|$)"))
    details = page.get_by_role("region", name="Source details for bay-arts-01", exact=True)
    expect(details).to_be_visible()
    expect(details.get_by_role("button", name="Retry source details", exact=True)).to_be_visible()
    expect(details.get_by_role("button", name="Edit reviewed config", exact=True)).to_have_count(0)
    assert ("GET", "/admin/v1/ingestion/sources/bay-arts-01") in scenario.calls
    page.go_back()
    expect(record).to_be_visible()
    record.click()
    store.get_by_role("button", name="Publishing run", exact=True).click()
    expect(page.get_by_role("heading", name="Runs", exact=True)).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["tab"] == ["runs"]
    assert params["run_selection"] == ["bay-arts-01|fixture-source-01-published-run"]
    assert ("GET", "/admin/v1/ingestion/runs") in scenario.calls
    page.go_back()
    expect(record).to_be_visible()


def test_catalog_source_choices_failure_keeps_independent_global_events_available(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    scenario.sources_failed = True
    page.goto(f"{base_url}/admin?tab=catalog")
    store = page.get_by_role("region", name="Catalog investigation")
    record = store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)
    expect(record).to_be_visible()
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("28")
    expect(store.get_by_text("Source filter choices are unavailable.", exact=False)).to_be_visible()
    expect(
        store.get_by_role("combobox", name="Catalog source", exact=True).locator("option")
    ).to_have_count(1)
    scenario.sources_failed = False
    page.get_by_role("button", name="Retry sources", exact=True).click()
    expect(
        store.get_by_role("combobox", name="Catalog source", exact=True).locator("option")
    ).to_have_count(15)
    expect(record).to_be_visible()
    store.get_by_role("combobox", name="Catalog source", exact=True).select_option("bay-arts-01")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("25")


@pytest.mark.parametrize("status", [403, 503])
def test_catalog_failed_refresh_hides_rows_details_and_authoritative_count(
    operations_page: tuple[Page, OperationsApi, str],
    status: int,
) -> None:
    page, scenario, base_url = operations_page
    page.goto(f"{base_url}/admin?tab=catalog")
    store = page.get_by_role("region", name="Catalog investigation")
    record = store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)
    record.click()
    expect(store.get_by_role("button", name="Copy event ID", exact=True)).to_be_visible()
    scenario.catalog_status = status
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(record).to_have_count(0)
    expect(store.get_by_role("button", name="Copy event ID", exact=True)).to_have_count(0)
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("—")
    expect(store.get_by_role("button", name="Next parsed events page", exact=True)).to_be_disabled()
    expect(store.get_by_role("button", name="Retry", exact=True)).to_be_visible()
    scenario.catalog_status = 200
    store.get_by_role("button", name="Retry", exact=True).click()
    expect(record).to_have_attribute("aria-expanded", "true")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("28")


@pytest.mark.parametrize("corruption", ["source", "dates", "price", "row"])
def test_catalog_unconfirmed_filter_scope_never_displays_records(
    operations_page: tuple[Page, OperationsApi, str],
    corruption: str,
) -> None:
    page, scenario, base_url = operations_page
    page.goto(
        f"{base_url}/admin?tab=catalog&store_source=bay-arts-01&store_dates=upcoming&store_price=free"
    )
    store = page.get_by_role("region", name="Catalog investigation")
    record = store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)
    expect(record).to_be_visible()
    scenario.catalog_corruption = corruption
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(store.get_by_role("button", name="Retry", exact=True)).to_be_visible()
    expect(record).to_have_count(0)
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("—")
    scenario.catalog_corruption = None
    store.get_by_role("button", name="Retry", exact=True).click()
    expect(record).to_be_visible()
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("25")


def test_catalog_late_source_response_cannot_replace_new_global_scope(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    page.goto(f"{base_url}/admin?tab=catalog&store_source=bay-arts-01")
    store = page.get_by_role("region", name="Catalog investigation")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("25")
    scenario.held_catalog_source = "bay-arts-01"
    with page.expect_request(
        lambda request: (
            urlsplit(request.url).path == "/admin/v1/ingestion/events"
            and parse_qs(urlsplit(request.url).query).get("source_key") == ["bay-arts-01"]
        )
    ):
        page.get_by_role("button", name="Refresh", exact=True).click()
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("—")
    store.get_by_role("combobox", name="Catalog source", exact=True).select_option("")
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("28")
    assert scenario.held_catalog_routes
    scenario.held_catalog_source = None
    for route in scenario.held_catalog_routes:
        scenario.handle_catalog_events(route)
    scenario.held_catalog_routes.clear()
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(
        store.get_by_role("region", name="Catalog overview", exact=True)
        .locator(":scope > dl dd")
        .first
    ).to_have_text("28")
    expect(store.get_by_role("button", name="Bay Arts 02 Event 01", exact=True)).to_be_visible()
    assert "store_source" not in parse_qs(urlsplit(page.url).query)


@pytest.mark.parametrize("width,height", [(390, 844), (1024, 768)])
def test_operations_and_catalog_keyboard_inspection_have_no_body_overflow(
    operations_page: tuple[Page, OperationsApi, str],
    width: int,
    height: int,
) -> None:
    page, _, base_url = operations_page
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{base_url}/admin?tab=catalog")
    store = page.get_by_role("region", name="Catalog investigation")
    record = store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)
    record.focus()
    record.press("Enter")
    expect(record).to_have_attribute("aria-expanded", "true")
    expect(store.get_by_role("button", name="Copy event ID", exact=True)).to_be_visible()
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    dimensions = page.evaluate("""() => ({
        viewport: document.documentElement.clientWidth,
        html: document.documentElement.scrollWidth,
        body: document.body.scrollWidth
    })""")
    assert dimensions["html"] <= dimensions["viewport"], dimensions
    assert dimensions["body"] <= dimensions["viewport"], dimensions
    navigation = page.get_by_role("navigation", name="Administration navigation").get_by_role(
        "button", name="Overview", exact=True
    )
    navigation.focus()
    navigation.press("Enter")
    entry = page.get_by_role("button", name="Inspect request starts", exact=True)
    entry.focus()
    entry.press("Enter")
    expect(page.get_by_role("heading", name="Overview", exact=True, level=1)).to_be_visible()
    expect(entry).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_role("region", name="Records", exact=True)).to_be_visible()
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    dimensions = page.evaluate("""() => ({
        viewport: document.documentElement.clientWidth,
        html: document.documentElement.scrollWidth,
        body: document.body.scrollWidth
    })""")
    assert dimensions["html"] <= dimensions["viewport"], dimensions
    assert dimensions["body"] <= dimensions["viewport"], dimensions


def test_shared_event_links_use_the_displayed_observation_source_and_run(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, _, base_url = operations_page
    page.goto(
        f"{base_url}/admin?tab=catalog&store_source=bay-arts-02&store_dates=upcoming&store_price=free"
    )
    store = page.get_by_role("region", name="Catalog investigation")
    record = store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)
    expect(record).to_be_visible()
    record.click()
    expect(store.get_by_role("link", name="Provider page", exact=True)).to_have_attribute(
        "href", "https://events.example.test/register/2/shared-event"
    )
    store.get_by_role("button", name="Publishing run", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]run_selection="))
    params = parse_qs(urlsplit(page.url).query)
    assert params["run_selection"] == ["bay-arts-02|fixture-source-02-shared-run"]
    page.go_back()
    expect(record).to_have_attribute("aria-expanded", "true")
    store.get_by_role("button", name="Manage source for Bay Arts 01 Event 01", exact=True).click()
    expect(page).to_have_url(re.compile(r"[?&]source_selection=bay-arts-02(?:&|$)"))
    page.go_back()
    expect(record).to_have_attribute("aria-expanded", "true")


def test_catalog_overview_has_one_source_filter_and_one_set_of_matching_totals(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    scenario.catalog_history_available = True
    page.goto(f"{base_url}/admin?tab=catalog")
    overview = page.get_by_role("region", name="Catalog overview", exact=True)
    expect(overview.locator(":scope > dl dd")).to_have_text(["28", "27", "2"])
    expect(overview.get_by_role("region", name="Events by source", exact=True)).to_have_count(0)
    expect(page.get_by_text("Matching events", exact=True)).to_have_count(1)
    source_filter = page.get_by_role("combobox", name="Catalog source", exact=True)
    expect(source_filter).to_have_count(1)
    source_filter.select_option("bay-arts-01")
    expect(overview.locator(":scope > dl dd")).to_have_text(["25", "25", "1"])
    expect(page.get_by_role("combobox", name="Catalog source", exact=True)).to_have_value(
        "bay-arts-01"
    )
    expect(
        overview.get_by_role("group", name="Published records timeline", exact=True)
    ).to_be_visible()
    assert scenario.catalog_history_calls[-1][0] == "bay-arts-01"
    page.get_by_role("combobox", name="Event dates", exact=True).select_option("past")
    expect(overview.locator(":scope > dl dd")).to_have_text(["0", "0", "0"])
    expect(page.get_by_text("No published events match these filters.", exact=True)).to_be_visible()
    # Filtering current records does not turn recorded collection history into zero.
    expect(
        overview.get_by_role("group", name="Published records timeline", exact=True)
    ).to_be_visible()
    expect(overview.get_by_text("7 published · 2 runs", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["store_dates"] == ["past"]


def test_catalog_publication_history_window_and_run_drilldown_preserve_catalog_scope(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    scenario.catalog_history_available = True
    page.goto(
        f"{base_url}/admin?tab=catalog&store_source=bay-arts-01&store_price=free&store_dates=upcoming"
    )
    trend = page.get_by_role("region", name="Catalog publication trend", exact=True)
    windows = trend.get_by_role("group", name="Catalog trend window", exact=True)
    windows.get_by_role("button", name="7d", exact=True).click()
    expect(windows.get_by_role("button", name="7d", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    first = trend.get_by_role(
        "button",
        name="Inspect published records from runs started 2026-09-01 16:00 UTC",
        exact=True,
    )
    expect(first).to_be_visible()
    assert scenario.catalog_history_calls[-1][1]["window_hours"] == ["168"]
    assert scenario.catalog_history_calls[-1][1]["bucket_hours"] == ["6"]
    saved = page.url
    page.reload()
    expect(first).to_be_visible()
    expect(windows.get_by_role("button", name="7d", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    first.focus()
    first.press("Enter")
    expect(page.get_by_role("heading", name="Runs", exact=True, level=1)).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["tab"] == ["runs"]
    assert params["run_source"] == ["bay-arts-01"]
    assert datetime.fromisoformat(params["run_after"][0]) == datetime.fromisoformat(
        "2026-09-01T16:00:00Z"
    )
    assert datetime.fromisoformat(params["run_before"][0]) == datetime.fromisoformat(
        "2026-09-01T22:00:00Z"
    )
    page.go_back()
    expect(first).to_be_visible()
    assert page.url == saved
    expect(page.get_by_role("combobox", name="Event price", exact=True)).to_have_value("free")
    expect(page.get_by_role("combobox", name="Event dates", exact=True)).to_have_value("upcoming")
    windows.get_by_role("button", name="24h", exact=True).click()
    expect(windows.get_by_role("button", name="24h", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.go_back()
    expect(windows.get_by_role("button", name="7d", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert page.url == saved
    page.go_forward()
    expect(windows.get_by_role("button", name="24h", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert parse_qs(urlsplit(page.url).query)["catalog_trend"] == ["24"]


def test_catalog_counts_and_collection_history_fail_and_recover_independently(
    operations_page: tuple[Page, OperationsApi, str],
) -> None:
    page, scenario, base_url = operations_page
    page.goto(f"{base_url}/admin?tab=catalog")
    overview = page.get_by_role("region", name="Catalog overview", exact=True)
    store = page.get_by_role("region", name="Catalog investigation", exact=True)
    timeline = overview.get_by_role("group", name="Published records timeline", exact=True)
    expect(overview.locator(":scope > dl dd")).to_have_text(["28", "27", "2"])
    expect(timeline).to_be_visible()
    scenario.throughput_failed = True
    scenario.catalog_status = 503
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(overview.locator(":scope > dl dd")).to_have_text(["—", "—", "—"])
    expect(overview.get_by_role("button", name="Filter catalog to Bay Arts 01")).to_have_count(0)
    expect(timeline).to_have_count(0)
    expect(overview.get_by_text("Collection history unavailable.", exact=True)).to_be_visible()
    scenario.catalog_status = 200
    store.get_by_role("button", name="Retry", exact=True).click()
    expect(overview.locator(":scope > dl dd")).to_have_text(["28", "27", "2"])
    expect(store.get_by_role("button", name="Bay Arts 01 Event 01", exact=True)).to_be_visible()
    expect(overview.get_by_text("Collection history unavailable.", exact=True)).to_be_visible()
    scenario.throughput_failed = False
    overview.get_by_role("button", name="Retry history", exact=True).click()
    expect(timeline).to_be_visible()
    expect(overview.locator(":scope > dl dd")).to_have_text(["28", "27", "2"])
