"""PostgreSQL policy-control-plane guarantees (FR-5.9, FR-7.2, ADR-004).

The application role receives fresh read-only snapshots.  Only the migration owner may change the
global/source/tenant controls, so a compromised request worker cannot release its own freeze or
quarantine.  No provider surface is involved in these local PostgreSQL checks.
"""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.policy.engine import StoreBackedPolicyEngine
from events_concierge.adapters.postgres.policy import (
    PostgresPolicySnapshotRepository,
    PostgresSourceQuarantineRepository,
)
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import Modality, Source
from events_concierge.domain.policy import SourceQuarantineSignal
from events_concierge.infra.db import system_session_scope, tenant_session_scope
from events_concierge.ports.policy import PolicyContext

pytestmark = pytest.mark.integration


async def test_policy_snapshot_reads_live_source_global_and_tenant_controls(db: None) -> None:
    """One already-composed PDP observes owner flips without rebuilding its process (ADR-004)."""
    owner = await _add_tenant("policy-owner")
    other = await _add_tenant("policy-other")
    engine = StoreBackedPolicyEngine(PostgresPolicySnapshotRepository())
    owner_context = _context(owner)
    other_context = _context(other)

    assert (await engine.evaluate(owner_context)).allowed is True
    try:
        await _set_source_policy(
            Source.MEETUP,
            automation_allowed={"api": False, "browser": False},
            quarantined=True,
        )
        source_denied = await engine.evaluate(owner_context)
        assert source_denied.allowed is False
        assert "quarantined" in source_denied.reason

        await _set_source_policy(
            Source.MEETUP,
            automation_allowed={"api": True, "browser": False},
            quarantined=False,
        )
        await _set_global_kill_switch(True)
        global_denied = await engine.evaluate(owner_context)
        assert global_denied.allowed is False
        assert "kill switch" in global_denied.reason.lower()
        assert await engine.kill_switch_engaged(other) is True

        await _set_global_kill_switch(False)
        await _set_tenant_kill_switch(owner, True)
        tenant_denied = await engine.evaluate(owner_context)
        assert tenant_denied.allowed is False
        assert "kill switch" in tenant_denied.reason.lower()
        assert (await engine.evaluate(other_context)).allowed is True
    finally:
        await _set_global_kill_switch(False)
        await _set_tenant_kill_switch(owner, False)
        await _set_source_policy(
            Source.MEETUP,
            automation_allowed={"api": True, "browser": False},
            quarantined=False,
        )


async def test_tenant_policy_override_obeys_forced_rls_and_empty_context_fails_closed(
    db: None,
) -> None:
    """A tenant's local freeze is visible only to that tenant's app-role transaction (FR-1.3/1.4)."""
    owner = await _add_tenant("policy-rls-owner")
    other = await _add_tenant("policy-rls-other")
    await _set_tenant_kill_switch(owner, True)
    visible = text("SELECT tenant_id, kill_switch FROM public.tenant_policy_control")
    try:
        async with tenant_session_scope(owner) as session:
            owner_rows = (await session.execute(visible)).all()
        async with tenant_session_scope(other) as session:
            other_rows = (await session.execute(visible)).all()
        async with tenant_session_scope(None) as session:
            unset_rows = (await session.execute(visible)).all()
        async with tenant_session_scope(None) as session:
            empty_context = (
                await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            ).scalar_one()
            empty_rows = (await session.execute(visible)).all()
    finally:
        await _set_tenant_kill_switch(owner, False)

    assert [(row.tenant_id, row.kill_switch) for row in owner_rows] == [(owner, True)]
    assert other_rows == []
    assert unset_rows == []
    assert empty_context == ""
    assert empty_rows == []


async def test_app_role_cannot_mutate_or_call_owner_policy_controls(db: None) -> None:
    """The runtime role cannot release a quarantine or engage/disengage an operator control."""
    tenant_id = await _add_tenant("policy-dml")
    statements = (
        (
            None,
            text("UPDATE public.source_policy SET quarantined = true WHERE source = 'meetup'"),
        ),
        (
            None,
            text(
                "UPDATE public.policy_global_control SET kill_switch = true WHERE singleton = true"
            ),
        ),
        (
            tenant_id,
            text(
                """INSERT INTO public.tenant_policy_control (tenant_id, kill_switch)
                   VALUES (:tenant_id, true)"""
            ),
        ),
        (
            None,
            text("SELECT public.fn_set_policy_global_kill_switch(true)"),
        ),
        (
            None,
            text(
                """SELECT public.fn_set_source_policy(
                       'meetup',
                       '{"api": true, "browser": false}'::jsonb,
                       false,
                       false,
                       'none'
                   )"""
            ),
        ),
    )
    for context, statement in statements:
        with pytest.raises(Exception, match="permission denied"):
            async with tenant_session_scope(context) as session:
                await session.execute(statement, {"tenant_id": tenant_id})


