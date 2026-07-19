"""Temporal activities for the journaled registration saga.

Activities are deliberately narrow: the deterministic child workflow mints every idempotency key,
then coordinates ``resolve_membership → policy_gate → register_or_rsvp → await_confirmation →
dedupe_calendar → write_to_calendar``. The container is injected at worker startup because activity
code, unlike workflow code, may call application services and adapters (FR-8.2/8.3, ADR-003).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from temporalio import activity

from ..application.reconciliation import OrganizerChange, completion_deadline
from ..application.registration import PacerDeferredError
from ..composition import Container
from ..domain.enums import (
    ConflictVerdict,
    EventStatus,
    HandoffReason,
    HandoffReminderKind,
    Lane,
    Source,
)
from ..domain.events import CanonicalEvent
from ..ports.sources import RegisterOutcome
from .dto import (
    AwaitConfirmationInput,
    AwaitConfirmationResult,
    CatalogRefreshActivityResult,
    CatalogRefreshInput,
    CloseFailedCandidateInput,
    CloseFailedCandidateResult,
    CompleteLifecycleInput,
    CompleteLifecycleResult,
    DedupeCalendarInput,
    DedupeCalendarResult,
    DiscoverResult,
    ExpireHandoffInput,
    ExpireHandoffResult,
    FinalizeNoCandidateInput,
    FinalizeNoCandidateResult,
    HandoffInput,
    HandoffReminderActivityResult,
    HandoffReminderInput,
    PolicyGateInput,
    PolicyGateResult,
    ReconcileOrganizerChangeInput,
    ReconcileOrganizerChangeResult,
    RegChildResult,
    RegisterOrRsvpInput,
    RegisterOrRsvpResult,
    RequestInput,
    ResolveMembershipInput,
    ResolveMembershipResult,
    UnrsvpActivityResult,
    UnrsvpInput,
    WriteToCalendarInput,
    WriteToCalendarResult,
)

_container: Container | None = None


def set_container(container: Container) -> None:
    """Bind the worker's dependency graph for the activity functions."""
    global _container
    _container = container


def _require() -> Container:
    if _container is None:
        raise RuntimeError("activity container not set; call set_container() at worker startup")
    return _container


async def _event(c: Container, canonical_event_id: str) -> CanonicalEvent | None:
    return await c.catalog.get(UUID(canonical_event_id))


@activity.defn
async def discover_and_rank(inp: RequestInput) -> DiscoverResult:
    """Read the RLS-scoped request then retrieve/rank; raw text never crosses workflow history.

    The parent carries only deterministic identifiers.  Re-reading the request at the activity
    boundary keeps user request text in PostgreSQL's tenant-scoped store instead of Temporal
    history, as required by ADR-011; catalog refresh remains outside request workflows (NFR-8).
    """
    c = _require()
    tenant_id = UUID(inp.tenant_id)
    request = await c.request_repo.get(tenant_id, UUID(inp.request_id))
    if request is None:
        # A malformed/orphaned opaque start cannot cause a cross-tenant read.  Let the parent
        # take its ordinary no-candidate terminal path, which is itself guarded and idempotent.
        return DiscoverResult()
    feed = await c.feed.build_feed(request, limit=inp.attempt_budget * 3)
    candidate_ids = [
        str(item.canonical_event.canonical_event_id)
        for item in feed.items
        if item.conflict_verdict is not ConflictVerdict.BLOCKED
    ]
    return DiscoverResult(candidate_ids=candidate_ids)


@activity.defn
async def refresh_catalog_single_get(inp: CatalogRefreshInput) -> CatalogRefreshActivityResult:
    """Run P15a's sole LibCal document GET behind the durable catalog-refresh ledger.

    A Pacer projection is returned to ``CatalogRefreshWorkflow`` rather than slept in this activity.
    The service rejects any multi-request source before a lease or source call, so a retry carries
    no unpersisted page cursor (FR-10.3/10.4, NFR-8, ADR-003/005).
    """
    c = _require()
    if not c.settings.uses_shared_pacer_redis:
        raise RuntimeError(
            "P15a catalog refresh requires a shared Redis Pacer before source egress"
        )
    result = await c.catalog_refresh.refresh(
        inp.source_key,
        inp.run_key,
        require_single_http_get=True,
    )
    return CatalogRefreshActivityResult(
        source_key=result.source_key,
        run_key=result.run_key,
        outcome=result.outcome.value,
        candidate_count=result.candidate_count,
        canonical_count=result.canonical_count,
        detail=result.detail,
        retry_after_seconds=result.retry_after_seconds,
    )


