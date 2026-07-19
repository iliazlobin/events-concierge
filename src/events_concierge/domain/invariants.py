"""PII-free lifecycle integrity projections for ADR-007/ADR-008 monitoring."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LifecycleDatabaseInvariantSnapshot:
    """Counts of database-only lifecycle/watch/handoff invariant violations.

    The shape is intentionally aggregate-only: it is safe to emit to logs or a future alert sink
    without exposing a tenant, workflow, event, title, URL, credential, or source payload
    (ADR-007/008, NFR-10). Counts are independently diagnostic rather than mutually exclusive;
    pending projection counts provide context for a just-committed lifecycle transition, never a
    repair authorization.
    """

    invalid_lifecycle_workflow_identity_count: int = 0
    active_lifecycles_missing_watch: int = 0
    active_lifecycles_mismatched_watch: int = 0
    terminal_watch_subscriptions: int = 0
    orphan_watch_subscriptions: int = 0
    orphan_watch_registry: int = 0
    active_handoffs_missing_expiry_queue: int = 0
    orphan_active_handoff_tasks: int = 0
    inactive_handoff_expiry_queue: int = 0
    pending_watch_register_projections: int = 0
    pending_watch_unregister_projections: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.invalid_lifecycle_workflow_identity_count,
            self.active_lifecycles_missing_watch,
            self.active_lifecycles_mismatched_watch,
            self.terminal_watch_subscriptions,
            self.orphan_watch_subscriptions,
            self.orphan_watch_registry,
            self.active_handoffs_missing_expiry_queue,
            self.orphan_active_handoff_tasks,
            self.inactive_handoff_expiry_queue,
            self.pending_watch_register_projections,
            self.pending_watch_unregister_projections,
        ):
            if value < 0:
                raise ValueError("lifecycle invariant counts must be non-negative")

    @property
    def observed_violation_count(self) -> int:
        """Return raw lifecycle/watch/handoff violations before projection-backlog interpretation."""
        return (
            self.invalid_lifecycle_workflow_identity_count
            + self.active_lifecycles_missing_watch
            + self.active_lifecycles_mismatched_watch
            + self.terminal_watch_subscriptions
            + self.orphan_watch_subscriptions
            + self.orphan_watch_registry
            + self.active_handoffs_missing_expiry_queue
            + self.orphan_active_handoff_tasks
            + self.inactive_handoff_expiry_queue
        )


@dataclass(frozen=True, slots=True)
class LifecycleInvariantReport:
    """One read-only combined PostgreSQL/Temporal integrity scan (ADR-007)."""

    database: LifecycleDatabaseInvariantSnapshot
    scanned_nonterminal_workflows: int = 0
    open_nonterminal_workflows: int = 0
    closed_nonterminal_workflows: int = 0
    uninspectable_nonterminal_workflows: int = 0

    def __post_init__(self) -> None:
        values = (
            self.scanned_nonterminal_workflows,
            self.open_nonterminal_workflows,
            self.closed_nonterminal_workflows,
            self.uninspectable_nonterminal_workflows,
        )
        if any(value < 0 for value in values):
            raise ValueError("lifecycle liveness counts must be non-negative")
        if (
            self.open_nonterminal_workflows
            + self.closed_nonterminal_workflows
            + self.uninspectable_nonterminal_workflows
            != self.scanned_nonterminal_workflows
        ):
            raise ValueError("lifecycle liveness outcomes must account for every scanned workflow")

    @property
    def has_divergence(self) -> bool:
        """Whether an authoritative lifecycle/watch/handoff divergence was observed."""
        return (
            self.database.observed_violation_count > 0
            or self.closed_nonterminal_workflows > 0
            or self.uninspectable_nonterminal_workflows > 0
        )

    @property
    def has_pending_projection_backlog(self) -> bool:
        """Whether an undelivered register/unregister projection needs operational attention."""
        return (
            self.database.pending_watch_register_projections > 0
            or self.database.pending_watch_unregister_projections > 0
        )

    @property
    def requires_attention(self) -> bool:
        """Whether either divergence or durable projection backlog needs an operator-facing signal."""
        return self.has_divergence or self.has_pending_projection_backlog
