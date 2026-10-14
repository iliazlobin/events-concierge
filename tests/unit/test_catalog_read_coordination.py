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


async def test_cache_freshness_includes_load_time():
    now = [0.0]
    coordinator = CatalogReadCoordinator(
        2, token=AsyncMock(return_value="a"), clock=lambda: now[0]
    )
    calls = 0

    async def load(send):
        nonlocal calls
        calls += 1
        now[0] += 9.0
        await _response(send)

    await _read(coordinator, b"filters", load)
    now[0] = 9.9
    await _read(coordinator, b"filters", load)
    assert calls == 1
    now[0] = 10.0
    await _read(coordinator, b"filters", load)
    assert calls == 2


async def test_load_that_consumes_ttl_is_not_cached():
    now = [0.0]
    coordinator = CatalogReadCoordinator(
        2, token=AsyncMock(return_value="a"), clock=lambda: now[0]
    )

    async def load(send):
        now[0] += 12.0
        await _response(send)

    result = await _read(coordinator, b"filters", load)
    assert result[0]["status"] == 200
    assert coordinator._cache == {} and coordinator._bytes == 0


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


def _middleware_request(coordinator, app, *, path="/v1/catalog/events", receive=None):
    state = SimpleNamespace(
        catalog_reads=coordinator,
        settings=SimpleNamespace(catalog_name_suggestions_enabled=True),
    )
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    messages = []

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": "GET", "path": path, "query_string": b""}
    request = asyncio.create_task(
        CatalogReadMiddleware(app, state=state)(scope, receive or incoming.get, send)
    )
    return request, incoming, messages


async def _until(predicate):
    async with asyncio.timeout(1):
        while not predicate():  # noqa: ASYNC110 - the observed queue state has no event.
            await asyncio.sleep(0)


async def test_socket_disconnect_cancels_load_and_releases_capacity_without_caching():
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))
    started, cleaned_up = asyncio.Event(), asyncio.Event()

    async def blocked(scope, receive, send):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    request, incoming, messages = _middleware_request(coordinator, blocked)
    await started.wait()
    incoming.put_nowait({"type": "http.disconnect"})
    await asyncio.wait_for(request, 1)
    assert cleaned_up.is_set()
    assert not messages and not coordinator._cache
    assert coordinator._waiting == 0 and coordinator._keys == {}
    recovered = await _read(coordinator, b"filters", _response)
    assert recovered[0]["status"] == 200


async def test_disconnected_waiter_does_not_cancel_the_reader_it_was_waiting_for():
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def app(scope, receive, send):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        await _response(send)

    first, _, first_messages = _middleware_request(coordinator, app)
    await started.wait()
    second, incoming, second_messages = _middleware_request(coordinator, app)
    await _until(lambda: coordinator._waiting == 2)
    incoming.put_nowait({"type": "http.disconnect"})
    await asyncio.wait_for(second, 1)
    assert not first.done() and not second_messages
    release.set()
    await asyncio.wait_for(first, 1)
    assert calls == 1 and first_messages[0]["status"] == 200
    assert coordinator._waiting == 0 and coordinator._keys == {}


async def test_waiting_reader_recovers_when_the_loading_request_disconnects():
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))
    started = asyncio.Event()
    calls = 0

    async def app(scope, receive, send):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        await _response(send)

    first, incoming, first_messages = _middleware_request(coordinator, app)
    await started.wait()
    second, _, second_messages = _middleware_request(coordinator, app)
    await _until(lambda: coordinator._waiting == 2)
    incoming.put_nowait({"type": "http.disconnect"})
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert not first_messages and second_messages[0]["status"] == 200
    assert calls == 2 and coordinator._waiting == 0 and coordinator._keys == {}


async def test_disconnect_monitor_forwards_request_body_chunks_to_the_handler():
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))
    incoming = asyncio.Queue()
    for body, more_body in [(b"part one ", True), (b"part two", False)]:
        incoming.put_nowait({"type": "http.request", "body": body, "more_body": more_body})

    async def app(scope, receive, send):
        body = bytearray()
        while True:
            message = await receive()
            assert message["type"] == "http.request"
            body.extend(message["body"])
            if not message["more_body"]:
                break
        await _response(send, value=body.decode())

    request, _, messages = _middleware_request(
        coordinator, app, path="/v1/catalog/sources", receive=incoming.get
    )
    await asyncio.wait_for(request, 1)
    assert messages[1]["body"] == b'{"value":"part one part two"}'


async def test_unconsumed_get_body_has_a_bounded_buffer_and_cancels_the_handler(monkeypatch):
    monkeypatch.setattr(module, "_MAX_REQUEST_BODY_BYTES", 8)
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))
    started, cleaned_up = asyncio.Event(), asyncio.Event()
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": b"", "more_body": True})

    async def app(scope, receive, send):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    request, _, messages = _middleware_request(coordinator, app, receive=incoming.get)
    await started.wait()
    incoming.put_nowait({"type": "http.request", "body": b"12345678", "more_body": True})
    incoming.put_nowait({"type": "http.request", "body": b"9", "more_body": False})
    await asyncio.wait_for(request, 1)
    assert messages[0]["status"] == 413 and cleaned_up.is_set()
    assert not coordinator._cache and coordinator._waiting == 0 and coordinator._keys == {}


@pytest.mark.parametrize("cancel_request", [False, True])
async def test_completed_or_cancelled_request_drains_its_disconnect_monitor(cancel_request):
    coordinator = CatalogReadCoordinator(2, token=AsyncMock(return_value="a"))
    receive_started, receive_cleaned_up = asyncio.Event(), asyncio.Event()

    async def receive():
        receive_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            receive_cleaned_up.set()

    async def app(scope, receive, send):
        await receive_started.wait()
        if cancel_request:
            await asyncio.Event().wait()
        await _response(send)

    request, _, messages = _middleware_request(coordinator, app, receive=receive)
    await receive_started.wait()
    if cancel_request:
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    else:
        await asyncio.wait_for(request, 1)
        assert messages[0]["status"] == 200
    assert receive_cleaned_up.is_set()
    assert coordinator._waiting == 0 and coordinator._keys == {}
