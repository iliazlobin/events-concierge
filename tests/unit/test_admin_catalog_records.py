"""The global catalog inventory retains operator-only auth and validates every scope."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from tests.unit.test_admin_api import FakeIngestionAdmin, _app, _client

from events_concierge.application.ingestion_admin import IngestionAdminService
from events_concierge.domain.ingestion_admin import IngestionCatalogRecordPage
from events_concierge.ports.ingestion_admin import IngestionAdminRepository

_NOW = datetime(2026, 9, 8, tzinfo=UTC)
_ID = UUID("019a7137-8b68-7bf4-b75c-000100000001")
_PATH = "/admin/v1/ingestion/events"


class Repository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.failure = False

    async def browse_catalog_records(self, **kwargs: object) -> IngestionCatalogRecordPage:
        self.calls.append(kwargs)
        if self.failure:
            raise RuntimeError("private database connection secret")
        return IngestionCatalogRecordPage(
            generated_at=_NOW,
            items=(),
            total=0,
            limit=cast(int, kwargs["limit"]),
            has_more=False,
            next_start_at=None,
            next_canonical_event_id=None,
            query=cast(str | None, kwargs["query"]),
            source_key=cast(str | None, kwargs["source_key"]),
            run_key=cast(str | None, kwargs["run_key"]),
            date_scope=cast(str, kwargs["date_scope"]),
            price_status=cast(str, kwargs["price_status"]),
            upcoming_total=0,
            source_counts=(),
            source_count=0,
            source_counts_truncated=False,
        )


def _service(repository: Repository) -> IngestionAdminService:
    return IngestionAdminService(cast(IngestionAdminRepository, repository))


async def test_global_catalog_defaults_and_exact_scopes_are_forwarded() -> None:
    repository = Repository()
    service = _service(repository)
    await service.list_catalog_records()
    assert repository.calls.pop() == {
        "source_key": None,
        "run_key": None,
        "query": None,
        "date_scope": "all",
        "price_status": "all",
        "limit": 20,
        "after_start_at": None,
        "after_canonical_event_id": None,
    }
    page = await service.list_catalog_records(
        source_key="city-events",
        run_key="manual:one",
        query=" music ",
        date_scope="past",
        price_status="free",
        limit=2,
        after_start_at=datetime.fromisoformat("2026-09-07T17:00:00-07:00"),
        after_canonical_event_id=_ID,
    )
    assert page.query == "music" and page.source_key == "city-events"
    assert page.run_key == "manual:one" and page.date_scope == "past"
    assert repository.calls == [
        {
            "source_key": "city-events",
            "run_key": "manual:one",
            "query": "music",
            "date_scope": "past",
            "price_status": "free",
            "limit": 2,
            "after_start_at": _NOW,
            "after_canonical_event_id": _ID,
        }
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"run_key": "manual:one"},
        {"source_key": "x"},
        {"source_key": "city-events", "run_key": ""},
        {"source_key": "city-events", "run_key": "bad\nkey"},
        {"source_key": "city-events", "run_key": "r" * 257},
        {"date_scope": "yesterday"},
        {"price_status": "cheap"},
        {"query": "x" * 161},
        {"query": "x\ny"},
        {"limit": 101},
        {"after_start_at": _NOW},
        {"after_canonical_event_id": _ID},
        {"after_start_at": datetime(2026, 9, 8), "after_canonical_event_id": _ID},
    ],
)
async def test_invalid_scope_never_reaches_repository(kwargs: dict[str, object]) -> None:
    repository = Repository()
    with pytest.raises(ValueError):
        await _service(repository).list_catalog_records(**kwargs)
    assert not repository.calls


async def test_operator_http_contract_no_store_and_sanitized_failures() -> None:
    repository = Repository()
    service = cast(FakeIngestionAdmin, _service(repository))
    async with _client(_app(service)) as client:
        response = await client.get(
            _PATH,
            params={
                "source_key": "city-events",
                "run_key": "manual:one",
                "q": "music",
                "date_scope": "upcoming",
                "price_status": "unknown",
                "limit": 2,
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "generated_at": "2026-09-08T00:00:00Z",
            "items": [],
            "total": 0,
            "limit": 2,
            "has_more": False,
            "next_start_at": None,
            "next_canonical_event_id": None,
            "query": "music",
            "source_key": "city-events",
            "run_key": "manual:one",
            "date_scope": "upcoming",
            "price_status": "unknown",
            "upcoming_total": 0,
            "source_counts": [],
            "source_count": 0,
            "source_counts_truncated": False,
        }
        assert response.headers["cache-control"] == "no-store, max-age=0"
        for params in (
            {"run_key": "manual:one"},
            {"source_key": "x"},
            {"date_scope": "bad"},
            {"price_status": "bad"},
            {"after_start_at": "2026-01-01T00:00:00Z"},
        ):
            assert (await client.get(_PATH, params=params)).status_code == 422
        assert (await client.post(_PATH, json={})).status_code == 405
        assert len(repository.calls) == 1
        repository.failure = True
        failed = await client.get(_PATH)
        assert failed.status_code == 503 and "secret" not in failed.text
        assert failed.headers["cache-control"] == "no-store, max-age=0"
    count = len(repository.calls)
    async with _client(_app(service, mock_cloud=False)) as client:
        assert (await client.get(_PATH)).status_code == 403
    assert len(repository.calls) == count
