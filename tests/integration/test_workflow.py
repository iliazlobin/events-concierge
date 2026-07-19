"""Durable-spine integration tests for the granular ADR-003 Temporal registration saga.

The parent starts a per-event child, which coordinates the narrow registration activities on
Temporal's in-process test server. The tests cover the free-crawl handoff path and lost-ACK
recovery for the source and deterministic calendar effects.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from events_concierge.adapters.crawl.source import PublicJsonLdSource
from events_concierge.adapters.luma.scripted_browser import (
    ScriptedBrowserRsvpDriver,
    ScriptedBrowserScenario,
)
from events_concierge.adapters.luma.source import LumaSource
from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.policy import (
    MockPolicySnapshotReader,
    MockSourceQuarantineRepository,
)
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.engine import StoreBackedPolicyEngine
from events_concierge.adapters.postgres.audit import PostgresRegistrationActionAuditRepository
from events_concierge.composition import Container, build_container
from events_concierge.config import get_settings
from events_concierge.domain.audit import RegistrationActionAudit, RegistrationActionAuditPhase
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import (
    GroupCondition,
    HandoffReason,
    HandoffState,
    Lane,
    LifecycleState,
    Modality,
    RsvpState,
    Source,
)
from events_concierge.domain.events import CandidateEvent
from events_concierge.domain.ids import registration_workflow_id, request_workflow_id
from events_concierge.domain.policy import SourceQuarantineSignal
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.infra.db import system_session_scope, tenant_session_scope
from events_concierge.policies import default_source_policies
from events_concierge.ports.browser import BrowserRsvpObservation, BrowserRsvpStatus
from events_concierge.ports.browser_admission import (
    BrowserAdmissionLease,
    BrowserAdmissionRequest,
)
from events_concierge.ports.policy import (
    PacerLease,
    PacerLeaseStatus,
    PacerOperation,
    PacerRequest,
)
from events_concierge.ports.sources import (
    RegistrationTarget,
    SourceAccessDeniedError,
    SourceRateLimitedError,
)
from events_concierge.workflows.activities import (
    await_confirmation,
    close_failed_candidate,
    compensate_calendar_write,
    complete_lifecycle,
    dedupe_calendar,
    discover_and_rank,
    enqueue_handoff_reminder,
    expire_handoff,
    finalize_no_candidate,
    policy_gate,
    reconcile_organizer_change,
    register_or_rsvp,
    resolve_membership,
    route_to_handoff,
    set_container,
    unrsvp,
    write_to_calendar,
)
from events_concierge.workflows.dto import (
    AwaitConfirmationInput,
    AwaitConfirmationResult,
    CompleteLifecycleInput,
    CompleteLifecycleResult,
    DiscoverResult,
    ExpireHandoffInput,
    ExpireHandoffResult,
    HandoffCompletionSignal,
    HandoffInput,
    HandoffReminderActivityResult,
    HandoffReminderInput,
    OrganizerChangeSignal,
    PendingLifecycleSignals,
    ReconcileOrganizerChangeInput,
    ReconcileOrganizerChangeResult,
    RegChildInput,
    RegChildResult,
    RegisterOrRsvpInput,
    RegisterOrRsvpResult,
    RequestInput,
    ResolveMembershipInput,
    ResolveMembershipResult,
    UnrsvpSignal,
)
from events_concierge.workflows.workflows import EventRequestWorkflow, RegistrationWorkflow

pytestmark = pytest.mark.integration


class RecordingPacer:
    """Test double proving every source operation begins only after a Pacer lease (ADR-005)."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.keys: list[str] = []
        self.queue_items: list[tuple[PacerOperation, str | None]] = []

    async def acquire(self, request: PacerRequest) -> PacerLease:
        self.keys.append(request.bucket_key)
        self.queue_items.append((request.operation, request.queue_item_id))
        self.events.append("pacer")
        return PacerLease(PacerLeaseStatus.GRANTED)

    async def observe_backoff(
        self,
        request: PacerRequest,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        del request, retry_after_seconds, reset_at


class WaitThenGrantPacer(RecordingPacer):
    """A Pacer seam that proves a projected quota wait becomes a Temporal timer."""

    def __init__(self, events: list[str], *, wait_on_call: int, seconds: float) -> None:
        super().__init__(events)
        self._wait_on_call = wait_on_call
        self._seconds = seconds
        self.calls = 0
        self.waits = 0

    async def acquire(self, request: PacerRequest) -> PacerLease:
        self.calls += 1
        self.keys.append(request.bucket_key)
        self.queue_items.append((request.operation, request.queue_item_id))
        if self.calls == self._wait_on_call:
            self.waits += 1
            self.events.append("pacer:wait")
            return PacerLease(PacerLeaseStatus.WAIT, self._seconds, "fixture quota wait")
        self.events.append("pacer:grant")
        return PacerLease(PacerLeaseStatus.GRANTED)


class DegradeFirstMutationPacer(RecordingPacer):
    """Return P1d's projected-wait handoff result immediately before the first RSVP mutation."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.operations: list[PacerOperation] = []
        self.degrade_count = 0

    async def acquire(self, request: PacerRequest) -> PacerLease:
        self.keys.append(request.bucket_key)
        self.queue_items.append((request.operation, request.queue_item_id))
        self.operations.append(request.operation)
        if request.operation is PacerOperation.REGISTRATION_MUTATION and self.degrade_count == 0:
            self.degrade_count += 1
            self.events.append("pacer:degrade")
            return PacerLease(
                PacerLeaseStatus.DEGRADE,
                retry_after_seconds=301.0,
                detail="fixture projected fair-share wait",
            )
        self.events.append("pacer:grant")
        return PacerLease(PacerLeaseStatus.GRANTED)


class SaturatedBrowserAdmission:
    """Fixture browser-pool boundary that denies before any scripted Luma navigation (AC-45)."""

    def __init__(self) -> None:
        self.requests: list[BrowserAdmissionRequest] = []
        self.releases: list[BrowserAdmissionLease] = []

    async def acquire(self, request: BrowserAdmissionRequest) -> BrowserAdmissionLease:
        self.requests.append(request)
        return BrowserAdmissionLease(
            granted=False,
            retry_after_seconds=1.0,
            detail="fixture browser pool is full",
        )

    async def release(self, lease: BrowserAdmissionLease) -> None:
        self.releases.append(lease)


class RecordingBrowserAdmission:
    """In-memory port seam proving activities clean up every held browser slot (ADR-003/005)."""

    def __init__(self) -> None:
        self.requests: list[BrowserAdmissionRequest] = []
        self.releases: list[BrowserAdmissionLease] = []

    async def acquire(self, request: BrowserAdmissionRequest) -> BrowserAdmissionLease:
        self.requests.append(request)
        return BrowserAdmissionLease(
            granted=True,
            lease_id=request.lease_id,
            fence_token=request.fence_token,
        )

    async def release(self, lease: BrowserAdmissionLease) -> None:
        self.releases.append(lease)


class RateLimitFeedbackPacer(RecordingPacer):
    """Records normalized provider throttles and turns the next lease into a durable wait."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.backoffs: list[tuple[str, float | None]] = []
        self._wait_seconds: float | None = None

    async def acquire(self, request: PacerRequest) -> PacerLease:
        self.keys.append(request.bucket_key)
        self.queue_items.append((request.operation, request.queue_item_id))
        if self._wait_seconds is not None:
            seconds = self._wait_seconds
            self._wait_seconds = None
            self.events.append("pacer:backoff-wait")
            return PacerLease(PacerLeaseStatus.WAIT, seconds, "fixture provider throttle")
        self.events.append("pacer:grant")
        return PacerLease(PacerLeaseStatus.GRANTED)

    async def observe_backoff(
        self,
        request: PacerRequest,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        del reset_at
        self.backoffs.append((request.bucket_key, retry_after_seconds))
        self._wait_seconds = retry_after_seconds or 1.0


class RateLimitedReadOnceSource(ConfirmingSource):
    """Fixture adapter exposing a normalized 429 instead of hiding it in a generic HTTP error."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(Source.MEETUP, event_log=events)
        self._rate_limited = False

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        if not self._rate_limited:
            self._rate_limited = True
            if self._event_log is not None:
                self._event_log.append("source:rate_limited")
            raise SourceRateLimitedError("fixture 429", retry_after_seconds=5.0)
        return await super().read_registration_state(tenant_id, target, modality)


class AccessDeniedOnRegistrationReadSource(ConfirmingSource):
    """Emit one typed ban from the source-state read before any RSVP wire mutation."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(Source.MEETUP, event_log=events)
        self.registration_read_attempts = 0
        self._access_denied = False

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        self.registration_read_attempts += 1
        if not self._access_denied:
            self._access_denied = True
            if self._event_log is not None:
                self._event_log.append("source:access_denied")
            raise SourceAccessDeniedError(SourceQuarantineSignal.FORBIDDEN)
        return await super().read_registration_state(tenant_id, target, modality)


class KillSwitchAfterMutationPacer(RecordingPacer):
    """Engage durable policy after the RSVP mutation permit but before adapter dispatch."""

    def __init__(self, events: list[str], engage: Callable[[], Awaitable[None]]) -> None:
        super().__init__(events)
        self._engage = engage
        self.engaged = False

    async def acquire(self, request: PacerRequest) -> PacerLease:
        lease = await super().acquire(request)
        if request.operation is PacerOperation.REGISTRATION_MUTATION and not self.engaged:
            await self._engage()
            self.engaged = True
            self.events.append("policy:kill_switch")
        return lease


class NotPresentOnConfirmationReadSource(ConfirmingSource):
    """Simulate an eventually-consistent RSVP read after an opaque confirmation wake-up."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(Source.MEETUP, pending_after_effect_once=True, event_log=events)
        self._return_not_present_once = False
        self.not_present_confirmation_reads = 0

    def return_not_present_on_next_read(self) -> None:
        """Schedule one stale read only after the workflow has reached its durable wait."""
        self._return_not_present_once = True

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        if self._return_not_present_once:
            self._return_not_present_once = False
            self.not_present_confirmation_reads += 1
            if self._event_log is not None:
                self._event_log.append("source:read:not_present")
            return RsvpState.NOT_PRESENT
        return await super().read_registration_state(tenant_id, target, modality)


class _CrashAfterOutcomeAudit:
    """Persist one source outcome, then lose the first activity acknowledgement."""

    def __init__(self) -> None:
        self._delegate = PostgresRegistrationActionAuditRepository()
        self.outcome_attempts = 0
        self._crashed = False

    async def append(self, record: RegistrationActionAudit) -> bool:
        appended = await self._delegate.append(record)
        if record.phase is RegistrationActionAuditPhase.SOURCE_RSVP_OUTCOME:
            self.outcome_attempts += 1
            if not self._crashed:
                self._crashed = True
                raise RuntimeError("simulated registration audit acknowledgement loss")
        return appended


class _QuarantineAckLossRecorder:
    """Fixture state proving a post-quarantine activity acknowledgement is retried once."""

    def __init__(self) -> None:
        self.invocations = 0
        self.crashed = False


_quarantine_ack_loss_recorder = _QuarantineAckLossRecorder()


@activity.defn(name="register_or_rsvp")
async def _crash_after_quarantine_result(inp: RegisterOrRsvpInput) -> RegisterOrRsvpResult:
    """Lose the first Temporal acknowledgement after the source quarantine has committed."""
    result = await register_or_rsvp(inp)
    _quarantine_ack_loss_recorder.invocations += 1
    if result.outcome == "source_quarantined" and not _quarantine_ack_loss_recorder.crashed:
        _quarantine_ack_loss_recorder.crashed = True
        raise RuntimeError("simulated source-quarantine acknowledgement loss")
    return result


_forced_candidate_ids: list[str] = []
_forced_membership_plans: dict[str, list[str]] = {}


class _BlockedConfirmation:
    """Mutable fixture state shared with one confirmation activity invocation."""

    def __init__(self) -> None:
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None


_blocked_confirmation = _BlockedConfirmation()


class _CompletionActivityRecorder:
    """Captures the Temporal logical timestamp at which the completion timer schedules work."""

    def __init__(self) -> None:
        self.scheduled_at: list[datetime] = []


_completion_activity_recorder = _CompletionActivityRecorder()


class _HandoffExpiryActivityRecorder:
    """Records a deliberately lost expiry acknowledgement after its guarded commit."""

    def __init__(self) -> None:
        self.attempts: list[int] = []
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None


_handoff_expiry_activity_recorder = _HandoffExpiryActivityRecorder()


class _HandoffReminderActivityRecorder:
    """Records a deliberately lost acknowledgement after the guarded reminder commit."""

    def __init__(self) -> None:
        self.attempts: list[int] = []
        self.statuses: list[str] = []


_handoff_reminder_activity_recorder = _HandoffReminderActivityRecorder()


class _HandoffCompletionActivityRecorder:
    """Records a lost Temporal acknowledgement after verified completion commits."""

    def __init__(self) -> None:
        self.attempts: list[int] = []
        self.statuses: list[str] = []
        self.crashed = False
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None


_handoff_completion_activity_recorder = _HandoffCompletionActivityRecorder()


class _PersistentHandoffCompletionFailureRecorder:
    """Capture each workflow-owned probe of a dependency that never recovers."""

    def __init__(self) -> None:
        self.completion_ids: list[str] = []
        self.activity_attempts: list[int] = []
        self.scheduled_at: list[datetime] = []


_persistent_handoff_completion_failure = _PersistentHandoffCompletionFailureRecorder()


async def _wait_for_persistent_completion_probes(
    expected: list[str],
    failure: str,
) -> None:
    """Wait for an exact deterministic probe sequence without hiding unexpected extra attempts."""
    for _ in range(400):
        if _persistent_handoff_completion_failure.completion_ids == expected:
            return
        await asyncio.sleep(0.01)
    pytest.fail(failure)


async def _wait_for_retained_handoff_state(
    container: Container,
    tenant_id: UUID,
    canonical_event_id: UUID,
    task_id: str,
    *,
    task_state: HandoffState | None = None,
    lifecycle_state: LifecycleState | None = None,
    failure: str,
) -> None:
    """Wait for one exact task/lifecycle projection used by retained-child regressions."""
    for _ in range(400):
        task = await container.handoff_repo.get(tenant_id, task_id)
        lifecycle = (
            await container.lifecycle_repo.find_active(tenant_id, canonical_event_id)
            if lifecycle_state is not None
            else None
        )
        if (
            task is not None
            and (task_state is None or task.state is task_state)
            and (lifecycle_state is None or lifecycle is not None)
            and (lifecycle_state is None or lifecycle.state is lifecycle_state)
        ):
            return
        await asyncio.sleep(0.01)
    pytest.fail(failure)


class _BlockedHandoffRoute:
    """Hold task creation after the workflow has opened its completion signal buffer."""

    def __init__(self) -> None:
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None


_blocked_handoff_route = _BlockedHandoffRoute()


class _ReconcileActivityRecorder:
    """Record organizer commands that reach the real reconciliation activity."""

    def __init__(self) -> None:
        self.fingerprints: list[str] = []


_reconcile_activity_recorder = _ReconcileActivityRecorder()


@activity.defn(name="discover_and_rank")
async def _fixed_discover_and_rank(_: RequestInput) -> DiscoverResult:
    """Test-only deterministic parent selection for the directive-protocol regression test."""
    return DiscoverResult(candidate_ids=list(_forced_candidate_ids))


@activity.defn(name="resolve_membership")
async def _fixed_resolve_membership(inp: ResolveMembershipInput) -> ResolveMembershipResult:
    """Keep real lifecycle setup while injecting per-candidate handoff eligibility for ADR-003."""
    result = await resolve_membership(inp)
    lane_plan = _forced_membership_plans.get(inp.canonical_event_id)
    if lane_plan is None:
        return result
    return ResolveMembershipResult(
        status=result.status,
        lane_plan=lane_plan,
        lane=result.lane,
        detail=result.detail,
        pacing_status=result.pacing_status,
        retry_after_seconds=result.retry_after_seconds,
    )


@activity.defn(name="await_confirmation")
async def _blocked_await_confirmation(inp: AwaitConfirmationInput) -> AwaitConfirmationResult:
    """Hold confirmation persistence open to exercise the REGISTERED fanout race."""
    if _blocked_confirmation.entered is None or _blocked_confirmation.release is None:
        raise RuntimeError("confirmation race fixture is not configured")
    _blocked_confirmation.entered.set()
    await _blocked_confirmation.release.wait()
    return await await_confirmation(inp)


@activity.defn(name="await_confirmation")
async def _crash_after_handoff_completion_commit(
    inp: AwaitConfirmationInput,
) -> AwaitConfirmationResult:
    """Lose the first activity ACK after a verified task/lifecycle receipt commits."""
    result = await await_confirmation(inp)
    if inp.handoff_completion_id is None:
        return result
    attempt = activity.info().attempt
    _handoff_completion_activity_recorder.attempts.append(attempt)
    _handoff_completion_activity_recorder.statuses.append(result.status)
    if not _handoff_completion_activity_recorder.crashed:
        entered = _handoff_completion_activity_recorder.entered
        release = _handoff_completion_activity_recorder.release
        if entered is not None and release is not None:
            entered.set()
            await release.wait()
        _handoff_completion_activity_recorder.crashed = True
        raise RuntimeError("simulated handoff-completion acknowledgement loss")
    return result


@activity.defn(name="await_confirmation")
async def _persistently_failing_handoff_confirmation(
    inp: AwaitConfirmationInput,
) -> AwaitConfirmationResult:
    """Model a provider/freeBusy transport outage that never consumes completion evidence."""
    if inp.handoff_completion_id is None:
        return await await_confirmation(inp)
    _persistent_handoff_completion_failure.completion_ids.append(
        inp.handoff_completion_id
    )
    _persistent_handoff_completion_failure.activity_attempts.append(activity.info().attempt)
    _persistent_handoff_completion_failure.scheduled_at.append(activity.info().scheduled_time)
    raise RuntimeError("simulated persistent handoff verification outage")


@activity.defn(name="route_to_handoff")
async def _blocked_route_to_handoff(inp: HandoffInput) -> RegChildResult:
    """Let a completion arrive before a zero-TTL handoff task activity acknowledges creation."""
    entered = _blocked_handoff_route.entered
    release = _blocked_handoff_route.release
    if entered is None or release is None:
        raise RuntimeError("handoff route gate is not configured")
    entered.set()
    await release.wait()
    return await route_to_handoff(inp)


@activity.defn(name="complete_lifecycle")
async def _recording_complete_lifecycle(inp: CompleteLifecycleInput) -> CompleteLifecycleResult:
    """Record Temporal's logical delivery time while preserving the real guarded transition."""
    _completion_activity_recorder.scheduled_at.append(activity.info().scheduled_time)
    return await complete_lifecycle(inp)


@activity.defn(name="reconcile_organizer_change")
async def _recording_reconcile_organizer_change(
    inp: ReconcileOrganizerChangeInput,
) -> ReconcileOrganizerChangeResult:
    """Expose serial fanout commands while retaining the real guarded reconciliation path."""
    _reconcile_activity_recorder.fingerprints.append(inp.fingerprint)
    return await reconcile_organizer_change(inp)


@activity.defn(name="expire_handoff")
async def _crash_after_handoff_expiry_commit(inp: ExpireHandoffInput) -> ExpireHandoffResult:
    """Lose the first activity acknowledgement after the task-bound expiry has committed."""
    if (
        _handoff_expiry_activity_recorder.entered is not None
        and _handoff_expiry_activity_recorder.release is not None
    ):
        _handoff_expiry_activity_recorder.entered.set()
        await _handoff_expiry_activity_recorder.release.wait()
    result = await expire_handoff(inp)
    attempt = activity.info().attempt
    _handoff_expiry_activity_recorder.attempts.append(attempt)
    if attempt == 1:
        raise RuntimeError("simulated handoff-expiry acknowledgement loss")
    return result


@activity.defn(name="compensate_calendar_write")
async def _backdate_calendar_recovery_for_reminder(inp: HandoffInput) -> RegChildResult:
    """Make a real retained recovery task due for T+24h without advancing PostgreSQL's clock."""
    result = await compensate_calendar_write(inp)
    if result.handoff_task_id is None:
        raise RuntimeError("calendar recovery fixture did not create a task")
    created_at = datetime.now(UTC) - timedelta(hours=25)
    await _set_handoff_task_created_at(result.handoff_task_id, created_at)
    return RegChildResult(
        status=result.status,
        lane=result.lane,
        calendar_event_id=result.calendar_event_id,
        handoff_task_id=result.handoff_task_id,
        handoff_expires_at=result.handoff_expires_at,
        handoff_expiry_transition_id=result.handoff_expiry_transition_id,
        handoff_created_at=created_at.isoformat(),
        detail=result.detail,
    )


@activity.defn(name="enqueue_handoff_reminder")
async def _crash_after_handoff_reminder_commit(
    inp: HandoffReminderInput,
) -> HandoffReminderActivityResult:
    """Lose one Temporal ACK after the guarded cadence/outbox transaction commits."""
    result = await enqueue_handoff_reminder(inp)
    attempt = activity.info().attempt
    _handoff_reminder_activity_recorder.attempts.append(attempt)
    _handoff_reminder_activity_recorder.statuses.append(result.status)
    if attempt == 1:
        raise RuntimeError("simulated handoff-reminder acknowledgement loss")
    return result


def _saga_activities(
    *,
    discovery_activity: object = discover_and_rank,
    membership_activity: object = resolve_membership,
    registration_activity: object = register_or_rsvp,
    confirmation_activity: object = await_confirmation,
    calendar_recovery_activity: object = compensate_calendar_write,
    completion_activity: object = complete_lifecycle,
    reconcile_activity: object = reconcile_organizer_change,
    expiry_activity: object = expire_handoff,
    reminder_activity: object = enqueue_handoff_reminder,
    handoff_activity: object = route_to_handoff,
) -> list[object]:
    return [
        discovery_activity,
        finalize_no_candidate,
        membership_activity,
        policy_gate,
        close_failed_candidate,
        registration_activity,
        confirmation_activity,
        calendar_recovery_activity,
        dedupe_calendar,
        write_to_calendar,
        reconcile_activity,
        unrsvp,
        handoff_activity,
        completion_activity,
        expiry_activity,
        reminder_activity,
    ]


async def _outbox_topic_counts(tenant_id: object) -> dict[str, int]:
    async with system_session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """SELECT topic, count(*) AS count FROM outbox
                       WHERE tenant_id = :tenant_id GROUP BY topic"""
                ),
                {"tenant_id": tenant_id},
            )
        ).all()
    return {str(row.topic): int(row.count) for row in rows}


