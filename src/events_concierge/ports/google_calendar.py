"""Narrow OAuth, binding, and sync seams for Google Calendar (FR-9.1/9.4/9.5/9.6).

The CalendarPort adapter receives only a short-lived bearer token and an already-provisioned,
tenant-scoped app-calendar binding.  OAuth consent, token refresh, and secondary-calendar creation
remain outside this offline-safe increment and must be enabled only after owner provisioning.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol
from urllib.parse import urlparse
from uuid import UUID

# Intentionally calendar-family only; no Gmail, Drive, Chat, or Photos scope is permitted (FR-9.1).
GOOGLE_CALENDAR_SCOPES: Final[tuple[str, str]] = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
)
GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY: Final = "events_concierge.canonical_event_id"
_GOOGLE_CALENDAR_CHANNEL_ID_MAX_LENGTH: Final = 64
_SHA256_HEX_LENGTH: Final = 64
_GOOGLE_CALENDAR_WEBHOOK_STATES: Final[frozenset[str]] = frozenset({"sync", "exists", "not_exists"})


class GoogleCalendarSyncTokenExpiredError(RuntimeError):
    """Google invalidated an incremental cursor; callers must reset then perform a full sync (FR-9.4)."""


class GoogleCalendarSyncProtocolError(RuntimeError):
    """A provider response cannot safely advance the local sync cursor (FR-9.4)."""


@dataclass(frozen=True, slots=True)
class GoogleCalendarAccess:
    """One short-lived bearer token resolved behind the credential/vault boundary."""

    bearer_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.bearer_token, str) or not self.bearer_token.strip():
            raise ValueError("Google Calendar bearer token must not be empty")


@dataclass(frozen=True, slots=True)
class GoogleCalendarBinding:
    """Tenant-local app-calendar write target plus explicit conflict-gate calendar IDs (FR-9.6)."""

    write_calendar_id: str
    free_busy_calendar_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.write_calendar_id, str) or not self.write_calendar_id.strip():
            raise ValueError("Google Calendar write_calendar_id must not be empty")
        if self.write_calendar_id.strip().lower() == "primary":
            raise ValueError(
                "Google Calendar write_calendar_id must be an app-created secondary calendar"
            )
        if not self.free_busy_calendar_ids:
            raise ValueError("Google Calendar free_busy_calendar_ids must not be empty")
        if any(
            not isinstance(calendar_id, str) or not calendar_id.strip()
            for calendar_id in self.free_busy_calendar_ids
        ):
            raise ValueError("Google Calendar free_busy_calendar_ids must not contain empty IDs")
        if len(set(self.free_busy_calendar_ids)) != len(self.free_busy_calendar_ids):
            raise ValueError("Google Calendar free_busy_calendar_ids must not contain duplicates")
        if self.write_calendar_id not in self.free_busy_calendar_ids:
            raise ValueError("Google Calendar write calendar must be included in free/busy IDs")


class GoogleCalendarAccessPort(Protocol):
    """Resolve an authenticated Google access token without exposing refresh-token storage."""

    async def get_access(self, tenant_id: UUID) -> GoogleCalendarAccess:
        """Return the tenant's valid Calendar API bearer token (FR-2.2/9.1)."""
        ...


class GoogleCalendarBindingPort(Protocol):
    """Persist the pre-provisioned app calendar and explicit free/busy set under RLS (FR-9.6)."""

    async def get_binding(self, tenant_id: UUID) -> GoogleCalendarBinding | None:
        """Return no binding until the owner-gated Google onboarding has completed."""
        ...

    async def upsert_binding(self, tenant_id: UUID, binding: GoogleCalendarBinding) -> None:
        """Persist a tenant's already-created app calendar and selected conflict calendars."""
        ...


