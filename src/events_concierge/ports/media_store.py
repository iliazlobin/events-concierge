"""Durable storage for tenant-owned media the product serves back to its owner.

This is deliberately a separate seam from ``ObjectStorePort``.  That port backs Temporal claim
checks: its objects are short-lived by design and its provisioned bucket carries an age-based
lifecycle rule that deletes them.  Profile media has the opposite lifetime -- it must survive until
its owner replaces or erases it -- so sharing the store would mean either avatars silently vanishing
or reworking a lifecycle rule that exists for a different tenant of the same bucket.

Local development uses a filesystem; hosted processes use a dedicated private GCS bucket without
expiry, versioning, soft delete or retention. Authenticated application routes serve the bytes; no
public or signed object URLs are issued. ``key`` is an opaque, server-generated, tenant-relative
identifier. Every implementation validates that identifier and enforces partitioning using the
separate ``tenant_id`` argument.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class MediaNotFoundError(LookupError):
    """The requested object is absent from the configured media store."""


class MediaStorePort(Protocol):
    """Persist and serve opaque media bytes under a tenant-scoped, opaque key."""

    async def put(self, tenant_id: UUID, key: str, data: bytes, content_type: str) -> None:
        """Persist bytes under one tenant-relative key, replacing any prior object at that key.

        ``content_type`` is recorded where the backend supports object metadata.  It is never the
        authority for what is served: the index row holds the type the re-encoder actually produced.
        """
        ...

    async def get(self, tenant_id: UUID, key: str) -> bytes:
        """Return exactly the bytes stored under the tenant-relative key.

        Raises ``MediaNotFoundError`` when the object is absent, which callers translate into a
        product-level 404 rather than an error that would reveal store topology.
        """
        ...

    async def delete(self, tenant_id: UUID, key: str) -> None:
        """Remove one object. Deleting an absent object succeeds, so retries converge."""
        ...

    async def delete_tenant(self, tenant_id: UUID) -> None:
        """Purge every object belonging to one tenant during account erasure (FR-10.5).

        The index rows disappear by cascade when the tenant row goes; this is the other half, and
        without it erasure would leave the bytes behind.
        """
        ...
