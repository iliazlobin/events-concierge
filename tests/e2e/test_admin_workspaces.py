"""Hermetic inspection of the source registry, pipeline, and run workspaces.

The real Next.js UI is served at EC_ADMIN_WEB_URL. Every admin API request is
intercepted, and the fixture rejects mutations and unknown endpoints.
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
from playwright.sync_api import Browser, Locator, Page, Route, expect

pytestmark = [
    pytest.mark.browser_e2e,
    pytest.mark.skipif(
        not os.environ.get("EC_ADMIN_WEB_URL"), reason="EC_ADMIN_WEB_URL is not set"
    ),
]
STAMP = "2026-09-08T16:10:00Z"
START = "2026-09-08T16:00:00Z"
SOURCE = "bay-arts-01"
RUN = "catalog:bay-arts-01:fixed-20260908"


def source_registration_history(days: int, fixtures: bool, total: int = 2) -> dict[str, Any]:
    """A retained fixture registry with no additions during the displayed window."""
    end = datetime.fromisoformat(STAMP)
    start = end - timedelta(days=days)
    return {
        "generated_at": STAMP,
        "window_days": days,
        "bucket_hours": 24,
        "window_start": start.isoformat(),
        "baseline_sources": total,
        "total_sources": total,
        "added_sources": 0,
        "include_fixtures": fixtures,
        "history_scope": "retained_registry",
        "items": [
            {
                "bucket_start": (start + timedelta(days=index)).isoformat(),
                "bucket_end": (start + timedelta(days=index + 1)).isoformat(),
                "registered_sources": total,
                "added_sources": 0,
            }
            for index in range(days)
        ],
    }


def run_record(number: int = 1) -> dict[str, Any]:
    failed = number == 1
    return {
        "source_key": f"bay-arts-{number:02}",
        "display_name": f"Bay Arts {number:02}",
        "run_key": RUN if failed else "catalog:bay-arts-02:fixed-20260908",
        "status": "failed" if failed else "succeeded",
        "started_at": START,
        "completed_at": STAMP,
        "candidate_count": 24,
        "canonical_count": None if failed else 7,
        "error": "transport_timeout" if failed else None,
        "attempt_count": 2 if failed else 1,
        "duration_ms": 12000,
        "source_revision": 4,
        "release_revision": "fixture-dirty",
        "image_digest": None,
        "provenance_status": "claim_recorded",
        "trigger": "cadence_or_manual",
        "source_configuration": None,
        "command": None,
        "execution": None,
        "resources": None,
        "is_latest_for_source": True,
        "resolved_by_newer_success": False,
        "timeline": {
            "evidence_scope": "lifecycle_and_aggregate_observations",
            "complete": False,
            "entries": [
                {
                    "observed_at": START,
                    "event_code": "run_attempt_started",
                    "timestamp_basis": "durable_transition",
                    "stage": None,
                    "outcome_code": None,
                    "duration_ms": None,
                    "observation_count": None,
                },
                {
                    "observed_at": STAMP,
                    "event_code": "stage_observed",
                    "timestamp_basis": "evidence_recorded",
                    "stage": "collect",
                    "outcome_code": "failed" if failed else "succeeded",
                    "duration_ms": 12000,
                    "observation_count": 2,
                },
                {
                    "observed_at": STAMP,
                    "event_code": "run_status_observed",
                    "timestamp_basis": "run_projection",
                    "stage": None,
                    "outcome_code": "failed" if failed else "succeeded",
                    "duration_ms": None,
                    "observation_count": None,
                },
            ],
        },
        "stage_trace": [
            {
                "stage": "collect",
                "label": "Collect",
                "evidence_status": "measured",
                "observation_count": 2,
                "duration_ms": 12000,
                "first_observed_at": START,
                "last_observed_at": STAMP,
                "last_outcome_code": "failed" if failed else "succeeded",
                "note_code": None,
            },
            {
                "stage": "extract_enrich",
                "label": "Extract + enrich",
                "evidence_status": "not_separately_instrumented",
                "observation_count": 0,
                "duration_ms": None,
                "first_observed_at": None,
                "last_observed_at": None,
                "last_outcome_code": None,
                "note_code": "included_in_collect",
            },
        ],
    }


def source_record(number: int = 1) -> dict[str, Any]:
    return {
        "source_key": f"bay-arts-{number:02}",
        "display_name": f"Bay Arts {number:02}",
        "source_revision": 4,
        "publisher": "Bay Arts Council",
        "mode": "public_jsonld",
        "region": "Bay Area",
        "seed_url": "https://example.test/events",
        "enabled": True,
        "retired_at": None,
        "retired_reason": None,
        "superseded_by_source_key": None,
        "review_status": "reviewed",
        "effective_status": "active",
        "due": False,
        "last_succeeded_at": START,
        "next_due_at": "2026-09-09T16:00:00Z",
        "event_count": number * 7,
        "latest_run": run_record(number),
    }


def source_detail(source_key: str, hours: int) -> dict[str, Any]:
    number = int(source_key[-2:])
    return {
        "generated_at": STAMP,
        "source": source_record(number)
        | {
            "seed_host": "example.test",
            "handoff_only": True,
            "approved_origins": ["https://example.test"],
            "reviewed_at": START,
            "review_expires_at": None,
            "refresh_interval_minutes": 360,
            "min_interval_ms": 1000,
            "page_limit": 10,
            "policy_blocked": False,
        },
        "window": {"hours": hours, "bucket_hours": 24, "starts_at": START, "ends_at": STAMP},
        "summary": {
            "total_runs": 2,
            "succeeded_runs": 1,
            "failed_runs": 1,
            "running_runs": 0,
            "success_rate": 0.5,
            "candidate_count": 24,
            "canonical_count": 7,
            "yield_rate": 7 / 24,
            "average_duration_ms": 12000,
            "p95_duration_ms": 12000,
            "latest_success_at": START,
            "latest_failure_at": STAMP,
        },
        "history": [
            {
                "bucket_start": START,
                "total_runs": 2,
                "succeeded_runs": 1,
                "failed_runs": 1,
                "candidate_count": 24,
                "canonical_count": 7,
                "average_duration_ms": 12000,
            }
        ],
        "recent_runs": [run_record(number)],
        "current_build": {"release_revision": "fixture-dirty", "image_digest": None},
    }


def stages(hours: int) -> dict[str, Any]:
    rows = []
    for position, stage in enumerate(
        ["admission", "collect", "extract_enrich", "normalize_dedupe", "catalog_publish"]
    ):
        measured = stage in {"admission", "collect", "catalog_publish"}
        rows.append(
            {
                "stage": stage,
                "stage_position": position,
                "evidence_status": "measured" if measured else "not_separately_instrumented",
                "folded_into": None
                if measured
                else "collect"
                if stage == "extract_enrich"
                else "catalog_publish",
                "runs_with_evidence": 2 if measured else 0,
                "observations": 2 if measured else 0,
                "total_ms": 12000 if measured else 0,
                "avg_ms": 6000 if measured else None,
                "p95_ms": 7000 if measured else None,
                "failed_count": 1 if measured else 0,
                "pct_of_wall": 33.3 if measured else None,
            }
        )
    return {"generated_at": STAMP, "window_hours": hours, "stages": rows}


@dataclass
class WorkspaceApi:
    role: str = "viewer"
    failed: set[str] = field(default_factory=set)
    status_overrides: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    run_rows: list[dict[str, Any]] = field(default_factory=lambda: [run_record(1), run_record(2)])
    source_page_responses: dict[tuple[str, int], tuple[dict[str, Any], int]] = field(
        default_factory=dict
    )
    hold_source_pages: set[tuple[str, int]] = field(default_factory=set)
    pending_source_pages: dict[tuple[str, int], list[Route]] = field(default_factory=dict)

    def handle(self, route: Route) -> None:  # noqa: PLR0912, PLR0915 - named fixture endpoints
        request = route.request
        path = urlsplit(request.url).path
        query = parse_qs(urlsplit(request.url).query)
        self.calls.append((path, query))
        if request.method != "GET":
            self.unexpected.append(f"{request.method} {path}")
            self.respond(route, {"detail": "Fixture forbids writes"}, 405)
            return
        resource = path.rsplit("/", 1)[-1]
        if status := self.status_overrides.get(path):
            self.respond(route, {"detail": "Fixture source access denied"}, status)
            return
        if resource in self.failed or path in self.failed:
            self.respond(route, {"detail": "Fixture read unavailable"}, 503)
            return
        hours = int(query.get("window_hours", ["168"])[0])
        if path == "/admin/v1/operator/session":
            self.respond(
                route,
                {
                    "subject": "workspace-fixture",
                    "role": self.role,
                    "capabilities": ["ingestion.sources.configure"]
                    if self.role == "reviewer"
                    else ["ingestion.sources.enable"]
                    if self.role == "operator"
                    else [],
                    "environment": "local",
                    "authentication": "local",
                },
            )
        elif path == "/admin/v1/ingestion/overview":
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "policy": {"allowed": True, "reason": "Fixture", "code": "allowed"},
                    "summary": {
                        "sources": 2,
                        "active_sources": 2,
                        "due_sources": 0,
                        "running_runs": 0,
                        "failed_runs_24h": 1,
                        "catalog_events": 21,
                        "pending_commands": 0,
                        "fixture_sources": 0,
                    },
                    "latest_success_at": START,
                },
            )
        elif path == "/admin/v1/ingestion/commands":
            self.respond(route, {"items": []})
        elif path == "/admin/v1/ingestion/sources":
            page_key = (query.get("query", [""])[0], int(query.get("offset", ["0"])[0]))
            if page_key in self.hold_source_pages:
                self.pending_source_pages.setdefault(page_key, []).append(route)
            else:
                self.respond_source_page(route, page_key)
        elif path == "/admin/v1/ingestion/source-registration-history":
            self.respond(
                route,
                source_registration_history(
                    int(query.get("window_days", ["90"])[0]),
                    query.get("include_fixtures", ["false"])[0] == "true",
                ),
            )
        elif path == "/admin/v1/ingestion/filters":
            self.respond(
                route,
                {
                    "modes": [{"value": "public_jsonld", "count": 2}],
                    "publishers": [{"value": "Bay Arts Council", "count": 2}],
                    "regions": [{"value": "Bay Area", "count": 2}],
                    "source_states": [{"value": "all", "label": "All states"}],
                    "run_statuses": [],
                    "window_hours": [24, 168, 336, 720],
                },
            )
        elif re.fullmatch(r"/admin/v1/ingestion/sources/bay-arts-\d+", path):
            self.respond(route, source_detail(resource, hours))
        elif re.fullmatch(r"/admin/v1/ingestion/sources/bay-arts-\d+/events", path):
            self.respond(
                route,
                {
                    "items": [],
                    "source_total": 0,
                    "limit": 20,
                    "has_more": False,
                    "next_start_at": None,
                    "next_canonical_event_id": None,
                    "query": None,
                },
            )
        elif path == "/admin/v1/ingestion/runs":
            rows = list(self.run_rows)
            if key := query.get("source_key", [None])[0]:
                rows = [row for row in rows if row["source_key"] == key]
            if status := query.get("status", [None])[0]:
                rows = [row for row in rows if row["status"] == status]
            if needle := query.get("query", [""])[0].lower():
                rows = [
                    row
                    for row in rows
                    if needle
                    in " ".join(
                        str(row.get(key) or "")
                        for key in ["source_key", "display_name", "run_key", "error"]
                    ).lower()
                ]
            if after := query.get("started_after", [None])[0]:
                rows = [
                    row
                    for row in rows
                    if datetime.fromisoformat(row["started_at"]) >= datetime.fromisoformat(after)
                ]
            if before := query.get("started_before", [None])[0]:
                rows = [
                    row
                    for row in rows
                    if datetime.fromisoformat(row["started_at"]) < datetime.fromisoformat(before)
                ]
            if stage := query.get("stage", [None])[0]:
                rows = [
                    row
                    for row in rows
                    if any(
                        item["stage"] == stage
                        and item["evidence_status"] == "measured"
                        and (
                            not query.get("stage_outcome") or item["last_outcome_code"] == "failed"
                        )
                        for item in row["stage_trace"]
                    )
                ]
            sort_field = {
                "source": "display_name",
                "status": "status",
                "started": "started_at",
                "duration": "duration_ms",
                "attempts": "attempt_count",
                "output": "canonical_count",
            }.get(query.get("sort_by", ["started"])[0], "duration_ms")
            rows.sort(
                key=lambda row: (
                    row[sort_field] or 0
                    if sort_field in {"duration_ms", "attempt_count", "canonical_count"}
                    else row[sort_field] or ""
                ),
                reverse=query.get("sort_direction", ["desc"])[0] == "desc",
            )
            total = len(rows)
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            self.respond(
                route,
                {
                    "items": rows[offset : offset + limit],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                },
            )
        elif path == "/admin/v1/ingestion/runs/lookup":
            rows = [
                *self.run_rows,
                run_record(3) | {"run_key": "old:run", "started_at": "2025-01-01T00:00:00Z"},
            ]
            exact = next(
                (
                    row
                    for row in rows
                    if row["source_key"] == query.get("source_key", [None])[0]
                    and row["run_key"] == query.get("run_key", [None])[0]
                ),
                None,
            )
            self.respond(route, exact or {"detail": "Run not found"}, 200 if exact else 404)
        elif path == "/admin/v1/ingestion/stages":
            self.respond(route, stages(hours))
        elif path == "/admin/v1/ingestion/summary":
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "window_start": START,
                    "window_hours": hours,
                    "runs": 2,
                    "succeeded": 1,
                    "failed": 1,
                    "running": 0,
                    "paused": 0,
                    "sources_run": 2,
                    "sources_failed": 1,
                    "attempts": 3,
                    "max_attempts": 2,
                    "retrying_runs": 1,
                    "candidates": 48,
                    "canonicals": 7,
                    "zero_yield_runs": 0,
                    "wall_ms": 24000,
                    "duration_p50_ms": 12000,
                    "duration_p95_ms": 12000,
                    "duration_p99_ms": 12000,
                },
            )
        elif path == "/admin/v1/ingestion/throughput":
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "window_hours": hours,
                    "bucket_hours": int(query.get("bucket_hours", ["24"])[0]),
                    "buckets": [
                        {
                            "bucket_start": START,
                            "runs": 2,
                            "succeeded": 1,
                            "failed": 1,
                            "deferred": 0,
                            "collected": 48,
                            "published": 7,
                            "yield_pct": 7 / 48,
                            "median_duration_ms": 12000,
                        }
                    ],
                },
            )
        elif path == "/admin/v1/ingestion/events":
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "items": [],
                    "total": 0,
                    "upcoming_total": 0,
                    "source_counts": [],
                    "source_count": 0,
                    "source_counts_truncated": False,
                    "limit": int(query.get("limit", ["20"])[0]),
                    "has_more": False,
                    "next_start_at": None,
                    "next_canonical_event_id": None,
                    "query": query.get("q", [None])[0],
                    "source_key": query.get("source_key", [None])[0],
                    "run_key": query.get("run_key", [None])[0],
                    "date_scope": query.get("date_scope", ["all"])[0],
                    "price_status": query.get("price_status", ["all"])[0],
                },
            )
        elif path == "/admin/v1/ingestion/catalog-freshness":
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "total_events": 21,
                    "buckets": [
                        {
                            "bucket": "fresh",
                            "bucket_position": 0,
                            "sources": 2,
                            "events": 21,
                            "pct": 100,
                        }
                    ],
                },
            )
        elif path == "/admin/v1/ingestion/source-health":
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "total": 1,
                    "sources": [
                        {
                            "source_key": SOURCE,
                            "display_name": "Bay Arts 01",
                            "publisher": "Bay Arts Council",
                            "mode": "public_jsonld",
                            "region": "Bay Area",
                            "enabled": True,
                            "retired_at": None,
                            "refresh_interval_minutes": 360,
                            "page_limit": 10,
                            "health": "down",
                            "run_state": "failed",
                            "freshness_state": "down",
                            "retry_state": "elevated",
                            "yield_state": "unknown",
                            "last_attempt_at": START,
                            "last_success_at": "2026-09-01T16:00:00Z",
                            "last_catalog_change_at": None,
                            "latest_run_status": "failed",
                            "latest_run_error": "transport_timeout",
                            "latest_attempt_count": 2,
                            "upcoming_events": 7,
                            "hours_since_success": 168,
                        }
                    ],
                },
            )
        elif path == "/admin/v1/operations/overview":
            self.respond(
                route,
                {
                    "generated_at": STAMP,
                    "environment": "local",
                    "release_revision": "fixture-dirty",
                    "image_digest": None,
                    "schema_revisions": ["0183"],
                    "measurement_scope": "durable_queue_state",
                    "worker_liveness": "not_measured",
                    "queues": [],
                },
            )
        else:
            self.unexpected.append(path)
            self.respond(route, {"detail": "Unexpected fixture endpoint"}, 404)

    @staticmethod
    def respond(route: Route, body: dict[str, Any], status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def respond_source_page(self, route: Route, page_key: tuple[str, int]) -> None:
        body, status = self.source_page_responses.get(
            page_key,
            (
                {
                    "items": [source_record(1), source_record(2)],
                    "total": 2,
                    "limit": 100,
                    "offset": 0,
                },
                200,
            ),
        )
        self.respond(route, body, status)

    def release_source_page(self, page_key: tuple[str, int]) -> None:
        self.hold_source_pages.discard(page_key)
        for route in self.pending_source_pages.pop(page_key):
            self.respond_source_page(route, page_key)


@pytest.fixture
def workspace_page(browser: Browser) -> Iterator[tuple[Page, WorkspaceApi, str]]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        reduced_motion="reduce",
        permissions=["clipboard-read", "clipboard-write"],
    )
    page = context.new_page()
    page.set_default_timeout(7000)
    scenario = WorkspaceApi()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/admin/v1/**", scenario.handle)
    try:
        yield page, scenario, os.environ["EC_ADMIN_WEB_URL"].rstrip("/")
        assert not errors, errors
        assert not scenario.unexpected, scenario.unexpected
    finally:
        page.unroute_all(behavior="ignoreErrors")
        context.close()


def source_details(page: Page, source_key: str = SOURCE) -> Locator:
    return page.get_by_role("region", name=f"Source details for {source_key}", exact=True)


def select_source(page: Page, source_key: str = SOURCE) -> Locator:
    page.locator("#source-registry-list").get_by_role(
        "button", name=f"Bay Arts {source_key[-2:]}", exact=True
    ).click()
    details = source_details(page, source_key)
    expect(details).to_be_visible()
    return details


def populate_source_roster(scenario: WorkspaceApi, total: int) -> None:
    """Populate real API-sized pages; individual tests control when each read resolves."""
    for offset in range(0, total, 100):
        scenario.source_page_responses[("", offset)] = (
            {
                "items": [
                    source_record(number)
                    for number in range(offset + 1, min(offset + 100, total) + 1)
                ],
                "total": total,
                "limit": 100,
                "offset": offset,
            },
            200,
        )


def test_source_roster_streams_first_page_without_claiming_empty_registry(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    populate_source_roster(scenario, 150)
    scenario.hold_source_pages.update({("", 0), ("", 100)})
    with page.expect_request(
        lambda request: urlsplit(request.url).path == "/admin/v1/ingestion/sources"
    ):
        page.goto(f"{base}/admin?tab=sources")
    charts = page.get_by_role("region", name="Source registry charts", exact=True)
    registry = page.locator("#source-registry-list")
    expect(charts).to_have_attribute("aria-busy", "true")
    expect(charts.get_by_text("Reading matching sources…", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"^0 matching(?: sources)?(?: ·|$)"))).to_have_count(0)
    expect(page.get_by_text("0 enabled · 0 paused", exact=True)).to_have_count(0)
    expect(charts.get_by_role("button", name="Show all 0", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="No sources match", exact=True)).to_have_count(0)

    with page.expect_request(
        lambda request: (
            urlsplit(request.url).path == "/admin/v1/ingestion/sources"
            and parse_qs(urlsplit(request.url).query).get("offset") == ["100"]
        )
    ):
        scenario.release_source_page(("", 0))
    expect(registry.get_by_role("button", name=re.compile(r"^Bay Arts \d+$"))).to_have_count(100)
    expect(registry.get_by_role("button", name="Bay Arts 100", exact=True)).to_be_visible()
    expect(charts.get_by_text("100 loaded of 150 matching sources", exact=True)).to_be_visible()
    expect(page.get_by_text("Loading remaining sources…", exact=True)).to_be_visible()
    expect(page.get_by_role("checkbox", name="Select Bay Arts 01", exact=True)).to_be_disabled()

    scenario.release_source_page(("", 100))
    expect(registry.get_by_role("button", name=re.compile(r"^Bay Arts \d+$"))).to_have_count(150)
    expect(charts).to_have_attribute("aria-busy", "false")
    expect(charts.get_by_text("150 matching sources", exact=True)).to_be_visible()
    expect(page.get_by_role("checkbox", name="Select Bay Arts 01", exact=True)).to_be_enabled()


def test_source_roster_later_failure_retains_readonly_partial_rows(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    scenario.role = "operator"
    populate_source_roster(scenario, 150)
    scenario.hold_source_pages.add(("", 100))
    scenario.source_page_responses[("", 100)] = ({"detail": "Page two unavailable"}, 503)
    page.goto(f"{base}/admin?tab=sources")
    registry = page.locator("#source-registry-list")
    charts = page.get_by_role("region", name="Source registry charts", exact=True)
    expect(registry.get_by_role("button", name=re.compile(r"^Bay Arts \d+$"))).to_have_count(100)
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/sources" and response.status == 503
        )
    ):
        scenario.release_source_page(("", 100))
    expect(page.get_by_text("Registry read incomplete.", exact=True)).to_be_visible()
    expect(charts).to_have_attribute("aria-busy", "false")
    expect(charts.get_by_text("100 loaded of 150 matching sources", exact=True)).to_be_visible()
    expect(registry.get_by_role("button", name=re.compile(r"^Bay Arts \d+$"))).to_have_count(100)
    expect(page.get_by_role("checkbox", name="Select Bay Arts 01", exact=True)).to_be_disabled()
    bulk = page.get_by_role("region", name="Bulk source controls", exact=True)
    expect(bulk.get_by_role("checkbox")).to_be_disabled()
    expect(bulk.get_by_role("button", name=re.compile(r"^Select .*shown$"))).to_be_disabled()
    expect(charts.get_by_role("button", name="Filter sources: Latest failed (1)")).to_be_disabled()
    expect(page.get_by_role("heading", name="No sources match", exact=True)).to_have_count(0)

    # A complete retry is the boundary that re-enables actions on registry evidence.
    populate_source_roster(scenario, 150)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(registry.get_by_role("button", name=re.compile(r"^Bay Arts \d+$"))).to_have_count(150)
    expect(page.get_by_role("checkbox", name="Select Bay Arts 01", exact=True)).to_be_enabled()
    expect(page.get_by_text("Registry read incomplete.", exact=True)).to_have_count(0)
    assert not scenario.unexpected


def test_source_roster_filter_change_cancels_pending_old_scope(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    populate_source_roster(scenario, 250)
    scenario.hold_source_pages.add(("", 100))
    scenario.source_page_responses[("current scope", 0)] = (
        {
            "items": [source_record(2) | {"display_name": "Current scope source"}],
            "total": 1,
            "limit": 100,
            "offset": 0,
        },
        200,
    )
    page.goto(f"{base}/admin?tab=sources")
    registry = page.locator("#source-registry-list")
    expect(registry.get_by_role("button", name=re.compile(r"^Bay Arts \d+$"))).to_have_count(100)
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/sources"
            and parse_qs(urlsplit(response.url).query).get("query") == ["current scope"]
        )
    ):
        page.get_by_role("textbox", name="Search sources", exact=True).fill("current scope")
    expect(registry.get_by_role("button", name="Current scope source", exact=True)).to_be_visible()
    expect(registry.get_by_role("button", name=re.compile(r"^Bay Arts \d+$"))).to_have_count(0)

    # Even if an obsolete network response is delivered, it must not publish or page onward.
    scenario.release_source_page(("", 100))
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    expect(registry.get_by_role("button", name="Current scope source", exact=True)).to_be_visible()
    expect(registry.get_by_role("button", name=re.compile(r"^Bay Arts \d+$"))).to_have_count(0)
    # Only the independent global-state reader continues its all-source roster.
    expect(
        page.get_by_role("button", name="Show Healthy sources (249)", exact=True)
    ).to_be_visible()
    assert (
        sum(
            path == "/admin/v1/ingestion/sources"
            and not query.get("query")
            and int(query.get("offset", ["0"])[0]) > 100
            for path, query in scenario.calls
        )
        == 1
    )
    expect(page.get_by_text("Admin data unavailable", exact=True)).to_have_count(0)


def test_source_filter_failure_stays_local_and_sort_does_not_repeat_facets(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    scenario.failed.add("filters")
    page.goto(f"{base}/admin?tab=sources")
    registry = page.locator("#source-registry-list")
    expect(registry.get_by_role("button", name="Bay Arts 01", exact=True)).to_be_visible()
    filter_error = page.get_by_role("alert").filter(has_text="Source filters are unavailable.")
    expect(filter_error).to_be_visible()
    expect(filter_error).to_contain_text("Fixture read unavailable")
    expect(page.get_by_text("Admin data unavailable", exact=True)).to_have_count(0)
    facet_reads = sum(path.endswith("/filters") for path, _ in scenario.calls)
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/sources"
            and parse_qs(urlsplit(response.url).query).get("sort_by") == ["health"]
        )
    ):
        page.get_by_role("combobox", name="Sort sources", exact=True).select_option("health")
    expect(registry.get_by_role("button", name="Bay Arts 01", exact=True)).to_be_visible()
    assert sum(path.endswith("/filters") for path, _ in scenario.calls) == facet_reads

    # Retrying facets should not refetch a roster that already loaded successfully.
    roster_reads = sum(path.endswith("/sources") for path, _ in scenario.calls)
    scenario.failed.remove("filters")
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/filters" and response.status == 200
        )
    ):
        page.get_by_role("button", name="Retry filters", exact=True).click()
    expect(filter_error).to_have_count(0)
    assert sum(path.endswith("/filters") for path, _ in scenario.calls) == facet_reads + 1
    assert sum(path.endswith("/sources") for path, _ in scenario.calls) == roster_reads


def test_source_expands_below_its_row_and_restores_selection_history(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, _, base = workspace_page
    page.goto(f"{base}/admin?tab=sources")
    details = select_source(page)
    toggle = page.get_by_role("button", name="Bay Arts 01", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_role("complementary", name="Source inspector")).to_have_count(0)
    expanded = details.locator("xpath=ancestor::tr[1]")
    expect(expanded).to_have_attribute("data-testid", "source-expanded-row")
    expect(
        expanded.locator("xpath=preceding-sibling::tr[1]").get_by_role(
            "button", name="Bay Arts 01", exact=True
        )
    ).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["source_selection"] == [SOURCE]
    details.get_by_role("button", name="Configuration", exact=True).click()
    expect(details.get_by_text("Read-only · reviewer access required", exact=True)).to_be_visible()
    page.reload()
    expect(details.get_by_role("button", name="Configuration", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.go_back()
    expect(details.get_by_role("button", name="Overview", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.go_back()
    expect(details).to_have_count(0)
    page.go_forward()
    expect(details).to_be_visible()
    toggle.click()
    expect(details).to_have_count(0)
    expect(toggle).to_have_attribute("aria-expanded", "false")


def test_source_failed_refresh_keeps_same_scope_evidence(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    page.goto(f"{base}/admin?tab=sources")
    details = select_source(page)
    expect(details.get_by_text("7 events", exact=True)).to_be_visible()
    scenario.failed.add(SOURCE)
    details.get_by_role("button", name="Refresh source details", exact=True).click()
    expect(details.get_by_role("alert")).to_be_visible()
    expect(details.get_by_text("7 events", exact=True)).to_be_visible()
    scenario.failed.clear()
    details.get_by_role("button", name="Retry source details", exact=True).click()
    expect(details.get_by_role("alert")).to_have_count(0)


def test_source_query_failure_does_not_relabel_cached_rows(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    page.goto(f"{base}/admin?tab=sources")
    details = select_source(page)
    registry = page.locator("#source-registry-list")
    expect(details.get_by_text("7 events", exact=True)).to_be_visible()
    expect(registry.get_by_role("button", name="Bay Arts 02", exact=True)).to_be_visible()
    scenario.failed.add("/admin/v1/ingestion/sources")
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/sources"
            and parse_qs(urlsplit(response.url).query).get("query") == ["different publisher"]
            and response.status == 503
        )
    ):
        page.get_by_role("textbox", name="Search sources", exact=True).fill("different publisher")
    expect(page.get_by_role("alert").filter(has_text="Registry read incomplete.")).to_contain_text(
        "Fixture read unavailable"
    )
    expect(registry.get_by_role("button", name=re.compile("^Bay Arts"))).to_have_count(0)
    expect(page.get_by_role("heading", name="No sources match", exact=True)).to_have_count(0)
    # A deliberate registry filter change closes the selected source atomically.
    expect(details).to_have_count(0)
    assert "source_selection" not in parse_qs(urlsplit(page.url).query)


def test_source_access_denial_clears_detail_without_registry_fallback(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    scenario.role = "reviewer"
    page.goto(f"{base}/admin?tab=sources")
    details = select_source(page)
    expect(details.get_by_text("7 events", exact=True)).to_be_visible()
    details.get_by_role("button", name="Configuration", exact=True).click()
    details.get_by_role("spinbutton", name="Page limit", exact=True).fill("15")
    scenario.status_overrides[f"/admin/v1/ingestion/sources/{SOURCE}"] = 403
    details.get_by_role("button", name="Refresh source details", exact=True).click()
    expect(details.get_by_role("alert")).to_be_visible()
    expect(details.get_by_text("7 events", exact=True)).to_have_count(0)
    expect(details.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_count(0)
    expect(details.get_by_role("button", name="Edit reviewed config", exact=True)).to_have_count(0)
    # The list still has cached source data; denied exact evidence cannot borrow it.
    expect(
        page.locator("#source-registry-list").get_by_role("button", name="Bay Arts 01", exact=True)
    ).to_be_visible()


def test_source_row_actions_open_exact_run_and_catalog_scope(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    page.goto(f"{base}/admin?tab=sources")
    page.get_by_role("button", name="Latest run for Bay Arts 01", exact=True).click()
    expect(
        page.locator("#run-inspector").get_by_role("heading", name="Bay Arts 01", exact=True)
    ).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["run_selection"] == [f"{SOURCE}|{RUN}"]
    assert any(
        path.endswith("/runs/lookup") and query.get("run_key") == [RUN]
        for path, query in scenario.calls
    )
    page.go_back()
    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/admin/v1/ingestion/events"
    ):
        page.get_by_role(
            "button", name="Browse upcoming events for Bay Arts 01", exact=True
        ).click()
    assert parse_qs(urlsplit(page.url).query)["store_source"] == [SOURCE]
    assert parse_qs(urlsplit(page.url).query)["store_dates"] == ["upcoming"]
    expect(page.get_by_role("combobox", name="Catalog source", exact=True)).to_have_value(SOURCE)
    expect(page.get_by_role("combobox", name="Event dates", exact=True)).to_have_value("upcoming")


@pytest.mark.parametrize("role", ["viewer", "reviewer"])
def test_inline_source_configuration_respects_reviewer_capability(
    workspace_page: tuple[Page, WorkspaceApi, str], role: str
) -> None:
    page, scenario, base = workspace_page
    scenario.role = role
    page.goto(f"{base}/admin?tab=sources")
    page.get_by_role("button", name="Configuration for Bay Arts 01", exact=True).click()
    details = source_details(page)
    save = details.get_by_role("button", name="Save", exact=True)
    if role == "viewer":
        expect(details.get_by_text("https://example.test/events", exact=True)).to_be_visible()
        expect(save).to_have_count(0)
        expect(
            details.get_by_text("Read-only · reviewer access required", exact=True)
        ).to_be_visible()
    else:
        expect(details.get_by_role("textbox", name="Seed URL", exact=True)).to_have_value(
            "https://example.test/events"
        )
        expect(details.get_by_role("spinbutton", name="Page limit", exact=True)).to_have_value("10")
        expect(save).to_be_disabled()


def install_source_line_history(page: Page, scenario: WorkspaceApi) -> datetime:
    """Return exactly the requested real intervals, with history newer than the summary."""
    end = datetime.fromisoformat("2026-09-08T16:10:00.987654+00:00")

    def respond(route: Route) -> None:
        request = route.request
        assert request.method == "GET", "Source history fixture forbids writes"
        query = parse_qs(urlsplit(request.url).query)
        scenario.calls.append((urlsplit(request.url).path, query))
        hours = int(query["window_hours"][0])
        bucket_hours = int(query["bucket_hours"][0])
        assert hours % bucket_hours == 0 and hours // bucket_hours <= 120
        start = end - timedelta(hours=hours)
        data = source_detail(SOURCE, hours)
        data["generated_at"] = (end - timedelta(seconds=1)).isoformat()
        data["window"].update(
            bucket_hours=bucket_hours,
            starts_at=(start - timedelta(seconds=1)).isoformat(),
            ends_at=data["generated_at"],
        )
        data["history"] = [
            {
                "bucket_start": (start + timedelta(hours=index * bucket_hours)).isoformat(),
                "total_runs": 2 if index % 11 == 0 else 0,
                "succeeded_runs": 1 if index % 11 == 0 else 0,
                "failed_runs": 1 if index % 11 == 0 else 0,
                "candidate_count": 15 if index % 11 == 0 else 0,
                "canonical_count": 9 if index % 11 == 0 else 0,
                "average_duration_ms": 1000 if index % 11 == 0 else None,
            }
            for index in range(hours // bucket_hours)
        ]
        scenario.respond(route, data)

    page.route(f"**/admin/v1/ingestion/sources/{SOURCE}?*", respond)
    return end


def test_inline_history_bucket_drills_exact_source_time_bounds(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    end = install_source_line_history(page, scenario)
    page.goto(f"{base}/admin?tab=sources")
    details = select_source(page)
    details.get_by_role("group", name="Source detail window").get_by_role(
        "button", name="24h", exact=True
    ).click()
    details.get_by_role("group", name="Source history metric").get_by_role(
        "button", name="Runs / failed", exact=True
    ).click()
    points = details.get_by_role("group", name="Source history buckets").get_by_role(
        "button", name=re.compile("^Inspect source runs started ")
    )
    expect(points).to_have_count(24)
    points.last.focus()
    points.last.press("Enter")
    expect(page.locator("#runs-ledger")).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["run_source"] == [SOURCE]
    assert datetime.fromisoformat(params["run_after"][0]) == end - timedelta(hours=1)
    assert datetime.fromisoformat(params["run_before"][0]) == end
    assert params["source_window"] == ["24"]
    assert any(
        path.endswith(f"/sources/{SOURCE}")
        and query.get("window_hours") == ["24"]
        and query.get("bucket_hours") == ["1"]
        for path, query in scenario.calls
    )
    page.go_back()
    expect(
        details.get_by_role("group", name="Source detail window").get_by_role(
            "button", name="24h", exact=True
        )
    ).to_have_attribute("aria-pressed", "true")
    expect(points).to_have_count(24)


def test_inline_source_line_history_has_more_points_and_preserves_time_window_history(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    install_source_line_history(page, scenario)
    page.goto(f"{base}/admin?tab=sources&registry_query=Bay")
    details = select_source(page)
    windows = details.get_by_role("group", name="Source detail window")
    expect(windows.get_by_role("button", name="30d", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    chart = details.get_by_role("group", name="Source history buckets")
    expect(chart).to_have_attribute("data-chart", "line")
    expect(chart.get_by_role("button")).to_have_count(120)
    expect(chart.locator("path")).to_have_count(2)
    expect(details.get_by_text("6h intervals · 120 recorded points", exact=False)).to_be_visible()
    assert any(
        query.get("window_hours") == ["720"] and query.get("bucket_hours") == ["6"]
        for path, query in scenario.calls
        if path.endswith(f"/sources/{SOURCE}")
    )
    windows.get_by_role("button", name="7d", exact=True).click()
    expect(chart.get_by_role("button")).to_have_count(84)
    assert parse_qs(urlsplit(page.url).query)["source_window"] == ["168"]
    windows.get_by_role("button", name="90d", exact=True).click()
    expect(chart.get_by_role("button")).to_have_count(90)
    expect(details.get_by_text("24h intervals · 90 recorded points", exact=False)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query)["source_window"] == ["2160"]
    page.reload()
    expect(chart.get_by_role("button")).to_have_count(90)
    expect(windows.get_by_role("button", name="90d", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.go_back()
    expect(chart.get_by_role("button")).to_have_count(84)
    expect(windows.get_by_role("button", name="7d", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.go_forward()
    expect(chart.get_by_role("button")).to_have_count(90)
    assert parse_qs(urlsplit(page.url).query)["registry_query"] == ["Bay"]


def test_inline_source_line_history_keyboard_keeps_quiet_points_and_mobile_width(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    install_source_line_history(page, scenario)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base}/admin?tab=sources")
    details = select_source(page)
    chart = details.get_by_role("group", name="Source history buckets")
    points = chart.get_by_role("button")
    expect(points).to_have_count(120)
    points.first.focus()
    points.first.press("ArrowRight")
    expect(points.nth(1)).to_be_focused()
    expect(
        details.get_by_role("region", name="Source history").get_by_text(
            "0 published / 0 collected", exact=True
        )
    ).to_be_visible()
    expect(
        details.get_by_role("region", name="Source history").get_by_text(
            "0 runs · 0 failed", exact=True
        )
    ).to_be_visible()
    points.nth(1).press("End")
    expect(points.last).to_be_focused()
    points.last.press("Home")
    expect(points.first).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.evaluate("document.body.scrollWidth <= innerWidth")


def test_source_keyboard_expansion_and_close_restore_toggle_focus(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, _, base = workspace_page
    page.goto(f"{base}/admin?tab=sources")
    toggle = page.get_by_role("button", name="Inspect Bay Arts 01", exact=True)
    toggle.focus()
    toggle.press("Enter")
    details = source_details(page)
    expect(details).to_be_visible()
    details.get_by_role("button", name="Close source details", exact=True).click()
    expect(details).to_have_count(0)
    expect(toggle).to_be_focused()


def test_source_charts_filter_roster_and_output_bar_expands_matching_source(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, _, base = workspace_page
    page.goto(f"{base}/admin?tab=sources")
    charts = page.get_by_role("region", name="Source registry charts", exact=True)
    registry = page.locator("#source-registry-list")
    first = registry.get_by_role("button", name="Bay Arts 01", exact=True)
    second = registry.get_by_role("button", name="Bay Arts 02", exact=True)
    page.get_by_role("region", name="Current source collection state", exact=True).get_by_role(
        "button", name="Show Healthy sources (1)", exact=True
    ).click()
    expect(first).to_have_count(0)
    expect(second).to_be_visible()
    charts.get_by_role("button", name="Filter sources: Latest failed (1)", exact=True).click()
    expect(first).to_be_visible()
    expect(second).to_have_count(0)
    page.reload()
    expect(
        charts.get_by_role("button", name="Filter sources: Latest failed (1)")
    ).to_have_attribute("aria-pressed", "true")
    expect(first).to_be_visible()
    page.go_back()
    assert parse_qs(urlsplit(page.url).query)["registry_lens"] == ["healthy"]
    expect(second).to_be_visible()
    page.go_forward()
    charts.get_by_role("button", name=re.compile("^Filter sources: Latest error: ")).click()
    expect(first).to_be_visible()
    expect(second).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query)["registry_lens"][0].startswith("error:")
    charts.get_by_role(
        "button", name="Expand source: Bay Arts 02 · 7 latest published", exact=True
    ).click()
    expect(source_details(page, "bay-arts-02")).to_be_visible()
    expect(second).to_be_visible()
    params = parse_qs(urlsplit(page.url).query)
    assert params["source_selection"] == ["bay-arts-02"]
    assert params.get("registry_lens", ["all"]) == ["all"]
    expect(charts.get_by_text(re.compile("1 latest runs have unknown output"))).to_be_visible()


def test_source_bulk_selection_resets_on_lens_back_and_forward(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    page.goto(f"{base}/admin?tab=sources")
    registry = page.locator("#source-registry-list")
    first = registry.get_by_role("checkbox", name="Select Bay Arts 01", exact=True)
    second = registry.get_by_role("checkbox", name="Select Bay Arts 02", exact=True)
    page.get_by_role("region", name="Current source collection state", exact=True).get_by_role(
        "button", name="Show Healthy sources (1)", exact=True
    ).click()
    second.check()
    expect(second).to_be_checked()

    page.go_back()
    expect(first).to_be_visible()
    expect(second).not_to_be_checked()
    first.check()
    expect(first).to_be_checked()
    page.go_forward()
    expect(first).to_have_count(0)
    expect(second).not_to_be_checked()

    # A selected source hidden by Forward must not reappear selected on the next Back.
    page.go_back()
    expect(first).not_to_be_checked()
    expect(second).not_to_be_checked()
    assert not scenario.unexpected  # The fixture rejects every mutation.


def test_source_bulk_controls_disable_after_registry_read_failure(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    scenario.role = "operator"
    page.goto(f"{base}/admin?tab=sources")
    selected = page.get_by_role("checkbox", name="Select Bay Arts 01", exact=True)
    selected.check()
    bulk = page.get_by_role("region", name="Bulk source controls", exact=True)
    pause = bulk.get_by_role("button", name="Pause 1", exact=True)
    expect(pause).to_be_enabled()

    scenario.failed.add("/admin/v1/ingestion/sources")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("alert").filter(has_text="Registry read incomplete.")).to_contain_text(
        "Fixture read unavailable"
    )
    expect(pause).to_be_disabled()
    expect(selected).to_be_disabled()
    expect(bulk.get_by_role("checkbox")).to_be_disabled()
    assert not scenario.unexpected  # No write is permitted or attempted.


def test_pipeline_stage_unknown_window_source_and_stale_scope(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    page.goto(f"{base}/admin?tab=pipeline")
    stage = page.get_by_role("group", name="Select pipeline stage")
    stage.get_by_role("button", name=re.compile("Extract \\+ enrich")).click()
    inspector = page.locator("#pipeline-inspector")
    inspector.get_by_role("button", name="Evidence", exact=True).click()
    total = inspector.locator("dl > div").filter(has=page.get_by_text("Total time", exact=True))
    expect(total.locator("dd")).to_have_text("Unavailable")
    scenario.failed.add("stages")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("last successful snapshot")
    expect(total.locator("dd")).to_have_text("Unavailable")
    scenario.failed.clear()
    page.get_by_role("group", name="Pipeline window").get_by_role(
        "button", name="30d", exact=True
    ).click()
    expect(page.get_by_text("720h · all run outcomes", exact=True)).to_be_visible()
    page.reload()
    expect(inspector.get_by_role("button", name="Evidence", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert parse_qs(urlsplit(page.url).query)["pipeline_stage"] == ["extract_enrich"]
    before = len([path for path, _ in scenario.calls if path.endswith("/stages")])
    page.goto(
        f"{base}/admin?tab=pipeline&run_source={SOURCE}&window=24&status=failed&pipeline_inspector=evidence"
    )
    expect(page.locator("span").filter(has_text=re.compile("^Bay Arts 01$"))).to_be_visible()
    expect(page.get_by_text("24h · all run outcomes", exact=True)).to_be_visible()
    expect(inspector).to_contain_text("Aggregate source timing is unavailable")
    expect(total.locator("dd")).to_have_text("Unavailable")
    assert len([path for path, _ in scenario.calls if path.endswith("/stages")]) == before
    page.get_by_role("button", name="Inspect failed runs", exact=True).click()
    expect(page.locator("#runs-ledger")).to_be_visible()
    query = parse_qs(urlsplit(page.url).query)
    assert (
        query["run_source"] == [SOURCE]
        and query["window"] == ["24"]
        and query["status"] == ["failed"]
    )
    assert any(
        query.get("window_hours") == ["720"]
        for path, query in scenario.calls
        if path.endswith("/stages")
    )


def test_run_selection_tabs_copy_reload_back_and_stale_snapshot(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    page.goto(f"{base}/admin?tab=runs&run_source={SOURCE}&window=24")
    page.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 01")).click()
    inspector = page.locator("#run-inspector")
    expect(
        inspector.locator("dl > div")
        .filter(has=page.get_by_text("Published records", exact=True))
        .locator("dd")
    ).to_contain_text("Not recorded")
    inspector.get_by_role("button", name="Stages", exact=True).click()
    inspector.get_by_role("button", name="Inspect Extract + enrich", exact=True).click()
    expect(inspector).to_contain_text("no separate duration")
    inspector.get_by_role("button", name="Copy link", exact=True).click()
    assert page.evaluate("navigator.clipboard.readText()") == page.url
    page.reload()
    expect(inspector.get_by_role("button", name="Stages", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    assert parse_qs(urlsplit(page.url).query)["run_selection"] == [f"{SOURCE}|{RUN}"]
    page.go_back()
    page.go_back()
    expect(inspector.get_by_role("button", name="Summary", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    # Back may reload this history entry; establish a successful ledger snapshot
    # before simulating a later refresh failure.
    expect(
        page.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 01"))
    ).to_be_visible()
    with page.expect_response(
        lambda response: (
            urlsplit(response.url).path == "/admin/v1/ingestion/runs" and response.status == 200
        )
    ):
        page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
    scenario.failed.add("runs")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("last successful snapshot is retained")
    expect(inspector.get_by_role("heading", name="Bay Arts 01", exact=True)).to_be_visible()
    # The exact-run read succeeded even though the separate ledger read failed.
    expect(inspector.get_by_text(re.compile("current state is unverified"))).to_have_count(0)


def test_pipeline_bucket_and_stage_actions_open_matching_runs(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    page.goto(f"{base}/admin?tab=pipeline&fixtures=true&run_query=old-search&run_page=8")
    expect(page.get_by_role("heading", name="Record flow", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="Catalog freshness", exact=True)).to_have_count(0)
    attention = page.get_by_role("list", name="Sources requiring investigation")
    expect(attention).to_contain_text("transport timeout")
    attention.get_by_role("button", name="Inspect runs", exact=True).click()
    expect(page.locator("#runs-ledger")).to_be_visible()
    query = parse_qs(urlsplit(page.url).query)
    assert query["run_source"] == [SOURCE]
    assert "run_query" not in query and "run_page" not in query
    page.go_back()
    page.get_by_role("button", name=re.compile("^Inspect 1 failed runs started")).click()
    expect(page.locator("#runs-ledger")).to_be_visible()
    query = parse_qs(urlsplit(page.url).query)
    assert query["status"] == ["failed"]
    assert query["run_after"] == ["2026-09-08T16:00:00.000Z"]
    assert query["run_before"] == ["2026-09-08T16:10:00.000Z"]
    assert "fixtures" not in query
    expect(page.get_by_role("button", name="Clear selected time interval")).to_be_visible()
    page.go_back()
    page.locator("#pipeline-inspector").get_by_role(
        "button", name="Slowest measured runs", exact=True
    ).click()
    expect(page.locator("#runs-ledger")).to_be_visible()
    expect(
        page.locator("#runs-ledger").get_by_label("Runs pagination", exact=True)
    ).to_contain_text("1\u20132 of 2")
    assert any(
        query.get("stage") == ["collect"] and query.get("sort_by") == ["stage_duration"]
        for path, query in scenario.calls
        if path.endswith("/runs")
    )
    page.go_back()
    page.locator("#pipeline-inspector").get_by_role(
        "button", name="Failed stage outcomes", exact=True
    ).click()
    expect(
        page.locator("#runs-ledger").get_by_label("Runs pagination", exact=True)
    ).to_contain_text("1\u20131 of 1")
    assert any(
        query.get("stage_outcome") == ["failed"]
        for path, query in scenario.calls
        if path.endswith("/runs")
    )


def test_runs_searches_beyond_loaded_page_and_sorts_on_server(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    scenario.run_rows = [run_record(number) for number in range(1, 106)]
    page.goto(f"{base}/admin?tab=runs")
    ledger = page.locator("#runs-ledger")
    expect(
        page.locator("#runs-ledger").get_by_label("Runs pagination", exact=True)
    ).to_contain_text("1\u2013100 of 105")
    expect(ledger.get_by_role("button", name=re.compile("Bay Arts 105"))).to_have_count(0)
    page.get_by_role("textbox", name="Search runs", exact=True).fill("Bay Arts 105")
    expect(ledger.get_by_role("button", name=re.compile("Bay Arts 105"))).to_be_visible()
    expect(
        page.locator("#runs-ledger").get_by_label("Runs pagination", exact=True)
    ).to_contain_text("1\u20131 of 1")
    assert any(
        query.get("query") == ["Bay Arts 105"]
        for path, query in scenario.calls
        if path.endswith("/runs")
    )
    ledger.get_by_role("button", name=re.compile("^Duration")).click()
    page.wait_for_function(
        "() => new URL(location.href).searchParams.get('run_sort') === 'duration'"
    )
    assert parse_qs(urlsplit(page.url).query)["run_query"] == ["Bay Arts 105"]
    expect(ledger.get_by_role("button", name=re.compile("Bay Arts 105"))).to_be_visible()
    assert any(
        query.get("sort_by") == ["duration"] and query.get("query") == ["Bay Arts 105"]
        for path, query in scenario.calls
        if path.endswith("/runs")
    )


@pytest.mark.parametrize("captured", [True, False])
def test_run_window_evidence_separates_current_attempt_and_frozen_settings(
    workspace_page: tuple[Page, WorkspaceApi, str], captured: bool
) -> None:
    page, scenario, base = workspace_page
    current = {
        "mode": "public_jsonld",
        "reviewed_at": START,
        "review_expires_at": None,
        "refresh_interval_minutes": 1440,
        "min_interval_ms": 1000,
        "page_limit": 40,
        "collection_horizon_days": 90,
    }
    scenario.run_rows[0]["source_configuration"] = current
    if captured:
        scenario.run_rows[0]["execution_configuration"] = current | {
            "source_revision": 4,
            "page_limit": 10,
            "collection_horizon_days": 30,
        }
        scenario.run_rows[0]["collection_window"] = {
            "window_source_revision": 3,
            "horizon_days": 60,
            "start_at": START,
            "end_at": "2026-11-07T16:00:00Z",
        }
    page.goto(f"{base}/admin?tab=runs&run_selection={SOURCE}%7C{RUN}")
    inspector = page.locator("#run-inspector")
    inspector.get_by_text("Run identity and source settings", exact=True).click()
    live = inspector.get_by_role("region", name="Current source settings", exact=True)
    execution = inspector.get_by_role("region", name="Captured attempt settings", exact=True)
    window = inspector.get_by_role("region", name="Frozen run event window", exact=True)
    expect(live.get_by_text("Upcoming 90 days", exact=True)).to_be_visible()
    if captured:
        expect(execution.get_by_text("Upcoming 30 days", exact=True)).to_be_visible()
        expect(window.get_by_text("60 days", exact=True)).to_be_visible()
        expect(window.locator("time").first).to_have_attribute("datetime", START)
        expect(window.locator("time").last).to_have_attribute("datetime", "2026-11-07T16:00:00Z")
        expect(
            window.locator("dl > div")
            .filter(has=page.get_by_text("Window source revision", exact=True))
            .locator("dd")
        ).to_have_text("3")
        expect(
            execution.locator("dl > div")
            .filter(has=page.get_by_text("Execution source revision", exact=True))
            .locator("dd")
        ).to_have_text("4")
        with page.expect_response(
            lambda response: (
                urlsplit(response.url).path == "/admin/v1/ingestion/runs"
                and parse_qs(urlsplit(response.url).query).get("window_hours") == ["2160"]
            )
        ):
            page.get_by_role("group", name="Run window", exact=True).get_by_role(
                "button", name="90d", exact=True
            ).click()
        assert parse_qs(urlsplit(page.url).query)["window"] == ["2160"]
        page.reload()
        expect(
            page.get_by_role("group", name="Run window", exact=True).get_by_role(
                "button", name="90d", exact=True
            )
        ).to_have_attribute("aria-pressed", "true")
    else:
        expect(window).to_contain_text("Event window was not recorded for this run.")
        expect(execution).to_contain_text("Execution settings were not recorded for this attempt.")
        expect(execution.get_by_text("Upcoming 90 days", exact=True)).to_have_count(0)
        expect(window.locator("time")).to_have_count(0)


def test_exact_run_links_work_outside_ledger_and_clear_on_access_denial(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    page.goto(f"{base}/admin?tab=runs&window=24&run_selection=bay-arts-03%7Cold:run")
    inspector = page.locator("#run-inspector")
    expect(inspector.get_by_role("heading", name="Bay Arts 03", exact=True)).to_be_visible()
    expect(
        page.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 03"))
    ).to_have_count(0)
    assert any(
        query.get("run_key") == ["old:run"]
        for path, query in scenario.calls
        if path.endswith("/runs/lookup")
    )
    scenario.status_overrides["/admin/v1/ingestion/runs/lookup"] = 403
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(inspector.get_by_role("heading", name="Bay Arts 03", exact=True)).to_have_count(0)
    expect(inspector.get_by_role("button", name="Retry selected run", exact=True)).to_be_visible()


def test_runs_next_previous_stage_selection_and_source_focus(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, _, base = workspace_page
    page.goto(f"{base}/admin?tab=runs")
    page.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 01")).click()
    inspector = page.locator("#run-inspector")
    inspector.get_by_role("button", name="Stages", exact=True).click()
    inspector.get_by_role("button", name="Inspect Collect", exact=True).click()
    expect(inspector.get_by_role("region", name="Selected stage evidence")).to_contain_text(
        "12.0 s"
    )
    inspector.get_by_role("button", name="Inspect next run", exact=True).click()
    expect(inspector.get_by_role("heading", name="Bay Arts 02", exact=True)).to_be_visible()
    expect(inspector.get_by_role("button", name="Stages", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    inspector.get_by_role("button", name="Inspect previous run", exact=True).click()
    expect(inspector.get_by_role("heading", name="Bay Arts 01", exact=True)).to_be_visible()
    inspector.get_by_role("button", name="Only this source", exact=True).click()
    expect(
        page.locator("#runs-ledger").get_by_role("button", name=re.compile("Bay Arts 02"))
    ).to_have_count(0)
    assert parse_qs(urlsplit(page.url).query)["run_source"] == [SOURCE]


def test_follow_active_run_refreshes_until_terminal_evidence(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, scenario, base = workspace_page
    scenario.run_rows[0] = run_record(1) | {
        "status": "running",
        "completed_at": None,
        "error": None,
    }
    page.goto(f"{base}/admin?tab=runs&run_selection={SOURCE}%7C{RUN}")
    inspector = page.locator("#run-inspector")
    inspector.get_by_role("button", name="Follow active run", exact=True).click()
    expect(inspector.get_by_role("button", name="Pause following", exact=True)).to_be_visible()
    scenario.run_rows[0] = scenario.run_rows[0] | {
        "status": "succeeded",
        "completed_at": STAMP,
        "canonical_count": 24,
    }
    expect(inspector.get_by_text("succeeded", exact=True)).to_be_visible(timeout=10000)
    expect(inspector.get_by_role("button", name="Pause following", exact=True)).to_have_count(0)


def test_stage_selection_opens_filterable_expandable_timeline_without_changing_ledger_scope(
    workspace_page: tuple[Page, WorkspaceApi, str],
) -> None:
    page, _, base = workspace_page
    page.goto(f"{base}/admin?tab=runs&run_stage=collect&run_selection={SOURCE}%7C{RUN}")
    inspector = page.locator("#run-inspector")
    inspector.get_by_role("button", name="Inspect Extract + enrich", exact=True).click()
    expect(inspector.get_by_role("region", name="Selected stage evidence")).to_contain_text(
        "no separate duration"
    )
    params = parse_qs(urlsplit(page.url).query)
    assert params["run_stage"] == ["collect"] and params["run_evidence_stage"] == ["extract_enrich"]
    inspector.get_by_role("button", name="Show retained stage events", exact=True).click()
    expect(
        inspector.get_by_role("group", name="Timeline evidence").get_by_role(
            "button", name="Stages", exact=True
        )
    ).to_have_attribute("aria-pressed", "true")
    expect(inspector).to_contain_text("1 of 3 retained entries")
    inspector.locator("summary").filter(has_text="stage observed").click()
    expect(inspector.get_by_text("evidence recorded", exact=True)).to_be_visible()
    page.reload()
    expect(inspector).to_contain_text("1 of 3 retained entries")
    assert parse_qs(urlsplit(page.url).query)["run_stage"] == ["collect"]


@pytest.mark.parametrize("width", [390, 1024, 1440])
def test_workspaces_selectable_without_body_overflow(
    workspace_page: tuple[Page, WorkspaceApi, str], width: int
) -> None:
    page, _, base = workspace_page
    page.set_viewport_size({"width": width, "height": 844})
    for tab in ["sources", "pipeline", "runs"]:
        page.goto(f"{base}/admin?tab={tab}")
        if tab == "sources":
            select_source(page)
            source_details(page).get_by_role("button", name="Configuration", exact=True).click()
        elif tab == "pipeline":
            page.get_by_role("group", name="Select pipeline stage").get_by_role(
                "button", name=re.compile("Catalog \\+ publish")
            ).click()
            page.locator("#pipeline-inspector").get_by_role(
                "button", name="Evidence", exact=True
            ).click()
        else:
            page.locator("#runs-ledger").get_by_role(
                "button", name=re.compile("Bay Arts 01")
            ).click()
            page.locator("#run-inspector").get_by_role(
                "button", name="Execution", exact=True
            ).click()
        dimensions = page.evaluate("""() => ({viewport: document.documentElement.clientWidth,
            html: document.documentElement.scrollWidth, body: document.body.scrollWidth})""")
        assert dimensions["html"] <= dimensions["viewport"], (tab, dimensions)
        assert dimensions["body"] <= dimensions["viewport"], (tab, dimensions)
