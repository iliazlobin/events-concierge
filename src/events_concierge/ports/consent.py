"""Opaque registration-consent evidence boundary (FR-2.9, FR-7.3, ADR-004).

This is deliberately a validation-only port.  Grant capture is not yet trustworthy without the
owner-gated OAuth/account-link flow, so application code cannot create, replace, revoke, or infer
consent through it.  It can only resolve and revalidate an opaque existing reference immediately
before a registered event-source action.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from ..domain.enums import ConsentScope, Modality, Source

_SUPPORTED_REGISTRATION_CONSENT_TARGETS = frozenset(
    {
        (Source.MEETUP, Modality.API),
        (Source.LUMA, Modality.BROWSER),
    }
)


def validate_registration_consent_target(
    source: Source,
    modality: Modality,
    scope: ConsentScope,
) -> None:
    """Reject unratified P14c consent tuples before any adapter can represent them (FR-2.9)."""
    if scope is not ConsentScope.REGISTRATION:
        raise ValueError("registration consent supports only the registration scope")
    if (source, modality) not in _SUPPORTED_REGISTRATION_CONSENT_TARGETS:
        raise ValueError("registration consent source/modality is not supported")


class RegistrationConsentEvidencePort(Protocol):
    """Resolve/revalidate owner-seeded consent for one supported registration lane (FR-2.9)."""

    async def resolve(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        scope: ConsentScope = ConsentScope.REGISTRATION,
    ) -> UUID | None:
        """Return only a matching opaque evidence UUID, or None on absence/unavailability."""
        ...

    async def validate(
        self,
        consent_ref: UUID,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        scope: ConsentScope = ConsentScope.REGISTRATION,
    ) -> bool:
        """Recheck one exact opaque reference at the last pre-source boundary."""
        ...
