"""Google Calendar v3 adapter behind CalendarPort (FR-4.5, FR-9.1/9.2/9.3/9.5/9.6).

The adapter is deliberately transport-injectable for offline fixtures.  It consumes an already
provisioned app-calendar binding and short-lived bearer token; OAuth consent, refresh, and calendar
creation are owner-gated integration concerns and are not performed here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ...domain.calendar_dedup import (
    FUZZY_CALENDAR_START_DELTA,
    CalendarFuzzyCandidate,
    fuzzy_calendar_matches,
)
from ...domain.conflict import BusyBlock
from ...ports.calendar import (
    CalendarBindingUnavailableError,
    CalendarEntry,
    CalendarReconsentRequiredError,
)
from ...ports.google_calendar import (
    GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY,
    GOOGLE_CALENDAR_OWNER_KEY,
    GOOGLE_CALENDAR_OWNER_VALUE,
    GoogleCalendarAccess,
    GoogleCalendarAccessPort,
    GoogleCalendarBinding,
    GoogleCalendarBindingPort,
)

_DEFAULT_BASE_URL: Final = "https://www.googleapis.com/calendar/v3"
_FREE_BUSY_TIME_ZONE: Final = "UTC"
_RECONSENT_FORBIDDEN_REASONS: Final[frozenset[str]] = frozenset(
    {
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        "authError",
        "insufficientAuthenticationScopes",
        "insufficientPermissions",
        "invalidCredentials",
    }
)
_RETRYABLE_RATE_OR_QUOTA_REASONS: Final[frozenset[str]] = frozenset(
    {
        "dailyLimitExceeded",
        "quotaExceeded",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    }
)
_RETRYABLE_GOOGLE_STATUSES: Final[frozenset[str]] = frozenset({"RESOURCE_EXHAUSTED"})
_ERASURE_PAGE_SIZE: Final = 250
_MAX_ERASURE_PAGES: Final = 100
_MIN_TIMEOUT_SECONDS: Final = 0.1
_MAX_TIMEOUT_SECONDS: Final = 60.0


class GoogleCalendarError(RuntimeError):
    """A Google Calendar API response that cannot safely be treated as a successful effect."""


class GoogleCalendarBindingNotFoundError(GoogleCalendarError, CalendarBindingUnavailableError):
    """The tenant has not completed owner-gated Google app-calendar provisioning (FR-9.6)."""


class GoogleCalendarReconsentRequiredError(GoogleCalendarError, CalendarReconsentRequiredError):
    """Google rejected Calendar authorization; callers must surface one explicit re-consent (FR-9.7)."""


class GoogleCalendarRetryableError(GoogleCalendarError):
    """Google applied transient Calendar rate or quota pressure; retry without prompting consent (FR-9.7)."""


class GoogleCalendarAmbiguousMatchError(GoogleCalendarError):
    """A canonical or fuzzy lookup has no single safe event to mutate (FR-9.3)."""


@dataclass(frozen=True, slots=True)
class _ListedCalendarEvent:
    """A validated minimum Google Events.list record used only for duplicate resolution."""

    event_id: str
    summary: str | None
    start_at: datetime | None
    location: str | None
    is_cancelled: bool
    is_concierge_owned: bool


class GoogleCalendarAdapter:
    """CalendarPort implementation for an explicitly bound Google app calendar (FR-9.5/9.6).

    ``upsert_event`` first resolves the canonical private property, then only uses FR-9.3's narrow
    fuzzy secondary for an unkeyed human-created event.  It refuses ambiguous results.  With no
    match, ``insert`` creates the deterministic event ID and only an HTTP 409 proceeds to ``patch``;
    a write retry therefore converges without guessing whether a remote effect occurred (FR-9.2,
    FR-9.3, NFR-8).  Default composition remains on ``MockCalendar``; this adapter is opt-in by
    injection.
    """

    def __init__(
        self,
        access: GoogleCalendarAccessPort,
        bindings: GoogleCalendarBindingPort,
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
        self._bindings = bindings
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def free_busy(
        self, tenant_id: UUID, window_start: datetime, window_end: datetime
    ) -> list[BusyBlock]:
        """Query only the binding's explicit calendar IDs for the mandatory conflict gate (FR-4.5)."""
        _validate_window(window_start, window_end)
        access, binding = await self._context(tenant_id)
        response = await self._request(
            "POST",
            f"{self._base_url}/freeBusy",
            access,
            json_body={
                "timeMin": _rfc3339(window_start),
                "timeMax": _rfc3339(window_end),
                "timeZone": _FREE_BUSY_TIME_ZONE,
                "items": [{"id": calendar_id} for calendar_id in binding.free_busy_calendar_ids],
            },
        )
        _raise_for_error(response)
        return _busy_blocks(response.json(), binding.free_busy_calendar_ids)

    async def upsert_event(self, tenant_id: UUID, entry: CalendarEntry) -> None:
        """Resolve duplicate keys, then insert deterministic ID or patch one matched event (FR-9.2/9.3)."""
        access, binding = await self._context(tenant_id)
        insert_payload = _event_payload(entry, include_id=True)
        patch_payload = _event_payload(entry, include_id=False)
        matched_event_id = await self._existing_provider_event_id(access, binding, entry)
        if matched_event_id is not None:
            await self._patch_event(access, binding, matched_event_id, patch_payload)
            return

        insert_url = self._events_url(binding.write_calendar_id)
        response = await self._request(
            "POST",
            insert_url,
            access,
            params={"sendUpdates": "none"},
            json_body=insert_payload,
        )
        if response.status_code != httpx.codes.CONFLICT:
            _raise_for_error(response)
            return

        await self._patch_event(
            access,
            binding,
            entry.calendar_event_id,
            patch_payload,
        )

    async def delete_event(
        self,
        tenant_id: UUID,
        calendar_event_id: str,
        *,
        canonical_event_id: UUID | None = None,
    ) -> None:
        """Delete canonical match or deterministic fallback; a Google 404 remains a no-op (FR-8.7/8.8/9.3)."""
        access, binding = await self._context(tenant_id)
        matched_event_id = (
            await self._canonical_provider_event_id(access, binding, canonical_event_id)
            if canonical_event_id is not None
            else None
        )
        response = await self._request(
            "DELETE",
            self._event_url(binding.write_calendar_id, matched_event_id or calendar_event_id),
            access,
            params={"sendUpdates": "none"},
        )
        if response.status_code != httpx.codes.NOT_FOUND:
            _raise_for_error(response)

    async def delete_tenant_events(self, tenant_id: UUID) -> None:
        """Enumerate and delete every app-owned event in the tenant's bound write calendar.

        Lifecycle rows are only a local projection and cannot prove the absence of orphaned remote
        writes. This sweep therefore paginates the provider collection and selects the private
        fixed owner marker written by every concierge upsert. Deletion happens page-by-page, so a
        safety-cap retry resumes from provider state instead of perpetually rebuilding an oversized
        in-memory inventory. A complete empty rescan is required before success.
        """
        binding = await self._bindings.get_binding(tenant_id)
        if binding is None:
            raise GoogleCalendarBindingNotFoundError(
                "Google app-calendar binding disappeared during account erasure"
            )
        access = await self._access.get_access(tenant_id)
        page_token: str | None = None
        seen_tokens: set[str] = set()
        deleted_in_pass = False
        for _page in range(_MAX_ERASURE_PAGES):
            params = {
                "showDeleted": "false",
                "maxResults": str(_ERASURE_PAGE_SIZE),
                "privateExtendedProperty": (
                    f"{GOOGLE_CALENDAR_OWNER_KEY}={GOOGLE_CALENDAR_OWNER_VALUE}"
                ),
            }
            if page_token is not None:
                params["pageToken"] = page_token
            response = await self._request(
                "GET",
                self._events_url(binding.write_calendar_id),
                access,
                params=params,
            )
            _raise_for_error(response)
            events, next_page_token = _erasure_event_page(response.json())
            provider_event_ids = tuple(
                event.event_id
                for event in events
                if event.is_concierge_owned and not event.is_cancelled
            )
            for provider_event_id in provider_event_ids:
                deleted = await self._request(
                    "DELETE",
                    self._event_url(binding.write_calendar_id, provider_event_id),
                    access,
                    params={"sendUpdates": "none"},
                )
                if deleted.status_code != httpx.codes.NOT_FOUND:
                    _raise_for_error(deleted)
                deleted_in_pass = True
            if next_page_token is None:
                if deleted_in_pass:
                    # Provider page tokens need not remain stable while their collection changes.
                    # Restart from page one and require an empty full pass before acknowledging.
                    page_token = None
                    seen_tokens.clear()
                    deleted_in_pass = False
                    continue
                return
            if next_page_token in seen_tokens:
                raise GoogleCalendarError("Google Calendar erasure pagination repeated a token")
            seen_tokens.add(next_page_token)
            page_token = next_page_token
        raise GoogleCalendarError(
            "Google Calendar erasure made bounded progress; retry to continue"
        )

    async def _existing_provider_event_id(
        self,
        access: GoogleCalendarAccess,
        binding: GoogleCalendarBinding,
        entry: CalendarEntry,
    ) -> str | None:
        """Resolve canonical metadata first, then one and only one FR-9.3 fuzzy candidate."""
        canonical_event_id = await self._canonical_provider_event_id(
            access, binding, entry.canonical_event_id
        )
        if canonical_event_id is not None:
            return canonical_event_id

        candidates = await self._fuzzy_candidates(access, binding, entry)
        matches = fuzzy_calendar_matches(
            title=entry.title,
            start_at=entry.start_at,
            location=entry.location,
            candidates=candidates,
        )
        if len(matches) > 1:
            raise GoogleCalendarAmbiguousMatchError(
                "Google Calendar fuzzy duplicate lookup returned more than one safe candidate"
            )
        return matches[0].provider_event_id if matches else None

    async def _canonical_provider_event_id(
        self,
        access: GoogleCalendarAccess,
        binding: GoogleCalendarBinding,
        canonical_event_id: UUID,
    ) -> str | None:
        """Return the only server-side canonical-private-property match, or fail closed (FR-9.3)."""
        events = await self._list_events(
            access,
            binding,
            params={
                "privateExtendedProperty": (
                    f"{GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY}={canonical_event_id}"
                ),
                "singleEvents": "true",
                "showDeleted": "false",
                "maxResults": "2",
            },
        )
        active_events = tuple(event for event in events if not event.is_cancelled)
        if len(active_events) > 1:
            raise GoogleCalendarAmbiguousMatchError(
                "Google Calendar canonical duplicate lookup returned more than one event"
            )
        return active_events[0].event_id if active_events else None

    async def _fuzzy_candidates(
        self,
        access: GoogleCalendarAccess,
        binding: GoogleCalendarBinding,
        entry: CalendarEntry,
    ) -> tuple[CalendarFuzzyCandidate, ...]:
        """Fetch the bounded candidate set for FR-9.3's local, deterministic secondary match."""
        events = await self._list_events(
            access,
            binding,
            params={
                "singleEvents": "true",
                "showDeleted": "false",
                "orderBy": "startTime",
                "timeMin": _rfc3339(entry.start_at - FUZZY_CALENDAR_START_DELTA),
                "timeMax": _rfc3339(entry.start_at + FUZZY_CALENDAR_START_DELTA),
                "maxResults": "250",
            },
        )
        return tuple(
            CalendarFuzzyCandidate(
                provider_event_id=event.event_id,
                title=event.summary,
                start_at=event.start_at,
                location=event.location,
            )
            for event in events
            if not event.is_cancelled and event.summary is not None and event.start_at is not None
        )

    async def _list_events(
        self,
        access: GoogleCalendarAccess,
        binding: GoogleCalendarBinding,
        *,
        params: Mapping[str, str],
    ) -> tuple[_ListedCalendarEvent, ...]:
        """List one complete, bounded response; pagination is ambiguous and therefore unsafe (FR-9.3)."""
        response = await self._request(
            "GET",
            self._events_url(binding.write_calendar_id),
            access,
            params=params,
        )
        _raise_for_error(response)
        return _listed_calendar_events(response.json())

    async def _patch_event(
        self,
        access: GoogleCalendarAccess,
        binding: GoogleCalendarBinding,
        provider_event_id: str,
        payload: Mapping[str, object],
    ) -> None:
        """Patch the actual provider ID; patch bodies omit immutable foreign event IDs (FR-9.3)."""
        patched = await self._request(
            "PATCH",
            self._event_url(binding.write_calendar_id, provider_event_id),
            access,
            params={"sendUpdates": "none"},
            json_body=payload,
        )
        _raise_for_error(patched)

    async def _context(self, tenant_id: UUID) -> tuple[GoogleCalendarAccess, GoogleCalendarBinding]:
        binding = await self._bindings.get_binding(tenant_id)
        if binding is None:
            raise GoogleCalendarBindingNotFoundError(
                "Google app-calendar binding is not provisioned"
            )
        return await self._access.get_access(tenant_id), binding

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

    def _event_url(self, calendar_id: str, event_id: str) -> str:
        return f"{self._events_url(calendar_id)}/{quote(event_id, safe='')}"


