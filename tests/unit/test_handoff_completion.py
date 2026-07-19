"""Fail-closed handoff mark-done verification (FR-6.3, FR-16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.consent import MockRegistrationConsentEvidence
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.engine import DataPolicyEngine
from events_concierge.adapters.policy.pacer import InMemoryPacer
from events_concierge.application.registration import (
    HandoffCompletionStatus,
    PacerDeferredError,
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
    RsvpState,
    Source,
)
from events_concierge.domain.events import CanonicalEvent, EventSourceLink
from events_concierge.domain.lifecycle import HandoffCompletionReceipt, HandoffTask, Lifecycle
from events_concierge.domain.policy import SourcePolicy
from events_concierge.ports.calendar import (
    CalendarBindingUnavailableError,
    CalendarReconsentRequiredError,
)
from events_concierge.ports.policy import Pacer, PacerLease, PacerLeaseStatus, PacerRequest
from events_concierge.ports.repositories import HandoffRepository, LifecycleRepository
from events_concierge.ports.sources import RegistrationTarget, SourceReconsentRequiredError


class _FailOnceRegistrationReadSource(ConfirmingSource):
    """Transient provider read outage that succeeds on the activity retry."""

    def __init__(self) -> None:
        super().__init__(Source.MEETUP)
        self._fail_next_read = True

    async def read_registration_state(
        self,
        tenant_id: UUID,
        target: RegistrationTarget,
        modality: Modality,
    ) -> RsvpState:
        if self._fail_next_read:
            self._fail_next_read = False
            raise RuntimeError("simulated provider read outage")
        return await super().read_registration_state(tenant_id, target, modality)


class _FailOnceFreeBusyCalendar(MockCalendar):
    """Transient Calendar read outage that succeeds without consuming completion evidence."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_next_read = True

    async def free_busy(
        self,
        tenant_id: UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> list[BusyBlock]:
        if self._fail_next_read:
            self._fail_next_read = False
            raise RuntimeError("simulated freeBusy outage")
        return await super().free_busy(tenant_id, window_start, window_end)


class _SaturateOncePacer(InMemoryPacer):
    """One transient admission failure followed by the normal granted local lease."""

    def __init__(self) -> None:
        super().__init__()
        self._saturate_next = True

    async def acquire(self, request: PacerRequest) -> PacerLease:
        if self._saturate_next:
            self._saturate_next = False
            return PacerLease(
                PacerLeaseStatus.SATURATED,
                retry_after_seconds=0.01,
                detail="simulated transient saturation",
            )
        return await super().acquire(request)


class _PermanentSourceReadFailure(ConfirmingSource):
    """Raise one explicit user-action outcome instead of a retryable provider outage."""

    def __init__(self, error: SourceReconsentRequiredError) -> None:
        super().__init__(Source.MEETUP)
        self._error = error

    async def read_registration_state(
        self,
        tenant_id: UUID,
        target: RegistrationTarget,
        modality: Modality,
    ) -> RsvpState:
        del tenant_id, target, modality
        raise self._error


class _PermanentCalendarReadFailure(MockCalendar):
    """Raise a typed Calendar setup/authorization outcome from freeBusy."""

    def __init__(
        self,
        error: CalendarBindingUnavailableError | CalendarReconsentRequiredError,
    ) -> None:
        super().__init__()
        self._error = error

    async def free_busy(
        self,
        tenant_id: UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> list[BusyBlock]:
        del tenant_id, window_start, window_end
        raise self._error


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
    pacer: Pacer | None = None,
    source_policy: SourcePolicy | None = None,
) -> RegistrationService:
    return RegistrationService(
        {Source.MEETUP: source},
        DataPolicyEngine(
            source_policies={
                Source.MEETUP: source_policy
                or SourcePolicy(
                    source=Source.MEETUP,
                    automation_allowed={Modality.API: True},
                ),
            }
        ),
        pacer or InMemoryPacer(),
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


async def test_transient_provider_read_does_not_consume_mark_done_before_retry() -> None:
    """A provider transport outage retries the same evidence instead of manufacturing a review."""
    tenant_id, event, lifecycle, task = _fixture()
    source = _FailOnceRegistrationReadSource()
    target = RegistrationTarget(
        event.source_links[0].source_event_id,
        event.source_links[0].registration_url,
    )
    await source.register(tenant_id, target, Modality.API, "outside-concierge")
    calendar = MockCalendar()
    handoff = _HandoffRepository(task)
    consent = MockRegistrationConsentEvidence()
    consent.seed(uuid4(), tenant_id, Source.MEETUP, Modality.API)
    service = _service(source, calendar, lifecycle, handoff, consent)

    with pytest.raises(RuntimeError, match="provider read outage"):
        await service.complete_handoff(
            tenant_id,
            event,
            lifecycle.workflow_id,
            task_id=task.task_id,
            completion_id="completion-provider-retry",
            registered_transition_id="registered-provider-retry",
            verification_read_queue_item_id="readback-provider-retry",
        )

    assert task.state is HandoffState.OPEN
    assert lifecycle.state is LifecycleState.HANDOFF
    assert handoff.reviews == []
    assert handoff.receipts == {}

    result = await service.complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-provider-retry",
        registered_transition_id="registered-provider-retry",
        verification_read_queue_item_id="readback-provider-retry",
    )

    assert result.status is HandoffCompletionStatus.CONFIRMED
    assert handoff.reviews == []
    assert [item[0] for item in handoff.verified] == ["completion-provider-retry"]


async def test_transient_pacer_saturation_does_not_consume_mark_done_before_retry() -> None:
    """Admission-plane saturation retains the same completion evidence for a durable timer retry."""
    tenant_id, event, lifecycle, task = _fixture()
    source = ConfirmingSource(Source.MEETUP)
    target = RegistrationTarget(
        event.source_links[0].source_event_id,
        event.source_links[0].registration_url,
    )
    await source.register(tenant_id, target, Modality.API, "outside-concierge")
    handoff = _HandoffRepository(task)
    consent = MockRegistrationConsentEvidence()
    consent.seed(uuid4(), tenant_id, Source.MEETUP, Modality.API)
    service = _service(
        source,
        MockCalendar(),
        lifecycle,
        handoff,
        consent,
        _SaturateOncePacer(),
    )

    with pytest.raises(PacerDeferredError) as deferred:
        await service.complete_handoff(
            tenant_id,
            event,
            lifecycle.workflow_id,
            task_id=task.task_id,
            completion_id="completion-pacer-retry",
            registered_transition_id="registered-pacer-retry",
            verification_read_queue_item_id="readback-pacer-retry",
        )

    assert deferred.value.lease.status is PacerLeaseStatus.SATURATED
    assert task.state is HandoffState.OPEN
    assert lifecycle.state is LifecycleState.HANDOFF
    assert handoff.reviews == []
    assert handoff.receipts == {}

    result = await service.complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-pacer-retry",
        registered_transition_id="registered-pacer-retry",
        verification_read_queue_item_id="readback-pacer-retry",
    )

    assert result.status is HandoffCompletionStatus.CONFIRMED
    assert handoff.reviews == []
    assert [item[0] for item in handoff.verified] == ["completion-pacer-retry"]