async def test_app_role_source_quarantine_is_monotonic_and_preserves_policy_fields(
    db: None,
) -> None:
    """A ban/403 signal flips only the circuit breaker; replay cannot rewrite policy (AC-72)."""
    source = Source.MEETUP
    automation_allowed = {"api": True, "browser": False}
    await _set_source_policy(source, automation_allowed=automation_allowed, quarantined=False)
    repository = PostgresSourceQuarantineRepository()
    try:
        first = await repository.quarantine(source, SourceQuarantineSignal.FORBIDDEN)
        after_first = await _app_source_policy_row(source)
        second = await repository.quarantine(source, SourceQuarantineSignal.BAN)
        after_second = await _app_source_policy_row(source)

        with pytest.raises(Exception, match="permission denied"):
            async with system_session_scope() as session:
                await session.execute(
                    text(
                        "UPDATE public.source_policy SET quarantined = false WHERE source = :source"
                    ),
                    {"source": source.value},
                )
        with pytest.raises(Exception, match="invalid source quarantine signal"):
            async with system_session_scope() as session:
                await session.execute(
                    text(
                        """SELECT public.fn_quarantine_source(
                               :source,
                               'rate_limited'
                           )"""
                    ),
                    {"source": source.value},
                )
    finally:
        await _set_source_policy(source, automation_allowed=automation_allowed, quarantined=False)

    assert first.source is source
    assert first.signal is SourceQuarantineSignal.FORBIDDEN
    assert first.newly_quarantined is True
    assert second.source is source
    assert second.signal is SourceQuarantineSignal.BAN
    assert second.newly_quarantined is False
    assert after_first["automation_allowed"] == automation_allowed
    assert after_first["paid_allowed"] is False
    assert after_first["signed_agent_mode"] == "none"
    assert after_first["quarantined"] is True
    assert after_second == after_first


async def test_policy_source_shape_rejects_malformed_owner_data(db: None) -> None:
    """Even the owner cannot persist a non-boolean or unknown-modality automation map."""
    with pytest.raises(Exception, match="source_policy_automation_allowed_valid"):
        await _owner_execute(
            """
            UPDATE public.source_policy
            SET automation_allowed = CAST(:automation_allowed AS jsonb)
            WHERE source = 'meetup'
            """,
            {"automation_allowed": json.dumps({"api": "not-a-boolean"})},
        )


async def _add_tenant(prefix: str) -> UUID:
    """Create a real tenant so the owner-only per-tenant control accepts its foreign key."""
    tenant_id = uuid4()
    tag = f"{prefix}-{tenant_id.hex}"
    await PostgresTenantRepository().add(
        Tenant(tenant_id, f"oidc|{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    return tenant_id


def _context(tenant_id: UUID) -> PolicyContext:
    """Use the current autonomous Meetup member API lane for one PDP read."""
    return PolicyContext(
        tenant_id=tenant_id,
        source=Source.MEETUP,
        modality=Modality.API,
        action="register",
    )


async def _set_global_kill_switch(engaged: bool) -> None:
    """Invoke the owner-only no-deploy global control and validate its closed return type."""
    result = await _owner_execute(
        "SELECT public.fn_set_policy_global_kill_switch(:engaged)",
        {"engaged": engaged},
    )
    if result is not engaged:
        raise RuntimeError("owner global policy control returned an unexpected value")


async def _set_tenant_kill_switch(tenant_id: UUID, engaged: bool) -> None:
    """Invoke the owner-only tenant control without granting that authority to ec_app."""
    result = await _owner_execute(
        "SELECT public.fn_set_tenant_policy_kill_switch(:tenant_id, :engaged)",
        {"tenant_id": tenant_id, "engaged": engaged},
    )
    if result is not engaged:
        raise RuntimeError("owner tenant policy control returned an unexpected value")


async def _set_source_policy(
    source: Source,
    *,
    automation_allowed: dict[str, bool],
    quarantined: bool,
) -> None:
    """Apply the current free-only source policy through its owner-only control function."""
    result = await _owner_execute(
        """
        SELECT public.fn_set_source_policy(
            :source,
            CAST(:automation_allowed AS jsonb),
            false,
            :quarantined,
            'none'
        )
        """,
        {
            "source": source.value,
            "automation_allowed": json.dumps(automation_allowed),
            "quarantined": quarantined,
        },
    )
    if result is not True:
        raise RuntimeError("owner source policy control returned an unexpected value")


async def _app_source_policy_row(source: Source) -> dict[str, object]:
    """Read only the fields the app role is allowed to inspect after an actuator call."""
    async with system_session_scope() as session:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT automation_allowed, paid_allowed, quarantined, signed_agent_mode, updated_at
                       FROM public.source_policy
                       WHERE source = :source"""
                    ),
                    {"source": source.value},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _owner_execute(statement: str, parameters: dict[str, object]) -> object:
    """Run one test-only control action as the migration owner, never as ec_app."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        raise RuntimeError("EC_MIGRATION_URL is required for policy-control integration tests")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            return (await connection.execute(text(statement), parameters)).scalar_one()
    finally:
        await owner_engine.dispose()
