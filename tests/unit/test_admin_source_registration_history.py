"""A bounded global registration curve does not invent historical source health."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from tests.unit.test_admin_api import FakeIngestionAdmin, _app, _client

from events_concierge.application.ingestion_admin import IngestionAdminService
from events_concierge.domain.ingestion_admin import (
    IngestionSourceRegistrationBucket,
    IngestionSourceRegistrationHistory,
)
from events_concierge.ports.ingestion_admin import IngestionAdminRepository

_NOW = datetime(2026, 9, 8, tzinfo=UTC)
_PATH = "/admin/v1/ingestion/source-registration-history"


def _history(days: int, fixtures: bool) -> IngestionSourceRegistrationHistory:
    start = _NOW - timedelta(days=days)
    return IngestionSourceRegistrationHistory(
        generated_at=_NOW,
        window_start=start,
        window_days=days,
        bucket_hours=24,
        baseline_sources=3,
        total_sources=3,
        added_sources=0,
        items=tuple(
            IngestionSourceRegistrationBucket(
                bucket_start=start + timedelta(days=i),
                bucket_end=start + timedelta(days=i + 1),
                registered_sources=3,
                added_sources=0,
            )
            for i in range(days)
        ),
        include_fixtures=fixtures,
    )


class Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []
        self.fail = False
        self.inconsistent = False

    async def source_registration_history(
        self, *, window_days: int, include_fixtures: bool
    ) -> IngestionSourceRegistrationHistory:
        self.calls.append((window_days, include_fixtures))
        if self.fail:
            raise RuntimeError("private registry connection credentials")
        result = _history(window_days, include_fixtures)
        return replace(result, total_sources=99) if self.inconsistent else result


def _service(repository: Repository) -> IngestionAdminService:
    return IngestionAdminService(cast(IngestionAdminRepository, repository))


async def test_default_and_allowed_registration_windows_use_only_global_scope() -> None:
    repository = Repository()
    service = _service(repository)
    await service.source_registration_history()
    for days in (7, 30, 90):
        result = await service.source_registration_history(window_days=days, include_fixtures=True)
        assert len(result.items) == days and result.history_scope == "retained_registry"
    assert repository.calls == [(90, False), (7, True), (30, True), (90, True)]


@pytest.mark.parametrize(
    "options",
    [
        {"window_days": 1},
        {"window_days": 8},
        {"window_days": 91},
        {"window_days": 7.0},
        {"window_days": True},
        {"include_fixtures": 1},
    ],
)
async def test_invalid_windows_and_fixture_scopes_fail_before_read(
    options: dict[str, object],
) -> None:
    repository = Repository()
    with pytest.raises(ValueError):
        await _service(repository).source_registration_history(**options)
    assert not repository.calls


async def test_http_history_is_bounded_read_only_no_store_and_keeps_operator_auth() -> None:
    repository = Repository()
    service = cast(FakeIngestionAdmin, _service(repository))
    async with _client(_app(service)) as client:
        default = await client.get(_PATH)
        assert default.status_code == 200 and default.json()["window_days"] == 90
        response = await client.get(_PATH, params={"window_days": 7, "include_fixtures": "true"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, max-age=0"
        data = response.json()
        assert data["include_fixtures"] is True and data["history_scope"] == "retained_registry"
        assert data["baseline_sources"] == data["total_sources"] == 3
        assert data["added_sources"] == 0 and len(data["items"]) == 7
        assert data["items"][0]["bucket_start"] == data["window_start"]
        assert data["items"][-1]["bucket_end"] == data["generated_at"]
        assert set(data["items"][0]) == {
            "bucket_start",
            "bucket_end",
            "registered_sources",
            "added_sources",
        }
        for params in ({"window_days": 8}, {"window_days": 91}, {"include_fixtures": "maybe"}):
            assert (await client.get(_PATH, params=params)).status_code == 422
        assert (await client.post(_PATH, json={})).status_code == 405
        assert repository.calls == [(90, False), (7, True)]
    async with _client(_app(service, mock_cloud=False)) as client:
        assert (await client.get(_PATH)).status_code == 403
    assert len(repository.calls) == 2


@pytest.mark.parametrize("inconsistent", [False, True])
async def test_failed_or_inconsistent_registration_history_is_sanitized_503(
    inconsistent: bool,
) -> None:
    repository = Repository()
    repository.inconsistent = inconsistent
    repository.fail = not inconsistent
    service = cast(FakeIngestionAdmin, _service(repository))
    async with _client(_app(service)) as client:
        response = await client.get(_PATH)
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert not any(secret in response.text for secret in ("credentials", "registry", "99"))