@activity.defn
async def refresh_catalog_paged_legistar(inp: CatalogRefreshInput) -> CatalogRefreshActivityResult:
    """Advance a P15b/P15c/P15d/P15e Legistar cursor by at most one Pacer-admitted HTTP GET.

    Each call reclaims a fresh database lease, then either promotes an already terminal stage,
    returns one durable Pacer delay, or stages exactly one page. The workflow keeps only opaque
    source/run identifiers; no source JSON or cursor payload enters Temporal history (FR-10.3/10.4,
    NFR-1/NFR-8, ADR-003/005).
    """
    c = _require()
    if not c.settings.uses_shared_pacer_redis:
        raise RuntimeError(
            "paged catalog refresh requires a shared Redis Pacer before source egress"
        )
    result = await c.catalog_paged_refresh.refresh_page(inp.source_key, inp.run_key)
    return CatalogRefreshActivityResult(
        source_key=result.source_key,
        run_key=result.run_key,
        outcome=result.outcome.value,
        candidate_count=result.candidate_count,
        canonical_count=result.canonical_count,
        detail=result.detail,
        retry_after_seconds=result.retry_after_seconds,
    )


@activity.defn
async def finalize_no_candidate(inp: FinalizeNoCandidateInput) -> FinalizeNoCandidateResult:
    """Persist the parent request's terminal no-result state and notification outbox (FR-5.0/6.6)."""
    c = _require()
    result = await c.request_terminal.finalize_no_candidate(
        UUID(inp.tenant_id), UUID(inp.request_id), inp.transition_id
    )
    return FinalizeNoCandidateResult(status=result.status.value)


@activity.defn
async def resolve_membership(inp: ResolveMembershipInput) -> ResolveMembershipResult:
    """Resolve the membership-aware lane plan and recover pre-existing lifecycle state (FR-5.2)."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        return ResolveMembershipResult(status="failed", detail="candidate not found")
    try:
        result = await c.registration.resolve_membership(
            UUID(inp.tenant_id),
            event,
            inp.workflow_id,
            c.feed.lane_plan(event),
            membership_queue_item_id=inp.pacer_queue_item_id,
        )
    except PacerDeferredError as deferred:
        return ResolveMembershipResult(
            status="pacing_wait",
            detail=deferred.lease.detail,
            pacing_status=deferred.lease.status.value,
            retry_after_seconds=deferred.lease.retry_after_seconds,
        )
    return ResolveMembershipResult(
        status=result.status,
        lane_plan=[lane.value for lane in result.lane_plan],
        lane=result.lane.value if result.lane is not None else None,
        detail=result.detail,
    )


@activity.defn
async def policy_gate(inp: PolicyGateInput) -> PolicyGateResult:
    """Authoritatively pre-screen an autonomous lane; the mutation repeats the data-plane guard."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        return PolicyGateResult(allowed=False, detail="candidate not found")
    result = await c.registration.policy_gate(
        UUID(inp.tenant_id),
        event,
        Lane(inp.lane),
        workflow_id=inp.workflow_id,
        source_idempotency_key=inp.source_idempotency_key,
    )
    return PolicyGateResult(
        allowed=result.allowed,
        detail=result.detail,
        source_quarantined=result.source_quarantined,
    )


@activity.defn
async def close_failed_candidate(
    inp: CloseFailedCandidateInput,
) -> CloseFailedCandidateResult:
    """Close a declined child through the guarded lifecycle terminal path (ADR-003/007)."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        return CloseFailedCandidateResult(status="ignored", detail="candidate not found")
    result = await c.registration.close_failed_candidate(
        UUID(inp.tenant_id),
        event,
        inp.workflow_id,
        transition_id=inp.transition_id,
        reason=inp.reason,
    )
    return CloseFailedCandidateResult(
        status=result.status.value,
        terminal_state=(result.terminal_state.value if result.terminal_state is not None else None),
        detail=result.detail,
    )


@activity.defn
async def register_or_rsvp(inp: RegisterOrRsvpInput) -> RegisterOrRsvpResult:
    """Read-before-mutate/detect-then-submit source step under Pacer leases (ADR-003/005)."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        return RegisterOrRsvpResult(RegisterOutcome.FAILED.value, "candidate not found")
    try:
        result = await c.registration.register_or_rsvp(
            UUID(inp.tenant_id),
            event,
            Lane(inp.lane),
            inp.source_idempotency_key,
            workflow_id=inp.workflow_id,
            registration_read_queue_item_id=inp.registration_read_queue_item_id,
        )
    except PacerDeferredError as deferred:
        return RegisterOrRsvpResult(
            outcome="pacing_wait",
            detail=deferred.lease.detail,
            pacing_status=deferred.lease.status.value,
            retry_after_seconds=deferred.lease.retry_after_seconds,
        )
    return RegisterOrRsvpResult(result.outcome.value, result.detail)


