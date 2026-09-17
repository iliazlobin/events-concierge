"""Filesystem-backed mock object storage shared by local Temporal client and worker processes.

This adapter deliberately stores opaque bytes only.  It is the offline ``mock_cloud`` stand-in for
the future provisioned object-store adapter, while preserving tenant prefixes and idempotent
content-addressed writes required by the Temporal claim-check contract (FR-8.5, ADR-010).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from uuid import UUID

from ...ports.object_store import ObjectStoreNotFoundError


class MockFilesystemObjectStore:
    """A tenant-isolated local object store with atomic write-if-absent semantics.

    A filesystem root is intentionally injected so separate API and worker processes can resolve
    the same claim.  Keys are strictly tenant-relative and cannot escape the configured root.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, tenant_id: UUID, key: str, data: bytes) -> None:
        """Atomically create one immutable object or verify an idempotent replay matches it."""
        await asyncio.to_thread(self._put_sync, tenant_id, key, data)

    async def get(self, tenant_id: UUID, key: str) -> bytes:
        """Read one tenant-scoped object without accepting a path outside its prefix."""
        return await asyncio.to_thread(self._get_sync, tenant_id, key)

    async def delete_tenant(self, tenant_id: UUID) -> None:
        """Remove only the tenant's prefix for the future account-erasure path (FR-10.5)."""
        await asyncio.to_thread(self._delete_tenant_sync, tenant_id)

    def _put_sync(self, tenant_id: UUID, key: str, data: bytes) -> None:
        path = self._object_path(tenant_id, key)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_directory(self._root)
        path.parent.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_directory(path.parent.parent)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_directory(path.parent)
        if path.exists():
            self._verify_existing(path, data)
            return

        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=".claim-check-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                # A hard link publishes exactly once without replacing another process's object.
                os.link(temporary_path, path)
            except FileExistsError:
                self._verify_existing(path, data)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _get_sync(self, tenant_id: UUID, key: str) -> bytes:
        path = self._object_path(tenant_id, key)
        if path.is_symlink():
            raise ValueError("claim-check object must not be a symbolic link")
        try:
            return path.read_bytes()
        except FileNotFoundError as error:
            raise ObjectStoreNotFoundError("claim-check object is not available") from error

    def _delete_tenant_sync(self, tenant_id: UUID) -> None:
        root = self._root.resolve(strict=False)
        tenant_root = root / str(tenant_id)
        if not tenant_root.exists():
            return
        if tenant_root.is_symlink() or not tenant_root.is_dir():
            raise ValueError("tenant claim-check prefix is not a directory")
        resolved_tenant_root = tenant_root.resolve(strict=True)
        if not resolved_tenant_root.is_relative_to(root):
            raise ValueError("tenant claim-check prefix escapes the configured root")
        shutil.rmtree(resolved_tenant_root)

    def _object_path(self, tenant_id: UUID, key: str) -> Path:
        relative = self._validated_relative_key(key)
        root = self._root.resolve(strict=False)
        tenant_root = root / str(tenant_id)
        path = tenant_root.joinpath(*relative.parts)
        resolved_path = path.resolve(strict=False)
        resolved_tenant_root = tenant_root.resolve(strict=False)
        if not resolved_path.is_relative_to(resolved_tenant_root):
            raise ValueError("claim-check key escapes the tenant prefix")
        if not resolved_tenant_root.is_relative_to(root):
            raise ValueError("tenant claim-check prefix escapes the configured root")
        return path

    @staticmethod
    def _validated_relative_key(key: str) -> PurePosixPath:
        path = PurePosixPath(key)
        if (
            not key
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
            or any("\\" in part for part in path.parts)
        ):
            raise ValueError("claim-check key must be a safe tenant-relative path")
        return path

    @staticmethod
    def _secure_directory(path: Path) -> None:
        """Keep locally mocked raw payloads private to the current account."""
        path.chmod(0o700)

    @staticmethod
    def _verify_existing(path: Path, expected: bytes) -> None:
        if path.is_symlink() or path.read_bytes() != expected:
            raise ValueError("claim-check object key is already bound to different bytes")
