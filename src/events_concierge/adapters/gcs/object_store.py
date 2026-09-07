"""SDK-independent Google Cloud Storage claim-check storage (FR-8.5, FR-10.5).

The adapter accepts the narrow synchronous surface exposed by ``google-cloud-storage``.  Cloud
credentials, retry policy, transport construction, and Workload Identity remain deployment-owned.
An immutable claim is created with GCS' generation-zero precondition; a retry is accepted only
after verifying that the existing bytes are identical.

Tenant erasure enumerates and deletes every object generation below one exact tenant prefix.  The
production bucket must have soft delete disabled (and no retention policy or hold), because an
adapter cannot turn a successful GCS delete into physical erasure while either service-side policy
retains the deleted generation.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from ipaddress import ip_address
from threading import Lock
from typing import Protocol
from uuid import UUID

from ...ports.object_store import ObjectStoreNotFoundError

_SAFE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_BUCKET_COMPONENT = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?")
_MIN_BUCKET_LENGTH = 3
_MAX_BUCKET_LENGTH = 222
_MAX_BUCKET_COMPONENT_LENGTH = 63
_MAX_OBJECT_NAME_BYTES = 1024
_LIST_PAGE_SIZE = 1000
_MAX_TENANT_PURGE_PASSES = 8
_HTTP_NOT_FOUND = 404
_HTTP_CONDITIONAL_CONFLICTS = frozenset({409, 412})


class GcsBlob(Protocol):
    """One synchronous GCS blob returned by the official client."""

    @property
    def name(self) -> str:
        """Complete object name."""
        ...

    @property
    def generation(self) -> int | str | None:
        """Immutable GCS object generation, populated for listed blobs."""
        ...

    def upload_from_string(self, data: bytes, *, if_generation_match: int) -> None:
        """Upload bytes with a generation precondition."""
        ...

    def download_as_bytes(self) -> bytes:
        """Download the complete blob value."""
        ...

    def delete(self, *, if_generation_match: int) -> None:
        """Delete exactly one immutable generation."""
        ...


class GcsBucket(Protocol):
    """Minimal bucket surface used for point reads and writes."""

    def blob(self, blob_name: str) -> GcsBlob:
        """Return a lazily bound blob reference without performing I/O."""
        ...


class GcsStorageClient(Protocol):
    """Minimal synchronous client surface used by :class:`GcsObjectStore`."""

    def bucket(self, bucket_name: str) -> GcsBucket:
        """Return a lazily bound bucket reference without performing I/O."""
        ...

    def list_blobs(
        self,
        bucket: GcsBucket,
        *,
        prefix: str,
        versions: bool,
        page_size: int,
    ) -> Iterable[GcsBlob]:
        """Iterate all object generations below ``prefix``."""
        ...


class GcsObjectStore:
    """Tenant-prefixed immutable byte storage backed by Google Cloud Storage."""

    def __init__(
        self,
        client: GcsStorageClient,
        *,
        bucket: str,
        root_prefix: str = "events-concierge/claim-check/v1",
    ) -> None:
        self._client = client
        self._bucket_name = _validated_bucket(bucket)
        self._bucket: GcsBucket | None = None
        self._bucket_lock = Lock()
        self._root_prefix = _validated_path(root_prefix, label="root prefix")

    async def put(self, tenant_id: UUID, key: str, data: bytes) -> None:
        """Create an immutable object, accepting only byte-identical conditional replays."""
        await asyncio.to_thread(self._put_sync, tenant_id, key, data)

    async def get(self, tenant_id: UUID, key: str) -> bytes:
        """Return one tenant-scoped object and translate ordinary absence to the port error."""
        return await asyncio.to_thread(self._get_sync, tenant_id, key)

    async def delete_tenant(self, tenant_id: UUID) -> None:
        """Delete all generations below exactly one tenant prefix until the prefix is stable."""
        await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    def _put_sync(self, tenant_id: UUID, key: str, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("claim-check object data must be bytes")
        object_name = self._object_name(tenant_id, key)
        try:
            self._bound_bucket().blob(object_name).upload_from_string(data, if_generation_match=0)
        except Exception as error:
            if not _is_conditional_conflict(error):
                raise
            try:
                existing = self._get_object_sync(object_name)
            except ObjectStoreNotFoundError as not_found_error:
                # Preserve the conditional-write failure when concurrent erasure wins between the
                # rejected create and the replay verification read.
                raise error from not_found_error
            if existing != data:
                raise ValueError(
                    "claim-check object key is already bound to different bytes"
                ) from error

    def _get_sync(self, tenant_id: UUID, key: str) -> bytes:
        return self._get_object_sync(self._object_name(tenant_id, key))

    def _get_object_sync(self, object_name: str) -> bytes:
        try:
            value = self._bound_bucket().blob(object_name).download_as_bytes()
        except Exception as error:
            if _http_status(error) == _HTTP_NOT_FOUND:
                raise ObjectStoreNotFoundError("claim-check object is not available") from error
            raise
        if not isinstance(value, bytes):
            raise TypeError("GCS Blob.download_as_bytes() must return bytes")
        return value

    def _delete_tenant_sync(self, tenant_id: UUID) -> None:
        tenant_prefix = self._tenant_prefix(tenant_id)
        for _ in range(_MAX_TENANT_PURGE_PASSES):
            blobs = self._tenant_purge_snapshot(tenant_prefix)
            if not blobs:
                # A second complete observation closes the common race where a generation lands
                # just after the first prefix listing completed.
                blobs = self._tenant_purge_snapshot(tenant_prefix)
                if not blobs:
                    return
            for blob, generation in blobs:
                try:
                    blob.delete(if_generation_match=generation)
                except Exception as error:
                    # Another eraser deleting this exact generation is an idempotent success.  All
                    # other failures remain visible; especially, authorization and retention/hold
                    # errors must never be reported as a completed tenant purge.
                    if _http_status(error) != _HTTP_NOT_FOUND:
                        raise

        if self._tenant_purge_snapshot(tenant_prefix) or self._tenant_purge_snapshot(tenant_prefix):
            raise RuntimeError("GCS tenant-prefix purge did not converge to an empty prefix")

    def _tenant_purge_snapshot(self, tenant_prefix: str) -> list[tuple[GcsBlob, int]]:
        listed = self._client.list_blobs(
            self._bound_bucket(),
            prefix=tenant_prefix,
            versions=True,
            page_size=_LIST_PAGE_SIZE,
        )
        unique: dict[tuple[str, int], tuple[GcsBlob, int]] = {}
        for blob in listed:
            name = _validated_listed_name(blob.name, tenant_prefix)
            generation = _validated_generation(blob.generation)
            unique[(name, generation)] = (blob, generation)
        return list(unique.values())

    def _bound_bucket(self) -> GcsBucket:
        bucket = self._bucket
        if bucket is not None:
            return bucket
        with self._bucket_lock:
            if self._bucket is None:
                self._bucket = self._client.bucket(self._bucket_name)
            return self._bucket

    def _tenant_prefix(self, tenant_id: UUID) -> str:
        canonical_tenant_id = _validated_tenant_id(tenant_id)
        prefix = f"{self._root_prefix}/{canonical_tenant_id}/"
        _validate_object_name_length(prefix)
        return prefix

    def _object_name(self, tenant_id: UUID, key: str) -> str:
        relative_key = _validated_path(key, label="claim-check key")
        object_name = f"{self._tenant_prefix(tenant_id)}{relative_key}"
        _validate_object_name_length(object_name)
        return object_name


def _validated_bucket(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _MIN_BUCKET_LENGTH <= len(value) <= _MAX_BUCKET_LENGTH
    ):
        raise ValueError("GCS bucket must be a bounded canonical bucket name")
    components = value.split(".")
    if (len(components) == 1 and len(value) > _MAX_BUCKET_COMPONENT_LENGTH) or any(
        _BUCKET_COMPONENT.fullmatch(component) is None for component in components
    ):
        raise ValueError("GCS bucket must be a bounded canonical bucket name")
    try:
        ip_address(value)
    except ValueError:
        return value
    raise ValueError("GCS bucket must not be formatted as an IP address")


def _validated_path(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SAFE_PATH.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe slash-delimited identifier")
    return value


def _validated_tenant_id(value: UUID) -> str:
    if not isinstance(value, UUID):
        raise TypeError("tenant_id must be a UUID")
    canonical = str(value)
    if UUID(canonical) != value:
        raise ValueError("tenant_id must be a canonical UUID")
    return canonical


def _validate_object_name_length(value: str) -> None:
    if len(value.encode("utf-8")) > _MAX_OBJECT_NAME_BYTES:
        raise ValueError("GCS object name exceeds the 1,024-byte limit")


def _validated_listed_name(value: object, tenant_prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(tenant_prefix):
        raise ValueError("GCS tenant-prefix listing returned an out-of-scope object")
    _validate_object_name_length(value)
    return value


def _validated_generation(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("GCS tenant-prefix listing returned an invalid generation")
    if isinstance(value, int):
        generation = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        generation = int(value)
    else:
        raise ValueError("GCS tenant-prefix listing returned an invalid generation")
    if generation <= 0:
        raise ValueError("GCS tenant-prefix listing returned an invalid generation")
    return generation


def _is_conditional_conflict(error: Exception) -> bool:
    status = _http_status(error)
    return status in _HTTP_CONDITIONAL_CONFLICTS


def _http_status(error: Exception) -> int | None:
    for value in (
        getattr(error, "code", None),
        getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
        getattr(getattr(error, "response", None), "status", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
    return None
