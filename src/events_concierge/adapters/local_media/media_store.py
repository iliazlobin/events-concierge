"""Filesystem-backed media store.

The first concrete ``MediaStorePort``.  It is a real implementation rather than a mock -- it is what
the product runs on until blob storage is provisioned -- so it takes the durability and isolation
details seriously: writes are atomic via rename, directories are private, and a key can never
address anything outside its tenant's prefix.

Two properties matter for the eventual swap to blob storage.  Objects are addressed only through the
port, so nothing above knows a path exists.  And keys are opaque server-generated strings validated
against a strict pattern here, which means the traversal defence lives in the one place that
actually touches a filesystem and does not need reimplementing per backend.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from uuid import UUID

from ...ports.media_store import MediaNotFoundError

# Keys are minted by the application from a content digest.  Anchored, no dots beyond the single
# extension, no separators: a value that matches this cannot escape its directory.
_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}\.[a-z0-9]{1,8}$")

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


class LocalFilesystemMediaStore:
    """Store tenant media under ``<root>/<tenant_id>/<key>``.

    The root is injected so the API process and any worker that purges media resolve the same
    location, and so a deployment can point it at a mounted volume without a code change.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, tenant_id: UUID, key: str, data: bytes, content_type: str) -> None:
        """Atomically place bytes at the tenant-relative key, replacing anything already there."""
        await asyncio.to_thread(self._put_sync, tenant_id, key, data)

    async def get(self, tenant_id: UUID, key: str) -> bytes:
        """Read one tenant-scoped object."""
        return await asyncio.to_thread(self._get_sync, tenant_id, key)

    async def delete(self, tenant_id: UUID, key: str) -> None:
        """Remove one object; absence is success so a retried erasure converges."""
        await asyncio.to_thread(self._delete_sync, tenant_id, key)

    async def delete_tenant(self, tenant_id: UUID) -> None:
        """Remove the tenant's whole prefix (FR-10.5)."""
        await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    # -- synchronous bodies, run off the event loop ---------------------------------------------

    def _put_sync(self, tenant_id: UUID, key: str, data: bytes) -> None:
        path = self._object_path(tenant_id, key)
        self._ensure_directory(path.parent)
        # Write to a sibling temporary file and rename, so a reader never observes a partial
        # object and a crash mid-write cannot leave a truncated avatar addressable.
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=".media-", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        try:
            with open(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
            temporary_path.chmod(_FILE_MODE)
            temporary_path.replace(path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def _get_sync(self, tenant_id: UUID, key: str) -> bytes:
        path = self._object_path(tenant_id, key)
        try:
            return path.read_bytes()
        except FileNotFoundError as error:
            raise MediaNotFoundError("media object is absent") from error

    def _delete_sync(self, tenant_id: UUID, key: str) -> None:
        self._object_path(tenant_id, key).unlink(missing_ok=True)

    def _delete_tenant_sync(self, tenant_id: UUID) -> None:
        shutil.rmtree(self._tenant_path(tenant_id), ignore_errors=True)

    def _ensure_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
        path.chmod(_DIRECTORY_MODE)

    def _tenant_path(self, tenant_id: UUID) -> Path:
        # ``UUID.hex`` cannot contain a separator, so the tenant segment is inherently safe.
        return self._root / tenant_id.hex

    def _object_path(self, tenant_id: UUID, key: str) -> Path:
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError("media key is not a valid opaque object identifier")
        return self._tenant_path(tenant_id) / key