@dataclass(frozen=True, slots=True)
class GoogleCalendarSyncChange:
    """One provider event returned by a full or token-delta Events.list page (FR-9.4)."""

    provider_event_id: str
    status: str
    version: str | None
    canonical_event_id: UUID | None
    summary: str | None
    start_at: datetime | None
    end_at: datetime | None

    def __post_init__(self) -> None:
        if not self.provider_event_id.strip():
            raise ValueError("Google Calendar sync change provider_event_id must not be empty")
        if not self.status.strip():
            raise ValueError("Google Calendar sync change status must not be empty")
        for name, moment in (("start_at", self.start_at), ("end_at", self.end_at)):
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                raise ValueError(f"Google Calendar sync change {name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class GoogleCalendarSyncPage:
    """One complete Google Events.list page, preserving its continuation contract (FR-9.4)."""

    changes: tuple[GoogleCalendarSyncChange, ...]
    next_page_token: str | None
    next_sync_token: str | None

    def __post_init__(self) -> None:
        for name, token in (
            ("next_page_token", self.next_page_token),
            ("next_sync_token", self.next_sync_token),
        ):
            if token is not None and not token.strip():
                raise ValueError(f"Google Calendar {name} must not be empty when present")
        if self.next_page_token is not None and self.next_sync_token is not None:
            raise ValueError(
                "Google Calendar sync page cannot carry both continuation and sync tokens"
            )


@dataclass(frozen=True, slots=True)
class GoogleCalendarWatchRequest:
    """A caller-minted channel identity and HTTPS callback for Events.watch (FR-9.4)."""

    channel_id: str
    channel_token: str
    callback_url: str

    def __post_init__(self) -> None:
        if (
            not self.channel_id.strip()
            or len(self.channel_id) > _GOOGLE_CALENDAR_CHANNEL_ID_MAX_LENGTH
        ):
            raise ValueError(
                "Google Calendar channel_id must be non-empty and at most 64 characters"
            )
        if not self.channel_token.strip():
            raise ValueError("Google Calendar channel_token must not be empty")
        parsed = urlparse(self.callback_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Google Calendar webhook callback_url must use HTTPS")


@dataclass(frozen=True, slots=True)
class GoogleCalendarWatch:
    """The provider-issued resource identity and expiry for one Events.watch channel (FR-9.4)."""

    channel_id: str
    resource_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.channel_id.strip() or not self.resource_id.strip():
            raise ValueError("Google Calendar watch identifiers must not be empty")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("Google Calendar watch expiration must be timezone-aware")


@dataclass(frozen=True, slots=True)
class GoogleCalendarWebhookNotification:
    """The authenticated header-only trigger Google sends for a watched event collection (FR-9.4)."""

    channel_id: str
    resource_id: str
    channel_token: str
    resource_state: str
    message_number: int

    def __post_init__(self) -> None:
        if (
            not self.channel_id.strip()
            or not self.resource_id.strip()
            or not self.channel_token.strip()
        ):
            raise ValueError("Google Calendar webhook identifiers and token must not be empty")
        if self.resource_state not in _GOOGLE_CALENDAR_WEBHOOK_STATES:
            raise ValueError("Google Calendar webhook resource state is not recognized")
        if self.message_number < 1:
            raise ValueError("Google Calendar webhook message_number must be positive")

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> GoogleCalendarWebhookNotification:
        """Parse exactly the headers required to validate a content-free Google push trigger."""
        normalized: dict[str, str] = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("Google Calendar webhook headers must be string pairs")
            normalized[name.lower()] = value.strip()
        message_number = normalized.get("x-goog-message-number", "")
        if not message_number.isdecimal():
            raise ValueError("Google Calendar webhook message number must be decimal")
        return cls(
            channel_id=_required_header(normalized, "x-goog-channel-id"),
            resource_id=_required_header(normalized, "x-goog-resource-id"),
            channel_token=_required_header(normalized, "x-goog-channel-token"),
            resource_state=_required_header(normalized, "x-goog-resource-state"),
            message_number=int(message_number),
        )


@dataclass(frozen=True, slots=True)
class GoogleCalendarChannelState:
    """The non-secret channel fields persisted to validate push triggers and renew by expiry (FR-9.4)."""

    channel_id: str
    resource_id: str
    channel_token_digest: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.channel_id.strip() or not self.resource_id.strip():
            raise ValueError("Google Calendar channel state identifiers must not be empty")
        if len(self.channel_token_digest) != _SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in self.channel_token_digest
        ):
            raise ValueError(
                "Google Calendar channel token digest must be a lowercase SHA-256 hex digest"
            )
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("Google Calendar channel expiration must be timezone-aware")

    @classmethod
    def from_watch(
        cls, request: GoogleCalendarWatchRequest, watch: GoogleCalendarWatch
    ) -> GoogleCalendarChannelState:
        """Persist only a digest of the callback bearer token, never the token itself (FR-9.4)."""
        if request.channel_id != watch.channel_id:
            raise ValueError("Google Calendar watch response channel_id does not match its request")
        return cls(
            channel_id=watch.channel_id,
            resource_id=watch.resource_id,
            channel_token_digest=google_calendar_channel_token_digest(request.channel_token),
            expires_at=watch.expires_at,
        )

    def accepts(self, notification: GoogleCalendarWebhookNotification) -> bool:
        """Return true only when all opaque channel identities match in constant time where secret."""
        return (
            self.channel_id == notification.channel_id
            and self.resource_id == notification.resource_id
            and hmac.compare_digest(
                self.channel_token_digest,
                google_calendar_channel_token_digest(notification.channel_token),
            )
        )

    def renewal_due_at(self, lead: timedelta) -> datetime:
        """Derive renewal scheduling from Google's returned expiry, never a guessed channel TTL (FR-9.4)."""
        if lead < timedelta(0):
            raise ValueError("Google Calendar renewal lead must not be negative")
        return self.expires_at - lead


@dataclass(frozen=True, slots=True)
class GoogleCalendarSyncState:
    """RLS-scoped sync cursor and optional active push channel for one tenant calendar (FR-1.3/9.4)."""

    tenant_id: UUID
    calendar_id: str
    sync_token: str | None
    channel: GoogleCalendarChannelState | None

    def __post_init__(self) -> None:
        if not self.calendar_id.strip():
            raise ValueError("Google Calendar sync state calendar_id must not be empty")
        if self.sync_token is not None and not self.sync_token.strip():
            raise ValueError("Google Calendar sync token must not be empty when present")


def google_calendar_channel_token_digest(channel_token: str) -> str:
    """Hash one callback bearer token before persistence or comparison (FR-9.4)."""
    if not channel_token.strip():
        raise ValueError("Google Calendar channel token must not be empty")
    return hashlib.sha256(channel_token.encode("utf-8")).hexdigest()


def _required_header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name, "")
    if not value:
        raise ValueError(f"Google Calendar webhook is missing {name}")
    return value


