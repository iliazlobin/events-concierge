"""JSON-native DTOs crossing the workflow/activity boundary (the Temporal default data converter
serializes these dataclasses directly; UUIDs and datetimes travel as strings)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RequestInput:
    """Opaque parent-workflow identity; request text stays in the RLS-protected store (ADR-011)."""

    tenant_id: str
    request_id: str
    attempt_budget: int = 3


@dataclass
class CatalogRefreshInput:
    """Opaque reviewed-source/run identity for a durable catalog continuation.

    The source document and normalized candidates remain in the activity/database boundary; the
    workflow history carries only owner-reviewed source identity and its stable refresh run key
    (FR-10.3, NFR-8, ADR-003).
    """

    source_key: str
    run_key: str


@dataclass
class CatalogRefreshActivityResult:
    """JSON-native projection of one catalog-refresh activity attempt (P15a/P15b/P15c/P15d/P15e)."""

    source_key: str
    run_key: str
    outcome: str
    candidate_count: int = 0
    canonical_count: int = 0
    detail: str = ""
    retry_after_seconds: float | None = None


@dataclass
class DiscoverResult:
    candidate_ids: list[str] = field(default_factory=list)


@dataclass
class RegisterWorkflowTargetsInput:
    """Complete deterministic child set persisted before any start-child command is emitted."""

    tenant_id: str
    workflow_ids: list[str] = field(default_factory=list)


@dataclass
class RegChildInput:
    tenant_id: str
    canonical_event_id: str
    # Parent-owned children retain the post-booking lifecycle; direct focused child tests do not.
    keep_open_after_scheduling: bool = False


@dataclass
class RegistrationSagaKeys:
    """Keys minted once in child-workflow state and passed to retried activities (FR-8.3/ADR-005)."""

    workflow_id: str
    run_id: str
    membership_queue_item_id: str
    source_by_lane: dict[str, str]
    registration_read_by_lane: dict[str, str]
    awaiting_transition_id: str
    confirmation_read_queue_item_id: str
    registered_transition_id: str
    scheduled_transition_id: str
    completed_transition_id: str
    close_candidate_transition_id: str
    handoff_task_id: str
    handoff_transition_id: str
    handoff_expiry_transition_id: str
    calendar_recovery_task_id: str
    calendar_recovery_expiry_transition_id: str


@dataclass
class ResolveMembershipInput:
    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    pacer_queue_item_id: str


@dataclass
class ResolveMembershipResult:
    status: str
    lane_plan: list[str] = field(default_factory=list)
    lane: str | None = None
    detail: str = ""
    pacing_status: str | None = None
    retry_after_seconds: float | None = None


@dataclass
class PolicyGateInput:
    tenant_id: str
    canonical_event_id: str
    lane: str
    workflow_id: str
    source_idempotency_key: str


@dataclass
class PolicyGateResult:
    allowed: bool
    detail: str = ""
    source_quarantined: bool = False


@dataclass
class RegisterOrRsvpInput:
    tenant_id: str
    canonical_event_id: str
    lane: str
    workflow_id: str
    source_idempotency_key: str
    registration_read_queue_item_id: str


@dataclass
class RegisterOrRsvpResult:
    outcome: str
    detail: str = ""
    pacing_status: str | None = None
    retry_after_seconds: float | None = None


@dataclass
class AwaitConfirmationInput:
    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    lane: str
    source_outcome: str
    awaiting_transition_id: str
    registered_transition_id: str
    confirmation_read_queue_item_id: str
    confirmation_reference: str | None = None
    # The existing activity name also owns handoff completion verification so deployed workers do
    # not need a second registration. These fields are all-or-none and carry only opaque IDs.
    handoff_task_id: str | None = None
    handoff_completion_id: str | None = None


@dataclass
class AwaitConfirmationResult:
    status: str
    detail: str = ""
    pacing_status: str | None = None
    retry_after_seconds: float | None = None
    conflict_warning: bool = False


@dataclass
class DedupeCalendarInput:
    tenant_id: str
    canonical_event_id: str
    calendar_event_id: str


@dataclass
class DedupeCalendarResult:
    calendar_event_id: str


@dataclass
class WriteToCalendarInput:
    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    calendar_event_id: str
    scheduled_transition_id: str


@dataclass
class WriteToCalendarResult:
    """The durable post-write calendar identity and lifecycle-completion deadline (ADR-007)."""

    calendar_event_id: str
    # ISO-8601, timezone-aware event-end-plus-24-hours deadline computed from the written entry.
    completion_deadline_at: str


@dataclass
class HandoffInput:
    """A replay-safe handoff command with an explicit, closed reason (ADR-003/005)."""

    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    handoff_task_id: str
    handoff_transition_id: str
    handoff_expiry_transition_id: str
    detail: str = ""
    # Keep historic/default handoffs compatible while a P1d fair-share degradation selects
    # ``saturation`` explicitly.  A string keeps the Temporal payload JSON-native.
    reason: str = "deferred_register"


@dataclass
class RegChildResult:
    status: str
    lane: str | None = None
    calendar_event_id: str | None = None
    handoff_task_id: str | None = None
    handoff_expires_at: str | None = None
    handoff_expiry_transition_id: str | None = None
    # PostgreSQL owns this durable instant. Retained children schedule reminder timers from it
    # instead of an activity-local wall clock (FR-6.6, ADR-007).
    handoff_created_at: str | None = None
    detail: str | None = None


@dataclass
class CloseFailedCandidateInput:
    """Once-keyed cleanup command for a declined child candidate (ADR-003/007)."""

    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    transition_id: str
    reason: str


@dataclass
class CloseFailedCandidateResult:
    """JSON-native result of terminalizing a child before parent fall-through."""

    status: str
    terminal_state: str | None = None
    detail: str = ""


@dataclass
class ChildOutcome:
    """A child-to-parent outcome signal used by ADR-003's directive protocol.

    A child reports ``failed`` before it closes so the request-level parent can choose either
    candidate fall-through (``close``) or terminal handoff (``demote_to_handoff``) without
    creating a second workflow for the same event.
    """

    canonical_event_id: str
    status: str
    lane: str | None = None
    detail: str = ""
    handoff_eligible: bool = False


@dataclass
class FinalizeNoCandidateInput:
    """Once-keyed request-level terminal command when no candidate can be retained (FR-5.0)."""

    tenant_id: str
    request_id: str
    transition_id: str


@dataclass
class FinalizeNoCandidateResult:
    """Serializable request no-result outcome for the parent workflow."""

    status: str


@dataclass
class LinkRequestOutcomeInput:
    """Opaque identities for the parent's immutable selected-lifecycle projection."""

    tenant_id: str
    request_id: str
    canonical_event_id: str


