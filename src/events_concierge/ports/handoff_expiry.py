"""Opaque durable handoff-TTL repair queue contract (FR-6.6, ADR-007).

The Temporal child remains the authoritative timer owner.  This queue starts only after its
five-minute grace period, so a sweeper can recover an abandoned execution without exposing task
contents or other tenant data to a cross-tenant worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class HandoffExpiryRecord:
    """One globally leased, opaque handoff-expiry instruction (FR-6.6, ADR-007)."""

    task_id: str
    tenant_id: UUID
    workflow_id: str
    canonical_event_id: UUID
    expiry_transition_id: str
    ttl_expires_at: datetime
    attempt_count: int
    lease_token: str


class HandoffExpiryRepairOutcome(StrEnum):
    """One atomic orphan-repair result, distinct from Temporal's ordinary TTL activity (ADR-007)."""

    EXPIRED = "expired"
    TERMINAL = "terminal"
    IGNORED = "ignored"
    LEASE_LOST = "lease_lost"


class HandoffExpiryPort(Protocol):
    """Lease the post-grace handoff-expiry safety net without becoming lifecycle truth."""

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[HandoffExpiryRecord]:
        """Lease TTL records only after their fixed five-minute orphan-recovery grace period."""
        ...

    async def expire_orphan(self, record: HandoffExpiryRecord) -> HandoffExpiryRepairOutcome:
        """Atomically terminalize only this still-live orphan-repair lease (FR-6.6, ADR-007)."""
        ...

    async def acknowledge(self, record: HandoffExpiryRecord) -> bool:
        """Resolve a record only while the caller still holds its opaque lease."""
        ...

    async def reschedule(
        self, record: HandoffExpiryRecord, *, retry_at: datetime, error: str
    ) -> bool:
        """Release one failed expiry repair for an explicit later retry; never drop it silently."""
        ...