async def _registration_action_audit_phase_counts(tenant_id: UUID) -> dict[str, int]:
    """Return only this tenant's closed P6a phase counts after a Temporal saga commits."""
    async with tenant_session_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    """SELECT phase, count(*) AS count
                       FROM registration_action_audit
                       GROUP BY phase"""
                )
            )
        ).all()
    return {str(row.phase): int(row.count) for row in rows}


async def _registration_action_audit_facts(
    tenant_id: UUID,
    workflow_id: str,
) -> list[tuple[str, str, UUID | None]]:
    """Read the minimal P14c audit proof for one child through its tenant-scoped view."""
    async with tenant_session_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    """SELECT phase, policy_decision, consent_ref
                       FROM registration_action_audit
                       WHERE workflow_id = :workflow_id
                       ORDER BY audit_key"""
                ),
                {"workflow_id": workflow_id},
            )
        ).all()
    facts: list[tuple[str, str, UUID | None]] = []
    for row in rows:
        consent_ref = row.consent_ref
        if consent_ref is not None and not isinstance(consent_ref, UUID):
            raise RuntimeError("registration audit consent reference was not a UUID")
        facts.append((str(row.phase), str(row.policy_decision), consent_ref))
    return facts


async def _lifecycle_state(tenant_id: UUID, workflow_id: str) -> str:
    """Read only this fixture's RLS-scoped lifecycle state after a Temporal activity commits."""
    async with tenant_session_scope(tenant_id) as session:
        return str(
            (
                await session.execute(
                    text("SELECT state FROM lifecycle WHERE workflow_id = :workflow_id"),
                    {"workflow_id": workflow_id},
                )
            )
            .one()
            .state
        )


async def _handoff_expiry_effect_counts(tenant_id: UUID, task_id: str) -> tuple[str, int, int]:
    """Return the task state and exactly-once terminal records for one TTL handoff fixture."""
    async with tenant_session_scope(tenant_id) as session:
        task = (
            await session.execute(
                text(
                    """SELECT state, expiry_transition_id FROM handoff_tasks
                       WHERE task_id = :task_id"""
                ),
                {"task_id": task_id},
            )
        ).one()
        ledger_count = (
            await session.execute(
                text(
                    """SELECT count(*) FROM transition_ledger
                       WHERE transition_id = :transition_id"""
                ),
                {"transition_id": task.expiry_transition_id},
            )
        ).scalar_one()
        expiry_outbox_count = (
            await session.execute(
                text(
                    """SELECT count(*) FROM outbox
                       WHERE tenant_id = :tenant_id
                         AND topic = 'lifecycle.expired'
                         AND payload ->> 'transition_id' = :transition_id"""
                ),
                {
                    "tenant_id": tenant_id,
                    "transition_id": task.expiry_transition_id,
                },
            )
        ).scalar_one()
    return str(task.state), int(ledger_count), int(expiry_outbox_count)


async def _set_handoff_task_created_at(task_id: str, created_at: datetime) -> None:
    """Use the migration owner only to make a deterministic elapsed-time Temporal fixture."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        raise RuntimeError("EC_MIGRATION_URL is required for the handoff reminder fixture")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.handoff_tasks
                    SET created_at = :created_at
                    WHERE task_id = :task_id
                    """
                ),
                {"task_id": task_id, "created_at": created_at},
            )
    finally:
        await owner_engine.dispose()


async def _set_global_policy_kill_switch(engaged: bool) -> None:
    """Use the migration owner to flip ADR-004's durable global control in a Temporal race."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        raise RuntimeError("EC_MIGRATION_URL is required for the policy-race fixture")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            result = (
                await connection.execute(
                    text("SELECT public.fn_set_policy_global_kill_switch(:engaged)"),
                    {"engaged": engaged},
                )
            ).scalar_one()
    finally:
        await owner_engine.dispose()
    if result is not engaged:
        raise RuntimeError("policy global control returned an unexpected value")


async def _seed_registration_consent(
    tenant_id: UUID,
    source: Source,
    modality: Modality,
) -> UUID:
    """Seed immutable P14c fixture evidence through the migration owner (FR-2.9, NFR-10).

    The app role deliberately has no grant/write capability.  Workflow tests that exercise an
    autonomous source lane therefore seed the already-recorded owner evidence explicitly, after
    their tenant exists, instead of letting a test double manufacture delegation at action time.
    """
    if (source, modality) not in {
        (Source.MEETUP, Modality.API),
        (Source.LUMA, Modality.BROWSER),
    }:
        raise ValueError("fixture consent only supports the closed P14c registration lanes")
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        raise RuntimeError("EC_MIGRATION_URL is required for registration-consent fixtures")
    consent_id = uuid4()
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            # Set the context even for the migration owner so this fixture remains valid if that
            # role stops bypassing FORCE RLS in a future local deployment.
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.tenant_source_consents (
                        consent_id, tenant_id, source, modality, scope
                    ) VALUES (
                        :consent_id, :tenant_id, :source, :modality, 'registration'
                    )
                    """
                ),
                {
                    "consent_id": consent_id,
                    "tenant_id": tenant_id,
                    "source": source.value,
                    "modality": modality.value,
                },
            )
    finally:
        await owner_engine.dispose()
    return consent_id


