"""RLS-scoped durable cursors and push-channel state for Google Calendar incremental sync (FR-1.3/9.4)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import text

from ...infra.db import tenant_session_scope
from ...ports.google_calendar import (
    GoogleCalendarChannelState,
    GoogleCalendarSyncState,
    GoogleCalendarSyncStatePort,
)


class _SyncStateRow(Protocol):
    """Named projection returned by the RLS-scoped state lookup."""

    sync_token: str | None
    channel_id: str | None
    resource_id: str | None
    channel_token_digest: str | None
    channel_expires_at: datetime | None


class PostgresGoogleCalendarSyncState(GoogleCalendarSyncStatePort):
    """Persist one tenant/calendar cursor and channel under FORCE RLS without retaining callback secrets."""

    async def get_state(self, tenant_id: UUID, calendar_id: str) -> GoogleCalendarSyncState | None:
        async with tenant_session_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """SELECT sync_token, channel_id, resource_id, channel_token_digest,
                                  channel_expires_at
                           FROM google_calendar_sync_state
                           WHERE tenant_id = :tenant_id AND calendar_id = :calendar_id"""
                    ),
                    {"tenant_id": tenant_id, "calendar_id": calendar_id},
                )
            ).first()
        if row is None:
            return None
        return _sync_state(tenant_id, calendar_id, cast(_SyncStateRow, row))

    async def store_sync_token(self, tenant_id: UUID, calendar_id: str, sync_token: str) -> None:
        if not sync_token.strip():
            raise ValueError("Google Calendar sync token must not be empty")
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """INSERT INTO google_calendar_sync_state (tenant_id, calendar_id, sync_token)
                       VALUES (:tenant_id, :calendar_id, :sync_token)
                       ON CONFLICT (tenant_id, calendar_id) DO UPDATE
                       SET sync_token = EXCLUDED.sync_token,
                           updated_at = now()"""
                ),
                {
                    "tenant_id": tenant_id,
                    "calendar_id": calendar_id,
                    "sync_token": sync_token,
                },
            )

    async def clear_sync_token(self, tenant_id: UUID, calendar_id: str) -> None:
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """INSERT INTO google_calendar_sync_state (tenant_id, calendar_id, sync_token)
                       VALUES (:tenant_id, :calendar_id, NULL)
                       ON CONFLICT (tenant_id, calendar_id) DO UPDATE
                       SET sync_token = NULL,
                           updated_at = now()"""
                ),
                {"tenant_id": tenant_id, "calendar_id": calendar_id},
            )

    async def store_channel(
        self,
        tenant_id: UUID,
        calendar_id: str,
        channel: GoogleCalendarChannelState,
    ) -> None:
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """INSERT INTO google_calendar_sync_state
                            (tenant_id, calendar_id, channel_id, resource_id,
                             channel_token_digest, channel_expires_at)
                       VALUES
                            (:tenant_id, :calendar_id, :channel_id, :resource_id,
                             :channel_token_digest, :channel_expires_at)
                       ON CONFLICT (tenant_id, calendar_id) DO UPDATE
                       SET channel_id = EXCLUDED.channel_id,
                           resource_id = EXCLUDED.resource_id,
                           channel_token_digest = EXCLUDED.channel_token_digest,
                           channel_expires_at = EXCLUDED.channel_expires_at,
                           updated_at = now()"""
                ),
                {
                    "tenant_id": tenant_id,
                    "calendar_id": calendar_id,
                    "channel_id": channel.channel_id,
                    "resource_id": channel.resource_id,
                    "channel_token_digest": channel.channel_token_digest,
                    "channel_expires_at": channel.expires_at,
                },
            )


def _sync_state(tenant_id: UUID, calendar_id: str, row: _SyncStateRow) -> GoogleCalendarSyncState:
    """Reject partially persisted channel tuples instead of weakening webhook validation (FR-9.4)."""
    sync_token = row.sync_token
    channel_id = row.channel_id
    resource_id = row.resource_id
    channel_token_digest = row.channel_token_digest
    channel_expires_at = row.channel_expires_at
    channel_values = (channel_id, resource_id, channel_token_digest, channel_expires_at)
    if all(value is None for value in channel_values):
        channel = None
    elif (
        channel_id is not None
        and resource_id is not None
        and channel_token_digest is not None
        and channel_expires_at is not None
    ):
        channel = GoogleCalendarChannelState(
            channel_id=channel_id,
            resource_id=resource_id,
            channel_token_digest=channel_token_digest,
            expires_at=channel_expires_at,
        )
    else:
        raise ValueError("google_calendar_sync_state has a partial channel tuple")
    return GoogleCalendarSyncState(
        tenant_id=tenant_id,
        calendar_id=calendar_id,
        sync_token=sync_token,
        channel=channel,
    )
