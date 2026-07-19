"""Offline doubles for the Google Calendar FR-9.4 cursor and change-projection ports."""

from __future__ import annotations

from uuid import UUID

from ...ports.google_calendar import (
    GoogleCalendarChannelState,
    GoogleCalendarSyncChange,
    GoogleCalendarSyncSink,
    GoogleCalendarSyncState,
    GoogleCalendarSyncStatePort,
)


class MockGoogleCalendarSyncState(GoogleCalendarSyncStatePort):
    """Process-local per-tenant cursor/channel store for deterministic offline sync tests (FR-9.4)."""

    def __init__(self) -> None:
        self._states: dict[tuple[UUID, str], GoogleCalendarSyncState] = {}
        self.cleared: list[tuple[UUID, str]] = []

    async def get_state(self, tenant_id: UUID, calendar_id: str) -> GoogleCalendarSyncState | None:
        return self._states.get((tenant_id, calendar_id))

    async def store_sync_token(self, tenant_id: UUID, calendar_id: str, sync_token: str) -> None:
        state = self._state_or_empty(tenant_id, calendar_id)
        self._states[(tenant_id, calendar_id)] = GoogleCalendarSyncState(
            tenant_id=tenant_id,
            calendar_id=calendar_id,
            sync_token=sync_token,
            channel=state.channel,
        )

    async def clear_sync_token(self, tenant_id: UUID, calendar_id: str) -> None:
        state = self._state_or_empty(tenant_id, calendar_id)
        self._states[(tenant_id, calendar_id)] = GoogleCalendarSyncState(
            tenant_id=tenant_id,
            calendar_id=calendar_id,
            sync_token=None,
            channel=state.channel,
        )
        self.cleared.append((tenant_id, calendar_id))

    async def store_channel(
        self,
        tenant_id: UUID,
        calendar_id: str,
        channel: GoogleCalendarChannelState,
    ) -> None:
        state = self._state_or_empty(tenant_id, calendar_id)
        self._states[(tenant_id, calendar_id)] = GoogleCalendarSyncState(
            tenant_id=tenant_id,
            calendar_id=calendar_id,
            sync_token=state.sync_token,
            channel=channel,
        )

    def _state_or_empty(self, tenant_id: UUID, calendar_id: str) -> GoogleCalendarSyncState:
        return self._states.get(
            (tenant_id, calendar_id),
            GoogleCalendarSyncState(
                tenant_id=tenant_id,
                calendar_id=calendar_id,
                sync_token=None,
                channel=None,
            ),
        )


class MockGoogleCalendarSyncSink(GoogleCalendarSyncSink):
    """Idempotent in-memory event projection with reset/batch inspection seams (FR-9.4, AC-67)."""

    def __init__(self) -> None:
        self._events: dict[tuple[UUID, str], dict[str, GoogleCalendarSyncChange]] = {}
        self.resets: list[tuple[UUID, str]] = []
        self.applied_batches: list[tuple[UUID, str, tuple[GoogleCalendarSyncChange, ...]]] = []

    async def reset_calendar(self, tenant_id: UUID, calendar_id: str) -> None:
        self._events.pop((tenant_id, calendar_id), None)
        self.resets.append((tenant_id, calendar_id))

    async def apply_changes(
        self,
        tenant_id: UUID,
        calendar_id: str,
        changes: tuple[GoogleCalendarSyncChange, ...],
    ) -> None:
        events = self._events.setdefault((tenant_id, calendar_id), {})
        for change in changes:
            events[change.provider_event_id] = change
        self.applied_batches.append((tenant_id, calendar_id, changes))

    def events(self, tenant_id: UUID, calendar_id: str) -> list[GoogleCalendarSyncChange]:
        """Expose the current idempotent projection for fixture assertions."""
        return list(self._events.get((tenant_id, calendar_id), {}).values())