@activity.defn
async def await_confirmation(inp: AwaitConfirmationInput) -> AwaitConfirmationResult:
    """Persist a pending/confirmed state; the 24-hour wait is a workflow timer, not worker time."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        return AwaitConfirmationResult(status="failed", detail="candidate not found")
    try:
        result = await c.registration.await_confirmation(
            UUID(inp.tenant_id),
            event,
            inp.workflow_id,
            Lane(inp.lane),
            RegisterOutcome(inp.source_outcome),
            awaiting_transition_id=inp.awaiting_transition_id,
            registered_transition_id=inp.registered_transition_id,
            confirmation_read_queue_item_id=inp.confirmation_read_queue_item_id,
            confirmation_reference=inp.confirmation_reference,
        )
    except PacerDeferredError as deferred:
        return AwaitConfirmationResult(
            status="pacing_wait",
            detail=deferred.lease.detail,
            pacing_status=deferred.lease.status.value,
            retry_after_seconds=deferred.lease.retry_after_seconds,
        )
    return AwaitConfirmationResult(status=result.status.value, detail=result.detail)


@activity.defn
async def dedupe_calendar(inp: DedupeCalendarInput) -> DedupeCalendarResult:
    """Validate the workflow-minted deterministic calendar key before the idempotent upsert."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        raise ValueError("candidate not found")
    entry = c.registration.dedupe_calendar(UUID(inp.tenant_id), event)
    if entry.calendar_event_id != inp.calendar_event_id:
        raise ValueError("workflow calendar id does not match deterministic event id")
    return DedupeCalendarResult(calendar_event_id=entry.calendar_event_id)


@activity.defn
async def write_to_calendar(inp: WriteToCalendarInput) -> WriteToCalendarResult:
    """Run the legacy combined write/schedule command for histories that already contain it."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        raise ValueError("candidate not found")
    entry = c.registration.dedupe_calendar(UUID(inp.tenant_id), event)
    if entry.end_at is None:
        raise ValueError("calendar entry needs an end before its lifecycle can complete")
    calendar_event_id = await c.registration.write_to_calendar(
        UUID(inp.tenant_id),
        event,
        inp.workflow_id,
        inp.calendar_event_id,
        inp.scheduled_transition_id,
    )
    return WriteToCalendarResult(
        calendar_event_id=calendar_event_id,
        completion_deadline_at=completion_deadline(entry.end_at).isoformat(),
    )


@activity.defn
async def reconcile_organizer_change(
    inp: ReconcileOrganizerChangeInput,
) -> ReconcileOrganizerChangeResult:
    """Apply one journaled organizer cancellation/reschedule command (FR-8.7, ADR-008)."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        return ReconcileOrganizerChangeResult(status="ignored", detail="candidate not found")
    result = await c.reconciliation.reconcile_organizer_change(
        UUID(inp.tenant_id),
        event,
        inp.workflow_id,
        OrganizerChange(
            fingerprint=inp.fingerprint,
            source=Source(inp.source),
            event_status=EventStatus(inp.event_status),
            start_at=_parse_datetime(inp.start_at),
            end_at=_parse_datetime(inp.end_at),
            time_zone=inp.time_zone,
            title=inp.title,
            venue_name=inp.venue_name,
        ),
        transition_id=inp.transition_id,
    )
    return ReconcileOrganizerChangeResult(
        status=result.status.value,
        conflict_warning=result.conflict_warning,
        detail=result.detail,
        completion_deadline_at=(
            result.completion_deadline_at.isoformat()
            if result.completion_deadline_at is not None
            else None
        ),
    )