async def test_transient_free_busy_read_does_not_consume_mark_done_before_retry() -> None:
    """A Calendar transport outage leaves the confirmed provider evidence retryable."""
    tenant_id, event, lifecycle, task = _fixture()
    source = ConfirmingSource(Source.MEETUP)
    target = RegistrationTarget(
        event.source_links[0].source_event_id,
        event.source_links[0].registration_url,
    )
    await source.register(tenant_id, target, Modality.API, "outside-concierge")
    calendar = _FailOnceFreeBusyCalendar()
    handoff = _HandoffRepository(task)
    consent = MockRegistrationConsentEvidence()
    consent.seed(uuid4(), tenant_id, Source.MEETUP, Modality.API)
    service = _service(source, calendar, lifecycle, handoff, consent)

    with pytest.raises(RuntimeError, match="freeBusy outage"):
        await service.complete_handoff(
            tenant_id,
            event,
            lifecycle.workflow_id,
            task_id=task.task_id,
            completion_id="completion-calendar-retry",
            registered_transition_id="registered-calendar-retry",
            verification_read_queue_item_id="readback-calendar-retry",
        )

    assert task.state is HandoffState.OPEN
    assert lifecycle.state is LifecycleState.HANDOFF
    assert handoff.reviews == []
    assert handoff.receipts == {}

    result = await service.complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-calendar-retry",
        registered_transition_id="registered-calendar-retry",
        verification_read_queue_item_id="readback-calendar-retry",
    )

    assert result.status is HandoffCompletionStatus.CONFIRMED
    assert handoff.reviews == []
    assert [item[0] for item in handoff.verified] == ["completion-calendar-retry"]


async def test_missing_consent_consumes_mark_done_into_durable_review() -> None:
    """Revoked verification consent needs owner action and must not create an endless retry."""
    tenant_id, event, lifecycle, task = _fixture()
    handoff = _HandoffRepository(task)

    result = await _service(
        ConfirmingSource(Source.MEETUP),
        MockCalendar(),
        lifecycle,
        handoff,
        MockRegistrationConsentEvidence(),
    ).complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-consent-review",
        registered_transition_id="registered-consent-review",
        verification_read_queue_item_id="readback-consent-review",
    )

    assert result.status is HandoffCompletionStatus.REVIEW_REQUIRED
    assert result.detail == "registration consent is no longer available"
    assert handoff.reviews == [("completion-consent-review", result.detail)]
    assert lifecycle.state is LifecycleState.HANDOFF
    assert task.state is HandoffState.OPEN


