"""Exact run attribution uses an operator read before pagination, never client filtering."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from tests.unit.test_admin_api import FakeIngestionAdmin, _app, _client

from events_concierge.application.ingestion_admin import IngestionAdminService
from events_concierge.domain.ingestion_admin import IngestionCatalogEventPage
from events_concierge.ports.ingestion_admin import IngestionAdminRepository


class Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def browse_run_events(
        self, source: str, run: str, **kwargs: object
    ) -> IngestionCatalogEventPage:
        self.calls.append((source, run, kwargs))
        return IngestionCatalogEventPage((), 0, 2, False, None, None, "music", run)


async def test_run_catalog_passes_exact_source_run_search_and_cursor_to_bounded_read() -> None:
    repository = Repository()
    service = IngestionAdminService(cast(IngestionAdminRepository, repository))
    stamp = datetime(2026, 9, 8, tzinfo=UTC)
    identifier = UUID("019a7137-8b68-7bf4-b75c-000100000001")
    page = await service.list_source_events(
        "city-events",
        run_key="manual:city-events:one",
        query=" music ",
        after_start_at=stamp,
        after_canonical_event_id=identifier,
        limit=2,
    )
    assert page.run_key == "manual:city-events:one"
    assert repository.calls == [
        (
            "city-events",
            "manual:city-events:one",
            {
                "query": "music",
                "after_start_at": stamp,
                "after_canonical_event_id": identifier,
                "limit": 2,
            },
        )
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"run_key": ""},
        {"run_key": "x" * 257},
        {"run_key": "manual:bad\nsecret"},
        {"run_key": "manual:one", "after_start_at": datetime(2026, 9, 8)},
        {"run_key": "manual:one", "limit": 101},
    ],
)
async def test_invalid_run_catalog_queries_do_not_reach_repository(
    kwargs: dict[str, object],
) -> None:
    repository = Repository()
    service = IngestionAdminService(cast(IngestionAdminRepository, repository))
    with pytest.raises(ValueError):
        await service.list_source_events("city-events", **kwargs)
    assert repository.calls == []


async def test_http_run_filter_is_read_only_no_store_and_keeps_operator_auth() -> None:
    repository = Repository()
    service = IngestionAdminService(cast(IngestionAdminRepository, repository))
    app = _app(cast(FakeIngestionAdmin, service))
    path = "/admin/v1/ingestion/sources/city-events/events"
    async with _client(app) as client:
        response = await client.get(
            path, params={"run_key": "manual:one", "limit": 2, "q": "music"}
        )
        assert response.status_code == 200
        assert response.json()["run_key"] == "manual:one"
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert (await client.post(path, json={"run_key": "manual:one"})).status_code == 405
        assert (await client.get(path, params={"run_key": "bad\nkey"})).status_code == 422
    async with _client(_app(cast(FakeIngestionAdmin, service), mock_cloud=False)) as client:
        assert (await client.get(path, params={"run_key": "manual:one"})).status_code == 403
    assert len(repository.calls) == 1
