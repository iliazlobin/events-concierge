"""Registration-saga application steps used by the Temporal child workflow.

The workflow owns idempotency and transition keys; this service performs one bounded step at a time.
That keeps source retries read-before-mutate, calendar writes idempotent, and lifecycle transitions
anchored in the guarded repository transaction (FR-5.3/5.5/5.9, FR-8.3, NFR-8, ADR-003/004/005/007).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import NoReturn
from unicodedata import category
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain import ids
from ..domain.audit import (
    RegistrationActionAudit,
    RegistrationActionAuditDecision,
    RegistrationActionAuditOutcome,
    RegistrationActionAuditPhase,
)
from ..domain.conflict import evaluate_conflict
from ..domain.enums import (
    ConflictVerdict,
    ConsentScope,
    GroupCondition,
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
from ..domain.events import CanonicalEvent
from ..domain.lifecycle import HandoffCompletionReceipt, HandoffTask, IllegalTransitionError
from ..domain.policy import PolicyDecision
from ..infra.logging import get_logger
from ..infra.ulid import new_ulid
from ..ports.audit import RegistrationActionAuditPort
from ..ports.browser_admission import (
    BrowserAdmissionLease,
    BrowserAdmissionPort,
    BrowserAdmissionRequest,
)
from ..ports.calendar import (
    CalendarBindingUnavailableError,
    CalendarEntry,
    CalendarPort,
    CalendarReconsentRequiredError,
)
from ..ports.consent import RegistrationConsentEvidencePort
from ..ports.credentials import CredentialVault
from ..ports.notification_secrets import NotificationSecretProtector
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
    RegisterOutcome,
    RegisterResult,
    RegistrationTarget,
    SourceAccessDeniedError,
    SourcePort,
    SourceRateLimitedError,
    SourceReconsentRequiredError,
)

_log = get_logger(__name__)

_DEFAULT_DURATION = timedelta(hours=2)
_MAX_PUBLIC_BASE_URL_LENGTH = 2048
_MAX_URL_PORT = 65535
_LANE_SOURCE = {Lane.AUTONOMOUS_SLA: Source.MEETUP, Lane.BROWSER_BEST_EFFORT: Source.LUMA}
_LANE_MODALITY = {Lane.AUTONOMOUS_SLA: Modality.API, Lane.BROWSER_BEST_EFFORT: Modality.BROWSER}
_SOURCE_OUTCOME_TO_AUDIT = {
    RegisterOutcome.CONFIRMED: RegistrationActionAuditOutcome.CONFIRMED,
    RegisterOutcome.NO_OP_ALREADY_CONFIRMED: RegistrationActionAuditOutcome.CONFIRMED,
    RegisterOutcome.PENDING_CONFIRMATION: RegistrationActionAuditOutcome.PENDING_CONFIRMATION,
    RegisterOutcome.NEEDS_HANDOFF: RegistrationActionAuditOutcome.NEEDS_HANDOFF,
    RegisterOutcome.SOURCE_QUARANTINED: RegistrationActionAuditOutcome.NEEDS_HANDOFF,
    RegisterOutcome.PAYWALL: RegistrationActionAuditOutcome.PAYWALL,
    RegisterOutcome.NEEDS_REAUTH: RegistrationActionAuditOutcome.NEEDS_REAUTH,
    RegisterOutcome.FAILED: RegistrationActionAuditOutcome.FAILED,
}


class RegistrationStatus(StrEnum):
    REGISTERED = "registered"
    HANDOFF = "handoff"
    FAILED = "failed"


class ConfirmationStatus(StrEnum):
    CONFIRMED = "confirmed"
    PENDING = "pending"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class CandidateCloseStatus(StrEnum):
    """Terminal result of a parent fall-through or expired directive (ADR-003/007)."""

    CLOSED = "closed"
    ALREADY_TERMINAL = "already_terminal"
    IGNORED = "ignored"


class HandoffCompletionStatus(StrEnum):
    """Fail-closed outcomes from one independently verified mark-done command."""

    CONFIRMED = "confirmed"
    REVIEW_REQUIRED = "review_required"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class CandidateCloseResult:
    """A JSON-native projection of one child lifecycle close command."""

    status: CandidateCloseStatus
    terminal_state: LifecycleState | None = None
    detail: str = ""


class PacerDeferredError(Exception):
    """Internal activity-control signal for a non-granted ADR-005 Pacer lease.

    This never crosses the Temporal activity boundary as an error.  Activities translate it to a
    JSON-native pacing result so the workflow can set a durable timer and retry the fresh source
    read/mutation sequence without spending an ACK-recovery attempt (ADR-003/005).
    """

    def __init__(self, lease: PacerLease) -> None:
        if lease.granted:
            raise ValueError("a granted Pacer lease cannot defer an activity")
        self.lease = lease
        super().__init__(lease.detail or lease.status.value)


class SourcePolicyDeniedError(Exception):
    """Internal no-wire-call result of a fresh source policy guard (ADR-004).

    This is deliberately distinct from an adapter ban/403 signal.  It carries only the
    deterministic policy decision and never crosses a Temporal activity as an exception, so a
    denied guard cannot trigger an automatic source retry.
    """

    def __init__(self, decision: PolicyDecision) -> None:
        if decision.allowed:
            raise ValueError("an allowed policy decision cannot block a source call")
        self.decision = decision
        super().__init__(decision.reason)


class SourceQuarantinedError(Exception):
    """Internal terminal control signal after a durable or freshly observed quarantine.

    Both a raw adapter ban/403 and the next fresh policy read arrive here.  The workflow converts
    it to an immediate handoff rather than attempting another autonomous lane or treating it as
    an activity failure (FR-7.6, FR-10.3, AC-72).
    """

    def __init__(self, source: Source, *, from_policy: bool = False) -> None:
        self.source = source
        self.from_policy = from_policy
        super().__init__(f"source '{source.value}' is quarantined")


class RegistrationConsentDeniedError(Exception):
    """Internal fail-closed result when registration evidence is absent or no longer exact (FR-2.9).

    This is deliberately not a provider or workflow error.  Callers turn it into a handoff result
    before a source call can be issued.
    """

    def __init__(self, detail: str = "registration consent evidence is unavailable") -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    status: RegistrationStatus
    lane: Lane | None = None
    calendar_event_id: str | None = None
    handoff_task_id: str | None = None
    detail: str = ""
    handoff_expires_at: datetime | None = None
    handoff_expiry_transition_id: str | None = None
    handoff_created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MembershipResolution:
    """The child saga's durable starting point after local membership/lane resolution."""

    status: str
    lane_plan: tuple[Lane, ...] = ()
    lane: Lane | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PolicyGateResult:
    allowed: bool
    detail: str = ""
    source_quarantined: bool = False


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    status: ConfirmationStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HandoffCompletionResult:
    status: HandoffCompletionStatus
    detail: str = ""
    conflict_warning: bool = False


