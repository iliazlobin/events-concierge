"""PostgreSQL reader for count-only lifecycle integrity monitoring (ADR-007/008)."""

from __future__ import annotations

from typing import Protocol, cast

from sqlalchemy import text

from ...domain.invariants import LifecycleDatabaseInvariantSnapshot
from ...infra.db import system_session_scope


class _DatabaseSnapshotRow(Protocol):
    invalid_lifecycle_workflow_identity_count: int
    active_lifecycles_missing_watch: int
    active_lifecycles_mismatched_watch: int
    terminal_watch_subscriptions: int
    orphan_watch_subscriptions: int
    orphan_watch_registry: int
    active_handoffs_missing_expiry_queue: int
    orphan_active_handoff_tasks: int
    inactive_handoff_expiry_queue: int
    pending_watch_register_projections: int
    pending_watch_unregister_projections: int


class _WorkflowIdRow(Protocol):
    workflow_id: str


class PostgresLifecycleInvariantRepository:
    """Invoke owner-defined global scans while exposing only safe aggregates to the application role."""

    async def database_snapshot(self) -> LifecycleDatabaseInvariantSnapshot:
        """Read the SECURITY DEFINER count projection; direct global lifecycle reads remain unavailable."""
        async with system_session_scope() as session:
            row = cast(
                _DatabaseSnapshotRow,
                (
                    await session.execute(
                        text("SELECT * FROM public.fn_lifecycle_invariant_snapshot()")
                    )
                ).one(),
            )
        return LifecycleDatabaseInvariantSnapshot(
            invalid_lifecycle_workflow_identity_count=int(
                row.invalid_lifecycle_workflow_identity_count
            ),
            active_lifecycles_missing_watch=int(row.active_lifecycles_missing_watch),
            active_lifecycles_mismatched_watch=int(row.active_lifecycles_mismatched_watch),
            terminal_watch_subscriptions=int(row.terminal_watch_subscriptions),
            orphan_watch_subscriptions=int(row.orphan_watch_subscriptions),
            orphan_watch_registry=int(row.orphan_watch_registry),
            active_handoffs_missing_expiry_queue=int(row.active_handoffs_missing_expiry_queue),
            orphan_active_handoff_tasks=int(row.orphan_active_handoff_tasks),
            inactive_handoff_expiry_queue=int(row.inactive_handoff_expiry_queue),
            pending_watch_register_projections=int(row.pending_watch_register_projections),
            pending_watch_unregister_projections=int(row.pending_watch_unregister_projections),
        )

    async def nonterminal_workflow_ids(
        self, *, after_workflow_id: str | None, limit: int
    ) -> tuple[str, ...]:
        """Page opaque nonterminal workflow identities for Temporal's read-only liveness check."""
        if limit < 1:
            raise ValueError("lifecycle invariant limit must be positive")
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """SELECT workflow_id
                           FROM public.fn_list_nonterminal_lifecycle_workflow_ids(
                               :after_workflow_id,
                               :limit
                           )"""
                    ),
                    {"after_workflow_id": after_workflow_id, "limit": limit},
                )
            ).all()
        return tuple(cast(_WorkflowIdRow, row).workflow_id for row in rows)
