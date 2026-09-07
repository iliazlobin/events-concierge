"""PostgreSQL operator-role lookup."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from ...infra.db import tenant_session_scope
from ...ports.tenant_roles import TenantRole


class PostgresTenantRoleRepository:
    """Read one tenant's granted role under its FORCE-RLS context."""

    async def get_role(self, tenant_id: UUID) -> TenantRole:
        """Return the granted role; an absent row is an ordinary member, not an error."""
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """SELECT role FROM public.tenant_roles
                               WHERE tenant_id = :tenant_id"""
                        ),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return TenantRole.MEMBER
        try:
            return TenantRole(row["role"])
        except ValueError:
            # A role this build does not know about must never widen access by accident.
            return TenantRole.MEMBER
