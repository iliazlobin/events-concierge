"""Tenant-isolation integration test (AC-1 / FR-1.3/1.4): a query under tenant A's context never sees
tenant B's rows, and a query with no tenant context fails closed to zero rows."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from events_concierge.adapters.postgres.tenant_repos import PostgresRequestRepository
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.infra.db import tenant_session_scope

pytestmark = pytest.mark.integration

_INSERT = text(
    "INSERT INTO lifecycle (lifecycle_id, tenant_id, canonical_event_id, workflow_id) "
    "VALUES (:l, :t, :c, :w)"
)


async def test_rls_isolates_tenants(db: None) -> None:
    a, b = uuid4(), uuid4()
    wid_a, wid_b = f"wid-a-{a.hex}", f"wid-b-{b.hex}"

    async with tenant_session_scope(a) as s:
        await s.execute(_INSERT, {"l": uuid4(), "t": a, "c": uuid4(), "w": wid_a})
    async with tenant_session_scope(b) as s:
        await s.execute(_INSERT, {"l": uuid4(), "t": b, "c": uuid4(), "w": wid_b})

    # Tenant A sees its own row and NOT tenant B's.
    async with tenant_session_scope(a) as s:
        rows = (await s.execute(text("SELECT workflow_id FROM lifecycle"))).all()
    wids = {r.workflow_id for r in rows}
    assert wid_a in wids
    assert wid_b not in wids

    # No tenant context -> RLS fails closed to zero rows.
    async with tenant_session_scope(None) as s:
        count = (await s.execute(text("SELECT count(*) AS n FROM lifecycle"))).one().n
    assert count == 0


async def test_insert_rejected_without_matching_tenant_context(db: None) -> None:
    a, other = uuid4(), uuid4()
    # WITH CHECK forbids writing a row for a tenant other than the established context.
    with pytest.raises(Exception):  # noqa: B017 -- RLS check violation surfaces as a DB error
        async with tenant_session_scope(a) as s:
            await s.execute(
                _INSERT, {"l": uuid4(), "t": other, "c": uuid4(), "w": f"x-{other.hex}"}
            )


async def test_request_repository_reads_only_through_its_tenant_context(db: None) -> None:
    """Tenant-scoped request reads work for the owner and fail closed for every other tenant (FR-1.3/1.4)."""
    owner, other = uuid4(), uuid4()
    request = EventRequest(
        request_id=uuid4(),
        tenant_id=owner,
        raw_text="quiet jazz after work",
        constraints=RequestConstraints(categories=("music",)),
    )
    repository = PostgresRequestRepository()

    await repository.add(request)

    owned = await repository.get(owner, request.request_id)
    assert owned is not None
    assert owned.tenant_id == owner
    assert owned.raw_text == request.raw_text
    assert owned.constraints == request.constraints
    assert await repository.get(other, request.request_id) is None

    async with tenant_session_scope(None) as session:
        count = (
            (
                await session.execute(
                    text("SELECT count(*) AS n FROM event_requests WHERE request_id = :request_id"),
                    {"request_id": request.request_id},
                )
            )
            .one()
            .n
        )
    assert count == 0
