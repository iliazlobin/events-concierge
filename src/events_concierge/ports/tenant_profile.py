"""Tenant-authored display facts, kept apart from tenant identity (FR-1.2--FR-1.4, ADR-011)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TenantProfile:
    """Presentation-only facts a user authors about themselves.

    Deliberately carries no address, no RelayInbox, and no OIDC subject: those are identity
    bindings owned by ``public.tenants`` and are not editable through this surface.
    """

    display_name: str | None = None
    time_zone: str | None = None
    revision: int = 0
    updated_at: datetime | None = None


class TenantProfileRepository(Protocol):
    """Read and whole-value replace one tenant's display facts under its RLS context."""

    async def get_profile(self, tenant_id: UUID) -> TenantProfile:
        """Return the stored profile, or empty defaults when the tenant has never saved one."""
        ...

    async def replace_profile(
        self, tenant_id: UUID, profile: TenantProfile
    ) -> TenantProfile:
        """Apply a whole-value replacement carrying a strictly increasing revision.

        Raises ``ValueError`` when the proposed revision does not advance the stored one, so the
        HTTP boundary can report a conflict rather than silently discarding a concurrent edit.
        """
        ...
