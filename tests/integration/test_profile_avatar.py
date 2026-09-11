"""Durable avatar replacement/removal serialize across replicas without starving small pools."""

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.unit.test_avatar_api import (
    _avatar_app,
    _AvatarRepository,
    _MediaStore,
    _png,
    _TenantEffectAuthority,
)

from events_concierge.adapters.local_media import LocalFilesystemMediaStore
from events_concierge.adapters.postgres.profile_avatar import PostgresProfileAvatarRepository
from events_concierge.adapters.postgres.profile_media import PostgresProfileMediaMutationGuard
from events_concierge.adapters.postgres.tenant_effects import PostgresTenantEffectAuthority
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.application.profile_media import normalize_avatar
from events_concierge.domain.credentials import Tenant
from events_concierge.infra.db import dispose_engine, get_engine, init_engine
from events_concierge.ports.media_store import MediaNotFoundError
from events_concierge.ports.profile_avatar import ProfileAvatar, ProfileMediaMutationBusyError
from events_concierge.ports.tenant_effects import TenantEffectTimedOutError

pytestmark = pytest.mark.integration


async def test_avatar_compare_delete_preserves_replacements_and_other_tenants(db: None) -> None:
    owner, other = uuid4(), uuid4()
    tenants = PostgresTenantRepository()
    await tenants.add(Tenant(owner, f"avatar-{owner}", "owner@example.test", "owner@u.test"))
    await tenants.add(Tenant(other, f"avatar-{other}", "other@example.test", "other@u.test"))
    avatars = PostgresProfileAvatarRepository()
    avatar = ProfileAvatar("a" * 64 + ".webp", "image/webp", 100, 256, 256, "a" * 64)
    first = await avatars.replace(owner, avatar)
    other_record = await avatars.replace(other, avatar)
    assert await avatars.delete(owner, expected=other_record) is None
    # Re-uploading identical bytes still creates a new index version; an old DELETE must preserve it.
    second = await avatars.replace(owner, avatar)
    assert first.created_at != second.created_at
    assert await avatars.delete(owner, expected=first) is None
    assert await avatars.get(owner) == second
    third = await avatars.replace(
        owner, replace(avatar, storage_key="b" * 64 + ".webp", checksum_sha256="b" * 64)
    )
    assert await avatars.delete(owner, expected=second) is None
    assert await avatars.get(owner) == third
    assert await avatars.delete(owner, expected=third) == third
    assert await avatars.get(owner) is None
    assert await avatars.get(other) == other_record
    assert await avatars.delete(other) == other_record


class _PausedAvatars(PostgresProfileAvatarRepository):
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def replace(self, tenant_id, avatar):
        if self.operation == "upload":
            self.started.set()
            await self.release.wait()
        return await super().replace(tenant_id, avatar)

    async def delete(self, tenant_id, *, expected=None):
        if self.operation == "delete":
            self.started.set()
            await self.release.wait()
        return await super().delete(tenant_id, expected=expected)


def _real_avatar_app(monkeypatch, tenant, guard, avatars, media):
    events = []
    authority = _TenantEffectAuthority(events)
    app = _avatar_app(
        monkeypatch,
        tenant,
        authority,
        _MediaStore(authority, events),
        _AvatarRepository(authority, events),
    )
    app.state.container.profile_media_mutations = guard
    app.state.container.profile_avatars = avatars
    app.state.container.media_store = media
    app.state.container.tenant_effect_authority = PostgresTenantEffectAuthority()
    return app


async def _add_avatar_tenant():
    tenant = uuid4()
    await PostgresTenantRepository().add(
        Tenant(tenant, f"avatar-{tenant}", f"{tenant}@example.test", f"{tenant}@u.test")
    )
    return tenant


