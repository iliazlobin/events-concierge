"""Browser-pool admission boundary (FR-6.4/NFR-4b, ADR-005).

Browser concurrency is not a rate token: it is a held, fenced capability that must be released
after the browser-facing source call. The port is intentionally separate from the one-shot Pacer
rate lease so a late activity cleanup cannot erase a newer browser session's capacity record.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..domain.enums import Source


@dataclass(frozen=True, slots=True)
class BrowserAdmissionRequest:
    """One physical browser-call admission request with a stable workflow lease ID (ADR-003/005)."""

    tenant_id: UUID
    source: Source
    lease_id: str
    fence_token: str

    def __post_init__(self) -> None:
        if not self.lease_id:
            raise ValueError("browser admission lease_id must not be empty")
        if not self.fence_token:
            raise ValueError("browser admission fence_token must not be empty")


@dataclass(frozen=True, slots=True)
class BrowserAdmissionLease:
    """A fenced browser slot result; saturated leases never authorize a browser call (AC-45)."""

    granted: bool
    lease_id: str | None = None
    fence_token: str | None = None
    retry_after_seconds: float = 0.0
    detail: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.retry_after_seconds) or self.retry_after_seconds < 0.0:
            raise ValueError(
                "browser admission retry_after_seconds must be finite and non-negative"
            )
        if self.granted:
            if not self.lease_id or not self.fence_token:
                raise ValueError("a granted browser admission requires lease_id and fence_token")
            if self.retry_after_seconds != 0.0:
                raise ValueError("a granted browser admission cannot carry a retry delay")
        elif self.retry_after_seconds <= 0.0:
            raise ValueError("a saturated browser admission requires a positive retry projection")


class BrowserAdmissionPort(Protocol):
    """Atomically lease/release the globally shared browser pool (FR-6.4, NFR-4b)."""

    async def acquire(self, request: BrowserAdmissionRequest) -> BrowserAdmissionLease:
        """Return a slot or a synchronous saturation result; never queue an activity worker."""
        ...

    async def release(self, lease: BrowserAdmissionLease) -> None:
        """Release only a matching fenced lease; repeats and stale releases are harmless."""
        ...
