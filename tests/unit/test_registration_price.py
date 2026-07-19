"""Price-safety regression tests for the registration data plane (FR-5.9/FR-5.10)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.consent import MockRegistrationConsentEvidence
from events_concierge.adapters.mock.notification_secrets import (
    DevelopmentNotificationSecretProtector,
)
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.engine import DataPolicyEngine
from events_concierge.adapters.policy.pacer import InMemoryPacer
from events_concierge.application.registration import (
    CandidateCloseStatus,
    RegistrationService,
    RegistrationStatus,
)
from events_concierge.domain.conflict import BusyBlock
from events_concierge.domain.enums import (
    ConsentScope,
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
from events_concierge.ports.repositories import HandoffRepository, LifecycleRepository
from events_concierge.ports.sources import RegisterOutcome

_SECRETS = DevelopmentNotificationSecretProtector()


class _LifecycleRepository:
    def __init__(self) -> None:
        self._lifecycle: Lifecycle | None = None
        self.transitions: list[tuple[str, LifecycleState, dict[str, object]]] = []

    async def get_or_create(
        self, tenant_id: UUID, canonical_event_id: UUID, workflow_id: str
    ) -> Lifecycle:
        if self._lifecycle is None:
            self._lifecycle = Lifecycle(uuid4(), tenant_id, canonical_event_id, workflow_id)
        return self._lifecycle

    async def transition(
        self,
        lifecycle: Lifecycle,
        to_state: LifecycleState,
        transition_id: str,
        outbox_payload: dict[str, object],
    ) -> None:
        self.transitions.append((transition_id, to_state, outbox_payload))
        lifecycle.transition(to_state)


class _HandoffRepository:
    def __init__(self) -> None:
        self.tasks: list[HandoffTask] = []
        self.outbox_payloads: list[dict[str, object]] = []

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
        del transition_id
        self.tasks.append(task)
        self.outbox_payloads.append(outbox_payload)
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


class _UnavailableCalendar(MockCalendar):
    """Calendar seam proving an unavailable conflict check fails safe before source I/O."""

    async def free_busy(
        self, tenant_id: UUID, window_start: datetime, window_end: datetime
    ) -> list[BusyBlock]:
        del tenant_id, window_start, window_end
        raise RuntimeError("simulated calendar outage")


def _event(
    price_status: PriceStatus, link_price_status: PriceStatus = PriceStatus.FREE
) -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Ticketed meetup",
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        price_status=price_status,
        source_links=[
            EventSourceLink(
                source=Source.MEETUP,
                source_event_id="price-guard",
                registration_url="https://meetup.example/price-guard",
                price_status=link_price_status,
            )
        ],
    )


@pytest.mark.parametrize("price_status", [PriceStatus.PAID, PriceStatus.UNKNOWN])
async def test_non_verified_free_event_never_reaches_an_autonomous_source(
    price_status: PriceStatus,
) -> None:
    """Paid and unknown catalog results discover/rank normally but hand off before a source call (FR-5.10)."""
    source_log: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=source_log)
    lifecycle = _LifecycleRepository()
    handoff = _HandoffRepository()
    service = RegistrationService(
        {Source.MEETUP: source},
        DataPolicyEngine(
            source_policies={
                Source.MEETUP: SourcePolicy(
                    source=Source.MEETUP,
                    automation_allowed={Modality.API: True},
                    paid_allowed=True,
                )
            }
        ),
        InMemoryPacer(),
        MockCalendar(),
        cast(LifecycleRepository, lifecycle),
        cast(HandoffRepository, handoff),
        notification_secret_protector=_SECRETS,
    )
    tenant_id = uuid4()
    event = _event(price_status)

    resolved = await service.resolve_membership(
        tenant_id, event, "price-guard-workflow", (Lane.AUTONOMOUS_SLA, Lane.HANDOFF)
    )
    gate = await service.policy_gate(tenant_id, event, Lane.AUTONOMOUS_SLA)
    result = await service.register_or_rsvp(
        tenant_id, event, Lane.AUTONOMOUS_SLA, "price-guard-idempotency-key"
    )
    terminal = await service.register_event(
        tenant_id, event, "price-guard-workflow", (Lane.AUTONOMOUS_SLA, Lane.HANDOFF)
    )

    assert resolved.lane_plan == (Lane.HANDOFF,)
    assert gate.allowed is False
    assert result.outcome is RegisterOutcome.NEEDS_HANDOFF
    assert terminal.status is RegistrationStatus.HANDOFF
    assert terminal.lane is Lane.HANDOFF
    assert len(handoff.tasks) == 1
    assert handoff.tasks[0].state is HandoffState.OPEN
    assert source_log == []
    assert source.registration_effects == 0
    assert source.register_attempts == 0


async def test_handoff_notification_uses_the_configured_absolute_completion_origin() -> None:
    """Email-facing capabilities are reachable URLs, never a process-relative path."""
    lifecycle = _LifecycleRepository()
    handoff = _HandoffRepository()
    service = RegistrationService(
        {},
        DataPolicyEngine(source_policies={}),
        InMemoryPacer(),
        MockCalendar(),
        cast(LifecycleRepository, lifecycle),
        cast(HandoffRepository, handoff),
        handoff_completion_base_url="https://concierge.example.test/",
        require_https_completion_links=True,
        notification_secret_protector=_SECRETS,
    )
    tenant_id = uuid4()
    event = _event(PriceStatus.FREE)
    workflow_id = f"{tenant_id}:{event.canonical_event_id}"

    await service.route_to_handoff(
        tenant_id,
        event,
        workflow_id,
        handoff_task_id=f"{workflow_id}:handoff",
        handoff_transition_id=f"{workflow_id}:handoff:1",
    )

    payload = handoff.outbox_payloads[0]
    assert "completion_url" not in payload
    protected_url = payload["protected_completion_url"]
    assert isinstance(protected_url, str)
    completion_url = await DevelopmentNotificationSecretProtector().reveal_completion_url(
        tenant_id,
        protected_url,
    )
    if not (
        completion_url.startswith("https://concierge.example.test/v1/tasks/")
        and completion_url.endswith("/done")
    ):
        raise AssertionError("completion capability did not use the configured HTTPS origin")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://concierge.example.test:0",
        "https://concierge.example.test:65536",
        "https://concierge.example.test:not-a-port",
        "https://concierge.example.test:",
        "https://[::1",
        "https://concierge.example.test/path\ninjected",
        "https://concierge.example.test/path\u200bhidden",
        "https://concierge.example.test?",
        "https://concierge.example.test#",
    ],
)
def test_handoff_completion_origin_rejects_malformed_ports_and_controls(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="handoff completion base URL"):
        RegistrationService(
            {},
            DataPolicyEngine(source_policies={}),
            InMemoryPacer(),
            MockCalendar(),
            cast(LifecycleRepository, _LifecycleRepository()),
            cast(HandoffRepository, _HandoffRepository()),
            handoff_completion_base_url=base_url,
            require_https_completion_links=True,
        )


def test_handoff_completion_origin_allows_a_valid_explicit_https_port() -> None:
    RegistrationService(
        {},
        DataPolicyEngine(source_policies={}),
        InMemoryPacer(),
        MockCalendar(),
        cast(LifecycleRepository, _LifecycleRepository()),
        cast(HandoffRepository, _HandoffRepository()),
        handoff_completion_base_url="https://concierge.example.test:8443",
        require_https_completion_links=True,
    )


async def test_free_canonical_with_a_non_free_target_link_never_reaches_source() -> None:
    """A malformed aggregate cannot select a paid link for an autonomous RSVP (FR-5.10)."""
    source_log: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=source_log)
    service = RegistrationService(
        {Source.MEETUP: source},
        DataPolicyEngine(
            source_policies={
                Source.MEETUP: SourcePolicy(
                    source=Source.MEETUP,
                    automation_allowed={Modality.API: True},
                    paid_allowed=True,
                )
            }
        ),
        InMemoryPacer(),
        MockCalendar(),
        cast(LifecycleRepository, _LifecycleRepository()),
        cast(HandoffRepository, _HandoffRepository()),
    )

    result = await service.register_or_rsvp(
        uuid4(),
        _event(PriceStatus.FREE, PriceStatus.PAID),
        Lane.AUTONOMOUS_SLA,
        "paid-target-link",
    )

    assert result.outcome is RegisterOutcome.NEEDS_HANDOFF
    assert source_log == []
    assert source.registration_effects == 0
    assert source.register_attempts == 0


async def test_fresh_hard_calendar_conflict_blocks_a_stale_registration_attempt() -> None:
    """A post-feed hard overlap fails safe before any RSVP wire call (FR-4.5, ADR-003)."""
    source_log: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=source_log)
    calendar = MockCalendar()
    lifecycle = _LifecycleRepository()
    handoff = _HandoffRepository()
    consent = MockRegistrationConsentEvidence()
    service = RegistrationService(
        {Source.MEETUP: source},
        DataPolicyEngine(
            source_policies={
                Source.MEETUP: SourcePolicy(
                    source=Source.MEETUP,
                    automation_allowed={Modality.API: True},
                )
            }
        ),
        InMemoryPacer(),
        calendar,
        cast(LifecycleRepository, lifecycle),
        cast(HandoffRepository, handoff),
        registration_consent=consent,
        notification_secret_protector=_SECRETS,
    )
    tenant_id = uuid4()
    consent.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    event = _event(PriceStatus.FREE)
    event_end = event.start_at.replace(hour=20)
    calendar.seed_busy(
        tenant_id,
        [BusyBlock(start=event.start_at.replace(hour=19), end=event_end)],
    )

    result = await service.register_event(
        tenant_id,
        event,
        "fresh-conflict-workflow",
        (Lane.AUTONOMOUS_SLA, Lane.HANDOFF),
    )

    assert result.status is RegistrationStatus.HANDOFF
    assert len(handoff.tasks) == 1
    assert source_log == ["source:membership:member"]
    assert source.register_attempts == 0
    assert source.registration_effects == 0


async def test_unavailable_fresh_conflict_check_fails_safe_before_source_io() -> None:
    """A calendar outage cannot turn into an unchecked RSVP (FR-4.5, NFR-15)."""
    source_log: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=source_log)
    consent = MockRegistrationConsentEvidence()
    service = RegistrationService(
        {Source.MEETUP: source},
        DataPolicyEngine(
            source_policies={
                Source.MEETUP: SourcePolicy(
                    source=Source.MEETUP,
                    automation_allowed={Modality.API: True},
                )
            }
        ),
        InMemoryPacer(),
        _UnavailableCalendar(),
        cast(LifecycleRepository, _LifecycleRepository()),
        cast(HandoffRepository, _HandoffRepository()),
        registration_consent=consent,
    )
    tenant_id = uuid4()
    consent.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )

    result = await service.register_or_rsvp(
        tenant_id, _event(PriceStatus.FREE), Lane.AUTONOMOUS_SLA, "calendar-unavailable"
    )

    assert result.outcome is RegisterOutcome.NEEDS_HANDOFF
    assert source_log == []
    assert source.register_attempts == 0
    assert source.registration_effects == 0


async def test_parent_fallthrough_terminalizes_found_candidate_without_a_user_notification() -> (
    None
):
    """A declined child cannot leave a closed workflow paired with active ``FOUND`` state (ADR-003/007)."""
    lifecycle = _LifecycleRepository()
    service = RegistrationService(
        {},
        DataPolicyEngine(source_policies={}),
        InMemoryPacer(),
        MockCalendar(),
        cast(LifecycleRepository, lifecycle),
        cast(HandoffRepository, _HandoffRepository()),
    )
    tenant_id = uuid4()
    event = _event(PriceStatus.FREE)
    workflow_id = "close-candidate-workflow"
    await service.resolve_membership(tenant_id, event, workflow_id, (Lane.HANDOFF,))

    closed = await service.close_failed_candidate(
        tenant_id,
        event,
        workflow_id,
        transition_id="close-candidate-workflow:close:1",
        reason="parent_fallthrough",
    )
    recovered = await service.close_failed_candidate(
        tenant_id,
        event,
        workflow_id,
        transition_id="close-candidate-workflow:close:1",
        reason="parent_fallthrough",
    )

    assert closed.status is CandidateCloseStatus.CLOSED
    assert closed.terminal_state is LifecycleState.FAILED_NO_CANDIDATE
    assert recovered.status is CandidateCloseStatus.ALREADY_TERMINAL
    assert lifecycle._lifecycle is not None
    assert lifecycle._lifecycle.state is LifecycleState.FAILED_NO_CANDIDATE
    assert lifecycle.transitions == [
        (
            "close-candidate-workflow:close:1",
            LifecycleState.FAILED_NO_CANDIDATE,
            {
                "canonical_event_id": str(event.canonical_event_id),
                "workflow_id": workflow_id,
                "event_summary": event.title,
                "candidate_close_reason": "parent_fallthrough",
                "notification_suppressed": True,
            },
        )
    ]