def _event_payload(entry: CalendarEntry, *, include_id: bool) -> dict[str, object]:
    """Build a Google Events resource with IANA zones and canonical private metadata (FR-9.3/9.5)."""
    if entry.end_at is None:
        raise ValueError("Google Calendar events require an end timestamp")
    _validate_window(entry.start_at, entry.end_at)
    zone = _iana_zone(entry.time_zone)
    private = _private_metadata(entry)
    payload: dict[str, object] = {
        "summary": entry.title,
        "start": _event_time(entry.start_at, zone, entry.time_zone),
        "end": _event_time(entry.end_at, zone, entry.time_zone),
        "extendedProperties": {"private": private},
    }
    if include_id:
        payload["id"] = entry.calendar_event_id
    if entry.location is not None:
        payload["location"] = entry.location
    return payload


def _private_metadata(entry: CalendarEntry) -> dict[str, str]:
    metadata = dict(entry.private_metadata)
    for key, value in metadata.items():
        if not key or not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(
                "calendar private metadata must use non-empty string keys and string values"
            )
    metadata[GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY] = str(entry.canonical_event_id)
    metadata[GOOGLE_CALENDAR_OWNER_KEY] = GOOGLE_CALENDAR_OWNER_VALUE
    return metadata


def _event_time(moment: datetime, zone: ZoneInfo, time_zone: str) -> dict[str, str]:
    return {"dateTime": _rfc3339(moment.astimezone(zone)), "timeZone": time_zone}


