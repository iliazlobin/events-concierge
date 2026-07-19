"""PostgreSQL registration-consent evidence adapter (FR-2.9, FR-7.3, ADR-004).

The application cannot read or write the owner-seeded evidence table directly.  These narrow
calls expose only an opaque UUID or an exact validation result under the established tenant GUC.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from ...domain.enums import ConsentScope, Modality, Source
from ...infra.db import tenant_session_scope
from ...ports.consent import validate_registration_consent_target


class PostgresRegistrationConsentEvidenceRepository:
    """Resolve/revalidate a fixed registration consent through SQL capabilities only."""

    async def resolve(
        self,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        scope: ConsentScope = ConsentScope.REGISTRATION,
    ) -> UUID | None:
        """Return one tenant-bound opaque ref, with missing evidence represented by None."""
        validate_registration_consent_target(source, modality, scope)
        async with tenant_session_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text(
                        """SELECT public.fn_resolve_registration_consent(
                               :source, :modality
                           ) AS consent_ref"""
                    ),
                    {"source": source.value, "modality": modality.value},
                )
            ).scalar_one()
        if value is not None and not isinstance(value, UUID):
            raise RuntimeError("registration consent resolver returned a non-UUID")
        return value

    async def validate(
        self,
        consent_ref: UUID,
        tenant_id: UUID,
        source: Source,
        modality: Modality,
        scope: ConsentScope = ConsentScope.REGISTRATION,
    ) -> bool:
        """Revalidate a single opaque reference under the current tenant scope."""
        if not isinstance(consent_ref, UUID):
            return False
        validate_registration_consent_target(source, modality, scope)
        async with tenant_session_scope(tenant_id) as session:
            valid = (
                await session.execute(
                    text(
                        """SELECT public.fn_validate_registration_consent(
                               :consent_ref, :source, :modality
                           ) AS valid"""
                    ),
                    {
                        "consent_ref": consent_ref,
                        "source": source.value,
                        "modality": modality.value,
                    },
                )
            ).scalar_one()
        if not isinstance(valid, bool):
            raise RuntimeError("registration consent validator returned a non-boolean")
        return valid
