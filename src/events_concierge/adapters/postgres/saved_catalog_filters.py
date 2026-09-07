"""PostgreSQL store for a tenant's named catalog filter selections.

A satellite of ``public.tenants`` under FORCE row-level security, written directly by the
application: a saved filter is presentation state, so it does not reopen the insert-once identity
surface ``public.tenants`` deliberately keeps closed (0061, ADR-011).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ...infra.db import tenant_session_scope
from ...ports.saved_catalog_filters import (
    MAX_SAVED_CATALOG_FILTERS,
    SavedCatalogFilter,
)

_COLUMNS = "saved_filter_id, name, filters, created_at, updated_at, last_used_at"


def _row_to_filter(row: Any) -> SavedCatalogFilter:
    payload = row["filters"]
    return SavedCatalogFilter(
        saved_filter_id=row["saved_filter_id"],
        name=str(row["name"]),
        # psycopg returns jsonb already decoded; a text driver would not.
        payload=payload if isinstance(payload, dict) else json.loads(payload),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
    )


class PostgresSavedCatalogFilterRepository:
    """Persist one tenant's saved filter selections under its FORCE-RLS context."""

    async def list_filters(self, tenant_id: UUID) -> list[SavedCatalogFilter]:
        """Return every saved selection, most recently used first."""
        async with tenant_session_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {_COLUMNS}
                                FROM public.saved_catalog_filters
                                WHERE tenant_id = :tenant_id
                                ORDER BY last_used_at DESC, saved_filter_id
                                LIMIT :limit"""
                        ),
                        {"tenant_id": tenant_id, "limit": MAX_SAVED_CATALOG_FILTERS},
                    )
                )
                .mappings()
                .all()
            )
        return [_row_to_filter(row) for row in rows]

    async def save_filter(
        self,
        tenant_id: UUID,
        *,
        name: str,
        payload: dict[str, Any],
        saved_filter_id: UUID | None = None,
    ) -> SavedCatalogFilter:
        """Create a selection, or replace the one identified by ``saved_filter_id``."""
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with tenant_session_scope(tenant_id) as session:
            if saved_filter_id is None:
                # The cap is read inside the tenant's own transaction, so a concurrent save can
                # at worst overshoot by the number of racing requests rather than run away.
                stored = int(
                    (
                        await session.execute(
                            text(
                                """SELECT count(*) FROM public.saved_catalog_filters
                                   WHERE tenant_id = :tenant_id"""
                            ),
                            {"tenant_id": tenant_id},
                        )
                    ).scalar_one()
                )
                if stored >= MAX_SAVED_CATALOG_FILTERS:
                    raise ValueError("tenant has too many saved catalog filters")
            statement = (
                f"""INSERT INTO public.saved_catalog_filters (tenant_id, name, filters)
                    VALUES (:tenant_id, :name, CAST(:filters AS jsonb))
                    RETURNING {_COLUMNS}"""
                if saved_filter_id is None
                else f"""UPDATE public.saved_catalog_filters
                         SET name = :name,
                             filters = CAST(:filters AS jsonb),
                             updated_at = clock_timestamp()
                         WHERE tenant_id = :tenant_id AND saved_filter_id = :saved_filter_id
                         RETURNING {_COLUMNS}"""
            )
            parameters: dict[str, Any] = {
                "tenant_id": tenant_id,
                "name": name,
                "filters": encoded,
            }
            if saved_filter_id is not None:
                parameters["saved_filter_id"] = saved_filter_id
            try:
                row = (
                    (await session.execute(text(statement), parameters)).mappings().one_or_none()
                )
            except IntegrityError as error:
                # Every other constraint on this table mirrors a bound the API already enforces,
                # so hitting one is a bug here rather than something to blame on the name.
                if getattr(getattr(error, "orig", None), "sqlstate", None) != "23505":
                    raise
                raise ValueError("saved catalog filter name is already used") from error
        if row is None:
            raise ValueError("saved catalog filter not found")
        return _row_to_filter(row)

    async def touch_filter(self, tenant_id: UUID, saved_filter_id: UUID) -> SavedCatalogFilter:
        """Record that a selection was applied, without counting it as an edit."""
        async with tenant_session_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""UPDATE public.saved_catalog_filters
                                SET last_used_at = clock_timestamp()
                                WHERE tenant_id = :tenant_id
                                  AND saved_filter_id = :saved_filter_id
                                RETURNING {_COLUMNS}"""
                        ),
                        {"tenant_id": tenant_id, "saved_filter_id": saved_filter_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ValueError("saved catalog filter not found")
        return _row_to_filter(row)

    async def delete_filter(self, tenant_id: UUID, saved_filter_id: UUID) -> bool:
        """Remove a selection. Returns False when the tenant has no such selection."""
        async with tenant_session_scope(tenant_id) as session:
            removed = (
                await session.execute(
                    text(
                        """DELETE FROM public.saved_catalog_filters
                           WHERE tenant_id = :tenant_id AND saved_filter_id = :saved_filter_id
                           RETURNING saved_filter_id"""
                    ),
                    {"tenant_id": tenant_id, "saved_filter_id": saved_filter_id},
                )
            ).scalar_one_or_none()
        return removed is not None
