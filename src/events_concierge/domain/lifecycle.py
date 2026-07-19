"""Per-(user, event) lifecycle aggregate and the handoff task. Transition legality mirrors the
guarded fn_transition (ADR-007): an illegal from-state is rejected, not silently applied."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from .enums import (
    HandoffCompletionOutcome,
    HandoffReason,
    HandoffReminderStatus,
    HandoffState,
    Lane,
    LifecycleState,
    Source,
)

# Legal state transitions (from -> allowed next). The registration workflow is the sole driver.
_LEGAL: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.FOUND: frozenset(
        {
            LifecycleState.AWAITING_CONFIRMATION,
            LifecycleState.REGISTERED,
            LifecycleState.HANDOFF,
            LifecycleState.FAILED_NO_CANDIDATE,
        }
    ),
    LifecycleState.HANDOFF: frozenset(
        {LifecycleState.REGISTERED, LifecycleState.EXPIRED, LifecycleState.CANCELLED}
    ),
    LifecycleState.AWAITING_CONFIRMATION: frozenset(
        {LifecycleState.REGISTERED, LifecycleState.HANDOFF, LifecycleState.CANCELLED}
    ),
    LifecycleState.REGISTERED: frozenset(
        {
            LifecycleState.SCHEDULED,
            LifecycleState.RECONCILED,
            LifecycleState.WITHDRAWING,
            LifecycleState.CANCELLED,
            LifecycleState.EXPIRED,
        }
    ),
    LifecycleState.SCHEDULED: frozenset(
        {
            LifecycleState.RECONCILED,
            LifecycleState.WITHDRAWING,
            LifecycleState.CANCELLED,
            LifecycleState.COMPLETED,
        }
    ),
    LifecycleState.RECONCILED: frozenset(
        {
            LifecycleState.SCHEDULED,
            LifecycleState.RECONCILED,
            LifecycleState.WITHDRAWING,
            LifecycleState.CANCELLED,
            LifecycleState.COMPLETED,
        }
    ),
    LifecycleState.WITHDRAWING: frozenset({LifecycleState.CANCELLED, LifecycleState.EXPIRED}),
}


class IllegalTransitionError(RuntimeError):
    """Raised when a transition is not permitted from the current state."""


def handoff_expiry_transition_id(workflow_id: str, task_id: str) -> str:
    """Derive a stable expiry transition identity from one workflow-minted handoff task.

    The expiry worker may retry after losing its lease.  Binding its guarded terminal transition to
    the task identity therefore makes retries converge at ``transition_ledger`` rather than minting
    a second terminal effect (FR-6.6, ADR-007).
    """
    if not workflow_id.strip() or not task_id.strip():
        raise ValueError("handoff expiry identity requires non-empty workflow_id and task_id")
    return f"{workflow_id}:handoff-expiry:{task_id}"


@dataclass(slots=True)
class Lifecycle:
    lifecycle_id: UUID
    tenant_id: UUID
    canonical_event_id: UUID
    workflow_id: str
    state: LifecycleState = LifecycleState.FOUND
    lane: Lane | None = None
    registration_source: Source | None = None
    conflict_warning: bool = False

    def can_transition(self, to: LifecycleState) -> bool:
        return to in _LEGAL.get(self.state, frozenset())

    def transition(self, to: LifecycleState) -> None:
        if not self.can_transition(to):
            raise IllegalTransitionError(f"{self.state} -> {to} is not a legal transition")
        self.state = to


@dataclass(slots=True)
class HandoffTask:
    task_id: str  # ULID
    tenant_id: UUID
    workflow_id: str
    canonical_event_id: UUID
    reason: HandoffReason
    deep_link: str
    event_summary: str
    ttl_expires_at: datetime
    state: HandoffState = HandoffState.OPEN
    metadata: dict[str, str] = field(default_factory=dict)
    # Existing callers mint only the task id.  The repository resolves this deterministic fallback
    # before persistence; workflows may pass an explicit id once the expiry activity is wired.
    expiry_transition_id: str | None = None
    # PostgreSQL owns the task's durable creation instant. The workflow must schedule reminder
    # timers from this persisted value, never an activity-local wall clock (ADR-007, NFR-8).
    created_at: datetime | None = None
    # A newly-created user registration handoff carries a high-entropy, single-use capability.
    # Only its digest is persisted on ``handoff_tasks``; the plaintext exists long enough to be
    # placed in the notification projection and is deliberately hidden from dataclass reprs.
    completion_token: str | None = field(default=None, repr=False)

    def resolved_expiry_transition_id(self) -> str:
        """Return the once-stable terminal transition id for this task's TTL worker.

        An explicit id is retained verbatim so a durable workflow can persist it in its state.  The
        fallback is deterministic from the already workflow-minted task id, preserving retry safety
        for legacy task constructors (FR-6.6, FR-8.3, ADR-007).
        """
        if self.expiry_transition_id is not None:
            if not self.expiry_transition_id.strip():
                raise ValueError("handoff expiry_transition_id must not be empty")
            return self.expiry_transition_id
        return handoff_expiry_transition_id(self.workflow_id, self.task_id)


@dataclass(frozen=True, slots=True)
class HandoffReminderResult:
    """One task-scoped reminder enqueue result from the guarded persistence boundary.

    A reminder announces an already-existing task rather than changing lifecycle state. The result
    therefore distinguishes a durable replay from an inactive or not-yet-due task while retaining
    the same at-least-once activity semantics as lifecycle transitions (FR-6.6, ADR-007/009).
    """

    status: HandoffReminderStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HandoffCompletionTarget:
    """Opaque capability resolution result returned by the privileged ingress boundary.

    The API never returns these tenant/workflow facts to the capability holder. They exist only so
    it can signal the exact retained workflow rather than reconstructing or accepting an identity
    from caller input (FR-6.3, FR-8.8, FR-16).
    """

    task_id: str
    tenant_id: UUID
    workflow_id: str
    canonical_event_id: UUID
    status: str


@dataclass(frozen=True, slots=True)
class HandoffCompletionReceipt:
    """RLS-scoped replay fact for one already-consumed completion command."""

    completion_id: str
    outcome: HandoffCompletionOutcome
    detail: str
    registration_source: Source | None
    conflict_warning: bool | None
