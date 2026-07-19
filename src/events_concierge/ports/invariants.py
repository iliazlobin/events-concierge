"""Read-only lifecycle integrity scan port (ADR-007/ADR-008)."""

from __future__ import annotations

from typing import Protocol

from ..domain.invariants import LifecycleDatabaseInvariantSnapshot


class LifecycleInvariantRepository(Protocol):
    """Return aggregate database drift plus opaque workflow IDs for liveness inspection."""

    async def database_snapshot(self) -> LifecycleDatabaseInvariantSnapshot:
        """Return count-only watch/handoff invariants from the PostgreSQL source of truth."""
        ...

    async def nonterminal_workflow_ids(
        self, *, after_workflow_id: str | None, limit: int
    ) -> tuple[str, ...]:
        """Return one ordered opaque batch, never tenant/event or product content."""
        ...
