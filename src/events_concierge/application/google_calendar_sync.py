"""Durable-cursor orchestration for Google webhook-triggered calendar sync (FR-9.4).

The provider adapter is intentionally limited to Events.list/Events.watch translation.  This
application service owns the important ordering: apply a complete page sequence to an idempotent
sink before storing its new ``syncToken``; on HTTP 410, clear both the old cursor and local
projection before performing a new full sync.  The outer HTTP receiver and scheduler remain
unwired until owner-gated Google callback deployment is configured.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from ..ports.google_calendar import (
    GoogleCalendarBinding,
    GoogleCalendarBindingPort,
    GoogleCalendarChannelState,
    GoogleCalendarSyncChange,
    GoogleCalendarSyncPort,
    GoogleCalendarSyncSink,
    GoogleCalendarSyncStatePort,
    GoogleCalendarSyncTokenExpiredError,
    GoogleCalendarWatchRequest,
    GoogleCalendarWebhookNotification,
)


class GoogleCalendarSyncBindingNotFoundError(RuntimeError):
    """A tenant has no app-calendar binding from which FR-9.4 can sync safely."""


class GoogleCalendarSyncInvariantError(RuntimeError):
    """A port returned an incomplete page sequence that cannot advance a durable cursor (FR-9.4)."""


class GoogleCalendarSyncMode(StrEnum):
    """The one complete sync path that produced a new durable cursor (FR-9.4)."""

    INITIAL_FULL = "initial_full"
    INCREMENTAL = "incremental"
    FULL_AFTER_TOKEN_RESET = "full_after_token_reset"


@dataclass(frozen=True, slots=True)
class GoogleCalendarSyncResult:
    """A completed, cursor-committed sync cycle for logs and fixture tests (FR-9.4)."""

    tenant_id: UUID
    calendar_id: str
    mode: GoogleCalendarSyncMode
    pages: int
    changes_applied: int
    sync_token: str


class GoogleCalendarIncrementalSyncService:
    """Apply full or token-delta Google changes with reset-on-410 ordering (FR-9.4, AC-67)."""

    def __init__(
        self,
        bindings: GoogleCalendarBindingPort,
        states: GoogleCalendarSyncStatePort,
        provider: GoogleCalendarSyncPort,
        sink: GoogleCalendarSyncSink,
    ) -> None:
        self._bindings = bindings
        self._states = states
        self._provider = provider
        self._sink = sink

    async def sync_tenant(self, tenant_id: UUID) -> GoogleCalendarSyncResult:
        """Sync the bound app calendar, retaining its old cursor until a full sink apply succeeds."""
        binding = await self._binding(tenant_id)
        calendar_id = binding.write_calendar_id
        state = await self._states.get_state(tenant_id, calendar_id)
        sync_token = state.sync_token if state is not None else None
        mode = (
            GoogleCalendarSyncMode.INCREMENTAL
            if sync_token is not None
            else GoogleCalendarSyncMode.INITIAL_FULL
        )
        try:
            return await self._sync(
                tenant_id,
                calendar_id,
                sync_token=sync_token,
                mode=mode,
            )
        except GoogleCalendarSyncTokenExpiredError:
            if sync_token is None:
                raise
            # Google explicitly requires a full local wipe for 410, rather than treating the
            # resulting full page as an ordinary delta (FR-9.4, AC-67).  The reset must happen
            # before forgetting the invalid cursor: if this process dies before the cursor clear,
            # the retry receives 410 again and repeats the idempotent reset.  Reversing that
            # ordering could leave an initial sync to merge with stale projected events.
            await self._sink.reset_calendar(tenant_id, calendar_id)
            await self._states.clear_sync_token(tenant_id, calendar_id)
            return await self._sync(
                tenant_id,
                calendar_id,
                sync_token=None,
                mode=GoogleCalendarSyncMode.FULL_AFTER_TOKEN_RESET,
            )

    async def _sync(
        self,
        tenant_id: UUID,
        calendar_id: str,
        *,
        sync_token: str | None,
        mode: GoogleCalendarSyncMode,
    ) -> GoogleCalendarSyncResult:
        """Read every page under one cursor, then commit sink effects and the returned replacement cursor."""
        page_token: str | None = None
        changes: list[GoogleCalendarSyncChange] = []
        pages = 0
        while True:
            page = await self._provider.list_events(
                tenant_id,
                calendar_id,
                sync_token=sync_token,
                page_token=page_token,
            )
            pages += 1
            changes.extend(page.changes)
            if page.next_page_token is not None:
                page_token = page.next_page_token
                continue
            if page.next_sync_token is None:
                raise GoogleCalendarSyncInvariantError(
                    "Google Calendar final sync page did not provide nextSyncToken"
                )
            await self._sink.apply_changes(tenant_id, calendar_id, tuple(changes))
            await self._states.store_sync_token(tenant_id, calendar_id, page.next_sync_token)
            return GoogleCalendarSyncResult(
                tenant_id=tenant_id,
                calendar_id=calendar_id,
                mode=mode,
                pages=pages,
                changes_applied=len(changes),
                sync_token=page.next_sync_token,
            )

    async def _binding(self, tenant_id: UUID) -> GoogleCalendarBinding:
        binding = await self._bindings.get_binding(tenant_id)
        if binding is None:
            raise GoogleCalendarSyncBindingNotFoundError(
                "Google app-calendar binding is not provisioned"
            )
        return binding


class GoogleCalendarWebhookOutcome(StrEnum):
    """A receiver result that remains truthful about validation and successful downstream sync (FR-9.4)."""

    REJECTED = "rejected"
    SYNCED = "synced"


@dataclass(frozen=True, slots=True)
class GoogleCalendarWebhookResult:
    """One header-only webhook validation result, with a sync only after channel authentication (FR-9.4)."""

    outcome: GoogleCalendarWebhookOutcome
    sync: GoogleCalendarSyncResult | None = None


class GoogleCalendarWebhookService:
    """Validate an active tenant channel before turning a body-free push into a token sync (FR-9.4)."""

    def __init__(
        self,
        bindings: GoogleCalendarBindingPort,
        states: GoogleCalendarSyncStatePort,
        sync: GoogleCalendarIncrementalSyncService,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._bindings = bindings
        self._states = states
        self._sync = sync
        self._now = now or (lambda: datetime.now(UTC))

    async def handle(
        self, tenant_id: UUID, headers: Mapping[str, str]
    ) -> GoogleCalendarWebhookResult:
        """Reject malformed/stale/forged triggers before any Google Events.list call (FR-9.4)."""
        try:
            notification = GoogleCalendarWebhookNotification.from_headers(headers)
        except ValueError:
            return GoogleCalendarWebhookResult(GoogleCalendarWebhookOutcome.REJECTED)
        binding = await self._bindings.get_binding(tenant_id)
        if binding is None:
            return GoogleCalendarWebhookResult(GoogleCalendarWebhookOutcome.REJECTED)
        state = await self._states.get_state(tenant_id, binding.write_calendar_id)
        channel = state.channel if state is not None else None
        now = self._now()
        if channel is None or channel.expires_at <= now or not channel.accepts(notification):
            return GoogleCalendarWebhookResult(GoogleCalendarWebhookOutcome.REJECTED)
        result = await self._sync.sync_tenant(tenant_id)
        return GoogleCalendarWebhookResult(GoogleCalendarWebhookOutcome.SYNCED, sync=result)


@dataclass(frozen=True, slots=True)
class GoogleCalendarChannelRenewalResult:
    """One renewal check; the next deadline is derived solely from Google's returned expiration (FR-9.4)."""

    tenant_id: UUID
    calendar_id: str
    renewed: bool
    renewal_due_at: datetime | None