async def _handoff_reminder_outbox_effects(
    tenant_id: UUID, task_id: str
) -> tuple[int, int, str | None]:
    """Read exactly the visible reminder effect for one task without exposing its private ledger."""
    async with tenant_session_scope(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS reminder_count,
                           count(DISTINCT payload ->> 'reminder_id') AS reminder_ids,
                           min(payload ->> 'reminder_kind') AS reminder_kind
                    FROM outbox
                    WHERE tenant_id = :tenant_id
                      AND topic = 'handoff.reminder'
                      AND payload ->> 'task_id' = :task_id
                    """
                ),
                {"tenant_id": tenant_id, "task_id": task_id},
            )
        ).one()
    mapping = row._mapping
    return (
        int(mapping["reminder_count"]),
        int(mapping["reminder_ids"]),
        str(mapping["reminder_kind"]) if mapping["reminder_kind"] is not None else None,
    )


async def _release_retained_handoff_expiry(
    env: WorkflowEnvironment,
    tenant_id: UUID,
    canonical_event_id: UUID,
    workflow_id: str,
    tag: str,
) -> RegChildResult:
    """Start a retained child, advance its zero-TTL timer, then release the first expiry attempt."""
    queue = f"ec-handoff-expiry-retry-{tag}"
    entered = _handoff_expiry_activity_recorder.entered
    release = _handoff_expiry_activity_recorder.release
    if entered is None or release is None:
        raise RuntimeError("expiry activity gate was not configured")
    with env.auto_time_skipping_disabled():
        child = await env.client.start_workflow(
            RegistrationWorkflow.run,
            RegChildInput(
                tenant_id=str(tenant_id),
                canonical_event_id=str(canonical_event_id),
                keep_open_after_scheduling=True,
            ),
            id=workflow_id,
            task_queue=queue,
        )
        for _ in range(100):
            try:
                state = await _lifecycle_state(tenant_id, workflow_id)
            except NoResultFound:
                state = None
            if state == LifecycleState.HANDOFF.value:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("handoff task did not become durable before its expiry timer")
        # An ``asyncio.Event`` is outside Temporal, so manually advance the test server clock
        # after the task exists. The first activity blocks at the gate until this test releases it.
        await env.sleep(timedelta(seconds=1))
        await asyncio.wait_for(entered.wait(), timeout=2)
        release.set()
    return await child.result()


async def test_request_workflow_end_to_end_keeps_raw_request_text_out_of_history(db: None) -> None:
    settings = get_settings()
    tag = uuid4().hex
    crawl_ev = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"wf-jazz-{tag}",
        title=f"Workflow Jazz {tag}",
        start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=4),
        registration_url="https://example.com/wf-jazz",
        city="New York",
        description="live jazz",
        is_free=True,
    )
    container = build_container(
        settings,
        discovery_sources=[PublicJsonLdSource(user_agent="test", fixture_events=[crawl_ev])],
        register_sources={},  # no autonomous register -> handoff lane through the full spine
    )
    set_container(container)
    # Source refresh is deliberately off the request workflow path. Seed this fixture catalog
    # explicitly, mirroring the dedicated catalog-refresh worker's effect.
    await container.discovery.discover(RequestConstraints())

    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|wf-{tag}", f"wf-{tag}@example.com", f"wf-{tag}@u.concierge.test")
    )
    request_id = uuid4()
    await container.request_repo.add(
        await container.parser.parse(tenant_id, request_id, "find me some jazz")
    )
    workflow_id = request_workflow_id(tenant_id, request_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-test",
            workflows=[EventRequestWorkflow, RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                EventRequestWorkflow.run,
                RequestInput(
                    tenant_id=str(tenant_id),
                    request_id=str(request_id),
                    attempt_budget=3,
                ),
                id=workflow_id,
                task_queue="ec-test",
            )
            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    assert result.outcome in ("handoff", "registered")
    assert result.attempts >= 1
    assert b"find me some jazz" not in b"".join(
        event.SerializeToString() for event in history.events
    )


async def test_register_saga_recovers_lost_source_ack_without_second_rsvp(db: None) -> None:
    """A source effect followed by an activity crash re-enters through read-before-mutate (AC-56)."""
    settings = get_settings()
    tag = uuid4().hex
    events: list[str] = []
    source = ConfirmingSource(
        Source.MEETUP,
        raise_after_effect_once=True,
        event_log=events,
    )
    pacer = RecordingPacer(events)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        pacer=pacer,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|source-crash-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    consent_ref = await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"source-crash-{tag}",
                    title=f"Source crash meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/source-crash",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-source-crash",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-source-crash",
            )
            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    assert result.status == "registered"
    assert source.registration_effects == 1
    assert source.register_attempts == 1
    assert len(source.idempotency_keys) == 1
    assert ":register_or_rsvp:1" in source.idempotency_keys[0]
    assert len(container.calendar.entries(tenant_id)) == 1
    source_indices = [index for index, event in enumerate(events) if event.startswith("source:")]
    assert source_indices
    assert all(index > 0 and events[index - 1] == "pacer" for index in source_indices)
    assert all(key.startswith(f"meetup:tenant:{tenant_id}") for key in pacer.keys)
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
    }
    assert await _registration_action_audit_phase_counts(tenant_id) == {
        "policy_precheck": 1,
        "policy_pre_mutate": 1,
        "source_rsvp_outcome": 1,
    }
    assert str(consent_ref).encode() not in b"".join(
        event.SerializeToString() for event in history.events
    )


async def test_register_saga_recovers_lost_action_audit_ack_without_second_rsvp(db: None) -> None:
    """A committed outcome fact with a lost ACK replays through a source-state read (NFR-8)."""
    settings = get_settings()
    tag = uuid4().hex
    events: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=events)
    audit = _CrashAfterOutcomeAudit()
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        pacer=RecordingPacer(events),
        action_audit=audit,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|audit-crash-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"audit-crash-{tag}",
                    title=f"Audit crash meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/audit-crash",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-audit-crash",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-audit-crash",
            )

    assert result.status == "registered"
    assert source.registration_effects == 1
    assert source.register_attempts == 1
    assert audit.outcome_attempts == 2
    assert await _registration_action_audit_phase_counts(tenant_id) == {
        "policy_precheck": 1,
        "policy_pre_mutate": 1,
        "source_rsvp_outcome": 1,
    }


async def test_register_saga_quarantine_survives_ack_loss_without_a_second_source_call(
    db: None,
) -> None:
    """A committed ban fence retries directly to handoff before another source request (AC-72)."""
    settings = get_settings()
    tag = uuid4().hex
    events: list[str] = []
    source = AccessDeniedOnRegistrationReadSource(events)
    source_policies = default_source_policies()
    snapshots = MockPolicySnapshotReader(source_policies)
    quarantine = MockSourceQuarantineRepository(source_policies)
    pacer = RecordingPacer(events)
    _quarantine_ack_loss_recorder.invocations = 0
    _quarantine_ack_loss_recorder.crashed = False
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        pacer=pacer,
        policy_engine=StoreBackedPolicyEngine(snapshots),
        source_quarantine=quarantine,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|quarantine-crash-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"quarantine-crash-{tag}",
                    title=f"Quarantine crash meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://meetup.example/quarantine-crash-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=f"ec-saga-quarantine-crash-{tag}",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(registration_activity=_crash_after_quarantine_result),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue=f"ec-saga-quarantine-crash-{tag}",
            )

    lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id, canonical.canonical_event_id, workflow_id
    )
    assert result.status == "handoff"
    assert result.handoff_task_id is not None
    assert lifecycle.state is LifecycleState.HANDOFF
    assert _quarantine_ack_loss_recorder.invocations == 2
    assert _quarantine_ack_loss_recorder.crashed is True
    assert quarantine.calls == [(Source.MEETUP, SourceQuarantineSignal.FORBIDDEN)]
    assert source_policies[Source.MEETUP].quarantined is True
    # The only source read after membership produced the typed ban. Recovery stopped at the fresh
    # policy guard before a second Pacer lease, source read, RSVP mutation, or calendar write.
    assert source.registration_read_attempts == 1
    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert container.calendar.entries(tenant_id) == []
    assert pacer.keys == [f"meetup:tenant:{tenant_id}"] * 2
    assert events == [
        "pacer",
        "source:membership:member",
        "pacer",
        "source:access_denied",
    ]
    assert await _outbox_topic_counts(tenant_id) == {"lifecycle.handoff": 1}
    assert await _registration_action_audit_phase_counts(tenant_id) == {
        "policy_precheck": 1,
        "source_rsvp_outcome": 1,
    }


async def test_register_saga_uses_a_durable_pacer_timer_before_the_source_mutation(
    db: None,
) -> None:
    """A quota wait parks Temporal, re-reads state on wake, and preserves exactly-one RSVP (ADR-005)."""
    settings = get_settings()
    tag = uuid4().hex
    events: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=events)
    # Calls are membership, RSVP-state read, then mutation. Defer exactly the pre-mutate lease.
    pacer = WaitThenGrantPacer(events, wait_on_call=3, seconds=5.0)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        pacer=pacer,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|pacer-timer-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"pacer-timer-{tag}",
                    title=f"Pacer timer meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/pacer-timer",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-pacer-timer",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            handle = await env.client.start_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-pacer-timer",
            )
            with env.auto_time_skipping_disabled():
                for _ in range(100):
                    if pacer.waits == 1:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("registration workflow did not receive the Pacer projection")

                # The activity returned before wire mutation; no worker-side sleep may bridge this.
                assert source.registration_effects == 0
                assert source.register_attempts == 0
                assert "source:register" not in events
                await env.sleep(5.0)
            result = await handle.result()

    assert result.status == "registered"
    assert pacer.waits == 1
    assert source.registration_effects == 1
    assert source.register_attempts == 1
    assert len(source.idempotency_keys) == 1
    assert len(container.calendar.entries(tenant_id)) == 1
    # Both source calls after the durable sleep reuse their workflow-minted queue identities. The
    # fair Pacer may re-charge after a lost advisory grant, but it must never create a second FIFO
    # entry for this same physical read/mutation attempt (ADR-003/005).
    assert pacer.queue_items[1][0] is PacerOperation.REGISTRATION_READ
    assert pacer.queue_items[1] == pacer.queue_items[3]
    assert pacer.queue_items[2][0] is PacerOperation.REGISTRATION_MUTATION
    assert pacer.queue_items[2] == pacer.queue_items[4]


async def test_register_saga_records_a_source_retry_after_before_retrying(db: None) -> None:
    """A normalized source 429 becomes a shared Pacer backoff and durable workflow timer (AC-73)."""
    settings = get_settings()
    tag = uuid4().hex
    events: list[str] = []
    source = RateLimitedReadOnceSource(events)
    pacer = RateLimitFeedbackPacer(events)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        pacer=pacer,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|source-429-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"source-429-{tag}",
                    title=f"Source throttle meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/source-429",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-source-429",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-source-429",
            )

    assert result.status == "registered"
    assert pacer.backoffs == [(f"meetup:tenant:{tenant_id}", 5.0)]
    assert events.index("source:rate_limited") < events.index("pacer:backoff-wait")
    assert source.registration_effects == 1
    assert source.register_attempts == 1
    assert len(source.idempotency_keys) == 1


async def test_register_saga_pre_mutation_guard_stops_a_midflight_kill_switch(db: None) -> None:
    """The final ADR-004 guard blocks a post-Pacer kill switch with zero RSVP effect (AC-40/50)."""
    settings = get_settings()
    tag = uuid4().hex
    events: list[str] = []

    async def engage_durable_global_kill_switch() -> None:
        await _set_global_policy_kill_switch(True)

    source = ConfirmingSource(Source.MEETUP, event_log=events)
    pacer = KillSwitchAfterMutationPacer(events, engage_durable_global_kill_switch)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        pacer=pacer,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|policy-race-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"policy-race-{tag}",
                    title=f"Policy race meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/policy-race",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="ec-saga-policy-race",
                workflows=[RegistrationWorkflow],
                activities=_saga_activities(),
            ):
                result = await env.client.execute_workflow(
                    RegistrationWorkflow.run,
                    RegChildInput(
                        tenant_id=str(tenant_id),
                        canonical_event_id=str(canonical.canonical_event_id),
                    ),
                    id=workflow_id,
                    task_queue="ec-saga-policy-race",
                )
    finally:
        await _set_global_policy_kill_switch(False)

    lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id, canonical.canonical_event_id, workflow_id
    )
    assert result.status == "handoff"
    assert result.handoff_task_id is not None
    assert lifecycle.state is LifecycleState.HANDOFF
    # Reaching the RSVP-state read proves the early workflow policy activity admitted this lane.
    # The Pacer then flips policy after the last source token is granted; the final in-activity
    # guard must still deny before the RSVP wire call.
    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert source.idempotency_keys == []
    assert container.calendar.entries(tenant_id) == []
    assert events == [
        "pacer",
        "source:membership:member",
        "pacer",
        "source:read:not_present",
        "pacer",
        "policy:kill_switch",
    ]
    assert pacer.engaged is True
    assert pacer.keys == [f"meetup:tenant:{tenant_id}"] * 3
    assert await _outbox_topic_counts(tenant_id) == {"lifecycle.handoff": 1}
    assert await _registration_action_audit_phase_counts(tenant_id) == {
        "policy_precheck": 1,
        "policy_pre_mutate": 1,
    }


async def test_register_saga_policy_store_outage_fails_to_handoff_before_source_mutation(
    db: None,
) -> None:
    """An unavailable durable PDP denies the child safely instead of extending autonomy (NFR-15)."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    reader = MockPolicySnapshotReader()
    reader.unavailable = True
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        policy_engine=StoreBackedPolicyEngine(reader),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|policy-outage-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"policy-outage-{tag}",
                    title=f"Policy store outage {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://meetup.example/policy-outage-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=f"ec-saga-policy-outage-{tag}",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue=f"ec-saga-policy-outage-{tag}",
            )

    assert result.status == "handoff"
    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert source.idempotency_keys == []
    assert await _lifecycle_state(tenant_id, workflow_id) == LifecycleState.HANDOFF.value
    assert await _registration_action_audit_phase_counts(tenant_id) == {"policy_precheck": 1}


