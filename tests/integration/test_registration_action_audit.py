"""PostgreSQL guarantees for the immutable registration-action audit (FR-7.3, NFR-8/10).

The application role may inspect its tenant's PII-minimized evidence, but only the guarded
``SECURITY DEFINER`` append function may create it. These checks exercise that boundary against the
real non-superuser integration connection, including concurrent Temporal-style retries.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from events_concierge.adapters.postgres.audit import PostgresRegistrationActionAuditRepository
from events_concierge.domain.audit import (
    RegistrationActionAudit,
    RegistrationActionAuditDecision,
    RegistrationActionAuditPhase,
)
from events_concierge.domain.enums import Modality, Source
from events_concierge.infra.db import system_session_scope, tenant_session_scope

pytestmark = pytest.mark.integration

_VISIBLE_AUDIT_KEYS = text(
    """
    SELECT audit_key
    FROM public.registration_action_audit
    WHERE audit_key = :audit_key
    """
)

_DIRECT_INSERT = text(
    """
    INSERT INTO public.registration_action_audit (
        audit_key, tenant_id, workflow_id, source, modality, phase, policy_decision, outcome
    ) VALUES (
        :audit_key, :tenant_id, :workflow_id, 'luma', 'browser', 'policy_precheck', 'allowed', NULL
    )
    """
)


async def test_registration_action_audit_exact_replay_is_a_noop_and_mismatch_is_rejected(
    db: None,
) -> None:
    """One retry-stable key appends once and cannot be rebound to a different fact (NFR-8)."""
    repository = PostgresRegistrationActionAuditRepository()
    record = _record()

    assert await repository.append(record) is True
    assert await repository.append(record) is False

    with pytest.raises(Exception, match="already bound"):
        await repository.append(
            replace(record, phase=RegistrationActionAuditPhase.POLICY_PRE_MUTATE)
        )

    async with tenant_session_scope(record.tenant_id) as session:
        rows = (await session.execute(_VISIBLE_AUDIT_KEYS, {"audit_key": record.audit_key})).all()

    assert len(rows) == 1
    assert rows[0].audit_key == record.audit_key


async def test_registration_action_audit_concurrent_exact_replays_converge_to_one_row(
    db: None,
) -> None:
    """Concurrent activity retries serialize on an audit key and return one append (ADR-003, NFR-8)."""
    repository = PostgresRegistrationActionAuditRepository()
    record = _record()
    barrier = asyncio.Barrier(2)

    async def append_once() -> bool:
        await barrier.wait()
        return await repository.append(record)

    results = await asyncio.gather(append_once(), append_once())

    assert results.count(True) == 1
    assert results.count(False) == 1
    async with tenant_session_scope(record.tenant_id) as session:
        count = (
            await session.execute(
                text(
                    """SELECT count(*) FROM public.registration_action_audit
                       WHERE audit_key = :audit_key"""
                ),
                {"audit_key": record.audit_key},
            )
        ).scalar_one()
    assert int(count) == 1


async def test_registration_action_audit_is_tenant_scoped_and_empty_context_fails_closed(
    db: None,
) -> None:
    """The evidence table honors RLS for owner, other, unset, and empty-GUC contexts (FR-1.3/1.4)."""
    repository = PostgresRegistrationActionAuditRepository()
    owner, other = uuid4(), uuid4()
    record = _record(owner)
    assert await repository.append(record) is True

    async with tenant_session_scope(owner) as session:
        owner_keys = set(
            (await session.execute(_VISIBLE_AUDIT_KEYS, {"audit_key": record.audit_key})).scalars()
        )
    async with tenant_session_scope(other) as session:
        other_keys = set(
            (await session.execute(_VISIBLE_AUDIT_KEYS, {"audit_key": record.audit_key})).scalars()
        )
    async with tenant_session_scope(None) as session:
        unset_keys = set(
            (await session.execute(_VISIBLE_AUDIT_KEYS, {"audit_key": record.audit_key})).scalars()
        )
    async with tenant_session_scope(None) as session:
        empty_context = (
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        ).scalar_one()
        empty_keys = set(
            (await session.execute(_VISIBLE_AUDIT_KEYS, {"audit_key": record.audit_key})).scalars()
        )

    assert owner_keys == {record.audit_key}
    assert other_keys == set()
    assert unset_keys == set()
    assert empty_context == ""
    assert empty_keys == set()


async def test_registration_action_audit_denies_app_role_dml_but_allows_guarded_append(
    db: None,
) -> None:
    """The app role cannot mutate audit rows directly, even under its tenant context (NFR-8/10)."""
    repository = PostgresRegistrationActionAuditRepository()
    record = _record()
    assert await repository.append(record) is True

    direct_record = _record(record.tenant_id)
    parameters = {
        "audit_key": direct_record.audit_key,
        "tenant_id": direct_record.tenant_id,
        "workflow_id": direct_record.workflow_id,
    }
    statements = (
        (_DIRECT_INSERT, parameters),
        (
            text(
                """UPDATE public.registration_action_audit
                   SET phase = 'policy_pre_mutate'
                   WHERE audit_key = :audit_key"""
            ),
            {"audit_key": record.audit_key},
        ),
        (
            text("DELETE FROM public.registration_action_audit WHERE audit_key = :audit_key"),
            {"audit_key": record.audit_key},
        ),
    )
    for statement, statement_parameters in statements:
        with pytest.raises(Exception, match="permission denied"):
            async with tenant_session_scope(record.tenant_id) as session:
                await session.execute(statement, statement_parameters)

    # The explicitly granted function remains the sole append path after direct DML is rejected.
    assert await repository.append(direct_record) is True


async def test_registration_action_audit_schema_excludes_raw_event_and_identity_content(
    db: None,
) -> None:
    """The P6a ledger stores only opaque identities and closed facts, never raw event/user content."""
    async with system_session_scope() as session:
        columns = set(
            (
                await session.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'registration_action_audit'
                        """
                    )
                )
            ).scalars()
        )

    assert columns == {
        "audit_id",
        "audit_key",
        "consent_ref",
        "created_at",
        "modality",
        "outcome",
        "phase",
        "policy_decision",
        "source",
        "tenant_id",
        "workflow_id",
    }
    assert columns.isdisjoint(
        {
            "credential",
            "credential_ref",
            "deep_link",
            "description",
            "detail",
            "event_title",
            "otp",
            "payload",
            "provider_payload",
            "raw_text",
            "title",
            "url",
        }
    )


def _record(tenant_id: UUID | None = None) -> RegistrationActionAudit:
    """Build a valid denied precheck without manufacturing P14c consent evidence."""
    resolved_tenant_id = tenant_id or uuid4()
    canonical_event_id = uuid4()
    return RegistrationActionAudit(
        audit_key=f"audit:{resolved_tenant_id}:{canonical_event_id}:{uuid4().hex}",
        tenant_id=resolved_tenant_id,
        workflow_id=f"{resolved_tenant_id}:{canonical_event_id}",
        source=Source.LUMA,
        modality=Modality.BROWSER,
        phase=RegistrationActionAuditPhase.POLICY_PRECHECK,
        policy_decision=RegistrationActionAuditDecision.DENIED,
    )
