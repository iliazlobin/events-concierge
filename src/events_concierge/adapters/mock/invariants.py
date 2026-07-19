"""Deterministic count-only lifecycle-invariant repository for offline scanner tests."""

from __future__ import annotations

from ...domain.invariants import LifecycleDatabaseInvariantSnapshot


class MockLifecycleInvariantRepository:
    """Fixture repository with an ordered opaque workflow-ID keyset scan."""

    def __init__(
        self,
        snapshot: LifecycleDatabaseInvariantSnapshot | None = None,
        workflow_ids: tuple[str, ...] = (),
    ) -> None:
        self.snapshot = snapshot or LifecycleDatabaseInvariantSnapshot()
        self.workflow_ids = workflow_ids
        self.calls: list[tuple[str | None, int]] = []

    async def database_snapshot(self) -> LifecycleDatabaseInvariantSnapshot:
        """Return the configured aggregate-only database projection."""
        return self.snapshot

    async def nonterminal_workflow_ids(
        self, *, after_workflow_id: str | None, limit: int
    ) -> tuple[str, ...]:
        """Return the next configured opaque keyset page."""
        self.calls.append((after_workflow_id, limit))
        return tuple(
            workflow_id
            for workflow_id in self.workflow_ids
            if after_workflow_id is None or workflow_id > after_workflow_id
        )[:limit]