class GoogleCalendarChannelRenewalWorker:
    """Perform one explicit tenant renewal check; an outer scheduler owns tenant enumeration (FR-9.4)."""

    def __init__(
        self,
        bindings: GoogleCalendarBindingPort,
        states: GoogleCalendarSyncStatePort,
        provider: GoogleCalendarSyncPort,
        channel_factory: Callable[[], GoogleCalendarWatchRequest],
        *,
        renewal_lead: timedelta = timedelta(minutes=15),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if renewal_lead < timedelta(0):
            raise ValueError("Google Calendar renewal_lead must not be negative")
        self._bindings = bindings
        self._states = states
        self._provider = provider
        self._channel_factory = channel_factory
        self._renewal_lead = renewal_lead
        self._now = now or (lambda: datetime.now(UTC))

    async def renew_if_due(self, tenant_id: UUID) -> GoogleCalendarChannelRenewalResult:
        """Create a replacement only when absent or due from returned expiration, never a guessed TTL."""
        binding = await self._bindings.get_binding(tenant_id)
        if binding is None:
            raise GoogleCalendarSyncBindingNotFoundError(
                "Google app-calendar binding is not provisioned"
            )
        calendar_id = binding.write_calendar_id
        state = await self._states.get_state(tenant_id, calendar_id)
        channel = state.channel if state is not None else None
        if channel is not None:
            due_at = channel.renewal_due_at(self._renewal_lead)
            if due_at > self._now():
                return GoogleCalendarChannelRenewalResult(
                    tenant_id=tenant_id,
                    calendar_id=calendar_id,
                    renewed=False,
                    renewal_due_at=due_at,
                )

        request = self._channel_factory()
        watch = await self._provider.watch_events(tenant_id, calendar_id, request)
        replacement = GoogleCalendarChannelState.from_watch(request, watch)
        await self._states.store_channel(tenant_id, calendar_id, replacement)
        return GoogleCalendarChannelRenewalResult(
            tenant_id=tenant_id,
            calendar_id=calendar_id,
            renewed=True,
            renewal_due_at=replacement.renewal_due_at(self._renewal_lead),
        )
