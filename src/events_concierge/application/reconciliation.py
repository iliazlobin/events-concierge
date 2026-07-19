"""Post-registration lifecycle actions for organizer changes and user un-RSVP commands.

The long-lived child workflow owns stable command keys and calls these bounded application steps.
Calendar effects are deterministic and source withdrawal is a fresh read-before-mutate sequence, so
crash recovery converges without inventing a second RSVP effect (FR-8.7/8.8, ADR-003/005/007/008).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain import ids
from ..domain.conflict import evaluate_conflict
from ..domain.enums import (
    ConflictVerdict,
    EventStatus,
    HandoffReason,
    HandoffState,
    Lane,
    LifecycleState,
    Modality,
    RsvpState,
    Source,
)
from ..domain.events import CanonicalEvent, EventSourceLink
from ..domain.lifecycle import HandoffTask, Lifecycle
from ..infra.logging import get_logger
from ..infra.ulid import new_ulid
from ..ports.browser_admission import (
    BrowserAdmissionLease,
    BrowserAdmissionPort,
    BrowserAdmissionRequest,
)
from ..ports.calendar import CalendarEntry, CalendarPort
from ..ports.credentials import CredentialVault
from ..ports.policy import (
    Pacer,
    PacerLease,
    PacerLeaseStatus,
    PacerOperation,
    PacerRequest,
    PolicyContext,
    PolicyEngine,
    SourceQuarantinePort,
)
from ..ports.repositories import HandoffRepository, LifecycleRepository
from ..ports.sources import (
    RegistrationTarget,
    SourceAccessDeniedError,
    SourceRateLimitedError,
)
from ..ports.withdrawal import (
    RegistrationWithdrawalPort,
    WithdrawalOutcome,
    WithdrawalResult,
)

_DEFAULT_DURATION = timedelta(hours=2)
_COMPLETION_GRACE = timedelta(hours=24)
_LANE_SOURCE_MODALITY: dict[Lane, tuple[Source, Modality]] = {
    Lane.AUTONOMOUS_SLA: (Source.MEETUP, Modality.API),
    Lane.BROWSER_BEST_EFFORT: (Source.LUMA, Modality.BROWSER),
}
_WITHDRAWABLE_STATES = frozenset(
    {
        LifecycleState.REGISTERED,
        LifecycleState.SCHEDULED,
        LifecycleState.RECONCILED,
        LifecycleState.WITHDRAWING,
    }
)
_RECONCILABLE_STATES = frozenset(
    {LifecycleState.REGISTERED, LifecycleState.SCHEDULED, LifecycleState.RECONCILED}
)
_CANCELLABLE_STATES = frozenset(
    {
        LifecycleState.REGISTERED,
        LifecycleState.SCHEDULED,
        LifecycleState.RECONCILED,
        LifecycleState.WITHDRAWING,
    }
)
_COMPLETABLE_STATES = frozenset({LifecycleState.SCHEDULED, LifecycleState.RECONCILED})

_log = get_logger(__name__)


def completion_deadline(event_end_at: datetime) -> datetime:
    """Return ADR-007's event-end-plus-24-hours terminalization deadline without consulting a clock."""
    if event_end_at.tzinfo is None or event_end_at.utcoffset() is None:
        raise ValueError("lifecycle completion deadline requires a timezone-aware event end")
    return event_end_at + _COMPLETION_GRACE


class ReconcileStatus(StrEnum):
    CANCELLED = "cancelled"
    RECONCILED = "reconciled"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """The journaled outcome of one organizer-change command (FR-8.7, ADR-008)."""

    status: ReconcileStatus
    conflict_warning: bool = False
    detail: str = ""
    completion_deadline_at: datetime | None = None


class LifecycleCompletionStatus(StrEnum):
    """The guarded terminalization result returned to the long-lived child (ADR-007)."""

    COMPLETED = "completed"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class LifecycleCompletionResult:
    """One idempotent event-end-plus-24-hours completion attempt (ADR-007)."""

    status: LifecycleCompletionStatus
    detail: str = ""


class HandoffExpiryStatus(StrEnum):
    """The guarded result of a handoff TTL terminalization (FR-6.6, ADR-007)."""

    EXPIRED = "expired"
    COMPLETION_COMMITTED = "completion_committed"
    TERMINAL = "terminal"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class HandoffExpiryResult:
    """One idempotent expiry attempt owned by a Temporal timer or orphan repair worker."""

    status: HandoffExpiryStatus
    detail: str = ""
    terminal_state: LifecycleState | None = None