class RegistrationService:
    """Application-layer implementations for the ADR-003 registration saga activities.

    The direct ``register_event`` method remains as a small sequential facade for the local slice;
    Temporal uses the individual step methods. In both cases, only the workflow/facade supplies
    idempotency keys, never an activity body.
    """

    def __init__(
        self,
        sources_by_source: dict[Source, SourcePort],
        policy: PolicyEngine,
        pacer: Pacer,
        calendar: CalendarPort,
        lifecycle_repo: LifecycleRepository,
        handoff_repo: HandoffRepository,
        *,
        action_audit: RegistrationActionAuditPort | None = None,
        registration_consent: RegistrationConsentEvidencePort | None = None,
        credential_vault: CredentialVault | None = None,
        browser_admission: BrowserAdmissionPort | None = None,
        source_quarantine: SourceQuarantinePort | None = None,
        handoff_ttl_days: int = 7,
        handoff_completion_base_url: str = "",
        require_https_completion_links: bool = False,
        notification_secret_protector: NotificationSecretProtector | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._sources = sources_by_source
        self._policy = policy
        self._pacer = pacer
        self._calendar = calendar
        self._lifecycle = lifecycle_repo
        self._handoff = handoff_repo
        self._action_audit = action_audit
        self._registration_consent = registration_consent
        self._vault = credential_vault
        self._browser_admission = browser_admission
        self._source_quarantine = source_quarantine
        self._ttl = timedelta(days=handoff_ttl_days)
        self._handoff_completion_base_url = _normalized_public_base_url(
            handoff_completion_base_url,
            require_https=require_https_completion_links,
        )
        self._notification_secret_protector = notification_secret_protector
        self._now = now or (lambda: datetime.now(UTC))

    async def resolve_membership(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        lane_plan: tuple[Lane, ...],
        *,
        membership_queue_item_id: str | None = None,
    ) -> MembershipResolution:
        """Open/recover lifecycle state and freshly verify Meetup membership before RSVP.

        The feed's lane plan is only a ranking-time hint.  A real Meetup adapter rechecks the
        member-group precondition under a Pacer lease immediately before the child chooses an
        autonomous lane; unknown, non-member, approval, and dues states fail closed to the next
        lane/handoff (FR-5.2, ADR-003/005).
        """
        lifecycle = await self._lifecycle.get_or_create(
            tenant_id, event.canonical_event_id, workflow_id
        )
        if lifecycle.state is LifecycleState.SCHEDULED:
            return MembershipResolution(
                "scheduled", lane=lifecycle.lane, detail="already scheduled"
            )
        if lifecycle.state is LifecycleState.REGISTERED:
            return MembershipResolution(
                "registered", lane=lifecycle.lane, detail="calendar write recovery required"
            )
        if lifecycle.state is LifecycleState.HANDOFF:
            return MembershipResolution("handoff", lane=Lane.HANDOFF, detail="already handed off")
        if event.price_status is not PriceStatus.FREE:
            # Paid and unverified-price events remain valuable discovery results, but launch has no
            # purchase authority. Do not even make a membership/source read on an autonomous path.
            return MembershipResolution(
                "ready",
                lane_plan=(Lane.HANDOFF,),
                detail="event price is not verified free; human handoff required",
            )
        try:
            refreshed_lane_plan = await self._fresh_membership_lane_plan(
                tenant_id,
                event,
                lane_plan,
                queue_item_id=membership_queue_item_id or f"{workflow_id}:membership_read:1",
            )
        except SourceQuarantinedError as quarantined:
            return MembershipResolution(
                "quarantined",
                lane=Lane.HANDOFF,
                detail=str(quarantined),
            )
        return MembershipResolution("ready", lane_plan=refreshed_lane_plan)

    async def policy_gate(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        lane: Lane,
        *,
        workflow_id: str | None = None,
        source_idempotency_key: str | None = None,
    ) -> PolicyGateResult:
        """Run the authoritative early policy gate before an autonomous lane (FR-5.9/5.10).

        ``register_or_rsvp`` repeats this as the data-plane pre-mutate guard immediately before
        the source mutation; this early activity only avoids needless source work (ADR-004).
        When called from the Temporal spine, the workflow-owned source key also records an
        immutable precheck fact; no raw event/provider/user content enters that record (FR-7.3,
        NFR-8/10, ADR-003/007).
        """
        audit_context = self._audit_context(workflow_id, source_idempotency_key)
        if event.price_status is not PriceStatus.FREE:
            return PolicyGateResult(
                False, "event price is not verified free; human handoff required"
            )
        target = self._target_for_lane(event, lane)
        if target is None:
            return PolicyGateResult(False, "no autonomous source target for lane")
        source, modality, _ = target
        consent_ref = await self._resolve_registration_consent(tenant_id, source, modality)
        if consent_ref is None:
            await self._record_policy_decision(
                tenant_id,
                audit_context,
                source,
                modality,
                RegistrationActionAuditPhase.POLICY_PRECHECK,
                False,
                consent_ref=None,
            )
            return PolicyGateResult(False, "registration consent evidence is unavailable")
        decision = await self._policy.evaluate(
            PolicyContext(tenant_id=tenant_id, source=source, modality=modality, action="register")
        )
        await self._record_policy_decision(
            tenant_id,
            audit_context,
            source,
            modality,
            RegistrationActionAuditPhase.POLICY_PRECHECK,
            decision.allowed,
            consent_ref=consent_ref,
        )
        return PolicyGateResult(
            decision.allowed,
            decision.reason,
            source_quarantined=decision.source_quarantined,
        )

    async def register_or_rsvp(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        lane: Lane,
        source_idempotency_key: str,
        *,
        workflow_id: str | None = None,
        registration_read_queue_item_id: str | None = None,
    ) -> RegisterResult:
        """Read remote state then perform at most one source mutation under a Pacer lease.

        A caller retries this *activity sequence* explicitly with the same key after an ACK loss.
        Every re-entry starts with a fresh calendar conflict gate and source-state detection, making
        a stale feed's blind re-POST unrepresentable (FR-4.5/5.3/5.5/5.10, NFR-8, ADR-003/005).
        A catalog event whose price is not verified free is rejected before either a source read or
        mutation, independently of any policy configuration.
        """
        audit_context = self._audit_context(workflow_id, source_idempotency_key)
        if event.price_status is not PriceStatus.FREE:
            return RegisterResult(
                RegisterOutcome.NEEDS_HANDOFF,
                "event price is not verified free; human handoff required",
            )
        target = self._target_for_lane(event, lane)
        if target is None:
            return RegisterResult(RegisterOutcome.NEEDS_HANDOFF, "no autonomous source target")
        source, modality, link = target
        adapter = self._sources.get(source)
        if adapter is None:
            return RegisterResult(RegisterOutcome.NEEDS_HANDOFF, "source adapter unavailable")
        consent_ref = await self._resolve_registration_consent(tenant_id, source, modality)
        if consent_ref is None:
            return RegisterResult(
                RegisterOutcome.NEEDS_HANDOFF,
                "registration consent evidence is unavailable",
            )

        conflict_outcome = await self._registration_conflict_outcome(tenant_id, event)
        if conflict_outcome is not None:
            return conflict_outcome

        read_outcome = await self._read_registration_state_or_outcome(
            tenant_id,
            source,
            adapter,
            link,
            modality,
            audit_context,
            consent_ref,
            queue_item_id=(
                registration_read_queue_item_id or f"{source_idempotency_key}:registration_read"
            ),
        )
        if read_outcome is not None:
            return read_outcome

        # Persist the early policy fact before spending browser/Pacer capacity. The enforcement
        # re-read lives after final admission in ``_perform_registration_mutation`` so a policy
        # flip while this activity waits cannot reach a source wire call (ADR-004).
        pacer_request = await self._pacer_request(
            tenant_id,
            source,
            PacerOperation.REGISTRATION_MUTATION,
            queue_item_id=source_idempotency_key,
        )
        decision = await self._policy.evaluate(
            PolicyContext(tenant_id=tenant_id, source=source, modality=modality, action="register")
        )
        try:
            consent_ref = await self._require_registration_consent(
                tenant_id, source, modality, consent_ref
            )
        except RegistrationConsentDeniedError as denied:
            return RegisterResult(RegisterOutcome.NEEDS_HANDOFF, denied.detail)
        await self._record_policy_decision(
            tenant_id,
            audit_context,
            source,
            modality,
            RegistrationActionAuditPhase.POLICY_PRE_MUTATE,
            decision.allowed,
            consent_ref=consent_ref,
        )
        if not decision.allowed:
            if decision.source_quarantined:
                return RegisterResult(RegisterOutcome.SOURCE_QUARANTINED, decision.reason)
            return RegisterResult(RegisterOutcome.NEEDS_HANDOFF, decision.reason)
        return await self._perform_registration_mutation(
            tenant_id,
            source,
            modality,
            adapter,
            link,
            source_idempotency_key,
            pacer_request,
            consent_ref,
            audit_context,
        )

    async def _perform_registration_mutation(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        adapter: SourcePort,
        link: RegistrationTarget,
        source_idempotency_key: str,
        pacer_request: PacerRequest,
        consent_ref: UUID,
        audit_context: tuple[str, str] | None,
    ) -> RegisterResult:
        """Run the final Pacer/evidence/policy fence and settle one source RSVP attempt.

        The caller already persisted its early pre-mutate policy fact. This boundary rereads
        policy after the final browser/Pacer admission and consent validation, immediately before
        the adapter call. A kill-switch or quarantine that changes while an activity waits cannot
        create a provider effect (FR-2.9, FR-5.3/5.9, FR-10.3, ADR-004/005).
        """
        source_result: RegisterResult | None = None
        try:
            browser_lease = await self._acquire_browser_admission(
                tenant_id, source, modality, source_idempotency_key
            )
            try:
                # Keep the rate permit immediately next to browser I/O. An unavailable/saturated
                # browser pool must not consume a scarce source token (ADR-003/005, AC-45).
                await self._require_pacer_lease(pacer_request)
                consent_ref = await self._require_registration_consent(
                    tenant_id, source, modality, consent_ref
                )
                final_policy = await self._policy.evaluate(
                    PolicyContext(
                        tenant_id=tenant_id,
                        source=source,
                        modality=modality,
                        action="register",
                    )
                )
                if not final_policy.allowed:
                    return RegisterResult(
                        (
                            RegisterOutcome.SOURCE_QUARANTINED
                            if final_policy.source_quarantined
                            else RegisterOutcome.NEEDS_HANDOFF
                        ),
                        final_policy.reason,
                    )
                try:
                    source_result = await adapter.register(
                        tenant_id, link, modality, source_idempotency_key
                    )
                except SourceRateLimitedError as throttle:
                    await self._defer_for_source_throttle(pacer_request, throttle)
                except SourceAccessDeniedError as denied:
                    await self._quarantine_after_access_denied(source, denied)
            finally:
                await self._release_browser_admission(browser_lease)
        except SourceQuarantinedError as quarantined:
            return await self._record_source_outcome(
                tenant_id,
                audit_context,
                source,
                modality,
                RegisterResult(RegisterOutcome.SOURCE_QUARANTINED, str(quarantined)),
                consent_ref=consent_ref,
            )
        except RegistrationConsentDeniedError as denied:
            return RegisterResult(RegisterOutcome.NEEDS_HANDOFF, denied.detail)
        if source_result is None:
            raise RuntimeError("registration source returned no settled result")
        return await self._record_source_outcome(
            tenant_id,
            audit_context,
            source,
            modality,
            source_result,
            consent_ref=consent_ref,
        )

    async def await_confirmation(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        lane: Lane,
        source_outcome: RegisterOutcome,
        *,
        awaiting_transition_id: str,
        registered_transition_id: str,
        confirmation_read_queue_item_id: str | None = None,
        confirmation_reference: str | None = None,
    ) -> ConfirmationResult:
        """Record the confirmation state; the durable timer/signal itself lives in the workflow.

        An immediate source confirmation advances to ``REGISTERED``. A pending result records
        ``AWAITING_CONFIRMATION`` then releases the worker so the workflow can race a signal against
        the 24-hour timer. The opaque signal reference only wakes this activity; a fresh paced
        source read must verify the confirmation before it advances (FR-8.4, ADR-003/011).
        """
        lifecycle = await self._lifecycle.get_or_create(
            tenant_id, event.canonical_event_id, workflow_id
        )
        if lifecycle.state in (LifecycleState.REGISTERED, LifecycleState.SCHEDULED):
            return ConfirmationResult(ConfirmationStatus.CONFIRMED, "already registered")
        if source_outcome is RegisterOutcome.SOURCE_QUARANTINED:
            return ConfirmationResult(
                ConfirmationStatus.QUARANTINED,
                "source quarantined before confirmation could be recorded",
            )
        if confirmation_reference is not None:
            try:
                verified = await self._verify_confirmation_reference(
                    tenant_id,
                    event,
                    lane,
                    source_outcome,
                    confirmation_reference,
                    confirmation_read_queue_item_id
                    or f"{awaiting_transition_id}:confirmation_read",
                )
            except SourceQuarantinedError as quarantined:
                return ConfirmationResult(ConfirmationStatus.QUARANTINED, str(quarantined))
            except SourcePolicyDeniedError as denied:
                return ConfirmationResult(ConfirmationStatus.FAILED, denied.decision.reason)
            except RegistrationConsentDeniedError as denied:
                return ConfirmationResult(ConfirmationStatus.FAILED, denied.detail)
            if isinstance(verified, ConfirmationResult):
                return verified
            source_outcome = verified
        if source_outcome is RegisterOutcome.PENDING_CONFIRMATION:
            lifecycle.lane = lane
            if lifecycle.state is LifecycleState.FOUND:
                await self._lifecycle.transition(
                    lifecycle,
                    LifecycleState.AWAITING_CONFIRMATION,
                    awaiting_transition_id,
                    {
                        "canonical_event_id": str(event.canonical_event_id),
                        "lane": lane.value,
                        "workflow_id": workflow_id,
                    },
                )
            return ConfirmationResult(ConfirmationStatus.PENDING, "awaiting source confirmation")
        if source_outcome not in (
            RegisterOutcome.CONFIRMED,
            RegisterOutcome.NO_OP_ALREADY_CONFIRMED,
        ):
            return ConfirmationResult(ConfirmationStatus.FAILED, source_outcome.value)

        registration_source = _LANE_SOURCE.get(lane)
        if registration_source is None:
            return ConfirmationResult(ConfirmationStatus.FAILED, "no source target to record")
        lifecycle.lane = lane
        if lifecycle.state not in (LifecycleState.FOUND, LifecycleState.AWAITING_CONFIRMATION):
            return ConfirmationResult(
                ConfirmationStatus.FAILED, f"lifecycle is {lifecycle.state.value}"
            )
        await self._lifecycle.transition(
            lifecycle,
            LifecycleState.REGISTERED,
            registered_transition_id,
            {
                "canonical_event_id": str(event.canonical_event_id),
                "lane": lane.value,
                "registration_source": registration_source.value,
                "workflow_id": workflow_id,
            },
        )
        return ConfirmationResult(ConfirmationStatus.CONFIRMED)

    async def _verify_confirmation_reference(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        lane: Lane,
        source_outcome: RegisterOutcome,
        confirmation_reference: str,
        confirmation_read_queue_item_id: str,
    ) -> RegisterOutcome | ConfirmationResult:
        """Turn an opaque wake-up signal into a freshly verified source confirmation (FR-8.4)."""
        if source_outcome is not RegisterOutcome.PENDING_CONFIRMATION:
            return ConfirmationResult(ConfirmationStatus.FAILED, "unexpected confirmation signal")
        if not confirmation_reference:
            return ConfirmationResult(ConfirmationStatus.FAILED, "empty confirmation reference")
        target = self._target_for_lane(event, lane)
        if target is None:
            return ConfirmationResult(ConfirmationStatus.FAILED, "no source target to verify")
        source, modality, link = target
        adapter = self._sources.get(source)
        if adapter is None:
            return ConfirmationResult(ConfirmationStatus.FAILED, "source adapter unavailable")
        # The signal is merely an opaque wake-up reference; only a fresh remote read may advance
        # lifecycle state to REGISTERED (FR-8.4, ADR-003/011).
        state = await self._paced_registration_state(
            tenant_id,
            source,
            adapter,
            link,
            modality,
            PacerOperation.CONFIRMATION_READ,
            queue_item_id=confirmation_read_queue_item_id,
        )
        # A signal is only a wake-up: eventual-consistency readback can briefly report either a
        # still-pending RSVP or no visible RSVP. Neither observation can advance the lifecycle or
        # justify an early handoff; retain the durable wait for a later independently confirmed
        # read (FR-8.4, FR-16.1, ADR-003).
        if state in (RsvpState.PENDING_CONFIRMATION, RsvpState.NOT_PRESENT):
            return ConfirmationResult(
                ConfirmationStatus.PENDING,
                "source confirmation not visible yet",
            )
        if state is not RsvpState.CONFIRMED:
            return ConfirmationResult(
                ConfirmationStatus.FAILED, "source confirmation was not verified"
            )
        return RegisterOutcome.CONFIRMED

    async def replay_handoff_completion(
        self,
        tenant_id: UUID,
        task_id: str,
        completion_id: str,
    ) -> HandoffCompletionResult | None:
        """Recover an exact committed completion before repeating any provider or catalog read."""
        receipt = await self._handoff.get_completion_attempt(
            tenant_id,
            task_id,
            completion_id,
        )
        if receipt is None:
            return None
        return self._handoff_completion_from_receipt(receipt)

    @staticmethod
    def _handoff_completion_from_receipt(
        receipt: HandoffCompletionReceipt,
    ) -> HandoffCompletionResult:
        if receipt.outcome is HandoffCompletionOutcome.REVIEW_REQUIRED:
            if receipt.registration_source is not None or receipt.conflict_warning is not None:
                raise RuntimeError("review completion receipt contains verified-only fields")
            return HandoffCompletionResult(
                HandoffCompletionStatus.REVIEW_REQUIRED,
                receipt.detail,
            )
        if receipt.outcome is HandoffCompletionOutcome.VERIFIED:
            if receipt.registration_source is None or receipt.conflict_warning is None:
                raise RuntimeError("verified completion receipt is incomplete")
            return HandoffCompletionResult(
                HandoffCompletionStatus.CONFIRMED,
                receipt.detail or "registration independently verified",
                conflict_warning=receipt.conflict_warning,
            )
        raise RuntimeError("handoff completion receipt has an unsupported outcome")

    async def complete_handoff(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        *,
        task_id: str,
        completion_id: str,
        registered_transition_id: str,
        verification_read_queue_item_id: str,
    ) -> HandoffCompletionResult:
        """Independently verify a mark-done before consuming it and advancing the lifecycle.

        A user click is only a wake-up signal. The activity freshly reads the provider registration
        state, then freshly reads every connected calendar through ``free_busy``. Only a confirmed
        provider state can atomically consume the capability, complete the task, and transition
        ``HANDOFF -> REGISTERED``. Authoritative negative/ambiguous verification is durably
        recorded for review; transport/provider exceptions remain retryable and never consume the
        one-time capability merely because a dependency was temporarily unavailable (FR-6.3,
        FR-16, NFR-8).
        """
        replay = await self.replay_handoff_completion(tenant_id, task_id, completion_id)
        if replay is not None:
            return replay
        task = await self._handoff.get(tenant_id, task_id)
        lifecycle = await self._lifecycle.find_active(tenant_id, event.canonical_event_id)
        if (
            task is None
            or lifecycle is None
            or lifecycle.workflow_id != workflow_id
            or task.workflow_id != workflow_id
            or task.canonical_event_id != event.canonical_event_id
            or task.state not in {HandoffState.OPEN, HandoffState.NOTIFIED}
            or lifecycle.state is not LifecycleState.HANDOFF
        ):
            return HandoffCompletionResult(
                HandoffCompletionStatus.INACTIVE,
                "handoff task is no longer active",
            )

        target = self._handoff_verification_target(event, task)
        if target is None:
            return await self._route_handoff_completion_review(
                task,
                completion_id,
                "no independent registration read-back is available",
            )
        source, modality, registration_target = target
        adapter = self._sources[source]
        try:
            state = await self._paced_registration_state(
                tenant_id,
                source,
                adapter,
                registration_target,
                modality,
                PacerOperation.CONFIRMATION_READ,
                queue_item_id=verification_read_queue_item_id,
            )
        except PacerDeferredError:
            # Every non-granted admission result is transient workflow control, including a
            # projected degrade or browser saturation. Temporal owns its durable retry timer; no
            # Pacer outcome is independent evidence that the user's registration claim is false.
            raise
        except (
            RegistrationConsentDeniedError,
            SourcePolicyDeniedError,
            SourceQuarantinedError,
            SourceReconsentRequiredError,
        ) as exc:
            detail = _permanent_source_verification_detail(exc)
            _log.info(
                "handoff_completion_verification_requires_review",
                workflow_id=workflow_id,
                task_id=task_id,
                outcome=type(exc).__name__,
            )
            return await self._route_handoff_completion_review(
                task,
                completion_id,
                detail,
            )
        except Exception as exc:
            _log.warning(
                "handoff_completion_verification_failed",
                workflow_id=workflow_id,
                task_id=task_id,
                error=type(exc).__name__,
            )
            raise
        if state is not RsvpState.CONFIRMED:
            return await self._route_handoff_completion_review(
                task,
                completion_id,
                f"independent registration verification returned {state.value}",
            )

        event_end = event.end_at or (event.start_at + _DEFAULT_DURATION)
        try:
            busy = await self._calendar.free_busy(tenant_id, event.start_at, event_end)
        except (CalendarBindingUnavailableError, CalendarReconsentRequiredError) as exc:
            detail = (
                "calendar authorization requires re-consent"
                if isinstance(exc, CalendarReconsentRequiredError)
                else "calendar binding is not available"
            )
            _log.info(
                "handoff_completion_calendar_requires_review",
                workflow_id=workflow_id,
                task_id=task_id,
                outcome=type(exc).__name__,
            )
            return await self._route_handoff_completion_review(
                task,
                completion_id,
                detail,
            )
        except Exception as exc:
            _log.warning(
                "handoff_completion_free_busy_failed",
                workflow_id=workflow_id,
                task_id=task_id,
                error=type(exc).__name__,
            )
            raise
        conflict_warning = (
            evaluate_conflict(event.start_at, event_end, busy) is ConflictVerdict.BLOCKED
        )
        try:
            await self._handoff.complete_verified(
                task,
                lifecycle,
                transition_id=registered_transition_id,
                completion_id=completion_id,
                registration_source=source,
                conflict_warning=conflict_warning,
                outbox_payload={
                    "canonical_event_id": str(event.canonical_event_id),
                    "workflow_id": workflow_id,
                    "event_summary": event.title,
                    "task_id": task.task_id,
                    "completion_id": completion_id,
                    "evidence": "user_mark_done",
                    "verification": "source_read_back",
                    "conflict_warning": conflict_warning,
                    "registration_source": source.value,
                },
            )
        except Exception:
            if await self._handoff_completion_became_inactive(task):
                return HandoffCompletionResult(
                    HandoffCompletionStatus.INACTIVE,
                    "handoff task became inactive during verification",
                )
            raise
        return HandoffCompletionResult(
            HandoffCompletionStatus.CONFIRMED,
            "registration independently verified",
            conflict_warning=conflict_warning,
        )

    async def _route_handoff_completion_review(
        self,
        task: HandoffTask,
        completion_id: str,
        detail: str,
    ) -> HandoffCompletionResult:
        """Consume one failed self-report into a durable review receipt exactly once."""
        try:
            await self._handoff.record_completion_review(
                task,
                completion_id=completion_id,
                detail=detail,
            )
        except Exception:
            if await self._handoff_completion_became_inactive(task):
                return HandoffCompletionResult(
                    HandoffCompletionStatus.INACTIVE,
                    "handoff task became inactive during verification",
                )
            raise
        return HandoffCompletionResult(HandoffCompletionStatus.REVIEW_REQUIRED, detail)

    async def _handoff_completion_became_inactive(self, task: HandoffTask) -> bool:
        """Distinguish an expiry/terminal race from a retryable persistence failure."""
        refreshed_task = await self._handoff.get(task.tenant_id, task.task_id)
        lifecycle = await self._lifecycle.find_active(
            task.tenant_id,
            task.canonical_event_id,
        )
        return (
            refreshed_task is None
            or refreshed_task.state not in {HandoffState.OPEN, HandoffState.NOTIFIED}
            or lifecycle is None
            or lifecycle.workflow_id != task.workflow_id
            or lifecycle.state is not LifecycleState.HANDOFF
        )

    def _handoff_verification_target(
        self,
        event: CanonicalEvent,
        task: HandoffTask,
    ) -> tuple[Source, Modality, RegistrationTarget] | None:
        """Select only a retained source link with a bound read-back adapter.

        The task deep link is the authoritative handoff surface. A merged event may carry several
        source links, so an exact URL match wins; falling back is allowed only when exactly one
        retained link exists. No caller-supplied URL/source enters this decision.
        """
        exact = [link for link in event.source_links if link.registration_url == task.deep_link]
        links = exact if exact else (event.source_links if len(event.source_links) == 1 else [])
        for link in links:
            adapter = self._sources.get(link.source)
            if adapter is None:
                continue
            if link.source is Source.MEETUP:
                modality = Modality.API
            elif link.source is Source.LUMA:
                modality = Modality.BROWSER
            else:
                # Future API-backed exact read-back adapters can opt in without changing the
                # capability contract. Discovery-only adapters still fail closed on consent,
                # policy, or a non-confirmed state.
                modality = Modality.API if adapter.capability.supports_api else Modality.BROWSER
            return (
                link.source,
                modality,
                RegistrationTarget(link.source_event_id, link.registration_url),
            )
        return None

    def dedupe_calendar(self, tenant_id: UUID, event: CanonicalEvent) -> CalendarEntry:
        """Prepare the deterministic, IANA-zone calendar upsert (FR-9.2/9.3/9.5, ADR-003)."""
        return CalendarEntry(
            calendar_event_id=ids.calendar_event_id(tenant_id, event.canonical_event_id),
            canonical_event_id=event.canonical_event_id,
            title=event.title,
            start_at=event.start_at,
            end_at=event.end_at or (event.start_at + _DEFAULT_DURATION),
            time_zone=_iana_time_zone(event.start_at),
            location=event.venue_name,
            private_metadata={
                "events_concierge.registration_state": "registered",
                "events_concierge.source_event_ids": json.dumps(
                    sorted(
                        f"{link.source.value}:{link.source_event_id}" for link in event.source_links
                    ),
                    separators=(",", ":"),
                ),
            },
        )

    async def write_to_calendar(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        calendar_event_id: str,
        scheduled_transition_id: str,
    ) -> str:
        """Idempotently write then atomically advance ``REGISTERED`` to ``SCHEDULED``.

        Retrying after a crash between the calendar upsert and transition repeats the same
        deterministic upsert and transition id, yielding one entry and one outbox row (NFR-8).
        """
        entry = self.dedupe_calendar(tenant_id, event)
        if entry.calendar_event_id != calendar_event_id:
            raise ValueError("calendar id was not minted from tenant and canonical event")
        await self._calendar.upsert_event(tenant_id, entry)
        lifecycle = await self._lifecycle.get_or_create(
            tenant_id, event.canonical_event_id, workflow_id
        )
        if lifecycle.state is LifecycleState.SCHEDULED:
            return calendar_event_id
        if lifecycle.state is not LifecycleState.REGISTERED:
            raise IllegalTransitionError(
                f"calendar write requires registered lifecycle, got {lifecycle.state.value}"
            )
        await self._lifecycle.transition(
            lifecycle,
            LifecycleState.SCHEDULED,
            scheduled_transition_id,
            {"calendar_event_id": calendar_event_id, "workflow_id": workflow_id},
        )
        return calendar_event_id

    async def route_to_handoff(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        *,
        handoff_task_id: str,
        handoff_transition_id: str,
        handoff_expiry_transition_id: str | None = None,
        reason: HandoffReason = HandoffReason.DEFERRED_REGISTER,
        detail: str = "",
    ) -> RegistrationResult:
        """Create a workflow-keyed handoff compensation and transition when still pre-registration."""
        lifecycle = await self._lifecycle.get_or_create(
            tenant_id, event.canonical_event_id, workflow_id
        )
        if lifecycle.state is LifecycleState.SCHEDULED:
            return RegistrationResult(
                RegistrationStatus.REGISTERED,
                lifecycle.lane,
                calendar_event_id=ids.calendar_event_id(tenant_id, event.canonical_event_id),
                detail="already scheduled",
            )
        link = event.source_links[0] if event.source_links else None
        existing_task = await self._handoff.get(tenant_id, handoff_task_id)
        completion_token = (
            secrets.token_urlsafe(32)
            if existing_task is None
            and lifecycle.state in (LifecycleState.FOUND, LifecycleState.AWAITING_CONFIRMATION)
            and reason
            not in {HandoffReason.CALENDAR_WRITE_FAILED, HandoffReason.WITHDRAWAL_REQUIRED}
            else None
        )
        task = HandoffTask(
            task_id=handoff_task_id,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            canonical_event_id=event.canonical_event_id,
            reason=reason,
            deep_link=link.registration_url if link else "",
            event_summary=event.title,
            ttl_expires_at=self._handoff_deadline(event),
            state=HandoffState.OPEN,
            metadata={"detail": detail} if detail else {},
            expiry_transition_id=handoff_expiry_transition_id,
            completion_token=completion_token,
        )
        if lifecycle.state in (LifecycleState.FOUND, LifecycleState.AWAITING_CONFIRMATION):
            completion_path = (
                f"/v1/tasks/{completion_token}/done" if completion_token is not None else None
            )
            completion_projection: dict[str, object] = {}
            if completion_path is not None:
                if self._notification_secret_protector is None:
                    raise RuntimeError("handoff completion requires a NotificationSecretProtector")
                completion_url = (
                    f"{self._handoff_completion_base_url}{completion_path}"
                    if self._handoff_completion_base_url
                    else completion_path
                )
                completion_projection[
                    "protected_completion_url"
                ] = await self._notification_secret_protector.protect_completion_url(
                    tenant_id,
                    completion_url,
                )
            await self._handoff.create_and_transition(
                task,
                lifecycle,
                LifecycleState.HANDOFF,
                handoff_transition_id,
                {
                    "task_id": task.task_id,
                    "reason": task.reason.value,
                    "workflow_id": workflow_id,
                    "event_summary": task.event_summary,
                    "deep_link": task.deep_link,
                    **completion_projection,
                },
            )
        elif lifecycle.state in (LifecycleState.HANDOFF, LifecycleState.REGISTERED):
            # A replay against an already-held handoff or registered recovery preserves the stable
            # task id. Only pre-registration routing needs the coupled lifecycle transition.
            await self._handoff.create(task)
        persisted = await self._handoff.get(tenant_id, task.task_id)
        if persisted is None:
            raise RuntimeError("handoff task was not durable after its task transaction")
        if lifecycle.state is LifecycleState.REGISTERED:
            return RegistrationResult(
                RegistrationStatus.REGISTERED,
                lifecycle.lane,
                handoff_task_id=persisted.task_id,
                detail=detail,
                handoff_expires_at=persisted.ttl_expires_at,
                handoff_expiry_transition_id=persisted.resolved_expiry_transition_id(),
                handoff_created_at=persisted.created_at,
            )
        return RegistrationResult(
            RegistrationStatus.HANDOFF,
            Lane.HANDOFF,
            handoff_task_id=persisted.task_id,
            detail=detail,
            handoff_expires_at=persisted.ttl_expires_at,
            handoff_expiry_transition_id=persisted.resolved_expiry_transition_id(),
            handoff_created_at=persisted.created_at,
        )

    async def close_failed_candidate(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        *,
        transition_id: str,
        reason: str,
    ) -> CandidateCloseResult:
        """Terminalize a child the parent declines to retain as the request handoff.

        ``resolve_membership`` has already opened the per-event lifecycle before reporting a
        failed child outcome.  Returning from the child without this guarded terminal transition
        would leave a closed workflow paired with an active ``found``/``awaiting_confirmation``
        row.  The transition's audit outbox is intentionally marked non-user-facing: another
        candidate may still satisfy the same request (FR-5.0, FR-6.6, ADR-003/007).
        """
        lifecycle = await self._lifecycle.get_or_create(
            tenant_id, event.canonical_event_id, workflow_id
        )
        if lifecycle.state.is_terminal:
            return CandidateCloseResult(
                CandidateCloseStatus.ALREADY_TERMINAL,
                lifecycle.state,
                f"lifecycle already {lifecycle.state.value}",
            )
        if lifecycle.state is LifecycleState.FOUND:
            terminal_state = LifecycleState.FAILED_NO_CANDIDATE
        elif lifecycle.state is LifecycleState.AWAITING_CONFIRMATION:
            # A source confirmation that never arrived is not a user-visible no-result: the
            # request parent may still advance another candidate, and the source state was never
            # factually confirmed.  ``cancelled`` is the legal cleanup edge in ADR-007.
            terminal_state = LifecycleState.CANCELLED
        else:
            return CandidateCloseResult(
                CandidateCloseStatus.IGNORED,
                detail=f"lifecycle {lifecycle.state.value} cannot close a failed candidate",
            )
        await self._lifecycle.transition(
            lifecycle,
            terminal_state,
            transition_id,
            {
                "canonical_event_id": str(event.canonical_event_id),
                "workflow_id": workflow_id,
                "event_summary": event.title,
                "candidate_close_reason": reason,
                "notification_suppressed": True,
            },
        )
        return CandidateCloseResult(CandidateCloseStatus.CLOSED, terminal_state)

    async def compensate_calendar_write(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        workflow_id: str,
        *,
        handoff_task_id: str,
        handoff_expiry_transition_id: str | None = None,
        detail: str,
    ) -> RegistrationResult:
        """Forward-recover a permanently failed calendar write without unsafe automatic un-RSVP.

        The registration is factual and valuable, so ADR-007 requires a manual calendar-recovery
        task rather than an automatic source compensation that could withdraw a valid RSVP. The
        task and relay record commit together; the lifecycle truth remains ``REGISTERED``.
        """
        lifecycle = await self._lifecycle.get_or_create(
            tenant_id, event.canonical_event_id, workflow_id
        )
        if lifecycle.state is LifecycleState.SCHEDULED:
            return RegistrationResult(
                RegistrationStatus.REGISTERED,
                lifecycle.lane,
                calendar_event_id=ids.calendar_event_id(tenant_id, event.canonical_event_id),
                detail="calendar write recovered before compensation",
            )
        if lifecycle.state is not LifecycleState.REGISTERED:
            raise IllegalTransitionError(
                f"calendar recovery requires registered lifecycle, got {lifecycle.state.value}"
            )
        # A direct activity/facade retry may not carry the workflow's persisted expiry identity.
        # Reuse the durable task's identity and deadline when it already exists; minting the
        # deterministic fallback here would disagree with the workflow-owned key and turn a lost
        # acknowledgement into a false task-id collision (FR-6.6, NFR-8, ADR-003/007).
        existing_task = await self._handoff.get(tenant_id, handoff_task_id)
        link = event.source_links[0] if event.source_links else None
        task = HandoffTask(
            task_id=handoff_task_id,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            canonical_event_id=event.canonical_event_id,
            reason=HandoffReason.CALENDAR_WRITE_FAILED,
            deep_link=link.registration_url if link else "",
            event_summary=event.title,
            ttl_expires_at=(
                existing_task.ttl_expires_at
                if existing_task is not None
                else self._handoff_deadline(event)
            ),
            state=HandoffState.OPEN,
            metadata={"detail": detail} if detail else {},
            expiry_transition_id=(
                handoff_expiry_transition_id
                if handoff_expiry_transition_id is not None
                else (
                    existing_task.resolved_expiry_transition_id()
                    if existing_task is not None
                    else None
                )
            ),
        )
        await self._handoff.create_calendar_recovery(
            task,
            {
                "task_id": task.task_id,
                "canonical_event_id": str(event.canonical_event_id),
                "workflow_id": workflow_id,
                "reason": task.reason.value,
                "event_summary": task.event_summary,
                "deep_link": task.deep_link,
            },
        )
        persisted = await self._handoff.get(tenant_id, task.task_id)
        if persisted is None:
            raise RuntimeError("calendar recovery task was not durable after its task transaction")
        return RegistrationResult(
            RegistrationStatus.REGISTERED,
            lifecycle.lane,
            handoff_task_id=persisted.task_id,
            detail="calendar recovery required",
            handoff_expires_at=persisted.ttl_expires_at,
            handoff_expiry_transition_id=persisted.resolved_expiry_transition_id(),
            handoff_created_at=persisted.created_at,
        )

    async def register_event(
        self, tenant_id: UUID, event: CanonicalEvent, workflow_id: str, lane_plan: tuple[Lane, ...]
    ) -> RegistrationResult:
        """Sequential facade retained for the local slice; production drives the same steps in Temporal."""
        resolved = await self.resolve_membership(tenant_id, event, workflow_id, lane_plan)
        if resolved.status == "scheduled":
            return RegistrationResult(
                RegistrationStatus.REGISTERED,
                resolved.lane,
                calendar_event_id=ids.calendar_event_id(tenant_id, event.canonical_event_id),
                detail=resolved.detail,
            )
        if resolved.status == "registered":
            calendar_id = ids.calendar_event_id(tenant_id, event.canonical_event_id)
            await self.write_to_calendar(
                tenant_id,
                event,
                workflow_id,
                calendar_id,
                f"{workflow_id}:scheduled",
            )
            return RegistrationResult(RegistrationStatus.REGISTERED, resolved.lane, calendar_id)
        if resolved.status == "handoff":
            return RegistrationResult(
                RegistrationStatus.HANDOFF, Lane.HANDOFF, detail=resolved.detail
            )
        if resolved.status == "quarantined":
            return await self._route_quarantine_handoff(
                tenant_id, event, workflow_id, resolved.detail
            )

        for lane in resolved.lane_plan:
            if lane is Lane.HANDOFF:
                break
            gate = await self.policy_gate(tenant_id, event, lane)
            if not gate.allowed:
                if gate.source_quarantined:
                    return await self._route_quarantine_handoff(
                        tenant_id, event, workflow_id, gate.detail
                    )
                continue
            outcome = await self.register_or_rsvp(
                tenant_id, event, lane, f"{workflow_id}:register_or_rsvp:{lane.value}:1"
            )
            if outcome.outcome is RegisterOutcome.SOURCE_QUARANTINED:
                return await self._route_quarantine_handoff(
                    tenant_id, event, workflow_id, outcome.detail
                )
            confirmation = await self.await_confirmation(
                tenant_id,
                event,
                workflow_id,
                lane,
                outcome.outcome,
                awaiting_transition_id=f"{workflow_id}:awaiting-confirmation",
                registered_transition_id=f"{workflow_id}:registered",
            )
            if confirmation.status is ConfirmationStatus.QUARANTINED:
                return await self._route_quarantine_handoff(
                    tenant_id, event, workflow_id, confirmation.detail
                )
            if confirmation.status is not ConfirmationStatus.CONFIRMED:
                continue
            entry = self.dedupe_calendar(tenant_id, event)
            calendar_id = await self.write_to_calendar(
                tenant_id,
                event,
                workflow_id,
                entry.calendar_event_id,
                f"{workflow_id}:scheduled",
            )
            return RegistrationResult(RegistrationStatus.REGISTERED, lane, calendar_id)

        return await self.route_to_handoff(
            tenant_id,
            event,
            workflow_id,
            handoff_task_id=new_ulid(),
            handoff_transition_id=f"{workflow_id}:handoff",
            detail=resolved.detail,
        )

    async def _route_quarantine_handoff(
        self, tenant_id: UUID, event: CanonicalEvent, workflow_id: str, detail: str
    ) -> RegistrationResult:
        """Route a terminal source-circuit-breaker result without trying another lane.

        The local sequential facade is not the production Temporal spine, but it shares the same
        safety contract: a durable global source quarantine is not a candidate-local failure.
        Returning immediately prevents an alternate lane from issuing another provider request
        after a ban/403 (FR-10.3, AC-72, ADR-003/004).
        """
        return await self.route_to_handoff(
            tenant_id,
            event,
            workflow_id,
            handoff_task_id=new_ulid(),
            handoff_transition_id=f"{workflow_id}:handoff",
            detail=detail,
        )

    def _handoff_deadline(self, event: CanonicalEvent) -> datetime:
        """Cap the owner-ratified manual-task TTL at the event start (ADR-007, FR-6.6)."""
        now = self._now()
        if event.start_at.tzinfo is None or event.start_at.utcoffset() is None:
            raise ValueError("handoff event start must be timezone-aware")
        return min(now + self._ttl, event.start_at)

    def _target_for_lane(
        self, event: CanonicalEvent, lane: Lane
    ) -> tuple[Source, Modality, RegistrationTarget] | None:
        source = _LANE_SOURCE.get(lane)
        modality = _LANE_MODALITY.get(lane)
        if source is None or modality is None:
            return None
        link = next(
            (
                link
                for link in event.source_links
                if link.source is source and link.price_status is PriceStatus.FREE
            ),
            None,
        )
        if link is None:
            return None
        return (
            source,
            modality,
            RegistrationTarget(
                source_event_id=link.source_event_id,
                registration_url=link.registration_url,
            ),
        )

    def _audit_context(
        self, workflow_id: str | None, source_idempotency_key: str | None
    ) -> tuple[str, str] | None:
        """Enable audit only for a workflow-owned identity pair.

        Direct callers have always supplied a source idempotency key to protect the RSVP effect but
        do not have Temporal's deterministic workflow identity. They remain intentionally unaudited
        in this bounded increment. Temporal activities supply both values from child-workflow state,
        so a missing source identity or port there is a fail-closed composition error rather than
        silently incomplete evidence (FR-7.3, NFR-8/10, ADR-003/007).
        """
        if workflow_id is None:
            return None
        if source_idempotency_key is None or not source_idempotency_key:
            raise ValueError("registration action audit source identity must be non-empty")
        if self._action_audit is None:
            raise RuntimeError("registration action audit port is not configured")
        return workflow_id, source_idempotency_key

    async def _resolve_registration_consent(
        self, tenant_id: UUID, source: Source, modality: Modality
    ) -> UUID | None:
        """Resolve the fixed P14c evidence before any event-source boundary (FR-2.9, ADR-004).

        Consent remains necessary but never sufficient: successful resolution merely permits the
        existing policy, Pacer, browser-admission, conflict, and source-state guards to run.  The
        port intentionally exposes no grant/revoke operation, so a missing port or lookup outage
        has the same fail-closed result as absent owner-seeded evidence.
        """
        evidence = self._registration_consent
        if evidence is None:
            return None
        try:
            consent_ref = await evidence.resolve(
                tenant_id,
                source,
                modality,
                ConsentScope.REGISTRATION,
            )
        except Exception as error:
            _log.warning(
                "registration_consent_resolution_failed",
                source=source.value,
                modality=modality.value,
                error_type=type(error).__name__,
            )
            return None
        if consent_ref is not None and not isinstance(consent_ref, UUID):
            _log.error(
                "registration_consent_resolution_invalid",
                source=source.value,
                modality=modality.value,
            )
            return None
        return consent_ref

    async def _require_registration_consent(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        consent_ref: UUID | None = None,
    ) -> UUID:
        """Resolve or revalidate an exact reference without creating any consent authority.

        Source reads use the resolve branch before their Pacer/browser/source call.  The RSVP
        mutation passes its prior reference through the validation branch immediately after the
        Pacer grant, fencing an evidence loss before the wire effect (FR-2.9, FR-5.3, ADR-004/005).
        """
        if consent_ref is None:
            resolved = await self._resolve_registration_consent(tenant_id, source, modality)
            if resolved is not None:
                return resolved
            raise RegistrationConsentDeniedError()

        evidence = self._registration_consent
        if evidence is None:
            raise RegistrationConsentDeniedError(
                "registration consent evidence no longer validates"
            )
        try:
            valid = await evidence.validate(
                consent_ref,
                tenant_id,
                source,
                modality,
                ConsentScope.REGISTRATION,
            )
        except Exception as error:
            _log.warning(
                "registration_consent_validation_failed",
                source=source.value,
                modality=modality.value,
                error_type=type(error).__name__,
            )
            valid = False
        if valid is True:
            return consent_ref
        raise RegistrationConsentDeniedError("registration consent evidence no longer validates")

    async def _append_action_audit(
        self,
        tenant_id: UUID,
        workflow_id: str,
        source_idempotency_key: str,
        source: Source,
        modality: Modality,
        phase: RegistrationActionAuditPhase,
        policy_decision: RegistrationActionAuditDecision,
        outcome: RegistrationActionAuditOutcome | None = None,
        consent_ref: UUID | None = None,
    ) -> None:
        """Append one opaque fact through the guarded port before/after the bounded action."""
        audit = self._action_audit
        if audit is None:
            raise RuntimeError("registration action audit port is not configured")
        await audit.append(
            RegistrationActionAudit(
                audit_key=f"{source_idempotency_key}:{phase.value}",
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                source=source,
                modality=modality,
                phase=phase,
                policy_decision=policy_decision,
                outcome=outcome,
                consent_ref=consent_ref,
            )
        )

    async def _record_source_outcome(
        self,
        tenant_id: UUID,
        audit_context: tuple[str, str] | None,
        source: Source,
        modality: Modality,
        result: RegisterResult,
        *,
        consent_ref: UUID | None,
    ) -> RegisterResult:
        """Persist a normalized settled source outcome; transient Pacer states never reach here."""
        if audit_context is None:
            return result
        normalized = _SOURCE_OUTCOME_TO_AUDIT.get(result.outcome)
        if normalized is None:
            raise ValueError("registration result has no audit-safe normalized outcome")
        workflow_id, source_idempotency_key = audit_context
        await self._append_action_audit(
            tenant_id,
            workflow_id,
            source_idempotency_key,
            source,
            modality,
            RegistrationActionAuditPhase.SOURCE_RSVP_OUTCOME,
            RegistrationActionAuditDecision.ALLOWED,
            normalized,
            consent_ref=consent_ref,
        )
        return result

    async def _record_policy_decision(
        self,
        tenant_id: UUID,
        audit_context: tuple[str, str] | None,
        source: Source,
        modality: Modality,
        phase: RegistrationActionAuditPhase,
        allowed: bool,
        *,
        consent_ref: UUID | None,
    ) -> None:
        """Persist an early or pre-mutation policy fact when the Temporal audit context is present."""
        if audit_context is None:
            return
        workflow_id, source_idempotency_key = audit_context
        await self._append_action_audit(
            tenant_id,
            workflow_id,
            source_idempotency_key,
            source,
            modality,
            phase,
            (
                RegistrationActionAuditDecision.ALLOWED
                if allowed
                else RegistrationActionAuditDecision.DENIED
            ),
            consent_ref=consent_ref,
        )

    async def _read_registration_state_or_outcome(
        self,
        tenant_id: UUID,
        source: Source,
        adapter: SourcePort,
        link: RegistrationTarget,
        modality: Modality,
        audit_context: tuple[str, str] | None,
        consent_ref: UUID,
        *,
        queue_item_id: str,
    ) -> RegisterResult | None:
        """Resolve the read-before-mutate state or return its settled safe outcome.

        This keeps the top-level activity method focused on the mutation boundary while preserving
        the distinction between a new raw ban signal (a source outcome exists) and a retry seeing
        an already-durable quarantine before any provider read (FR-5.3, FR-10.3, AC-72).
        """
        try:
            state = await self._paced_registration_state(
                tenant_id,
                source,
                adapter,
                link,
                modality,
                PacerOperation.REGISTRATION_READ,
                queue_item_id=queue_item_id,
                consent_ref=consent_ref,
            )
        except SourceQuarantinedError as quarantined:
            result = RegisterResult(RegisterOutcome.SOURCE_QUARANTINED, str(quarantined))
            if quarantined.from_policy:
                return result
            return await self._record_source_outcome(
                tenant_id,
                audit_context,
                source,
                modality,
                result,
                consent_ref=consent_ref,
            )
        except SourcePolicyDeniedError as denied:
            return RegisterResult(RegisterOutcome.NEEDS_HANDOFF, denied.decision.reason)
        except RegistrationConsentDeniedError as denied:
            return RegisterResult(RegisterOutcome.NEEDS_HANDOFF, denied.detail)

        if state is RsvpState.CONFIRMED:
            return await self._record_source_outcome(
                tenant_id,
                audit_context,
                source,
                modality,
                RegisterResult(
                    RegisterOutcome.NO_OP_ALREADY_CONFIRMED, "remote RSVP already confirmed"
                ),
                consent_ref=consent_ref,
            )
        if state is RsvpState.PENDING_CONFIRMATION:
            return await self._record_source_outcome(
                tenant_id,
                audit_context,
                source,
                modality,
                RegisterResult(
                    RegisterOutcome.PENDING_CONFIRMATION,
                    "remote RSVP is awaiting confirmation",
                ),
                consent_ref=consent_ref,
            )
        if state is RsvpState.AMBIGUOUS:
            return await self._record_source_outcome(
                tenant_id,
                audit_context,
                source,
                modality,
                RegisterResult(RegisterOutcome.NEEDS_HANDOFF, "remote RSVP state is ambiguous"),
                consent_ref=consent_ref,
            )
        return None

    async def _registration_conflict_outcome(
        self, tenant_id: UUID, event: CanonicalEvent
    ) -> RegisterResult | None:
        """Re-check calendar truth before a source read or mutation (FR-4.5, FR-5.3)."""
        event_end = event.end_at or (event.start_at + _DEFAULT_DURATION)
        try:
            busy = await self._calendar.free_busy(tenant_id, event.start_at, event_end)
        except Exception as exc:
            _log.warning("calendar_conflict_check_failed", error=str(exc))
            return RegisterResult(
                RegisterOutcome.NEEDS_HANDOFF,
                "calendar conflict check unavailable; human handoff required",
            )
        if evaluate_conflict(event.start_at, event_end, busy) is ConflictVerdict.BLOCKED:
            return RegisterResult(
                RegisterOutcome.NEEDS_HANDOFF,
                "event hard-overlaps a calendar commitment; human handoff required",
            )
        return None

    async def _fresh_membership_lane_plan(
        self,
        tenant_id: UUID,
        event: CanonicalEvent,
        lane_plan: tuple[Lane, ...],
        *,
        queue_item_id: str,
    ) -> tuple[Lane, ...]:
        """Replace a stale Meetup lane hint with a fresh authenticated membership read.

        G2 is still open, so only an already-``MEMBER`` user may enter the autonomous Meetup lane;
        ``OPEN_INSTANT_JOIN`` deliberately remains handoff-only. A source-read failure removes the
        API lane rather than risking a mutation against an unknown membership state. Missing P14c
        evidence preserves the advisory lane only long enough for the separate policy activity to
        record its denied precheck; it still cannot issue a membership read (FR-2.9, FR-7.3).
        """
        target = self._target_for_lane(event, Lane.AUTONOMOUS_SLA)
        without_meetup = tuple(lane for lane in lane_plan if lane is not Lane.AUTONOMOUS_SLA)
        if target is None:
            return lane_plan
        source, modality, registration_target = target
        adapter = self._sources.get(source)
        if adapter is None:
            return without_meetup
        try:
            await self._require_registration_consent(tenant_id, source, modality)
        except RegistrationConsentDeniedError:
            # Keep the non-I/O advisory lane so Temporal's authoritative policy gate records the
            # explicit missing-evidence denial. That gate performs the same resolve before any
            # Pacer/browser/source boundary, so this cannot turn absence into a provider call.
            return lane_plan
        try:
            await self._require_source_policy(tenant_id, source, modality)
        except SourceQuarantinedError:
            raise
        except SourcePolicyDeniedError:
            # A current policy disable/kill switch makes no membership wire call. Preserve the
            # advisory lane plan so the separate workflow policy gate records its authoritative
            # precheck/audit denial before it selects an alternate lane or handoff (ADR-004,
            # FR-7.3). No provider request can pass this branch.
            return lane_plan
        pacer_request = await self._pacer_request(
            tenant_id,
            source,
            PacerOperation.MEMBERSHIP_READ,
            queue_item_id=queue_item_id,
        )
        await self._require_pacer_lease(pacer_request)
        try:
            # A source ban or kill switch can change while Pacer waits. Re-read after the final
            # admission so no subsequent membership request escapes the data-plane policy fence.
            await self._require_source_policy(tenant_id, source, modality)
            membership = await adapter.read_membership_state(tenant_id, registration_target)
        except SourceQuarantinedError:
            raise
        except SourcePolicyDeniedError:
            return lane_plan
        except SourceRateLimitedError as throttle:
            await self._defer_for_source_throttle(pacer_request, throttle)
        except SourceAccessDeniedError as denied:
            await self._quarantine_after_access_denied(source, denied)
        except Exception as exc:
            _log.warning("source_membership_read_failed", source=source.value, error=str(exc))
            return without_meetup
        if membership is not GroupCondition.MEMBER:
            return without_meetup
        return (Lane.AUTONOMOUS_SLA, *without_meetup)

    async def _require_source_policy(
        self, tenant_id: UUID, source: Source, modality: Modality
    ) -> None:
        """Re-read policy before every source read as well as every mutation (ADR-004).

        A post-quarantine activity retry reaches this guard before Pacer/browser admission or a
        provider request. That makes the raw ban/403's durable policy flip the recovery fence:
        retrying an activity cannot issue a second source call (FR-7.6, FR-10.1/10.3, AC-72).
        """
        decision = await self._policy.evaluate(
            PolicyContext(tenant_id=tenant_id, source=source, modality=modality, action="register")
        )
        if decision.allowed:
            return
        if decision.source_quarantined:
            raise SourceQuarantinedError(source, from_policy=True)
        raise SourcePolicyDeniedError(decision)

    async def _quarantine_after_access_denied(
        self, source: Source, denied: SourceAccessDeniedError
    ) -> NoReturn:
        """Trip the monotonic circuit breaker, then force this activity to handoff.

        A persistence outage cannot justify retrying the provider: it produces the same terminal
        no-wire-call outcome, while the fresh policy readers separately fail closed on a store
        outage. Only a closed ban/forbidden signal reaches this method; arbitrary adapter errors
        retain their existing bounded failure behavior (FR-7.6, FR-10.3, AC-72, ADR-004).
        """
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
        raise SourceQuarantinedError(source)

    async def _pacer_request(
        self,
        tenant_id: UUID,
        source: Source,
        operation: PacerOperation,
        *,
        cost: int = 1,
        queue_item_id: str | None = None,
    ) -> PacerRequest:
        """Build a typed ADR-005 request without exposing plaintext credential material.

        The mocked foundation may not yet have a stored credential record; its per-tenant/source
        vault slot is still a distinct credential scope, represented by the tenant fallback.
        """
        credential_scope = f"tenant:{tenant_id}"
        if self._vault is not None:
            credential = await self._vault.get(tenant_id, source)
            if credential is not None:
                credential_scope = str(credential.credential_id)
        return PacerRequest(
            source=source,
            quota_scope=credential_scope,
            operation=operation,
            cost=cost,
            tenant_id=tenant_id,
            queue_item_id=queue_item_id,
        )

    async def _paced_registration_state(
        self,
        tenant_id: UUID,
        source: Source,
        adapter: SourcePort,
        target: RegistrationTarget,
        modality: Modality,
        operation: PacerOperation,
        *,
        queue_item_id: str,
        consent_ref: UUID | None = None,
    ) -> RsvpState:
        """Run one consent-gated source read under one Pacer decision (FR-2.9, ADR-004/005)."""
        await self._require_registration_consent(tenant_id, source, modality, consent_ref)
        await self._require_source_policy(tenant_id, source, modality)
        pacer_request = await self._pacer_request(
            tenant_id,
            source,
            operation,
            queue_item_id=queue_item_id,
        )
        browser_lease = await self._acquire_browser_admission(
            tenant_id, source, modality, queue_item_id
        )
        try:
            # The held browser slot and rate permit are deliberately distinct: one enforces
            # physical browser capacity, the other provider quota.  Both are acquired immediately
            # before the source call and a failed rate acquisition releases the slot (AC-45).
            await self._require_pacer_lease(pacer_request)
            # The initial read guard avoids needless admission. This second guard is the
            # authority boundary: it is after any wait and directly before provider I/O.
            await self._require_source_policy(tenant_id, source, modality)
            try:
                return await adapter.read_registration_state(tenant_id, target, modality)
            except SourceRateLimitedError as throttle:
                await self._defer_for_source_throttle(pacer_request, throttle)
            except SourceAccessDeniedError as denied:
                await self._quarantine_after_access_denied(source, denied)
        finally:
            await self._release_browser_admission(browser_lease)

    async def _acquire_browser_admission(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        lease_id: str,
    ) -> BrowserAdmissionLease | None:
        """Lease a browser slot before every browser-backed source call (AC-45, ADR-005).

        The durable workflow supplies ``lease_id`` once.  The short-lived ULID is a generation
        fence for this physical holder, so cleanup from an expired/retried activity cannot free a
        later browser session that reuses the durable identity.
        """
        if modality is not Modality.BROWSER:
            return None
        admission = self._browser_admission
        if admission is None:
            raise PacerDeferredError(
                PacerLease(
                    PacerLeaseStatus.SATURATED,
                    1.0,
                    "browser admission is unavailable",
                )
            )
        try:
            lease = await admission.acquire(
                BrowserAdmissionRequest(
                    tenant_id=tenant_id,
                    source=source,
                    lease_id=lease_id,
                    fence_token=new_ulid(),
                )
            )
        except Exception as exc:
            # The distinct adapter itself is fail-closed, but retain that invariant for injected
            # ports as well.  A browser call after an admission-plane outage would violate NFR-15.
            _log.warning("browser_admission_failed", source=source.value, error=str(exc))
            raise PacerDeferredError(
                PacerLease(PacerLeaseStatus.SATURATED, 1.0, "browser admission unavailable")
            ) from exc
        if lease.granted:
            return lease
        raise PacerDeferredError(
            PacerLease(
                PacerLeaseStatus.SATURATED,
                max(lease.retry_after_seconds, 0.001),
                lease.detail or "browser pool is saturated",
            )
        )

    async def _release_browser_admission(self, lease: BrowserAdmissionLease | None) -> None:
        """Best-effort fenced cleanup that never changes a completed source effect into a retry."""
        if lease is None:
            return
        admission = self._browser_admission
        if admission is None:
            return
        try:
            await admission.release(lease)
        except Exception as exc:
            # Its TTL/recovery fence preserves the safety boundary.  Propagating this after a
            # source response would make Temporal retry a completed browser interaction.
            _log.warning("browser_admission_release_failed", error=str(exc))

    async def _require_pacer_lease(self, request: PacerRequest) -> None:
        """Convert a non-blocking Pacer decision into workflow-owned pacing control flow."""
        lease = await self._pacer.acquire(request)
        if not lease.granted:
            raise PacerDeferredError(lease)

    async def _defer_for_source_throttle(
        self, request: PacerRequest, throttle: SourceRateLimitedError
    ) -> NoReturn:
        """Persist a normalized 429/reset signal, then return a workflow-owned durable wait.

        The post-recording acquisition intentionally proves the shared bucket has the block. If
        Redis fails in between, its own throttle-first failure result still prevents a blind retry.
        """
        await self._pacer.observe_backoff(
            request,
            retry_after_seconds=throttle.retry_after_seconds,
            reset_at=throttle.reset_at,
        )
        lease = await self._pacer.acquire(request)
        if lease.granted:
            # A malformed/past provider hint must never turn an observed throttle into a retry
            # loop. A one-second durable floor is conservative and keeps the source untouched.
            lease = PacerLease(PacerLeaseStatus.WAIT, 1.0, str(throttle))
        raise PacerDeferredError(lease)


def _permanent_source_verification_detail(exc: Exception) -> str:
    """Render a closed, non-sensitive review reason for permanent source guard outcomes."""
    if isinstance(exc, RegistrationConsentDeniedError):
        return "registration consent is no longer available"
    if isinstance(exc, SourceQuarantinedError):
        return "registration source is quarantined"
    if isinstance(exc, SourceReconsentRequiredError):
        return "registration source authorization requires re-consent"
    if isinstance(exc, SourcePolicyDeniedError):
        return "source automation policy no longer permits verification"
    raise TypeError("unsupported permanent source verification outcome")


def _normalized_public_base_url(value: str, *, require_https: bool) -> str:
    """Validate the deployment-owned origin before placing a capability in a notification."""
    if not isinstance(value, str):
        raise TypeError("handoff completion base URL must be a string")
    if not value:
        if require_https:
            raise ValueError("non-mock handoff completion links require a public HTTPS base URL")
        return ""
    if len(value) > _MAX_PUBLIC_BASE_URL_LENGTH or any(
        character.isspace() or category(character) in {"Cc", "Cf"} for character in value
    ):
        raise ValueError("handoff completion base URL must be a bounded absolute URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("handoff completion base URL must be a bounded absolute URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= _MAX_URL_PORT)
    ):
        raise ValueError("handoff completion base URL must be a bounded absolute URL")
    if require_https and parsed.scheme != "https":
        raise ValueError("non-mock handoff completion links require a public HTTPS base URL")
    return value.rstrip("/")


def _iana_time_zone(moment: datetime) -> str:
    """Keep an IANA zone when supplied; normalize offset-only source timestamps to UTC (FR-9.5)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("calendar event timestamps must be timezone-aware")
    zone_name = getattr(moment.tzinfo, "key", None)
    candidate = zone_name if isinstance(zone_name, str) and zone_name else "UTC"
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"calendar event timezone is not an IANA zone: {candidate}") from error
    return candidate