@dataclass
class LinkRequestOutcomeResult:
    """Serializable convergence result for a first link or an exact activity replay."""

    status: str


@dataclass
class OrganizerChangeSignal:
    """A normalized, deduplicable organizer change delivered to one lifecycle workflow.

    ``fingerprint`` is the central detector's stable ``event_change`` identity.  The values remain
    JSON-native because Temporal persists them in workflow history; no raw source payload or user
    PII belongs here (FR-8.7/8.7a, ADR-008).
    """

    fingerprint: str
    canonical_event_id: str
    source: str
    event_status: str
    start_at: str | None = None
    end_at: str | None = None
    time_zone: str | None = None
    title: str | None = None
    venue_name: str | None = None


@dataclass
class ReconcileOrganizerChangeInput:
    """Workflow-minted, JSON-native organizer reconciliation command (FR-8.7, ADR-008)."""

    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    fingerprint: str
    source: str
    event_status: str
    transition_id: str
    start_at: str | None = None
    end_at: str | None = None
    time_zone: str | None = None
    title: str | None = None
    venue_name: str | None = None


@dataclass
class ReconcileOrganizerChangeResult:
    """Serializable organizer-change activity result for the long-lived child workflow."""

    status: str
    conflict_warning: bool = False
    detail: str = ""
    # A successful reschedule returns the new calendar-entry end plus ADR-007's 24-hour grace.
    completion_deadline_at: str | None = None


@dataclass
class CompleteLifecycleInput:
    """Once-keyed quiet-lifecycle terminalization command (ADR-007)."""

    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    completed_transition_id: str


@dataclass
class CompleteLifecycleResult:
    """Serializable completion result; the workflow decides when the durable timer is due."""

    status: str
    detail: str = ""


@dataclass
class ExpireHandoffInput:
    """Once-keyed handoff TTL terminalization owned by the child workflow (FR-6.6, ADR-007)."""

    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    task_id: str
    expiry_transition_id: str


@dataclass
class ExpireHandoffResult:
    """Serializable guarded expiry result; terminal state remains JSON-native for the workflow."""

    status: str
    detail: str = ""
    terminal_state: str | None = None


@dataclass
class HandoffReminderInput:
    """Once-keyed request to enqueue one due task reminder (FR-6.6, ADR-007/009)."""

    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    task_id: str
    expiry_transition_id: str
    reminder_kind: str
    reminder_id: str


@dataclass
class HandoffReminderActivityResult:
    """JSON-native guarded reminder result for a retained child workflow."""

    status: str
    detail: str = ""


@dataclass
class UnrsvpSignal:
    """A user-requested withdrawal command with a stable inbound request identity (FR-6.7/8.8)."""

    request_id: str


@dataclass
class HandoffCompletionSignal:
    """Capability-authenticated mark-done command delivered to one retained child workflow."""

    task_id: str
    completion_id: str
    evidence: str = "user_mark_done"


@dataclass
class UnrsvpInput:
    """Once-keyed user withdrawal command executed by the lifecycle-owning child (FR-8.8)."""

    tenant_id: str
    canonical_event_id: str
    workflow_id: str
    request_id: str
    withdrawing_transition_id: str
    cancelled_transition_id: str
    handoff_expiry_transition_id: str
    source_read_queue_item_id: str
    source_mutation_idempotency_key: str


@dataclass
class UnrsvpActivityResult:
    """Serializable result that lets the workflow own Pacer timers and retries (ADR-003/005)."""

    status: str
    detail: str = ""
    retry_after_seconds: float | None = None
    handoff_task_id: str | None = None
    handoff_expires_at: str | None = None
    handoff_expiry_transition_id: str | None = None
    handoff_created_at: str | None = None


@dataclass
class PendingLifecycleSignals:
    """Query-safe view of the durable post-booking signal queues.

    This exists both for the lifecycle worker's integration tests and for operational diagnosis. The
    eventual reconcile/withdraw activities consume these queued, already-deduplicated commands;
    they must not depend on an in-memory API request surviving (ADR-003/ADR-008).
    """

    organizer_changes: list[OrganizerChangeSignal] = field(default_factory=list)
    unrsvp_requests: list[UnrsvpSignal] = field(default_factory=list)
    handoff_completions: list[HandoffCompletionSignal] = field(default_factory=list)


@dataclass
class RequestResult:
    outcome: str
    attempts: int
    detail: str = ""
