"""Opaque byte-object storage used by Temporal claim checks.

The workflow history stores only a claim reference.  Concrete stores keep the serialized Temporal
payload under a tenant-scoped key and expose an explicit tenant purge seam for FR-10.5 erasure.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class ObjectStorePort(Protocol):
    """Store opaque claim-check bytes without exposing their contents to workflow code.

    ``key`` is a validated, tenant-relative opaque identifier supplied by the claim-check driver;
    callers must not use it as a filesystem path or include untrusted page content in it
    (FR-8.5, FR-10.5, ADR-003/007/010).
    """

    async def put(self, tenant_id: UUID, key: str, data: bytes) -> None:
        """Idempotently persist bytes under one tenant-relative key."""
        ...

    async def get(self, tenant_id: UUID, key: str) -> bytes:
        """Return exactly the bytes previously stored under the tenant-relative key."""
        ...

    async def delete_tenant(self, tenant_id: UUID) -> None:
        """Purge all tenant-scoped claim-check bytes during account erasure (FR-10.5)."""
        ...


class ObjectStoreNotFoundError(LookupError):
    """A requested opaque claim is absent from the configured object store."""
