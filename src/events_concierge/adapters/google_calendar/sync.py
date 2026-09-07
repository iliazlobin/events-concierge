"""Fixture-injectable Google Events.list/Events.watch translation for FR-9.4.

Push messages are deliberately not treated as change payloads.  They only wake the application
service, which asks this adapter for a complete ``syncToken`` page sequence and commits a new
cursor after its idempotent sink succeeds.  No OAuth consent, callback deployment, or live network
setup occurs here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote
from uuid import UUID

import httpx

from ...ports.google_calendar import (
    GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY,
    GoogleCalendarAccess,
    GoogleCalendarAccessPort,
    GoogleCalendarSyncChange,
    GoogleCalendarSyncPage,
    GoogleCalendarSyncPort,
    GoogleCalendarSyncProtocolError,
    GoogleCalendarSyncTokenExpiredError,
    GoogleCalendarWatch,
    GoogleCalendarWatchRequest,
)
from .calendar import raise_for_google_calendar_error

_DEFAULT_BASE_URL: Final = "https://www.googleapis.com/calendar/v3"
_SYNC_PAGE_SIZE: Final = "2500"
_MIN_TIMEOUT_SECONDS: Final = 0.1
_MAX_TIMEOUT_SECONDS: Final = 60.0


class GoogleCalendarSyncAdapter(GoogleCalendarSyncPort):
    """Translate one tenant's authenticated Google sync/watch requests behind a typed port (FR-9.4)."""

    def __init__(
        self,
        access: GoogleCalendarAccessPort,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_s: float = 10.0,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("Google Calendar base URL must use HTTPS")
        if (
            isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s)
            or not _MIN_TIMEOUT_SECONDS <= timeout_s <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "Google Calendar timeout must be finite and between 0.1 and 60 seconds"
            )
        self._access = access
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def list_events(
        self,
        tenant_id: UUID,
        calendar_id: str,
        *,
        sync_token: str | None,
        page_token: str | None,
    ) -> GoogleCalendarSyncPage:
        """Fetch one exact full/delta query page, preserving a token across pagination (FR-9.4)."""
        _non_empty_calendar_id(calendar_id)
        access = await self._access.get_access(tenant_id)
        params = {
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": _SYNC_PAGE_SIZE,
        }
        if sync_token is not None:
            if not sync_token.strip():
                raise ValueError("Google Calendar sync token must not be empty when present")
            params["syncToken"] = sync_token
        if page_token is not None:
            if not page_token.strip():
                raise ValueError("Google Calendar page token must not be empty when present")
            params["pageToken"] = page_token
        response = await self._request(
            "GET",
            self._events_url(calendar_id),
            access,
            params=params,
        )
        if response.status_code == httpx.codes.GONE and sync_token is not None:
            raise GoogleCalendarSyncTokenExpiredError("Google Calendar sync token has expired")
        _raise_for_error(response)
        return _sync_page(response.json())

    async def watch_events(
        self,
        tenant_id: UUID,
        calendar_id: str,
        request: GoogleCalendarWatchRequest,
    ) -> GoogleCalendarWatch:
        """Create one HTTPS channel and parse only its channel/resource/expiry response (FR-9.4)."""
        _non_empty_calendar_id(calendar_id)
        access = await self._access.get_access(tenant_id)
        response = await self._request(
            "POST",
            f"{self._events_url(calendar_id)}/watch",
            access,
            json_body={
                "id": request.channel_id,
                "type": "web_hook",
                "address": request.callback_url,
                "token": request.channel_token,
            },
        )
        _raise_for_error(response)
        return _watch(response.json(), request.channel_id)

    async def _request(
        self,
        method: str,
        url: str,
        access: GoogleCalendarAccess,
        *,
        params: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access.bearer_token}"}
        if self._client is not None:
            return await self._client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self._timeout_s,
            )
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            return await client.request(method, url, headers=headers, params=params, json=json_body)

    def _events_url(self, calendar_id: str) -> str:
        return f"{self._base_url}/calendars/{quote(calendar_id, safe='')}/events"


