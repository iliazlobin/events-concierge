"""Mock SourcePort adapters for the slice/tests: a source that confirms autonomously (stands in for
a Meetup member-group API RSVP) and returns fixture candidates. Not a production adapter."""

from __future__ import annotations

import asyncio
from uuid import UUID

from ...domain.enums import GroupCondition, Modality, RsvpState, Source
from ...domain.events import CandidateEvent
from ...domain.request import RequestConstraints
from ...ports.sources import (
    RegisterOutcome,
    RegisterResult,
    RegistrationTarget,
    SourceCapability,
)
from ...ports.withdrawal import WithdrawalOutcome, WithdrawalResult


class ConfirmingSource:
    """Discovers fixtures and models an idempotent RSVP surface for offline saga tests.

    ``raise_after_effect_once`` simulates a worker crash after the remote RSVP succeeded but before
    the activity acknowledgement. A workflow retry must read ``CONFIRMED`` and avoid a second
    mutation (NFR-8, ADR-003).
    """

    def __init__(
        self,
        source: Source,
        fixture_events: list[CandidateEvent] | None = None,
        *,
        raise_after_effect_once: bool = False,
        pending_after_effect_once: bool = False,
        pending_reads_before_confirmation: int | None = None,
        membership_state: GroupCondition | None = None,
        raise_after_withdraw_effect_once: bool = False,
        withdrawal_supported: bool = True,
        event_log: list[str] | None = None,
    ) -> None:
        self.capability = SourceCapability(
            source=source,
            supports_api=True,
            supports_browser_discovery=False,
            supports_autonomous_register=True,
        )
        self._source = source
        self._fixtures = fixture_events or []
        self._registered: set[tuple[UUID, str, Modality]] = set()
        self._pending: set[tuple[UUID, str, Modality]] = set()
        self._pending_reads_remaining: dict[tuple[UUID, str, Modality], int] = {}
        self._raise_after_effect_once = raise_after_effect_once
        self._raised_after_effect = False
        self._pending_after_effect_once = pending_after_effect_once
        self._pending_reads_before_confirmation = pending_reads_before_confirmation
        self._raised_after_pending_effect = False
        self._membership_state = membership_state or (
            GroupCondition.MEMBER if source is Source.MEETUP else GroupCondition.UNKNOWN
        )
        self._raise_after_withdraw_effect_once = raise_after_withdraw_effect_once
        self._raised_after_withdraw_effect = False
        self._withdrawal_supported = withdrawal_supported
        self._event_log = event_log
        self.register_attempts = 0
        self.registration_effects = 0
        self._registration_effect_observed = asyncio.Event()
        self.idempotency_keys: list[str] = []
        self.withdraw_attempts = 0
        self.withdrawal_effects = 0
        self.withdrawal_idempotency_keys: list[str] = []

    async def discover(self, constraints: RequestConstraints) -> list[CandidateEvent]:
        events = self._fixtures
        if constraints.time_window is not None:
            events = [e for e in events if constraints.time_window.contains(e.start_at)]
        return [event for event in events if constraints.accepts_price(event.price_status)]

    async def read_membership_state(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> GroupCondition:
        if self._event_log is not None:
            self._event_log.append(f"source:membership:{self._membership_state.value}")
        return self._membership_state

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        key = (tenant_id, target.source_event_id, modality)
        if key in self._registered:
            state = RsvpState.CONFIRMED
        elif key in self._pending:
            remaining = self._pending_reads_remaining.get(key)
            if remaining is not None and remaining <= 0:
                self._pending.remove(key)
                self._registered.add(key)
                state = RsvpState.CONFIRMED
            else:
                if remaining is not None:
                    self._pending_reads_remaining[key] = remaining - 1
                state = RsvpState.PENDING_CONFIRMATION
        else:
            state = RsvpState.NOT_PRESENT
        if self._event_log is not None:
            self._event_log.append(f"source:read:{state.value}")
        return state

    async def register(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality, idempotency_key: str
    ) -> RegisterResult:
        if self._event_log is not None:
            self._event_log.append("source:register")
        self.register_attempts += 1
        self.idempotency_keys.append(idempotency_key)
        key = (tenant_id, target.source_event_id, modality)
        if key in self._registered:
            return RegisterResult(outcome=RegisterOutcome.NO_OP_ALREADY_CONFIRMED)
        if key in self._pending:
            return RegisterResult(outcome=RegisterOutcome.PENDING_CONFIRMATION)
        if self._pending_after_effect_once:
            self._pending.add(key)
            if self._pending_reads_before_confirmation is not None:
                self._pending_reads_remaining[key] = self._pending_reads_before_confirmation
            self.registration_effects += 1
            self._registration_effect_observed.set()
            if not self._raised_after_pending_effect:
                self._raised_after_pending_effect = True
                raise RuntimeError("simulated pending RSVP acknowledgement loss")
            return RegisterResult(outcome=RegisterOutcome.PENDING_CONFIRMATION)
        self._registered.add(key)
        self.registration_effects += 1
        self._registration_effect_observed.set()
        if self._raise_after_effect_once and not self._raised_after_effect:
            self._raised_after_effect = True
            raise RuntimeError("simulated source acknowledgement loss")
        return RegisterResult(outcome=RegisterOutcome.CONFIRMED, detail="mock autonomous confirm")

    async def wait_for_registration_effect(self) -> None:
        """Wait for one fixture RSVP effect, including one whose activity ACK is intentionally lost.

        The quality harness uses this in-process seam to inject its opaque confirmation only after
        the mock provider effect exists. It is not a production synchronization mechanism
        (NFR-8, ADR-003).
        """
        await self._registration_effect_observed.wait()

    async def withdraw(
        self,
        tenant_id: UUID,
        target: RegistrationTarget,
        modality: Modality,
        idempotency_key: str,
    ) -> WithdrawalResult:
        """Fixture read-before-withdraw effect with an optional lost-ACK seam (FR-8.8, ADR-003)."""
        if self._event_log is not None:
            self._event_log.append("source:withdraw")
        self.withdraw_attempts += 1
        self.withdrawal_idempotency_keys.append(idempotency_key)
        if not self._withdrawal_supported:
            return WithdrawalResult(
                WithdrawalOutcome.NEEDS_HANDOFF,
                "fixture source does not support autonomous withdrawal",
                target.registration_url,
            )
        key = (tenant_id, target.source_event_id, modality)
        if key not in self._registered and key not in self._pending:
            return WithdrawalResult(WithdrawalOutcome.NO_OP_ALREADY_ABSENT)
        self._registered.discard(key)
        self._pending.discard(key)
        self._pending_reads_remaining.pop(key, None)
        self.withdrawal_effects += 1
        if self._raise_after_withdraw_effect_once and not self._raised_after_withdraw_effect:
            self._raised_after_withdraw_effect = True
            raise RuntimeError("simulated withdrawal acknowledgement loss")
        return WithdrawalResult(WithdrawalOutcome.WITHDRAWN, "mock RSVP withdrawn")

    def confirm_pending(self, tenant_id: UUID, source_event_id: str, modality: Modality) -> None:
        """Test seam that makes a previously submitted RSVP visible as confirmed remotely."""
        key = (tenant_id, source_event_id, modality)
        self._pending.discard(key)
        self._pending_reads_remaining.pop(key, None)
        self._registered.add(key)
