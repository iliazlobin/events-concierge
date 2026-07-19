"""Postgres GoogleCalendarBindingPort adapter with tenant-scoped FORCE RLS (FR-1.3/9.6)."""

from __future__ import annotations

import json
from typing import cast
from uuid import UUID

from sqlalchemy import text

from ...infra.db import tenant_session_scope
from ...ports.google_calendar import GoogleCalendarBinding


class PostgresGoogleCalendarBindings:
    """Store non-secret Google calendar IDs under the same RLS context as lifecycle data."""

    async def get_binding(self, tenant_id: UUID) -> GoogleCalendarBinding | None:
        async with tenant_session_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """SELECT write_calendar_id, free_busy_calendar_ids
                           FROM calendar_bindings WHERE tenant_id = :tenant_id"""
                    ),
                    {"tenant_id": tenant_id},
                )
            ).first()
        if row is None:
            return None
        return GoogleCalendarBinding(
            write_calendar_id=str(row.write_calendar_id),
            free_busy_calendar_ids=_calendar_ids(row.free_busy_calendar_ids),
        )

    async def upsert_binding(self, tenant_id: UUID, binding: GoogleCalendarBinding) -> None:
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """INSERT INTO calendar_bindings
                            (tenant_id, write_calendar_id, free_busy_calendar_ids)
                       VALUES (:tenant_id, :write_calendar_id, CAST(:free_busy_calendar_ids AS jsonb))
                       ON CONFLICT (tenant_id) DO UPDATE
                       SET write_calendar_id = EXCLUDED.write_calendar_id,
                           free_busy_calendar_ids = EXCLUDED.free_busy_calendar_ids,
                           updated_at = now()"""
                ),
                {
                    "tenant_id": tenant_id,
                    "write_calendar_id": binding.write_calendar_id,
                    "free_busy_calendar_ids": json.dumps(list(binding.free_busy_calendar_ids)),
                },
            )


def _calendar_ids(value: object) -> tuple[str, ...]:
    """Normalize JSONB drivers that return either decoded lists or JSON strings."""
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise ValueError("calendar_bindings.free_busy_calendar_ids must be a JSON string array")
    return tuple(cast("list[str]", decoded))