async def test_register_saga_missing_consent_writes_denied_precheck_without_source_io(
    db: None,
) -> None:
    """The normal Temporal path audits absent evidence before it can spend a provider lease (FR-2.9)."""
    settings = get_settings()
    tag = uuid4().hex
    events: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=events)
    pacer = RecordingPacer(events)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        pacer=pacer,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|missing-consent-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"missing-consent-{tag}",
                    title=f"Missing consent meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://meetup.example/missing-consent-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=f"ec-saga-missing-consent-{tag}",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue=f"ec-saga-missing-consent-{tag}",
            )

    assert result.status == "handoff"
    assert result.handoff_task_id is not None
    assert events == []
    assert pacer.keys == []
    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert container.calendar.entries(tenant_id) == []
    assert await _lifecycle_state(tenant_id, workflow_id) == LifecycleState.HANDOFF.value
    assert await _outbox_topic_counts(tenant_id) == {"lifecycle.handoff": 1}
    assert await _registration_action_audit_phase_counts(tenant_id) == {"policy_precheck": 1}
    assert await _registration_action_audit_facts(tenant_id, workflow_id) == [
        ("policy_precheck", "denied", None)
    ]


async def test_register_saga_rejects_a_duplicate_tenant_event_workflow(db: None) -> None:
    """Temporal rejects a second ``{tenant}:{event}`` child while one saga owns its effects (AC-55)."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP, pending_after_effect_once=True)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|duplicate-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    source_event_id = f"duplicate-{tag}"
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=source_event_id,
                    title=f"Duplicate workflow meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/duplicate",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    child_input = RegChildInput(
        tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-duplicate",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            handle = await env.client.start_workflow(
                RegistrationWorkflow.run,
                child_input,
                id=workflow_id,
                task_queue="ec-saga-duplicate",
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            for _ in range(100):
                lifecycle = await container.lifecycle_repo.get_or_create(
                    tenant_id, canonical.canonical_event_id, workflow_id
                )
                if lifecycle.state is LifecycleState.AWAITING_CONFIRMATION:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("first registration workflow did not enter durable confirmation wait")

            with pytest.raises(WorkflowAlreadyStartedError):
                await env.client.start_workflow(
                    RegistrationWorkflow.run,
                    child_input,
                    id=workflow_id,
                    task_queue="ec-saga-duplicate",
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )

            source.confirm_pending(tenant_id, source_event_id, Modality.API)
            await handle.signal("confirmation_received", "opaque-confirmed")
            result = await handle.result()

    async with tenant_session_scope(tenant_id) as session:
        lifecycle_count = (
            await session.execute(
                text(
                    """SELECT count(*) AS count FROM lifecycle
                       WHERE tenant_id = :tenant_id AND canonical_event_id = :canonical_event_id"""
                ),
                {
                    "tenant_id": tenant_id,
                    "canonical_event_id": canonical.canonical_event_id,
                },
            )
        ).one()

    assert result.status == "registered"
    assert int(lifecycle_count.count) == 1
    assert source.registration_effects == 1
    assert source.register_attempts == 1
    assert len(container.calendar.entries(tenant_id)) == 1
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.awaiting_confirmation": 1,
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
    }


async def test_register_saga_keeps_an_unverified_confirmation_signal_pending(db: None) -> None:
    """A signal only wakes the ADR-003 wait; a fresh confirmed read alone can schedule (AC-57)."""
    settings = get_settings()
    tag = uuid4().hex
    source_events: list[str] = []
    source = ConfirmingSource(
        Source.MEETUP,
        pending_after_effect_once=True,
        pending_reads_before_confirmation=2,
        event_log=source_events,
    )
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|pending-ack-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    source_event_id = f"pending-ack-{tag}"
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=source_event_id,
                    title=f"Pending acknowledgement meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/pending-ack",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-pending-ack",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            handle = await env.client.start_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-pending-ack",
            )
            for _ in range(100):
                lifecycle = await container.lifecycle_repo.get_or_create(
                    tenant_id, canonical.canonical_event_id, workflow_id
                )
                if lifecycle.state is LifecycleState.AWAITING_CONFIRMATION:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("registration workflow did not enter durable confirmation wait")

            # The first opaque signal wakes a fresh read, but this source still reports pending.
            # The workflow must return to its durable wait rather than treating the signal as proof.
            await handle.signal("confirmation_received", "opaque-not-yet-confirmed")
            for _ in range(100):
                lifecycle = await container.lifecycle_repo.get_or_create(
                    tenant_id, canonical.canonical_event_id, workflow_id
                )
                if source_events.count("source:read:pending_confirmation") >= 2:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail(
                    "confirmation signal was not verified against fresh pending source state"
                )

            assert lifecycle.state is LifecycleState.AWAITING_CONFIRMATION
            assert container.calendar.entries(tenant_id) == []
            assert await _outbox_topic_counts(tenant_id) == {
                "lifecycle.awaiting_confirmation": 1,
            }

            await handle.signal("confirmation_received", "opaque-confirmed")
            result = await handle.result()

    assert result.status == "registered", (result, source_events)
    assert source.registration_effects == 1
    assert source.register_attempts == 1
    assert len(source.idempotency_keys) == 1
    assert source_events == [
        "source:membership:member",
        "source:read:not_present",
        "source:register",
        "source:read:pending_confirmation",
        "source:read:pending_confirmation",
        "source:read:confirmed",
    ]
    assert len(container.calendar.entries(tenant_id)) == 1
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.awaiting_confirmation": 1,
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
    }


async def test_register_saga_keeps_a_not_present_confirmation_read_awaiting(db: None) -> None:
    """A stale post-signal NOT_PRESENT read cannot emit REGISTERED before a later readback (FR-8.4/16.1)."""
    settings = get_settings()
    tag = uuid4().hex
    source_events: list[str] = []
    source = NotPresentOnConfirmationReadSource(source_events)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|not-present-ack-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    source_event_id = f"not-present-ack-{tag}"
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=source_event_id,
                    title=f"Not-present acknowledgement meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/not-present-ack",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-not-present-ack",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            handle = await env.client.start_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-not-present-ack",
            )
            for _ in range(100):
                lifecycle = await container.lifecycle_repo.get_or_create(
                    tenant_id, canonical.canonical_event_id, workflow_id
                )
                if lifecycle.state is LifecycleState.AWAITING_CONFIRMATION:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("registration workflow did not enter durable confirmation wait")

            # The opaque wake-up is not proof. A source whose read replica has not caught up can
            # still return NOT_PRESENT after the RSVP effect, so the child must re-arm its timer
            # without a registered transition or calendar write.
            source.return_not_present_on_next_read()
            await handle.signal("confirmation_received", "opaque-not-yet-visible")
            for _ in range(100):
                lifecycle = await container.lifecycle_repo.get_or_create(
                    tenant_id, canonical.canonical_event_id, workflow_id
                )
                if source.not_present_confirmation_reads == 1:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("confirmation signal was not verified against a fresh NOT_PRESENT read")

            assert lifecycle.state is LifecycleState.AWAITING_CONFIRMATION
            assert container.calendar.entries(tenant_id) == []
            assert await _outbox_topic_counts(tenant_id) == {
                "lifecycle.awaiting_confirmation": 1,
            }

            source.confirm_pending(tenant_id, source_event_id, Modality.API)
            await handle.signal("confirmation_received", "opaque-confirmed")
            result = await handle.result()

    assert result.status == "registered", (result, source_events)
    assert source.registration_effects == 1
    assert source.register_attempts == 1
    assert len(source.idempotency_keys) == 1
    assert source_events == [
        "source:membership:member",
        "source:read:not_present",
        "source:register",
        "source:read:pending_confirmation",
        "source:read:not_present",
        "source:read:confirmed",
    ]
    assert len(container.calendar.entries(tenant_id)) == 1
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.awaiting_confirmation": 1,
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
    }


async def test_register_saga_routes_pending_confirmation_timeout_to_handoff(db: None) -> None:
    """The 24-hour confirmation timer expires durably and creates one handoff task (FR-8.4)."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP, pending_after_effect_once=True)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|confirmation-timeout-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"confirmation-timeout-{tag}",
                    title=f"Confirmation timeout meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/confirmation-timeout",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-confirmation-timeout",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-confirmation-timeout",
            )

    assert result.status == "handoff"
    assert result.handoff_task_id is not None
    assert source.registration_effects == 1
    assert source.register_attempts == 1
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.awaiting_confirmation": 1,
        "lifecycle.handoff": 1,
    }


async def test_register_saga_retries_calendar_ack_loss_with_one_entry_and_transition(
    db: None,
) -> None:
    """A write effect followed by a lost ACK retries the same deterministic calendar/upsert keys."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|calendar-crash-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"calendar-crash-{tag}",
                    title=f"Calendar crash meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/calendar-crash",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    container.calendar.raise_after_upsert_once()
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-calendar-crash",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-calendar-crash",
            )

    assert result.status == "registered"
    assert source.registration_effects == 1
    assert len(container.calendar.entries(tenant_id)) == 1
    assert container.calendar.upsert_attempts == 2
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
    }


async def test_register_saga_forward_recovers_a_permanent_calendar_failure(db: None) -> None:
    """A confirmed RSVP remains registered when all idempotent calendar retries fail (ADR-007)."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|calendar-fail-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"calendar-fail-{tag}",
                    title=f"Calendar failure meetup {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url="https://meetup.example/calendar-fail",
                    city="New York",
                    is_free=True,
                )
            ]
        )
    )[0]
    container.calendar.fail_before_upsert(3)
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-calendar-failure",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-calendar-failure",
            )

    lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id, canonical.canonical_event_id, workflow_id
    )
    assert result.status == "registered"
    assert result.calendar_event_id is None
    assert result.handoff_task_id is not None
    assert lifecycle.state is LifecycleState.REGISTERED
    assert source.registration_effects == 1
    assert container.calendar.entries(tenant_id) == []
    assert container.calendar.upsert_attempts == 3
    retry = await container.registration.compensate_calendar_write(
        tenant_id,
        canonical,
        workflow_id,
        handoff_task_id=result.handoff_task_id,
        detail="simulated compensation acknowledgement loss",
    )
    assert retry.status.value == "registered"
    assert retry.handoff_task_id == result.handoff_task_id
    assert await _outbox_topic_counts(tenant_id) == {
        "calendar_recovery_required": 1,
        "lifecycle.registered": 1,
    }


async def test_retained_calendar_recovery_accepts_reschedule_before_its_task_ttl(db: None) -> None:
    """A reschedule interrupts a calendar-recovery handoff and retires its stale expiry task.

    The child has a factual RSVP but no calendar entry after its write retries exhaust.  It must
    continue to journal organizer changes while the manual task is open; a reschedule writes the
    entry, transitions ``registered`` to ``reconciled``, and atomically cancels the old task before
    a later cancellation ends the retained child (FR-6.6/8.7, ADR-003/007/008).
    """
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|retained-calendar-recovery-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    start_at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"retained-calendar-recovery-{tag}",
                    title=f"Retained calendar recovery {tag}",
                    start_at=start_at,
                    end_at=start_at + timedelta(hours=2),
                    registration_url=f"https://meetup.example/retained-calendar-recovery-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    # The real calendar activity has a bounded three-attempt policy.  Fail each attempt before
    # effect so the workflow creates its durable calendar-recovery task.
    container.calendar.fail_before_upsert(3)
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    task_id = f"{workflow_id}:calendar-recovery"
    rescheduled_start = start_at + timedelta(hours=4)
    rescheduled_end = rescheduled_start + timedelta(hours=2)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=f"ec-retained-calendar-recovery-{tag}",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            with env.auto_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    RegistrationWorkflow.run,
                    RegChildInput(
                        tenant_id=str(tenant_id),
                        canonical_event_id=str(canonical.canonical_event_id),
                        keep_open_after_scheduling=True,
                    ),
                    id=workflow_id,
                    task_queue=f"ec-retained-calendar-recovery-{tag}",
                )
                for _ in range(400):
                    task = await container.handoff_repo.get(tenant_id, task_id)
                    lifecycle = await container.lifecycle_repo.get_or_create(
                        tenant_id, canonical.canonical_event_id, workflow_id
                    )
                    if task is not None and lifecycle.state is LifecycleState.REGISTERED:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("retained child did not create its calendar-recovery task")

                await handle.signal(
                    "organizer_change",
                    OrganizerChangeSignal(
                        fingerprint=f"calendar-recovery-reschedule-{tag}",
                        canonical_event_id=str(canonical.canonical_event_id),
                        source=Source.MEETUP.value,
                        event_status="rescheduled",
                        start_at=rescheduled_start.isoformat(),
                        end_at=rescheduled_end.isoformat(),
                        time_zone="UTC",
                    ),
                )
                for _ in range(200):
                    lifecycle = await container.lifecycle_repo.get_or_create(
                        tenant_id, canonical.canonical_event_id, workflow_id
                    )
                    task = await container.handoff_repo.get(tenant_id, task_id)
                    if lifecycle.state is LifecycleState.RECONCILED and task is not None:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("calendar-recovery reschedule did not reconcile the retained child")

                assert task.state is HandoffState.CANCELLED
                await handle.signal(
                    "organizer_change",
                    OrganizerChangeSignal(
                        fingerprint=f"calendar-recovery-cancel-{tag}",
                        canonical_event_id=str(canonical.canonical_event_id),
                        source=Source.MEETUP.value,
                        event_status="cancelled",
                    ),
                )
                child_result = await handle.result()

    assert child_result.status == "cancelled"
    assert source.registration_effects == 1
    assert container.calendar.upsert_attempts == 4
    assert len(container.calendar.entries(tenant_id)) == 0
    assert await _outbox_topic_counts(tenant_id) == {
        "calendar_recovery_required": 1,
        "lifecycle.registered": 1,
        "lifecycle.reconciled": 1,
        "lifecycle.cancelled": 1,
    }