@pytest.mark.parametrize(
    ("source_policy", "expected_detail"),
    [
        (
            SourcePolicy(
                source=Source.MEETUP,
                automation_allowed={Modality.API: False},
            ),
            "source automation policy no longer permits verification",
        ),
        (
            SourcePolicy(
                source=Source.MEETUP,
                automation_allowed={Modality.API: True},
                quarantined=True,
            ),
            "registration source is quarantined",
        ),
    ],
)
async def test_permanent_source_guard_consumes_mark_done_into_review(
    source_policy: SourcePolicy,
    expected_detail: str,
) -> None:
    """Policy and quarantine decisions are durable review outcomes, not dependency retries."""
    tenant_id, event, lifecycle, task = _fixture()
    consent = MockRegistrationConsentEvidence()
    consent.seed(uuid4(), tenant_id, Source.MEETUP, Modality.API)
    handoff = _HandoffRepository(task)

    result = await _service(
        ConfirmingSource(Source.MEETUP),
        MockCalendar(),
        lifecycle,
        handoff,
        consent,
        source_policy=source_policy,
    ).complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-source-guard-review",
        registered_transition_id="registered-source-guard-review",
        verification_read_queue_item_id="readback-source-guard-review",
    )

    assert result.status is HandoffCompletionStatus.REVIEW_REQUIRED
    assert result.detail == expected_detail
    assert handoff.reviews == [("completion-source-guard-review", expected_detail)]
    assert lifecycle.state is LifecycleState.HANDOFF
    assert task.state is HandoffState.OPEN


async def test_source_reconsent_consumes_mark_done_into_durable_review() -> None:
    """A rejected source credential cannot recover until user action, so the command is reviewed."""
    tenant_id, event, lifecycle, task = _fixture()
    consent = MockRegistrationConsentEvidence()
    consent.seed(uuid4(), tenant_id, Source.MEETUP, Modality.API)
    handoff = _HandoffRepository(task)

    result = await _service(
        _PermanentSourceReadFailure(SourceReconsentRequiredError("credential rejected")),
        MockCalendar(),
        lifecycle,
        handoff,
        consent,
    ).complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-source-reconsent-review",
        registered_transition_id="registered-source-reconsent-review",
        verification_read_queue_item_id="readback-source-reconsent-review",
    )

    assert result.status is HandoffCompletionStatus.REVIEW_REQUIRED
    assert result.detail == "registration source authorization requires re-consent"
    assert handoff.reviews == [("completion-source-reconsent-review", result.detail)]
    assert lifecycle.state is LifecycleState.HANDOFF
    assert task.state is HandoffState.OPEN


@pytest.mark.parametrize(
    ("calendar_error", "expected_detail"),
    [
        (
            CalendarBindingUnavailableError("binding missing"),
            "calendar binding is not available",
        ),
        (
            CalendarReconsentRequiredError("scope rejected"),
            "calendar authorization requires re-consent",
        ),
    ],
)
async def test_permanent_calendar_access_outcome_consumes_mark_done_into_review(
    calendar_error: CalendarBindingUnavailableError | CalendarReconsentRequiredError,
    expected_detail: str,
) -> None:
    """Calendar setup/auth failures require owner action while transport failures remain retryable."""
    tenant_id, event, lifecycle, task = _fixture()
    source = ConfirmingSource(Source.MEETUP)
    await source.register(
        tenant_id,
        RegistrationTarget(
            event.source_links[0].source_event_id,
            event.source_links[0].registration_url,
        ),
        Modality.API,
        "outside-concierge-calendar-access",
    )
    consent = MockRegistrationConsentEvidence()
    consent.seed(uuid4(), tenant_id, Source.MEETUP, Modality.API)
    handoff = _HandoffRepository(task)

    result = await _service(
        source,
        _PermanentCalendarReadFailure(calendar_error),
        lifecycle,
        handoff,
        consent,
    ).complete_handoff(
        tenant_id,
        event,
        lifecycle.workflow_id,
        task_id=task.task_id,
        completion_id="completion-calendar-access-review",
        registered_transition_id="registered-calendar-access-review",
        verification_read_queue_item_id="readback-calendar-access-review",
    )

    assert result.status is HandoffCompletionStatus.REVIEW_REQUIRED
    assert result.detail == expected_detail
    assert handoff.reviews == [("completion-calendar-access-review", expected_detail)]
    assert lifecycle.state is LifecycleState.HANDOFF
    assert task.state is HandoffState.OPEN


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
