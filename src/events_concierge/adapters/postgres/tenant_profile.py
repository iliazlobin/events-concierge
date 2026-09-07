"""PostgreSQL tenant display-profile repository.

The table is a satellite of ``public.tenants`` rather than columns on it: 0061 made tenant identity
an insert-once, application-unwritable read model, and display facts must not reopen that surface.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from ...infra.db import tenant_session_scope
from ...ports.tenant_profile import TenantProfile


class PostgresTenantProfileRepository:
    """Persist one tenant's display facts under its FORCE-RLS context."""

    async def get_profile(self, tenant_id: UUID) -> TenantProfile:
        """Return the durable profile, or empty defaults for a tenant that never saved one."""
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """SELECT display_name, time_zone, revision, updated_at
                               FROM public.tenant_profiles
                               WHERE tenant_id = :tenant_id"""
                        ),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return TenantProfile()
        return TenantProfile(
            display_name=row["display_name"],
            time_zone=row["time_zone"],
            revision=int(row["revision"]),
            updated_at=row["updated_at"],
        )

    async def replace_profile(self, tenant_id: UUID, profile: TenantProfile) -> TenantProfile:
        """Replace the whole row when the proposed revision advances the stored one.

        The conditional upsert is the concurrency control: a stale or replayed revision changes
        nothing and leaves the newer stored state in place, which the caller detects by comparing
        the returned revision against what it proposed.
        """
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """INSERT INTO public.tenant_profiles AS current (
                                   tenant_id, display_name, time_zone, revision, updated_at
                               )
                               VALUES (
                                   :tenant_id, :display_name, :time_zone, :revision,
                                   clock_timestamp()
                               )
                               ON CONFLICT (tenant_id) DO UPDATE
                               SET display_name = EXCLUDED.display_name,
                                   time_zone = EXCLUDED.time_zone,
                                   revision = EXCLUDED.revision,
                                   updated_at = clock_timestamp()
                               WHERE current.revision < EXCLUDED.revision
                               RETURNING display_name, time_zone, revision, updated_at"""
                        ),
                        {
                            "tenant_id": tenant_id,
                            "display_name": profile.display_name,
                            "time_zone": profile.time_zone,
                            "revision": profile.revision,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )

        if row is None:
            # The upsert declined: a concurrent writer already holds this revision or a newer one.
            raise ValueError("tenant profile revision conflicts")
        return TenantProfile(
            display_name=row["display_name"],
            time_zone=row["time_zone"],
            revision=int(row["revision"]),
            updated_at=row["updated_at"],
        )
