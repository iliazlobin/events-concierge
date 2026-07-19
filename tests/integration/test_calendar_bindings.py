"""RLS coverage for persisted Google calendar bindings (FR-1.3/1.4, FR-9.6)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from events_concierge.adapters.postgres.calendar_bindings import PostgresGoogleCalendarBindings
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.credentials import Tenant
from events_concierge.infra.db import tenant_session_scope
from events_concierge.ports.google_calendar import GoogleCalendarBinding

pytestmark = pytest.mark.integration


async def _add_tenant(tenant_id: UUID) -> None:
    tag = f"calendar-binding-{tenant_id.hex}"
    await PostgresTenantRepository().add(
        Tenant(tenant_id, tag, f"{tag}@example.test", f"{tag}@u.example.test")
    )


async def test_calendar_binding_rls_isolates_tenants_and_fails_closed_without_context(
    db: None,
) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    await _add_tenant(tenant_a)
    await _add_tenant(tenant_b)
    bindings = PostgresGoogleCalendarBindings()
    await bindings.upsert_binding(
        tenant_a,
        GoogleCalendarBinding("calendar-a", ("primary-a", "calendar-a")),
    )
    await bindings.upsert_binding(
        tenant_b,
        GoogleCalendarBinding("calendar-b", ("primary-b", "calendar-b")),
    )

    async with tenant_session_scope(tenant_a) as session:
        visible = (await session.execute(text("SELECT tenant_id FROM calendar_bindings"))).all()
    assert [row.tenant_id for row in visible] == [tenant_a]

    async with tenant_session_scope(None) as session:
        count = (await session.execute(text("SELECT count(*) AS n FROM calendar_bindings"))).one().n
    assert count == 0