def _raise_for_error(response: httpx.Response) -> None:
    raise_for_google_calendar_error(response, operation="Google Calendar sync request")


def _sync_page(payload: object) -> GoogleCalendarSyncPage:
    if not isinstance(payload, Mapping):
        raise GoogleCalendarSyncProtocolError("Google Calendar sync response must be an object")
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise GoogleCalendarSyncProtocolError("Google Calendar sync response items must be a list")
    next_page_token = _optional_string(payload.get("nextPageToken"), "nextPageToken")
    next_sync_token = _optional_string(payload.get("nextSyncToken"), "nextSyncToken")
    try:
        return GoogleCalendarSyncPage(
            changes=tuple(_sync_change(item) for item in items),
            next_page_token=next_page_token,
            next_sync_token=next_sync_token,
        )
    except ValueError as error:
        raise GoogleCalendarSyncProtocolError(
            "Google Calendar sync response has invalid cursors"
        ) from error


def _sync_change(item: object) -> GoogleCalendarSyncChange:
    if not isinstance(item, Mapping):
        raise GoogleCalendarSyncProtocolError("Google Calendar sync item must be an object")
    provider_event_id = item.get("id")
    if not isinstance(provider_event_id, str) or not provider_event_id.strip():
        raise GoogleCalendarSyncProtocolError("Google Calendar sync item must include an event id")
    status = item.get("status")
    if not isinstance(status, str) or not status.strip():
        status = "confirmed"
    version = _optional_string(item.get("etag"), "event etag")
    summary = item.get("summary")
    return GoogleCalendarSyncChange(
        provider_event_id=provider_event_id,
        status=status,
        version=version,
        canonical_event_id=_canonical_event_id(item.get("extendedProperties")),
        summary=summary if isinstance(summary, str) else None,
        start_at=_event_time(item.get("start")),
        end_at=_event_time(item.get("end")),
    )


def _canonical_event_id(value: object) -> UUID | None:
    if not isinstance(value, Mapping):
        return None
    private = value.get("private")
    if not isinstance(private, Mapping):
        return None
    raw = private.get(GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY)
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _event_time(value: object) -> datetime | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("dateTime")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def _watch(payload: object, requested_channel_id: str) -> GoogleCalendarWatch:
    if not isinstance(payload, Mapping):
        raise GoogleCalendarSyncProtocolError("Google Calendar watch response must be an object")
    channel_id = payload.get("id")
    resource_id = payload.get("resourceId")
    if not isinstance(channel_id, str) or channel_id != requested_channel_id:
        raise GoogleCalendarSyncProtocolError(
            "Google Calendar watch response has an unexpected channel id"
        )
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise GoogleCalendarSyncProtocolError(
            "Google Calendar watch response must include resourceId"
        )
    expiration = _watch_expiration(payload.get("expiration"))
    try:
        return GoogleCalendarWatch(
            channel_id=channel_id,
            resource_id=resource_id,
            expires_at=expiration,
        )
    except ValueError as error:
        raise GoogleCalendarSyncProtocolError(
            "Google Calendar watch response is invalid"
        ) from error


def _watch_expiration(value: object) -> datetime:
    raw: int
    if isinstance(value, int) and not isinstance(value, bool):
        raw = value
    elif isinstance(value, str) and value.isdecimal():
        raw = int(value)
    else:
        raise GoogleCalendarSyncProtocolError(
            "Google Calendar watch response must include epoch expiration"
        )
    try:
        return datetime.fromtimestamp(raw / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise GoogleCalendarSyncProtocolError(
            "Google Calendar watch expiration is invalid"
        ) from error


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GoogleCalendarSyncProtocolError(f"Google Calendar {name} must be a non-empty string")
    return value


def _non_empty_calendar_id(calendar_id: str) -> None:
    if not calendar_id.strip():
        raise ValueError("Google Calendar calendar_id must not be empty")
