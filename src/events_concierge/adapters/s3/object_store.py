"""SDK-agnostic S3-compatible claim-check storage (FR-8.5, FR-10.5).

The adapter accepts an injected synchronous client so the core package does not own cloud
credentials or depend on one vendor SDK.  The client must implement conditional ``PutObject`` with
``If-None-Match: *``; silently falling back to an unconditional overwrite would violate the
claim-check driver's immutable, idempotent-write contract.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from typing import Protocol, TypeGuard, cast
from uuid import UUID

from ...ports.object_store import ObjectStoreNotFoundError

_SAFE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_MAX_BUCKET_LENGTH = 255
_MAX_OBJECT_KEY_BYTES = 1024
_DELETE_BATCH_SIZE = 1000
_MAX_TENANT_PURGE_PASSES = 8
_CONTROL_CHARACTER_CUTOFF = 32
_HTTP_NOT_FOUND = 404
_NOT_FOUND_CODES = frozenset({"404", "nosuchkey", "notfound", "nosuchobject"})
_CONDITIONAL_CONFLICT_CODES = frozenset(
    {"409", "412", "conditionalrequestconflict", "preconditionfailed"}
)


class S3ObjectClient(Protocol):
    """Minimal synchronous client surface used by :class:`S3CompatibleObjectStore`.

    Boto3 and most S3-compatible SDK clients expose these keyword-oriented operations.  Deployment
    code owns client construction, credentials, endpoint selection, retries, and TLS policy.
    """

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        """Conditionally create one object."""
        ...

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        """Return a mapping containing the object's ``Body``."""
        ...

    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        """List one page below a prefix."""
        ...

    def list_object_versions(self, **kwargs: object) -> Mapping[str, object]:
        """List one page of versions and delete markers below a prefix."""
        ...

    def delete_objects(self, **kwargs: object) -> Mapping[str, object]:
        """Delete up to 1,000 named objects."""
        ...


class _ReadableBody(Protocol):
    def read(self) -> bytes | bytearray | memoryview:
        """Read the complete response body."""
        ...


