"""Narrow RSVP-withdrawal boundary.

Discovery and registration surfaces must not be forced to implement a destructive operation just
because they can list events.  This port therefore carries only the read-before-withdraw contract
used by the long-lived lifecycle workflow (FR-8.8, ADR-003/005).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ..domain.enums import Modality, RsvpState
from .sources import RegistrationTarget


class WithdrawalOutcome(StrEnum):
    """The normalized result of one idempotent RSVP withdrawal attempt (FR-8.8)."""

    WITHDRAWN = "withdrawn"
    NO_OP_ALREADY_ABSENT = "no_op_already_absent"
    NEEDS_HANDOFF = "needs_handoff"


@dataclass(frozen=True, slots=True)
class WithdrawalResult:
    """Secret-free outcome returned after a source-specific withdrawal attempt."""

    outcome: WithdrawalOutcome
    detail: str = ""
    deep_link: str | None = None


class RegistrationWithdrawalPort(Protocol):
    """Read an RSVP immediately before a replay-safe withdrawal mutation (FR-8.8, ADR-003).

    Implementations may represent API, browser, or a fixture-backed interaction.  The application
    acquires an ADR-005 Pacer lease before *each* method and treats ``NOT_PRESENT`` as convergence,
    so a crash after a provider accepted the withdrawal cannot create a second source effect. A
    provider ban/403 raises ``SourceAccessDeniedError`` from ``ports.sources`` so the caller trips
    the durable source quarantine and routes the destructive action to handoff (FR-10.3, AC-72).
    """

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        """Return the current remote RSVP state before any withdrawal wire call."""
        ...

    async def withdraw(
        self,
        tenant_id: UUID,
        target: RegistrationTarget,
        modality: Modality,
        idempotency_key: str,
    ) -> WithdrawalResult:
        """Withdraw once, or return an explicit safe handoff outcome (FR-8.8)."""
        ...
