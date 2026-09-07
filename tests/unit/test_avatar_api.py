"""Account-erasure ordering at the profile-avatar object-store boundary."""

from __future__ import annotations

import asyncio
import importlib
import io
from collections.abc import Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.exc import DBAPIError

from events_concierge.config import Settings
from events_concierge.ports.profile_avatar import ProfileAvatar
from events_concierge.ports.tenant_effects import (
    TenantEffectFencedError,
    TenantEffectKind,
    TenantEffectRequest,
)

app_module = importlib.import_module("events_concierge.api.app")


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), (30, 120, 210)).save(buffer, "PNG")
    return buffer.getvalue()


class _AuthContext:
    def __init__(self, tenant_id: UUID) -> None:
        self._tenant_id = tenant_id

    async def resolve_tenant_id(self, headers: Mapping[str, str]) -> UUID:
        del headers
        return self._tenant_id


class _CsrfProtection:
    async def verify_state_change(
        self,
        tenant_id: UUID,
        headers: Mapping[str, str],
    ) -> None:
        del tenant_id, headers


class _TenantRepository:
    async def get(self, tenant_id: UUID) -> object:
        del tenant_id
        return object()


class _NoErasureRepository:
    async def get(self, tenant_id: UUID) -> None:
        del tenant_id


class _TenantEffectAuthority:
    """Small deterministic model of the production tenant advisory-lock boundary."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests: list[TenantEffectRequest] = []
        self.active = False
        self.fenced = False
        self._lock = asyncio.Lock()
        self.erasure_attempted = asyncio.Event()

    async def run(
        self,
        request: TenantEffectRequest,
        effect: Callable[[], Awaitable[Any]],
    ) -> Any:
        self.requests.append(request)
        async with self._lock:
            if self.fenced:
                self.events.append("authority_fenced")
                raise TenantEffectFencedError("tenant account is fenced for erasure")
            self.active = True
            self.events.append("authority_enter")
            try:
                return await effect()
            finally:
                self.active = False
                self.events.append("authority_exit")

    async def begin_erasure(self) -> None:
        self.erasure_attempted.set()
        async with self._lock:
            self.fenced = True
            self.events.append("erasure_committed")


class _MediaStore:
    def __init__(
        self,
        authority: _TenantEffectAuthority,
        events: list[str],
        *,
        release_put: asyncio.Event | None = None,
    ) -> None:
        self._authority = authority
        self._events = events
        self._release_put = release_put
        self.put_started = asyncio.Event()
        self.put_calls: list[tuple[UUID, str, bytes, str]] = []

    async def put(self, tenant_id: UUID, key: str, data: bytes, content_type: str) -> None:
        assert self._authority.active, "media writes must run under tenant effect authority"
        self.put_calls.append((tenant_id, key, data, content_type))
        self._events.append("media_put_started")
        self.put_started.set()
        if self._release_put is not None:
            await self._release_put.wait()
        self._events.append("media_put_finished")


class _AvatarRepository:
    def __init__(
        self,
        authority: _TenantEffectAuthority,
        events: list[str],
        *,
        replace_error: DBAPIError | None = None,
    ) -> None:
        self._authority = authority
        self._events = events
        self._replace_error = replace_error
        self.current: ProfileAvatar | None = None
        self.replace_calls = 0

    async def get(self, tenant_id: UUID) -> ProfileAvatar | None:
        del tenant_id
        self._events.append("avatar_read")
        return self.current

    async def replace(self, tenant_id: UUID, avatar: ProfileAvatar) -> ProfileAvatar:
        del tenant_id
        assert not self._authority.active, (
            "the trigger-protected database upsert must stay outside external-effect authority"
        )
        self.replace_calls += 1
        if self._replace_error is not None:
            raise self._replace_error
        self.current = avatar
        self._events.append("avatar_replaced")
        return avatar


class _DriverDatabaseError(Exception):
    def __init__(self, sqlstate: str, primary_message: str) -> None:
        super().__init__(primary_message)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(message_primary=primary_message)


def _database_error(sqlstate: str, primary_message: str) -> DBAPIError:
    return DBAPIError(
        "fixture statement",
        {},
        _DriverDatabaseError(sqlstate, primary_message),
    )


def _avatar_app(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: UUID,
    authority: _TenantEffectAuthority,
    media_store: _MediaStore,
    avatars: _AvatarRepository,
    *,
    timeout_seconds: float = 1.25,
) -> Any:
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(tenant_effect_timeout_seconds=timeout_seconds),
    )
    app = app_module.create_app()
    app.state.container = SimpleNamespace(
        auth_context=_AuthContext(tenant_id),
        csrf_protection=_CsrfProtection(),
        tenant_repo=_TenantRepository(),
        account_erasure_repo=_NoErasureRepository(),
        tenant_effect_authority=authority,
        media_store=media_store,
        profile_avatars=avatars,
    )
    return app


async def test_normal_avatar_upload_guards_only_the_media_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    events: list[str] = []
    authority = _TenantEffectAuthority(events)
    media_store = _MediaStore(authority, events)
    avatars = _AvatarRepository(authority, events)
    app = _avatar_app(monkeypatch, tenant_id, authority, media_store, avatars)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/me/avatar",
            content=_png(),
            headers={"Content-Type": "image/png"},
        )

    assert response.status_code == 200
    assert response.json()["content_type"] == "image/webp"
    assert response.json()["width"] == response.json()["height"] == 256
    assert events == [
        "avatar_read",
        "authority_enter",
        "media_put_started",
        "media_put_finished",
        "authority_exit",
        "avatar_replaced",
    ]
    assert avatars.replace_calls == 1
    assert len(media_store.put_calls) == 1
    assert len(authority.requests) == 1
    effect_request = authority.requests[0]
    assert effect_request.tenant_id == tenant_id
    assert effect_request.kind is TenantEffectKind.PROFILE_MEDIA_WRITE
    assert effect_request.timeout_seconds == 1.25


async def test_an_already_started_avatar_put_delays_erasure_until_it_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    events: list[str] = []
    release_put = asyncio.Event()
    authority = _TenantEffectAuthority(events)
    media_store = _MediaStore(authority, events, release_put=release_put)
    avatars = _AvatarRepository(authority, events)
    app = _avatar_app(monkeypatch, tenant_id, authority, media_store, avatars)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        upload = asyncio.create_task(
            client.post(
                "/v1/me/avatar",
                content=_png(),
                headers={"Content-Type": "image/png"},
            )
        )
        await asyncio.wait_for(media_store.put_started.wait(), timeout=1.0)
        erasure = asyncio.create_task(authority.begin_erasure())
        await asyncio.wait_for(authority.erasure_attempted.wait(), timeout=1.0)
        await asyncio.sleep(0)

        assert not erasure.done()
        release_put.set()
        response = await asyncio.wait_for(upload, timeout=1.0)
        await asyncio.wait_for(erasure, timeout=1.0)

    assert response.status_code == 200
    assert events.index("media_put_finished") < events.index("erasure_committed")


async def test_a_raced_erasure_tombstone_prevents_the_avatar_put_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    events: list[str] = []
    authority = _TenantEffectAuthority(events)
    authority.fenced = True
    media_store = _MediaStore(authority, events)
    avatars = _AvatarRepository(authority, events)
    app = _avatar_app(monkeypatch, tenant_id, authority, media_store, avatars)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/me/avatar",
            content=_png(),
            headers={"Content-Type": "image/png"},
        )

    assert response.status_code == 423
    assert response.json() == {"detail": "account erasure in progress"}
    assert events == ["avatar_read", "authority_fenced"]
    assert media_store.put_calls == []
    assert avatars.replace_calls == 0
    assert authority.requests[0].kind is TenantEffectKind.PROFILE_MEDIA_WRITE


async def test_an_erasure_fence_racing_the_avatar_index_write_returns_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    events: list[str] = []
    authority = _TenantEffectAuthority(events)
    media_store = _MediaStore(authority, events)
    avatars = _AvatarRepository(
        authority,
        events,
        replace_error=_database_error("55000", "tenant account is fenced for erasure"),
    )
    app = _avatar_app(monkeypatch, tenant_id, authority, media_store, avatars)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/me/avatar",
            content=_png(),
            headers={"Content-Type": "image/png"},
        )

    assert response.status_code == 423
    assert response.json() == {"detail": "account erasure in progress"}
    assert len(media_store.put_calls) == 1
    assert avatars.replace_calls == 1
    assert "avatar_replaced" not in events


@pytest.mark.parametrize(
    ("sqlstate", "primary_message"),
    [
        ("55000", "some other database invariant failed"),
        ("23505", "tenant account is fenced for erasure"),
    ],
)
def test_unrelated_database_errors_are_not_classified_as_the_erasure_fence(
    sqlstate: str,
    primary_message: str,
) -> None:
    assert not app_module._is_account_erasure_write_fence(
        _database_error(sqlstate, primary_message)
    )