class S3CompatibleObjectStore:
    """Tenant-prefixed immutable byte storage backed by an S3-compatible API."""

    def __init__(
        self,
        client: S3ObjectClient,
        *,
        bucket: str,
        root_prefix: str = "events-concierge/claim-check/v1",
    ) -> None:
        self._client = client
        self._bucket = _validated_bucket(bucket)
        self._root_prefix = _validated_path(root_prefix, label="root prefix")

    async def put(self, tenant_id: UUID, key: str, data: bytes) -> None:
        """Create an immutable object, accepting only byte-identical conditional replays."""
        await asyncio.to_thread(self._put_sync, tenant_id, key, data)

    async def get(self, tenant_id: UUID, key: str) -> bytes:
        """Return one tenant-scoped object's bytes and map ordinary absence to the port error."""
        return await asyncio.to_thread(self._get_sync, tenant_id, key)

    async def delete_tenant(self, tenant_id: UUID) -> None:
        """Purge every object below exactly one tenant prefix, in bounded delete batches."""
        await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    def _put_sync(self, tenant_id: UUID, key: str, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("claim-check object data must be bytes")
        object_key = self._object_key(tenant_id, key)
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=data,
                IfNoneMatch="*",
            )
        except Exception as error:
            if not _is_conditional_conflict(error):
                raise
            try:
                existing = self._get_object_sync(object_key)
            except ObjectStoreNotFoundError as not_found_error:
                # Preserve the conditional-write failure if a concurrent erasure removed the
                # object before the replay verification read.
                raise error from not_found_error
            if existing != data:
                raise ValueError(
                    "claim-check object key is already bound to different bytes"
                ) from error

    def _get_sync(self, tenant_id: UUID, key: str) -> bytes:
        return self._get_object_sync(self._object_key(tenant_id, key))

    def _get_object_sync(self, object_key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=object_key)
        except Exception as error:
            if _is_not_found(error):
                raise ObjectStoreNotFoundError("claim-check object is not available") from error
            raise
        body = response.get("Body")
        if isinstance(body, (bytes, bytearray, memoryview)):
            return bytes(body)
        reader = cast("_ReadableBody", body)
        if body is None or not callable(getattr(reader, "read", None)):
            raise TypeError("S3 get_object response must contain a readable Body")
        try:
            value = reader.read()
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("S3 object Body.read() must return bytes")
        return bytes(value)

    def _delete_tenant_sync(self, tenant_id: UUID) -> None:
        tenant_prefix = self._tenant_prefix(tenant_id)
        for _ in range(_MAX_TENANT_PURGE_PASSES):
            identifiers = self._tenant_purge_snapshot(tenant_prefix)
            if not identifiers:
                # A second complete observation closes the common race where a versioned write
                # (and possibly its delete marker) lands between the version and live-key scans.
                identifiers = self._tenant_purge_snapshot(tenant_prefix)
                if not identifiers:
                    return
            self._delete_identifiers(identifiers)

        # Final double-read verification distinguishes a purge that converged on its last allowed
        # mutation pass from a prefix being continually repopulated.  Returning before these
        # observations would falsely report success while concurrent writes remain.
        if self._tenant_purge_snapshot(tenant_prefix) or self._tenant_purge_snapshot(
            tenant_prefix
        ):
            raise RuntimeError("S3 tenant-prefix purge did not converge to an empty prefix")

    def _tenant_purge_snapshot(self, tenant_prefix: str) -> list[dict[str, str]]:
        version_identifiers = self._list_tenant_version_identifiers(tenant_prefix)
        versioned_keys = {identifier["Key"] for identifier in version_identifiers}
        live_keys = self._list_tenant_keys(tenant_prefix)
        # An unversioned S3-compatible implementation can return live keys without exposing a
        # VersionId.  A key-only deletion is safe only for keys absent from the version snapshot:
        # key-only deletion of a versioned object would create another delete marker.
        unversioned_identifiers = [
            {"Key": key} for key in live_keys if key not in versioned_keys
        ]
        return _unique_identifiers([*version_identifiers, *unversioned_identifiers])

    def _delete_identifiers(self, identifiers: Sequence[dict[str, str]]) -> None:
        for offset in range(0, len(identifiers), _DELETE_BATCH_SIZE):
            batch = identifiers[offset : offset + _DELETE_BATCH_SIZE]
            response = self._client.delete_objects(
                Bucket=self._bucket,
                Delete={
                    "Objects": batch,
                    "Quiet": True,
                },
            )
            errors = response.get("Errors", ())
            if not _is_empty_sequence(errors):
                raise RuntimeError("S3 tenant-prefix purge reported object deletion failures")

    def _list_tenant_version_identifiers(self, tenant_prefix: str) -> list[dict[str, str]]:
        identifiers: list[dict[str, str]] = []
        key_marker: str | None = None
        version_id_marker: str | None = None
        seen_markers: set[tuple[str, str]] = set()
        while True:
            arguments: dict[str, object] = {
                "Bucket": self._bucket,
                "Prefix": tenant_prefix,
                "MaxKeys": _DELETE_BATCH_SIZE,
            }
            if key_marker is not None:
                arguments["KeyMarker"] = key_marker
                arguments["VersionIdMarker"] = version_id_marker
            response = self._client.list_object_versions(**arguments)
            for field in ("Versions", "DeleteMarkers"):
                entries = response.get(field, ())
                if not _is_sequence(entries):
                    raise TypeError(f"S3 list_object_versions {field} must be a sequence")
                for item in entries:
                    if not isinstance(item, Mapping):
                        raise TypeError(
                            f"S3 list_object_versions {field} entries must be mappings"
                        )
                    listed_key = _validated_listed_key(item.get("Key"), tenant_prefix)
                    version_id = item.get("VersionId")
                    if not isinstance(version_id, str) or not version_id:
                        raise ValueError(
                            "S3 tenant-prefix version listing returned an invalid VersionId"
                        )
                    identifiers.append({"Key": listed_key, "VersionId": version_id})

            is_truncated = response.get("IsTruncated")
            if not isinstance(is_truncated, bool):
                raise TypeError("S3 list_object_versions IsTruncated must be a boolean")
            if not is_truncated:
                return _unique_identifiers(identifiers)

            next_key_marker = _validated_listed_key(
                response.get("NextKeyMarker"),
                tenant_prefix,
                label="pagination marker",
            )
            next_version_id_marker = response.get("NextVersionIdMarker")
            if (
                not isinstance(next_version_id_marker, str)
                or not next_version_id_marker
            ):
                raise RuntimeError(
                    "S3 tenant-prefix version listing returned an invalid pagination marker"
                )
            next_markers = (next_key_marker, next_version_id_marker)
            if next_markers in seen_markers:
                raise RuntimeError(
                    "S3 tenant-prefix version listing returned an invalid pagination marker"
                )
            seen_markers.add(next_markers)
            key_marker, version_id_marker = next_markers

    def _list_tenant_keys(self, tenant_prefix: str) -> list[str]:
        keys: list[str] = []
        continuation_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            arguments: dict[str, object] = {
                "Bucket": self._bucket,
                "Prefix": tenant_prefix,
                "MaxKeys": _DELETE_BATCH_SIZE,
            }
            if continuation_token is not None:
                arguments["ContinuationToken"] = continuation_token
            response = self._client.list_objects_v2(**arguments)
            contents = response.get("Contents", ())
            if not _is_sequence(contents):
                raise TypeError("S3 list_objects_v2 Contents must be a sequence")
            for item in contents:
                if not isinstance(item, Mapping):
                    raise TypeError("S3 list_objects_v2 content entries must be mappings")
                keys.append(_validated_listed_key(item.get("Key"), tenant_prefix))

            is_truncated = response.get("IsTruncated")
            if not isinstance(is_truncated, bool):
                raise TypeError("S3 list_objects_v2 IsTruncated must be a boolean")
            if not is_truncated:
                return list(dict.fromkeys(keys))
            next_token = response.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise RuntimeError("S3 tenant-prefix listing returned an invalid continuation")
            seen_tokens.add(next_token)
            continuation_token = next_token

    def _tenant_prefix(self, tenant_id: UUID) -> str:
        canonical_tenant_id = _validated_tenant_id(tenant_id)
        prefix = f"{self._root_prefix}/{canonical_tenant_id}/"
        _validate_object_key_length(prefix)
        return prefix

    def _object_key(self, tenant_id: UUID, key: str) -> str:
        relative_key = _validated_path(key, label="claim-check key")
        object_key = f"{self._tenant_prefix(tenant_id)}{relative_key}"
        _validate_object_key_length(object_key)
        return object_key


