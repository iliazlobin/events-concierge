"""Luma's fixture-backed browser SourcePort adapter (FR-5.4/5.5, ADR-003/006).

Luma is browser best-effort and off the reliability SLA.  This adapter never performs a live
browser or network call itself; it translates a narrow, typed browser port into SourcePort state
and enforces detect-then-submit at the adapter boundary.
"""

from __future__ import annotations

from uuid import UUID

from ...domain.enums import GroupCondition, Modality, RsvpState, Source
from ...domain.events import CandidateEvent
from ...domain.request import RequestConstraints
from ...ports.browser import BrowserRsvpObservation, BrowserRsvpPort, BrowserRsvpStatus
from ...ports.policy import PolicyContext, PolicyEngine
from ...ports.sources import (
    RegisterOutcome,
    RegisterResult,
    RegistrationTarget,
    SourceCapability,
)

_PRE_SUBMIT_BLOCKS: dict[BrowserRsvpStatus, tuple[RegisterOutcome, str]] = {
    BrowserRsvpStatus.CONFIRMED: (
        RegisterOutcome.NO_OP_ALREADY_CONFIRMED,
        "Luma RSVP already confirmed by fresh browser detection",
    ),
    BrowserRsvpStatus.PENDING_CONFIRMATION: (
        RegisterOutcome.PENDING_CONFIRMATION,
        "Luma RSVP is awaiting confirmation",
    ),
    BrowserRsvpStatus.AMBIGUOUS: (
        RegisterOutcome.NEEDS_HANDOFF,
        "Luma RSVP state is ambiguous and requires human review",
    ),
    BrowserRsvpStatus.LOGIN_REQUIRED: (RegisterOutcome.NEEDS_REAUTH, "Luma login is required"),
    BrowserRsvpStatus.PAYWALL: (RegisterOutcome.PAYWALL, "Luma RSVP surfaced a payment wall"),
    BrowserRsvpStatus.CAPTCHA: (
        RegisterOutcome.NEEDS_HANDOFF,
        "Luma browser flow cannot safely continue",
    ),
    BrowserRsvpStatus.FAILED: (
        RegisterOutcome.NEEDS_HANDOFF,
        "Luma browser flow cannot safely continue",
    ),
}


