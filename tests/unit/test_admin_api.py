"""Local-only ingestion-admin API contracts with no database or provider dependencies."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from events_concierge.api.admin import IngestionRunOut, install_ingestion_admin_routes
from events_concierge.domain.ingestion_admin import IngestionSourceRevisionTarget
from events_concierge.ports.ingestion_admin import (
    IngestionCommandConflictError,
    IngestionCommandRejectedError,
    IngestionCommandUnavailableError,
    IngestionSourceConfigurationConflictError,
    IngestionSourceConfigurationUnavailableError,
    IngestionSourceNotFoundError,
)

_NOW = datetime(2026, 7, 23, 18, 30, tzinfo=UTC)
_COMMAND_ID = UUID("11111111-1111-4111-8111-111111111111")
_SECOND_COMMAND_ID = UUID("22222222-2222-4222-8222-222222222222")


def _run(
    *,
    run_key: str = "admin:city-events:20260723T183000Z",
    status: str = "succeeded",
    error: str | None = None,
    is_latest_for_source: bool = True,
    resolved_by_newer_success: bool = False,
) -> dict[str, object]:
    return {
        "source_key": "city-events",
        "display_name": "City events",
        "run_key": run_key,
        "status": status,
        "started_at": _NOW,
        "completed_at": _NOW if status in {"succeeded", "failed"} else None,
        "candidate_count": 12 if status == "succeeded" else None,
        "canonical_count": 10 if status == "succeeded" else None,
        "error": error,
        "attempt_count": 1,
        "is_latest_for_source": is_latest_for_source,
        "resolved_by_newer_success": resolved_by_newer_success,
        # Capabilities and raw provider material must be dropped by the response projection.
        "lease_token": "never-render-this",
        "provider_payload": "never-render-this-either",
    }


def _command(
    command_id: UUID = _COMMAND_ID,
    *,
    action: str = "refresh_source",
    source_key: str | None = "city-events",
) -> dict[str, object]:
    return {
        "command_id": command_id,
        "action": action,
        "source_key": source_key,
        "status": "queued",
        "requested_at": _NOW,
        "started_at": None,
        "completed_at": None,
        "result": None,
        "error_code": None,
        "lease_token": "never-render-this",
    }


class FakeIngestionAdmin:
    """Record edge calls while deliberately exposing no inline refresh runner."""

    def __init__(self) -> None:
        self.overview_calls = 0
        self.source_calls: list[dict[str, object]] = []
        self.run_calls: list[dict[str, object]] = []
        self.filter_calls: list[dict[str, object]] = []
        self.detail_calls: list[dict[str, object]] = []
        self.event_calls: list[dict[str, object]] = []
        self.configuration_calls: list[dict[str, object]] = []
        self.bulk_configuration_calls: list[dict[str, object]] = []
        self.command_limits: list[int] = []
        self.command_detail_calls: list[UUID] = []
        self.enqueue_calls: list[dict[str, object]] = []
        self.commands: dict[UUID, dict[str, object]] = {_COMMAND_ID: _command()}
        self.enqueue_error: Exception | None = None
        self.configuration_error: Exception | None = None
        self.bulk_configuration_error: Exception | None = None
        self.inline_execution_calls = 0

    async def get_overview(self) -> dict[str, object]:
        self.overview_calls += 1
        return {
            "generated_at": _NOW,
            "policy": {
                "allowed": True,
                "reason": "Reviewed public catalog policy is admitted.",
                "code": "allowed",
            },
            "summary": {
                "sources": 38,
                "active_sources": 17,
                "due_sources": 5,
                "running_runs": 1,
                "failed_runs_24h": 2,
                "catalog_events": 900,
                "pending_commands": 1,
                "fixture_sources": 497,
            },
            "latest_success_at": _NOW,
            "database_dsn": "never-render-this",
        }

    async def list_sources(self, **kwargs: object) -> dict[str, object]:
        self.source_calls.append(kwargs)
        latest = _run()
        latest.pop("source_key")
        latest.pop("display_name")
        return {
            "items": [
                {
                    "source_key": "city-events",
                    "display_name": "City events",
                    "publisher": "City",
                    "mode": "public_jsonld",
                    "region": "bay_area_9_county",
                    "seed_url": "https://events.example.org/calendar",
                    "enabled": True,
                    "source_revision": 4,
                    "review_status": "reviewed",
                    "effective_status": "due",
                    "policy_blocked": False,
                    "due": True,
                    "last_succeeded_at": _NOW,
                    "next_due_at": _NOW,
                    "event_count": 10,
                    "total_event_count": 25,
                    "upcoming_event_count": 12,
                    "latest_run": latest,
                }
            ],
            "total": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    async def list_runs(self, **kwargs: object) -> dict[str, object]:
        self.run_calls.append(kwargs)
        return {
            "items": [
                _run(
                    status="failed",
                    error="source_rate_limited",
                    is_latest_for_source=False,
                    resolved_by_newer_success=True,
                )
            ],
            "total": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    async def list_commands(self, limit: int) -> tuple[dict[str, object], ...]:
        self.command_limits.append(limit)
        return tuple(self.commands.values())[:limit]

    async def get_command_detail(self, command_id: UUID) -> dict[str, object] | None:
        self.command_detail_calls.append(command_id)
        command = self.commands.get(command_id)
        if command is None:
            return None
        return {
            "generated_at": _NOW,
            "command": {
                **command,
                "status": "running",
                "started_at": _NOW,
                "requested_by": "local-admin",
                "attempt_count": 51,
                "available_at": _NOW,
                "lease_expires_at": _NOW,
                "worker_state": "heartbeat_live",
            },
            "progress": {
                "total": 2,
                "pending": 1,
                "running": 1,
                "succeeded": 0,
                "failed": 0,
                "paused": 0,
                "completed": 0,
                "active_source_key": "city-events",
                "active_display_name": "City events",
                "active_phase": "collecting",
                "updated_at": _NOW,
            },
            "runs": [
                {
                    "position": 0,
                    "source_key": "city-events",
                    "display_name": "City events",
                    "run_key": "cadence:city-events:20260723T183000Z",
                    "status": "running",
                    "phase": "collecting",
                    "linked_at": _NOW,
                    "started_at": _NOW,
                    "completed_at": None,
                    "candidate_count": None,
                    "canonical_count": None,
                    "error_code": None,
                    "attempt_count": 1,
                    "duration_ms": 500,
                    "updated_at": _NOW,
                    "lease_token": "never-render-this",
                    "provider_payload": "never-render-this-either",
                },
                {
                    "position": 1,
                    "source_key": "other-events",
                    "display_name": "Other events",
                    "run_key": "cadence:other-events:20260723T183000Z",
                    "status": "pending",
                    "phase": "awaiting_dispatch",
                    "linked_at": _NOW,
                    "started_at": None,
                    "completed_at": None,
                    "candidate_count": None,
                    "canonical_count": None,
                    "error_code": None,
                    "attempt_count": None,
                    "duration_ms": None,
                    "updated_at": _NOW,
                },
            ],
        }

    async def get_filter_metadata(self, **kwargs: object) -> object:
        self.filter_calls.append(kwargs)
        value = lambda name, count: SimpleNamespace(value=name, count=count)  # noqa: E731
        return SimpleNamespace(
            modes=(value("public_jsonld", 3),),
            publishers=(value("City", 2),),
            regions=(value("bay_area_9_county", 3),),
        )

    async def get_source_detail(
        self,
        source_key: str,
        *,
        window_hours: int,
        bucket_hours: int,
        include_fixtures: bool,
    ) -> object | None:
        self.detail_calls.append(
            {
                "source_key": source_key,
                "window_hours": window_hours,
                "bucket_hours": bucket_hours,
                "include_fixtures": include_fixtures,
            }
        )
        if source_key == "missing-source":
            return None
        latest = _run()
        latest.pop("source_key")
        latest.pop("display_name")
        return {
            "generated_at": _NOW,
            "source": {
                "source_key": source_key,
                "display_name": "City events",
                "publisher": "City",
                "mode": "public_jsonld",
                "region": "bay_area_9_county",
                "seed_url": "https://events.example.org/calendar",
                "seed_host": "events.example.org",
                "enabled": True,
                "handoff_only": True,
                "approved_origins": ["https://events.example.org"],
                "reviewed_at": _NOW,
                "review_expires_at": None,
                "refresh_interval_minutes": 60,
                "min_interval_ms": 1_500,
                "page_limit": 1,
                "source_revision": 4,
                "review_status": "reviewed",
                "effective_status": "active",
                "policy_blocked": False,
                "due": False,
                "last_succeeded_at": _NOW,
                "next_due_at": _NOW,
                "event_count": 10,
                "total_event_count": 25,
                "upcoming_event_count": 12,
                "latest_run": latest,
            },
            "window": {
                "hours": window_hours,
                "bucket_hours": bucket_hours,
                "starts_at": _NOW,
                "ends_at": _NOW,
            },
            "summary": {
                "total_runs": 2,
                "succeeded_runs": 1,
                "failed_runs": 1,
                "running_runs": 0,
                "success_rate": 0.5,
                "candidate_count": 12,
                "canonical_count": 10,
                "yield_rate": 10 / 12,
                "average_duration_ms": 250,
                "p95_duration_ms": 250,
                "latest_success_at": _NOW,
                "latest_failure_at": _NOW,
            },
            "history": [
                {
                    "bucket_start": _NOW,
                    "total_runs": 2,
                    "succeeded_runs": 1,
                    "failed_runs": 1,
                    "candidate_count": 12,
                    "canonical_count": 10,
                    "average_duration_ms": 250,
                }
            ],
            "recent_runs": [
                _run(),
                _run(
                    run_key="legacy:city-events:20260723T180000Z",
                    status="failed",
                    error="source_timeout",
                    is_latest_for_source=False,
                    resolved_by_newer_success=True,
                ),
            ],
            "current_build": {
                "release_revision": "worker-abc123",
                "image_digest": f"sha256:{'a' * 64}",
            },
            "raw_provider_payload": "never-render-this",
        }

    async def list_source_events(
        self,
        source_key: str,
        *,
        query: str | None,
        after_start_at: datetime | None,
        after_canonical_event_id: UUID | None,
        limit: int,
    ) -> dict[str, object]:
        self.event_calls.append(
            {
                "source_key": source_key,
                "query": query,
                "after_start_at": after_start_at,
                "after_canonical_event_id": after_canonical_event_id,
                "limit": limit,
            }
        )
        event_id = UUID("33333333-3333-4333-8333-333333333333")
        return {
            "items": [
                {
                    "canonical_event_id": event_id,
                    "title": "Parsed event",
                    "start_at": _NOW,
                    "end_at": None,
                    "venue_name": "Civic Hall",
                    "city": "sanfrancisco",
                    "latitude": 37.77,
                    "longitude": -122.42,
                    "description": "Normalized public description.",
                    "description_length": 30,
                    "price_status": "paid",
                    "price_min_cents": 2_500,
                    "price_max_cents": 5_000,
                    "price_currency": "USD",
                    "event_status": "scheduled",
                    "normalizer_version": 2,
                    "merge_version": 3,
                    "source_event_id": "provider-42",
                    "registration_url": "https://events.example.org/provider-42",
                    "last_seen_at": _NOW,
                    "refresh_run_key": "admin:city-events:20260723T183000Z",
                    "quality_issues": ["missing_end_time"],
                    "organizer_name": "Example Organizer",
                    "entity_profiles": [
                        {
                            "name": "Example Organizer",
                            "role": "organizer",
                            "kind": "organization",
                            "profile_url": ("https://www.linkedin.com/company/example-organizer"),
                        }
                    ],
                    "raw_provider_payload": "never-render-this",
                }
            ],
            "source_total": 10,
            "limit": limit,
            "has_more": True,
            "next_start_at": _NOW,
            "next_canonical_event_id": event_id,
            "query": query,
        }

    async def enqueue_command(
        self,
        *,
        command_id: UUID,
        action: object,
        source_key: str | None,
        requested_by: str,
    ) -> dict[str, object]:
        self.enqueue_calls.append(
            {
                "command_id": command_id,
                "action": str(action),
                "source_key": source_key,
                "requested_by": requested_by,
            }
        )
        if self.enqueue_error is not None:
            raise self.enqueue_error
        existing = self.commands.get(command_id)
        if existing is not None:
            return existing
        command = _command(command_id, action=str(action), source_key=source_key)
        self.commands[command_id] = command
        return command

    async def update_source_configuration(
        self,
        source_key: str,
        **kwargs: object,
    ) -> object:
        self.configuration_calls.append({"source_key": source_key, **kwargs})
        if self.configuration_error is not None:
            raise self.configuration_error
        return SimpleNamespace(
            source_key=source_key,
            source_revision=5,
            reviewed_at=_NOW,
            updated_at=_NOW,
        )

    async def set_sources_enabled(
        self,
        targets: tuple[IngestionSourceRevisionTarget, ...],
        *,
        enabled: bool,
        requested_by: str,
    ) -> object:
        self.bulk_configuration_calls.append(
            {
                "targets": targets,
                "enabled": enabled,
                "requested_by": requested_by,
            }
        )
        if self.bulk_configuration_error is not None:
            raise self.bulk_configuration_error
        items = [
            SimpleNamespace(
                source_key=target.source_key,
                source_revision=target.expected_revision + 1,
                reviewed_at=_NOW,
                updated_at=_NOW,
            )
            for target in targets
        ]
        return SimpleNamespace(
            enabled=enabled,
            requested=len(targets),
            updated=len(targets),
            unchanged=0,
            items=items,
        )


def _app(
    service: FakeIngestionAdmin | None = None,
    *,
    enabled: bool = True,
    mock_cloud: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        admin_ingestion_enabled=enabled,
        mock_cloud=mock_cloud,
    )
    if service is not None:
        app.state.ingestion_admin = service
    install_ingestion_admin_routes(app)
    return app


def _client(app: FastAPI, *, base_url: str = "http://localhost") -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=base_url)


async def test_admin_routes_are_absent_unless_explicitly_enabled() -> None:
    app = _app(FakeIngestionAdmin(), enabled=False)

    async with _client(app) as client:
        response = await client.get("/admin/v1/ingestion/overview")

    assert response.status_code == 404
    assert "/admin/v1/ingestion/overview" not in {route.path for route in app.routes}


async def test_every_admin_request_requires_mock_mode_loopback_host_and_service() -> None:
    service = FakeIngestionAdmin()
    local = _app(service)
    production_shaped = _app(service, mock_cloud=False)
    missing_service = _app()

    async with (
        _client(local) as client,
        _client(production_shaped) as deployed,
        _client(missing_service) as unavailable,
    ):
        accepted = await client.get(
            "/admin/v1/ingestion/overview",
            headers={
                "X-EC-Tenant-ID": "not-an-admin-boundary",
                "Authorization": "Bearer ignored-local-token",
            },
        )
        external_host = await client.get(
            "/admin/v1/ingestion/overview",
            headers={"Host": "admin.example.com"},
        )
        non_mock = await deployed.get("/admin/v1/ingestion/overview")
        absent = await unavailable.get("/admin/v1/ingestion/overview")

    assert accepted.status_code == 200
    assert service.overview_calls == 1
    assert external_host.status_code == 403
    assert external_host.json() == {"detail": "local ingestion admin requires a loopback Host"}
    assert non_mock.status_code == 403
    assert non_mock.json() == {"detail": "local ingestion admin is unavailable"}
    assert absent.status_code == 503
    assert absent.json() == {"detail": "ingestion administration is unavailable"}


@pytest.mark.parametrize(
    "base_url",
    ("http://localhost", "http://127.0.0.1:8080", "http://[::1]:8080"),
)
async def test_loopback_host_variants_are_admitted(base_url: str) -> None:
    service = FakeIngestionAdmin()

    async with _client(_app(service), base_url=base_url) as client:
        response = await client.get("/admin/v1/ingestion/overview")

    assert response.status_code == 200


async def test_overview_sources_runs_and_commands_have_exact_safe_shapes() -> None:
    service = FakeIngestionAdmin()
    app = _app(service)

    async with _client(app) as client:
        overview = await client.get("/admin/v1/ingestion/overview")
        sources = await client.get(
            "/admin/v1/ingestion/sources",
            params={
                "query": "  city  ",
                "state": "due",
                "include_fixtures": "true",
                "sort_by": "catalog_total",
                "sort_direction": "desc",
                "limit": 25,
                "offset": 50,
            },
        )
        runs = await client.get(
            "/admin/v1/ingestion/runs",
            params={
                "status": "failed",
                "source_key": "city-events",
                "include_fixtures": "true",
                "limit": 20,
                "offset": 40,
            },
        )
        commands = await client.get("/admin/v1/ingestion/commands", params={"limit": 30})

    assert overview.status_code == 200
    assert overview.json() == {
        "generated_at": "2026-07-23T18:30:00Z",
        "policy": {
            "allowed": True,
            "reason": "Reviewed public catalog policy is admitted.",
            "code": "allowed",
        },
        "summary": {
            "sources": 38,
            "active_sources": 17,
            "due_sources": 5,
            "running_runs": 1,
            "failed_runs_24h": 2,
            "catalog_events": 900,
            "pending_commands": 1,
            "fixture_sources": 497,
        },
        "latest_success_at": "2026-07-23T18:30:00Z",
    }
    assert sources.status_code == 200
    source_page = sources.json()
    assert source_page == {
        "items": [
            {
                "source_key": "city-events",
                "display_name": "City events",
                "publisher": "City",
                "mode": "public_jsonld",
                "region": "bay_area_9_county",
                "seed_url": "https://events.example.org/calendar",
                "enabled": True,
                "source_revision": 4,
                "retired_at": None,
                "retired_reason": None,
                "superseded_by_source_key": None,
                "review_status": "reviewed",
                "effective_status": "due",
                "due": True,
                "last_succeeded_at": "2026-07-23T18:30:00Z",
                "next_due_at": "2026-07-23T18:30:00Z",
                "event_count": 10,
                "total_event_count": 25,
                "upcoming_event_count": 12,
                "latest_run": {
                    "run_key": "admin:city-events:20260723T183000Z",
                    "status": "succeeded",
                    "started_at": "2026-07-23T18:30:00Z",
                    "completed_at": "2026-07-23T18:30:00Z",
                    "candidate_count": 12,
                    "canonical_count": 10,
                    "error": None,
                    "attempt_count": 1,
                    "duration_ms": None,
                    "source_revision": None,
                    "release_revision": None,
                    "image_digest": None,
                    "provenance_status": "legacy_unavailable",
                    "trigger": "cadence_or_manual",
                    "source_configuration": None,
                    "execution_configuration": None,
                    "collection_window": None,
                    "command": None,
                    "execution": None,
                    "resources": None,
                    "stage_trace": [],
                    "timeline": {
                        "evidence_scope": "lifecycle_only",
                        "complete": False,
                        "entries": [],
                    },
                },
            }
        ],
        "total": 1,
        "limit": 25,
        "offset": 50,
    }
    assert service.source_calls == [
        {
            "query": "city",
            "state": "due",
            "mode": None,
            "publisher": None,
            "region": None,
            "include_fixtures": True,
            "sort_by": "catalog_total",
            "sort_direction": "desc",
            "limit": 25,
            "offset": 50,
        }
    ]
    assert runs.status_code == 200
    run_page = runs.json()
    assert run_page["total"] == 1
    assert run_page["limit"] == 20
    assert run_page["offset"] == 40
    assert run_page["items"] == [
        {
            "run_key": "admin:city-events:20260723T183000Z",
            "status": "failed",
            "started_at": "2026-07-23T18:30:00Z",
            "completed_at": "2026-07-23T18:30:00Z",
            "candidate_count": None,
            "canonical_count": None,
            "error": "source_rate_limited",
            "attempt_count": 1,
            "duration_ms": None,
            "source_revision": None,
            "release_revision": None,
            "image_digest": None,
            "provenance_status": "legacy_unavailable",
            "trigger": "cadence_or_manual",
            "source_configuration": None,
            "execution_configuration": None,
            "collection_window": None,
            "command": None,
            "execution": None,
            "resources": None,
            "stage_trace": [],
            "timeline": {
                "evidence_scope": "lifecycle_only",
                "complete": False,
                "entries": [],
            },
            "source_key": "city-events",
            "display_name": "City events",
            "is_latest_for_source": False,
            "resolved_by_newer_success": True,
        }
    ]
    assert "lease_token" not in runs.text
    assert "provider_payload" not in runs.text
    assert service.run_calls == [
        {
            "status": "failed",
            "source_key": "city-events",
            "mode": None,
            "publisher": None,
            "region": None,
            "window_hours": None,
            "include_fixtures": True,
            "limit": 20,
            "offset": 40,
        }
    ]
    assert commands.status_code == 200
    assert commands.json() == {"items": [_command_json()]}
    assert service.command_limits == [30]
    for response in (overview, sources, runs, commands):
        assert response.headers["cache-control"] == "no-store, max-age=0"


def _command_json(command_id: UUID = _COMMAND_ID) -> dict[str, object]:
    return {
        "command_id": str(command_id),
        "action": "refresh_source",
        "source_key": "city-events",
        "status": "queued",
        "requested_at": "2026-07-23T18:30:00Z",
        "started_at": None,
        "completed_at": None,
        "result": None,
        "error_code": None,
        "source_revision": None,
        "release_revision": None,
        "image_digest": None,
        "executor_source_revision": None,
        "executor_release_revision": None,
        "executor_image_digest": None,
    }


async def test_command_detail_exposes_live_child_progress_without_private_material() -> None:
    service = FakeIngestionAdmin()

    async with _client(_app(service)) as client:
        response = await client.get(f"/admin/v1/ingestion/commands/{_COMMAND_ID}")
        missing = await client.get(f"/admin/v1/ingestion/commands/{_SECOND_COMMAND_ID}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["command"]["worker_state"] == "heartbeat_live"
    assert payload["command"]["attempt_count"] == 51
    assert payload["progress"] == {
        "total": 2,
        "pending": 1,
        "running": 1,
        "succeeded": 0,
        "failed": 0,
        "paused": 0,
        "completed": 0,
        "active_source_key": "city-events",
        "active_display_name": "City events",
        "active_phase": "collecting",
        "updated_at": "2026-07-23T18:30:00Z",
    }
    assert [run["status"] for run in payload["runs"]] == ["running", "pending"]
    assert "lease_token" not in response.text
    assert "provider_payload" not in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert missing.status_code == 404
    assert missing.json() == {"detail": "ingestion command not found"}
    assert service.command_detail_calls == [_COMMAND_ID, _SECOND_COMMAND_ID]


async def test_filter_metadata_and_source_detail_have_bounded_safe_contracts() -> None:
    service = FakeIngestionAdmin()

    async with _client(_app(service)) as client:
        filters = await client.get(
            "/admin/v1/ingestion/filters",
            params={
                "query": "  city  ",
                "state": "active",
                "mode": "public_jsonld",
                "publisher": "City",
                "region": "bay_area_9_county",
                "include_fixtures": "true",
            },
        )
        detail = await client.get(
            "/admin/v1/ingestion/sources/city-events",
            params={"window_hours": 24, "bucket_hours": 1},
        )
        missing = await client.get(
            "/admin/v1/ingestion/sources/missing-source",
        )
        invalid_bucket = await client.get(
            "/admin/v1/ingestion/sources/city-events",
            params={"window_hours": 25, "bucket_hours": 24},
        )

    assert filters.status_code == 200
    assert filters.json() == {
        "modes": [{"value": "public_jsonld", "count": 3}],
        "publishers": [{"value": "City", "count": 2}],
        "regions": [{"value": "bay_area_9_county", "count": 3}],
        "source_states": [
            {"value": "all", "label": "All states"},
            {"value": "active", "label": "Active"},
            {"value": "due", "label": "Due"},
            {"value": "blocked", "label": "Blocked"},
            {"value": "failed", "label": "Failed"},
        ],
        "run_statuses": [
            {"value": "running", "label": "Running"},
            {"value": "paused", "label": "Paused"},
            {"value": "succeeded", "label": "Succeeded"},
            {"value": "failed", "label": "Failed"},
        ],
        "window_hours": [24, 168, 720],
    }
    assert service.filter_calls == [
        {
            "query": "city",
            "state": "active",
            "mode": "public_jsonld",
            "publisher": "City",
            "region": "bay_area_9_county",
            "include_fixtures": True,
        }
    ]
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["source"]["source_key"] == "city-events"
    assert payload["source"]["seed_host"] == "events.example.org"
    assert payload["source"]["source_revision"] == 4
    assert payload["summary"]["success_rate"] == 0.5
    assert payload["current_build"]["release_revision"] == "worker-abc123"
    assert payload["recent_runs"][0]["provenance_status"] == "legacy_unavailable"
    assert payload["recent_runs"][0]["is_latest_for_source"] is True
    assert payload["recent_runs"][0]["resolved_by_newer_success"] is False
    assert payload["recent_runs"][1]["is_latest_for_source"] is False
    assert payload["recent_runs"][1]["resolved_by_newer_success"] is True
    assert "raw_provider_payload" not in payload
    assert service.detail_calls == [
        {
            "source_key": "city-events",
            "window_hours": 24,
            "bucket_hours": 1,
            "include_fixtures": False,
        },
        {
            "source_key": "missing-source",
            "window_hours": 168,
            "bucket_hours": 24,
            "include_fixtures": False,
        },
    ]
    assert missing.status_code == 404
    assert invalid_bucket.status_code == 422
    for response in (filters, detail):
        assert response.headers["cache-control"] == "no-store, max-age=0"


async def test_source_event_table_exposes_parsed_projection_and_safe_provenance() -> None:
    service = FakeIngestionAdmin()
    event_id = "33333333-3333-4333-8333-333333333333"

    async with _client(_app(service)) as client:
        response = await client.get(
            "/admin/v1/ingestion/sources/city-events/events",
            params={"q": " civic ", "limit": 20},
        )
        invalid_cursor = await client.get(
            "/admin/v1/ingestion/sources/city-events/events",
            params={"after_start_at": _NOW.isoformat()},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_total"] == 10
    assert payload["has_more"] is True
    assert payload["next_canonical_event_id"] == event_id
    assert payload["items"][0] == {
        "canonical_event_id": event_id,
        "title": "Parsed event",
        "start_at": _NOW.isoformat().replace("+00:00", "Z"),
        "end_at": None,
        "venue_name": "Civic Hall",
        "city": "sanfrancisco",
        "latitude": 37.77,
        "longitude": -122.42,
        "description": "Normalized public description.",
        "description_length": 30,
        "price_status": "paid",
        "price_min_cents": 2_500,
        "price_max_cents": 5_000,
        "price_currency": "USD",
        "event_status": "scheduled",
        "normalizer_version": 2,
        "merge_version": 3,
        "source_event_id": "provider-42",
        "registration_url": "https://events.example.org/provider-42",
        "last_seen_at": _NOW.isoformat().replace("+00:00", "Z"),
        "refresh_run_key": "admin:city-events:20260723T183000Z",
        "quality_issues": ["missing_end_time"],
        "organizer_name": "Example Organizer",
        "host_names": [],
        "speaker_names": [],
        "partner_names": [],
        "entity_profiles": [
            {
                "name": "Example Organizer",
                "role": "organizer",
                "kind": "organization",
                "profile_url": "https://www.linkedin.com/company/example-organizer",
            }
        ],
        "attendance_count": None,
        "registration_status": "unknown",
    }
    assert "raw_provider_payload" not in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert service.event_calls == [
        {
            "source_key": "city-events",
            "query": " civic ",
            "after_start_at": None,
            "after_canonical_event_id": None,
            "limit": 20,
        }
    ]
    assert invalid_cursor.status_code == 422


def test_run_projection_names_worker_claim_provenance_without_implying_execution() -> None:
    claim = {
        **_run(),
        "source_revision": 4,
        "release_revision": "worker-abc1234",
        "image_digest": f"sha256:{'4' * 64}",
        "provenance_status": "claim_recorded",
    }

    projected = IngestionRunOut.model_validate(claim)

    assert projected.provenance_status == "claim_recorded"
    assert projected.release_revision == "worker-abc1234"
    assert projected.is_latest_for_source is True
    assert projected.resolved_by_newer_success is False
    with pytest.raises(ValidationError):
        IngestionRunOut.model_validate({**claim, "provenance_status": "recorded"})


def test_run_projection_exposes_bounded_execution_evidence_without_raw_logs() -> None:
    run = {
        **_run(),
        "source_configuration": {
            "mode": "public_jsonld",
            "reviewed_at": _NOW,
            "review_expires_at": None,
            "refresh_interval_minutes": 120,
            "min_interval_ms": 1_500,
            "page_limit": 40,
            "seed_url": "https://never-render.example/provider",
        },
        "command": {
            "command_id": _COMMAND_ID,
            "action": "refresh_source",
            "requested_at": _NOW,
            "started_at": _NOW,
            "completed_at": _NOW,
            "lease_token": "never-render-this",
        },
        "execution": {
            "execution_path": "guarded_direct",
            "worker_service": "ingestion-command-worker",
            "task_queue": None,
            "adapter_id": "public_jsonld",
            "adapter_module": "src/events_concierge/adapters/crawl/public_jsonld.py",
            "adapter_symbol": "PublicJsonLdCatalogSource.fetch",
            "orchestration_module": "src/events_concierge/application/catalog_refresh.py",
            "orchestration_symbol": "CatalogRefreshService.refresh",
            "worker_module": "src/events_concierge/workers/ingestion_commands.py",
            "worker_symbol": "IngestionCommandWorker.run",
            "shell_command": "never-render-this",
        },
        "resources": {
            "execution_count": 1,
            "wall_time_ms": 81,
            "process_cpu_time_ms": 24,
            "cpu_utilization_percent": 29.63,
            "rss_before_bytes": 10_000,
            "rss_after_bytes": 12_000,
            "boundary_observed_peak_rss_bytes": 12_000,
            "process_lifetime_peak_rss_bytes": 24_000,
            "measurement_source": "python_monotonic+process_time+linux_procfs+getrusage",
            "measurement_scope": "worker_process_boundary_samples",
            "measurement_quality": "best_effort_process_delta_sequential_worker",
            "last_outcome_code": "succeeded",
            "first_observed_at": _NOW,
            "last_observed_at": _NOW,
            "process_environment": "never-render-this",
        },
        "stage_trace": [
            {
                "stage": "collect",
                "label": "Collect",
                "evidence_status": "measured",
                "observation_count": 1,
                "duration_ms": 42,
                "last_outcome_code": "succeeded",
                "first_observed_at": _NOW,
                "last_observed_at": _NOW,
                "note_code": "adapter_boundary_includes_extract_enrich",
                "raw_log": "never-render-this",
            }
        ],
        "timeline": {
            "evidence_scope": "lifecycle_and_aggregate_observations",
            "complete": False,
            "entries": [
                {
                    "observed_at": _NOW,
                    "event_code": "stage_observed",
                    "timestamp_basis": "evidence_recorded",
                    "stage": "collect",
                    "outcome_code": "succeeded",
                    "duration_ms": 42,
                    "observation_count": 1,
                    "raw_log": "never-render-this",
                    "message": "never-render-this",
                }
            ],
            "provider_payload": "never-render-this",
        },
    }

    payload = IngestionRunOut.model_validate(run).model_dump(mode="json")

    assert payload["execution"]["worker_module"] == (
        "src/events_concierge/workers/ingestion_commands.py"
    )
    assert payload["resources"]["measurement_scope"] == "worker_process_boundary_samples"
    assert payload["stage_trace"] == [
        {
            "stage": "collect",
            "label": "Collect",
            "evidence_status": "measured",
            "observation_count": 1,
            "duration_ms": 42,
            "last_outcome_code": "succeeded",
            "first_observed_at": _NOW.isoformat().replace("+00:00", "Z"),
            "last_observed_at": _NOW.isoformat().replace("+00:00", "Z"),
            "note_code": "adapter_boundary_includes_extract_enrich",
        }
    ]
    assert payload["timeline"] == {
        "evidence_scope": "lifecycle_and_aggregate_observations",
        "complete": False,
        "entries": [
            {
                "observed_at": _NOW.isoformat().replace("+00:00", "Z"),
                "event_code": "stage_observed",
                "timestamp_basis": "evidence_recorded",
                "stage": "collect",
                "outcome_code": "succeeded",
                "duration_ms": 42,
                "observation_count": 1,
            }
        ],
    }
    rendered = str(payload)
    assert "never-render-this" not in rendered


def test_run_timeline_rejects_unbounded_or_untyped_log_material() -> None:
    base_entry = {
        "observed_at": _NOW,
        "event_code": "stage_observed",
        "timestamp_basis": "evidence_recorded",
        "stage": "collect",
        "outcome_code": "succeeded",
        "duration_ms": 1,
        "observation_count": 1,
    }
    with pytest.raises(ValidationError):
        IngestionRunOut.model_validate(
            {
                **_run(),
                "timeline": {
                    "evidence_scope": "lifecycle_and_aggregate_observations",
                    "complete": True,
                    "entries": [base_entry],
                },
            }
        )
    with pytest.raises(ValidationError):
        IngestionRunOut.model_validate(
            {
                **_run(),
                "timeline": {
                    "evidence_scope": "lifecycle_and_aggregate_observations",
                    "complete": False,
                    "entries": [
                        {
                            **base_entry,
                            "timestamp_basis": "durable_transition",
                        }
                    ],
                },
            }
        )
    with pytest.raises(ValidationError):
        IngestionRunOut.model_validate(
            {
                **_run(),
                "timeline": {
                    "evidence_scope": "lifecycle_only",
                    "complete": False,
                    "entries": [base_entry],
                },
            }
        )
    with pytest.raises(ValidationError):
        IngestionRunOut.model_validate(
            {
                **_run(),
                "timeline": {
                    "evidence_scope": "lifecycle_and_aggregate_observations",
                    "complete": False,
                    "entries": [{**base_entry, "event_code": "raw_log"}],
                },
            }
        )
    with pytest.raises(ValidationError):
        IngestionRunOut.model_validate(
            {
                **_run(),
                "timeline": {
                    "evidence_scope": "lifecycle_and_aggregate_observations",
                    "complete": False,
                    "entries": [base_entry] * 12,
                },
            }
        )


async def test_command_post_only_enqueues_and_exact_replay_returns_the_same_receipt() -> None:
    service = FakeIngestionAdmin()
    app = _app(service)
    body = {
        "command_id": str(_COMMAND_ID),
        "action": "refresh_source",
        "source_key": "city-events",
    }

    async with _client(app) as client:
        first = await client.post("/admin/v1/ingestion/commands", json=body)
        replay = await client.post("/admin/v1/ingestion/commands", json=body)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.json() == _command_json()
    assert replay.json() == first.json()
    assert service.inline_execution_calls == 0
    assert service.enqueue_calls == [
        {
            "command_id": _COMMAND_ID,
            "action": "refresh_source",
            "source_key": "city-events",
            "requested_by": "local-admin",
        },
        {
            "command_id": _COMMAND_ID,
            "action": "refresh_source",
            "source_key": "city-events",
            "requested_by": "local-admin",
        },
    ]
    assert "lease_token" not in first.text
    assert first.headers["cache-control"] == "no-store, max-age=0"


async def test_command_post_rejects_cross_origin_but_accepts_absent_or_exact_origin() -> None:
    service = FakeIngestionAdmin()
    app = _app(service)
    first = {
        "command_id": str(_COMMAND_ID),
        "action": "refresh_source",
        "source_key": "city-events",
    }
    second = {
        "command_id": str(_SECOND_COMMAND_ID),
        "action": "refresh_due",
    }

    async with _client(app, base_url="http://localhost:8080") as client:
        cross_origin = await client.post(
            "/admin/v1/ingestion/commands",
            json=second,
            headers={"Origin": "http://evil.example"},
        )
        misleading_port = await client.post(
            "/admin/v1/ingestion/commands",
            json=second,
            headers={"Origin": "http://localhost"},
        )
        absent = await client.post("/admin/v1/ingestion/commands", json=first)
        same_origin = await client.post(
            "/admin/v1/ingestion/commands",
            json=second,
            headers={"Origin": "http://localhost:8080"},
        )

    assert cross_origin.status_code == 403
    assert misleading_port.status_code == 403
    assert absent.status_code == 202
    assert same_origin.status_code == 202
    assert len(service.enqueue_calls) == 2


async def test_source_configuration_patch_is_reviewed_bounded_and_optimistic() -> None:
    service = FakeIngestionAdmin()
    body = {
        "expected_revision": 4,
        "seed_url": "https://events.example.org/calendar",
        "approved_origins": ["https://events.example.org"],
        "mode": "public_jsonld",
        "enabled": True,
        "handoff_only": True,
        "review_expires_at": "2027-07-23T18:30:00Z",
        "refresh_interval_minutes": 90,
        "min_interval_ms": 2_000,
        "page_limit": 2,
        "review_acknowledged": True,
    }

    async with _client(_app(service), base_url="http://localhost:8080") as client:
        cross_origin = await client.patch(
            "/admin/v1/ingestion/sources/city-events",
            json=body,
            headers={"Origin": "http://evil.example"},
        )
        accepted = await client.patch(
            "/admin/v1/ingestion/sources/city-events",
            json=body,
            headers={"Origin": "http://localhost:8080"},
        )
        unacknowledged = await client.patch(
            "/admin/v1/ingestion/sources/city-events",
            json={**body, "review_acknowledged": False},
        )

    assert cross_origin.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {
        "source_key": "city-events",
        "source_revision": 5,
        "reviewed_at": "2026-07-23T18:30:00Z",
        "updated_at": "2026-07-23T18:30:00Z",
    }
    assert unacknowledged.status_code == 422
    assert service.configuration_calls == [
        {
            "source_key": "city-events",
            "expected_revision": 4,
            "seed_url": "https://events.example.org/calendar",
            "approved_origins": ("https://events.example.org",),
            "mode": "public_jsonld",
            "enabled": True,
            "handoff_only": True,
            "review_expires_at": datetime(2027, 7, 23, 18, 30, tzinfo=UTC),
            "refresh_interval_minutes": 90,
            "min_interval_ms": 2_000,
            "page_limit": 2,
            "requested_by": "local-admin",
        }
    ]


async def test_source_configuration_conflict_returns_a_fixed_safe_code() -> None:
    service = FakeIngestionAdmin()
    service.configuration_error = IngestionSourceConfigurationConflictError(
        "source_revision_conflict"
    )

    async with _client(_app(service)) as client:
        response = await client.patch(
            "/admin/v1/ingestion/sources/city-events",
            json={
                "expected_revision": 4,
                "seed_url": "https://events.example.org/calendar",
                "approved_origins": ["https://events.example.org"],
                "mode": "public_jsonld",
                "enabled": True,
                "handoff_only": True,
                "review_expires_at": None,
                "refresh_interval_minutes": 60,
                "min_interval_ms": 1_500,
                "page_limit": 1,
                "review_acknowledged": True,
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "source_revision_conflict"}


async def test_bulk_source_enabled_patch_is_bounded_reviewed_and_same_origin() -> None:
    service = FakeIngestionAdmin()
    body = {
        "targets": [
            {"source_key": "city-events", "expected_revision": 4},
            {"source_key": "county-events", "expected_revision": 7},
        ],
        "enabled": False,
        "review_acknowledged": True,
    }

    async with _client(_app(service), base_url="http://localhost:8080") as client:
        cross_origin = await client.patch(
            "/admin/v1/ingestion/sources/bulk/enabled",
            json=body,
            headers={"Origin": "http://evil.example"},
        )
        accepted = await client.patch(
            "/admin/v1/ingestion/sources/bulk/enabled",
            json=body,
            headers={"Origin": "http://localhost:8080"},
        )
        duplicate = await client.patch(
            "/admin/v1/ingestion/sources/bulk/enabled",
            json={
                **body,
                "targets": [
                    {"source_key": "city-events", "expected_revision": 4},
                    {"source_key": "city-events", "expected_revision": 5},
                ],
            },
        )
        unacknowledged = await client.patch(
            "/admin/v1/ingestion/sources/bulk/enabled",
            json={**body, "review_acknowledged": False},
        )

    assert cross_origin.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {
        "enabled": False,
        "requested": 2,
        "updated": 2,
        "unchanged": 0,
        "items": [
            {
                "source_key": "city-events",
                "source_revision": 5,
                "reviewed_at": "2026-07-23T18:30:00Z",
                "updated_at": "2026-07-23T18:30:00Z",
            },
            {
                "source_key": "county-events",
                "source_revision": 8,
                "reviewed_at": "2026-07-23T18:30:00Z",
                "updated_at": "2026-07-23T18:30:00Z",
            },
        ],
    }
    assert duplicate.status_code == 422
    assert unacknowledged.status_code == 422
    assert len(service.bulk_configuration_calls) == 1
    forwarded = service.bulk_configuration_calls[0]
    forwarded_targets = cast(
        "tuple[IngestionSourceRevisionTarget, ...]",
        forwarded["targets"],
    )
    assert [(target.source_key, target.expected_revision) for target in forwarded_targets] == [
        ("city-events", 4),
        ("county-events", 7),
    ]
    assert forwarded["enabled"] is False
    assert forwarded["requested_by"] == "local-admin"


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    (
        (
            IngestionSourceConfigurationConflictError("source_revision_conflict"),
            409,
            "source_revision_conflict",
        ),
        (
            IngestionSourceConfigurationUnavailableError("source_unavailable"),
            409,
            "source_unavailable",
        ),
        (
            IngestionSourceNotFoundError("source_not_found"),
            404,
            "ingestion source not found",
        ),
    ),
)
async def test_bulk_source_enabled_patch_has_fixed_atomic_error_codes(
    error: Exception,
    status_code: int,
    detail: str,
) -> None:
    service = FakeIngestionAdmin()
    service.bulk_configuration_error = error

    async with _client(_app(service)) as client:
        response = await client.patch(
            "/admin/v1/ingestion/sources/bulk/enabled",
            json={
                "targets": [{"source_key": "city-events", "expected_revision": 4}],
                "enabled": True,
                "review_acknowledged": True,
            },
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize(
    ("body", "path"),
    (
        (
            {
                "command_id": str(_SECOND_COMMAND_ID),
                "action": "refresh_source",
            },
            "/admin/v1/ingestion/commands",
        ),
        (
            {
                "command_id": str(_SECOND_COMMAND_ID),
                "action": "refresh_due",
                "source_key": "city-events",
            },
            "/admin/v1/ingestion/commands",
        ),
        (
            {
                "command_id": str(_SECOND_COMMAND_ID),
                "action": "delete_everything",
            },
            "/admin/v1/ingestion/commands",
        ),
        (
            {
                "command_id": str(_SECOND_COMMAND_ID),
                "action": "refresh_source",
                "source_key": "HTTPS://attacker.example",
            },
            "/admin/v1/ingestion/commands",
        ),
    ),
)
async def test_command_body_has_a_closed_bounded_vocabulary(
    body: dict[str, object],
    path: str,
) -> None:
    service = FakeIngestionAdmin()

    async with _client(_app(service)) as client:
        response = await client.post(path, json=body)

    assert response.status_code == 422
    assert service.enqueue_calls == []


@pytest.mark.parametrize(
    ("path", "params"),
    (
        ("/admin/v1/ingestion/sources", {"state": "unknown"}),
        ("/admin/v1/ingestion/sources", {"limit": 101}),
        ("/admin/v1/ingestion/sources", {"offset": 100_001}),
        ("/admin/v1/ingestion/sources", {"sort_by": "seed_url"}),
        ("/admin/v1/ingestion/sources", {"sort_direction": "sideways"}),
        ("/admin/v1/ingestion/runs", {"status": "unknown"}),
        ("/admin/v1/ingestion/runs", {"source_key": "../secret"}),
        ("/admin/v1/ingestion/runs", {"limit": 0}),
        ("/admin/v1/ingestion/commands", {"limit": 101}),
    ),
)
async def test_admin_query_filters_and_pagination_are_bounded(
    path: str,
    params: dict[str, object],
) -> None:
    async with _client(_app(FakeIngestionAdmin())) as client:
        response = await client.get(path, params=params)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "expected_status", "detail"),
    (
        (IngestionSourceNotFoundError("source_not_found"), 404, "ingestion source not found"),
        (
            IngestionCommandUnavailableError("source_unavailable"),
            409,
            "source_unavailable",
        ),
        (IngestionCommandConflictError("command_conflict"), 409, "command_conflict"),
        (IngestionCommandRejectedError("invalid_command"), 422, "invalid_command"),
    ),
)
async def test_command_errors_map_only_fixed_safe_codes(
    error: Exception,
    expected_status: int,
    detail: str,
) -> None:
    service = FakeIngestionAdmin()
    service.enqueue_error = error

    async with _client(_app(service)) as client:
        response = await client.post(
            "/admin/v1/ingestion/commands",
            json={
                "command_id": str(_SECOND_COMMAND_ID),
                "action": "refresh_source",
                "source_key": "city-events",
            },
        )

    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}


async def test_unexpected_service_failure_is_redacted_as_unavailable() -> None:
    service = FakeIngestionAdmin()
    service.enqueue_error = RuntimeError("postgresql://operator:secret@database/private")

    async with _client(_app(service)) as client:
        response = await client.post(
            "/admin/v1/ingestion/commands",
            json={
                "command_id": str(_SECOND_COMMAND_ID),
                "action": "refresh_due",
            },
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "ingestion administration is unavailable"}
    assert "secret" not in response.text


async def test_unrecognized_domain_error_code_is_not_reflected() -> None:
    service = FakeIngestionAdmin()
    service.enqueue_error = IngestionCommandUnavailableError(
        "postgresql://operator:secret@database/private"
    )

    async with _client(_app(service)) as client:
        response = await client.post(
            "/admin/v1/ingestion/commands",
            json={
                "command_id": str(_SECOND_COMMAND_ID),
                "action": "refresh_due",
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "ingestion command unavailable"}
    assert "secret" not in response.text
