"""Tenant-identity provisioning and FORCE-RLS guarantees (FR-1.2--1.4, ADR-001).

``tenants`` contains identity and contact data, so the application role may neither enumerate it
outside its tenant context nor mutate it directly.  Provisioning is deliberately a narrow,
replay-safe SQL boundary; its conflict surface must not disclose a prior tenant's contact data.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.credentials import Tenant
from events_concierge.infra.db import system_session_scope, tenant_session_scope

pytestmark = pytest.mark.integration

_VISIBLE_TENANTS = text(
    """
    SELECT tenant_id, oidc_subject, notify_email, relay_inbox
    FROM public.tenants
    ORDER BY tenant_id
    """
)


async def test_tenant_identity_obeys_forced_rls_for_owner_other_unset_and_empty_context(
    db: None,
) -> None:
    """An app-role tenant sees only itself; omitted and blank GUC contexts fail closed."""
    owner = _tenant("tenant-identity-owner")
    other = _tenant("tenant-identity-other")
    repository = PostgresTenantRepository()
    await repository.add(owner)
    await repository.add(other)

    async with tenant_session_scope(owner.tenant_id) as session:
        owner_rows = (await session.execute(_VISIBLE_TENANTS)).all()
    async with tenant_session_scope(other.tenant_id) as session:
        other_rows = (await session.execute(_VISIBLE_TENANTS)).all()
    async with tenant_session_scope(None) as session:
        unset_rows = (await session.execute(_VISIBLE_TENANTS)).all()
    async with tenant_session_scope(None) as session:
        empty_context = (
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        ).scalar_one()
        empty_rows = (await session.execute(_VISIBLE_TENANTS)).all()

    assert _tenant_rows(owner_rows) == [_tenant_row(owner)]
    assert _tenant_rows(other_rows) == [_tenant_row(other)]
    assert unset_rows == []
    assert empty_context == ""
    assert empty_rows == []


async def test_app_role_cannot_mutate_tenant_identity_directly(db: None) -> None:
    """Only ``fn_provision_tenant`` may write identity/contact bindings (FR-1.2, ADR-001)."""
    tenant = _tenant("tenant-identity-direct-dml")
    direct_insert = _tenant("tenant-identity-direct-insert")
    await PostgresTenantRepository().add(tenant)
    statements = (
        (
            direct_insert.tenant_id,
            text(
                """
                INSERT INTO public.tenants (tenant_id, oidc_subject, notify_email, relay_inbox)
                VALUES (:tenant_id, :oidc_subject, :notify_email, :relay_inbox)
                """
            ),
            _tenant_parameters(direct_insert),
        ),
        (
            tenant.tenant_id,
            text(
                """
                UPDATE public.tenants
                SET notify_email = :notify_email
                WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": tenant.tenant_id, "notify_email": "tampered@example.test"},
        ),
        (
            tenant.tenant_id,
            text("DELETE FROM public.tenants WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant.tenant_id},
        ),
    )

    for context, statement, parameters in statements:
        with pytest.raises(Exception, match="permission denied"):
            async with tenant_session_scope(context) as session:
                await session.execute(statement, parameters)

    assert await PostgresTenantRepository().get(tenant.tenant_id) == tenant
    assert await PostgresTenantRepository().get(direct_insert.tenant_id) is None