@pytest.mark.parametrize("first_operation", ["upload", "delete"])
async def test_same_key_upload_delete_across_replica_pools_has_no_dangling_avatar(
    db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_operation: str,
) -> None:
    tenant = await _add_avatar_tenant()
    url = os.environ["EC_DATABASE_URL"]
    engines = [
        create_async_engine(url, pool_size=2, max_overflow=0, pool_timeout=0.5) for _ in range(2)
    ]
    guards = [
        PostgresProfileMediaMutationGuard(
            pool_capacity=2, lock_timeout_seconds=2, mutation_timeout_seconds=5, engine=engine
        )
        for engine in engines
    ]
    media = LocalFilesystemMediaStore(tmp_path)
    normalized = normalize_avatar(_png(), "image/png")
    avatar = ProfileAvatar(
        normalized.storage_key,
        normalized.content_type,
        normalized.byte_size,
        normalized.width_px,
        normalized.height_px,
        normalized.checksum_sha256,
    )
    repository = PostgresProfileAvatarRepository()
    await repository.replace(tenant, avatar)
    await media.put(tenant, normalized.storage_key, normalized.data, normalized.content_type)
    paused = _PausedAvatars(first_operation)
    apps = [
        _real_avatar_app(monkeypatch, tenant, guards[0], paused, media),
        _real_avatar_app(monkeypatch, tenant, guards[1], repository, media),
    ]
    try:
        async with (
            AsyncClient(transport=ASGITransport(app=apps[0]), base_url="http://test") as a,
            AsyncClient(transport=ASGITransport(app=apps[1]), base_url="http://test") as b,
        ):
            first = asyncio.create_task(
                a.request(
                    "post" if first_operation == "upload" else "delete",
                    "/v1/me/avatar",
                    content=_png(),
                    headers={"Content-Type": "image/png"},
                )
            )
            await asyncio.wait_for(paused.started.wait(), timeout=2)
            second = asyncio.create_task(
                b.request(
                    "delete" if first_operation == "upload" else "post",
                    "/v1/me/avatar",
                    content=_png(),
                    headers={"Content-Type": "image/png"},
                )
            )

            # Prove the second independent pool reached PostgreSQL and is waiting on the media lock.
            async def wait_for_lock():
                while True:
                    async with get_engine().connect() as c:
                        waiting = await c.scalar(
                            text(
                                "SELECT EXISTS (SELECT FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory')"
                            )
                        )
                    if waiting:
                        return
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_lock(), timeout=2)
            assert not second.done()
            paused.release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
            assert [r.status_code for r in responses] == (
                [200, 204] if first_operation == "upload" else [204, 200]
            )
        if first_operation == "upload":
            assert await repository.get(tenant) is None
            with pytest.raises(MediaNotFoundError):
                await media.get(tenant, normalized.storage_key)
        else:
            current = await repository.get(tenant)
            assert current is not None
            assert await media.get(tenant, current.storage_key) == normalized.data
    finally:
        paused.release.set()
        for engine in engines:
            await engine.dispose()


async def test_eight_tenants_and_guard_instances_share_pool_two_without_starvation(
    db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await dispose_engine()
    engine = init_engine(
        os.environ["EC_DATABASE_URL"], pool_size=2, max_overflow=0, pool_timeout_seconds=0.25
    )
    tenants = [await _add_avatar_tenant() for _ in range(8)]
    media, avatars = LocalFilesystemMediaStore(tmp_path), PostgresProfileAvatarRepository()

    async def upload(tenant):
        guard = PostgresProfileMediaMutationGuard(
            pool_capacity=2, lock_timeout_seconds=2, mutation_timeout_seconds=5
        )
        app = _real_avatar_app(monkeypatch, tenant, guard, avatars, media)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                "/v1/me/avatar", content=_png(), headers={"Content-Type": "image/png"}
            )

    responses = await asyncio.wait_for(
        asyncio.gather(*(upload(tenant) for tenant in tenants)), timeout=5
    )
    assert [response.status_code for response in responses] == [200] * 8
    for tenant in tenants:
        current = await avatars.get(tenant)
        assert current is not None
        assert await media.get(tenant, current.storage_key)
    assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_media_lock_is_bounded_and_drained_before_cancellation_or_timeout_unlocks(
    db: None,
    stop: str,
) -> None:
    tenant = await _add_avatar_tenant()
    engines = [
        create_async_engine(
            os.environ["EC_DATABASE_URL"], pool_size=2, max_overflow=0, pool_timeout=0.25
        )
        for _ in range(2)
    ]
    first, second = [
        PostgresProfileMediaMutationGuard(
            pool_capacity=2, lock_timeout_seconds=0.1, mutation_timeout_seconds=0.1, engine=e
        )
        for e in engines
    ]
    started, release = asyncio.Event(), asyncio.Event()

    async def mutation():
        started.set()
        await release.wait()
        return "settled"

    async def no_effect():
        return "acquired"

    task = asyncio.create_task(first.run(tenant, mutation))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        if stop == "cancel":
            task.cancel()
        else:
            await asyncio.sleep(0.15)
        with pytest.raises(ProfileMediaMutationBusyError, match="lock deadline"):
            await asyncio.wait_for(second.run(tenant, no_effect), timeout=1)
        assert not task.done()
        release.set()
        with pytest.raises(
            asyncio.CancelledError if stop == "cancel" else TenantEffectTimedOutError
        ):
            await asyncio.wait_for(task, timeout=1)
        assert await second.run(tenant, no_effect) == "acquired"
    finally:
        release.set()
        if not task.done():
            await task
        for engine in engines:
            await engine.dispose()
