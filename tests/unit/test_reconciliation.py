"""Offline lifecycle reconciliation and un-RSVP recovery tests (FR-8.7/8.8, ADR-008)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.engine import DataPolicyEngine
from events_concierge.application.reconciliation import (
    HandoffExpiryStatus,
    LifecycleCompletionStatus,
    LifecycleReconciliationService,
    OrganizerChange,
    ReconcileStatus,
    UnrsvpStatus,
)
from events_concierge.domain import ids
from events_concierge.domain.conflict import BusyBlock
from events_concierge.domain.enums import (
    EventStatus,
    HandoffReason,
    HandoffState,
    Lane,
    LifecycleState,
    Modality,
    PriceStatus,
    Source,
)
from events_concierge.domain.events import CanonicalEvent, EventSourceLink
from events_concierge.domain.lifecycle import HandoffTask, Lifecycle
from events_concierge.domain.policy import SourcePolicy
from events_concierge.ports.calendar import CalendarEntry
from events_concierge.ports.policy import PacerLease, PacerLeaseStatus, PacerOperation, PacerRequest
from events_concierge.ports.sources import RegistrationTarget
from events_concierge.ports.withdrawal import RegistrationWithdrawalPort


class _LifecycleRepository:
    """Minimal guarded-repository double retaining transition keys for recovery assertions."""

    def __init__(self, lifecycle: Lifecycle) -> None:
        self.lifecycle = lifecycle
        self.transitions: list[tuple[str, LifecycleState]] = []
        self.transition_payloads: list[dict[str, object]] = []
        self._applied: set[str] = set()

    async def get_or_create(
        self, tenant_id: UUID, canonical_event_id: UUID, workflow_id: str
    ) -> Lifecycle:
        if (
            self.lifecycle.tenant_id != tenant_id
            or self.lifecycle.canonical_event_id != canonical_event_id
            or self.lifecycle.workflow_id != workflow_id
        ):
            raise AssertionError("completion must reload the existing workflow lifecycle")
        return self.lifecycle

    async def find_active(self, tenant_id: UUID, canonical_event_id: UUID) -> Lifecycle | None:
        if (
            self.lifecycle.tenant_id != tenant_id
            or self.lifecycle.canonical_event_id != canonical_event_id
            or self.lifecycle.state.is_terminal
        ):
            return None
        return self.lifecycle

    async def transition(
        self,
        lifecycle: Lifecycle,
        to_state: LifecycleState,
        transition_id: str,
        outbox_payload: dict[str, object],
    ) -> None:
        if transition_id in self._applied:
            return
        self._applied.add(transition_id)
        lifecycle.transition(to_state)
        self.transitions.append((transition_id, to_state))
        self.transition_payloads.append(outbox_payload)


class _HandoffRepository:
    """Captures the atomically-requested withdrawal handoff without widening source behavior."""

    def __init__(self) -> None:
        self.tasks: list[HandoffTask] = []
        self.payloads: list[dict[str, object]] = []

    async def create(self, task: HandoffTask) -> None:
        self._record(task, {})

    async def create_and_transition(
        self,
        task: HandoffTask,
        lifecycle: Lifecycle,
        to_state: LifecycleState,
        transition_id: str,
        outbox_payload: dict[str, object],
    ) -> None:
        del transition_id
        self._record(task, outbox_payload)
        lifecycle.transition(to_state)

    async def create_calendar_recovery(
        self, task: HandoffTask, outbox_payload: dict[str, object]
    ) -> None:
        self._record(task, outbox_payload)

    async def create_withdrawal_handoff(
        self, task: HandoffTask, outbox_payload: dict[str, object]
    ) -> None:
        self._record(task, outbox_payload)

    async def get(self, tenant_id: UUID, task_id: str) -> HandoffTask | None:
        del tenant_id
        return next((task for task in self.tasks if task.task_id == task_id), None)

    def _record(self, task: HandoffTask, outbox_payload: dict[str, object]) -> None:
        if not any(existing.task_id == task.task_id for existing in self.tasks):
            self.tasks.append(task)
            self.payloads.append(outbox_payload)


class _RecordingPacer:
    """Proves every fixture source read/mutation is preceded by its dedicated Pacer lease."""

    def __init__(self) -> None:
        self.operations: list[PacerOperation] = []

    async def acquire(self, request: PacerRequest) -> PacerLease:
        self.operations.append(request.operation)
        return PacerLease(PacerLeaseStatus.GRANTED)

    async def observe_backoff(
        self,
        request: PacerRequest,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        del request, retry_after_seconds, reset_at


class _KillSwitchEngagingPacer(_RecordingPacer):
    """Engage a fixture kill switch after one withdrawal Pacer grant."""

    def __init__(self, policy: DataPolicyEngine, operation: PacerOperation) -> None:
        super().__init__()
        self._policy = policy
        self._operation = operation
        self.engaged = False

    async def acquire(self, request: PacerRequest) -> PacerLease:
        lease = await super().acquire(request)
        if request.operation is self._operation and not self.engaged:
            self._policy.set_kill_switch(True)
            self.engaged = True
        return lease


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Lifecycle fixture",
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 20, 0, tzinfo=UTC),
        venue_name="Fixture Hall",
        price_status=PriceStatus.FREE,
        source_links=[
            EventSourceLink(
                source=Source.MEETUP,
                source_event_id="lifecycle-fixture",
                registration_url="https://meetup.example/lifecycle-fixture",
                price_status=PriceStatus.FREE,
            )
        ],
    )


def _service(
    lifecycle: Lifecycle,
    calendar: MockCalendar,
    *,
    withdrawals: dict[Source, RegistrationWithdrawalPort] | None = None,
    pacer: _RecordingPacer | None = None,
    policy: DataPolicyEngine | None = None,
) -> tuple[
    LifecycleReconciliationService, _LifecycleRepository, _HandoffRepository, _RecordingPacer
]:
    repository = _LifecycleRepository(lifecycle)
    handoff = _HandoffRepository()
    recorded_pacer = pacer or _RecordingPacer()
    service = LifecycleReconciliationService(
        calendar,
        repository,
        handoff,
        policy
        or DataPolicyEngine(
            source_policies={
                Source.MEETUP: SourcePolicy(
                    source=Source.MEETUP,
                    automation_allowed={Modality.API: True},
                )
            }
        ),
        recorded_pacer,
        withdrawals or {},
        now=lambda: datetime(2026, 7, 20, tzinfo=UTC),
    )
    return service, repository, handoff, recorded_pacer


async def _seed_calendar(calendar: MockCalendar, tenant_id: UUID, event: CanonicalEvent) -> None:
    await calendar.upsert_event(
        tenant_id,
        CalendarEntry(
            calendar_event_id=ids.calendar_event_id(tenant_id, event.canonical_event_id),
            canonical_event_id=event.canonical_event_id,
            title=event.title,
            start_at=event.start_at,
            end_at=event.end_at,
            time_zone="UTC",
            location=event.venue_name,
        ),
    )


async def test_organizer_cancellation_deletes_one_deterministic_entry_then_terminalizes() -> None:
    """A cancellation converges to one calendar removal and one guarded transition (FR-8.7)."""
    tenant_id, event = uuid4(), _event()
    lifecycle = Lifecycle(
        uuid4(), tenant_id, event.canonical_event_id, "tenant:event", LifecycleState.SCHEDULED
    )
    calendar = MockCalendar()
    await _seed_calendar(calendar, tenant_id, event)
    service, repository, _, _ = _service(lifecycle, calendar)

    result = await service.reconcile_organizer_change(
        tenant_id,
        event,
        lifecycle.workflow_id,
        OrganizerChange("cancel-v1", Source.MEETUP, EventStatus.CANCELLED),
        transition_id="tenant:event:reconcile:cancel-v1:1",
    )

    assert result.status is ReconcileStatus.CANCELLED
    assert lifecycle.state is LifecycleState.CANCELLED
    assert calendar.entries(tenant_id) == []
    assert repository.transitions == [
        ("tenant:event:reconcile:cancel-v1:1", LifecycleState.CANCELLED)
    ]


async def test_multiple_reschedules_patch_the_same_calendar_id_and_remain_active() -> None:
    """A reconciled lifecycle accepts a later organizer update instead of dropping its watch (ADR-008)."""
    tenant_id, event = uuid4(), _event()
    lifecycle = Lifecycle(
        uuid4(), tenant_id, event.canonical_event_id, "tenant:event", LifecycleState.SCHEDULED
    )
    calendar = MockCalendar()
    await _seed_calendar(calendar, tenant_id, event)
    new_start = event.start_at + timedelta(days=1)
    calendar.seed_busy(
        tenant_id,
        [BusyBlock(start=new_start + timedelta(minutes=30), end=new_start + timedelta(hours=1))],
    )
    service, repository, _, _ = _service(lifecycle, calendar)

    first = await service.reconcile_organizer_change(
        tenant_id,
        event,
        lifecycle.workflow_id,
        OrganizerChange(
            "reschedule-v1", Source.MEETUP, EventStatus.RESCHEDULED, start_at=new_start
        ),
        transition_id="tenant:event:reconcile:reschedule-v1:1",
    )
    later_start = new_start + timedelta(days=1)
    second = await service.reconcile_organizer_change(
        tenant_id,
        event,
        lifecycle.workflow_id,
        OrganizerChange(
            "reschedule-v2", Source.MEETUP, EventStatus.RESCHEDULED, start_at=later_start
        ),
        transition_id="tenant:event:reconcile:reschedule-v2:1",
    )

    entries = calendar.entries(tenant_id)
    assert first.status is ReconcileStatus.RECONCILED
    assert first.conflict_warning is True
    assert second.status is ReconcileStatus.RECONCILED
    assert lifecycle.state is LifecycleState.RECONCILED
    assert len(entries) == 1
    assert entries[0].calendar_event_id == ids.calendar_event_id(
        tenant_id, event.canonical_event_id
    )
    assert entries[0].start_at == later_start
    assert first.completion_deadline_at == new_start + timedelta(days=1, hours=2)
    assert second.completion_deadline_at == later_start + timedelta(days=1, hours=2)
    assert [state for _, state in repository.transitions] == [
        LifecycleState.RECONCILED,
        LifecycleState.RECONCILED,
    ]


async def test_quiet_completion_uses_one_guarded_terminal_transition_and_recovers_a_lost_ack() -> (
    None
):
    """An activity retry observes an already-completed lifecycle instead of writing a second outbox row (ADR-007)."""
    tenant_id, event = uuid4(), _event()
    lifecycle = Lifecycle(
        uuid4(), tenant_id, event.canonical_event_id, "tenant:event", LifecycleState.SCHEDULED
    )
    service, repository, _, _ = _service(lifecycle, MockCalendar())

    completed = await service.complete_lifecycle(
        tenant_id,
        event,
        lifecycle.workflow_id,
        transition_id="tenant:event:completed:1",
    )
    recovered = await service.complete_lifecycle(
        tenant_id,
        event,
        lifecycle.workflow_id,
        transition_id="tenant:event:completed:1",
    )

    assert completed.status is LifecycleCompletionStatus.COMPLETED
    assert recovered.status is LifecycleCompletionStatus.COMPLETED
    assert lifecycle.state is LifecycleState.COMPLETED
    assert repository.transitions == [("tenant:event:completed:1", LifecycleState.COMPLETED)]


@pytest.mark.parametrize(
    "state",
    (
        LifecycleState.HANDOFF,
        LifecycleState.REGISTERED,
        LifecycleState.WITHDRAWING,
    ),
)
async def test_handoff_expiry_terminalizes_each_task_owning_lifecycle_once(
    state: LifecycleState,
) -> None:
    """All ADR-007 human-task paths converge after a lost expiry activity acknowledgement."""
    tenant_id, event = uuid4(), _event()
    lifecycle = Lifecycle(uuid4(), tenant_id, event.canonical_event_id, "tenant:event", state)
    service, repository, handoffs, _ = _service(lifecycle, MockCalendar())
    task = HandoffTask(
        task_id="tenant:event:task",
        tenant_id=tenant_id,
        workflow_id=lifecycle.workflow_id,
        canonical_event_id=event.canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link="https://example.test/handoff",
        event_summary="Persisted handoff summary",
        ttl_expires_at=datetime(2026, 7, 20, tzinfo=UTC),
        expiry_transition_id="tenant:event:task-expired:1",
    )
    await handoffs.create(task)

    first = await service.expire_handoff(
        tenant_id,
        event.canonical_event_id,
        lifecycle.workflow_id,
        task_id="tenant:event:task",
        expiry_transition_id="tenant:event:task-expired:1",
    )
    recovered = await service.expire_handoff(
        tenant_id,
        event.canonical_event_id,
        lifecycle.workflow_id,
        task_id="tenant:event:task",
        expiry_transition_id="tenant:event:task-expired:1",
    )

    assert first.status is HandoffExpiryStatus.EXPIRED
    assert recovered.status is HandoffExpiryStatus.EXPIRED
    assert lifecycle.state is LifecycleState.EXPIRED
    assert repository.transitions == [("tenant:event:task-expired:1", LifecycleState.EXPIRED)]
    assert repository.transition_payloads[0]["event_summary"] == "Persisted handoff summary"


async def test_handoff_expiry_never_overwrites_a_verified_completion_commit() -> None:
    """A completed task proves mark-done won even if its activity acknowledgement was lost."""
    tenant_id, event = uuid4(), _event()
    lifecycle = Lifecycle(
        uuid4(),
        tenant_id,
        event.canonical_event_id,
        "tenant:event",
        LifecycleState.REGISTERED,
    )
    service, repository, handoffs, _ = _service(lifecycle, MockCalendar())
    task = HandoffTask(
        task_id="tenant:event:verified-task",
        tenant_id=tenant_id,
        workflow_id=lifecycle.workflow_id,
        canonical_event_id=event.canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link="https://example.test/handoff",
        event_summary="Verified handoff summary",
        ttl_expires_at=datetime(2026, 7, 20, tzinfo=UTC),
        expiry_transition_id="tenant:event:verified-expiry:1",
        state=HandoffState.COMPLETED,
    )
    await handoffs.create(task)

    result = await service.expire_handoff(
        tenant_id,
        event.canonical_event_id,
        lifecycle.workflow_id,
        task_id=task.task_id,
        expiry_transition_id=task.resolved_expiry_transition_id(),
    )

    assert result.status is HandoffExpiryStatus.COMPLETION_COMMITTED
    assert lifecycle.state is LifecycleState.REGISTERED
    assert repository.transitions == []


async def test_unrsvp_recovery_after_source_ack_loss_has_one_withdrawal_effect() -> None:
    """A retry re-reads NOT_PRESENT after a lost ACK and never calls withdrawal twice (FR-8.8)."""
    tenant_id, event = uuid4(), _event()
    lifecycle = Lifecycle(
        uuid4(),
        tenant_id,
        event.canonical_event_id,
        "tenant:event",
        LifecycleState.SCHEDULED,
        lane=Lane.AUTONOMOUS_SLA,
        registration_source=Source.MEETUP,
    )
    calendar = MockCalendar()
    await _seed_calendar(calendar, tenant_id, event)
    source = ConfirmingSource(Source.MEETUP, raise_after_withdraw_effect_once=True)
    target = event.source_links[0]
    await source.register(
        tenant_id,
        RegistrationTarget(target.source_event_id, target.registration_url),
        Modality.API,
        "seed-registration",
    )
    service, _, _, pacer = _service(lifecycle, calendar, withdrawals={Source.MEETUP: source})

    with pytest.raises(RuntimeError, match="withdrawal acknowledgement loss"):
        await service.request_unrsvp(
            tenant_id,
            event,
            lifecycle.workflow_id,
            "unrsvp-1",
            withdrawing_transition_id="tenant:event:unrsvp-1:withdrawing",
            cancelled_transition_id="tenant:event:unrsvp-1:cancelled",
            handoff_expiry_transition_id="tenant:event:unrsvp-1:expired",
            source_read_queue_item_id="tenant:event:unrsvp-1:read",
            source_mutation_idempotency_key="tenant:event:unrsvp-1:withdraw",
        )
    recovered = await service.request_unrsvp(
        tenant_id,
        event,
        lifecycle.workflow_id,
        "unrsvp-1",
        withdrawing_transition_id="tenant:event:unrsvp-1:withdrawing",
        cancelled_transition_id="tenant:event:unrsvp-1:cancelled",
        handoff_expiry_transition_id="tenant:event:unrsvp-1:expired",
        source_read_queue_item_id="tenant:event:unrsvp-1:read",
        source_mutation_idempotency_key="tenant:event:unrsvp-1:withdraw",
    )

    assert recovered.status is UnrsvpStatus.CANCELLED
    assert lifecycle.state is LifecycleState.CANCELLED
    assert calendar.entries(tenant_id) == []
    assert source.withdrawal_effects == 1
    assert source.withdraw_attempts == 1
    assert source.withdrawal_idempotency_keys == ["tenant:event:unrsvp-1:withdraw"]
    assert pacer.operations == [
        PacerOperation.WITHDRAWAL_READ,
        PacerOperation.WITHDRAWAL_MUTATION,
        PacerOperation.WITHDRAWAL_READ,
    ]


@pytest.mark.parametrize(
    ("operation", "expected_events"),
    [
        (PacerOperation.WITHDRAWAL_READ, []),
        (PacerOperation.WITHDRAWAL_MUTATION, ["source:read:confirmed"]),
    ],
)
async def test_policy_flip_after_final_pacer_grant_blocks_withdrawal_source_io(
    operation: PacerOperation,
    expected_events: list[str],
) -> None:
    """A policy flip during withdrawal admission yields handoff without a later source call."""
    tenant_id, event = uuid4(), _event()
    lifecycle = Lifecycle(
        uuid4(),
        tenant_id,
        event.canonical_event_id,
        "tenant:event",
        LifecycleState.SCHEDULED,
        lane=Lane.AUTONOMOUS_SLA,
        registration_source=Source.MEETUP,
    )
    calendar = MockCalendar()
    await _seed_calendar(calendar, tenant_id, event)
    events: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=events)
    target = event.source_links[0]
    await source.register(
        tenant_id,
        RegistrationTarget(target.source_event_id, target.registration_url),
        Modality.API,
        "seed-registration",
    )
    events.clear()
    policy = DataPolicyEngine(
        source_policies={
            Source.MEETUP: SourcePolicy(
                source=Source.MEETUP,
                automation_allowed={Modality.API: True},
            )
        }
    )
    pacer = _KillSwitchEngagingPacer(policy, operation)
    service, _, handoff, _ = _service(
        lifecycle,
        calendar,
        withdrawals={Source.MEETUP: source},
        pacer=pacer,
        policy=policy,
    )

    result = await service.request_unrsvp(
        tenant_id,
        event,
        lifecycle.workflow_id,
        f"policy-flip-after-{operation.value}",
        withdrawing_transition_id="tenant:event:policy-flip:withdrawing",
        cancelled_transition_id="tenant:event:policy-flip:cancelled",
        handoff_expiry_transition_id="tenant:event:policy-flip:expired",
        source_read_queue_item_id="tenant:event:policy-flip:read",
        source_mutation_idempotency_key="tenant:event:policy-flip:withdraw",
    )

    assert result.status is UnrsvpStatus.HANDOFF
    assert pacer.engaged is True
    assert events == expected_events
    assert source.withdraw_attempts == 0
    assert source.withdrawal_effects == 0
    assert lifecycle.state is LifecycleState.WITHDRAWING
    assert len(handoff.tasks) == 1


async def test_unsupported_withdrawal_creates_one_handoff_and_keeps_truthful_withdrawing_state() -> (
    None
):
    """Unsupported source withdrawal removes calendar intent but never pretends the RSVP is gone (FR-8.8)."""
    tenant_id, event = uuid4(), _event()
    lifecycle = Lifecycle(
        uuid4(),
        tenant_id,
        event.canonical_event_id,
        "tenant:event",
        LifecycleState.SCHEDULED,
        lane=Lane.AUTONOMOUS_SLA,
        registration_source=Source.MEETUP,
    )
    calendar = MockCalendar()
    await _seed_calendar(calendar, tenant_id, event)
    source = ConfirmingSource(Source.MEETUP, withdrawal_supported=False)
    source_link = event.source_links[0]
    await source.register(
        tenant_id,
        RegistrationTarget(source_link.source_event_id, source_link.registration_url),
        Modality.API,
        "seed-registration",
    )
    service, _, handoff, _ = _service(lifecycle, calendar, withdrawals={Source.MEETUP: source})

    result = await service.request_unrsvp(
        tenant_id,
        event,
        lifecycle.workflow_id,
        "unrsvp-handoff",
        withdrawing_transition_id="tenant:event:unrsvp-handoff:withdrawing",
        cancelled_transition_id="tenant:event:unrsvp-handoff:cancelled",
        handoff_expiry_transition_id="tenant:event:unrsvp-handoff:expired",
        source_read_queue_item_id="tenant:event:unrsvp-handoff:read",
        source_mutation_idempotency_key="tenant:event:unrsvp-handoff:withdraw",
    )

    assert result.status is UnrsvpStatus.HANDOFF
    assert lifecycle.state is LifecycleState.WITHDRAWING
    assert calendar.entries(tenant_id) == []
    assert len(handoff.tasks) == 1
    assert handoff.tasks[0].state is HandoffState.OPEN
    assert handoff.payloads[0]["reason"] == "withdrawal_required"