class GoogleCalendarSyncPort(Protocol):
    """Translate provider Events.list/Events.watch calls behind fixture-injectable contracts (FR-9.4)."""

    async def list_events(
        self,
        tenant_id: UUID,
        calendar_id: str,
        *,
        sync_token: str | None,
        page_token: str | None,
    ) -> GoogleCalendarSyncPage:
        """Fetch one full or incremental Events.list page, preserving the supplied cursor query."""
        ...

    async def watch_events(
        self,
        tenant_id: UUID,
        calendar_id: str,
        request: GoogleCalendarWatchRequest,
    ) -> GoogleCalendarWatch:
        """Create one HTTPS Events.watch channel and return its provider resource/expiration."""
        ...


class GoogleCalendarSyncStatePort(Protocol):
    """Persist per-tenant, per-calendar cursors and push-channel metadata under FORCE RLS (FR-1.3/9.4)."""

    async def get_state(self, tenant_id: UUID, calendar_id: str) -> GoogleCalendarSyncState | None:
        """Load this tenant's state or no state before its initial full sync."""
        ...

    async def store_sync_token(self, tenant_id: UUID, calendar_id: str, sync_token: str) -> None:
        """Durably advance the cursor only after the caller applied the whole page sequence."""
        ...

    async def clear_sync_token(self, tenant_id: UUID, calendar_id: str) -> None:
        """Forget an invalidated cursor after its idempotent local projection reset (FR-9.4)."""
        ...

    async def store_channel(
        self,
        tenant_id: UUID,
        calendar_id: str,
        channel: GoogleCalendarChannelState,
    ) -> None:
        """Replace active channel metadata after a successful watch response."""
        ...


class GoogleCalendarSyncSink(Protocol):
    """Idempotently project Google calendar changes; a cursor advances only after this sink succeeds (FR-9.4)."""

    async def reset_calendar(self, tenant_id: UUID, calendar_id: str) -> None:
        """Wipe the local calendar projection before the server-required full resync."""
        ...

    async def apply_changes(
        self,
        tenant_id: UUID,
        calendar_id: str,
        changes: tuple[GoogleCalendarSyncChange, ...],
    ) -> None:
        """Apply a complete full/delta sequence idempotently before its new token is stored."""
        ...
