"""Durable repair boundary for organizer-change signals that reach a closed workflow.

ADR-008 keeps the normal path inside the lifecycle-owning Temporal child.  A ``NOT_FOUND`` signal
is different from a transient engine failure: it is preserved for the 15-minute sweeper, which
re-enters the same idempotent reconciliation service only when the database still says that exact
lifecycle is active.  This prevents both infinite closed-workflow retries and a late reschedule
recreating an event a user already un-RSVPed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .change_detection import DetectedEventChange, OrganizerChangeDelivery


class ClosedWorkflowSignalError(RuntimeError):
    """A fanout target is closed or violates the deterministic lifecycle routing invariant."""


@dataclass(frozen=True, slots=True)
class CalendarRepairRecord:
    """One leased, delayed reconciliation safety-net record with an opaque workflow target."""

    repair_id: int
    change: DetectedEventChange
    tenant_id: UUID
    workflow_id: str
    attempt_count: int
    lease_token: str


class ClosedWorkflowCalendarRepairPort(Protocol):
    """Persist/lease the ADR-008 15-minute direct-reconciliation safety net."""

    async def enqueue(self, delivery: OrganizerChangeDelivery, *, error: str) -> bool:
        """Retire one exact delivery lease and atomically schedule its delayed repair once."""
        ...

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[CalendarRepairRecord]:
        """Lease repairs whose fixed 15-minute grace window has elapsed."""
        ...

    async def mark_repaired(self, record: CalendarRepairRecord) -> bool:
        """Acknowledge a successful/idempotently unnecessary repair under its exact lease."""
        ...

    async def reschedule(self, record: CalendarRepairRecord, *, error: str) -> bool:
        """Release a failed repair with bounded backoff; no repair is silently dropped."""
        ...

    async def organizer_change_applied(self, record: CalendarRepairRecord) -> bool:
        """Return whether this held repair lease's normal transition already committed its identity."""
        ...

    async def has_live_repair_lease(self, record: CalendarRepairRecord) -> bool:
        """Return whether this exact repair lease remains current before calendar reconciliation."""
        ...
