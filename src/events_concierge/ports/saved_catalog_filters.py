"""Named catalog filter selections a tenant saves and reuses (FR-1.2--FR-1.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

# A tenant that has saved this many selections is collecting, not filtering.
MAX_SAVED_CATALOG_FILTERS = 60


@dataclass(frozen=True, slots=True)
class SavedCatalogFilter:
    """One named filter selection.

    ``payload`` is the client's own filter vocabulary, kept opaque here on purpose: the catalog
    filter set is a UI contract that changes with the UI, and the server's job is to store it
    bounded and tenant-isolated rather than to re-model it.
    """

    saved_filter_id: UUID
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_used_at: datetime | None = None


class SavedCatalogFilterRepository(Protocol):
    """Read and write one tenant's saved filter selections under its RLS context."""

    async def list_filters(self, tenant_id: UUID) -> list[SavedCatalogFilter]:
        """Return every saved selection, most recently used first."""
        ...

    async def save_filter(
        self,
        tenant_id: UUID,
        *,
        name: str,
        payload: dict[str, Any],
        saved_filter_id: UUID | None = None,
    ) -> SavedCatalogFilter:
        """Create a selection, or replace the one identified by ``saved_filter_id``.

        Raises ``ValueError`` when the tenant is at its cap, when the name collides with another
        selection, or when the identified selection does not belong to the tenant, so the HTTP
        boundary can answer with a status rather than a silent no-op.
        """
        ...

    async def touch_filter(self, tenant_id: UUID, saved_filter_id: UUID) -> SavedCatalogFilter:
        """Record that a selection was applied, which is what recency ordering sorts on.

        Raises ``ValueError`` when the selection does not belong to the tenant.
        """
        ...

    async def delete_filter(self, tenant_id: UUID, saved_filter_id: UUID) -> bool:
        """Remove a selection. Returns False when the tenant has no such selection."""
        ...
