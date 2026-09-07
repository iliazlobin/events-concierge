"""Hermetic contract coverage for the native GCS claim-check adapter."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.gcs import (
    GcsBlob,
    GcsBucket,
    GcsObjectStore,
    GcsStorageClient,
)
from events_concierge.ports.object_store import ObjectStoreNotFoundError


class _ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"status-{status}")
        self.code = status


class _MemoryBlob:
    def __init__(
        self,
        client: _MemoryClient,
        name: str,
        generation: int | str | None = None,
    ) -> None:
        self._client = client
        self.name = name
        self.generation = generation

    def upload_from_string(self, data: bytes, *, if_generation_match: int) -> None:
        self._client.upload_calls.append(
            {
                "name": self.name,
                "data": data,
                "if_generation_match": if_generation_match,
            }
        )
        if if_generation_match != 0:
            raise AssertionError("adapter did not use the generation-zero create precondition")
        if self.name in self._client.live:
            raise _ApiError(412)
        generation = self._client.next_generation
        self._client.next_generation += 1
        self._client.generations[(self.name, generation)] = data
        self._client.live[self.name] = generation

    def download_as_bytes(self) -> bytes:
        generation = (
            int(self.generation)
            if self.generation is not None
            else self._client.live.get(self.name)
        )
        if generation is None or (self.name, generation) not in self._client.generations:
            raise _ApiError(404)
        return self._client.generations[(self.name, generation)]

    def delete(self, *, if_generation_match: int) -> None:
        self._client.delete_calls.append(
            {
                "name": self.name,
                "generation": self.generation,
                "if_generation_match": if_generation_match,
            }
        )
        identity = (self.name, if_generation_match)
        if identity not in self._client.generations:
            raise _ApiError(404)
        del self._client.generations[identity]
        if self._client.live.get(self.name) == if_generation_match:
            del self._client.live[self.name]
        if self._client.after_delete is not None:
            self._client.after_delete(self.name)


class _MemoryBucket:
    def __init__(self, client: _MemoryClient, name: str) -> None:
        self._client = client
        self.name = name

    def blob(self, blob_name: str) -> GcsBlob:
        return _MemoryBlob(self._client, blob_name)


class _MemoryClient:
    def __init__(self) -> None:
        self.bucket_names: list[str] = []
        self.generations: dict[tuple[str, int], bytes] = {}
        self.live: dict[str, int] = {}
        self.next_generation = 1
        self.upload_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.after_delete: Callable[[str], None] | None = None

    def bucket(self, bucket_name: str) -> GcsBucket:
        self.bucket_names.append(bucket_name)
        return _MemoryBucket(self, bucket_name)

    def list_blobs(
        self,
        bucket: GcsBucket,
        *,
        prefix: str,
        versions: bool,
        page_size: int,
    ) -> Iterable[GcsBlob]:
        self.list_calls.append(
            {
                "bucket": bucket,
                "prefix": prefix,
                "versions": versions,
                "page_size": page_size,
            }
        )
        return [
            _MemoryBlob(self, name, generation)
            for name, generation in sorted(self.generations)
            if name.startswith(prefix)
        ]

    def add_generation(self, name: str, generation: int, data: bytes, *, live: bool) -> None:
        self.generations[(name, generation)] = data
        self.next_generation = max(self.next_generation, generation + 1)
        if live:
            self.live[name] = generation


def _store(client: _MemoryClient) -> GcsObjectStore:
    return GcsObjectStore(
        cast("GcsStorageClient", client),
        bucket="private-claims",
        root_prefix="events-concierge/claims/v1",
    )


async def test_put_uses_generation_zero_and_accepts_only_identical_replay() -> None:
    client = _MemoryClient()
    store = _store(client)
    tenant_id = uuid4()

    await store.put(tenant_id, "temporal/abc.payload", b"opaque")
    await store.put(tenant_id, "temporal/abc.payload", b"opaque")

    object_name = f"events-concierge/claims/v1/{tenant_id}/temporal/abc.payload"
    assert client.live == {object_name: 1}
    assert client.upload_calls[0] == {
        "name": object_name,
        "data": b"opaque",
        "if_generation_match": 0,
    }

    with pytest.raises(ValueError, match="different bytes"):
        await store.put(tenant_id, "temporal/abc.payload", b"changed")


async def test_put_preserves_conflict_when_erasure_wins_before_replay_read() -> None:
    client = _MemoryClient()
    store = _store(client)
    tenant_id = uuid4()
    await store.put(tenant_id, "temporal/abc.payload", b"opaque")

    original_blob = cast("_MemoryBucket", store._bucket).blob

    def conflicting_blob(name: str) -> GcsBlob:
        blob = cast("_MemoryBlob", original_blob(name))

        def conflict_then_delete(data: bytes, *, if_generation_match: int) -> None:
            del data, if_generation_match
            generation = client.live.pop(name)
            del client.generations[(name, generation)]
            raise _ApiError(412)

        blob.upload_from_string = conflict_then_delete  # type: ignore[method-assign]
        return blob

    cast("_MemoryBucket", store._bucket).blob = conflicting_blob  # type: ignore[assignment]

    with pytest.raises(_ApiError, match="status-412"):
        await store.put(tenant_id, "temporal/abc.payload", b"opaque")


async def test_get_maps_only_not_found_and_requires_bytes() -> None:
    client = _MemoryClient()
    store = _store(client)
    tenant_id = uuid4()
    await store.put(tenant_id, "temporal/abc.payload", b"opaque")

    assert await store.get(tenant_id, "temporal/abc.payload") == b"opaque"
    with pytest.raises(ObjectStoreNotFoundError):
        await store.get(tenant_id, "temporal/missing.payload")

    blob = cast("_MemoryBlob", cast("_MemoryBucket", store._bucket).blob("ignored"))

    def denied() -> bytes:
        raise _ApiError(403)

    blob.download_as_bytes = denied  # type: ignore[method-assign]
    cast("_MemoryBucket", store._bucket).blob = lambda _: blob  # type: ignore[assignment]
    with pytest.raises(_ApiError, match="status-403"):
        await store.get(tenant_id, "temporal/abc.payload")

    blob.download_as_bytes = lambda: cast("bytes", bytearray(b"wrong"))  # type: ignore[method-assign]
    with pytest.raises(TypeError, match="must return bytes"):
        await store.get(tenant_id, "temporal/abc.payload")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/absolute",
        "../escape",
        "temporal/../escape",
        "temporal//double",
        "temporal/trailing/",
        "temporal/.hidden",
        "temporal/back\\slash",
        "temporal/white space",
        "temporal/é.payload",
    ],
)
async def test_keys_are_strictly_tenant_relative(key: str) -> None:
    with pytest.raises(ValueError, match="safe slash-delimited"):
        await _store(_MemoryClient()).put(uuid4(), key, b"opaque")


async def test_invalid_tenant_data_and_oversized_name_fail_before_upload() -> None:
    client = _MemoryClient()
    store = _store(client)

    with pytest.raises(TypeError, match="UUID"):
        await store.put(cast("UUID", str(uuid4())), "temporal/abc.payload", b"opaque")
    with pytest.raises(TypeError, match="must be bytes"):
        await store.put(uuid4(), "temporal/abc.payload", cast("bytes", bytearray(b"no")))
    with pytest.raises(ValueError, match="1,024-byte"):
        await store.put(uuid4(), "a" * 1000, b"opaque")

    assert client.upload_calls == []


async def test_delete_tenant_removes_all_generations_under_only_the_exact_prefix() -> None:
    client = _MemoryClient()
    store = _store(client)
    erased_tenant, surviving_tenant = uuid4(), uuid4()
    erased_prefix = f"events-concierge/claims/v1/{erased_tenant}/"
    survivor_name = f"events-concierge/claims/v1/{surviving_tenant}/temporal/keep.payload"
    client.add_generation(f"{erased_prefix}temporal/a.payload", 11, b"old", live=False)
    client.add_generation(f"{erased_prefix}temporal/a.payload", 12, b"new", live=True)
    client.add_generation(f"{erased_prefix}temporal/b.payload", 13, b"only", live=True)
    client.add_generation(survivor_name, 14, b"keep", live=True)

    await store.delete_tenant(erased_tenant)

    assert client.generations == {(survivor_name, 14): b"keep"}
    assert {(call["name"], call["if_generation_match"]) for call in client.delete_calls} == {
        (f"{erased_prefix}temporal/a.payload", 11),
        (f"{erased_prefix}temporal/a.payload", 12),
        (f"{erased_prefix}temporal/b.payload", 13),
    }
    assert all(call["versions"] is True for call in client.list_calls)
    assert all(call["page_size"] == 1000 for call in client.list_calls)


async def test_delete_tenant_is_idempotent_and_rechecks_an_empty_listing() -> None:
    client = _MemoryClient()

    await _store(client).delete_tenant(uuid4())

    assert client.delete_calls == []
    assert len(client.list_calls) == 2


async def test_delete_tenant_repeats_for_a_concurrent_generation() -> None:
    client = _MemoryClient()
    store = _store(client)
    tenant_id = uuid4()
    prefix = f"events-concierge/claims/v1/{tenant_id}/"
    first = f"{prefix}temporal/first.payload"
    raced = f"{prefix}temporal/raced.payload"
    client.add_generation(first, 1, b"first", live=True)
    inserted = False

    def insert_once(_: str) -> None:
        nonlocal inserted
        if not inserted:
            client.add_generation(raced, 2, b"raced", live=True)
            inserted = True

    client.after_delete = insert_once

    await store.delete_tenant(tenant_id)

    assert client.generations == {}
    assert len(client.delete_calls) == 2


async def test_delete_tenant_fails_when_the_prefix_is_continually_repopulated() -> None:
    client = _MemoryClient()
    store = _store(client)
    tenant_id = uuid4()
    name = f"events-concierge/claims/v1/{tenant_id}/temporal/recreated.payload"
    client.add_generation(name, 1, b"first", live=True)

    def recreate(_: str) -> None:
        generation = client.next_generation
        client.add_generation(name, generation, b"again", live=True)

    client.after_delete = recreate

    with pytest.raises(RuntimeError, match="did not converge"):
        await store.delete_tenant(tenant_id)

    assert client.generations


@pytest.mark.parametrize("generation", [None, 0, -1, True, "", "abc"])
async def test_delete_fails_closed_on_invalid_generation(generation: object) -> None:
    client = _MemoryClient()
    store = _store(client)
    tenant_id = uuid4()
    prefix = f"events-concierge/claims/v1/{tenant_id}/"
    malformed = SimpleNamespace(name=f"{prefix}temporal/a.payload", generation=generation)
    client.list_blobs = lambda *args, **kwargs: [malformed]  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="invalid generation"):
        await store.delete_tenant(tenant_id)


async def test_delete_fails_closed_on_out_of_scope_listing_and_access_denial() -> None:
    client = _MemoryClient()
    store = _store(client)
    client.list_blobs = lambda *args, **kwargs: [  # type: ignore[method-assign]
        SimpleNamespace(name="another/prefix/object", generation=1)
    ]
    with pytest.raises(ValueError, match="out-of-scope"):
        await store.delete_tenant(uuid4())

    client = _MemoryClient()
    store = _store(client)
    tenant_id = uuid4()
    name = f"events-concierge/claims/v1/{tenant_id}/temporal/a.payload"
    client.add_generation(name, 1, b"opaque", live=True)

    def deny(*, if_generation_match: int) -> None:
        del if_generation_match
        raise _ApiError(403)

    blob = _MemoryBlob(client, name, 1)
    blob.delete = deny  # type: ignore[method-assign]
    client.list_blobs = lambda *args, **kwargs: [blob]  # type: ignore[method-assign]
    with pytest.raises(_ApiError, match="status-403"):
        await store.delete_tenant(tenant_id)


@pytest.mark.parametrize(
    "bucket",
    [
        "",
        "ab",
        "Uppercase",
        " leading",
        "trailing ",
        "white space",
        "192.168.1.1",
        "a" * 64,
        "a..b",
        "a.-b",
    ],
)
def test_bucket_identifier_is_bounded_and_canonical(bucket: str) -> None:
    with pytest.raises(ValueError, match="bucket"):
        GcsObjectStore(cast("GcsStorageClient", _MemoryClient()), bucket=bucket)
