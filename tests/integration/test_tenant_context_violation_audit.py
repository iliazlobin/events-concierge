"""FR-1.4 missing-tenant-context evidence at the explicit database-session boundary.

The private audit is intentionally asserted through the migration owner only: a context-free event
cannot be tenant-RLS scoped, and the runtime app role must not inspect or modify it.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.credentials import Tenant
from events_concierge.infra.db import system_session_scope, tenant_session_scope

pytestmark = pytest.mark.integration


async def test_missing_tenant_scope_fails_closed_and_commits_private_audit(db: None) -> None:
    """One context-free tenant read returns no rows and leaves exactly one durable AC-3 event."""
    tenant = await _add_tenant("missing-context-read")
    before = await _audit_count()

    async with tenant_session_scope(None) as session:
        rows = (
            await session.execute(
                text("SELECT tenant_id FROM public.tenants WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant.tenant_id},
            )
        ).all()

    assert rows == []
    assert await _audit_count() == before + 1
    assert await _latest_audit() == ("missing_tenant_context", "ec_app")


async def test_missing_tenant_audit_survives_a_rejected_rls_write(db: None) -> None:
    """The audit transaction commits before a later target transaction rolls back (FR-1.4)."""
    before = await _audit_count()

    with pytest.raises(Exception, match="row-level security policy"):
        async with tenant_session_scope(None) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO public.event_requests (request_id, tenant_id, raw_text, constraints)
                    VALUES (:request_id, :tenant_id, 'no context', '{}'::jsonb)
                    """
                ),
                {"request_id": uuid4(), "tenant_id": uuid4()},
            )

    assert await _audit_count() == before + 1


async def test_valid_tenant_and_explicit_system_scopes_do_not_audit(db: None) -> None:
    """Tenant-neutral work clears the GUC without fabricating a missing-tenant event (FR-1.3/1.4)."""
    tenant = await _add_tenant("scope-intent")
    before = await _audit_count()

    async with tenant_session_scope(tenant.tenant_id) as session:
        tenant_context = (
            await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        ).scalar_one()
        visible = (
            await session.execute(
                text("SELECT tenant_id FROM public.tenants WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant.tenant_id},
            )
        ).all()

    async with system_session_scope() as session:
        system_context = (
            await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        ).scalar_one()
        catalog_count = (
            await session.execute(text("SELECT count(*) FROM public.source_policy"))
        ).scalar_one()

    assert tenant_context == str(tenant.tenant_id)
    assert [row.tenant_id for row in visible] == [tenant.tenant_id]
    assert system_context == ""
    assert int(catalog_count) >= 0
    assert await _audit_count() == before


async def test_app_role_has_only_the_empty_context_audit_capability(db: None) -> None:
    """The app cannot read, alter, or forge the private evidence under a real tenant context."""
    tenant = await _add_tenant("private-audit-privileges")
    before = await _audit_count()

    async with system_session_scope() as session:
        privileges = dict(
            (
                await session.execute(
                    text(
                        """
                        SELECT has_table_privilege(
                                   current_user,
                                   'public.tenant_context_violation_audit',
                                   'SELECT,INSERT,UPDATE,DELETE'
                               ) AS table_access,
                               has_sequence_privilege(
                                   current_user,
                                   'public.tenant_context_violation_audit_violation_id_seq',
                                   'USAGE,SELECT,UPDATE'
                               ) AS sequence_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_record_missing_tenant_context()',
                                   'EXECUTE'
                               ) AS function_access
                        """
                    )
                )
            )
            .mappings()
            .one()
        )

    assert privileges == {
        "table_access": False,
        "sequence_access": False,
        "function_access": True,
    }

    with pytest.raises(Exception, match="permission denied"):
        async with system_session_scope() as session:
            await session.execute(text("SELECT * FROM public.tenant_context_violation_audit"))

    with pytest.raises(Exception, match="empty tenant context"):
        async with tenant_session_scope(tenant.tenant_id) as session:
            await session.execute(text("SELECT public.fn_record_missing_tenant_context()"))

    assert await _audit_count() == before


async def _add_tenant(label: str) -> Tenant:
    """Provision one valid identity through its guarded runtime capability."""
    tenant_id = uuid4()
    tenant = Tenant(
        tenant_id,
        f"tenant-context-audit-{label}-{tenant_id.hex}",
        f"tenant-context-audit-{tenant_id.hex}@example.test",
        f"tenant-context-audit-{tenant_id.hex}@u.example.test",
    )
    await PostgresTenantRepository().add(tenant)
    return tenant


async def _audit_count() -> int:
    """Count private policy-violation evidence through the migration owner only."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.connect() as connection:
            count = (
                await connection.execute(
                    text("SELECT count(*) FROM public.tenant_context_violation_audit")
                )
            ).scalar_one()
    finally:
        await owner_engine.dispose()
    return int(count)


async def _latest_audit() -> tuple[str, str]:
    """Read only the PII-free fixed fields of the newest private event as the owner."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT violation_type, caller_role
                        FROM public.tenant_context_violation_audit
                        ORDER BY violation_id DESC
                        LIMIT 1
                        """
                    )
                )
            ).one()
    finally:
        await owner_engine.dispose()
    return str(row.violation_type), str(row.caller_role)
