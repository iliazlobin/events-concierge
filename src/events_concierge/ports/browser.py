"""Narrow browser RSVP boundary for Luma's best-effort lane (FR-5.4/5.5, ADR-006).

The browser worker may inspect a page, but it returns only these typed observations.  Raw DOM,
screenshots, credentials, and page text are intentionally absent from the port so they cannot
reach registration decisions or workflow history.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from .sources import RegistrationTarget


class BrowserRsvpStatus(StrEnum):
    """Closed outcomes from the browser worker's read-only RSVP detection."""

    CONFIRMED = "confirmed"
    PENDING_CONFIRMATION = "pending_confirmation"
    NOT_PRESENT = "not_present"
    AMBIGUOUS = "ambiguous"
    LOGIN_REQUIRED = "login_required"
    PAYWALL = "paywall"
    CAPTCHA = "captcha"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BrowserRsvpObservation:
    """Sanitized, schema-validated browser state used by the deterministic action gate.

    ``price_cents`` is set only when the worker can type the visible RSVP price.  The Luma adapter
    requires it to equal zero before submitting, implementing the FR-5.10 free-only data-plane
    guard without forwarding page content.
    """

    status: BrowserRsvpStatus
    price_cents: int | None = None

    def __post_init__(self) -> None:
        if self.price_cents is not None and (
            isinstance(self.price_cents, bool)
            or not isinstance(self.price_cents, int)
            or self.price_cents < 0
        ):
            raise ValueError("browser RSVP price_cents must be a non-negative integer or None")


class BrowserRsvpPort(Protocol):
    """The read-before-submit surface used by browser-only source adapters (FR-5.5)."""

    async def detect(self, tenant_id: UUID, target: RegistrationTarget) -> BrowserRsvpObservation:
        """Read the current RSVP marker without clicking a registration control."""
        ...

    async def submit(
        self, tenant_id: UUID, target: RegistrationTarget, idempotency_key: str
    ) -> BrowserRsvpObservation:
        """Submit only after the adapter's fresh ``NOT_PRESENT`` and zero-price action gate."""
        ...
