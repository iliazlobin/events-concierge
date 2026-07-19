"""In-memory CalendarPort mock (mock_cloud=True stand-in for the Google adapter).

Writes are idempotent upserts keyed by CalendarEntry.calendar_event_id (FR-9.2); free_busy reads
seedable per-tenant busy blocks for the mandatory pre-registration conflict gate (FR-4.5)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from ...domain.conflict import BusyBlock
from ...ports.calendar import CalendarEntry


class MockCalendar:
    """Process-local CalendarPort implementation. Per-tenant entry stores plus seedable busy blocks."""

    def __init__(self) -> None:
        self._entries: dict[UUID, dict[str, CalendarEntry]] = {}
        self._busy: dict[UUID, list[BusyBlock]] = {}
        self._raise_after_upsert_once = False
        self._raised_after_upsert = False
        self._fail_before_upsert_remaining = 0
        self.upsert_attempts = 0

    def seed_busy(self, tenant_id: UUID, blocks: list[BusyBlock]) -> None:
        """Seed the busy blocks free_busy will return for a tenant (test/slice fixture helper)."""
        self._busy[tenant_id] = list(blocks)

    def raise_after_upsert_once(self) -> None:
        """Simulate a lost activity ACK after the idempotent calendar effect (NFR-8 test seam)."""
        self._raise_after_upsert_once = True

    def fail_before_upsert(self, attempts: int) -> None:
        """Fail a bounded number of writes before any calendar effect for recovery testing."""
        if attempts < 1:
            raise ValueError("attempts must be positive")
        self._fail_before_upsert_remaining = attempts

    async def free_busy(
        self, tenant_id: UUID, window_start: datetime, window_end: datetime
    ) -> list[BusyBlock]:
        """Return seeded busy blocks overlapping the requested window (default empty, FR-4.5)."""
        return [
            block
            for block in self._busy.get(tenant_id, [])
            if block.overlaps(window_start, window_end)
        ]

    async def upsert_event(self, tenant_id: UUID, entry: CalendarEntry) -> None:
        """Idempotent upsert keyed by entry.calendar_event_id (FR-9.2): a repeat id overwrites."""
        self.upsert_attempts += 1
        if self._fail_before_upsert_remaining > 0:
            self._fail_before_upsert_remaining -= 1
            raise RuntimeError("simulated calendar write failure")
        self._entries.setdefault(tenant_id, {})[entry.calendar_event_id] = entry
        if self._raise_after_upsert_once and not self._raised_after_upsert:
            self._raised_after_upsert = True
            raise RuntimeError("simulated calendar acknowledgement loss")

    async def delete_event(
        self,
        tenant_id: UUID,
        calendar_event_id: str,
        *,
        canonical_event_id: UUID | None = None,
    ) -> None:
        """Remove a concierge-written entry; the deterministic mock key needs no remote canonical lookup."""
        del canonical_event_id
        self._entries.get(tenant_id, {}).pop(calendar_event_id, None)

    def entries(self, tenant_id: UUID) -> list[CalendarEntry]:
        """Expose the current entries for a tenant (test/slice inspection)."""
        return list(self._entries.get(tenant_id, {}).values())
