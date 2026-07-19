"""Unit coverage for P6a's immutable registration action-audit seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.mock.audit import MockRegistrationActionAudit
from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.consent import MockRegistrationConsentEvidence
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.engine import DataPolicyEngine
from events_concierge.adapters.policy.pacer import InMemoryPacer
from events_concierge.application.registration import RegistrationService
from events_concierge.domain.audit import (
    RegistrationActionAudit,
    RegistrationActionAuditDecision,
    RegistrationActionAuditOutcome,
    RegistrationActionAuditPhase,
)
from events_concierge.domain.enums import ConsentScope, Lane, Modality, PriceStatus, Source
from events_concierge.domain.events import CanonicalEvent, EventSourceLink
from events_concierge.domain.policy import SourcePolicy
from events_concierge.ports.repositories import HandoffRepository, LifecycleRepository


class _FailOnPreMutateAudit(MockRegistrationActionAudit):
    """Persist the early fact, then make the authoritative pre-mutate evidence unavailable."""

    async def append(self, record: RegistrationActionAudit) -> bool:
        if record.phase is RegistrationActionAuditPhase.POLICY_PRE_MUTATE:
            raise RuntimeError("simulated audit persistence outage")
        return await super().append(record)


def _event() -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Audit fixture meetup",
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        price_status=PriceStatus.FREE,
        source_links=[
            EventSourceLink(
                source=Source.MEETUP,
                source_event_id="audit-fixture",
                registration_url="https://meetup.example/audit-fixture",
                price_status=PriceStatus.FREE,
            )
        ],
    )


def _workflow_id(tenant_id: UUID, event: CanonicalEvent) -> str:
    return f"{tenant_id}:{event.canonical_event_id}"


def _service(
    source: ConfirmingSource,
    audit: MockRegistrationActionAudit,
    *,
    automation_allowed: bool = True,
) -> tuple[RegistrationService, MockRegistrationConsentEvidence]:
    consent = MockRegistrationConsentEvidence()
    return (
        RegistrationService(
            {Source.MEETUP: source},
            DataPolicyEngine(
                source_policies={
                    Source.MEETUP: SourcePolicy(
                        source=Source.MEETUP,
                        automation_allowed={Modality.API: automation_allowed},
                    )
                }
            ),
            InMemoryPacer(),
            MockCalendar(),
            cast(LifecycleRepository, object()),
            cast(HandoffRepository, object()),
            action_audit=audit,
            registration_consent=consent,
        ),
        consent,
    )


def _grant_registration_consent(consent: MockRegistrationConsentEvidence, tenant_id: UUID) -> UUID:
    """Seed the explicit owner-side fixture evidence required for a test RSVP path."""
    consent_ref = uuid4()
    consent.seed(
        consent_ref,
        tenant_id,
        Source.MEETUP,
        Modality.API,
        ConsentScope.REGISTRATION,
    )
    return consent_ref


def _policy_record(
    *,
    audit_key: str = "fixture-source-key:policy_precheck",
    decision: RegistrationActionAuditDecision = RegistrationActionAuditDecision.ALLOWED,
) -> RegistrationActionAudit:
    tenant_id = uuid4()
    event_id = uuid4()
    return RegistrationActionAudit(
        audit_key=audit_key,
        tenant_id=tenant_id,
        workflow_id=f"{tenant_id}:{event_id}",
        source=Source.MEETUP,
        modality=Modality.API,
        phase=RegistrationActionAuditPhase.POLICY_PRECHECK,
        policy_decision=decision,
    )


def test_registration_action_audit_enforces_closed_phase_and_identity_contract() -> None:
    """Only the three P6a phase combinations and deterministic workflow identity are representable."""
    record = _policy_record()

    with pytest.raises(ValueError, match="must not carry"):
        replace(record, outcome=RegistrationActionAuditOutcome.CONFIRMED)
    with pytest.raises(ValueError, match="requires an allowed"):
        RegistrationActionAudit(
            audit_key="fixture-source-key:outcome",
            tenant_id=record.tenant_id,
            workflow_id=record.workflow_id,
            source=Source.MEETUP,
            modality=Modality.API,
            phase=RegistrationActionAuditPhase.SOURCE_RSVP_OUTCOME,
            policy_decision=RegistrationActionAuditDecision.DENIED,
            outcome=RegistrationActionAuditOutcome.CONFIRMED,
        )
    with pytest.raises(ValueError, match="deterministic"):
        replace(record, workflow_id="not-a-tenant-event-workflow")


async def test_mock_registration_action_audit_replays_only_the_exact_immutable_fact() -> None:
    """A same-key replay converges; a changed valid fact cannot overwrite evidence."""
    audit = MockRegistrationActionAudit()
    record = _policy_record()

    assert await audit.append(record)
    assert not await audit.append(record)
    with pytest.raises(ValueError, match="already bound"):
        await audit.append(replace(record, policy_decision=RegistrationActionAuditDecision.DENIED))
    assert audit.records == [record]


async def test_registration_service_records_one_precheck_pre_mutate_and_normalized_outcome() -> (
    None
):
    """The allowed path writes three PII-free facts around exactly one autonomous RSVP."""
    tenant_id = uuid4()
    event = _event()
    workflow_id = _workflow_id(tenant_id, event)
    source_key = "fixture-source-key"
    source = ConfirmingSource(Source.MEETUP)
    audit = MockRegistrationActionAudit()
    service, consent = _service(source, audit)
    consent_ref = _grant_registration_consent(consent, tenant_id)

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
    assert result.outcome.value == "confirmed"
    assert source.registration_effects == 1
    assert [(record.phase, record.policy_decision, record.outcome) for record in audit.records] == [
        (
            RegistrationActionAuditPhase.POLICY_PRECHECK,
            RegistrationActionAuditDecision.ALLOWED,
            None,
        ),
        (
            RegistrationActionAuditPhase.POLICY_PRE_MUTATE,
            RegistrationActionAuditDecision.ALLOWED,
            None,
        ),
        (
            RegistrationActionAuditPhase.SOURCE_RSVP_OUTCOME,
            RegistrationActionAuditDecision.ALLOWED,
            RegistrationActionAuditOutcome.CONFIRMED,
        ),
    ]
    assert [record.consent_ref for record in audit.records] == [
        consent_ref,
        consent_ref,
        consent_ref,
    ]
    assert all(record.workflow_id == workflow_id for record in audit.records)


async def test_early_policy_denial_records_only_the_denied_precheck() -> None:
    """A denied workflow gate creates no source mutation or later audit fact."""
    tenant_id = uuid4()
    event = _event()
    source = ConfirmingSource(Source.MEETUP)
    audit = MockRegistrationActionAudit()
    service, consent = _service(source, audit, automation_allowed=False)
    consent_ref = _grant_registration_consent(consent, tenant_id)

    gate = await service.policy_gate(
        tenant_id,
        event,
        Lane.AUTONOMOUS_SLA,
        workflow_id=_workflow_id(tenant_id, event),
        source_idempotency_key="denied-source-key",
    )

    assert not gate.allowed
    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert [(record.phase, record.policy_decision, record.outcome) for record in audit.records] == [
        (
            RegistrationActionAuditPhase.POLICY_PRECHECK,
            RegistrationActionAuditDecision.DENIED,
            None,
        )
    ]
    assert [record.consent_ref for record in audit.records] == [consent_ref]


async def test_pre_mutate_audit_failure_stops_before_the_source_mutation() -> None:
    """If authoritative pre-mutate evidence cannot persist, the RSVP wire effect is fail-closed."""
    tenant_id = uuid4()
    event = _event()
    source = ConfirmingSource(Source.MEETUP)
    audit = _FailOnPreMutateAudit()
    service, consent = _service(source, audit)
    consent_ref = _grant_registration_consent(consent, tenant_id)
    workflow_id = _workflow_id(tenant_id, event)

    await service.policy_gate(
        tenant_id,
        event,
        Lane.AUTONOMOUS_SLA,
        workflow_id=workflow_id,
        source_idempotency_key="audit-failure-source-key",
    )
    with pytest.raises(RuntimeError, match="audit persistence outage"):
        await service.register_or_rsvp(
            tenant_id,
            event,
            Lane.AUTONOMOUS_SLA,
            "audit-failure-source-key",
            workflow_id=workflow_id,
        )

    assert source.register_attempts == 0
    assert source.registration_effects == 0
    assert [record.phase for record in audit.records] == [
        RegistrationActionAuditPhase.POLICY_PRECHECK
    ]
    assert [record.consent_ref for record in audit.records] == [consent_ref]
