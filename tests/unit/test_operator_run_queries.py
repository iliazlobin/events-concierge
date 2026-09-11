"""HTTP query constraints and exact lookup routing without database or provider I/O."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.unit.test_admin_api import FakeIngestionAdmin, _app, _client, _run


@pytest.mark.parametrize(
    "params",
    [
        {"started_after": "2026-09-01T00:00:00Z"},
        {"started_after": "2026-09-01T00:00:00", "started_before": "2026-09-02T00:00:00"},
        {"started_after": "2026-09-02T00:00:00Z", "started_before": "2026-09-01T00:00:00Z"},
        {"started_after": "2026-01-01T00:00:00Z", "started_before": "2026-09-01T00:00:00Z"},
        {"sort_by": "stage_duration"},
        {"stage_outcome": "failed"},
        {"stage": "unmeasured"},
        {"query": "private\ncontent"},
        {"sort_by": "SELECT"},
    ],
)
async def test_invalid_run_drilldowns_are_rejected_before_service(params: dict[str, str]) -> None:
    service = FakeIngestionAdmin()
    async with _client(_app(service)) as client:
        response = await client.get("/admin/v1/ingestion/runs", params=params)
    assert response.status_code == 422
    assert service.run_calls == []


async def test_run_query_forwards_global_scope_and_anchored_interval() -> None:
    service = FakeIngestionAdmin()
    async with _client(_app(service)) as client:
        response = await client.get(
            "/admin/v1/ingestion/runs",
            params={
                "query": "library",
                "sort_by": "stage_duration",
                "sort_direction": "asc",
                "stage": "collect",
                "stage_outcome": "failed",
                "started_after": "2026-09-01T00:00:00Z",
                "started_before": "2026-09-02T00:00:00Z",
                "window_hours": "1",
            },
        )
    assert response.status_code == 200
    call = service.run_calls[0]
    assert call["query"] == "library" and call["sort_by"] == "stage_duration"
    assert call["started_after"] == datetime(2026, 9, 1, tzinfo=UTC)
    assert call["started_before"] == datetime(2026, 9, 2, tzinfo=UTC)
    assert call["stage_outcome"] == "failed"


async def test_exact_lookup_returns_rich_run_or_no_store_not_found() -> None:
    class Service(FakeIngestionAdmin):
        async def lookup_run(
            self, source_key: str, run_key: str, *, include_fixtures: bool = False
        ) -> object:
            assert source_key == "city-events" and not include_fixtures
            return _run() if run_key == "known" else None

    async with _client(_app(Service())) as client:
        found = await client.get(
            "/admin/v1/ingestion/runs/lookup",
            params={"source_key": "city-events", "run_key": "known"},
        )
        missing = await client.get(
            "/admin/v1/ingestion/runs/lookup",
            params={"source_key": "city-events", "run_key": "unknown"},
        )
        invalid = await client.get(
            "/admin/v1/ingestion/runs/lookup",
            params={"source_key": "city-events", "run_key": "unsafe\nkey"},
        )
    assert found.status_code == 200 and "stage_trace" in found.json()
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store, max-age=0"
    assert invalid.status_code == 422
