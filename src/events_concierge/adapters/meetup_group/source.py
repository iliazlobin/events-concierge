"""Read the official public group iCalendar export, never a member or RSVP feed.

One reviewed group identity supplies one bounded calendar document. Every explicitly public
occurrence is validated before any detail request; malformed/truncated calendars fail as a
whole. The export determines coverage, so this adapter does not promise complete city search.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ...application.ingestion_telemetry import record_ingestion_collection_progress
from ...domain.catalog_sources import CatalogSource
from ...domain.catalog_window import collection_end_at, collection_reference_time
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent
from ...domain.policy import SourceQuarantineSignal
from ...ports.sources import SourceAccessDeniedError, SourceRateLimitedError, SourceTransientError
from ..meetup_city.source import (
    PUBLIC_EVENT_ID_PATTERN,
    MeetupCityCatalogFetcher,
    public_retry_after,
)

_ORIGIN = "https://www.meetup.com"
_FEED_PATH = re.compile(r"^/(?P<group>[a-z0-9][a-z0-9_-]{0,119})/events/ical/$")
_UID = re.compile(rf"event_({PUBLIC_EVENT_ID_PATTERN})@meetup\.com")
_MAX_BYTES = 2_000_000
_MAX_LINE = 20_000
_MAX_EVENTS = 100
_REQUEST_UNITS = _MAX_EVENTS + 1
_MIN_INTERVAL_MS = 1500
_MAX_TITLE_CHARS = 500
_FETCH_TIMEOUT_S = 20.0
_PROPERTIES = frozenset({"CLASS", "UID", "URL", "DTSTART", "DTEND", "SUMMARY", "STATUS"})
_RECURRENCE = frozenset({"RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"})


class MeetupGroupFetchError(RuntimeError):
    """The reviewed public calendar could not be proven safe and complete."""


@dataclass(frozen=True, slots=True)
class _Property:
    value: str
    parameters: tuple[tuple[str, str], ...]


class MeetupGroupCalendarCatalogFetcher:
    """Collect every explicit public occurrence returned by one official group export."""

    def __init__(
        self,
        *,
        user_agent: str,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._now = now or (lambda: datetime.now(UTC))
        self._transport = transport
        self._details = MeetupCityCatalogFetcher(
            user_agent=user_agent, now=self._now, clock=clock, sleep=sleep, transport=transport,
        )

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        group = reviewed_group_slug(source.seed_url)
        if (source.mode is not CatalogSourceMode.MEETUP_GROUP_ICS or group is None
            or not source.handoff_only or source.approved_origins != (_ORIGIN,)
            or source.page_limit != _REQUEST_UNITS or source.min_interval_ms < _MIN_INTERVAL_MS):
            raise ValueError("Meetup group source must retain its reviewed public export contract")
        now = collection_reference_time(source, self._now())
        horizon_end = collection_end_at(source, now + timedelta(days=source.collection_horizon_days))
        async with httpx.AsyncClient(
            headers={"User-Agent": self._user_agent}, follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S, transport=self._transport,
        ) as client:
            await self._details.wait_for_public_slot(source.seed_url, source.min_interval_ms)
            payload = await _read_feed(client, source, now)
            candidates = [c for c in _candidates(payload, group, now) if c.start_at < horizon_end]
            await record_ingestion_collection_progress(
                source_key=source.source_key, request_completed=True,
                page_completed=True, candidate_count=len(candidates),
            )
            return await self._details.enrich_public_candidates(
                client, source, candidates, max_requests=_MAX_EVENTS, strict_boundary_signals=True,
            )


def reviewed_group_slug(url: str) -> str | None:
    """Accept only an exact anonymous group export URL, with no tokens or queries."""
    try:
        parsed = urlsplit(url)
        match = _FEED_PATH.fullmatch(parsed.path)
        if (parsed.scheme != "https" or parsed.netloc != "www.meetup.com"
            or parsed.query or parsed.fragment or match is None):
            return None
        return match.group("group")
    except ValueError:
        return None


async def _read_feed(client: httpx.AsyncClient, source: CatalogSource, now: datetime) -> str:
    try:
        async with asyncio.timeout(_FETCH_TIMEOUT_S), client.stream(
            "GET", source.seed_url, follow_redirects=False,
        ) as response:
            if response.is_redirect or str(response.url) != source.seed_url:
                raise MeetupGroupFetchError("Meetup group export redirected or changed identity")
            if response.status_code in (401, 403):
                raise SourceAccessDeniedError(SourceQuarantineSignal.FORBIDDEN)
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                raise SourceRateLimitedError(
                    "Meetup group export is rate limited",
                    retry_after_seconds=public_retry_after(response.headers.get("Retry-After"), now),
                )
            if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
                raise SourceTransientError("Meetup group export is temporarily unavailable",
                                           retry_after_seconds=30.0)
            response.raise_for_status()
            if response.headers.get("content-type", "").partition(";")[0].strip().lower() != "text/calendar":
                raise MeetupGroupFetchError("Meetup group export has an invalid content type")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > _MAX_BYTES):
                raise MeetupGroupFetchError("Meetup group export exceeds its response limit")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > _MAX_BYTES:
                    raise MeetupGroupFetchError("Meetup group export exceeds its response limit")
                content.extend(chunk)
        return content.decode("utf-8-sig")
    except (httpx.TransportError, TimeoutError) as exc:
        raise SourceTransientError("Meetup group export transport is temporarily unavailable",
                                   retry_after_seconds=30.0) from exc
    except (httpx.HTTPError, UnicodeDecodeError) as exc:
        raise MeetupGroupFetchError("Meetup group export request or encoding is invalid") from exc


def _events(payload: str) -> list[dict[str, _Property]]:  # noqa: PLR0912, PLR0915
    """Parse only bounded public event properties; attendee/description data is ignored."""
    lines: list[str] = []
    for line in payload.splitlines():
        if line.startswith((" ", "\t")):
            if not lines:
                raise MeetupGroupFetchError("Meetup export starts with an invalid folded line")
            lines[-1] += line[1:]
        else:
            lines.append(line)
        if len(lines[-1]) > _MAX_LINE:
            raise MeetupGroupFetchError("Meetup export contains an oversized property")
    stack: list[str] = []
    result: list[dict[str, _Property]] = []
    current: dict[str, _Property] | None = None
    finished = False
    for line in lines:
        if not line:
            continue
        if finished:
            raise MeetupGroupFetchError("Meetup export has trailing calendar content")
        if line.startswith("BEGIN:"):
            component = line[6:]
            valid = (not stack and component == "VCALENDAR") or (
                stack == ["VCALENDAR"] and component in ("VEVENT", "VTIMEZONE")) or (
                stack == ["VCALENDAR", "VTIMEZONE"] and component in ("STANDARD", "DAYLIGHT"))
            if not valid:
                raise MeetupGroupFetchError("Meetup export contains an invalid calendar envelope")
            stack.append(component)
            if component == "VEVENT":
                current = {}
            continue
        if line.startswith("END:"):
            component = line[4:]
            if not stack or stack.pop() != component:
                raise MeetupGroupFetchError("Meetup export contains an invalid calendar envelope")
            if component == "VEVENT":
                assert current is not None
                result.append(current)
                current = None
                if len(result) > _MAX_EVENTS:
                    raise MeetupGroupFetchError("Meetup export exceeds its reviewed occurrence cap")
            if component == "VCALENDAR":
                finished = True
            continue
        if not stack:
            raise MeetupGroupFetchError("Meetup export is missing its calendar envelope")
        if current is None:
            continue
        label, separator, value = line.partition(":")
        parts = label.split(";")
        name = parts[0].upper()
        if name not in _PROPERTIES and name not in _RECURRENCE:
            continue
        if not separator or name in current:
            raise MeetupGroupFetchError("Meetup export contains a duplicate or invalid event property")
        parameters: list[tuple[str, str]] = []
        for parameter in parts[1:]:
            key, equal, val = parameter.partition("=")
            if not equal or not key or not val or key.upper() in dict(parameters):
                raise MeetupGroupFetchError("Meetup export contains invalid property parameters")
            parameters.append((key.upper(), val.strip('"')))
        current[name] = _Property(value, tuple(parameters))
    if not finished or stack or current is not None:
        raise MeetupGroupFetchError("Meetup export is truncated")
    return result


def _candidates(payload: str, group: str, now: datetime) -> list[CandidateEvent]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Meetup collection clock must be timezone-aware")
    result: list[CandidateEvent] = []
    seen: set[str] = set()
    for event in _events(payload):
        visibility = event.get("CLASS")
        if visibility is None or visibility.value != "PUBLIC" or visibility.parameters:
            continue
        if any(name in event for name in _RECURRENCE):
            raise MeetupGroupFetchError("Meetup export must contain explicit dated occurrences")
        uid = event.get("UID")
        match = _UID.fullmatch(uid.value) if uid is not None and not uid.parameters else None
        if match is None:
            raise MeetupGroupFetchError("Meetup export event identity is invalid")
        identity = match[1]
        url = f"{_ORIGIN}/{group}/events/{identity}/"
        link = event.get("URL")
        if (link is None or link.value != url
            or link.parameters not in ((), (("VALUE", "URI"),)) or identity in seen):
            raise MeetupGroupFetchError("Meetup export event link or identity is inconsistent")
        seen.add(identity)
        start = _timestamp(event.get("DTSTART"))
        end = _timestamp(event["DTEND"]) if "DTEND" in event else None
        if end is not None and end <= start:
            raise MeetupGroupFetchError("Meetup export event duration is invalid")
        summary = event.get("SUMMARY")
        if summary is None or summary.parameters:
            raise MeetupGroupFetchError("Meetup export event title is missing")
        title = " ".join(_unescape(summary.value).split())
        if not title or len(title) > _MAX_TITLE_CHARS:
            raise MeetupGroupFetchError("Meetup export event title is invalid")
        status = event.get("STATUS", _Property("CONFIRMED", ()))
        if status.parameters or status.value not in ("CONFIRMED", "TENTATIVE", "CANCELLED"):
            raise MeetupGroupFetchError("Meetup export event status is invalid")
        if start < now or status.value == "CANCELLED":
            continue
        result.append(CandidateEvent(
            source=Source.PUBLIC_JSONLD, source_event_id=f"meetup:{identity}",
            registration_url=url, title=title, start_at=start, end_at=end,
            price_status=PriceStatus.UNKNOWN,
            raw={"adapter": "meetup_group_ics", "event_url": url,
                 "organizer_url": f"{_ORIGIN}/{group}/", "calendar_visibility": "public"},
        ))
    return result


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda match: "\n" if match[1] in "nN" else match[1], value)


def _timestamp(prop: _Property | None) -> datetime:
    if prop is None:
        raise MeetupGroupFetchError("Meetup export event time is missing")
    params = dict(prop.parameters)
    try:
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z?", prop.value):
            raise ValueError("timestamp syntax")
        if prop.value.endswith("Z") and params in ({}, {"VALUE": "DATE-TIME"}):
            return datetime.strptime(prop.value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if set(params) not in ({"TZID"}, {"TZID", "VALUE"}) or params.get("VALUE", "DATE-TIME") != "DATE-TIME":
            raise ValueError("floating or all-day time")
        zone_name = params["TZID"]
        if not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z_]+){1,2}", zone_name):
            raise ValueError("timezone name")
        local = datetime.strptime(prop.value, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(zone_name))
        if local.astimezone(UTC).astimezone(local.tzinfo).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ValueError("nonexistent local time")
        if local.utcoffset() != local.replace(fold=1).utcoffset():
            raise ValueError("ambiguous local time")
        return local.astimezone(UTC)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise MeetupGroupFetchError("Meetup export event time is invalid") from exc