def _validated_bucket(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_BUCKET_LENGTH
        or any(
            character.isspace() or ord(character) < _CONTROL_CHARACTER_CUTOFF for character in value
        )
    ):
        raise ValueError("S3 bucket must be a non-empty bounded identifier")
    return value


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


def _validate_object_key_length(value: str) -> None:
    if len(value.encode("utf-8")) > _MAX_OBJECT_KEY_BYTES:
        raise ValueError("S3 object key exceeds the 1,024-byte limit")


def _validated_listed_key(
    value: object,
    tenant_prefix: str,
    *,
    label: str = "object",
) -> str:
    if not isinstance(value, str) or not value.startswith(tenant_prefix):
        raise ValueError(f"S3 tenant-prefix listing returned an out-of-scope {label}")
    _validate_object_key_length(value)
    return value


def _unique_identifiers(identifiers: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[tuple[str, str | None], dict[str, str]] = {}
    for identifier in identifiers:
        identity = (identifier["Key"], identifier.get("VersionId"))
        unique[identity] = identifier
    return list(unique.values())


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _is_empty_sequence(value: object) -> bool:
    return _is_sequence(value) and not value


def _is_not_found(error: Exception) -> bool:
    code, status = _error_code_and_status(error)
    return (code is not None and code.casefold() in _NOT_FOUND_CODES) or status == _HTTP_NOT_FOUND


def _is_conditional_conflict(error: Exception) -> bool:
    code, status = _error_code_and_status(error)
    return (code is not None and code.casefold() in _CONDITIONAL_CONFLICT_CODES) or status in {
        409,
        412,
    }


def _error_code_and_status(error: Exception) -> tuple[str | None, int | None]:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None, None
    error_payload = response.get("Error")
    code: str | None = None
    if isinstance(error_payload, Mapping):
        raw_code = error_payload.get("Code")
        if isinstance(raw_code, (str, int)):
            code = str(raw_code)
    metadata = response.get("ResponseMetadata")
    status: int | None = None
    if isinstance(metadata, Mapping):
        raw_status = metadata.get("HTTPStatusCode")
        if isinstance(raw_status, int):
            status = raw_status
    return code, status
