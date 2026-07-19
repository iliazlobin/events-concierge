"""Declarative per-source policy and the decision object the money-/action-moving boundary evaluates.

Policy is DATA, never code (FR-10.1): flipping automation_allowed or quarantined changes behavior
without a deploy. The pre-mutate guard (ADR-004) evaluates a PolicyDecision immediately before every
mutating wire call."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .enums import Modality, Source


@dataclass(slots=True)
class SourcePolicy:
    source: Source
    automation_allowed: dict[Modality, bool] = field(default_factory=dict)
    paid_allowed: bool = False
    quarantined: bool = False
    signed_agent_mode: str = "none"  # none | present-if-honored | required (FR-17.3)

    def allows(self, modality: Modality) -> bool:
        return not self.quarantined and self.automation_allowed.get(modality, False)


class SourceQuarantineSignal(StrEnum):
    """Closed adapter evidence that may trip the source circuit breaker.

    The actuator deliberately accepts only a ban or an HTTP-equivalent forbidden signal.  Broader
    provider failures must not gain authority to freeze a source merely by being rendered as an
    arbitrary string (FR-10.3, FR-7.6, ADR-004).
    """

    BAN = "ban"
    FORBIDDEN = "forbidden"


class PolicyDecisionCode(StrEnum):
    """Closed reason code for an action-boundary policy decision (ADR-004).

    Display text is intentionally not a control-flow contract.  Activities use this code to
    recognize a durable source quarantine after a crash/retry without parsing a human-facing
    explanation, so they can hand off before issuing another source request (FR-10.3, AC-72).
    """

    ALLOWED = "allowed"
    KILL_SWITCH = "kill_switch"
    POLICY_STORE_UNAVAILABLE = "policy_store_unavailable"
    UNKNOWN_SOURCE = "unknown_source"
    SOURCE_QUARANTINED = "source_quarantined"
    MODALITY_DISABLED = "modality_disabled"
    PAID_NOT_ALLOWED = "paid_not_allowed"


@dataclass(frozen=True, slots=True)
class SourceQuarantineResult:
    """The durable result of one monotonic source-quarantine attempt.

    `newly_quarantined` is true only for the first false-to-true transition.  Replays retain the
    original quarantine and return false, so an adapter crash after the database commit cannot
    reopen the source or manufacture a second policy effect (FR-10.3, AC-72, ADR-004).
    """

    source: Source
    signal: SourceQuarantineSignal
    newly_quarantined: bool


@dataclass(frozen=True, slots=True)
class PolicyLimits:
    rsvps_per_period: int = 20
    concurrent_open_cap: int = 10  # FR-11.3, owner target


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str
    code: PolicyDecisionCode

    @classmethod
    def allow(cls) -> PolicyDecision:
        return cls(True, "ok", PolicyDecisionCode.ALLOWED)

    @classmethod
    def deny(
        cls, reason: str, code: PolicyDecisionCode = PolicyDecisionCode.POLICY_STORE_UNAVAILABLE
    ) -> PolicyDecision:
        """Create an explicit deny; callers must never infer control behavior from text."""
        return cls(False, reason, code)

    @property
    def source_quarantined(self) -> bool:
        """Whether a durable source circuit breaker—not a generic deny—blocked the call."""
        return self.code is PolicyDecisionCode.SOURCE_QUARANTINED


@dataclass(frozen=True, slots=True)
class PolicyControlState:
    """The global and tenant kill-switch state read at the action boundary.

    The control rows deliberately contain no user/profile or provider data.  A policy reader
    supplies this immutable snapshot to the deterministic PDP, which fails closed when it cannot
    obtain one (FR-5.9, FR-7.2, ADR-004).
    """

    global_kill_switch: bool = False
    tenant_kill_switch: bool = False

    @property
    def engaged(self) -> bool:
        """Whether either data-plane freeze is active (FR-7.2)."""
        return self.global_kill_switch or self.tenant_kill_switch


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """One fresh, PII-free source-policy and kill-switch read (ADR-004).

    ``source_policy`` is ``None`` for an unknown or absent source row.  The PDP treats that as a
    denial rather than synthesizing a default allow.
    """

    control: PolicyControlState
    source_policy: SourcePolicy | None
