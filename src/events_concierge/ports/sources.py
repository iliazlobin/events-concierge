"""SourcePort: one discover() + register() per source, with deterministic capability flags.

Adapter selection (API preferred, browser fallback) is a capability decision, never a model choice
(FR-3.1). Free-crawl sources implement discover() and route register() to handoff."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ..domain.enums import GroupCondition, Modality, RsvpState, Source
from ..domain.events import CandidateEvent
from ..domain.policy import SourceQuarantineSignal
from ..domain.request import RequestConstraints


@dataclass(frozen=True, slots=True)
class SourceCapability:
    source: Source
    supports_api: bool
    supports_browser_discovery: bool
    supports_autonomous_register: bool


@dataclass(frozen=True, slots=True)
class RegistrationTarget:
    """The retained source-specific surface used for a registration attempt.

    A canonical event can retain several ``EventSourceLink`` values after deduplication.  The
    source adapter needs both the stable source identifier and the original registration URL:
    Meetup keys GraphQL calls by ID, while Luma's browser detect/submit flow must navigate the
    exact retained URL (FR-3.8, FR-5.3/5.5).
    """

    source_event_id: str
    registration_url: str


class RegisterOutcome(StrEnum):
    CONFIRMED = "confirmed"
    PENDING_CONFIRMATION = "pending_confirmation"
    FAILED = "failed"
    NEEDS_HANDOFF = "needs_handoff"
    PAYWALL = "paywall"
    NEEDS_REAUTH = "needs_reauth"
    NO_OP_ALREADY_CONFIRMED = "no_op_already_confirmed"
    SOURCE_QUARANTINED = "source_quarantined"


class SourceRateLimitedError(Exception):
    """A normalized provider throttle signal for the shared Pacer (FR-10.4, AC-73).

    Adapters raise this instead of hiding a usable ``Retry-After`` or reset timestamp inside an
    opaque HTTP error. The application records it through ``Pacer.observe_backoff`` and returns a
    workflow-owned durable wait; no adapter needs to import a concrete Pacer implementation.
    """

    def __init__(
        self,
        detail: str,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        if retry_after_seconds is None and reset_at is None:
            raise ValueError("a source throttle requires retry_after_seconds or reset_at")
        if retry_after_seconds is not None and (
            not math.isfinite(retry_after_seconds) or retry_after_seconds < 0.0
        ):
            raise ValueError("source throttle retry_after_seconds must be finite and non-negative")
        if reset_at is not None and (reset_at.tzinfo is None or reset_at.utcoffset() is None):
            raise ValueError("source throttle reset_at must be timezone-aware")
        self.retry_after_seconds = retry_after_seconds
        self.reset_at = reset_at
        super().__init__(detail)


class SourceTransientError(Exception):
    """A retryable source transport failure that must not become terminal operator work."""

    def __init__(self, detail: str, *, retry_after_seconds: float) -> None:
        if not math.isfinite(retry_after_seconds) or retry_after_seconds <= 0.0:
            raise ValueError("source transient retry_after_seconds must be finite and positive")
        self.retry_after_seconds = retry_after_seconds
        super().__init__(detail)


class SourceAccessDeniedError(Exception):
    """A normalized ban/403 signal that must trip the durable source circuit breaker.

    Adapters expose only the closed signal rather than provider response text, cookies, request
    URLs, or other sensitive material. The caller persists the monotonic quarantine before
    returning an immediate human-handoff outcome; it must never retry into the same source
    (FR-7.6, FR-10.3, AC-72, ADR-004).
    """

    def __init__(self, signal: SourceQuarantineSignal) -> None:
        self.signal = signal
        super().__init__(f"normalized source access denial: {signal.value}")


class SourceReconsentRequiredError(RuntimeError):
    """A tenant source credential cannot be used again until the user re-authorizes it."""


@dataclass(frozen=True, slots=True)
class RegisterResult:
    outcome: RegisterOutcome
    detail: str = ""
    deep_link: str | None = None


class SourcePort(Protocol):
    """A single external event surface. `modality` selects the adapter behind the port."""

    capability: SourceCapability

    async def discover(self, constraints: RequestConstraints) -> list[CandidateEvent]:
        """Return raw candidate events matching the constraints (before ACL/dedup)."""
        ...

    async def read_membership_state(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> GroupCondition:
        """Resolve a source-specific membership precondition before autonomous registration.

        Meetup implements the fresh member-group guard.  Sources without a membership concept
        return ``UNKNOWN``.  The application acquires the Pacer lease before this remote read and
        fails closed unless the result is ``MEMBER`` (FR-5.2, ADR-003/005). A provider throttle
        raises ``SourceRateLimitedError`` so the application can update the Pacer (AC-73); a ban
        or 403 raises ``SourceAccessDeniedError`` for durable quarantine (FR-10.3).
        """
        ...

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        """Read remote RSVP state before a retried mutation.

        API adapters implement the Meetup read-before-mutate guard; browser adapters implement
        Luma's detect-then-submit check. Callers must acquire the Pacer lease before this source
        call and treat ``CONFIRMED`` as a no-op (FR-5.3/5.5, ADR-003/005). A provider throttle
        raises ``SourceRateLimitedError`` for shared backoff bookkeeping (AC-73); a ban or 403
        raises ``SourceAccessDeniedError`` for durable quarantine (FR-10.3).
        """
        ...

    async def register(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality, idempotency_key: str
    ) -> RegisterResult:
        """Attempt to secure a place; throttle and ban/403 signals use the typed errors above."""
        ...
