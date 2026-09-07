"""Tenant-wide authority for externally visible mutations during account erasure.

Database write triggers fence transactional state, but an HTTP, browser, calendar, or object-store
mutation cannot share a transaction with PostgreSQL.  This port provides that missing ordering
boundary: implementations serialize one tenant's external mutation with the durable erasure
tombstone and retain authority until the already-started operation has actually settled.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar
from uuid import UUID

_MIN_EFFECT_TIMEOUT_SECONDS = 0.1
_MAX_EFFECT_TIMEOUT_SECONDS = 60.0
_MIN_LOCK_TIMEOUT_SECONDS = 0.1
_MAX_LOCK_TIMEOUT_SECONDS = 30.0

T = TypeVar("T")


class TenantEffectKind(StrEnum):
    """Closed vocabulary for tenant-scoped external mutation families."""

    REGISTRATION = "registration"
    WITHDRAWAL = "withdrawal"
    CALENDAR_UPSERT = "calendar_upsert"
    CALENDAR_DELETE = "calendar_delete"
    CALENDAR_WATCH = "calendar_watch"
    CLAIM_CHECK_WRITE = "claim_check_write"
    CREDENTIAL_STORE = "credential_store"
    CREDENTIAL_REVOKE = "credential_revoke"
    ERASURE_DRAIN = "erasure_drain"
    NOTIFICATION = "notification"
    PROFILE_MEDIA_WRITE = "profile_media_write"
    TEMPORAL_START = "temporal_start"


class TenantEffectMode(StrEnum):
    """Whether an effect is ordinary product work or explicit post-fence cleanup."""

    LIVE = "live"
    ERASURE_CLEANUP = "erasure_cleanup"


@dataclass(frozen=True, slots=True)
class TenantEffectRequest:
    """One bounded authorization request without provider or user payload data."""

    tenant_id: UUID
    kind: TenantEffectKind
    timeout_seconds: float
    mode: TenantEffectMode = TenantEffectMode.LIVE

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, UUID):
            raise TypeError("tenant effect tenant_id must be a UUID")
        _finite_timeout(
            self.timeout_seconds,
            label="tenant effect timeout_seconds",
            minimum=_MIN_EFFECT_TIMEOUT_SECONDS,
            maximum=_MAX_EFFECT_TIMEOUT_SECONDS,
        )


class TenantEffectFencedError(RuntimeError):
    """The tenant has an erasure tombstone, so an ordinary mutation cannot start."""


class TenantEffectCleanupNotAuthorizedError(RuntimeError):
    """Cleanup mode was requested before the durable erasure fence existed."""


class TenantEffectLockTimeoutError(TimeoutError):
    """The database could not acquire the tenant erasure lock within its finite deadline."""


class TenantEffectTimedOutError(TimeoutError):
    """The effect exceeded its deadline; it was drained before this error became visible."""


class TenantEffectNestingError(RuntimeError):
    """A nested effect does not exactly match its still-active outer lock capability."""


class TenantEffectAuthority(Protocol):
    """Run an external mutation only while holding tenant-wide erasure authority."""

    async def run(
        self,
        request: TenantEffectRequest,
        effect: Callable[[], Awaitable[T]],
    ) -> T:
        """Authorize, start, and cancellation-safely settle exactly one effect."""
        ...


@dataclass(frozen=True, slots=True)
class TenantEffectAuthorityConfig:
    """Finite database-lock deadline shared by the concrete authority."""

    lock_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        _finite_timeout(
            self.lock_timeout_seconds,
            label="tenant effect lock_timeout_seconds",
            minimum=_MIN_LOCK_TIMEOUT_SECONDS,
            maximum=_MAX_LOCK_TIMEOUT_SECONDS,
        )


def _finite_timeout(value: float, *, label: str, minimum: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(f"{label} must be finite and between {minimum:g} and {maximum:g}")