async def test_provisioning_function_is_replay_safe_and_hides_conflicting_binding_data(
    db: None,
) -> None:
    """The SQL boundary returns true/new, false/exact replay, and a generic binding conflict."""
    tenant = _tenant("tenant-identity-provision")
    first = await _provision(tenant)
    replay = await _provision(tenant)
    conflicting = Tenant(
        tenant.tenant_id,
        f"{tenant.oidc_subject}-different",
        "private-conflict@example.test",
        "private-conflict@u.example.test",
    )
    conflicting_subject = Tenant(
        uuid4(),
        tenant.oidc_subject,
        "private-subject-conflict@example.test",
        "private-subject-conflict@u.example.test",
    )

    conflict_messages: list[str] = []
    for attempted_binding in (conflicting, conflicting_subject):
        with pytest.raises(Exception) as raised:
            await _provision(attempted_binding)
        conflict_messages.append(_database_primary_message(raised.value))

    assert first is True
    assert replay is False
    assert all("conflict" in message.lower() for message in conflict_messages)
    for private_value in (
        tenant.oidc_subject,
        tenant.notify_email,
        tenant.relay_inbox,
        conflicting.oidc_subject,
        conflicting.notify_email,
        conflicting.relay_inbox,
        conflicting_subject.notify_email,
        conflicting_subject.relay_inbox,
    ):
        assert all(private_value not in message for message in conflict_messages)

    # The failed retry cannot alter or expose the established binding through the table path.
    assert await PostgresTenantRepository().get(tenant.tenant_id) == tenant


async def test_tenant_repository_provisions_and_reads_only_its_scoped_identity(db: None) -> None:
    """The compatibility ``add`` facade succeeds on an exact retry and ``get`` sets RLS context."""
    tenant = _tenant("tenant-identity-repository")
    repository = PostgresTenantRepository()

    await repository.add(tenant)
    await repository.add(tenant)

    assert await repository.get(tenant.tenant_id) == tenant
    assert await repository.get(uuid4()) is None


async def _provision(tenant: Tenant) -> bool:
    """Call the sole app-role identity write boundary without relying on a table grant."""
    async with system_session_scope() as session:
        return bool(
            (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_provision_tenant(
                            :tenant_id,
                            :oidc_subject,
                            :notify_email,
                            :relay_inbox
                        ) AS provisioned
                        """
                    ),
                    _tenant_parameters(tenant),
                )
            ).scalar_one()
        )


def _tenant(prefix: str) -> Tenant:
    """Make an isolated tenant fixture whose PII is unique enough to detect accidental disclosure."""
    tenant_id = uuid4()
    tag = f"{prefix}-{tenant_id.hex}"
    return Tenant(
        tenant_id=tenant_id,
        oidc_subject=f"oidc|{tag}",
        notify_email=f"{tag}@example.test",
        relay_inbox=f"{tag}@u.example.test",
    )


def _tenant_parameters(tenant: Tenant) -> dict[str, UUID | str]:
    """Bind only the identity fields accepted by ``fn_provision_tenant``."""
    return {
        "tenant_id": tenant.tenant_id,
        "oidc_subject": tenant.oidc_subject,
        "notify_email": tenant.notify_email,
        "relay_inbox": tenant.relay_inbox,
    }


def _tenant_row(tenant: Tenant) -> tuple[UUID, str, str, str]:
    """Normalize the own-tenant projection used by the visibility assertions."""
    return (
        tenant.tenant_id,
        tenant.oidc_subject,
        tenant.notify_email,
        tenant.relay_inbox,
    )


def _tenant_rows(rows: object) -> list[tuple[UUID, str, str, str]]:
    """Validate the app-role projection shape before comparing only self-visible rows."""
    if not isinstance(rows, list):
        raise RuntimeError("tenant visibility query returned an unexpected row container")
    normalized: list[tuple[UUID, str, str, str]] = []
    for row in rows:
        values = tuple(row)
        if (
            len(values) != 4
            or not isinstance(values[0], UUID)
            or not all(isinstance(value, str) for value in values[1:])
        ):
            raise RuntimeError("tenant visibility query returned an unexpected row shape")
        normalized.append((values[0], values[1], values[2], values[3]))
    return normalized


def _database_primary_message(error: BaseException) -> str:
    """Read PostgreSQL's server message, excluding client-rendered SQL parameter echoes."""
    original = getattr(error, "orig", error)
    diagnostics = getattr(original, "diag", None)
    message = getattr(diagnostics, "message_primary", None)
    if not isinstance(message, str):
        raise RuntimeError("tenant provisioning did not return a PostgreSQL primary error message")
    return message