def _iana_zone(time_zone: str) -> ZoneInfo:
    try:
        return ZoneInfo(time_zone)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"calendar time_zone must be an IANA zone: {time_zone}") from error


def _validate_window(window_start: datetime, window_end: datetime) -> None:
    if (
        window_start.tzinfo is None
        or window_end.tzinfo is None
        or window_start.utcoffset() is None
        or window_end.utcoffset() is None
    ):
        raise ValueError("Google Calendar timestamps must be timezone-aware")
    if window_start >= window_end:
        raise ValueError("Google Calendar window end must be after window start")


def _rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Google Calendar timestamps must be timezone-aware")
    return moment.isoformat()


def _listed_calendar_events(payload: object) -> tuple[_ListedCalendarEvent, ...]:
    """Validate a complete Events.list page before a duplicate-resolution mutation (FR-9.3)."""
    if not isinstance(payload, Mapping):
        raise GoogleCalendarError("Google Calendar event list response must be an object")
    page_token = payload.get("nextPageToken")
    if page_token is not None and not isinstance(page_token, str):
        raise GoogleCalendarError("Google Calendar event list nextPageToken must be a string")
    if isinstance(page_token, str) and page_token:
        raise GoogleCalendarAmbiguousMatchError(
            "Google Calendar duplicate lookup requires pagination and cannot choose safely"
        )
    items = payload.get("items")
    if not isinstance(items, list):
        raise GoogleCalendarError("Google Calendar event list response must include items")
    return tuple(_listed_calendar_event(item) for item in items)


