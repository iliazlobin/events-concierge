"""Bound expensive public catalog reads and reuse identical, publication-fenced responses.

Only public JSON discovery reads enter the cache. Account/session responses never do.
The database fence is read before every hit, so source revocation and publication invalidate
immediately. Ten seconds bounds other changing event facts and relative-time admission.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from time import monotonic
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..infra.db import system_session_scope

_CACHED_PATHS = frozenset(
    {
        "/v1/catalog/events",
        "/v1/catalog/events/summary",
        "/v1/catalog/name-suggestions",
        "/v1/catalog/name-index",
    }
)
_TTL_SECONDS = 10.0
_MAX_ENTRIES = 32
_MAX_BYTES = 8 * 1024 * 1024
_MAX_ENTRY_BYTES = 1024 * 1024
_MAX_WAITERS = 32
_WAIT_SECONDS = 5.0
_MAX_KEY_BYTES = 16_384
_MAX_REQUEST_BODY_BYTES = 64 * 1024


class CatalogNameIndexCompression:
    """Compress the public preload outside the uncompressed response cache."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=2048, compresslevel=5)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        target = self.compressed if scope.get("path") == "/v1/catalog/name-index" else self.app
        await target(scope, receive, send)


async def catalog_public_read_token() -> str:
    async with system_session_scope() as session:
        return str(
            (
                await session.execute(text("SELECT public.fn_catalog_public_read_token_v1()"))
            ).scalar_one()
        )


@dataclass(frozen=True)
class _CachedResponse:
    token: str
    at: float
    messages: tuple[Message, ...]
    size: int


class _CatalogBusyError(Exception):
    pass


class _CatalogBodyTooLargeError(Exception):
    pass


class _CatalogReceive:
    """Keep reading disconnects without losing or unboundedly queuing request bodies."""

    def __init__(self) -> None:
        self._body = bytearray()
        self._body_bytes = 0
        self._more_body = True
        self._disconnected = False
        self._ready = asyncio.Event()

    async def watch(self, receive: Receive) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self._disconnected = True
                self._ready.set()
                return
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                self._body_bytes += len(chunk)
                if self._body_bytes > _MAX_REQUEST_BODY_BYTES:
                    raise _CatalogBodyTooLargeError
                self._body.extend(chunk)
                self._more_body = message.get("more_body", False)
                self._ready.set()

    async def __call__(self) -> Message:
        await self._ready.wait()
        if self._disconnected:
            return {"type": "http.disconnect"}
        message: Message = {
            "type": "http.request",
            "body": bytes(self._body),
            "more_body": self._more_body,
        }
        self._body.clear()
        self._ready.clear()
        return message