class LumaSource:
    """Browser-only Luma adapter with a fresh detect immediately before every submit (FR-5.5).

    A real Browserbase/CDP driver can later satisfy ``BrowserRsvpPort``.  Until the owner completes
    G3, this scaffold is exercised only with the scripted fixture driver and introduces no login,
    credential, OTP, or live-browser behavior.
    """

    def __init__(
        self,
        browser: BrowserRsvpPort,
        fixture_events: list[CandidateEvent] | None = None,
        *,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.capability = SourceCapability(
            source=Source.LUMA,
            supports_api=False,
            supports_browser_discovery=True,
            supports_autonomous_register=True,
        )
        self._browser = browser
        self._fixtures = list(fixture_events or [])
        self._policy = policy

    def bind_policy(self, policy: PolicyEngine) -> None:
        """Bind composition's data-plane policy guard before this fixture adapter can submit.

        ``register`` re-detects the page before submitting, so the application-level guard that
        precedes the SourcePort call is not sufficiently close to the internal browser submit.
        Composition binds the same policy engine here; a direct fixture construction without it
        remains fail-closed (FR-5.9, FR-7.2, ADR-004).
        """
        if self._policy is not None and self._policy is not policy:
            raise ValueError("Luma policy guard is already bound")
        self._policy = policy

    async def discover(self, constraints: RequestConstraints) -> list[CandidateEvent]:
        """Return supplied Luma fixtures; live per-user discovery is outside this scaffold."""
        return [
            event
            for event in self._fixtures
            if (constraints.time_window is None or constraints.time_window.contains(event.start_at))
            and constraints.accepts_price(event.price_status)
        ]

    async def read_membership_state(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> GroupCondition:
        """Luma has no group-membership precondition (FR-5.1)."""
        del tenant_id, target
        return GroupCondition.UNKNOWN

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        """Run read-only RSVP detection, never a browser submit (FR-5.5)."""
        self._require_browser_target(target, modality)
        observation = await self._browser.detect(tenant_id, target)
        return self._rsvp_state(observation)

    async def register(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality, idempotency_key: str
    ) -> RegisterResult:
        """Re-detect, require an explicitly free RSVP, then submit once at most (FR-5.5/5.10).

        ``BrowserAcknowledgementLostError`` and other driver faults intentionally propagate.  The
        ADR-003 workflow recovery loop invokes this adapter again with the same minted key, where a
        fresh detect can return ``CONFIRMED`` and prevent a blind second submit.
        """
        self._require_browser_target(target, modality)
        observation = await self._browser.detect(tenant_id, target)
        blocked = self._pre_submit_result(observation)
        if blocked is not None:
            return blocked

        policy_result = await self._pre_submit_policy_result(tenant_id)
        if policy_result is not None:
            return policy_result
        submitted = await self._browser.submit(tenant_id, target, idempotency_key)
        return self._post_submit_result(submitted)

    async def _pre_submit_policy_result(self, tenant_id: UUID) -> RegisterResult | None:
        """Re-read policy after detect and immediately before the browser submit (ADR-004).

        A data-plane change during a browser detect must not permit the later click. The adapter
        deliberately fails closed when composition has not bound its policy engine, keeping the
        fixture-only surface unable to submit outside the established application graph.
        """
        policy = self._policy
        if policy is None:
            return RegisterResult(
                RegisterOutcome.NEEDS_HANDOFF,
                "Luma pre-submit policy guard is unavailable",
            )
        try:
            decision = await policy.evaluate(
                PolicyContext(
                    tenant_id=tenant_id,
                    source=Source.LUMA,
                    modality=Modality.BROWSER,
                    action="register",
                )
            )
        except Exception:
            return RegisterResult(
                RegisterOutcome.NEEDS_HANDOFF,
                "Luma pre-submit policy guard is unavailable",
            )
        if decision.allowed:
            return None
        return RegisterResult(
            (
                RegisterOutcome.SOURCE_QUARANTINED
                if decision.source_quarantined
                else RegisterOutcome.NEEDS_HANDOFF
            ),
            decision.reason,
        )

    @staticmethod
    def _require_browser_target(target: RegistrationTarget, modality: Modality) -> None:
        if modality is not Modality.BROWSER:
            raise ValueError("Luma registration is available only through the browser modality")
        if not target.source_event_id or not target.registration_url:
            raise ValueError("Luma registration target must include source event ID and URL")

    @staticmethod
    def _rsvp_state(observation: BrowserRsvpObservation) -> RsvpState:
        if observation.status is BrowserRsvpStatus.CONFIRMED:
            return RsvpState.CONFIRMED
        if observation.status is BrowserRsvpStatus.PENDING_CONFIRMATION:
            return RsvpState.PENDING_CONFIRMATION
        if observation.status is BrowserRsvpStatus.NOT_PRESENT:
            return RsvpState.NOT_PRESENT
        return RsvpState.AMBIGUOUS

    @staticmethod
    def _pre_submit_result(observation: BrowserRsvpObservation) -> RegisterResult | None:
        if observation.status is BrowserRsvpStatus.NOT_PRESENT and observation.price_cents == 0:
            return None
        if observation.status is BrowserRsvpStatus.NOT_PRESENT and observation.price_cents is None:
            return RegisterResult(
                RegisterOutcome.NEEDS_HANDOFF,
                "Luma RSVP price could not be verified as free",
            )
        if observation.status is BrowserRsvpStatus.NOT_PRESENT:
            return RegisterResult(RegisterOutcome.PAYWALL, "Luma RSVP price is not free")
        outcome, detail = _PRE_SUBMIT_BLOCKS[observation.status]
        return RegisterResult(outcome, detail)

    @classmethod
    def _post_submit_result(cls, observation: BrowserRsvpObservation) -> RegisterResult:
        if observation.status is BrowserRsvpStatus.CONFIRMED:
            return RegisterResult(RegisterOutcome.CONFIRMED, "Luma RSVP confirmed")
        if observation.status is BrowserRsvpStatus.PENDING_CONFIRMATION:
            return RegisterResult(
                RegisterOutcome.PENDING_CONFIRMATION,
                "Luma RSVP submitted and awaits confirmation",
            )
        if observation.status is BrowserRsvpStatus.NOT_PRESENT:
            return RegisterResult(
                RegisterOutcome.NEEDS_HANDOFF,
                "Luma submit did not yield a confirmed RSVP marker",
            )
        blocked = cls._pre_submit_result(observation)
        if blocked is None:
            raise AssertionError(
                "a post-submit browser observation must resolve to a terminal outcome"
            )
        return blocked