def _listed_calendar_event(item: object) -> _ListedCalendarEvent:
    if not isinstance(item, Mapping):
        raise GoogleCalendarError("Google Calendar event list item must be an object")
    event_id = item.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise GoogleCalendarError("Google Calendar event list item must include an event id")
    summary = item.get("summary")
    location = item.get("location")
    extended = item.get("extendedProperties")
    private = extended.get("private") if isinstance(extended, Mapping) else None
    return _ListedCalendarEvent(
        event_id=event_id,
        summary=summary if isinstance(summary, str) else None,
        start_at=_listed_event_start(item.get("start")),
        location=location if isinstance(location, str) else None,
        is_cancelled=item.get("status") == "cancelled",
        is_concierge_owned=_is_concierge_private_metadata(private),
    )


def _is_concierge_private_metadata(private: object) -> bool:
    """Require both the fixed owner schema and one canonical lowercase UUID marker."""
    if not isinstance(private, Mapping):
        return False
    if private.get(GOOGLE_CALENDAR_OWNER_KEY) != GOOGLE_CALENDAR_OWNER_VALUE:
        return False
    raw_canonical_id = private.get(GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY)
    if not isinstance(raw_canonical_id, str):
        return False
    try:
        canonical_id = UUID(raw_canonical_id)
    except ValueError:
        return False
    return str(canonical_id) == raw_canonical_id


