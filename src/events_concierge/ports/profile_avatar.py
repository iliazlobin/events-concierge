"""Index over tenant avatar objects held in the media store."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProfileAvatar:
    """What the product knows about one stored avatar, without holding its bytes.

    Every field is an output of the server-side re-encode. None of it is caller-supplied, which is
    why ``content_type`` here -- not the request header -- is what the read path serves.
    """

    storage_key: str
    content_type: str
    byte_size: int
    width_px: int
    height_px: int
    checksum_sha256: str
    created_at: datetime | None = None


class ProfileAvatarRepository(Protocol):
    """Read, replace and remove one tenant's avatar index row."""

    async def get(self, tenant_id: UUID) -> ProfileAvatar | None:
        """Return the current avatar record, or ``None`` when the tenant has not set one."""
        ...

    async def replace(self, tenant_id: UUID, avatar: ProfileAvatar) -> ProfileAvatar:
        """Point the tenant at a new object, replacing any previous record."""
        ...

    async def delete(
        self, tenant_id: UUID, *, expected: ProfileAvatar | None = None
    ) -> ProfileAvatar | None:
        """Remove the record, optionally only if it still matches the already-purged version.

        ``None`` means no matching row was removed. Comparing key and creation time preserves a
        concurrent replacement, including one that reuses the same content-addressed object key.
        """
        ...


class ProfileMediaMutationBusyError(RuntimeError):
    """The bounded media mutation admission or distributed lock wait expired."""


class ProfileMediaMutationGuard(Protocol):
    """Serialize a tenant's complete media/index mutation across application processes."""

    async def run[T](self, tenant_id: UUID, mutation: Callable[[], Awaitable[T]]) -> T:
        """Retain exclusive media authority until every started mutation has settled."""
        ...
