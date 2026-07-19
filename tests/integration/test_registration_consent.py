"""Registration-consent capability and audit-guard guarantees (FR-2.9, FR-7.3, NFR-8/10).

The migration owner seeds opaque evidence directly for isolated fixtures. The non-superuser app
role receives only tenant-derived resolver and validator functions, never a raw consent-table
grant. These checks also preserve the historical nullable-audit replay invariant from ADR-003.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from events_concierge.adapters.postgres.audit import PostgresRegistrationActionAuditRepository
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.audit import (
    RegistrationActionAudit,
    RegistrationActionAuditDecision,
    RegistrationActionAuditPhase,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import Modality, Source
from events_concierge.infra.db import tenant_session_scope

pytestmark = pytest.mark.integration

_RESOLVE_CONSENT = text(
    """
    SELECT public.fn_resolve_registration_consent(:source, :modality) AS consent_ref
    """
)
_VALIDATE_CONSENT = text(
    """
    SELECT public.fn_validate_registration_consent(
        :consent_ref, :source, :modality
    ) AS valid
    """
)
_CURRENT_TENANT_SETTING = text("SELECT current_setting('app.tenant_id', true)")
_SET_EMPTY_TENANT_SETTING = text("SELECT set_config('app.tenant_id', '', true)")
_OWNER_INSERT_CONSENT = text(
    """
    INSERT INTO public.tenant_source_consents (
        consent_id, tenant_id, source, modality, scope
    ) VALUES (
        :consent_id, :tenant_id, :source, :modality, 'registration'
    )
    """
)
_OWNER_INSERT_HISTORICAL_NULL_AUDIT = text(
    """
    INSERT INTO public.registration_action_audit (
        audit_key, tenant_id, workflow_id, source, modality, phase, policy_decision,
        outcome, consent_ref
    ) VALUES (
        :audit_key, :tenant_id, :workflow_id, :source, :modality, :phase,
        :policy_decision, :outcome, NULL
    )
    """
)


async def test_owner_seeded_consent_resolves_and_validates_only_in_its_tenant_context(
    db: None,
) -> None:
    """Capabilities reveal only one matching tenant/source/modality consent (FR-2.9, NFR-10)."""
    owner = _tenant("registration-consent-owner")
    other = _tenant("registration-consent-other")
    tenants = PostgresTenantRepository()
    await tenants.add(owner)
    await tenants.add(other)
    owner_meetup, owner_luma, other_meetup = await _owner_seed_consents(
        (
            (owner.tenant_id, Source.MEETUP, Modality.API),
            (owner.tenant_id, Source.LUMA, Modality.BROWSER),
            (other.tenant_id, Source.MEETUP, Modality.API),
        )
    )

    async with tenant_session_scope(owner.tenant_id) as session:
        assert await _resolve(session, Source.MEETUP, Modality.API) == owner_meetup
        assert await _resolve(session, Source.LUMA, Modality.BROWSER) == owner_luma
        assert await _validate(session, owner_meetup, Source.MEETUP, Modality.API)
        assert not await _validate(session, other_meetup, Source.MEETUP, Modality.API)
        assert not await _validate(session, owner_luma, Source.MEETUP, Modality.API)

    async with tenant_session_scope(other.tenant_id) as session:
        assert await _resolve(session, Source.MEETUP, Modality.API) == other_meetup
        assert await _resolve(session, Source.LUMA, Modality.BROWSER) is None
        assert await _validate(session, other_meetup, Source.MEETUP, Modality.API)
        assert not await _validate(session, owner_meetup, Source.MEETUP, Modality.API)

    (
        unset_setting,
        unset_resolved,
        unset_valid,
        empty_setting,
        empty_resolved,
        empty_valid,
    ) = await _capabilities_without_tenant_context(owner_meetup)

    assert unset_setting is None
    assert unset_resolved is None
    assert unset_valid is False
    assert empty_setting == ""
    assert empty_resolved is None
    assert empty_valid is False


async def test_app_role_has_consent_capabilities_but_no_raw_consent_table_access(
    db: None,
) -> None:
    """The app role may call the fixed functions but cannot select or mutate consent evidence."""
    tenant = _tenant("registration-consent-capability")
    await PostgresTenantRepository().add(tenant)
    (consent_id,) = await _owner_seed_consents(((tenant.tenant_id, Source.MEETUP, Modality.API),))

    async with tenant_session_scope(tenant.tenant_id) as session:
        assert await _resolve(session, Source.MEETUP, Modality.API) == consent_id
        assert await _validate(session, consent_id, Source.MEETUP, Modality.API)

    statements = (
        (
            text(
                """
                SELECT consent_id
                FROM public.tenant_source_consents
                WHERE consent_id = :consent_id
                """
            ),
            {"consent_id": consent_id},
        ),
        (
            text(
                """
                INSERT INTO public.tenant_source_consents (
                    consent_id, tenant_id, source, modality, scope
                ) VALUES (
                    :new_consent_id, :tenant_id, 'meetup', 'api', 'registration'
                )
                """
            ),
            {"new_consent_id": uuid4(), "tenant_id": tenant.tenant_id},
        ),
        (
            text(
                """
                UPDATE public.tenant_source_consents
                SET scope = 'registration'
                WHERE consent_id = :consent_id
                """
            ),
            {"consent_id": consent_id},
        ),
        (
            text(
                """
                DELETE FROM public.tenant_source_consents
                WHERE consent_id = :consent_id
                """
            ),
            {"consent_id": consent_id},
        ),
    )
    for statement, parameters in statements:
        with pytest.raises(Exception, match="permission denied"):
            async with tenant_session_scope(tenant.tenant_id) as session:
                await session.execute(statement, parameters)

    async with tenant_session_scope(tenant.tenant_id) as session:
        assert await _resolve(session, Source.MEETUP, Modality.API) == consent_id


async def test_audit_guard_rejects_invalid_consent_and_replays_historical_null_evidence(
    db: None,
) -> None:
    """New allowed facts need exact current evidence; a P6a historical exact replay still converges."""
    owner = _tenant("registration-consent-audit-owner")
    other = _tenant("registration-consent-audit-other")
    tenants = PostgresTenantRepository()
    await tenants.add(owner)
    await tenants.add(other)
    owner_meetup, owner_luma, other_meetup = await _owner_seed_consents(
        (
            (owner.tenant_id, Source.MEETUP, Modality.API),
            (owner.tenant_id, Source.LUMA, Modality.BROWSER),
            (other.tenant_id, Source.MEETUP, Modality.API),
        )
    )
    repository = PostgresRegistrationActionAuditRepository()

    assert await repository.append(_allowed_audit(owner.tenant_id, owner_meetup, "valid")) is True

    invalid_references = (
        ("missing", None, "requires a consent reference"),
        ("nonexistent", uuid4(), "consent reference is invalid"),
        ("foreign", other_meetup, "consent reference is invalid"),
        ("mismatched", owner_luma, "consent reference is invalid"),
    )
    for label, consent_ref, error_message in invalid_references:
        with pytest.raises(Exception, match=error_message):
            await repository.append(_allowed_audit(owner.tenant_id, consent_ref, label))

    historical = _allowed_audit(owner.tenant_id, None, "historical-null")
    await _owner_seed_historical_null_audit(historical)

    assert await repository.append(historical) is False


async def _resolve(
    connection: AsyncConnection | AsyncSession,
    source: Source,
    modality: Modality,
) -> UUID | None:
    """Resolve one opaque consent through the app-role capability."""
    resolved = (
        await connection.execute(
            _RESOLVE_CONSENT,
            {"source": source.value, "modality": modality.value},
        )
    ).scalar_one()
    if resolved is not None and not isinstance(resolved, UUID):
        raise RuntimeError("registration consent resolver returned a non-UUID")
    return resolved


async def _validate(
    connection: AsyncConnection | AsyncSession,
    consent_ref: UUID,
    source: Source,
    modality: Modality,
) -> bool:
    """Validate one opaque consent through the app-role capability."""
    valid = (
        await connection.execute(
            _VALIDATE_CONSENT,
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


async def _capabilities_without_tenant_context(
    consent_ref: UUID,
) -> tuple[str | None, UUID | None, bool, str | None, UUID | None, bool]:
    """Use fresh app-role connections to distinguish absent and deliberately empty tenant settings."""
    app_engine = create_async_engine(_app_database_url(), poolclass=NullPool)
    try:
        async with app_engine.connect() as connection:
            unset_setting = await _tenant_setting(connection)
            unset_resolved = await _resolve(connection, Source.MEETUP, Modality.API)
            unset_valid = await _validate(
                connection,
                consent_ref,
                Source.MEETUP,
                Modality.API,
            )

        async with app_engine.connect() as connection:
            await connection.execute(_SET_EMPTY_TENANT_SETTING)
            empty_setting = await _tenant_setting(connection)
            empty_resolved = await _resolve(connection, Source.MEETUP, Modality.API)
            empty_valid = await _validate(
                connection,
                consent_ref,
                Source.MEETUP,
                Modality.API,
            )
    finally:
        await app_engine.dispose()

    return (
        unset_setting,
        unset_resolved,
        unset_valid,
        empty_setting,
        empty_resolved,
        empty_valid,
    )


async def _tenant_setting(connection: AsyncConnection) -> str | None:
    """Read the optional tenant GUC while preserving the distinction needed by the fail-closed test."""
    value = (await connection.execute(_CURRENT_TENANT_SETTING)).scalar_one()
    if value is not None and not isinstance(value, str):
        raise RuntimeError("app.tenant_id GUC returned an unexpected value")
    return value


async def _owner_seed_consents(
    seeds: tuple[tuple[UUID, Source, Modality], ...],
) -> tuple[UUID, ...]:
    """Seed trusted fixture evidence through the migration-owner connection only."""
    consent_ids = tuple(uuid4() for _ in seeds)
    owner_engine = create_async_engine(_migration_database_url(), pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            for consent_id, (tenant_id, source, modality) in zip(consent_ids, seeds, strict=True):
                await connection.execute(
                    _OWNER_INSERT_CONSENT,
                    {
                        "consent_id": consent_id,
                        "tenant_id": tenant_id,
                        "source": source.value,
                        "modality": modality.value,
                    },
                )
    finally:
        await owner_engine.dispose()
    return consent_ids


async def _owner_seed_historical_null_audit(record: RegistrationActionAudit) -> None:
    """Model a pre-P14c nullable row with the migration owner before exercising its exact replay."""
    owner_engine = create_async_engine(_migration_database_url(), pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                _OWNER_INSERT_HISTORICAL_NULL_AUDIT,
                {
                    "audit_key": record.audit_key,
                    "tenant_id": record.tenant_id,
                    "workflow_id": record.workflow_id,
                    "source": record.source.value,
                    "modality": record.modality.value,
                    "phase": record.phase.value,
                    "policy_decision": record.policy_decision.value,
                    "outcome": record.outcome.value if record.outcome is not None else None,
                },
            )
    finally:
        await owner_engine.dispose()


def _tenant(prefix: str) -> Tenant:
    """Build a tenant whose provisioned identity is unique to this database integration case."""
    tenant_id = uuid4()
    tag = f"{prefix}-{tenant_id.hex}"
    return Tenant(
        tenant_id=tenant_id,
        oidc_subject=f"oidc|{tag}",
        notify_email=f"{tag}@example.test",
        relay_inbox=f"{tag}@u.example.test",
    )


def _allowed_audit(
    tenant_id: UUID,
    consent_ref: UUID | None,
    label: str,
) -> RegistrationActionAudit:
    """Build one new allowed Meetup/API precheck with a deterministic workflow identity."""
    event_id = uuid4()
    return RegistrationActionAudit(
        audit_key=f"registration-consent:{label}:{tenant_id}:{event_id}:{uuid4().hex}",
        tenant_id=tenant_id,
        workflow_id=f"{tenant_id}:{event_id}",
        source=Source.MEETUP,
        modality=Modality.API,
        phase=RegistrationActionAuditPhase.POLICY_PRECHECK,
        policy_decision=RegistrationActionAuditDecision.ALLOWED,
        consent_ref=consent_ref,
    )


def _app_database_url() -> str:
    """Return the non-superuser application URL required for RLS/function boundary checks."""
    database_url = os.environ.get("EC_DATABASE_URL")
    if not database_url:
        pytest.skip("EC_DATABASE_URL not set; run make test-integration")
    return database_url


def _migration_database_url() -> str:
    """Return the owner URL used only to install trusted fixture rows."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if not migration_url:
        pytest.skip("EC_MIGRATION_URL not set; run make test-integration")
    return migration_url
