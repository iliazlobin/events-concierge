"""Offline tenant-erasure inventory double with opaque, deterministic calendar targets."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from ...ports.erasure import CalendarDeletionTarget


class MockTenantErasureInventory:
    """Process-local TenantErasureInventoryPort fixture store (FR-10.5, ADR-007)."""

    def __init__(self) -> None:
        self._targets: dict[UUID, tuple[CalendarDeletionTarget, ...]] = {}

    def seed_calendar_targets(self, tenant_id: UUID, canonical_event_ids: Iterable[UUID]) -> None:
        """Replace one tenant's fixture targets in stable UUID order without event content."""
        self._targets[tenant_id] = tuple(
            CalendarDeletionTarget(tenant_id, canonical_event_id)
            for canonical_event_id in sorted(set(canonical_event_ids), key=str)
        )

    async def list_calendar_deletion_targets(
        self, tenant_id: UUID
    ) -> tuple[CalendarDeletionTarget, ...]:
        """Return this tenant's opaque target tuple, or an idempotent empty inventory."""
        return self._targets.get(tenant_id, ())
