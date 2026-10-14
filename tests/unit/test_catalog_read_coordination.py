"""Public read reuse, revocation fences, bounded contention and account exclusions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from starlette.responses import JSONResponse

from events_concierge.api import catalog_reads as module
from events_concierge.api.catalog_reads import CatalogReadCoordinator, CatalogReadMiddleware


async def _response(send, *, status=200, headers=None, value="public"):
    response = JSONResponse({"value": value}, status_code=status, headers=headers)
    await send({"type": "http.response.start", "status": status, "headers": response.raw_headers})
    await send({"type": "http.response.body", "body": response.body})


async def _read(coordinator, key, load):
    messages = []

    async def send(message):
        messages.append(message)

    await coordinator.serve(key, load, send)
    return messages


async def test_concurrent_identical_reads_share_one_scan_even_with_multiple_slots():
    token = AsyncMock(return_value="revision-a")
    coordinator = CatalogReadCoordinator(5, token=token)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load(send):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        await _response(send)

    requests = [asyncio.create_task(_read(coordinator, b"same-filters", load)) for _ in range(8)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*requests)
    assert calls == 1
    assert all(result[0]["status"] == 200 for result in results)
    assert (
        sum(dict(result[0]["headers"])[b"x-ec-catalog-cache"] == b"hit" for result in results) == 7
    )
    assert coordinator._waiting == 0 and coordinator._keys == {}


async def test_publication_revocation_and_ttl_invalidate_before_reuse():
    token = AsyncMock(return_value="admitted-a")
    now = [0.0]
    coordinator = CatalogReadCoordinator(2, token=token, clock=lambda: now[0])
    load = AsyncMock(side_effect=_response)
    await _read(coordinator, b"source-a", load)
    await _read(coordinator, b"source-a", load)
    assert load.await_count == 1
    token.return_value = "source-disabled"
    await _read(coordinator, b"source-a", load)
    assert load.await_count == 2
    now[0] = 10.0
    await _read(coordinator, b"source-a", load)
    assert load.await_count == 3
    await _read(coordinator, b"different-city-price-date", load)
    assert load.await_count == 4


async def test_refresh_during_load_does_not_publish_a_stale_cache_entry():
    token = AsyncMock(side_effect=["a", "b", "b", "b"])
    coordinator = CatalogReadCoordinator(2, token=token)
    load = AsyncMock(side_effect=_response)
    await _read(coordinator, b"filters", load)
    assert coordinator._cache == {}
    await _read(coordinator, b"filters", load)
    assert load.await_count == 2


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (422, None),
        (500, None),
        (200, {"Set-Cookie": "session=private"}),
    ],
)
async def test_errors_and_cookie_responses_are_never_cached(status, headers):
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))

    async def load(send):
        await _response(send, status=status, headers=headers)

    await _read(coordinator, b"filters", load)
    assert coordinator._cache == {}


async def test_byte_budget_and_entry_budget_bound_retained_responses(monkeypatch):
    monkeypatch.setattr(module, "_MAX_BYTES", 40)
    monkeypatch.setattr(module, "_MAX_ENTRIES", 2)
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))
    for key in (b"a", b"b", b"c"):
        await _read(coordinator, key, _response)
    assert len(coordinator._cache) <= 2 and coordinator._bytes <= 40
    monkeypatch.setattr(module, "_MAX_ENTRY_BYTES", 10)
    await _read(coordinator, b"large", _response)
    assert b"large" not in coordinator._cache


async def test_cancelled_reader_releases_capacity_and_failed_load_can_retry():
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))
    started = asyncio.Event()

    async def blocked(send):
        started.set()
        await asyncio.Event().wait()

    request = asyncio.create_task(_read(coordinator, b"filters", blocked))
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    result = await _read(coordinator, b"filters", _response)
    assert result[0]["status"] == 200
    assert coordinator._waiting == 0 and coordinator._keys == {}


async def test_contention_gets_retryable_503_and_does_not_acquire_a_database_connection(
    monkeypatch,
):
    monkeypatch.setattr(module, "_WAIT_SECONDS", 0.02)
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))
    started = asyncio.Event()

    async def blocked(send):
        started.set()
        await asyncio.Event().wait()

    first = asyncio.create_task(_read(coordinator, b"a", blocked))
    await started.wait()
    result = await _read(coordinator, b"b", _response)
    assert result[0]["status"] == 503
    assert dict(result[0]["headers"])[b"retry-after"] == b"2"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert coordinator._waiting == 0 and coordinator._keys == {}


async def test_pool_timeout_becomes_uncached_503_then_recovers():
    token = AsyncMock(side_effect=[SqlAlchemyTimeoutError("pool busy"), "a", "a"])
    coordinator = CatalogReadCoordinator(2, token=token)
    result = await _read(coordinator, b"a", _response)
    assert result[0]["status"] == 503 and coordinator._cache == {}
    recovered = await _read(coordinator, b"a", _response)
    assert recovered[0]["status"] == 200


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/v1/me", "GET"),
        ("/v1/me/saved-filters", "GET"),
        ("/auth/logout", "POST"),
        ("/v1/catalog/entities/a/refresh", "POST"),
    ],
)
async def test_account_session_and_mutation_routes_bypass_shared_catalog_cache(path, method):
    token = AsyncMock(return_value="a")
    coordinator = CatalogReadCoordinator(2, token=token)
    state = SimpleNamespace(
        catalog_reads=coordinator,
        settings=SimpleNamespace(
            catalog_name_suggestions_enabled=True,
        ),
    )
    calls = 0

    async def app(scope, receive, send):
        nonlocal calls
        calls += 1
        await _response(send, value="account" + str(calls))

    middleware = CatalogReadMiddleware(app, state=state)
    messages = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": method, "path": path, "query_string": b""}
    await middleware(scope, receive, send)
    await middleware(scope, receive, send)
    assert calls == 2
    token.assert_not_called()
    assert coordinator._cache == {}
