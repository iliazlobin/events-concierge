"""RLS coverage for Google Calendar sync cursor/channel state (FR-1.3, FR-9.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from events_concierge.adapters.postgres.calendar_sync import PostgresGoogleCalendarSyncState
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.credentials import Tenant
from events_concierge.infra.db import tenant_session_scope
from events_concierge.ports.google_calendar import (
    GoogleCalendarChannelState,
    GoogleCalendarWatch,
    GoogleCalendarWatchRequest,
)

pytestmark = pytest.mark.integration


async def _add_tenant(tenant_id: UUID) -> None:
    tag = f"calendar-sync-{tenant_id.hex}"
    await PostgresTenantRepository().add(
        Tenant(tenant_id, tag, f"{tag}@example.test", f"{tag}@u.example.test")
    )


def _channel(channel_id: str, token: str) -> GoogleCalendarChannelState:
    request = GoogleCalendarWatchRequest(
        channel_id=channel_id,
        channel_token=token,
        callback_url="https://callbacks.example.test/google-calendar",
    )
    watch = GoogleCalendarWatch(
        channel_id=channel_id,
        resource_id=f"resource-{channel_id}",
        expires_at=datetime(2026, 7, 16, 18, 0, tzinfo=UTC),
    )
    return GoogleCalendarChannelState.from_watch(request, watch)


async def test_google_calendar_sync_state_is_rls_isolated_and_stores_only_token_digest(
    db: None,
) -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    tag = uuid4().hex
    calendar_a, calendar_b = f"calendar-sync-a-{tag}", f"calendar-sync-b-{tag}"
    await _add_tenant(tenant_a)
    await _add_tenant(tenant_b)
    states = PostgresGoogleCalendarSyncState()

    channel_a = _channel(f"channel-sync-a-{tag}", f"raw-callback-token-a-{tag}")
    channel_b = _channel(f"channel-sync-b-{tag}", f"raw-callback-token-b-{tag}")
    await states.store_sync_token(tenant_a, calendar_a, f"sync-token-a-{tag}")
    await states.store_channel(tenant_a, calendar_a, channel_a)
    await states.store_sync_token(tenant_b, calendar_b, f"sync-token-b-{tag}")
    await states.store_channel(tenant_b, calendar_b, channel_b)

    state_a = await states.get_state(tenant_a, calendar_a)
    assert state_a is not None
    assert state_a.sync_token == f"sync-token-a-{tag}"
    assert state_a.channel == channel_a
    assert state_a.channel.channel_token_digest != f"raw-callback-token-a-{tag}"

    async with tenant_session_scope(tenant_a) as session:
        visible = (
            await session.execute(text("SELECT tenant_id FROM google_calendar_sync_state"))
        ).all()
    assert [row.tenant_id for row in visible] == [tenant_a]

    async with tenant_session_scope(None) as session:
        count = (
            (await session.execute(text("SELECT count(*) AS n FROM google_calendar_sync_state")))
            .one()
            .n
        )
    assert count == 0