class CatalogReadCoordinator:
    def __init__(
        self,
        pool_capacity: int,
        *,
        token: Callable[[], Awaitable[str]] = catalog_public_read_token,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        # A page's two ancillary reads are sequential. Leave one connection for
        # authenticated settings/session reads when the process has more than one.
        self._slots = asyncio.Semaphore(max(1, pool_capacity - 1))
        self._token = token
        self._clock = clock
        self._cache: OrderedDict[bytes, _CachedResponse] = OrderedDict()
        self._bytes = 0
        self._waiting = 0
        self._keys: dict[bytes, tuple[asyncio.Lock, int]] = {}

    async def serve(
        self,
        key: bytes | None,
        load: Callable[[Send], Awaitable[None]],
        send: Send,
    ) -> None:
        if self._waiting >= _MAX_WAITERS:
            await self._busy(send)
            return
        self._waiting += 1
        try:
            async with self._slot(key):
                if key is None:
                    await load(send)
                else:
                    await self._serve_cached(key, load, send)
        except (_CatalogBusyError, SqlAlchemyTimeoutError):
            # Competing work gets a bounded retry response, never an unhandled
            # pool-timeout 500 or a cached failure.
            await self._busy(send)
        finally:
            self._waiting -= 1

    @asynccontextmanager
    async def _slot(self, key: bytes | None) -> AsyncIterator[None]:
        acquired = False
        locked = False
        key_lock: asyncio.Lock | None = None
        if key is not None:
            key_lock, users = self._keys.get(key, (asyncio.Lock(), 0))
            self._keys[key] = key_lock, users + 1
        try:
            try:
                async with asyncio.timeout(_WAIT_SECONDS):
                    if key_lock is not None:
                        await key_lock.acquire()
                        locked = True
                    await self._slots.acquire()
                acquired = True
            except TimeoutError:
                raise _CatalogBusyError from None
            yield
        finally:
            if acquired:
                self._slots.release()
            if key is not None and key_lock is not None:
                if locked:
                    key_lock.release()
                _, users = self._keys[key]
                if users == 1:
                    del self._keys[key]
                else:
                    self._keys[key] = key_lock, users - 1

    async def _serve_cached(
        self,
        key: bytes,
        load: Callable[[Send], Awaitable[None]],
        send: Send,
    ) -> None:
        token = await self._token()
        # A visible browser refreshes its ten-second preload before expiry. Let
        # that read renew the index rather than replaying an almost-expired copy.
        ttl = 8.0 if key.startswith(b"/v1/catalog/name-index?") else _TTL_SECONDS
        cached = self._cache.get(key)
        if cached is not None:
            if cached.token == token and self._clock() - cached.at < ttl:
                self._cache.move_to_end(key)
                await self._replay(cached.messages, send, "hit")
                return
            self._remove(key)
        messages: list[Message] = []

        async def capture(message: Message) -> None:
            messages.append(message)

        read_started = self._clock()
        await load(capture)
        snapshot = tuple(messages)
        size = sum(len(message.get("body", b"")) for message in snapshot)
        start = snapshot[0] if snapshot else {}
        headers = {name.lower(): value for name, value in start.get("headers", [])}
        if (
            start.get("status") == HTTPStatus.OK
            and size <= _MAX_ENTRY_BYTES
            and b"application/json" in headers.get(b"content-type", b"")
            and b"set-cookie" not in headers
            and await self._token() == token
            and self._clock() - read_started < ttl
        ):
            self._cache[key] = _CachedResponse(token, read_started, snapshot, size)
            self._bytes += size
            while len(self._cache) > _MAX_ENTRIES or self._bytes > _MAX_BYTES:
                self._remove(next(iter(self._cache)))
        await self._replay(snapshot, send, "miss")

    def _remove(self, key: bytes) -> None:
        self._bytes -= self._cache.pop(key).size

    @staticmethod
    async def _replay(messages: tuple[Message, ...], send: Send, cache: str) -> None:
        for message in messages:
            outgoing = message
            if message["type"] == "http.response.start":
                outgoing = {
                    **message,
                    "headers": [
                        (name, value)
                        for name, value in message.get("headers", [])
                        if name.lower() not in {b"cache-control", b"x-ec-catalog-cache"}
                    ]
                    + [(b"cache-control", b"no-store"), (b"x-ec-catalog-cache", cache.encode())],
                }
            await send(outgoing)

    @staticmethod
    async def _busy(send: Send) -> None:
        response = JSONResponse(
            {"detail": "Catalog is busy. Please try again shortly."},
            status_code=503,
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )
        await send({"type": "http.response.start", "status": 503, "headers": response.raw_headers})
        await send({"type": "http.response.body", "body": response.body})


class CatalogReadMiddleware:
    def __init__(self, app: ASGIApp, *, state: Any) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        coordinator: CatalogReadCoordinator | None = getattr(self.state, "catalog_reads", None)
        path = scope.get("path", "")
        if (
            coordinator is None
            or scope["type"] != "http"
            or scope.get("method") != "GET"
            or not path.startswith("/v1/catalog/")
            or (
                path in {"/v1/catalog/name-suggestions", "/v1/catalog/name-index"}
                and not self.state.settings.catalog_name_suggestions_enabled
            )
        ):
            await self.app(scope, receive, send)
            return
        query = scope.get("query_string", b"")
        key = (
            path.encode() + b"?" + query
            if path in _CACHED_PATHS and len(query) < _MAX_KEY_BYTES
            else None
        )

        relay = _CatalogReceive()
        response_started = False

        async def load(target: Send) -> None:
            await self.app(scope, relay, target)

        async def send_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        request = asyncio.create_task(coordinator.serve(key, load, send_response))
        disconnected = asyncio.create_task(relay.watch(receive))
        body_too_large = False
        try:
            done, _ = await asyncio.wait(
                {request, disconnected}, return_when=asyncio.FIRST_COMPLETED
            )
            if request in done:
                await request
            else:
                # A socket disconnect does not cancel Uvicorn's handler. Cancel
                # this requester, including its queued key lock/slot/SQL session.
                await disconnected
        except _CatalogBodyTooLargeError:
            body_too_large = True
        finally:
            for task in (request, disconnected):
                task.cancel()
            await asyncio.gather(request, disconnected, return_exceptions=True)
        if body_too_large and not response_started:
            response = JSONResponse(
                {"detail": "request body too large"},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
