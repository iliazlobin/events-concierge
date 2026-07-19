"""Fail-closed registration-consent evidence coverage (FR-2.9, FR-7.3, ADR-004)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.mock.audit import MockRegistrationActionAudit
from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.consent import MockRegistrationConsentEvidence
from events_concierge.adapters.mock.notification_secrets import (
    DevelopmentNotificationSecretProtector,
)
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.engine import DataPolicyEngine
from events_concierge.adapters.policy.pacer import InMemoryPacer
from events_concierge.application.registration import (
    ConfirmationStatus,
    RegistrationService,
    RegistrationStatus,
)
from events_concierge.domain.audit import (
    RegistrationActionAuditDecision,
    RegistrationActionAuditPhase,
)
from events_concierge.domain.enums import (
    ConsentScope,
    Lane,
    LifecycleState,
    Modality,
    PriceStatus,
    RsvpState,
    Source,
)
from events_concierge.domain.events import CanonicalEvent, EventSourceLink
from events_concierge.domain.lifecycle import HandoffTask, Lifecycle
from events_concierge.domain.policy import SourcePolicy
from events_concierge.ports.policy import (
    Pacer,
    PacerLease,
    PacerLeaseStatus,
    PacerOperation,
    PacerRequest,
)
from events_concierge.ports.repositories import HandoffRepository, LifecycleRepository
from events_concierge.ports.sources import RegisterOutcome, RegistrationTarget


class _LifecycleRepository:
    """Minimal lifecycle seam for the direct-facade no-source-call assertion."""

    def __init__(self) -> None:
        self.value: Lifecycle | None = None

    async def get_or_create(
        self, tenant_id: UUID, canonical_event_id: UUID, workflow_id: str
    ) -> Lifecycle:
        if self.value is None:
            self.value = Lifecycle(uuid4(), tenant_id, canonical_event_id, workflow_id)
        return self.value

    async def transition(
        self,
        lifecycle: Lifecycle,
        to_state: LifecycleState,
        transition_id: str,
        outbox_payload: dict[str, object],
    ) -> None:
        del transition_id, outbox_payload
        lifecycle.transition(to_state)


class _HandoffRepository:
    """Small durable-task seam used only when a denied facade routes to handoff."""

    def __init__(self) -> None:
        self.tasks: list[HandoffTask] = []

    async def create(self, task: HandoffTask) -> None:
        self.tasks.append(task)

    async def create_and_transition(
        self,
        task: HandoffTask,
        lifecycle: Lifecycle,
        to_state: LifecycleState,
        transition_id: str,
        outbox_payload: dict[str, object],
    ) -> None:
        del transition_id, outbox_payload
        self.tasks.append(task)
        lifecycle.transition(to_state)

    async def create_calendar_recovery(
        self, task: HandoffTask, outbox_payload: dict[str, object]
    ) -> None:
        del outbox_payload
        self.tasks.append(task)

    async def get(self, tenant_id: UUID, task_id: str) -> HandoffTask | None:
        del tenant_id
        return next((task for task in self.tasks if task.task_id == task_id), None)

    async def mark(self, task_id: str, tenant_id: UUID, state: str) -> None:
        del task_id, tenant_id, state


class _RecordingPacer:
    """Record whether a fail-closed activity reaches a Pacer boundary."""

    def __init__(self) -> None:
        self.requests: list[PacerRequest] = []

    async def acquire(self, request: PacerRequest) -> PacerLease:
        self.requests.append(request)
        return PacerLease(PacerLeaseStatus.GRANTED)

    async def observe_backoff(
        self,
        request: PacerRequest,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        del request, retry_after_seconds, reset_at


class _ConsentRemovingPacer(_RecordingPacer):
    """Remove fixture evidence immediately after the mutation permit is granted."""

    def __init__(
        self,
        evidence: MockRegistrationConsentEvidence,
        tenant_id: UUID,
    ) -> None:
        super().__init__()
        self._evidence = evidence
        self._tenant_id = tenant_id
        self.removed_after_mutation_lease = False

    async def acquire(self, request: PacerRequest) -> PacerLease:
        lease = await super().acquire(request)
        if request.operation is PacerOperation.REGISTRATION_MUTATION:
            self._evidence.remove(
                self._tenant_id,
                Source.MEETUP,
                Modality.API,
                ConsentScope.REGISTRATION,
            )
            self.removed_after_mutation_lease = True
        return lease


class _KillSwitchEngagingPacer(_RecordingPacer):
    """Flip the in-memory data plane after one final Pacer grant."""

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


class _ConfirmationVisibilitySource(ConfirmingSource):
    """Return the independently observed RSVP state for a confirmation wake-up."""

    def __init__(self) -> None:
        super().__init__(Source.MEETUP)
        self.confirmation_state = RsvpState.NOT_PRESENT
        self.confirmation_read_attempts = 0

    async def read_registration_state(
        self,
        tenant_id: UUID,
        target: RegistrationTarget,
        modality: Modality,
    ) -> RsvpState:
        del tenant_id, target, modality
        self.confirmation_read_attempts += 1
        return self.confirmation_state


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Consent fixture meetup",
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        price_status=PriceStatus.FREE,
        source_links=[
            EventSourceLink(
                source=Source.MEETUP,
                source_event_id="consent-fixture",
                registration_url="https://meetup.example/consent-fixture",
                price_status=PriceStatus.FREE,
            )
        ],
    )


def _service(
    source: ConfirmingSource,
    consent: MockRegistrationConsentEvidence,
    *,
    pacer: Pacer | None = None,
    audit: MockRegistrationActionAudit | None = None,
    lifecycle: _LifecycleRepository | None = None,
    policy: DataPolicyEngine | None = None,
) -> RegistrationService:
    return RegistrationService(
        {Source.MEETUP: source},
        policy
        or DataPolicyEngine(
            source_policies={
                Source.MEETUP: SourcePolicy(
                    source=Source.MEETUP,
                    automation_allowed={Modality.API: True},
                )
            }
        ),
        pacer or InMemoryPacer(),
        MockCalendar(),
        cast(LifecycleRepository, lifecycle or _LifecycleRepository()),
        cast(HandoffRepository, _HandoffRepository()),
        action_audit=audit,
        registration_consent=consent,
        notification_secret_protector=DevelopmentNotificationSecretProtector(),
    )


async def test_mock_registration_consent_evidence_is_exactly_bound_to_ratified_targets() -> None:
    """Fixture evidence rejects unratified tuples and cannot cross tenant/source boundaries."""
    evidence = MockRegistrationConsentEvidence()
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    meetup_ref = uuid4()
    luma_ref = uuid4()

    evidence.seed(
        meetup_ref,
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    evidence.seed(
        luma_ref,
        tenant_id,
        Source.LUMA,
        Modality.BROWSER,
        ConsentScope.REGISTRATION,
    )

    assert (
        await evidence.resolve(tenant_id, Source.MEETUP, Modality.API, ConsentScope.REGISTRATION)
        == meetup_ref
    )
    assert (
        await evidence.resolve(tenant_id, Source.LUMA, Modality.BROWSER, ConsentScope.REGISTRATION)
        == luma_ref
    )
    assert (
        await evidence.resolve(
            other_tenant_id, Source.MEETUP, Modality.API, ConsentScope.REGISTRATION
        )
        is None
    )
    with pytest.raises(ValueError, match="source/modality"):
        evidence.seed(
            uuid4(),
            tenant_id,
            Source.MEETUP,
            Modality.BROWSER,
            ConsentScope.REGISTRATION,
        )
    with pytest.raises(ValueError, match="source/modality"):
        await evidence.resolve(
            tenant_id, Source.MEETUP, Modality.BROWSER, ConsentScope.REGISTRATION
        )
    assert await evidence.validate(
        meetup_ref,
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    assert not await evidence.validate(
        meetup_ref,
        other_tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    assert not await evidence.validate(
        meetup_ref,
        tenant_id,
        Source.LUMA,
        Modality.BROWSER,
        ConsentScope.REGISTRATION,
    )
    with pytest.raises(ValueError, match="source/modality"):
        await evidence.validate(
            luma_ref,
            tenant_id,
            Source.LUMA,
            Modality.API,
            ConsentScope.REGISTRATION,
        )
    assert not await evidence.validate(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )


async def test_missing_registration_consent_has_zero_membership_read_and_mutation_effects() -> None:
    """A direct facade cannot make any provider call when no exact consent evidence exists."""
    tenant_id = uuid4()
    event = _event()
    source_log: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=source_log)
    evidence = MockRegistrationConsentEvidence()
    service = _service(source, evidence)

    result = await service.register_event(
        tenant_id,
        event,
        f"{tenant_id}:{event.canonical_event_id}",
        (Lane.AUTONOMOUS_SLA, Lane.HANDOFF),
    )

    assert result.status is RegistrationStatus.HANDOFF
    assert source_log == []
    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert evidence.resolve_calls == [
        (tenant_id, Source.MEETUP, Modality.API, ConsentScope.REGISTRATION),
        (tenant_id, Source.MEETUP, Modality.API, ConsentScope.REGISTRATION),
    ]
    assert evidence.validate_calls == []


async def test_missing_registration_consent_records_one_denied_workflow_precheck_without_source_io() -> (
    None
):
    """A workflow gate retains the explicit missing-evidence fact before it denies the lane."""
    tenant_id = uuid4()
    event = _event()
    source_log: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=source_log)
    evidence = MockRegistrationConsentEvidence()
    audit = MockRegistrationActionAudit()
    service = _service(source, evidence, audit=audit)

    gate = await service.policy_gate(
        tenant_id,
        event,
        Lane.AUTONOMOUS_SLA,
        workflow_id=f"{tenant_id}:{event.canonical_event_id}",
        source_idempotency_key="missing-consent-precheck",
    )

    assert not gate.allowed
    assert source_log == []
    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert [
        (record.phase, record.policy_decision, record.outcome, record.consent_ref)
        for record in audit.records
    ] == [
        (
            RegistrationActionAuditPhase.POLICY_PRECHECK,
            RegistrationActionAuditDecision.DENIED,
            None,
            None,
        )
    ]


async def test_missing_registration_consent_blocks_confirmation_poll_without_source_io() -> None:
    """An opaque confirmation wake-up cannot cause a provider poll without current evidence."""
    tenant_id = uuid4()
    event = _event()
    source_log: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=source_log)
    evidence = MockRegistrationConsentEvidence()
    pacer = _RecordingPacer()
    service = _service(source, evidence, pacer=pacer)

    result = await service.await_confirmation(
        tenant_id,
        event,
        f"{tenant_id}:{event.canonical_event_id}",
        Lane.AUTONOMOUS_SLA,
        RegisterOutcome.PENDING_CONFIRMATION,
        awaiting_transition_id="missing-consent-awaiting",
        registered_transition_id="missing-consent-registered",
        confirmation_reference="opaque-confirmation-wakeup",
    )

    assert result.status is ConfirmationStatus.FAILED
    assert source_log == []
    assert pacer.requests == []
    assert evidence.resolve_calls == [
        (tenant_id, Source.MEETUP, Modality.API, ConsentScope.REGISTRATION)
    ]


async def test_not_present_confirmation_read_keeps_the_lifecycle_awaiting_until_confirmed() -> None:
    """An opaque signal is never proof when the fresh source read has not caught up (FR-8.4/16.1)."""
    tenant_id = uuid4()
    event = _event()
    workflow_id = f"{tenant_id}:{event.canonical_event_id}"
    evidence = MockRegistrationConsentEvidence()
    evidence.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    source = _ConfirmationVisibilitySource()
    lifecycle = _LifecycleRepository()
    service = _service(source, evidence, lifecycle=lifecycle)

    initial = await service.await_confirmation(
        tenant_id,
        event,
        workflow_id,
        Lane.AUTONOMOUS_SLA,
        RegisterOutcome.PENDING_CONFIRMATION,
        awaiting_transition_id="awaiting-confirmation",
        registered_transition_id="registered",
    )
    still_unverified = await service.await_confirmation(
        tenant_id,
        event,
        workflow_id,
        Lane.AUTONOMOUS_SLA,
        RegisterOutcome.PENDING_CONFIRMATION,
        awaiting_transition_id="awaiting-confirmation",
        registered_transition_id="registered",
        confirmation_reference="opaque-out-of-band-wakeup",
    )

    assert initial.status is ConfirmationStatus.PENDING
    assert still_unverified.status is ConfirmationStatus.PENDING
    assert lifecycle.value is not None
    assert lifecycle.value.state.value == LifecycleState.AWAITING_CONFIRMATION.value
    assert source.confirmation_read_attempts == 1
    assert source.register_attempts == 0

    source.confirmation_state = RsvpState.CONFIRMED
    confirmed = await service.await_confirmation(
        tenant_id,
        event,
        workflow_id,
        Lane.AUTONOMOUS_SLA,
        RegisterOutcome.PENDING_CONFIRMATION,
        awaiting_transition_id="awaiting-confirmation",
        registered_transition_id="registered",
        confirmation_reference="opaque-confirmed-readback",
    )

    assert confirmed.status is ConfirmationStatus.CONFIRMED
    assert lifecycle.value.state.value == LifecycleState.REGISTERED.value
    assert source.confirmation_read_attempts == 2


async def test_consent_removed_after_mutation_pacer_lease_prevents_the_source_mutation() -> None:
    """The final evidence revalidation catches a post-lease removal before the RSVP wire call."""
    tenant_id = uuid4()
    event = _event()
    source = ConfirmingSource(Source.MEETUP)
    evidence = MockRegistrationConsentEvidence()
    evidence.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    pacer = _ConsentRemovingPacer(evidence, tenant_id)
    service = _service(source, evidence, pacer=pacer)

    result = await service.register_or_rsvp(
        tenant_id,
        event,
        Lane.AUTONOMOUS_SLA,
        "consent-disappeared-after-lease",
    )

    assert result.outcome is RegisterOutcome.NEEDS_HANDOFF
    assert pacer.removed_after_mutation_lease
    assert [request.operation for request in pacer.requests] == [
        PacerOperation.REGISTRATION_READ,
        PacerOperation.REGISTRATION_MUTATION,
    ]
    assert source.register_attempts == 0
    assert source.registration_effects == 0


@pytest.mark.parametrize(
    ("operation", "expected_source_events"),
    [
        (PacerOperation.REGISTRATION_READ, []),
        (PacerOperation.REGISTRATION_MUTATION, ["source:read:not_present"]),
    ],
)
async def test_policy_flip_after_final_pacer_grant_blocks_registration_source_io(
    operation: PacerOperation,
    expected_source_events: list[str],
) -> None:
    """A policy flip during admission blocks both source reads and the RSVP mutation (ADR-004)."""
    tenant_id = uuid4()
    event = _event()
    events: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=events)
    evidence = MockRegistrationConsentEvidence()
    evidence.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    policy = DataPolicyEngine(
        source_policies={
            Source.MEETUP: SourcePolicy(
                source=Source.MEETUP,
                automation_allowed={Modality.API: True},
            )
        }
    )
    pacer = _KillSwitchEngagingPacer(policy, operation)
    service = _service(source, evidence, pacer=pacer, policy=policy)

    result = await service.register_or_rsvp(
        tenant_id,
        event,
        Lane.AUTONOMOUS_SLA,
        f"policy-flip-after-{operation.value}",
    )

    assert result.outcome is RegisterOutcome.NEEDS_HANDOFF
    assert pacer.engaged is True
    assert events == expected_source_events
    assert source.register_attempts == 0
    assert source.registration_effects == 0


async def test_policy_flip_after_membership_pacer_grant_blocks_membership_source_io() -> None:
    """A newly engaged kill switch cannot spend a post-admission membership provider request."""
    tenant_id = uuid4()
    event = _event()
    events: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=events)
    evidence = MockRegistrationConsentEvidence()
    evidence.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    policy = DataPolicyEngine(
        source_policies={
            Source.MEETUP: SourcePolicy(
                source=Source.MEETUP,
                automation_allowed={Modality.API: True},
            )
        }
    )
    pacer = _KillSwitchEngagingPacer(policy, PacerOperation.MEMBERSHIP_READ)
    service = _service(source, evidence, pacer=pacer, policy=policy)

    resolution = await service.resolve_membership(
        tenant_id,
        event,
        f"{tenant_id}:{event.canonical_event_id}",
        (Lane.AUTONOMOUS_SLA, Lane.HANDOFF),
    )

    assert resolution.status == "ready"
    assert resolution.lane_plan == (Lane.AUTONOMOUS_SLA, Lane.HANDOFF)
    assert pacer.engaged is True
    assert events == []


async def test_valid_registration_consent_binds_the_same_non_null_ref_to_all_action_audit_facts() -> (
    None
):
    """The resolved opaque ref follows the one RSVP attempt, never event/provider detail."""
    tenant_id = uuid4()
    event = _event()
    workflow_id = f"{tenant_id}:{event.canonical_event_id}"
    source_key = "consent-audit-source-key"
    source = ConfirmingSource(Source.MEETUP)
    evidence = MockRegistrationConsentEvidence()
    consent_ref = uuid4()
    evidence.seed(
        consent_ref,
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    audit = MockRegistrationActionAudit()
    service = _service(source, evidence, audit=audit)

    gate = await service.policy_gate(
        tenant_id,
        event,
        Lane.AUTONOMOUS_SLA,
        workflow_id=workflow_id,
        source_idempotency_key=source_key,
    )
    result = await service.register_or_rsvp(
        tenant_id,
        event,
        Lane.AUTONOMOUS_SLA,
        source_key,
        workflow_id=workflow_id,
    )

    assert gate.allowed
    assert result.outcome is RegisterOutcome.CONFIRMED
    assert source.registration_effects == 1
    assert [record.consent_ref for record in audit.records] == [
        consent_ref,
        consent_ref,
        consent_ref,
    ]