class UnrsvpStatus(StrEnum):
    CANCELLED = "cancelled"
    HANDOFF = "handoff"
    PACING_WAIT = "pacing_wait"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class UnrsvpResult:
    """A stable un-RSVP command result returned to the durable workflow (FR-8.8)."""

    status: UnrsvpStatus
    detail: str = ""
    retry_after_seconds: float | None = None
    handoff_task_id: str | None = None
    handoff_expires_at: datetime | None = None
    handoff_expiry_transition_id: str | None = None
    handoff_created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OrganizerChange:
    """A normalized, de-duplicable organizer update from the central detector (ADR-008).

    ``fingerprint`` is supplied by the detector and is persisted/deduplicated outside this service;
    keeping it in the activity payload preserves the audit link in the lifecycle outbox projection.
    A reschedule carries enough authoritative calendar fields to update one deterministic entry even
    before a future catalog refresh has materialized the changed canonical row (FR-8.7/FR-8.7a).
    """

    fingerprint: str
    source: Source
    event_status: EventStatus
    start_at: datetime | None = None
    end_at: datetime | None = None
    time_zone: str | None = None
    title: str | None = None
    venue_name: str | None = None

    def __post_init__(self) -> None:
        if not self.fingerprint.strip():
            raise ValueError("organizer-change fingerprint must not be empty")
        if self.event_status not in (EventStatus.CANCELLED, EventStatus.RESCHEDULED):
            raise ValueError("organizer change must be cancelled or rescheduled")
        for value in (self.start_at, self.end_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("organizer-change timestamps must be timezone-aware")
        if self.event_status is EventStatus.RESCHEDULED and self.start_at is None:
            raise ValueError("a reschedule requires a new start_at")
        if self.start_at is not None and self.end_at is not None and self.end_at <= self.start_at:
            raise ValueError("organizer-change end_at must be after start_at")
        if self.time_zone is not None:
            try:
                ZoneInfo(self.time_zone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("organizer-change time_zone must be an IANA zone") from error


class LifecycleReconciliationService:
    """Apply cancellation/reschedule/un-RSVP commands through the guarded lifecycle repository.

    This class deliberately owns no polling, OAuth, or browser implementation.  A fixture detector
    signals the workflow, and provider-specific withdrawal adapters remain optional behind a narrow
    port.  Missing or denied automation produces a manual task while retaining ``withdrawing`` as
    the truthful state (FR-8.7/8.8, ADR-003/004/005/007/008).
    """

    def __init__(
        self,
        calendar: CalendarPort,
        lifecycle_repo: LifecycleRepository,
        handoff_repo: HandoffRepository,
        policy: PolicyEngine,
        pacer: Pacer,
        withdrawals_by_source: dict[Source, RegistrationWithdrawalPort],
        *,
        credential_vault: CredentialVault | None = None,
        browser_admission: BrowserAdmissionPort | None = None,
        source_quarantine: SourceQuarantinePort | None = None,
        handoff_ttl_days: int = 7,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._calendar = calendar
        self._lifecycle = lifecycle_repo
        self._handoff = handoff_repo
        self._policy = policy
        self._pacer = pacer
        self._withdrawals = withdrawals_by_source
        self._vault = credential_vault
        self._browser_admission = browser_admission
        self._source_quarantine = source_quarantine
        self._handoff_ttl = timedelta(days=handoff_ttl_days)
        self._now = now or (lambda: datetime.now(UTC))

    async def reconcile_organizer_change(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        change: OrganizerChange,
        *,
        transition_id: str,
    ) -> ReconcileResult:
        """Reconcile one deduplicated organizer cancellation or reschedule (FR-8.7, ADR-008)."""
        lifecycle = await self._active_lifecycle(tenant_id, event, workflow_id)
        if lifecycle is None:
            return ReconcileResult(ReconcileStatus.IGNORED, detail="no active matching lifecycle")

        if change.event_status is EventStatus.CANCELLED:
            return await self._cancel_for_organizer_change(lifecycle, event, change, transition_id)
        return await self._reschedule(lifecycle, event, change, transition_id)

    async def complete_lifecycle(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        *,
        transition_id: str,
    ) -> LifecycleCompletionResult:
        """Terminalize a quiet scheduled/reconciled lifecycle after its Temporal-owned grace timer.

        The workflow supplies the once-minted transition ID and owns the calendar-derived deadline;
        this bounded application step only validates the durable state and commits the ADR-007
        guarded transition/outbox.  Reloading by workflow ID makes a lost activity acknowledgement
        converge when the prior attempt already committed ``COMPLETED``.
        """
        lifecycle = await self._lifecycle.get_or_create(
            tenant_id, event.canonical_event_id, workflow_id
        )
        if lifecycle.state is LifecycleState.COMPLETED:
            return LifecycleCompletionResult(
                LifecycleCompletionStatus.COMPLETED,
                "lifecycle already completed",
            )
        if lifecycle.state not in _COMPLETABLE_STATES:
            return LifecycleCompletionResult(
                LifecycleCompletionStatus.IGNORED,
                f"lifecycle {lifecycle.state.value} is not quiet enough to complete",
            )
        await self._lifecycle.transition(
            lifecycle,
            LifecycleState.COMPLETED,
            transition_id,
            {
                "canonical_event_id": str(event.canonical_event_id),
                "workflow_id": workflow_id,
                "event_summary": event.title,
                "completion_reason": "event_end_plus_24h",
            },
        )
        return LifecycleCompletionResult(LifecycleCompletionStatus.COMPLETED)

    async def expire_handoff(
        self,
        tenant_id: UUID,
        canonical_event_id: UUID,
        workflow_id: str,
        *,
        task_id: str,
        expiry_transition_id: str,
    ) -> HandoffExpiryResult:
        """Expire one due human task through the guarded lifecycle transition (FR-6.6, ADR-007).

        Temporal owns the ordinary timer and the orphan queue may invoke this same method only
        after its grace period. The task itself is the durable expiry context, so catalog retention
        cannot strand an otherwise valid lifecycle. The database guard verifies the exact
        task/transition identity and due timestamp, atomically marks all open lifecycle tasks
        expired, releases their control records, and writes the terminal outbox event. An
        acknowledgement loss reloads the terminal lifecycle instead of publishing a second expiry.
        """
        task = await self._handoff.get(tenant_id, task_id)
        if task is None:
            return HandoffExpiryResult(HandoffExpiryStatus.IGNORED, "handoff task not found")
        if (
            task.workflow_id != workflow_id
            or task.canonical_event_id != canonical_event_id
            or task.resolved_expiry_transition_id() != expiry_transition_id
        ):
            return HandoffExpiryResult(
                HandoffExpiryStatus.IGNORED,
                "handoff task does not match the expiry command",
            )
        lifecycle = await self._lifecycle.get_or_create(
            tenant_id, task.canonical_event_id, workflow_id
        )
        if task.state is HandoffState.COMPLETED:
            # Verified mark-done commits the task and HANDOFF -> REGISTERED in one transaction.
            # An activity acknowledgement can be lost after that commit; task TTL must never
            # overwrite the factual registration while the workflow recovers its exact receipt.
            return HandoffExpiryResult(
                HandoffExpiryStatus.COMPLETION_COMMITTED,
                "verified handoff completion already committed",
            )
        if lifecycle.state is LifecycleState.EXPIRED:
            return HandoffExpiryResult(
                HandoffExpiryStatus.EXPIRED,
                "handoff already expired",
                LifecycleState.EXPIRED,
            )
        if lifecycle.state in {
            LifecycleState.COMPLETED,
            LifecycleState.CANCELLED,
            LifecycleState.FAILED_NO_CANDIDATE,
        }:
            return HandoffExpiryResult(
                HandoffExpiryStatus.TERMINAL,
                f"lifecycle already {lifecycle.state.value}",
                lifecycle.state,
            )
        if lifecycle.state not in {
            LifecycleState.HANDOFF,
            LifecycleState.REGISTERED,
            LifecycleState.WITHDRAWING,
        }:
            return HandoffExpiryResult(
                HandoffExpiryStatus.IGNORED,
                f"lifecycle {lifecycle.state.value} cannot expire its handoff",
            )
        await self._lifecycle.transition(
            lifecycle,
            LifecycleState.EXPIRED,
            expiry_transition_id,
            {
                "canonical_event_id": str(task.canonical_event_id),
                "workflow_id": workflow_id,
                "event_summary": task.event_summary,
                "task_id": task_id,
                "expiry_transition_id": expiry_transition_id,
                "expiry_reason": "handoff_ttl",
            },
        )
        return HandoffExpiryResult(
            HandoffExpiryStatus.EXPIRED,
            terminal_state=LifecycleState.EXPIRED,
        )

    async def request_unrsvp(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        request_id: str,
        *,
        withdrawing_transition_id: str,
        cancelled_transition_id: str,
        handoff_expiry_transition_id: str,
        source_read_queue_item_id: str,
        source_mutation_idempotency_key: str,
    ) -> UnrsvpResult:
        """Remove calendar intent, then safely withdraw or create a durable human handoff (FR-8.8).

        The workflow supplies every key once.  A retry after a provider ACK loss starts over at the
        remote state read; ``NOT_PRESENT`` then finalizes the same lifecycle without a second
        withdrawal mutation (ADR-003/005/007).
        """
        lifecycle = await self._active_lifecycle(tenant_id, event, workflow_id)
        if lifecycle is None:
            return UnrsvpResult(UnrsvpStatus.IGNORED, "no active matching lifecycle")
        if lifecycle.state not in _WITHDRAWABLE_STATES:
            return UnrsvpResult(
                UnrsvpStatus.IGNORED,
                f"lifecycle {lifecycle.state.value} cannot be withdrawn",
            )

        if lifecycle.state is not LifecycleState.WITHDRAWING:
            await self._lifecycle.transition(
                lifecycle,
                LifecycleState.WITHDRAWING,
                withdrawing_transition_id,
                {
                    "canonical_event_id": str(event.canonical_event_id),
                    "workflow_id": workflow_id,
                    "unrsvp_request_id": request_id,
                    "event_summary": event.title,
                },
            )

        # This is deliberately before the source read/mutation: the user's calendar intent is
        # immediately removed even when policy, Pacer, or a provider forces manual completion.
        calendar_event_id = ids.calendar_event_id(tenant_id, event.canonical_event_id)
        await self._calendar.delete_event(
            tenant_id,
            calendar_event_id,
            canonical_event_id=event.canonical_event_id,
        )

        target = self._withdrawal_target(event, lifecycle)
        if target is None:
            return await self._create_withdrawal_handoff(
                lifecycle,
                event,
                request_id,
                "no source-specific autonomous withdrawal target",
                handoff_expiry_transition_id,
            )
        source, modality, registration_target, adapter = target

        early_decision = await self._policy.evaluate(
            PolicyContext(tenant_id=tenant_id, source=source, modality=modality, action="withdraw")
        )
        if not early_decision.allowed:
            return await self._create_withdrawal_handoff(
                lifecycle, event, request_id, early_decision.reason, handoff_expiry_transition_id
            )

        read_request = await self._pacer_request(
            tenant_id,
            source,
            PacerOperation.WITHDRAWAL_READ,
            queue_item_id=source_read_queue_item_id,
        )
        state_or_result = await self._read_before_withdraw(
            tenant_id,
            source,
            modality,
            registration_target,
            adapter,
            read_request,
        )
        if isinstance(state_or_result, UnrsvpResult):
            if state_or_result.status is UnrsvpStatus.HANDOFF:
                return await self._create_withdrawal_handoff(
                    lifecycle,
                    event,
                    request_id,
                    state_or_result.detail,
                    handoff_expiry_transition_id,
                )
            return state_or_result
        if state_or_result is RsvpState.NOT_PRESENT:
            return await self._finish_withdrawal(
                lifecycle, event, request_id, cancelled_transition_id, "remote RSVP already absent"
            )
        if state_or_result is not RsvpState.CONFIRMED:
            return await self._create_withdrawal_handoff(
                lifecycle,
                event,
                request_id,
                f"remote RSVP state is {state_or_result.value}",
                handoff_expiry_transition_id,
            )

        # Repeat the policy guard directly before the destructive provider operation (ADR-004).
        decision = await self._policy.evaluate(
            PolicyContext(tenant_id=tenant_id, source=source, modality=modality, action="withdraw")
        )
        if not decision.allowed:
            return await self._create_withdrawal_handoff(
                lifecycle, event, request_id, decision.reason, handoff_expiry_transition_id
            )

        mutation_request = await self._pacer_request(
            tenant_id,
            source,
            PacerOperation.WITHDRAWAL_MUTATION,
            queue_item_id=source_mutation_idempotency_key,
        )
        mutation_result = await self._withdraw_once(
            tenant_id,
            source,
            modality,
            registration_target,
            adapter,
            source_mutation_idempotency_key,
            mutation_request,
        )
        if isinstance(mutation_result, UnrsvpResult):
            if mutation_result.status is UnrsvpStatus.HANDOFF:
                return await self._create_withdrawal_handoff(
                    lifecycle,
                    event,
                    request_id,
                    mutation_result.detail,
                    handoff_expiry_transition_id,
                )
            return mutation_result
        if mutation_result.outcome is WithdrawalOutcome.NEEDS_HANDOFF:
            return await self._create_withdrawal_handoff(
                lifecycle,
                event,
                request_id,
                mutation_result.detail or "source requires a human withdrawal",
                handoff_expiry_transition_id,
            )
        return await self._finish_withdrawal(
            lifecycle,
            event,
            request_id,
            cancelled_transition_id,
            mutation_result.detail or mutation_result.outcome.value,
        )

    async def _cancel_for_organizer_change(
        self,
        lifecycle: Lifecycle,
        event: CanonicalEvent,
        change: OrganizerChange,
        transition_id: str,
    ) -> ReconcileResult:
        if lifecycle.state not in _CANCELLABLE_STATES:
            return ReconcileResult(
                ReconcileStatus.IGNORED,
                detail=f"lifecycle {lifecycle.state.value} cannot be cancelled",
            )
        calendar_event_id = ids.calendar_event_id(lifecycle.tenant_id, event.canonical_event_id)
        await self._calendar.delete_event(
            lifecycle.tenant_id,
            calendar_event_id,
            canonical_event_id=event.canonical_event_id,
        )
        await self._lifecycle.transition(
            lifecycle,
            LifecycleState.CANCELLED,
            transition_id,
            {
                "canonical_event_id": str(event.canonical_event_id),
                "calendar_event_id": calendar_event_id,
                "workflow_id": lifecycle.workflow_id,
                "event_summary": event.title,
                "source": change.source.value,
                "organizer_change_fingerprint": change.fingerprint,
            },
        )
        return ReconcileResult(ReconcileStatus.CANCELLED)

    async def _reschedule(
        self,
        lifecycle: Lifecycle,
        event: CanonicalEvent,
        change: OrganizerChange,
        transition_id: str,
    ) -> ReconcileResult:
        if lifecycle.state not in _RECONCILABLE_STATES:
            return ReconcileResult(
                ReconcileStatus.IGNORED,
                detail=f"lifecycle {lifecycle.state.value} cannot be rescheduled",
            )
        entry = self._rescheduled_entry(lifecycle.tenant_id, event, change)
        if entry.end_at is None:
            raise ValueError("rescheduled calendar entry requires an end time")
        conflict_warning = False
        conflict_detail = ""
        try:
            busy = await self._calendar.free_busy(lifecycle.tenant_id, entry.start_at, entry.end_at)
            conflict_warning = (
                evaluate_conflict(entry.start_at, entry.end_at, busy) is ConflictVerdict.BLOCKED
            )
            if conflict_warning:
                conflict_detail = "rescheduled time has a calendar conflict"
        except Exception as exc:
            # An organizer update must not leave a user's calendar stale.  Write it, but surface a
            # warning because the required conflict re-check could not prove the new slot clear.
            conflict_warning = True
            conflict_detail = "rescheduled time could not be checked for calendar conflicts"
            _log.warning("reconcile_free_busy_failed", error=str(exc))
        await self._calendar.upsert_event(lifecycle.tenant_id, entry)
        lifecycle.conflict_warning = conflict_warning
        await self._lifecycle.transition(
            lifecycle,
            LifecycleState.RECONCILED,
            transition_id,
            {
                "canonical_event_id": str(event.canonical_event_id),
                "calendar_event_id": entry.calendar_event_id,
                "workflow_id": lifecycle.workflow_id,
                "event_summary": entry.title,
                "source": change.source.value,
                "organizer_change_fingerprint": change.fingerprint,
                "conflict_warning": conflict_warning,
                "conflict_detail": conflict_detail,
            },
        )
        return ReconcileResult(
            ReconcileStatus.RECONCILED,
            conflict_warning,
            conflict_detail,
            completion_deadline_at=completion_deadline(entry.end_at),
        )

    async def _finish_withdrawal(
        self,
        lifecycle: Lifecycle,
        event: CanonicalEvent,
        request_id: str,
        transition_id: str,
        detail: str,
    ) -> UnrsvpResult:
        if lifecycle.state is LifecycleState.CANCELLED:
            return UnrsvpResult(UnrsvpStatus.CANCELLED, detail)
        if lifecycle.state is not LifecycleState.WITHDRAWING:
            return UnrsvpResult(
                UnrsvpStatus.IGNORED,
                f"lifecycle {lifecycle.state.value} is no longer withdrawing",
            )
        await self._lifecycle.transition(
            lifecycle,
            LifecycleState.CANCELLED,
            transition_id,
            {
                "canonical_event_id": str(event.canonical_event_id),
                "workflow_id": lifecycle.workflow_id,
                "unrsvp_request_id": request_id,
                "event_summary": event.title,
                "withdrawal_detail": detail,
            },
        )
        return UnrsvpResult(UnrsvpStatus.CANCELLED, detail)

    async def _create_withdrawal_handoff(
        self,
        lifecycle: Lifecycle,
        event: CanonicalEvent,
        request_id: str,
        detail: str,
        expiry_transition_id: str,
    ) -> UnrsvpResult:
        task = HandoffTask(
            # A later delivery of the same user intent may have a different inbound transport id.
            # There is only one active lifecycle withdrawal, so this task identity is lifecycle-
            # scoped and absorbs duplicate API/reply deliveries without emitting two manual tasks.
            task_id=f"{lifecycle.workflow_id}:withdrawal-handoff",
            tenant_id=lifecycle.tenant_id,
            workflow_id=lifecycle.workflow_id,
            canonical_event_id=event.canonical_event_id,
            reason=HandoffReason.WITHDRAWAL_REQUIRED,
            deep_link=self._handoff_link(event, lifecycle),
            event_summary=event.title,
            ttl_expires_at=self._handoff_deadline(event),
            state=HandoffState.OPEN,
            metadata={"detail": detail, "unrsvp_request_id": request_id},
            expiry_transition_id=expiry_transition_id,
        )
        await self._handoff.create_withdrawal_handoff(
            task,
            {
                "task_id": task.task_id,
                "canonical_event_id": str(event.canonical_event_id),
                "workflow_id": lifecycle.workflow_id,
                "reason": task.reason.value,
                "event_summary": task.event_summary,
                "deep_link": task.deep_link,
                "unrsvp_request_id": request_id,
            },
        )
        persisted = await self._handoff.get(lifecycle.tenant_id, task.task_id)
        if persisted is None:
            raise RuntimeError("withdrawal handoff was not durable after its task transaction")
        return UnrsvpResult(
            UnrsvpStatus.HANDOFF,
            detail,
            handoff_task_id=persisted.task_id,
            handoff_expires_at=persisted.ttl_expires_at,
            handoff_expiry_transition_id=persisted.resolved_expiry_transition_id(),
            handoff_created_at=persisted.created_at,
        )

    async def _active_lifecycle(
        self, tenant_id: UUID, event: CanonicalEvent, workflow_id: str
    ) -> Lifecycle | None:
        lifecycle = await self._lifecycle.find_active(tenant_id, event.canonical_event_id)
        if lifecycle is None or lifecycle.workflow_id != workflow_id:
            return None
        return lifecycle

    def _rescheduled_entry(
        self, tenant_id: UUID, event: CanonicalEvent, change: OrganizerChange
    ) -> CalendarEntry:
        if change.start_at is None:
            raise ValueError("reschedule requires start_at")
        start_at = change.start_at
        old_end = event.end_at or (event.start_at + _DEFAULT_DURATION)
        duration = old_end - event.start_at
        end_at = change.end_at or (start_at + max(duration, _DEFAULT_DURATION))
        return CalendarEntry(
            calendar_event_id=ids.calendar_event_id(tenant_id, event.canonical_event_id),
            canonical_event_id=event.canonical_event_id,
            title=change.title or event.title,
            start_at=start_at,
            end_at=end_at,
            time_zone=change.time_zone or _iana_time_zone(start_at),
            location=change.venue_name if change.venue_name is not None else event.venue_name,
            private_metadata={
                "events_concierge.registration_state": "reconciled",
                "events_concierge.organizer_change_fingerprint": change.fingerprint,
                "events_concierge.source_event_ids": _source_event_ids(event),
            },
        )

    def _withdrawal_target(
        self, event: CanonicalEvent, lifecycle: Lifecycle
    ) -> tuple[Source, Modality, RegistrationTarget, RegistrationWithdrawalPort] | None:
        if lifecycle.lane is None or lifecycle.registration_source is None:
            return None
        lane_source_modality = _LANE_SOURCE_MODALITY.get(lifecycle.lane)
        if lane_source_modality is None:
            return None
        source = lifecycle.registration_source
        modality = lane_source_modality[1]
        adapter = self._withdrawals.get(source)
        link = _source_link(event, source)
        if adapter is None or link is None:
            return None
        return (
            source,
            modality,
            RegistrationTarget(link.source_event_id, link.registration_url),
            adapter,
        )

    async def _read_before_withdraw(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        target: RegistrationTarget,
        adapter: RegistrationWithdrawalPort,
        request: PacerRequest,
    ) -> RsvpState | UnrsvpResult:
        policy_denial = await self._withdrawal_source_policy(tenant_id, source, modality)
        if policy_denial is not None:
            return policy_denial
        browser_lease = await self._browser_lease(
            tenant_id, source, modality, request.queue_item_id
        )
        if browser_lease is False:
            return UnrsvpResult(
                UnrsvpStatus.HANDOFF, "browser admission unavailable for withdrawal"
            )
        try:
            lease = await self._pacer.acquire(request)
            projected = self._project_pacer(lease)
            if projected is not None:
                return projected
            # Policy can change while browser/Pacer admission waits. Re-read directly before
            # this provider read so a newly quarantined source receives no further request.
            policy_denial = await self._withdrawal_source_policy(tenant_id, source, modality)
            if policy_denial is not None:
                return policy_denial
            try:
                return await adapter.read_registration_state(tenant_id, target, modality)
            except SourceRateLimitedError as throttle:
                return await self._source_throttle(request, throttle)
            except SourceAccessDeniedError as denied:
                return await self._quarantine_after_access_denied(source, denied)
            except Exception as exc:
                _log.warning("withdrawal_state_read_failed", source=source.value, error=str(exc))
                return UnrsvpResult(UnrsvpStatus.HANDOFF, "withdrawal state read failed")
        finally:
            await self._release_browser_lease(browser_lease)

    async def _withdraw_once(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        target: RegistrationTarget,
        adapter: RegistrationWithdrawalPort,
        idempotency_key: str,
        request: PacerRequest,
    ) -> WithdrawalResult | UnrsvpResult:
        policy_denial = await self._withdrawal_source_policy(tenant_id, source, modality)
        if policy_denial is not None:
            return policy_denial
        browser_lease = await self._browser_lease(
            tenant_id, source, modality, request.queue_item_id
        )
        if browser_lease is False:
            return UnrsvpResult(
                UnrsvpStatus.HANDOFF, "browser admission unavailable for withdrawal"
            )
        try:
            lease = await self._pacer.acquire(request)
            projected = self._project_pacer(lease)
            if projected is not None:
                return projected
            # This is the ADR-004 data-plane guard. The earlier checks avoid needless capacity
            # use; this post-admission re-read is immediately before the destructive wire call.
            policy_denial = await self._withdrawal_source_policy(tenant_id, source, modality)
            if policy_denial is not None:
                return policy_denial
            try:
                return await adapter.withdraw(tenant_id, target, modality, idempotency_key)
            except SourceRateLimitedError as throttle:
                return await self._source_throttle(request, throttle)
            except SourceAccessDeniedError as denied:
                return await self._quarantine_after_access_denied(source, denied)
        finally:
            await self._release_browser_lease(browser_lease)

    async def _withdrawal_source_policy(
        self, tenant_id: UUID, source: Source, modality: Modality
    ) -> UnrsvpResult | None:
        """Freshly deny a withdrawal source call before browser/Pacer/provider work (ADR-004)."""
        decision = await self._policy.evaluate(
            PolicyContext(tenant_id=tenant_id, source=source, modality=modality, action="withdraw")
        )
        if decision.allowed:
            return None
        return UnrsvpResult(UnrsvpStatus.HANDOFF, decision.reason)

    async def _quarantine_after_access_denied(
        self, source: Source, denied: SourceAccessDeniedError
    ) -> UnrsvpResult:
        """Trip the same source circuit breaker from a withdrawal boundary (FR-10.3)."""
        actuator = self._source_quarantine
        if actuator is None:
            _log.error(
                "source_quarantine_actuator_unconfigured",
                source=source.value,
                signal=denied.signal.value,
            )
        else:
            try:
                receipt = await actuator.quarantine(source, denied.signal)
            except Exception as error:
                _log.error(
                    "source_quarantine_actuation_failed",
                    source=source.value,
                    signal=denied.signal.value,
                    error_type=type(error).__name__,
                )
            else:
                _log.warning(
                    "source_quarantined",
                    source=receipt.source.value,
                    signal=receipt.signal.value,
                    newly_quarantined=receipt.newly_quarantined,
                )
        return UnrsvpResult(UnrsvpStatus.HANDOFF, "source access denied; source quarantined")

    @staticmethod
    def _project_pacer(lease: PacerLease) -> UnrsvpResult | None:
        if lease.granted:
            return None
        if lease.status is PacerLeaseStatus.WAIT:
            return UnrsvpResult(
                UnrsvpStatus.PACING_WAIT,
                lease.detail,
                retry_after_seconds=lease.retry_after_seconds,
            )
        return UnrsvpResult(UnrsvpStatus.HANDOFF, lease.detail or "withdrawal pacing unavailable")

    async def _source_throttle(
        self, request: PacerRequest, throttle: SourceRateLimitedError
    ) -> UnrsvpResult:
        await self._pacer.observe_backoff(
            request,
            retry_after_seconds=throttle.retry_after_seconds,
            reset_at=throttle.reset_at,
        )
        lease = await self._pacer.acquire(request)
        if lease.granted:
            lease = PacerLease(PacerLeaseStatus.WAIT, 1.0, str(throttle))
        return UnrsvpResult(
            UnrsvpStatus.PACING_WAIT,
            lease.detail,
            retry_after_seconds=lease.retry_after_seconds,
        )

    async def _pacer_request(
        self,
        tenant_id: UUID,
        source: Source,
        operation: PacerOperation,
        *,
        queue_item_id: str,
    ) -> PacerRequest:
        credential_scope = f"tenant:{tenant_id}"
        if self._vault is not None:
            credential = await self._vault.get(tenant_id, source)
            if credential is not None:
                credential_scope = str(credential.credential_id)
        return PacerRequest(
            source=source,
            quota_scope=credential_scope,
            operation=operation,
            tenant_id=tenant_id,
            queue_item_id=queue_item_id,
        )

    async def _browser_lease(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        lease_id: str | None,
    ) -> BrowserAdmissionLease | None | bool:
        if modality is not Modality.BROWSER:
            return None
        if self._browser_admission is None:
            return False
        try:
            lease = await self._browser_admission.acquire(
                BrowserAdmissionRequest(
                    tenant_id=tenant_id,
                    source=source,
                    lease_id=lease_id or "withdrawal",
                    fence_token=new_ulid(),
                )
            )
        except Exception as exc:
            _log.warning("withdrawal_browser_admission_failed", source=source.value, error=str(exc))
            return False
        return lease if lease.granted else False

    async def _release_browser_lease(self, lease: BrowserAdmissionLease | None | bool) -> None:
        if not isinstance(lease, BrowserAdmissionLease) or self._browser_admission is None:
            return
        try:
            await self._browser_admission.release(lease)
        except Exception as exc:
            _log.warning("withdrawal_browser_release_failed", error=str(exc))

    def _handoff_deadline(self, event: CanonicalEvent) -> datetime:
        """Cap the owner-ratified task TTL at the event start (ADR-007, FR-6.6)."""
        now = self._now()
        if event.start_at.tzinfo is None or event.start_at.utcoffset() is None:
            raise ValueError("handoff event start must be timezone-aware")
        return min(now + self._handoff_ttl, event.start_at)

    @staticmethod
    def _handoff_link(event: CanonicalEvent, lifecycle: Lifecycle) -> str:
        if lifecycle.lane is not None:
            source_modality = _LANE_SOURCE_MODALITY.get(lifecycle.lane)
            if source_modality is not None:
                link = _source_link(event, source_modality[0])
                if link is not None:
                    return link.registration_url
        return event.source_links[0].registration_url if event.source_links else ""


def _source_link(event: CanonicalEvent, source: Source) -> EventSourceLink | None:
    """Select the retained source surface used by the already-recorded lifecycle lane."""
    return next((link for link in event.source_links if link.source is source), None)


def _source_event_ids(event: CanonicalEvent) -> str:
    """Serialize stable source identifiers for CalendarPort private metadata (FR-9.3)."""
    return json.dumps(
        sorted(f"{link.source.value}:{link.source_event_id}" for link in event.source_links),
        separators=(",", ":"),
    )


def _iana_time_zone(moment: datetime) -> str:
    """Preserve an IANA zone when available and safely normalize offset-only input to UTC (FR-9.5)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("calendar timestamps must be timezone-aware")
    candidate = getattr(moment.tzinfo, "key", None)
    zone_name = candidate if isinstance(candidate, str) and candidate else "UTC"
    try:
        ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("calendar time zone must be IANA") from error
    return zone_name
