"""Explicit-seed registration-consent evidence double (FR-2.9, FR-7.3, ADR-004).

The mock intentionally has no permissive default and no application-facing grant method.  Tests
must seed a specific opaque reference for a tenant/source/modality tuple before an autonomous
fixture source can run, mirroring the owner-seeded PostgreSQL control plane.
"""

from __future__ import annotations

from uuid import UUID

from ...domain.enums import ConsentScope, Modality, Source
from ...ports.consent import (
    RegistrationConsentEvidencePort,
    validate_registration_consent_target,
)


class MockRegistrationConsentEvidence(RegistrationConsentEvidencePort):
    """In-memory exact-match evidence registry for offline registration fixtures (FR-2.9)."""

    def __init__(self) -> None:
        self._records: dict[tuple[UUID, Source, Modality, ConsentScope], UUID] = {}
        self.resolve_calls: list[tuple[UUID, Source, Modality, ConsentScope]] = []
        self.validate_calls: list[tuple[UUID, UUID, Source, Modality, ConsentScope]] = []
        self.resolve_error: Exception | None = None
        self.validate_error: Exception | None = None

    def seed(
        self,
        consent_ref: UUID,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        scope: ConsentScope = ConsentScope.REGISTRATION,
    ) -> None:
        """Install one explicit fixture grant; duplicate tuple values must be replay-identical."""
        if not isinstance(consent_ref, UUID) or not isinstance(tenant_id, UUID):
            raise ValueError("fixture registration consent identities must be UUIDs")
        if not isinstance(source, Source) or not isinstance(modality, Modality):
            raise ValueError("fixture registration consent source and modality must be typed")
        if not isinstance(scope, ConsentScope):
            raise ValueError("fixture registration consent scope must be typed")
        validate_registration_consent_target(source, modality, scope)
        key = (tenant_id, source, modality, scope)
        existing = self._records.get(key)
        if existing is not None and existing != consent_ref:
            raise ValueError("fixture registration consent tuple is already bound")
        self._records[key] = consent_ref

    def remove(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        scope: ConsentScope = ConsentScope.REGISTRATION,
    ) -> None:
        """Remove a fixture record to exercise a pre-mutate evidence loss; not a product revoke API."""
        validate_registration_consent_target(source, modality, scope)
        self._records.pop((tenant_id, source, modality, scope), None)

    async def resolve(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        scope: ConsentScope = ConsentScope.REGISTRATION,
    ) -> UUID | None:
        """Return only an exact opaque fixture ref, never a consent document or secret."""
        validate_registration_consent_target(source, modality, scope)
        self.resolve_calls.append((tenant_id, source, modality, scope))
        if self.resolve_error is not None:
            raise self.resolve_error
        return self._records.get((tenant_id, source, modality, scope))

    async def validate(
        self,
        consent_ref: UUID,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        scope: ConsentScope = ConsentScope.REGISTRATION,
    ) -> bool:
        """Return True only for the tenant/source/modality-bound fixture reference."""
        validate_registration_consent_target(source, modality, scope)
        self.validate_calls.append((consent_ref, tenant_id, source, modality, scope))
        if self.validate_error is not None:
            raise self.validate_error
        return self._records.get((tenant_id, source, modality, scope)) == consent_ref
