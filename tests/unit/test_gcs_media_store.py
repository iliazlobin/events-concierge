"""Private media durability, tenant partitioning and truthful erasure contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from tests.unit.test_account_erasure import (
    _MemoryErasureRepository,
    _RecordingCalendar,
    _RecordingExternalEffects,
    _RecordingObjectStore,
    _RecordingSessions,
    _RecordingVault,
    _snapshot,
    _WorkflowCanceller,
)

from events_concierge.adapters.gcs import GcsBlob, GcsBucket, GcsStorageClient
from events_concierge.adapters.gcs.media_store import GcsMediaStore
from events_concierge.application.account_erasure import AccountErasureService
from events_concierge.domain.account_erasure import AccountErasureStage, AccountErasureStatus
from events_concierge.ports.media_store import MediaNotFoundError


class _ApiError(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"status-{code}")


@dataclass
class _Object:
    data: bytes
    metadata: dict[str, str]
    content_type: str = "image/webp"
    content_encoding: str | None = None
    cache_control: str = "private, no-store"
    size: int = 0
    held: bool = False


class _Blob:
    def __init__(self, client: _Client, name: str, generation: int | None = None) -> None:
        self.client, self.name, self.generation = client, name, generation
        self.metadata: dict[str, str] | None = None
        self.cache_control: str | None = None

    def reload(self) -> None:
        self.client.calls.append("object.get")
        if self.client.read_error:
            raise _ApiError(self.client.read_error)
        self.generation = self.client.live.get(self.name)
        if self.generation is None:
            raise _ApiError(404)
        obj = self.client.objects[(self.name, self.generation)]
        for key in ("metadata", "cache_control", "content_type", "content_encoding", "size"):
            setattr(self, key, getattr(obj, key))

    def upload_from_string(
        self,
        data: bytes,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None:
        self.client.calls.append("object.create")
        assert if_generation_match == 0
        if self.name in self.client.live:
            raise _ApiError(412)
        assert self.metadata and self.cache_control
        generation = self.client.next_generation
        self.client.next_generation += 1
        self.client.live[self.name] = generation
        self.client.objects[(self.name, generation)] = _Object(
            data,
            dict(self.metadata),
            content_type=content_type,
            cache_control=self.cache_control,
            size=len(data),
        )

    def download_as_bytes(
        self,
        *,
        start: int,
        end: int,
        raw_download: bool,
        if_generation_match: int,
    ) -> bytes:
        self.client.calls.append("object.download")
        assert (start, end, raw_download) == (0, 32 * 1024, True)
        if self.client.download_conflict or if_generation_match != self.generation:
            raise _ApiError(412)
        return self.client.objects[(self.name, if_generation_match)].data[start : end + 1]

    def delete(self, *, if_generation_match: int) -> None:
        self.client.calls.append("object.delete")
        identity = (self.name, if_generation_match)
        if identity not in self.client.objects:
            raise _ApiError(404)
        if self.client.delete_error:
            raise _ApiError(self.client.delete_error)
        if self.client.objects[identity].held:
            raise _ApiError(403)
        del self.client.objects[identity]
        if self.client.live.get(self.name) == if_generation_match:
            del self.client.live[self.name]
        if self.client.enable_soft_delete_after_delete:
            self.client.bound_bucket.soft_delete_policy.retention_duration_seconds = 604800


class _Bucket:
    def __init__(self, client: _Client) -> None:
        self.client = client
        self.soft_delete_policy = SimpleNamespace(retention_duration_seconds=0)
        self.retention_period: int | None = None
        self.default_event_based_hold = False
        self.versioning_enabled = False

    def blob(self, name: str) -> _Blob:
        return _Blob(self.client, name)

    def reload(self) -> None:
        self.client.calls.append("bucket.get")
        if self.client.policy_error:
            raise _ApiError(self.client.policy_error)


@dataclass
class _Client:
    calls: list[str] = field(default_factory=list)
    objects: dict[tuple[str, int], _Object] = field(default_factory=dict)
    live: dict[str, int] = field(default_factory=dict)
    next_generation: int = 1
    read_error: int | None = None
    delete_error: int | None = None
    policy_error: int | None = None
    download_conflict: bool = False
    enable_soft_delete_after_delete: bool = False

    def __post_init__(self) -> None:
        self.bound_bucket = _Bucket(self)

    def bucket(self, bucket_name: str) -> GcsBucket:
        assert bucket_name == "private-media"
        self.calls.append("bucket.bind")
        return cast("GcsBucket", self.bound_bucket)

    def list_blobs(
        self,
        bucket: GcsBucket,
        *,
        prefix: str,
        versions: bool,
        page_size: int,
    ) -> Iterable[GcsBlob]:
        assert bucket is self.bound_bucket and versions and page_size == 1000
        assert prefix.startswith("events-concierge/media/v1/") and prefix.endswith("/")
        self.calls.append("objects.list")
        return [
            cast("GcsBlob", _Blob(self, name, generation))
            for name, generation in sorted(self.objects)
            if name.startswith(prefix)
        ]


def _store(client: _Client) -> GcsMediaStore:
    return GcsMediaStore(cast("GcsStorageClient", client), bucket="private-media")


def _key(data: bytes = b"normalized-webp") -> str:
    return hashlib.sha256(data).hexdigest() + ".webp"


async def test_independent_instances_share_private_media_and_validate_identical_replays() -> None:
    client, tenant = _Client(), uuid4()
    writer, reader = _store(client), _store(client)
    assert client.calls == []
    await writer.put(tenant, _key(), b"normalized-webp", "image/webp")
    await writer.put(tenant, _key(), b"normalized-webp", "image/webp")
    assert await reader.get(tenant, _key()) == b"normalized-webp"
    assert len(client.objects) == 1
    stored = next(iter(client.objects.values()))
    assert stored.cache_control == "private, no-store"
    assert stored.content_type == "image/webp"
    assert stored.metadata == {
        "media_schema": "1",
        "tenant_id": str(tenant),
        "sha256": _key()[:-5],
    }
    assert "bucket.get" not in client.calls
    with pytest.raises(MediaNotFoundError):
        await reader.get(uuid4(), _key())
    await reader.delete(uuid4(), _key())
    assert len(client.objects) == 1
    await reader.delete(tenant, _key())
    await reader.delete(tenant, _key())
    assert client.objects == {}


@pytest.mark.parametrize("key", ["../escape", "a.webp", "A" * 64 + ".webp", "0" * 64 + ".png", ""])
async def test_invalid_keys_fail_before_any_provider_operation(key: str) -> None:
    client, tenant = _Client(), uuid4()
    store = _store(client)
    for operation in (
        lambda: store.put(tenant, key, b"normalized-webp", "image/webp"),
        lambda: store.get(tenant, key),
        lambda: store.delete(tenant, key),
    ):
        with pytest.raises(ValueError, match="content-addressed WebP"):
            await operation()
    assert client.calls == []


async def test_invalid_tenant_fails_before_any_provider_operation() -> None:
    client, tenant = _Client(), cast("UUID", "../../other")
    store = _store(client)
    for operation in (
        lambda: store.put(tenant, _key(), b"normalized-webp", "image/webp"),
        lambda: store.get(tenant, _key()),
        lambda: store.delete(tenant, _key()),
        lambda: store.delete_tenant(tenant),
    ):
        with pytest.raises(TypeError, match="UUID"):
            await operation()
    assert client.calls == []


@pytest.mark.parametrize(
    "data,content_type",
    [
        (b"", "image/webp"),
        (b"x" * (32 * 1024 + 1), "image/webp"),
        (b"normalized-webp", "image/png"),
        (b"different", "image/webp"),
    ],
)
async def test_invalid_upload_fails_before_provider_io(data: bytes, content_type: str) -> None:
    client = _Client()
    with pytest.raises(ValueError):
        await _store(client).put(uuid4(), _key(), data, content_type)
    assert client.calls == []


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("content_type", "text/html"),
        ("content_encoding", "gzip"),
        ("cache_control", "public"),
        ("metadata", {}),
        ("metadata", {"media_schema": "1", "tenant_id": str(uuid4()), "sha256": _key()[:-5]}),
        ("size", True),
        ("size", 32769),
        ("size", 0),
        ("size", "15"),
    ],
)
async def test_invalid_provider_metadata_is_rejected_before_download(
    field_name: str,
    value: object,
) -> None:
    client, tenant = _Client(), uuid4()
    store = _store(client)
    await store.put(tenant, _key(), b"normalized-webp", "image/webp")
    setattr(next(iter(client.objects.values())), field_name, value)
    with pytest.raises(ValueError, match="metadata"):
        await store.get(tenant, _key())
    with pytest.raises(ValueError, match="metadata"):
        await store.put(tenant, _key(), b"normalized-webp", "image/webp")
    assert "object.download" not in client.calls


async def test_provider_corrupt_bytes_and_generation_races_never_return_media() -> None:
    client, tenant = _Client(), uuid4()
    store = _store(client)
    await store.put(tenant, _key(), b"normalized-webp", "image/webp")
    obj = next(iter(client.objects.values()))
    obj.data = b"corrupt"
    with pytest.raises(ValueError, match="digest"):
        await store.get(tenant, _key())
    obj.data = b"normalized-webp"
    obj.size = 1
    with pytest.raises(ValueError, match="size is inconsistent"):
        await store.get(tenant, _key())
    obj.size = len(obj.data)
    client.download_conflict = True
    with pytest.raises(_ApiError, match="412"):
        await store.get(tenant, _key())
    client.download_conflict = False
    client.read_error = 403
    with pytest.raises(_ApiError, match="403"):
        await store.get(tenant, _key())


async def test_tenant_purge_removes_old_generations_and_orphans_but_preserves_other_tenants() -> (
    None
):
    client, tenant, survivor = _Client(), uuid4(), uuid4()
    store = _store(client)
    await store.put(tenant, _key(), b"normalized-webp", "image/webp")
    await store.put(survivor, _key(), b"normalized-webp", "image/webp")
    name = f"events-concierge/media/v1/{tenant}/{_key()}"
    client.objects[(name, 99)] = _Object(b"old", {})
    client.objects[(f"events-concierge/media/v1/{tenant}/orphan.webp", 100)] = _Object(
        b"orphan", {}
    )
    await store.delete_tenant(tenant)
    assert len(client.objects) == 1
    assert await store.get(survivor, _key()) == b"normalized-webp"
    assert client.calls.count("objects.list") == 3
    assert client.calls.count("bucket.get") == 2


@pytest.mark.parametrize(
    "policy", ["soft_delete", "retention", "default_hold", "versioning", "denied"]
)
async def test_delete_fails_closed_when_policy_cannot_prove_physical_erasure(policy: str) -> None:
    client, tenant = _Client(), uuid4()
    store = _store(client)
    await store.put(tenant, _key(), b"normalized-webp", "image/webp")
    match policy:
        case "soft_delete":
            client.bound_bucket.soft_delete_policy.retention_duration_seconds = 604800
        case "retention":
            client.bound_bucket.retention_period = 60
        case "default_hold":
            client.bound_bucket.default_event_based_hold = True
        case "versioning":
            client.bound_bucket.versioning_enabled = True
        case "denied":
            client.policy_error = 403
    for operation in (lambda: store.delete(tenant, _key()), lambda: store.delete_tenant(tenant)):
        with pytest.raises((RuntimeError, _ApiError)):
            await operation()
    assert "object.delete" not in client.calls and "objects.list" not in client.calls
    assert len(client.objects) == 1


@pytest.mark.parametrize("tenant_purge", [False, True])
async def test_object_holds_permission_failures_and_policy_races_never_claim_erasure(
    tenant_purge: bool,
) -> None:
    client, tenant = _Client(), uuid4()
    store = _store(client)
    await store.put(tenant, _key(), b"normalized-webp", "image/webp")
    operation = (
        (lambda: store.delete_tenant(tenant))
        if tenant_purge
        else lambda: store.delete(tenant, _key())
    )
    obj = next(iter(client.objects.values()))
    obj.held = True
    with pytest.raises(_ApiError, match="403"):
        await operation()
    obj.held = False
    client.delete_error = 403
    with pytest.raises(_ApiError, match="403"):
        await operation()
    client.delete_error = None
    client.enable_soft_delete_after_delete = True
    with pytest.raises(RuntimeError, match="policy"):
        await operation()


async def test_account_erasure_keeps_database_until_media_purge_is_proven(tmp_path) -> None:
    client, tenant, request = _Client(), uuid4(), uuid4()
    store = _store(client)
    await store.put(tenant, _key(), b"normalized-webp", "image/webp")
    client.bound_bucket.soft_delete_policy.retention_duration_seconds = 604800
    operations: list[str] = []
    repository = _MemoryErasureRepository(_snapshot(tenant, request, ()), operations)
    service = AccountErasureService(
        repository,
        _RecordingExternalEffects(operations),
        _WorkflowCanceller(operations),
        _RecordingCalendar(operations),
        _RecordingSessions(operations),
        _RecordingVault(operations),
        _RecordingObjectStore(tmp_path / "claims", operations),
        media_store=store,
    )
    result = await service.erase(tenant, request)
    assert result.status is AccountErasureStatus.ERASING
    assert result.failed_stage is AccountErasureStage.OBJECT_STORE
    assert not result.object_store_purged and not repository.finalize_calls
    assert "mark:object_store" not in operations
    assert len(client.objects) == 1

    client.bound_bucket.soft_delete_policy.retention_duration_seconds = 0
    result = await service.erase(tenant, request)
    assert result.status is AccountErasureStatus.COMPLETED
    assert result.object_store_purged and repository.finalize_calls == 1
    assert not client.objects