async def test_retained_calendar_recovery_accepts_unrsvp_before_its_task_ttl(db: None) -> None:
    """User withdrawal interrupts a calendar-recovery task instead of waiting for expiry (FR-8.8)."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        withdrawal_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|calendar-recovery-unrsvp-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    start_at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"calendar-recovery-unrsvp-{tag}",
                    title=f"Calendar recovery un-RSVP {tag}",
                    start_at=start_at,
                    end_at=start_at + timedelta(hours=2),
                    registration_url=f"https://meetup.example/calendar-recovery-unrsvp-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    container.calendar.fail_before_upsert(3)
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    task_id = f"{workflow_id}:calendar-recovery"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=f"ec-calendar-recovery-unrsvp-{tag}",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            with env.auto_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    RegistrationWorkflow.run,
                    RegChildInput(
                        tenant_id=str(tenant_id),
                        canonical_event_id=str(canonical.canonical_event_id),
                        keep_open_after_scheduling=True,
                    ),
                    id=workflow_id,
                    task_queue=f"ec-calendar-recovery-unrsvp-{tag}",
                )
                for _ in range(400):
                    task = await container.handoff_repo.get(tenant_id, task_id)
                    lifecycle = await container.lifecycle_repo.get_or_create(
                        tenant_id, canonical.canonical_event_id, workflow_id
                    )
                    if task is not None and lifecycle.state is LifecycleState.REGISTERED:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("retained child did not create its calendar-recovery task")

                await handle.signal(
                    "unrsvp_requested", UnrsvpSignal(request_id=f"calendar-recovery-unrsvp-{tag}")
                )
                child_result = await handle.result()

    task = await container.handoff_repo.get(tenant_id, task_id)
    lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id, canonical.canonical_event_id, workflow_id
    )
    assert child_result.status == "cancelled"
    assert task is not None and task.state is HandoffState.CANCELLED
    assert lifecycle.state is LifecycleState.CANCELLED
    assert source.registration_effects == 1
    assert source.withdrawal_effects == 1
    assert container.calendar.upsert_attempts == 3
    assert container.calendar.entries(tenant_id) == []
    assert await _outbox_topic_counts(tenant_id) == {
        "calendar_recovery_required": 1,
        "lifecycle.registered": 1,
        "lifecycle.withdrawing": 1,
        "lifecycle.cancelled": 1,
    }


async def test_retained_handoff_reminder_retries_lost_ack_with_one_outbox_effect(db: None) -> None:
    """A T+24 reminder replays after its commit ACK is lost without notifying twice.

    The test backdates only the fixture task through the migration owner because Temporal's
    time-skipping clock must not be confused with PostgreSQL's guarded ``clock_timestamp()``.
    The actual reminder activity and its durable SQL guard still execute on both attempts
    (FR-6.6, FR-8.3/8.9, ADR-003/007/009).
    """
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|handoff-reminder-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    start_at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"handoff-reminder-{tag}",
                    title=f"Handoff reminder retry {tag}",
                    start_at=start_at,
                    end_at=start_at + timedelta(hours=2),
                    registration_url=f"https://meetup.example/handoff-reminder-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    # Exhaust the bounded calendar write attempts, then create an active recovery task whose
    # persisted ``created_at`` the test activity moves to T+25h.
    container.calendar.fail_before_upsert(3)
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    task_id = f"{workflow_id}:calendar-recovery"
    _handoff_reminder_activity_recorder.attempts.clear()
    _handoff_reminder_activity_recorder.statuses.clear()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=f"ec-handoff-reminder-retry-{tag}",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(
                calendar_recovery_activity=_backdate_calendar_recovery_for_reminder,
                reminder_activity=_crash_after_handoff_reminder_commit,
            ),
        ):
            with env.auto_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    RegistrationWorkflow.run,
                    RegChildInput(
                        tenant_id=str(tenant_id),
                        canonical_event_id=str(canonical.canonical_event_id),
                        keep_open_after_scheduling=True,
                    ),
                    id=workflow_id,
                    task_queue=f"ec-handoff-reminder-retry-{tag}",
                )
                for _ in range(500):
                    if _handoff_reminder_activity_recorder.attempts == [1, 2]:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("due handoff reminder did not retry after its simulated ACK loss")

                assert _handoff_reminder_activity_recorder.statuses == [
                    "enqueued",
                    "already_enqueued",
                ]
                assert await _handoff_reminder_outbox_effects(tenant_id, task_id) == (1, 1, "t24h")

                # End the retained child through an organizer cancellation, which also retires
                # the recovery task before its later TTL and prevents a T+5 follow-up.
                await handle.signal(
                    "organizer_change",
                    OrganizerChangeSignal(
                        fingerprint=f"handoff-reminder-cancel-{tag}",
                        canonical_event_id=str(canonical.canonical_event_id),
                        source=Source.MEETUP.value,
                        event_status="cancelled",
                    ),
                )
                child_result = await handle.result()

    assert child_result.status == "cancelled"
    assert source.registration_effects == 1
    assert await _handoff_reminder_outbox_effects(tenant_id, task_id) == (1, 1, "t24h")


async def test_retained_handoff_mark_done_buffers_organizer_command_through_lost_ack(
    db: None,
) -> None:
    """A post-commit organizer command is journaled before the completion activity ACK settles."""
    _handoff_completion_activity_recorder.attempts.clear()
    _handoff_completion_activity_recorder.statuses.clear()
    _handoff_completion_activity_recorder.crashed = False
    _handoff_completion_activity_recorder.entered = asyncio.Event()
    _handoff_completion_activity_recorder.release = asyncio.Event()
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP, membership_state=GroupCondition.NON_MEMBER)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.NON_MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|handoff-completion-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    start_at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"handoff-completion-{tag}",
                    title=f"Handoff completion {tag}",
                    start_at=start_at,
                    end_at=start_at + timedelta(hours=2),
                    registration_url=f"https://meetup.example/handoff-completion-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    task_id = f"{workflow_id}:handoff"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=f"ec-handoff-completion-{tag}",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(
                confirmation_activity=_crash_after_handoff_completion_commit,
            ),
        ):
            with env.auto_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    RegistrationWorkflow.run,
                    RegChildInput(
                        tenant_id=str(tenant_id),
                        canonical_event_id=str(canonical.canonical_event_id),
                        keep_open_after_scheduling=True,
                    ),
                    id=workflow_id,
                    task_queue=f"ec-handoff-completion-{tag}",
                )
                await _wait_for_retained_handoff_state(
                    container,
                    tenant_id,
                    canonical.canonical_event_id,
                    task_id,
                    lifecycle_state=LifecycleState.HANDOFF,
                    failure="retained child did not create its registration handoff",
                )

                await source.register(
                    tenant_id,
                    RegistrationTarget(
                        canonical.source_links[0].source_event_id,
                        canonical.source_links[0].registration_url,
                    ),
                    Modality.API,
                    f"outside-concierge-{tag}",
                )
                completion_id = f"{workflow_id}:handoff-completion:{task_id}:1"
                await handle.signal(
                    "handoff_completed",
                    HandoffCompletionSignal(
                        task_id=task_id,
                        completion_id=completion_id,
                    ),
                )
                entered = _handoff_completion_activity_recorder.entered
                release = _handoff_completion_activity_recorder.release
                if entered is None or release is None:
                    raise RuntimeError("handoff completion ACK-loss gate was not configured")
                await asyncio.wait_for(entered.wait(), timeout=2)
                cancellation_fingerprint = f"handoff-completion-cancel-{tag}"
                await handle.signal(
                    "organizer_change",
                    OrganizerChangeSignal(
                        fingerprint=cancellation_fingerprint,
                        canonical_event_id=str(canonical.canonical_event_id),
                        source=Source.MEETUP.value,
                        event_status="cancelled",
                    ),
                )
                for _ in range(400):
                    pending = await handle.query(
                        "pending_lifecycle_signals",
                        result_type=PendingLifecycleSignals,
                    )
                    if any(
                        signal.fingerprint == cancellation_fingerprint
                        for signal in pending.organizer_changes
                    ):
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("post-commit organizer command was not retained")
                release.set()
                child_result = await handle.result()

    assert child_result.status == "cancelled"
    assert source.registration_effects == 1
    assert _handoff_completion_activity_recorder.attempts == [1]
    assert _handoff_completion_activity_recorder.statuses == ["confirmed"]
    _handoff_completion_activity_recorder.entered = None
    _handoff_completion_activity_recorder.release = None


async def test_retained_handoff_dependency_failure_uses_interruptible_workflow_backoff(
    db: None,
) -> None:
    """Retries are one-attempt, interruptible, and exponentially bounded in workflow history."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|handoff-completion-outage-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"handoff-completion-outage-{tag}",
                    title=f"Handoff completion outage {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://meetup.example/handoff-completion-outage-{tag}",
                    city="San Francisco",
                    # Paid candidates route directly to a retained task without source effects.
                    is_free=False,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    task_id = f"{workflow_id}:handoff"
    first_completion_id = f"{workflow_id}:completion:first"
    second_completion_id = f"{workflow_id}:completion:second"
    recorder = _persistent_handoff_completion_failure
    recorder.completion_ids.clear()
    recorder.activity_attempts.clear()
    recorder.scheduled_at.clear()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=f"ec-handoff-completion-outage-{tag}",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(
                confirmation_activity=_persistently_failing_handoff_confirmation,
            ),
        ):
            with env.auto_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    RegistrationWorkflow.run,
                    RegChildInput(
                        tenant_id=str(tenant_id),
                        canonical_event_id=str(canonical.canonical_event_id),
                        keep_open_after_scheduling=True,
                    ),
                    id=workflow_id,
                    task_queue=f"ec-handoff-completion-outage-{tag}",
                )
                await _wait_for_retained_handoff_state(
                    container,
                    tenant_id,
                    canonical.canonical_event_id,
                    task_id,
                    failure="retained child did not create its handoff task",
                )

                await handle.signal(
                    "handoff_completed",
                    HandoffCompletionSignal(
                        task_id=task_id,
                        completion_id=first_completion_id,
                    ),
                )
                await _wait_for_persistent_completion_probes(
                    [first_completion_id],
                    "first handoff verification probe did not fail",
                )

                pending = await handle.query(
                    "pending_lifecycle_signals",
                    result_type=PendingLifecycleSignals,
                )
                assert pending.handoff_completions == [
                    HandoffCompletionSignal(
                        task_id=task_id,
                        completion_id=first_completion_id,
                    )
                ]

                # No hidden activity retry may run before the workflow's one-minute timer.
                await env.sleep(timedelta(seconds=30))
                await asyncio.sleep(0.05)
                assert recorder.completion_ids == [first_completion_id]
                await env.sleep(timedelta(seconds=31))
                await _wait_for_persistent_completion_probes(
                    [
                        first_completion_id,
                        first_completion_id,
                    ],
                    "workflow-owned handoff verification retry did not run",
                )

                assert recorder.activity_attempts == [1, 1]
                assert (
                    recorder.scheduled_at[1] - recorder.scheduled_at[0]
                    >= timedelta(minutes=1)
                )

                # The second failure doubles the workflow-owned floor to two minutes. This keeps
                # a seven-day task to a bounded number of history events even during an outage.
                await env.sleep(timedelta(seconds=60))
                await asyncio.sleep(0.05)
                assert recorder.completion_ids == [
                    first_completion_id,
                    first_completion_id,
                ]
                await env.sleep(timedelta(seconds=61))
                await _wait_for_persistent_completion_probes(
                    [
                        first_completion_id,
                        first_completion_id,
                        first_completion_id,
                    ],
                    "exponentially backed-off handoff verification retry did not run",
                )
                assert (
                    recorder.scheduled_at[2] - recorder.scheduled_at[1]
                    >= timedelta(minutes=2)
                )

                # A new exact command wakes the retained child during the first command's next
                # backoff. It receives its own bounded probe instead of waiting behind an activity.
                await handle.signal(
                    "handoff_completed",
                    HandoffCompletionSignal(
                        task_id=task_id,
                        completion_id=second_completion_id,
                    ),
                )
                await _wait_for_persistent_completion_probes(
                    [
                        first_completion_id,
                        first_completion_id,
                        first_completion_id,
                        second_completion_id,
                    ],
                    "new completion command did not interrupt workflow backoff",
                )
                pending = await handle.query(
                    "pending_lifecycle_signals",
                    result_type=PendingLifecycleSignals,
                )
                assert [item.completion_id for item in pending.handoff_completions] == [
                    first_completion_id,
                    second_completion_id,
                ]
                assert recorder.activity_attempts == [1, 1, 1, 1]
                await handle.cancel()

    task = await container.handoff_repo.get(tenant_id, task_id)
    assert task is not None and task.state in {HandoffState.OPEN, HandoffState.NOTIFIED}
    assert source.registration_effects == 0
    assert container.calendar.entries(tenant_id) == []


async def test_persistent_handoff_completion_outage_cannot_extend_task_ttl(db: None) -> None:
    """A completion arriving at the deadline gets one probe, then guarded expiry settles the task."""
    settings = get_settings().model_copy(update={"handoff_ttl_days": 0})
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|handoff-completion-expiry-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"handoff-completion-expiry-{tag}",
                    title=f"Handoff completion expiry {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://meetup.example/handoff-completion-expiry-{tag}",
                    city="San Francisco",
                    is_free=False,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    task_id = f"{workflow_id}:handoff"
    completion_id = f"{workflow_id}:completion:at-expiry"
    recorder = _persistent_handoff_completion_failure
    recorder.completion_ids.clear()
    recorder.activity_attempts.clear()
    recorder.scheduled_at.clear()
    _blocked_handoff_route.entered = asyncio.Event()
    _blocked_handoff_route.release = asyncio.Event()

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-handoff-completion-expiry-{tag}",
                workflows=[RegistrationWorkflow],
                activities=_saga_activities(
                    confirmation_activity=_persistently_failing_handoff_confirmation,
                    handoff_activity=_blocked_route_to_handoff,
                ),
            ):
                with env.auto_time_skipping_disabled():
                    handle = await env.client.start_workflow(
                        RegistrationWorkflow.run,
                        RegChildInput(
                            tenant_id=str(tenant_id),
                            canonical_event_id=str(canonical.canonical_event_id),
                            keep_open_after_scheduling=True,
                        ),
                        id=workflow_id,
                        task_queue=f"ec-handoff-completion-expiry-{tag}",
                    )
                    entered = _blocked_handoff_route.entered
                    release = _blocked_handoff_route.release
                    if entered is None or release is None:
                        raise RuntimeError("handoff route gate was not configured")
                    await asyncio.wait_for(entered.wait(), timeout=2)
                    await handle.signal(
                        "handoff_completed",
                        HandoffCompletionSignal(
                            task_id=task_id,
                            completion_id=completion_id,
                        ),
                    )
                    release.set()
                    child_result = await handle.result()
    finally:
        release = _blocked_handoff_route.release
        if release is not None:
            release.set()
        _blocked_handoff_route.entered = None
        _blocked_handoff_route.release = None

    assert child_result.status == "expired"
    assert recorder.completion_ids == [completion_id]
    assert recorder.activity_attempts == [1]
    task = await container.handoff_repo.get(tenant_id, task_id)
    assert task is not None and task.state is HandoffState.EXPIRED
    assert await _lifecycle_state(tenant_id, workflow_id) == LifecycleState.EXPIRED.value
    assert source.registration_effects == 0
    assert container.calendar.entries(tenant_id) == []


