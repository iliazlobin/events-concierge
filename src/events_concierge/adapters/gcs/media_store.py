"""Private, bounded avatar objects in a dedicated durable GCS media bucket.

Only authenticated application routes serve these bytes. Objects are immutable and content
addressed within one tenant prefix; this adapter never generates a public or signed URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from threading import Lock
from typing import Protocol, cast
from uuid import UUID

from ...ports.media_store import MediaNotFoundError
from .object_store import (
    GcsObjectStore,
    GcsStorageClient,
    _http_status,
    _validate_object_name_length,
    _validated_bucket,
    _validated_generation,
    _validated_path,
    _validated_tenant_id,
)

_MAX_MEDIA_BYTES = 32 * 1024
_CONTENT_TYPE = "image/webp"
_CACHE_CONTROL = "private, no-store"
_HTTP_NOT_FOUND = 404
_HTTP_PRECONDITION_FAILED = 412
_KEY = re.compile(r"([0-9a-f]{64})\.webp")


class _MediaBlob(Protocol):
    generation: int | str | None
    size: int | None
    content_type: str | None
    content_encoding: str | None
    cache_control: str | None
    metadata: dict[str, str] | None

    def reload(self) -> None: ...

    def upload_from_string(
        self,
        data: bytes,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None: ...

    def download_as_bytes(
        self,
        *,
        start: int,
        end: int,
        raw_download: bool,
        if_generation_match: int,
    ) -> bytes: ...

    def delete(self, *, if_generation_match: int) -> None: ...


class _SoftDeletePolicy(Protocol):
    retention_duration_seconds: int | None


class _MediaBucket(Protocol):
    soft_delete_policy: _SoftDeletePolicy
    retention_period: int | None
    default_event_based_hold: bool | None
    versioning_enabled: bool

    def reload(self) -> None: ...

    def blob(self, name: str) -> _MediaBlob: ...


class GcsMediaStore:
    """Store normalized WebP avatars, validating their metadata and bytes on every read."""

    def __init__(
        self,
        client: GcsStorageClient,
        *,
        bucket: str,
        root_prefix: str = "events-concierge/media/v1",
    ) -> None:
        self._client = client
        self._bucket_name = _validated_bucket(bucket)
        self._root_prefix = _validated_path(root_prefix, label="media prefix")
        if not self._root_prefix.startswith("events-concierge/media/"):
            raise ValueError("GCS media requires its separate events-concierge/media/ prefix")
        self._bucket: _MediaBucket | None = None
        self._lock = Lock()
        # Reuse the generation-aware, bounded tenant purge, under the media-only prefix.
        self._objects = GcsObjectStore(client, bucket=bucket, root_prefix=root_prefix)

    async def put(self, tenant_id: UUID, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._put_sync, tenant_id, key, data, content_type)

    async def get(self, tenant_id: UUID, key: str) -> bytes:
        return await asyncio.to_thread(self._get_sync, tenant_id, key)

    async def delete(self, tenant_id: UUID, key: str) -> None:
        await asyncio.to_thread(self._delete_sync, tenant_id, key)

    async def delete_tenant(self, tenant_id: UUID) -> None:
        _validated_tenant_id(tenant_id)
        await asyncio.to_thread(self._validate_erasure_policy)
        await self._objects.delete_tenant(tenant_id)
        await asyncio.to_thread(self._validate_erasure_policy)

    def _bound_bucket(self) -> _MediaBucket:
        with self._lock:
            if self._bucket is None:
                self._bucket = cast("_MediaBucket", self._client.bucket(self._bucket_name))
            return self._bucket

    def _object_name(self, tenant_id: UUID, key: str) -> str:
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise ValueError("media key must be a content-addressed WebP identifier")
        name = f"{self._root_prefix}/{_validated_tenant_id(tenant_id)}/{key}"
        _validate_object_name_length(name)
        return name

    @staticmethod
    def _metadata(tenant_id: UUID, key: str) -> dict[str, str]:
        return {"media_schema": "1", "tenant_id": str(tenant_id), "sha256": key[:-5]}

    def _put_sync(self, tenant_id: UUID, key: str, data: bytes, content_type: str) -> None:
        name = self._object_name(tenant_id, key)
        if content_type != _CONTENT_TYPE:
            raise ValueError("media storage accepts only normalized image/webp")
        self._validate_bytes(key, data)
        blob = self._bound_bucket().blob(name)
        blob.metadata = self._metadata(tenant_id, key)
        blob.cache_control = _CACHE_CONTROL
        try:
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except Exception as error:
            if _http_status(error) != _HTTP_PRECONDITION_FAILED:
                raise
            # A repeated identical upload is valid only if the retained object still passes every
            # read check. A failed/conflicting write must never silently replace another object.
            if self._get_sync(tenant_id, key) != data:
                raise ValueError("media key is already bound to different bytes") from error

    def _get_sync(self, tenant_id: UUID, key: str) -> bytes:
        name = self._object_name(tenant_id, key)
        blob = self._bound_bucket().blob(name)
        try:
            blob.reload()
            if (
                blob.content_type != _CONTENT_TYPE
                or blob.content_encoding is not None
                or blob.cache_control != _CACHE_CONTROL
                or blob.metadata != self._metadata(tenant_id, key)
                or isinstance(blob.size, bool)
                or not isinstance(blob.size, int)
                or not 0 < blob.size <= _MAX_MEDIA_BYTES
            ):
                raise ValueError("stored media metadata is invalid")
            # Pin the generation observed above and bound even a corrupt provider response.
            data = blob.download_as_bytes(
                start=0,
                end=_MAX_MEDIA_BYTES,
                raw_download=True,
                if_generation_match=_validated_generation(blob.generation),
            )
        except Exception as error:
            if _http_status(error) == _HTTP_NOT_FOUND:
                raise MediaNotFoundError("media object is absent") from error
            raise
        self._validate_bytes(key, data)
        if len(data) != blob.size:
            raise ValueError("stored media size is inconsistent")
        return data

    def _delete_sync(self, tenant_id: UUID, key: str) -> None:
        name = self._object_name(tenant_id, key)
        self._validate_erasure_policy()
        blob = self._bound_bucket().blob(name)
        try:
            blob.reload()
            blob.delete(if_generation_match=_validated_generation(blob.generation))
        except Exception as error:
            if _http_status(error) != _HTTP_NOT_FOUND:
                raise
        self._validate_erasure_policy()

    def _validate_erasure_policy(self) -> None:
        bucket = self._bound_bucket()
        bucket.reload()
        if (
            bucket.soft_delete_policy.retention_duration_seconds not in (None, 0)
            or bucket.retention_period not in (None, 0)
            or bucket.default_event_based_hold not in (None, False)
            or bucket.versioning_enabled is not False
        ):
            raise RuntimeError(
                "media erasure is blocked by bucket retention, versioning or hold policy"
            )

    @staticmethod
    def _validate_bytes(key: str, data: bytes) -> None:
        if not isinstance(data, bytes) or not 0 < len(data) <= _MAX_MEDIA_BYTES:
            raise ValueError("media bytes must be nonempty and at most 32 KB")
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), key[:-5]):
            raise ValueError("media content digest does not match its key")
