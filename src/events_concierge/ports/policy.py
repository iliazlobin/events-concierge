"""Policy + pacing ports. The policy engine is evaluated at the action-moving boundary and re-read
by the pre-mutate guard (ADR-004); the pacer is an off-engine fair-share limiter keyed
(source, credential) (ADR-005)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ..domain.enums import Modality, Source
from ..domain.policy import (
    PolicyControlState,
    PolicyDecision,
    PolicySnapshot,
    SourceQuarantineResult,
    SourceQuarantineSignal,
)


@dataclass(frozen=True, slots=True)
class PolicyContext:
    tenant_id: UUID
    source: Source
    modality: Modality
    action: str  # "register" | "discover" | "withdraw"


class PolicyEngine(Protocol):
    async def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        """Allow or deny at the boundary: automation_allowed, per-user limits, paid_allowed,
        and the kill-switch. Evaluation error or store outage fails CLOSED (deny)."""
        ...

    async def kill_switch_engaged(self, tenant_id: UUID | None = None) -> bool:
        """Global or per-user data-plane freeze (FR-7.2). Engaged -> no registration proceeds."""
        ...


class PolicySnapshotReader(Protocol):
    """Read a fresh declarative policy snapshot for the in-process PDP (ADR-004).

    This narrow port makes PostgreSQL the durable control plane while preserving a deterministic
    offline reader for tests.  Readers must raise on store failure; the PDP, not an adapter,
    owns the fail-closed decision.
    """

    async def read_snapshot(self, tenant_id: UUID, source: Source) -> PolicySnapshot:
        """Return the source policy plus global/per-tenant control state."""
        ...

    async def read_control(self, tenant_id: UUID | None = None) -> PolicyControlState:
        """Return global control plus the supplied tenant's optional local freeze."""
        ...


class SourceQuarantinePort(Protocol):
    """Trip only the durable source-ban circuit breaker (FR-10.3, AC-72, ADR-004).

    Implementations receive no policy map, kill-switch, tenant, or clear operation.  A caller can
    only submit a normalized ban/forbidden signal for a closed `Source` and must fail safe to
    handoff if the durable write cannot be acknowledged.
    """

    async def quarantine(
        self, source: Source, signal: SourceQuarantineSignal
    ) -> SourceQuarantineResult:
        """Atomically set `quarantined` once; an exact retry returns an idempotent receipt."""
        ...


class PacerLeaseStatus(StrEnum):
    """The non-blocking outcome of one ADR-005 Pacer acquisition attempt."""

    GRANTED = "granted"
    WAIT = "wait"
    DEGRADE = "degrade"
    SATURATED = "saturated"


class PacerOperation(StrEnum):
    """The external operation guarded by a Pacer decision (ADR-005, AC-73)."""

    MEMBERSHIP_READ = "membership_read"
    REGISTRATION_READ = "registration_read"
    REGISTRATION_MUTATION = "registration_mutation"
    WITHDRAWAL_READ = "withdrawal_read"
    WITHDRAWAL_MUTATION = "withdrawal_mutation"
    CONFIRMATION_READ = "confirmation_read"
    CATALOG_REFRESH = "catalog_refresh"
    CATALOG_HTTP_GET = "catalog_http_get"
    BROWSER_ADMISSION = "browser_admission"


@dataclass(frozen=True, slots=True)
class PacerRequest:
    """Typed, secret-free input to the shared Pacer.

    ``quota_scope`` is an opaque credential, calendar, or app identifier—not a token. ``tenant_id``
    and ``queue_item_id`` are carried now for P1d's future per-app fair-share lanes; the baseline
    credential-scoped buckets use only the source/scope key (ADR-005). A catalog HTTP GET carries
    its registry-ratified minimum inter-request interval so Pacer can use a one-token bucket for
    that individual source request.
    """

    source: Source
    quota_scope: str
    operation: PacerOperation
    cost: int = 1
    tenant_id: UUID | None = None
    queue_item_id: str | None = None
    catalog_min_interval_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.quota_scope:
            raise ValueError("Pacer quota_scope must not be empty")
        if self.cost <= 0:
            raise ValueError("Pacer cost must be positive")
        if self.operation is PacerOperation.CATALOG_HTTP_GET:
            if self.catalog_min_interval_ms is None:
                raise ValueError("catalog HTTP GET Pacer requests require catalog_min_interval_ms")
            if (
                isinstance(self.catalog_min_interval_ms, bool)
                or not isinstance(self.catalog_min_interval_ms, int)
                or self.catalog_min_interval_ms <= 0
            ):
                raise ValueError("catalog_min_interval_ms must be a positive integer")
            try:
                catalog_rate_per_sec = 1_000.0 / self.catalog_min_interval_ms
            except OverflowError as exc:
                raise ValueError(
                    "catalog_min_interval_ms must yield a finite positive pacing rate"
                ) from exc
            if not math.isfinite(catalog_rate_per_sec) or catalog_rate_per_sec <= 0.0:
                raise ValueError("catalog_min_interval_ms must yield a finite positive pacing rate")
        elif self.catalog_min_interval_ms is not None:
            raise ValueError("catalog_min_interval_ms is valid only for catalog HTTP GET requests")

    @property
    def bucket_key(self) -> str:
        """Return the opaque `(source, quota scope)` bucket identity (ADR-005)."""
        return f"{self.source.value}:{self.quota_scope}"


@dataclass(frozen=True, slots=True)
class PacerBudget:
    """One source's rate/burst shape; durable daily budgets belong to P1c (ADR-002)."""

    rate_per_sec: float
    burst: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.rate_per_sec) or self.rate_per_sec <= 0.0:
            raise ValueError("Pacer budget rate_per_sec must be finite and positive")
        if self.burst <= 0:
            raise ValueError("Pacer budget burst must be positive")


@dataclass(frozen=True, slots=True)
class PacerLease:
    """A disposable Pacer decision, never a reservation across a workflow timer.

    A ``WAIT`` result is deliberately advisory: a workflow sleeps durably, then acquires again
    before any wire call. ``DEGRADE`` and ``SATURATED`` let a future fair-share/browser-cap
    implementation route safely to handoff rather than queue a worker (ADR-005, AC-45/73).
    """

    status: PacerLeaseStatus
    retry_after_seconds: float = 0.0
    detail: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.retry_after_seconds) or self.retry_after_seconds < 0.0:
            raise ValueError("Pacer lease retry_after_seconds must be finite and non-negative")
        if self.status is not PacerLeaseStatus.GRANTED and self.retry_after_seconds <= 0.0:
            raise ValueError("a non-granted Pacer lease requires a positive retry projection")

    @property
    def granted(self) -> bool:
        """Whether this acquisition consumed a source-budget token."""
        return self.status is PacerLeaseStatus.GRANTED


class Pacer(Protocol):
    async def acquire(self, request: PacerRequest) -> PacerLease:
        """Try once to acquire a typed source budget without sleeping.

        Callers make no source request unless the result is ``GRANTED``. On ``WAIT`` they return
        the projection to Temporal, which owns the durable timer and re-acquires on wake; activity
        workers are never parked on a quota (ADR-003/ADR-005).
        """
        ...

    async def observe_backoff(
        self,
        request: PacerRequest,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        """Record a source-supplied throttle window for all workers sharing ``key`` (AC-73)."""
        ...