async def test_verified_handoff_ack_loss_at_ttl_recovers_receipt_instead_of_expiring(
    db: None,
) -> None:
    """A completed task is durable proof that expiry must yield to exact receipt recovery."""
    settings = get_settings().model_copy(update={"handoff_ttl_days": 0})
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP, membership_state=GroupCondition.NON_MEMBER)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.NON_MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|handoff-completion-ttl-ack-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"handoff-completion-ttl-ack-{tag}",
                    title=f"Handoff completion TTL ACK {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    end_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2, hours=2),
                    registration_url=f"https://meetup.example/handoff-completion-ttl-ack-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    task_id = f"{workflow_id}:handoff"
    await source.register(
        tenant_id,
        RegistrationTarget(
            canonical.source_links[0].source_event_id,
            canonical.source_links[0].registration_url,
        ),
        Modality.API,
        f"outside-concierge-ttl-{tag}",
    )
    recorder = _handoff_completion_activity_recorder
    recorder.attempts.clear()
    recorder.statuses.clear()
    recorder.crashed = False
    recorder.entered = None
    recorder.release = None
    _blocked_handoff_route.entered = asyncio.Event()
    _blocked_handoff_route.release = asyncio.Event()

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-handoff-completion-ttl-ack-{tag}",
                workflows=[RegistrationWorkflow],
                activities=_saga_activities(
                    confirmation_activity=_crash_after_handoff_completion_commit,
                    handoff_activity=_blocked_route_to_handoff,
                ),
            ):
                with env.auto_time_skipping_disabled():
                    handle = await env.client.start_workflow(
                        RegistrationWorkflow.run,
                        RegChildInput(
                            tenant_id=str(tenant_id),
                            canonical_event_id=str(canonical.canonical_event_id),
                            keep_open_after_scheduling=True,
                        ),
                        id=workflow_id,
                        task_queue=f"ec-handoff-completion-ttl-ack-{tag}",
                    )
                    entered = _blocked_handoff_route.entered
                    release = _blocked_handoff_route.release
                    if entered is None or release is None:
                        raise RuntimeError("handoff route gate was not configured")
                    await asyncio.wait_for(entered.wait(), timeout=2)
                    await handle.signal(
                        "handoff_completed",
                        HandoffCompletionSignal(
                            task_id=task_id,
                            completion_id=f"{workflow_id}:completion:ttl-ack",
                        ),
                    )
                    release.set()
                    await _wait_for_retained_handoff_state(
                        container,
                        tenant_id,
                        canonical.canonical_event_id,
                        task_id,
                        task_state=HandoffState.COMPLETED,
                        lifecycle_state=LifecycleState.SCHEDULED,
                        failure="TTL-edge completion receipt did not recover into scheduling",
                    )
                    await handle.signal(
                        "organizer_change",
                        OrganizerChangeSignal(
                            fingerprint=f"handoff-completion-ttl-cancel-{tag}",
                            canonical_event_id=str(canonical.canonical_event_id),
                            source=Source.MEETUP.value,
                            event_status="cancelled",
                        ),
                    )
                    child_result = await handle.result()
    finally:
        release = _blocked_handoff_route.release
        if release is not None:
            release.set()
        _blocked_handoff_route.entered = None
        _blocked_handoff_route.release = None

    assert child_result.status == "cancelled"
    assert recorder.attempts == [1, 1]
    assert recorder.statuses == ["confirmed", "confirmed"]
    task = await container.handoff_repo.get(tenant_id, task_id)
    assert task is not None and task.state is HandoffState.COMPLETED
    assert await _lifecycle_state(tenant_id, workflow_id) == LifecycleState.CANCELLED.value
    assert "lifecycle.expired" not in await _outbox_topic_counts(tenant_id)


async def test_parent_routes_projected_meetup_saturation_to_one_handoff_without_fallthrough(
    db: None,
) -> None:
    """A P1d ``degrade`` ahead of wire mutation ends the request at a SATURATION handoff.

    Two otherwise-autonomous candidates make the assertion load-bearing: the parent must not
    start candidate two after candidate one's fair-share queue projects more than five minutes.
    No RSVP or calendar effect may occur before that terminal compensation (ADR-003/005).
    """
    settings = get_settings()
    tag = uuid4().hex
    events: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=events)
    pacer = DegradeFirstMutationPacer(events)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        pacer=pacer,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|saturation-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    candidates = await container.catalog.upsert_candidates(
        [
            CandidateEvent(
                source=Source.MEETUP,
                source_event_id=f"saturation-first-{tag}",
                title=f"Saturation first meetup {tag}",
                start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                registration_url="https://meetup.example/saturation-first",
                city="New York",
                is_free=True,
            ),
            CandidateEvent(
                source=Source.MEETUP,
                source_event_id=f"saturation-second-{tag}",
                title=f"Saturation second meetup {tag}",
                start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=3),
                registration_url="https://meetup.example/saturation-second",
                city="New York",
                is_free=True,
            ),
        ]
    )
    first, second = candidates
    _forced_candidate_ids[:] = [
        str(first.canonical_event_id),
        str(second.canonical_event_id),
    ]

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="ec-saga-pacer-degrade",
                workflows=[EventRequestWorkflow, RegistrationWorkflow],
                activities=_saga_activities(discovery_activity=_fixed_discover_and_rank),
            ):
                result = await env.client.execute_workflow(
                    EventRequestWorkflow.run,
                    RequestInput(
                        tenant_id=str(tenant_id),
                        request_id=str(uuid4()),
                        attempt_budget=2,
                    ),
                    id=f"req-saturation-{tag}",
                    task_queue="ec-saga-pacer-degrade",
                )
    finally:
        _forced_candidate_ids.clear()

    async with tenant_session_scope(tenant_id) as session:
        handoff_rows = (
            await session.execute(
                text(
                    """SELECT canonical_event_id, reason FROM handoff_tasks
                       WHERE tenant_id = :tenant_id ORDER BY task_id"""
                ),
                {"tenant_id": tenant_id},
            )
        ).all()

    assert result.outcome == "handoff"
    assert result.attempts == 1
    assert pacer.degrade_count == 1
    assert pacer.operations == [
        PacerOperation.MEMBERSHIP_READ,
        PacerOperation.REGISTRATION_READ,
        PacerOperation.REGISTRATION_MUTATION,
    ]
    assert [(row.canonical_event_id, str(row.reason)) for row in handoff_rows] == [
        (first.canonical_event_id, HandoffReason.SATURATION.value)
    ]
    assert source.registration_effects == 0
    assert source.register_attempts == 0
    assert source.idempotency_keys == []
    assert container.calendar.entries(tenant_id) == []
    assert await _outbox_topic_counts(tenant_id) == {"lifecycle.handoff": 1}


async def test_parent_routes_browser_pool_saturation_before_luma_detect_or_candidate_fallthrough(
    db: None,
) -> None:
    """A full P1e browser pool routes one Luma candidate directly to SATURATION handoff (AC-45).

    The fixture driver makes this stronger than a policy-only test: neither the first candidate's
    browser detection/submission nor the second candidate can start when admission is unavailable.
    """
    settings = get_settings()
    tag = uuid4().hex
    driver = ScriptedBrowserRsvpDriver(
        {
            f"browser-saturation-first-{tag}": ScriptedBrowserScenario(
                initial_detection=BrowserRsvpObservation(BrowserRsvpStatus.NOT_PRESENT, 0),
                after_submit=BrowserRsvpObservation(BrowserRsvpStatus.CONFIRMED),
            ),
            f"browser-saturation-second-{tag}": ScriptedBrowserScenario(
                initial_detection=BrowserRsvpObservation(BrowserRsvpStatus.NOT_PRESENT, 0),
                after_submit=BrowserRsvpObservation(BrowserRsvpStatus.CONFIRMED),
            ),
        }
    )
    admission = SaturatedBrowserAdmission()
    container = build_container(
        settings,
        register_sources={Source.LUMA: LumaSource(driver)},
        browser_admission=admission,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|browser-saturation-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.LUMA, Modality.BROWSER)
    candidates = await container.catalog.upsert_candidates(
        [
            CandidateEvent(
                source=Source.LUMA,
                source_event_id=f"browser-saturation-first-{tag}",
                title=f"Browser saturation first Luma {tag}",
                start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                registration_url=f"https://luma.fixture/browser-saturation-first-{tag}",
                city="San Francisco",
                is_free=True,
            ),
            CandidateEvent(
                source=Source.LUMA,
                source_event_id=f"browser-saturation-second-{tag}",
                title=f"Browser saturation second Luma {tag}",
                start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=3),
                registration_url=f"https://luma.fixture/browser-saturation-second-{tag}",
                city="San Francisco",
                is_free=True,
            ),
        ]
    )
    first, second = candidates
    _forced_candidate_ids[:] = [
        str(first.canonical_event_id),
        str(second.canonical_event_id),
    ]

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="ec-saga-browser-saturation",
                workflows=[EventRequestWorkflow, RegistrationWorkflow],
                activities=_saga_activities(discovery_activity=_fixed_discover_and_rank),
            ):
                result = await env.client.execute_workflow(
                    EventRequestWorkflow.run,
                    RequestInput(
                        tenant_id=str(tenant_id),
                        request_id=str(uuid4()),
                        attempt_budget=2,
                    ),
                    id=f"req-browser-saturation-{tag}",
                    task_queue="ec-saga-browser-saturation",
                )
    finally:
        _forced_candidate_ids.clear()

    async with tenant_session_scope(tenant_id) as session:
        handoff_rows = (
            await session.execute(
                text(
                    """SELECT canonical_event_id, reason FROM handoff_tasks
                       WHERE tenant_id = :tenant_id ORDER BY task_id"""
                ),
                {"tenant_id": tenant_id},
            )
        ).all()

    assert result.outcome == "handoff"
    assert result.attempts == 1
    assert len(admission.requests) == 1
    assert admission.requests[0].source is Source.LUMA
    assert admission.releases == []
    assert [(row.canonical_event_id, str(row.reason)) for row in handoff_rows] == [
        (first.canonical_event_id, HandoffReason.SATURATION.value)
    ]
    assert driver.detect_calls == 0
    assert driver.submit_calls == 0
    assert driver.idempotency_keys == []
    assert container.calendar.entries(tenant_id) == []
    assert await _outbox_topic_counts(tenant_id) == {"lifecycle.handoff": 1}


async def test_luma_lost_ack_releases_each_browser_lease_and_never_double_submits(db: None) -> None:
    """Crash recovery holds no slot across retries and reuses the minted read identity (ADR-003)."""
    settings = get_settings()
    tag = uuid4().hex
    source_event_id = f"browser-lost-ack-{tag}"
    driver = ScriptedBrowserRsvpDriver(
        {
            source_event_id: ScriptedBrowserScenario(
                initial_detection=BrowserRsvpObservation(BrowserRsvpStatus.NOT_PRESENT, 0),
                after_submit=BrowserRsvpObservation(BrowserRsvpStatus.CONFIRMED),
                raise_after_effect_once=True,
            )
        }
    )
    admission = RecordingBrowserAdmission()
    container = build_container(
        settings,
        register_sources={Source.LUMA: LumaSource(driver)},
        browser_admission=admission,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|browser-lost-ack-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.LUMA, Modality.BROWSER)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.LUMA,
                    source_event_id=source_event_id,
                    title=f"Browser lost acknowledgement Luma {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://luma.fixture/{source_event_id}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-browser-lost-ack",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            result = await env.client.execute_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-browser-lost-ack",
            )

    assert result.status == "registered"
    assert driver.submit_calls == 1
    assert len(driver.idempotency_keys) == 1
    assert len(container.calendar.entries(tenant_id)) == 1
    # initial read, mutation (whose ACK is lost), and recovery read. The recovery reuses the
    # durable read identity but every physical holder gets a new fence and a matching cleanup.
    assert len(admission.requests) == 3
    assert len(admission.releases) == 3
    assert admission.requests[0].lease_id == admission.requests[2].lease_id
    assert admission.requests[0].lease_id != admission.requests[1].lease_id
    assert [lease.lease_id for lease in admission.releases] == [
        request.lease_id for request in admission.requests
    ]
    assert [lease.fence_token for lease in admission.releases] == [
        request.fence_token for request in admission.requests
    ]


async def test_luma_pacer_wait_releases_browser_admission_before_the_durable_timer(
    db: None,
) -> None:
    """A source-rate wait cannot hold a physical browser slot while Temporal is parked (AC-45)."""
    settings = get_settings()
    tag = uuid4().hex
    source_event_id = f"browser-pacer-wait-{tag}"
    events: list[str] = []
    driver = ScriptedBrowserRsvpDriver(
        {
            source_event_id: ScriptedBrowserScenario(
                initial_detection=BrowserRsvpObservation(BrowserRsvpStatus.NOT_PRESENT, 0),
                after_submit=BrowserRsvpObservation(BrowserRsvpStatus.CONFIRMED),
            )
        }
    )
    admission = RecordingBrowserAdmission()
    # Luma has no membership Pacer call, so the first acquisition is its read-only browser detect.
    pacer = WaitThenGrantPacer(events, wait_on_call=1, seconds=1.0)
    container = build_container(
        settings,
        register_sources={Source.LUMA: LumaSource(driver)},
        pacer=pacer,
        browser_admission=admission,
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|browser-pacer-wait-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.LUMA, Modality.BROWSER)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.LUMA,
                    source_event_id=source_event_id,
                    title=f"Browser Pacer wait Luma {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://luma.fixture/{source_event_id}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="ec-saga-browser-pacer-wait",
            workflows=[RegistrationWorkflow],
            activities=_saga_activities(),
        ):
            handle = await env.client.start_workflow(
                RegistrationWorkflow.run,
                RegChildInput(
                    tenant_id=str(tenant_id), canonical_event_id=str(canonical.canonical_event_id)
                ),
                id=workflow_id,
                task_queue="ec-saga-browser-pacer-wait",
            )
            with env.auto_time_skipping_disabled():
                for _ in range(100):
                    if pacer.waits == 1:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("browser registration did not receive the Pacer wait projection")

                assert driver.detect_calls == 0
                assert driver.submit_calls == 0
                assert len(admission.requests) == 1
                assert len(admission.releases) == 1
                await env.sleep(1.0)
            result = await handle.result()

    assert result.status == "registered"
    assert driver.submit_calls == 1
    assert len(admission.requests) == 3
    assert len(admission.releases) == 3
    assert admission.requests[0].lease_id == admission.requests[1].lease_id
    assert admission.requests[1].lease_id != admission.requests[2].lease_id


