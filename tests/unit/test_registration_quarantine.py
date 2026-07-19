"""Source-ban recovery tests for the paced registration saga (FR-10.3, AC-72)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.consent import MockRegistrationConsentEvidence
from events_concierge.adapters.mock.policy import (
    MockPolicySnapshotReader,
    MockSourceQuarantineRepository,
)
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.engine import StoreBackedPolicyEngine
from events_concierge.adapters.policy.pacer import InMemoryPacer
from events_concierge.application.registration import RegistrationService, RegistrationStatus
from events_concierge.domain.enums import (
    ConsentScope,
    GroupCondition,
    Lane,
    LifecycleState,
    Modality,
    PriceStatus,
    RsvpState,
    Source,
)
from events_concierge.domain.events import CanonicalEvent, EventSourceLink
from events_concierge.domain.lifecycle import HandoffTask, Lifecycle
from events_concierge.domain.policy import SourcePolicy, SourceQuarantineSignal
from events_concierge.ports.repositories import HandoffRepository, LifecycleRepository
from events_concierge.ports.sources import (
    RegisterOutcome,
    RegisterResult,
    RegistrationTarget,
    SourceAccessDeniedError,
)


class _LifecycleRepository:
    """Small in-memory lifecycle seam for direct-facade terminal handoff assertions."""

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
    """Minimal durable-task seam that preserves replay lookup behavior."""

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


class _BoundaryDeniedSource(ConfirmingSource):
    """Fixture source that emits a closed ban signal at exactly one provider boundary."""

    def __init__(self, boundary: str) -> None:
        super().__init__(Source.MEETUP)
        self._boundary = boundary
        self.membership_attempts = 0
        self.registration_read_attempts = 0
        self.mutation_attempts = 0

    async def read_membership_state(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> GroupCondition:
        self.membership_attempts += 1
        if self._boundary == "membership":
            raise SourceAccessDeniedError(SourceQuarantineSignal.FORBIDDEN)
        return await super().read_membership_state(tenant_id, target)

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        self.registration_read_attempts += 1
        if self._boundary == "read":
            raise SourceAccessDeniedError(SourceQuarantineSignal.BAN)
        return await super().read_registration_state(tenant_id, target, modality)

    async def register(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality, idempotency_key: str
    ) -> RegisterResult:
        self.mutation_attempts += 1
        if self._boundary == "mutation":
            raise SourceAccessDeniedError(SourceQuarantineSignal.FORBIDDEN)
        return await super().register(tenant_id, target, modality, idempotency_key)


class _MembershipReadFailureSource(ConfirmingSource):
    """Fixture source whose membership boundary is unavailable without a ban/403 signal."""

    def __init__(self) -> None:
        super().__init__(Source.MEETUP)
        self.membership_attempts = 0

    async def read_membership_state(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> GroupCondition:
        del tenant_id, target
        self.membership_attempts += 1
        raise RuntimeError("simulated membership provider outage")


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Quarantine fixture",
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        price_status=PriceStatus.FREE,
        source_links=[
            EventSourceLink(
                source=Source.MEETUP,
                source_event_id="quarantine-fixture",
                registration_url="https://meetup.example/quarantine-fixture",
                price_status=PriceStatus.FREE,
            )
        ],
    )


def _service(
    source: ConfirmingSource,
) -> tuple[
    RegistrationService,
    dict[Source, SourcePolicy],
    MockSourceQuarantineRepository,
    _HandoffRepository,
    MockRegistrationConsentEvidence,
]:
    policies = {
        Source.MEETUP: SourcePolicy(
            source=Source.MEETUP,
            automation_allowed={Modality.API: True},
        )
    }
    quarantine = MockSourceQuarantineRepository(policies)
    handoff = _HandoffRepository()
    consent = MockRegistrationConsentEvidence()
    service = RegistrationService(
        {Source.MEETUP: source},
        StoreBackedPolicyEngine(MockPolicySnapshotReader(policies)),
        InMemoryPacer(),
        MockCalendar(),
        cast(LifecycleRepository, _LifecycleRepository()),
        cast(HandoffRepository, handoff),
        registration_consent=consent,
        source_quarantine=quarantine,
    )
    return service, policies, quarantine, handoff, consent


def _grant_registration_consent(consent: MockRegistrationConsentEvidence, tenant_id: UUID) -> None:
    """Seed the exact owner-side fixture evidence for a deliberate Meetup source call."""
    consent.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )


async def test_unknown_membership_removes_autonomous_lane_and_routes_to_handoff() -> None:
    """An unknown Meetup membership state cannot reach an RSVP mutation (FR-5.2, ADR-003/005)."""
    source = ConfirmingSource(Source.MEETUP, membership_state=GroupCondition.UNKNOWN)
    service, _, _, handoff, consent = _service(source)
    tenant_id = uuid4()
    event = _event()
    workflow_id = f"{tenant_id}:{event.canonical_event_id}"
    _grant_registration_consent(consent, tenant_id)

    resolution = await service.resolve_membership(
        tenant_id,
        event,
        workflow_id,
        (Lane.AUTONOMOUS_SLA, Lane.HANDOFF),
    )
    result = await service.register_event(
        tenant_id,
        event,
        workflow_id,
        (Lane.AUTONOMOUS_SLA, Lane.HANDOFF),
    )

    assert resolution.status == "ready"
    assert resolution.lane_plan == (Lane.HANDOFF,)
    assert result.status is RegistrationStatus.HANDOFF
    assert result.lane is Lane.HANDOFF
    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert len(handoff.tasks) == 1


async def test_membership_read_error_removes_autonomous_lane_and_routes_to_handoff() -> None:
    """An unavailable membership read fails closed before any RSVP mutation (FR-5.2, ADR-003/005)."""
    source = _MembershipReadFailureSource()
    service, _, _, handoff, consent = _service(source)
    tenant_id = uuid4()
    event = _event()
    workflow_id = f"{tenant_id}:{event.canonical_event_id}"
    _grant_registration_consent(consent, tenant_id)

    resolution = await service.resolve_membership(
        tenant_id,
        event,
        workflow_id,
        (Lane.AUTONOMOUS_SLA, Lane.HANDOFF),
    )
    result = await service.register_event(
        tenant_id,
        event,
        workflow_id,
        (Lane.AUTONOMOUS_SLA, Lane.HANDOFF),
    )

    assert resolution.status == "ready"
    assert resolution.lane_plan == (Lane.HANDOFF,)
    assert result.status is RegistrationStatus.HANDOFF
    assert result.lane is Lane.HANDOFF
    assert source.membership_attempts == 2
    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert len(handoff.tasks) == 1


async def test_ban_on_membership_read_routes_direct_facade_to_handoff_without_another_lane() -> (
    None
):
    """A global source ban does not fall through the local facade to another autonomous lane."""
    source = _BoundaryDeniedSource("membership")
    service, policies, quarantine, handoff, consent = _service(source)
    tenant_id = uuid4()
    event = _event()
    _grant_registration_consent(consent, tenant_id)

    first = await service.register_event(
        tenant_id,
        event,
        f"{tenant_id}:{event.canonical_event_id}",
        (Lane.AUTONOMOUS_SLA, Lane.BROWSER_BEST_EFFORT, Lane.HANDOFF),
    )
    replay = await service.register_event(
        tenant_id,
        event,
        f"{tenant_id}:{event.canonical_event_id}",
        (Lane.AUTONOMOUS_SLA, Lane.BROWSER_BEST_EFFORT, Lane.HANDOFF),
    )

    assert first.status is RegistrationStatus.HANDOFF
    assert replay.status is RegistrationStatus.HANDOFF
    assert source.membership_attempts == 1
    assert source.registration_read_attempts == 0
    assert source.mutation_attempts == 0
    assert policies[Source.MEETUP].quarantined is True
    assert quarantine.calls == [(Source.MEETUP, SourceQuarantineSignal.FORBIDDEN)]
    assert len(handoff.tasks) == 1


async def test_ban_on_read_is_a_terminal_outcome_and_retry_never_reaches_the_provider() -> None:
    """A post-commit retry observes the fresh quarantine before its second source read."""
    source = _BoundaryDeniedSource("read")
    service, policies, quarantine, _, consent = _service(source)
    tenant_id = uuid4()
    event = _event()
    _grant_registration_consent(consent, tenant_id)

    first = await service.register_or_rsvp(
        tenant_id, event, Lane.AUTONOMOUS_SLA, "quarantine-read-key"
    )
    replay = await service.register_or_rsvp(
        tenant_id, event, Lane.AUTONOMOUS_SLA, "quarantine-read-key"
    )

    assert first.outcome is RegisterOutcome.SOURCE_QUARANTINED
    assert replay.outcome is RegisterOutcome.SOURCE_QUARANTINED
    assert source.registration_read_attempts == 1
    assert source.mutation_attempts == 0
    assert policies[Source.MEETUP].quarantined is True
    assert quarantine.calls == [(Source.MEETUP, SourceQuarantineSignal.BAN)]


async def test_ban_on_mutation_is_terminal_and_retry_does_not_repeat_the_wire_effect() -> None:
    """The fresh read guard catches a quarantine before a retried RSVP mutation (AC-72)."""
    source = _BoundaryDeniedSource("mutation")
    service, policies, quarantine, _, consent = _service(source)
    tenant_id = uuid4()
    event = _event()
    _grant_registration_consent(consent, tenant_id)

    first = await service.register_or_rsvp(
        tenant_id, event, Lane.AUTONOMOUS_SLA, "quarantine-mutation-key"
    )
    replay = await service.register_or_rsvp(
        tenant_id, event, Lane.AUTONOMOUS_SLA, "quarantine-mutation-key"
    )

    assert first.outcome is RegisterOutcome.SOURCE_QUARANTINED
    assert replay.outcome is RegisterOutcome.SOURCE_QUARANTINED
    assert source.registration_read_attempts == 1
    assert source.mutation_attempts == 1
    assert source.registration_effects == 0
    assert policies[Source.MEETUP].quarantined is True
    assert quarantine.calls == [(Source.MEETUP, SourceQuarantineSignal.FORBIDDEN)]