@activity.defn
async def complete_lifecycle(inp: CompleteLifecycleInput) -> CompleteLifecycleResult:
    """Commit the quiet lifecycle's guarded ``COMPLETED`` transition (ADR-007)."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        return CompleteLifecycleResult(status="ignored", detail="candidate not found")
    result = await c.reconciliation.complete_lifecycle(
        UUID(inp.tenant_id),
        event,
        inp.workflow_id,
        transition_id=inp.completed_transition_id,
    )
    return CompleteLifecycleResult(status=result.status.value, detail=result.detail)


@activity.defn
async def expire_handoff(inp: ExpireHandoffInput) -> ExpireHandoffResult:
    """Run the guarded task-TTL terminalization owned by the workflow (FR-6.6, ADR-007)."""
    c = _require()
    result = await c.reconciliation.expire_handoff(
        UUID(inp.tenant_id),
        UUID(inp.canonical_event_id),
        inp.workflow_id,
        task_id=inp.task_id,
        expiry_transition_id=inp.expiry_transition_id,
    )
    return ExpireHandoffResult(
        status=result.status.value,
        detail=result.detail,
        terminal_state=(result.terminal_state.value if result.terminal_state is not None else None),
    )


@activity.defn
async def enqueue_handoff_reminder(
    inp: HandoffReminderInput,
) -> HandoffReminderActivityResult:
    """Atomically queue one due handoff reminder through the guarded persistence boundary.

    The child workflow owns the durable timer and passes its once-minted reminder identity. The
    application/SQL layer remains the authority for task state, persisted cadence, and one outbox
    effect if this activity crosses its commit point before Temporal receives an acknowledgement
    (FR-6.6, FR-8.3/8.9, ADR-003/007/009).
    """
    c = _require()
    result = await c.handoff_reminders.enqueue(
        UUID(inp.tenant_id),
        inp.task_id,
        inp.workflow_id,
        UUID(inp.canonical_event_id),
        inp.expiry_transition_id,
        HandoffReminderKind(inp.reminder_kind),
        inp.reminder_id,
    )
    return HandoffReminderActivityResult(status=result.status.value, detail=result.detail)


@activity.defn
async def unrsvp(inp: UnrsvpInput) -> UnrsvpActivityResult:
    """Run one read-before-withdraw recovery sequence after the user signals intent (FR-8.8)."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        return UnrsvpActivityResult(status="ignored", detail="candidate not found")
    result = await c.reconciliation.request_unrsvp(
        UUID(inp.tenant_id),
        event,
        inp.workflow_id,
        inp.request_id,
        withdrawing_transition_id=inp.withdrawing_transition_id,
        cancelled_transition_id=inp.cancelled_transition_id,
        handoff_expiry_transition_id=inp.handoff_expiry_transition_id,
        source_read_queue_item_id=inp.source_read_queue_item_id,
        source_mutation_idempotency_key=inp.source_mutation_idempotency_key,
    )
    return UnrsvpActivityResult(
        status=result.status.value,
        detail=result.detail,
        retry_after_seconds=result.retry_after_seconds,
        handoff_task_id=result.handoff_task_id,
        handoff_expires_at=(
            result.handoff_expires_at.isoformat() if result.handoff_expires_at is not None else None
        ),
        handoff_expiry_transition_id=result.handoff_expiry_transition_id,
        handoff_created_at=(
            result.handoff_created_at.isoformat() if result.handoff_created_at is not None else None
        ),
    )


@activity.defn
async def route_to_handoff(inp: HandoffInput) -> RegChildResult:
    """Compensate a lane with its workflow-selected closed handoff reason (ADR-003/005)."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        raise ValueError("candidate not found")
    result = await c.registration.route_to_handoff(
        UUID(inp.tenant_id),
        event,
        inp.workflow_id,
        handoff_task_id=inp.handoff_task_id,
        handoff_transition_id=inp.handoff_transition_id,
        handoff_expiry_transition_id=inp.handoff_expiry_transition_id,
        reason=HandoffReason(inp.reason or HandoffReason.DEFERRED_REGISTER.value),
        detail=inp.detail,
    )
    return RegChildResult(
        status=result.status.value,
        lane=result.lane.value if result.lane is not None else None,
        calendar_event_id=result.calendar_event_id,
        handoff_task_id=result.handoff_task_id,
        handoff_expires_at=(
            result.handoff_expires_at.isoformat() if result.handoff_expires_at is not None else None
        ),
        handoff_expiry_transition_id=result.handoff_expiry_transition_id,
        handoff_created_at=(
            result.handoff_created_at.isoformat() if result.handoff_created_at is not None else None
        ),
        detail=result.detail,
    )


@activity.defn
async def compensate_calendar_write(inp: HandoffInput) -> RegChildResult:
    """Create a manual calendar-recovery task after exhausted idempotent write retries (ADR-007)."""
    c = _require()
    event = await _event(c, inp.canonical_event_id)
    if event is None:
        raise ValueError("candidate not found")
    result = await c.registration.compensate_calendar_write(
        UUID(inp.tenant_id),
        event,
        inp.workflow_id,
        handoff_task_id=inp.handoff_task_id,
        handoff_expiry_transition_id=inp.handoff_expiry_transition_id,
        detail=inp.detail,
    )
    return RegChildResult(
        status=result.status.value,
        lane=result.lane.value if result.lane is not None else None,
        calendar_event_id=result.calendar_event_id,
        handoff_task_id=result.handoff_task_id,
        handoff_expires_at=(
            result.handoff_expires_at.isoformat() if result.handoff_expires_at is not None else None
        ),
        handoff_expiry_transition_id=result.handoff_expiry_transition_id,
        handoff_created_at=(
            result.handoff_created_at.isoformat() if result.handoff_created_at is not None else None
        ),
        detail=result.detail,
    )


def _parse_datetime(value: str | None) -> datetime | None:
    """Decode the JSON-native Temporal timestamp used by organizer-change signals."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
