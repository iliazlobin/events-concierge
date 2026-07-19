"""Fail-closed handoff mark-done verification (FR-6.3, FR-16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.consent import MockRegistrationConsentEvidence
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.engine import DataPolicyEngine
from events_concierge.adapters.policy.pacer import InMemoryPacer
from events_concierge.application.registration import (
    HandoffCompletionStatus,
    RegistrationService,
)
from events_concierge.domain.conflict import BusyBlock
from events_concierge.domain.enums import (
    ConsentScope,
    HandoffCompletionOutcome,
    HandoffReason,
    HandoffState,
    Lane,
    LifecycleState,
    Modality,
    PriceStatus,
    Source,
)
from events_concierge.domain.events import CanonicalEvent, EventSourceLink
from events_concierge.domain.lifecycle import HandoffCompletionReceipt, HandoffTask, Lifecycle
from events_concierge.domain.policy import SourcePolicy
from events_concierge.ports.repositories import HandoffRepository, LifecycleRepository
from events_concierge.ports.sources import RegistrationTarget


class _LifecycleRepository:
    def __init__(self, lifecycle: Lifecycle) -> None:
        self.lifecycle = lifecycle

    async def get_or_create(
        self,
        tenant_id: UUID,
        canonical_event_id: UUID,
        workflow_id: str,
    ) -> Lifecycle:
        assert (tenant_id, canonical_event_id, workflow_id) == (
            self.lifecycle.tenant_id,
            self.lifecycle.canonical_event_id,
            self.lifecycle.workflow_id,
        )
        return self.lifecycle

    async def find_active(
        self,
        tenant_id: UUID,
        canonical_event_id: UUID,
    ) -> Lifecycle | None:
        if (tenant_id, canonical_event_id) != (
            self.lifecycle.tenant_id,
            self.lifecycle.canonical_event_id,
        ):
            return None
        return self.lifecycle


class _HandoffRepository:
    def __init__(self, task: HandoffTask, *, expire_before_verified: bool = False) -> None:
        self.task = task
        self.expire_before_verified = expire_before_verified
        self.verified: list[tuple[str, Source, bool]] = []
        self.reviews: list[tuple[str, str]] = []
        self.receipts: dict[str, HandoffCompletionReceipt] = {}

    async def get(self, tenant_id: UUID, task_id: str) -> HandoffTask | None:
        if (tenant_id, task_id) != (self.task.tenant_id, self.task.task_id):
            return None
        return self.task

    async def get_completion_attempt(
        self,
        tenant_id: UUID,
        task_id: str,
        completion_id: str,
    ) -> HandoffCompletionReceipt | None:
        if (tenant_id, task_id) != (self.task.tenant_id, self.task.task_id):
            return None
        return self.receipts.get(completion_id)

    async def complete_verified(
        self,
        task: HandoffTask,
        lifecycle: Lifecycle,
        *,
        transition_id: str,
        completion_id: str,
        registration_source: Source,
        conflict_warning: bool,
        outbox_payload: dict[str, object],
    ) -> bool:
        del transition_id, outbox_payload
        assert task is self.task
        if self.expire_before_verified:
            task.state = HandoffState.EXPIRED
            lifecycle.transition(LifecycleState.EXPIRED)
            raise RuntimeError("simulated expiry race")
        lifecycle.lane = Lane.HANDOFF
        lifecycle.conflict_warning = conflict_warning
        lifecycle.registration_source = registration_source
        lifecycle.transition(LifecycleState.REGISTERED)
        task.state = HandoffState.COMPLETED
        self.verified.append((completion_id, registration_source, conflict_warning))
        self.receipts[completion_id] = HandoffCompletionReceipt(
            completion_id=completion_id,
            outcome=HandoffCompletionOutcome.VERIFIED,
            detail="",
            registration_source=registration_source,
            conflict_warning=conflict_warning,
        )
        return True

    async def record_completion_review(
        self,
        task: HandoffTask,
        *,
        completion_id: str,
        detail: str,
    ) -> bool:
        assert task is self.task
        self.reviews.append((completion_id, detail))
        self.receipts[completion_id] = HandoffCompletionReceipt(
            completion_id=completion_id,
            outcome=HandoffCompletionOutcome.REVIEW_REQUIRED,
            detail=detail,
            registration_source=None,
            conflict_warning=None,
        )
        return True


def _fixture() -> tuple[UUID, CanonicalEvent, Lifecycle, HandoffTask]:
    tenant_id = uuid4()
    event = CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Out-of-band meetup",
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 20, 0, tzinfo=UTC),
        price_status=PriceStatus.FREE,
        source_links=[
            EventSourceLink(
                source=Source.MEETUP,
                source_event_id="handoff-readback",
                registration_url="https://meetup.example/handoff-readback",
                price_status=PriceStatus.FREE,
            )
        ],
    )
    workflow_id = f"{tenant_id}:{event.canonical_event_id}"
    lifecycle = Lifecycle(
        uuid4(),
        tenant_id,
        event.canonical_event_id,
        workflow_id,
        state=LifecycleState.HANDOFF,
        lane=Lane.HANDOFF,
    )
    task = HandoffTask(
        task_id=f"{workflow_id}:handoff",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=event.canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link=event.source_links[0].registration_url,
        event_summary=event.title,
        ttl_expires_at=event.start_at,
    )
    return tenant_id, event, lifecycle, task


def _service(
    source: ConfirmingSource,
    calendar: MockCalendar,
    lifecycle: Lifecycle,
    handoff: _HandoffRepository,
    consent: MockRegistrationConsentEvidence,
) -> RegistrationService:
    return RegistrationService(
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
        cast(LifecycleRepository, _LifecycleRepository(lifecycle)),
        cast(HandoffRepository, handoff),
        registration_consent=consent,
    )


async def test_mark_done_requires_source_readback_then_warns_on_fresh_conflict() -> None:
    """A self-report advances only after remote confirmation; conflict warns but does not hide it."""
    tenant_id, event, lifecycle, task = _fixture()
    source_events: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=source_events)
    target = RegistrationTarget(
        event.source_links[0].source_event_id,
        event.source_links[0].registration_url,
    )
    await source.register(tenant_id, target, Modality.API, "outside-concierge")
    calendar = MockCalendar()
    calendar.seed_busy(
        tenant_id,
        [
            BusyBlock(
                event.start_at + timedelta(minutes=30),
                event.start_at + timedelta(hours=1),
            )
        ],
    )
    handoff = _HandoffRepository(task)
    consent = MockRegistrationConsentEvidence()
    consent.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )

    result = await _service(source, calendar, lifecycle, handoff, consent).complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-1",
        registered_transition_id="registered-1",
        verification_read_queue_item_id="readback-1",
    )
    source_events_after_commit = list(source_events)
    replay = await _service(source, calendar, lifecycle, handoff, consent).complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-1",
        registered_transition_id="registered-1",
        verification_read_queue_item_id="readback-1",
    )

    assert result.status is HandoffCompletionStatus.CONFIRMED
    assert replay == result
    assert source_events == source_events_after_commit
    assert result.conflict_warning is True
    assert lifecycle.state is LifecycleState.REGISTERED
    assert lifecycle.conflict_warning is True
    assert task.state is HandoffState.COMPLETED
    assert handoff.verified == [("completion-1", Source.MEETUP, True)]
    assert handoff.reviews == []
    assert calendar.upsert_attempts == 0


async def test_uncorroborated_mark_done_is_consumed_for_review_without_calendar_write() -> None:
    """NOT_PRESENT is a discrepancy, never sufficient evidence for lifecycle/calendar advance."""
    tenant_id, event, lifecycle, task = _fixture()
    source_events: list[str] = []
    source = ConfirmingSource(Source.MEETUP, event_log=source_events)
    calendar = MockCalendar()
    handoff = _HandoffRepository(task)
    consent = MockRegistrationConsentEvidence()
    consent.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )

    result = await _service(source, calendar, lifecycle, handoff, consent).complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-2",
        registered_transition_id="registered-2",
        verification_read_queue_item_id="readback-2",
    )
    await source.register(
        tenant_id,
        RegistrationTarget(
            event.source_links[0].source_event_id,
            event.source_links[0].registration_url,
        ),
        Modality.API,
        "provider-changed-after-review",
    )
    source_events_after_change = list(source_events)
    replay = await _service(source, calendar, lifecycle, handoff, consent).complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-2",
        registered_transition_id="registered-2",
        verification_read_queue_item_id="readback-2",
    )

    assert result.status is HandoffCompletionStatus.REVIEW_REQUIRED
    assert replay == result
    assert source_events == source_events_after_change
    assert "not_present" in result.detail
    assert lifecycle.state is LifecycleState.HANDOFF
    assert task.state is HandoffState.OPEN
    assert handoff.verified == []
    assert handoff.reviews == [("completion-2", result.detail)]
    assert calendar.upsert_attempts == 0


async def test_expiry_winning_during_verification_returns_inactive_without_retry_loop() -> None:
    """A terminal task race becomes a typed no-op so the workflow can converge on normal expiry."""
    tenant_id, event, lifecycle, task = _fixture()
    source = ConfirmingSource(Source.MEETUP)
    await source.register(
        tenant_id,
        RegistrationTarget(
            event.source_links[0].source_event_id,
            event.source_links[0].registration_url,
        ),
        Modality.API,
        "outside-concierge-expiry-race",
    )
    calendar = MockCalendar()
    handoff = _HandoffRepository(task, expire_before_verified=True)
    consent = MockRegistrationConsentEvidence()
    consent.seed(
        uuid4(),
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )

    result = await _service(source, calendar, lifecycle, handoff, consent).complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-expiry-race",
        registered_transition_id="registered-expiry-race",
        verification_read_queue_item_id="readback-expiry-race",
    )

    assert result.status is HandoffCompletionStatus.INACTIVE
    assert lifecycle.state is LifecycleState.EXPIRED
    assert task.state is HandoffState.EXPIRED
    assert calendar.upsert_attempts == 0