async def test_parent_zero_discovery_terminalizes_request_and_emits_one_no_result(
    db: None,
) -> None:
    """An empty ranked set leaves one request terminal/outbox effect (FR-5.0/6.6, ADR-003/007)."""
    settings = get_settings()
    tag = uuid4().hex
    container = build_container(settings)
    set_container(container)
    tenant_id = uuid4()
    request_id = uuid4()
    workflow_id = request_workflow_id(tenant_id, request_id)
    transition_id = f"{workflow_id}:failed-no-candidate:1"
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|zero-discovery-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await container.request_repo.add(
        EventRequest(
            request_id=request_id,
            tenant_id=tenant_id,
            raw_text="no matching fixture events",
            constraints=RequestConstraints(categories=("music",)),
        )
    )
    _forced_candidate_ids.clear()

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-zero-discovery-{tag}",
                workflows=[EventRequestWorkflow],
                activities=_saga_activities(discovery_activity=_fixed_discover_and_rank),
            ):
                result = await env.client.execute_workflow(
                    EventRequestWorkflow.run,
                    RequestInput(
                        tenant_id=str(tenant_id),
                        request_id=str(request_id),
                        attempt_budget=3,
                    ),
                    id=workflow_id,
                    task_queue=f"ec-zero-discovery-{tag}",
                )
    finally:
        _forced_candidate_ids.clear()

    async with tenant_session_scope(tenant_id) as session:
        request_state = (
            await session.execute(
                text("SELECT state FROM event_requests WHERE request_id = :request_id"),
                {"request_id": request_id},
            )
        ).scalar_one()
        outbox_rows = (
            await session.execute(
                text(
                    """SELECT topic, payload ->> 'transition_id' AS transition_id,
                              payload ->> 'request_id' AS request_id
                       FROM outbox
                       WHERE tenant_id = :tenant_id
                         AND topic = 'request.failed_no_candidate'
                         AND payload ->> 'transition_id' = :transition_id"""
                ),
                {"tenant_id": tenant_id, "transition_id": transition_id},
            )
        ).all()
        lifecycle_count = (
            await session.execute(
                text("SELECT count(*) AS n FROM lifecycle WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
        ).scalar_one()

    assert result.outcome == LifecycleState.FAILED_NO_CANDIDATE.value
    assert result.attempts == 0
    assert request_state == LifecycleState.FAILED_NO_CANDIDATE.value
    assert [(row.topic, row.transition_id, row.request_id) for row in outbox_rows] == [
        ("request.failed_no_candidate", transition_id, str(request_id))
    ]
    assert int(lifecycle_count) == 0


async def test_parent_demotes_the_best_handoff_eligible_failed_candidate(db: None) -> None:
    """The terminal task belongs to the highest-ranked eligible failure, not simply the last one.

    Both child attempts fail their autonomous work.  Only the first (higher-ranked) child has a
    handoff fallback, so it must remain parked until the parent has seen the second failure and
    then receive ``demote_to_handoff``; the second is safely terminalized without a user notice
    (FR-5.0, AC-33, ADR-003/007).
    """
    settings = get_settings()
    tag = uuid4().hex
    container = build_container(settings)
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|best-handoff-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    candidates = await container.catalog.upsert_candidates(
        [
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id=f"best-handoff-first-{tag}",
                title=f"Best handoff first {tag}",
                start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                registration_url=f"https://events.example/best-handoff-first-{tag}",
                city="San Francisco",
                is_free=True,
            ),
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id=f"best-handoff-second-{tag}",
                title=f"Best handoff second {tag}",
                start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=3),
                registration_url=f"https://events.example/best-handoff-second-{tag}",
                city="San Francisco",
                is_free=True,
            ),
        ]
    )
    first, second = candidates
    _forced_candidate_ids[:] = [str(first.canonical_event_id), str(second.canonical_event_id)]
    _forced_membership_plans.update(
        {
            str(first.canonical_event_id): [Lane.HANDOFF.value],
            str(second.canonical_event_id): [],
        }
    )

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-best-handoff-{tag}",
                workflows=[EventRequestWorkflow, RegistrationWorkflow],
                activities=_saga_activities(
                    discovery_activity=_fixed_discover_and_rank,
                    membership_activity=_fixed_resolve_membership,
                ),
            ):
                with env.auto_time_skipping_disabled():
                    result = await env.client.execute_workflow(
                        EventRequestWorkflow.run,
                        RequestInput(
                            tenant_id=str(tenant_id),
                            request_id=str(uuid4()),
                            attempt_budget=2,
                        ),
                        id=f"req-best-handoff-{tag}",
                        task_queue=f"ec-best-handoff-{tag}",
                    )
    finally:
        _forced_candidate_ids.clear()
        _forced_membership_plans.clear()

    first_workflow_id = registration_workflow_id(tenant_id, first.canonical_event_id)
    second_workflow_id = registration_workflow_id(tenant_id, second.canonical_event_id)
    first_lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id, first.canonical_event_id, first_workflow_id
    )
    second_lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id, second.canonical_event_id, second_workflow_id
    )
    task = await container.handoff_repo.get(tenant_id, f"{first_workflow_id}:handoff")

    assert result.outcome == "handoff"
    assert result.attempts == 2
    assert first_lifecycle.state is LifecycleState.HANDOFF
    assert second_lifecycle.state is LifecycleState.FAILED_NO_CANDIDATE
    assert task is not None and task.workflow_id == first_workflow_id
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.handoff": 1,
        "lifecycle.failed_no_candidate": 1,
    }


async def test_parent_terminalizes_request_after_noneligible_failed_child(db: None) -> None:
    """A declined child closes first; the parent then emits the sole user-visible no-result (AC-33)."""
    settings = get_settings()
    tag = uuid4().hex
    container = build_container(settings)
    set_container(container)
    tenant_id, request_id = uuid4(), uuid4()
    workflow_id = request_workflow_id(tenant_id, request_id)
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|no-eligible-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    candidate = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.PUBLIC_JSONLD,
                    source_event_id=f"no-eligible-{tag}",
                    title=f"No eligible handoff {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://events.example/no-eligible-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    await container.request_repo.add(
        EventRequest(
            request_id=request_id,
            tenant_id=tenant_id,
            raw_text="no eligible candidate fixture",
            constraints=RequestConstraints(categories=("music",)),
        )
    )
    _forced_candidate_ids[:] = [str(candidate.canonical_event_id)]
    _forced_membership_plans[str(candidate.canonical_event_id)] = []

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-no-eligible-{tag}",
                workflows=[EventRequestWorkflow, RegistrationWorkflow],
                activities=_saga_activities(
                    discovery_activity=_fixed_discover_and_rank,
                    membership_activity=_fixed_resolve_membership,
                ),
            ):
                result = await env.client.execute_workflow(
                    EventRequestWorkflow.run,
                    RequestInput(
                        tenant_id=str(tenant_id),
                        request_id=str(request_id),
                        attempt_budget=1,
                    ),
                    id=workflow_id,
                    task_queue=f"ec-no-eligible-{tag}",
                )
    finally:
        _forced_candidate_ids.clear()
        _forced_membership_plans.clear()

    lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id,
        candidate.canonical_event_id,
        registration_workflow_id(tenant_id, candidate.canonical_event_id),
    )
    async with tenant_session_scope(tenant_id) as session:
        request_state = (
            await session.execute(
                text("SELECT state FROM event_requests WHERE request_id = :request_id"),
                {"request_id": request_id},
            )
        ).scalar_one()
    assert result.outcome == LifecycleState.FAILED_NO_CANDIDATE.value
    assert result.attempts == 1
    assert lifecycle.state is LifecycleState.FAILED_NO_CANDIDATE
    assert request_state == LifecycleState.FAILED_NO_CANDIDATE.value
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.failed_no_candidate": 1,
        "request.failed_no_candidate": 1,
    }


async def test_parent_returns_while_scheduled_child_accepts_unrsvp(db: None) -> None:
    """The parent returns while the child executes one deduplicated un-RSVP recovery (FR-8.8)."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        withdrawal_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|directive-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    candidates = await container.catalog.upsert_candidates(
        [
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id=f"directive-crawl-{tag}",
                title=f"Directive crawl {tag}",
                start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                registration_url="https://events.example/directive-crawl",
                city="New York",
                is_free=True,
            ),
            CandidateEvent(
                source=Source.MEETUP,
                source_event_id=f"directive-meetup-{tag}",
                title=f"Directive meetup {tag}",
                start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=3),
                registration_url="https://meetup.example/directive-meetup",
                city="New York",
                is_free=True,
            ),
        ]
    )
    _forced_candidate_ids[:] = [str(candidate.canonical_event_id) for candidate in candidates]
    unrsvp = UnrsvpSignal(request_id=f"fixture-unrsvp-{tag}")

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="ec-directive",
                workflows=[EventRequestWorkflow, RegistrationWorkflow],
                activities=_saga_activities(discovery_activity=_fixed_discover_and_rank),
            ):
                # The test server auto-advances an otherwise idle child to its execution timeout.
                # Keep virtual time frozen after the parent returns so this test exercises the
                # production signal path rather than that test-only clock behavior.
                with env.auto_time_skipping_disabled():
                    result = await env.client.execute_workflow(
                        EventRequestWorkflow.run,
                        RequestInput(
                            tenant_id=str(tenant_id),
                            request_id=str(uuid4()),
                            attempt_budget=2,
                        ),
                        id=f"req-directive-{tag}",
                        task_queue="ec-directive",
                    )
                    lifecycle_handle = env.client.get_workflow_handle(
                        registration_workflow_id(tenant_id, candidates[1].canonical_event_id)
                    )
                    await lifecycle_handle.signal("unrsvp_requested", unrsvp)
                    lifecycle_result = RegChildResult(**(await lifecycle_handle.result()))
    finally:
        _forced_candidate_ids.clear()

    assert result.outcome == "registered"
    assert result.attempts == 2
    assert lifecycle_result.status == "cancelled"
    assert source.registration_effects == 1
    assert source.withdrawal_effects == 1
    assert source.withdraw_attempts == 1
    assert isinstance(container.calendar, MockCalendar)
    assert container.calendar.entries(tenant_id) == []
    async with tenant_session_scope(tenant_id) as session:
        state = (
            (
                await session.execute(
                    text("SELECT state FROM lifecycle WHERE workflow_id = :workflow_id"),
                    {
                        "workflow_id": registration_workflow_id(
                            tenant_id, candidates[1].canonical_event_id
                        )
                    },
                )
            )
            .one()
            .state
        )
        closed_first_state = (
            (
                await session.execute(
                    text("SELECT state FROM lifecycle WHERE workflow_id = :workflow_id"),
                    {
                        "workflow_id": registration_workflow_id(
                            tenant_id, candidates[0].canonical_event_id
                        )
                    },
                )
            )
            .one()
            .state
        )
    assert state == LifecycleState.CANCELLED.value
    assert closed_first_state == LifecycleState.FAILED_NO_CANDIDATE.value
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.failed_no_candidate": 1,
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
        "lifecycle.withdrawing": 1,
        "lifecycle.cancelled": 1,
    }


async def test_registered_change_buffers_through_confirmation_persistence_while_early_unrsvp_is_dropped(
    db: None,
) -> None:
    """A detector change survives confirmation→REGISTERED→SCHEDULED; early un-RSVP never queues."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        withdrawal_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|registered-race-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    start_at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"registered-race-{tag}",
                    title=f"Registered signal race {tag}",
                    start_at=start_at,
                    end_at=start_at + timedelta(hours=2),
                    registration_url=f"https://meetup.example/registered-race-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    _blocked_confirmation.entered = asyncio.Event()
    _blocked_confirmation.release = asyncio.Event()

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-registered-race-{tag}",
                workflows=[RegistrationWorkflow],
                activities=_saga_activities(confirmation_activity=_blocked_await_confirmation),
            ):
                with env.auto_time_skipping_disabled():
                    handle = await env.client.start_workflow(
                        RegistrationWorkflow.run,
                        RegChildInput(
                            tenant_id=str(tenant_id),
                            canonical_event_id=str(canonical.canonical_event_id),
                            keep_open_after_scheduling=True,
                        ),
                        id=workflow_id,
                        task_queue=f"ec-registered-race-{tag}",
                    )
                    await asyncio.wait_for(_blocked_confirmation.entered.wait(), timeout=2)
                    await handle.signal(
                        "unrsvp_requested", UnrsvpSignal(request_id=f"early-unrsvp-{tag}")
                    )
                    await handle.signal(
                        "organizer_change",
                        OrganizerChangeSignal(
                            fingerprint=f"registered-cancel-{tag}",
                            canonical_event_id=str(canonical.canonical_event_id),
                            source=Source.MEETUP.value,
                            event_status="cancelled",
                        ),
                    )
                    _blocked_confirmation.release.set()
                    result = await handle.result()
    finally:
        _blocked_confirmation.entered = None
        _blocked_confirmation.release = None

    assert result.status == "cancelled"
    assert source.withdrawal_effects == 0
    assert source.withdraw_attempts == 0
    assert await _lifecycle_state(tenant_id, workflow_id) == LifecycleState.CANCELLED.value
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
        "lifecycle.cancelled": 1,
    }


