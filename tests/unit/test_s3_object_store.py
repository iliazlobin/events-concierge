"""SDK-independent contract coverage for the S3-compatible claim-check adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.s3 import S3CompatibleObjectStore
from events_concierge.ports.object_store import ObjectStoreNotFoundError


class _ClientError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response: Mapping[str, object] = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _StreamingBody:
    def __init__(self, value: bytes) -> None:
        self._value = value
        self.closed = False

    def read(self) -> bytes:
        return self._value

    def close(self) -> None:
        self.closed = True


class _MemoryS3Client:
    def __init__(self, *, page_size: int = 1000, version_page_size: int = 1000) -> None:
        self.objects: dict[str, bytes] = {}
        self.versions: dict[tuple[str, str], bytes] = {}
        self.delete_markers: set[tuple[str, str]] = set()
        self.page_size = page_size
        self.version_page_size = version_page_size
        self.put_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.last_body: _StreamingBody | None = None

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        self.put_calls.append(kwargs)
        key = cast("str", kwargs["Key"])
        body = cast("bytes", kwargs["Body"])
        if key in self.objects:
            raise _ClientError("PreconditionFailed", 412)
        self.objects[key] = body
        return {}

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        key = cast("str", kwargs["Key"])
        if key not in self.objects:
            raise _ClientError("NoSuchKey", 404)
        self.last_body = _StreamingBody(self.objects[key])
        return {"Body": self.last_body}

    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        prefix = cast("str", kwargs["Prefix"])
        matching = sorted(key for key in self.objects if key.startswith(prefix))
        offset = int(cast("str", kwargs.get("ContinuationToken", "0")))
        page = matching[offset : offset + self.page_size]
        next_offset = offset + len(page)
        response: dict[str, object] = {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": next_offset < len(matching),
        }
        if next_offset < len(matching):
            response["NextContinuationToken"] = str(next_offset)
        return response

    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]:
        prefix = cast("str", kwargs["Prefix"])
        entries = sorted(
            [
                ("version", key, version_id)
                for key, version_id in self.versions
                if key.startswith(prefix)
            ]
            + [
                ("delete-marker", key, version_id)
                for key, version_id in self.delete_markers
                if key.startswith(prefix)
            ]
            + [
                ("version", key, "null")
                for key in self.objects
                if key.startswith(prefix)
            ],
            key=lambda entry: (entry[1], entry[2]),
        )
        key_marker = kwargs.get("KeyMarker")
        version_id_marker = kwargs.get("VersionIdMarker")
        offset = 0
        if key_marker is not None:
            marker = (cast("str", key_marker), cast("str", version_id_marker))
            offset = next(
                index + 1
                for index, (_, key, version_id) in enumerate(entries)
                if (key, version_id) == marker
            )
        page = entries[offset : offset + self.version_page_size]
        versions = [
            {"Key": key, "VersionId": version_id}
            for kind, key, version_id in page
            if kind == "version"
        ]
        delete_markers = [
            {"Key": key, "VersionId": version_id}
            for kind, key, version_id in page
            if kind == "delete-marker"
        ]
        next_offset = offset + len(page)
        response: dict[str, object] = {
            "Versions": versions,
            "DeleteMarkers": delete_markers,
            "IsTruncated": next_offset < len(entries),
        }
        if next_offset < len(entries):
            _, next_key_marker, next_version_id_marker = page[-1]
            response["NextKeyMarker"] = next_key_marker
            response["NextVersionIdMarker"] = next_version_id_marker
        return response

    def delete_objects(self, **kwargs: object) -> Mapping[str, object]:
        self.delete_calls.append(kwargs)
        request = cast("dict[str, object]", kwargs["Delete"])
        entries = cast("list[dict[str, str]]", request["Objects"])
        for entry in entries:
            key = entry["Key"]
            version_id = entry.get("VersionId")
            if version_id is None or version_id == "null":
                self.objects.pop(key, None)
            else:
                self.versions.pop((key, version_id), None)
                self.delete_markers.discard((key, version_id))
        return {}


def _store(client: _MemoryS3Client) -> S3CompatibleObjectStore:
    return S3CompatibleObjectStore(
        client,
        bucket="private-claims",
        root_prefix="events-concierge/claims/v1",
    )


async def test_put_is_conditional_and_identical_replay_is_idempotent() -> None:
    client = _MemoryS3Client()
    store = _store(client)
    tenant_id = uuid4()
    key = "temporal/abc.payload"

    await store.put(tenant_id, key, b"opaque")
    await store.put(tenant_id, key, b"opaque")

    expected_key = f"events-concierge/claims/v1/{tenant_id}/{key}"
    assert client.objects == {expected_key: b"opaque"}
    assert client.put_calls[0] == {
        "Bucket": "private-claims",
        "Key": expected_key,
        "Body": b"opaque",
        "IfNoneMatch": "*",
    }
    assert client.last_body is not None and client.last_body.closed


async def test_put_refuses_to_rebind_an_existing_key() -> None:
    client = _MemoryS3Client()
    store = _store(client)
    tenant_id = uuid4()

    await store.put(tenant_id, "temporal/abc.payload", b"first")

    with pytest.raises(ValueError, match="different bytes"):
        await store.put(tenant_id, "temporal/abc.payload", b"second")


async def test_get_maps_only_not_found_and_closes_streaming_body() -> None:
    client = _MemoryS3Client()
    store = _store(client)
    tenant_id = uuid4()
    await store.put(tenant_id, "temporal/abc.payload", b"opaque")

    assert await store.get(tenant_id, "temporal/abc.payload") == b"opaque"
    assert client.last_body is not None and client.last_body.closed

    with pytest.raises(ObjectStoreNotFoundError):
        await store.get(tenant_id, "temporal/missing.payload")

    def deny(**kwargs: object) -> Mapping[str, object]:
        del kwargs
        raise _ClientError("AccessDenied", 403)

    client.get_object = deny  # type: ignore[method-assign]
    with pytest.raises(_ClientError, match="AccessDenied"):
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
        await _store(_MemoryS3Client()).put(uuid4(), key, b"opaque")


async def test_invalid_tenant_type_and_oversized_full_key_fail_before_client_call() -> None:
    client = _MemoryS3Client()
    store = _store(client)

    with pytest.raises(TypeError, match="UUID"):
        await store.put(cast("UUID", str(uuid4())), "temporal/abc.payload", b"opaque")
    with pytest.raises(ValueError, match="1,024-byte"):
        await store.put(uuid4(), "a" * 1000, b"opaque")

    assert client.put_calls == []


async def test_delete_tenant_paginates_and_deletes_only_exact_prefix_in_batches() -> None:
    client = _MemoryS3Client(page_size=127, version_page_size=127)
    store = _store(client)
    erased_tenant, surviving_tenant = uuid4(), uuid4()
    for index in range(1002):
        await store.put(erased_tenant, f"temporal/{index}.payload", b"erased")
    await store.put(surviving_tenant, "temporal/keep.payload", b"survives")

    await store.delete_tenant(erased_tenant)

    assert len(client.delete_calls) == 2
    assert [
        len(cast("dict[str, object]", call["Delete"])["Objects"])  # type: ignore[arg-type]
        for call in client.delete_calls
    ] == [1000, 2]
    assert len(client.objects) == 1
    assert await store.get(surviving_tenant, "temporal/keep.payload") == b"survives"


async def test_delete_tenant_is_idempotent_when_prefix_is_empty() -> None:
    client = _MemoryS3Client()

    await _store(client).delete_tenant(uuid4())

    assert client.delete_calls == []


async def test_delete_tenant_permanently_removes_versions_and_delete_markers() -> None:
    client = _MemoryS3Client(version_page_size=1)
    store = _store(client)
    erased_tenant, surviving_tenant = uuid4(), uuid4()
    erased_prefix = f"events-concierge/claims/v1/{erased_tenant}/"
    surviving_prefix = f"events-concierge/claims/v1/{surviving_tenant}/"
    client.versions.update(
        {
            (f"{erased_prefix}temporal/a.payload", "v1"): b"old",
            (f"{erased_prefix}temporal/a.payload", "v2"): b"new",
            (f"{surviving_prefix}temporal/keep.payload", "v1"): b"keep",
        }
    )
    client.delete_markers.update(
        {
            (f"{erased_prefix}temporal/a.payload", "d1"),
            (f"{erased_prefix}temporal/gone.payload", "d2"),
            (f"{surviving_prefix}temporal/keep.payload", "d1"),
        }
    )

    await store.delete_tenant(erased_tenant)

    assert {
        (identifier["Key"], identifier["VersionId"])
        for call in client.delete_calls
        for identifier in cast(
            "list[dict[str, str]]",
            cast("dict[str, object]", call["Delete"])["Objects"],
        )
    } == {
        (f"{erased_prefix}temporal/a.payload", "v1"),
        (f"{erased_prefix}temporal/a.payload", "v2"),
        (f"{erased_prefix}temporal/a.payload", "d1"),
        (f"{erased_prefix}temporal/gone.payload", "d2"),
    }
    assert client.versions == {
        (f"{surviving_prefix}temporal/keep.payload", "v1"): b"keep"
    }
    assert client.delete_markers == {
        (f"{surviving_prefix}temporal/keep.payload", "d1")
    }


async def test_delete_tenant_handles_live_keys_omitted_from_version_listing() -> None:
    client = _MemoryS3Client()
    store = _store(client)
    tenant_id = uuid4()
    await store.put(tenant_id, "temporal/unversioned.payload", b"opaque")

    def no_version_support(**kwargs: object) -> Mapping[str, object]:
        del kwargs
        return {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}

    client.list_object_versions = no_version_support  # type: ignore[method-assign]

    await store.delete_tenant(tenant_id)

    identifiers = cast(
        "list[dict[str, str]]",
        cast("dict[str, object]", client.delete_calls[0]["Delete"])["Objects"],
    )
    assert identifiers == [
        {
            "Key": (
                f"events-concierge/claims/v1/{tenant_id}/"
                "temporal/unversioned.payload"
            )
        }
    ]
    assert client.objects == {}


async def test_delete_tenant_repeats_until_a_concurrent_write_is_observed_empty() -> None:
    client = _MemoryS3Client()
    store = _store(client)
    tenant_id = uuid4()
    first_key = f"events-concierge/claims/v1/{tenant_id}/temporal/first.payload"
    concurrent_key = f"events-concierge/claims/v1/{tenant_id}/temporal/concurrent.payload"
    client.objects[first_key] = b"first"
    original_delete = client.delete_objects
    inserted = False

    def insert_during_first_delete(**kwargs: object) -> Mapping[str, object]:
        nonlocal inserted
        response = original_delete(**kwargs)
        if not inserted:
            client.objects[concurrent_key] = b"concurrent"
            inserted = True
        return response

    client.delete_objects = insert_during_first_delete  # type: ignore[method-assign]

    await store.delete_tenant(tenant_id)

    assert client.objects == {}
    assert len(client.delete_calls) == 2


async def test_delete_tenant_rechecks_an_empty_snapshot_for_a_version_listing_race() -> None:
    client = _MemoryS3Client()
    store = _store(client)
    tenant_id = uuid4()
    key = f"events-concierge/claims/v1/{tenant_id}/temporal/raced.payload"
    original_list_live = client.list_objects_v2
    inserted = False

    def insert_after_first_version_scan(**kwargs: object) -> Mapping[str, object]:
        nonlocal inserted
        response = original_list_live(**kwargs)
        if not inserted:
            client.versions[(key, "v1")] = b"raced"
            client.delete_markers.add((key, "d1"))
            inserted = True
        return response

    client.list_objects_v2 = insert_after_first_version_scan  # type: ignore[method-assign]

    await store.delete_tenant(tenant_id)

    assert client.versions == {}
    assert client.delete_markers == set()
    assert len(client.delete_calls) == 1


async def test_delete_tenant_fails_when_concurrent_writes_never_converge() -> None:
    client = _MemoryS3Client()
    store = _store(client)
    tenant_id = uuid4()
    key = f"events-concierge/claims/v1/{tenant_id}/temporal/recreated.payload"
    client.objects[key] = b"first"
    original_delete = client.delete_objects

    def recreate_after_every_delete(**kwargs: object) -> Mapping[str, object]:
        response = original_delete(**kwargs)
        client.objects[key] = b"recreated"
        return response

    client.delete_objects = recreate_after_every_delete  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="did not converge"):
        await store.delete_tenant(tenant_id)

    assert len(client.delete_calls) > 1
    assert client.objects == {key: b"recreated"}


async def test_delete_fails_closed_on_out_of_scope_listing_or_partial_delete() -> None:
    client = _MemoryS3Client()
    store = _store(client)
    tenant_id = uuid4()

    def out_of_scope(**kwargs: object) -> Mapping[str, object]:
        del kwargs
        return {"Contents": [{"Key": "another/prefix/key"}]}

    client.list_objects_v2 = out_of_scope  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="out-of-scope"):
        await store.delete_tenant(tenant_id)

    client = _MemoryS3Client()
    store = _store(client)
    await store.put(tenant_id, "temporal/abc.payload", b"opaque")

    def partial_delete(**kwargs: object) -> Mapping[str, object]:
        del kwargs
        return {"Errors": [{"Code": "AccessDenied"}]}

    client.delete_objects = partial_delete  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="deletion failures"):
        await store.delete_tenant(tenant_id)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            {
                "Versions": [{"Key": "another/prefix/key", "VersionId": "v1"}],
                "IsTruncated": False,
            },
            "out-of-scope",
        ),
        (
            {
                "Versions": [{"Key": "replace-me", "VersionId": ""}],
                "IsTruncated": False,
            },
            "invalid VersionId",
        ),
        (
            {
                "Versions": [],
                "IsTruncated": True,
                "NextKeyMarker": "another/prefix/key",
                "NextVersionIdMarker": "v1",
            },
            "out-of-scope",
        ),
        (
            {
                "Versions": [],
                "IsTruncated": True,
                "NextKeyMarker": "replace-me",
                "NextVersionIdMarker": "",
            },
            "invalid pagination marker",
        ),
        (
            {
                "Versions": [],
                "IsTruncated": True,
                "NextKeyMarker": "replace-me",
                "NextVersionIdMarker": "v1",
            },
            "invalid pagination marker",
        ),
    ],
)
async def test_delete_fails_closed_on_malformed_version_listing(
    response: dict[str, object],
    error: str,
) -> None:
    client = _MemoryS3Client()
    store = _store(client)
    tenant_id = uuid4()
    prefix = f"events-concierge/claims/v1/{tenant_id}/"
    in_scope_key = f"{prefix}temporal/key.payload"
    resolved_response = dict(response)
    if resolved_response.get("NextKeyMarker") == "replace-me":
        resolved_response["NextKeyMarker"] = in_scope_key
    versions = resolved_response.get("Versions")
    if isinstance(versions, list):
        resolved_response["Versions"] = [
            {
                **cast("dict[str, object]", item),
                "Key": (
                    in_scope_key
                    if cast("dict[str, object]", item).get("Key") == "replace-me"
                    else cast("dict[str, object]", item).get("Key")
                ),
            }
            for item in versions
        ]

    def malformed(**kwargs: object) -> Mapping[str, object]:
        del kwargs
        return resolved_response

    client.list_object_versions = malformed  # type: ignore[method-assign]

    with pytest.raises((RuntimeError, ValueError), match=error):
        await store.delete_tenant(tenant_id)


@pytest.mark.parametrize("bucket", ["", " leading", "trailing ", "white space", "x" * 256])
def test_bucket_identifier_is_bounded_and_unambiguous(bucket: str) -> None:
    with pytest.raises(ValueError, match="bucket"):
        S3CompatibleObjectStore(_MemoryS3Client(), bucket=bucket)