def _erasure_event_page(payload: object) -> tuple[tuple[_ListedCalendarEvent, ...], str | None]:
    """Validate one provider page without applying duplicate-resolution's no-pagination rule."""
    if not isinstance(payload, Mapping):
        raise GoogleCalendarError("Google Calendar erasure list response must be an object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise GoogleCalendarError("Google Calendar erasure list response must include items")
    raw_token = payload.get("nextPageToken")
    if raw_token is not None and not isinstance(raw_token, str):
        raise GoogleCalendarError("Google Calendar erasure nextPageToken must be a string")
    token = raw_token.strip() if isinstance(raw_token, str) else None
    return tuple(_listed_calendar_event(item) for item in items), token or None


def _listed_event_start(value: object) -> datetime | None:
    """Return a timed Google start; all-day or malformed candidates cannot pass FR-9.3."""
    if not isinstance(value, Mapping):
        return None
    date_time = value.get("dateTime")
    if not isinstance(date_time, str):
        return None
    try:
        moment = datetime.fromisoformat(date_time)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def _busy_blocks(payload: object, calendar_ids: tuple[str, ...]) -> list[BusyBlock]:
    if not isinstance(payload, Mapping):
        raise GoogleCalendarError("Google freeBusy response must be an object")
    calendars = payload.get("calendars")
    if not isinstance(calendars, Mapping):
        raise GoogleCalendarError("Google freeBusy response must include calendars")

    blocks: list[BusyBlock] = []
    for calendar_id in calendar_ids:
        calendar = calendars.get(calendar_id)
        if not isinstance(calendar, Mapping):
            raise GoogleCalendarError("Google freeBusy response omitted a requested calendar")
        errors = calendar.get("errors")
        if isinstance(errors, list) and errors:
            raise GoogleCalendarError("Google freeBusy returned an error for a requested calendar")
        busy = calendar.get("busy")
        if not isinstance(busy, list):
            raise GoogleCalendarError("Google freeBusy calendar response must include busy blocks")
        for item in busy:
            blocks.append(_busy_block(item))
    return blocks


def _busy_block(item: object) -> BusyBlock:
    if not isinstance(item, Mapping):
        raise GoogleCalendarError("Google freeBusy block must be an object")
    start = item.get("start")
    end = item.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise GoogleCalendarError("Google freeBusy block must include RFC3339 start and end")
    try:
        parsed_start = datetime.fromisoformat(start)
        parsed_end = datetime.fromisoformat(end)
    except ValueError as error:
        raise GoogleCalendarError("Google freeBusy block timestamps must be RFC3339") from error
    _validate_window(parsed_start, parsed_end)
    return BusyBlock(start=parsed_start, end=parsed_end)


def _raise_for_error(response: httpx.Response) -> None:
    raise_for_google_calendar_error(response, operation="Google Calendar request")


def raise_for_google_calendar_error(response: httpx.Response, *, operation: str) -> None:
    """Raise one typed Calendar failure from a provider response (FR-9.7, NFR-8)."""
    if requires_reconsent(response):
        raise GoogleCalendarReconsentRequiredError(
            "Google Calendar authorization requires re-consent"
        )
    if is_retryable_google_calendar_response(response):
        raise GoogleCalendarRetryableError(
            f"{operation} was rejected by a transient Google rate or quota limit"
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise GoogleCalendarError(f"{operation} failed") from error


def requires_reconsent(response: httpx.Response) -> bool:
    """Classify only explicit credential or scope failures as a re-consent action (FR-9.7)."""
    if response.status_code == httpx.codes.UNAUTHORIZED:
        return True
    if response.status_code not in (httpx.codes.BAD_REQUEST, httpx.codes.FORBIDDEN):
        return False

    error = _google_error(response)
    if error == "invalid_grant":
        return True
    if not isinstance(error, Mapping):
        return False
    if error.get("status") == "INVALID_GRANT":
        return True
    return response.status_code == httpx.codes.FORBIDDEN and _has_reconsent_reason(error)


def is_retryable_google_calendar_response(response: httpx.Response) -> bool:
    """Recognize Google Calendar's documented rate/quota signals without treating them as auth failures."""
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        return True
    if response.status_code != httpx.codes.FORBIDDEN:
        return False
    error = _google_error(response)
    if not isinstance(error, Mapping):
        return False
    status = error.get("status")
    return (
        isinstance(status, str) and status in _RETRYABLE_GOOGLE_STATUSES
    ) or _has_retryable_rate_or_quota_reason(error)


def _google_error(response: httpx.Response) -> object | None:
    try:
        payload: object = response.json()
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    return payload.get("error")


def _has_reconsent_reason(error: Mapping[object, object]) -> bool:
    if _is_reconsent_reason(error.get("reason")):
        return True
    details = error.get("errors")
    if not isinstance(details, list):
        return False
    for detail in details:
        if isinstance(detail, Mapping) and _is_reconsent_reason(detail.get("reason")):
            return True
    return False


def _has_retryable_rate_or_quota_reason(error: Mapping[object, object]) -> bool:
    if _is_retryable_rate_or_quota_reason(error.get("reason")):
        return True
    details = error.get("errors")
    if not isinstance(details, list):
        return False
    for detail in details:
        if isinstance(detail, Mapping) and _is_retryable_rate_or_quota_reason(detail.get("reason")):
            return True
    return False


def _is_reconsent_reason(reason: object) -> bool:
    return isinstance(reason, str) and reason in _RECONSENT_FORBIDDEN_REASONS


def _is_retryable_rate_or_quota_reason(reason: object) -> bool:
    return isinstance(reason, str) and reason in _RETRYABLE_RATE_OR_QUOTA_REASONS