async def test_retained_handoff_timer_retries_a_lost_expiry_ack_once(db: None) -> None:
    """A 0-day retained handoff expires through one guarded effect after an activity ACK loss.

    The zero-day fixture makes the real PostgreSQL due check true without relying on Temporal test
    time to advance the database clock.  The first expiry activity commits, then crashes before its
    acknowledgement; Temporal retries it with the same workflow-minted task transition identity.
    """
    settings = get_settings().model_copy(update={"handoff_ttl_days": 0})
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|handoff-expiry-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"handoff-expiry-{tag}",
                    title=f"Handoff expiry retry {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://meetup.example/handoff-expiry-{tag}",
                    city="San Francisco",
                    # A paid catalog result remains discoverable but cannot spend a source token;
                    # it must enter the durable human-handoff lane before any RSVP attempt.
                    is_free=False,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    _handoff_expiry_activity_recorder.attempts.clear()
    _handoff_expiry_activity_recorder.entered = asyncio.Event()
    _handoff_expiry_activity_recorder.release = asyncio.Event()

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-handoff-expiry-retry-{tag}",
                workflows=[RegistrationWorkflow],
                activities=_saga_activities(
                    expiry_activity=_crash_after_handoff_expiry_commit,
                ),
            ):
                try:
                    child_result = await _release_retained_handoff_expiry(
                        env, tenant_id, canonical.canonical_event_id, workflow_id, tag
                    )
                finally:
                    release = _handoff_expiry_activity_recorder.release
                    if release is not None:
                        release.set()
    finally:
        _handoff_expiry_activity_recorder.entered = None
        _handoff_expiry_activity_recorder.release = None

    assert child_result.status == "expired"
    assert child_result.handoff_task_id is not None
    assert _handoff_expiry_activity_recorder.attempts == [1, 2]
    assert source.registration_effects == 0
    assert source.register_attempts == 0
    assert source.idempotency_keys == []
    assert container.calendar.entries(tenant_id) == []
    assert await _lifecycle_state(tenant_id, workflow_id) == LifecycleState.EXPIRED.value
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.handoff": 1,
        "lifecycle.expired": 1,
    }
    task_state, ledger_count, expiry_outbox_count = await _handoff_expiry_effect_counts(
        tenant_id, child_result.handoff_task_id
    )
    assert task_state == "expired"
    assert ledger_count == 1
    assert expiry_outbox_count == 1


async def test_parent_returns_after_terminal_handoff_while_child_retains_its_ttl(db: None) -> None:
    """A terminal directive returns the request parent after durable handoff, not task TTL expiry.

    The child first reports ``failed`` so the parent can select its final candidate. Once signalled
    to demote, it reports the durable ``handoff`` task and remains alive as the task-TTL executor;
    awaiting the child itself here would violate ADR-003's short-lived parent contract.
    """
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|parent-handoff-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"parent-handoff-{tag}",
                    title=f"Parent retained handoff {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://meetup.example/parent-handoff-{tag}",
                    city="San Francisco",
                    is_free=False,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    _forced_candidate_ids[:] = [str(canonical.canonical_event_id)]

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-parent-handoff-{tag}",
                workflows=[EventRequestWorkflow, RegistrationWorkflow],
                activities=_saga_activities(discovery_activity=_fixed_discover_and_rank),
            ):
                # Freeze automatic skipping: a passing parent must return after the child reports
                # its task, before the child's seven-day expiry timer can run.
                with env.auto_time_skipping_disabled():
                    result = await env.client.execute_workflow(
                        EventRequestWorkflow.run,
                        RequestInput(
                            tenant_id=str(tenant_id),
                            request_id=str(uuid4()),
                            attempt_budget=1,
                        ),
                        id=f"req-parent-handoff-{tag}",
                        task_queue=f"ec-parent-handoff-{tag}",
                    )
    finally:
        _forced_candidate_ids.clear()

    assert result.outcome == "handoff"
    assert result.attempts == 1
    assert source.registration_effects == 0
    assert await _lifecycle_state(tenant_id, workflow_id) == LifecycleState.HANDOFF.value
    task_state, ledger_count, expiry_outbox_count = await _handoff_expiry_effect_counts(
        tenant_id, f"{workflow_id}:handoff"
    )
    assert task_state == "open"
    assert ledger_count == 0
    assert expiry_outbox_count == 0


async def test_short_lived_parent_leaves_quiet_child_to_complete_at_event_end_plus_24h(
    db: None,
) -> None:
    """The parent returns at scheduling; the retained child owns the ADR-007 durable completion timer."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|quiet-complete-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    start_at = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"quiet-complete-{tag}",
                    title=f"Quiet completion {tag}",
                    start_at=start_at,
                    end_at=start_at + timedelta(hours=1),
                    registration_url=f"https://meetup.example/quiet-complete-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    _forced_candidate_ids[:] = [str(canonical.canonical_event_id)]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    _completion_activity_recorder.scheduled_at.clear()

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-quiet-complete-{tag}",
                workflows=[EventRequestWorkflow, RegistrationWorkflow],
                activities=_saga_activities(
                    discovery_activity=_fixed_discover_and_rank,
                    completion_activity=_recording_complete_lifecycle,
                ),
            ):
                with env.auto_time_skipping_disabled():
                    parent_result = await env.client.execute_workflow(
                        EventRequestWorkflow.run,
                        RequestInput(
                            tenant_id=str(tenant_id),
                            request_id=str(uuid4()),
                            attempt_budget=1,
                        ),
                        id=f"req-quiet-complete-{tag}",
                        task_queue=f"ec-quiet-complete-{tag}",
                    )
                    child = env.client.get_workflow_handle(workflow_id)
                # ``get_workflow_handle`` deliberately returns an unwrapped handle. Use the test
                # environment's same time-unlock context that start-workflow handles use, so this
                # retained child can advance to its durable timer without making the parent wait.
                async with env.time_skipping_unlocked():
                    child_result = RegChildResult(**(await child.result()))
    finally:
        _forced_candidate_ids.clear()

    assert parent_result.outcome == "registered"
    assert child_result.status == "completed"
    assert await _lifecycle_state(tenant_id, workflow_id) == LifecycleState.COMPLETED.value
    assert len(_completion_activity_recorder.scheduled_at) == 1
    assert (
        _completion_activity_recorder.scheduled_at[0] - (start_at + timedelta(hours=25))
    ) <= timedelta(seconds=1)
    assert (
        (start_at + timedelta(hours=25)) - _completion_activity_recorder.scheduled_at[0]
    ) <= timedelta(seconds=1)
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
        "lifecycle.completed": 1,
    }


async def test_reschedule_replaces_quiet_completion_deadline_before_the_old_timer_fires(
    db: None,
) -> None:
    """A reconciled calendar end replaces the prior timer; only the new event-end-plus-24h deadline completes."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(tenant_id, f"oidc|reschedule-timer-{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    start_at = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"reschedule-timer-{tag}",
                    title=f"Reschedule timer {tag}",
                    start_at=start_at,
                    end_at=start_at + timedelta(hours=1),
                    registration_url=f"https://meetup.example/reschedule-timer-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    _forced_candidate_ids[:] = [str(canonical.canonical_event_id)]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    rescheduled_start = start_at + timedelta(days=2)
    rescheduled_end = rescheduled_start + timedelta(hours=4)
    _completion_activity_recorder.scheduled_at.clear()

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-reschedule-timer-{tag}",
                workflows=[EventRequestWorkflow, RegistrationWorkflow],
                activities=_saga_activities(
                    discovery_activity=_fixed_discover_and_rank,
                    completion_activity=_recording_complete_lifecycle,
                ),
            ):
                with env.auto_time_skipping_disabled():
                    parent_result = await env.client.execute_workflow(
                        EventRequestWorkflow.run,
                        RequestInput(
                            tenant_id=str(tenant_id),
                            request_id=str(uuid4()),
                            attempt_budget=1,
                        ),
                        id=f"req-reschedule-timer-{tag}",
                        task_queue=f"ec-reschedule-timer-{tag}",
                    )
                    child = env.client.get_workflow_handle(workflow_id)
                    await child.signal(
                        "organizer_change",
                        OrganizerChangeSignal(
                            fingerprint=f"reschedule-timer-change-{tag}",
                            canonical_event_id=str(canonical.canonical_event_id),
                            source=Source.MEETUP.value,
                            event_status="rescheduled",
                            start_at=rescheduled_start.isoformat(),
                            end_at=rescheduled_end.isoformat(),
                            time_zone="UTC",
                        ),
                    )
                    for _ in range(100):
                        if (
                            await _lifecycle_state(tenant_id, workflow_id)
                            == LifecycleState.RECONCILED.value
                        ):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail("reschedule signal did not reconcile before deadline test")
                async with env.time_skipping_unlocked():
                    child_result = RegChildResult(**(await child.result()))
    finally:
        _forced_candidate_ids.clear()

    assert parent_result.outcome == "registered"
    assert child_result.status == "completed"
    assert await _lifecycle_state(tenant_id, workflow_id) == LifecycleState.COMPLETED.value
    assert len(_completion_activity_recorder.scheduled_at) == 1
    assert (
        _completion_activity_recorder.scheduled_at[0] - (rescheduled_end + timedelta(hours=24))
    ) <= timedelta(seconds=1)
    assert (
        (rescheduled_end + timedelta(hours=24)) - _completion_activity_recorder.scheduled_at[0]
    ) <= timedelta(seconds=1)
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
        "lifecycle.reconciled": 1,
        "lifecycle.completed": 1,
    }


async def test_retained_child_ignores_serial_duplicate_organizer_change_after_reconcile(
    db: None,
) -> None:
    """A recovered fanout duplicate cannot repeat a calendar reconciliation (ADR-008)."""
    settings = get_settings()
    tag = uuid4().hex
    source = ConfirmingSource(Source.MEETUP)
    container = build_container(
        settings,
        register_sources={Source.MEETUP: source},
        membership_resolver=lambda value: (
            GroupCondition.MEMBER if value is Source.MEETUP else GroupCondition.UNKNOWN
        ),
    )
    set_container(container)
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|serial-organizer-dedup-{tag}",
            f"{tag}@example.com",
            f"{tag}@u.test",
        )
    )
    await _seed_registration_consent(tenant_id, Source.MEETUP, Modality.API)
    start_at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.MEETUP,
                    source_event_id=f"serial-organizer-dedup-{tag}",
                    title=f"Serial organizer dedup {tag}",
                    start_at=start_at,
                    end_at=start_at + timedelta(hours=2),
                    registration_url=f"https://meetup.example/serial-organizer-dedup-{tag}",
                    city="San Francisco",
                    is_free=True,
                )
            ]
        )
    )[0]
    _forced_candidate_ids[:] = [str(canonical.canonical_event_id)]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)
    reschedule_fingerprint = f"serial-organizer-reschedule-{tag}"
    cancellation_fingerprint = f"serial-organizer-cancel-{tag}"
    rescheduled_start = start_at + timedelta(days=1)
    rescheduled_end = rescheduled_start + timedelta(hours=2)
    _reconcile_activity_recorder.fingerprints.clear()

    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=f"ec-serial-organizer-dedup-{tag}",
                workflows=[EventRequestWorkflow, RegistrationWorkflow],
                activities=_saga_activities(
                    discovery_activity=_fixed_discover_and_rank,
                    reconcile_activity=_recording_reconcile_organizer_change,
                ),
                # Every signal task reconstructs state from history, including the P37 marker
                # and handled-fingerprint set, rather than relying on a sticky worker cache.
                max_cached_workflows=0,
            ):
                with env.auto_time_skipping_disabled():
                    parent_result = await env.client.execute_workflow(
                        EventRequestWorkflow.run,
                        RequestInput(
                            tenant_id=str(tenant_id),
                            request_id=str(uuid4()),
                            attempt_budget=1,
                        ),
                        id=f"req-serial-organizer-dedup-{tag}",
                        task_queue=f"ec-serial-organizer-dedup-{tag}",
                    )
                    child = env.client.get_workflow_handle(workflow_id)
                    reschedule = OrganizerChangeSignal(
                        fingerprint=reschedule_fingerprint,
                        canonical_event_id=str(canonical.canonical_event_id),
                        source=Source.MEETUP.value,
                        event_status="rescheduled",
                        start_at=rescheduled_start.isoformat(),
                        end_at=rescheduled_end.isoformat(),
                        time_zone="UTC",
                    )
                    await child.signal("organizer_change", reschedule)
                    for _ in range(200):
                        pending = await child.query(
                            "pending_lifecycle_signals",
                            result_type=PendingLifecycleSignals,
                        )
                        if _reconcile_activity_recorder.fingerprints == [
                            reschedule_fingerprint
                        ] and not any(
                            signal.fingerprint == reschedule_fingerprint
                            for signal in pending.organizer_changes
                        ):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail(
                            "first organizer reschedule did not finish its workflow command"
                        )

                    # A recovered fanout delivery can arrive after its original command has been
                    # consumed. It must no-op in workflow state before CalendarPort upsert.
                    await child.signal("organizer_change", reschedule)
                    await child.signal(
                        "organizer_change",
                        OrganizerChangeSignal(
                            fingerprint=cancellation_fingerprint,
                            canonical_event_id=str(canonical.canonical_event_id),
                            source=Source.MEETUP.value,
                            event_status="cancelled",
                        ),
                    )
                    child_result = RegChildResult(**(await child.result()))
    finally:
        _forced_candidate_ids.clear()

    assert parent_result.outcome == "registered"
    assert child_result.status == "cancelled"
    assert _reconcile_activity_recorder.fingerprints == [
        reschedule_fingerprint,
        cancellation_fingerprint,
    ]
    # The initial schedule and the one distinct reschedule write exactly once each. The
    # cancellation removes that same deterministic entry.
    assert container.calendar.upsert_attempts == 2
    assert container.calendar.entries(tenant_id) == []
    assert source.registration_effects == 1
    assert await _outbox_topic_counts(tenant_id) == {
        "lifecycle.registered": 1,
        "lifecycle.scheduled": 1,
        "lifecycle.reconciled": 1,
        "lifecycle.cancelled": 1,
    }
