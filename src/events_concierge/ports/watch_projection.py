"""Durable lifecycle-to-watch-registry projection contracts (FR-8.7a, ADR-008).

The guarded lifecycle transition writes an opaque record to this dedicated transactional outbox in
the same database transaction as its lifecycle/outbox ledger entry.  A worker later applies it to
the central watch registry.  Keeping this separate from the notification outbox preserves both
consumers' at-least-once semantics without allowing one consumer to acknowledge the other's work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ..domain.enums import Source


class WatchProjectionAction(StrEnum):
    """The only lifecycle resource projection operations (ADR-008)."""

    REGISTER = "register"
    UNREGISTER = "unregister"


@dataclass(frozen=True, slots=True)
class WatchProjectionRecord:
    """One leased lifecycle transition projection with no tenant payload beyond opaque IDs."""

    projection_id: int
    tenant_id: UUID
    workflow_id: str
    canonical_event_id: UUID
    source: Source
    action: WatchProjectionAction
    active_since: datetime
    attempt_count: int
    lease_token: str


class LifecycleWatchProjectionOutboxPort(Protocol):
    """Lease the distinct watch-projection outbox independently from notifications (ADR-008)."""

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[WatchProjectionRecord]:
        """Lease ready records globally; an expired lease is safely replayable."""
        ...

    async def has_live_lease(self, record: WatchProjectionRecord) -> bool:
        """Confirm the exact projection lease remains live before mutating the watch registry (NFR-8)."""
        ...

    async def mark_delivered(self, record: WatchProjectionRecord) -> bool:
        """Acknowledge one record only while the caller holds its lease."""
        ...

    async def reschedule(self, record: WatchProjectionRecord, *, error: str) -> bool:
        """Release a failed record with durable bounded backoff and no terminal drop."""
        ...
