"""Approved-origin Mountain View Public Library LibCal ICS adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

Mountain View Public Library publishes an anonymous LibCal iCalendar feed. This adapter accepts
only that closed publisher endpoint in a fixed local 90-day window, emits physical discovery
handoffs, and never follows source URLs, signs in, registers, purchases, or mutates the source.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from math import isfinite
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.catalog_window import collection_end_at, collection_reference_time
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent
from ...domain.policy import SourceQuarantineSignal
from ...infra.logging import get_logger
from ...ports.sources import SourceAccessDeniedError, SourceRateLimitedError

_log = get_logger("libcal.source")

_FETCH_TIMEOUT_S = 15.0
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_EVENTS = 300
_MAX_UNFOLDED_LINE_LENGTH = 20_000
_HORIZON_DAYS = 90
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_CONTROL_CHARACTER_LIMIT = 32
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_UTC_TIMESTAMP = re.compile(r"^\d{8}T\d{6}Z$")
_POSITIVE_EVENT_ID = re.compile(r"^[1-9][0-9]*$")
_RETRY_AFTER_SECONDS = re.compile(r"^[0-9]+$")
_CANCELLED = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
_RECURRENCE_PROPERTIES = frozenset({"EXDATE", "RDATE", "RECURRENCE-ID", "RRULE"})
_SINGLE_PROPERTIES = frozenset(
    {"DESCRIPTION", "DTEND", "DTSTART", "LOCATION", "STATUS", "SUMMARY", "UID", "URL"}
)


@dataclass(frozen=True, slots=True)
class _LibCalPublisher:
    """One closed feed/handoff pair; registry data cannot open another LibCal client (FR-10.3)."""

    source_key: str
    api_host: str
    api_path: str
    fixed_query: tuple[tuple[str, str], ...]
    handoff_host: str
    uid_prefix: str
    physical_locations: frozenset[str]


@dataclass(frozen=True, slots=True)
class _IcsProperty:
    """One parsed ICS property, retaining whether source parameters changed its time semantics."""

    value: str
    has_parameters: bool


@dataclass(frozen=True, slots=True)
class _IcsEvent:
    """One syntactically bounded VEVENT extracted from the publisher's single feed."""

    properties: dict[str, _IcsProperty]


_LIBCAL_PUBLISHERS = {
    "mountain-view-library-events": _LibCalPublisher(
        source_key="mountain-view-library-events",
        api_host="mountainview.libcal.com",
        api_path="/ical_subscribe.php",
        fixed_query=(("src", "p"), ("cid", "8800")),
        handoff_host="mountainview.libcal.com",
        uid_prefix="LibCal-8800-",
        physical_locations=frozenset(
            {
                "1st Floor Program Room",
                "2nd Floor Program Room",
                "Bike Fix-it Station (Outside Franklin St. Entrance)",
                "Bookmobile Garage",
                "Children's Room",
                "History Center",
                "Pioneer Park",
                "Teen Zone",
            }
        ),
    ),
}


class LibCalIcsCatalogFetcher:
    """Fetch the closed Mountain View calendar through upstream Pacer admission (FR-10.3/FR-10.4)."""

    def __init__(
        self,
        *,
        user_agent: str,
        now: Callable[[], datetime] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._now = now or (lambda: datetime.now(UTC))
        self._transport = transport

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        """Return future timed physical occurrences in the reviewed local horizon (FR-3.1)."""
        if source.mode is not CatalogSourceMode.LIBCAL_ICS:
            raise ValueError(f"unsupported LibCal source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("LibCal catalog sources must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("LibCal source must use a reviewed public calendar endpoint")

        provider_now = _as_utc(self._now())
        now = _as_utc(collection_reference_time(source, provider_now))
        horizon_end = collection_end_at(source, _horizon_end(now, source.collection_horizon_days))
        response = await self._response_or_error(source, publisher, provider_now)
        events = _events_from_response(response, source.source_key)
        candidates: list[CandidateEvent] = []
        seen_event_ids: set[str] = set()
        for event in events:
            event_id = _event_id(event.properties.get("UID"), publisher)
            if event_id is not None:
                if event_id in seen_event_ids:
                    raise LibCalIcsFetchError(
                        f"LibCal feed repeated an event uid for {source.source_key}"
                    )
                seen_event_ids.add(event_id)
            candidate = _candidate_from_event(event, source, publisher, now, horizon_end)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def _response_or_error(
        self, source: CatalogSource, publisher: _LibCalPublisher, now: datetime
    ) -> httpx.Response:
        """Request the exact calendar endpoint once; redirects and unsafe responses fail closed (FR-10.3)."""
        url = source.seed_url
        if not _is_approved_endpoint_url(source, publisher, url):
            raise LibCalIcsFetchError("LibCal request left the approved endpoint")
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            try:
                response = await client.get(url, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise LibCalIcsFetchError(
                    f"LibCal source {source.source_key} request failed: {exc}"
                ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url)
        ):
            raise LibCalIcsFetchError("LibCal request left the approved endpoint")
        if response.status_code == httpx.codes.FORBIDDEN:
            raise SourceAccessDeniedError(SourceQuarantineSignal.FORBIDDEN)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            retry_after_seconds = _retry_after_seconds(response.headers.get("Retry-After"), now)
            if retry_after_seconds is not None:
                raise SourceRateLimitedError(
                    "LibCal source returned HTTP 429",
                    retry_after_seconds=retry_after_seconds,
                )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LibCalIcsFetchError(
                f"LibCal source {source.source_key} request failed: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise LibCalIcsFetchError("LibCal response exceeded its reviewed response-size limit")
        return response


def _publisher_for_source(source: CatalogSource) -> _LibCalPublisher | None:
    """Return the sole reviewed publisher only when its key and seed agree exactly (FR-10.3)."""
    publisher = _LIBCAL_PUBLISHERS.get(source.source_key)
    if publisher is None:
        return None
    parsed = urlsplit(source.seed_url)
    return (
        publisher
        if (
            parsed.scheme.casefold() == "https"
            and parsed.netloc.casefold() == publisher.api_host
            and parsed.path == publisher.api_path
            and parse_qsl(parsed.query, keep_blank_values=True) == list(publisher.fixed_query)
            and not parsed.fragment
        )
        else None
    )


def _is_approved_endpoint_url(source: CatalogSource, publisher: _LibCalPublisher, url: str) -> bool:
    """Require registry approval plus the exact reviewed list endpoint and query (FR-10.3)."""
    parsed = urlsplit(url)
    return (
        source.allows_url(url)
        and parsed.netloc.casefold() == publisher.api_host
        and parsed.path == publisher.api_path
        and parse_qsl(parsed.query, keep_blank_values=True) == list(publisher.fixed_query)
        and not parsed.fragment
    )


def _events_from_response(response: httpx.Response, source_key: str) -> list[_IcsEvent]:
    """Parse a bounded non-recurring RFC 5545 subset before events enter the catalog (NFR-8)."""
    try:
        payload = response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LibCalIcsFetchError(f"LibCal response was not UTF-8 for {source_key}") from exc
    if payload.startswith("\ufeff"):
        payload = payload[1:]
    lines = _unfold_lines(payload, source_key)
    events: list[_IcsEvent] = []
    properties: dict[str, _IcsProperty] | None = None
    calendar_started = False
    calendar_finished = False
    for line in lines:
        if not line:
            continue
        if calendar_finished:
            raise LibCalIcsFetchError(
                f"LibCal response had content after its calendar envelope for {source_key}"
            )
        if line == "BEGIN:VCALENDAR":
            if calendar_started or calendar_finished or properties is not None:
                raise LibCalIcsFetchError(
                    f"LibCal response had an invalid calendar envelope for {source_key}"
                )
            calendar_started = True
            continue
        if line == "END:VCALENDAR":
            if not calendar_started or calendar_finished or properties is not None:
                raise LibCalIcsFetchError(
                    f"LibCal response had an invalid calendar envelope for {source_key}"
                )
            calendar_finished = True
            continue
        if line == "BEGIN:VEVENT":
            if not calendar_started or calendar_finished or properties is not None:
                raise LibCalIcsFetchError(
                    f"LibCal response had an invalid VEVENT envelope for {source_key}"
                )
            properties = {}
            continue
        if line == "END:VEVENT":
            if properties is None:
                raise LibCalIcsFetchError(
                    f"LibCal response had an invalid VEVENT envelope for {source_key}"
                )
            events.append(_IcsEvent(properties))
            if len(events) >= _MAX_EVENTS:
                raise LibCalIcsFetchError(
                    f"LibCal response exceeded its reviewed event-count limit for {source_key}"
                )
            properties = None
            continue
        if properties is None:
            continue
        if line.startswith(("BEGIN:", "END:")):
            raise LibCalIcsFetchError(
                f"LibCal response had an unsupported nested component for {source_key}"
            )
        _add_property(line, properties, source_key)
    if not calendar_started or not calendar_finished or properties is not None:
        raise LibCalIcsFetchError(
            f"LibCal response had an incomplete calendar envelope for {source_key}"
        )
    return events


def _unfold_lines(payload: str, source_key: str) -> list[str]:
    """Apply RFC 5545 line unfolding while rejecting malformed or oversized source content (NFR-8)."""
    unfolded: list[str] = []
    for line in payload.splitlines():
        if "\x00" in line:
            raise LibCalIcsFetchError(f"LibCal response contained a NUL byte for {source_key}")
        if line.startswith((" ", "\t")):
            if not unfolded:
                raise LibCalIcsFetchError(
                    f"LibCal response began with a folded line for {source_key}"
                )
            unfolded[-1] = f"{unfolded[-1]}{line[1:]}"
        else:
            unfolded.append(line)
        if len(unfolded[-1]) > _MAX_UNFOLDED_LINE_LENGTH:
            raise LibCalIcsFetchError(
                f"LibCal response contained an oversized line for {source_key}"
            )
    return unfolded


def _add_property(line: str, properties: dict[str, _IcsProperty], source_key: str) -> None:
    """Retain only the singleton event fields needed by the closed source contract (FR-3.7)."""
    head, value = _split_property_line(line)
    separator = bool(head)
    if not separator or not head:
        raise LibCalIcsFetchError(f"LibCal response contained an invalid property for {source_key}")
    name, parameter_separator, _ = head.partition(";")
    property_name = name.upper()
    if property_name in _RECURRENCE_PROPERTIES:
        raise LibCalIcsFetchError(
            f"LibCal response contained unsupported recurrence data for {source_key}"
        )
    if property_name not in _SINGLE_PROPERTIES:
        return
    if property_name in properties:
        raise LibCalIcsFetchError(f"LibCal response repeated a singleton property for {source_key}")
    properties[property_name] = _IcsProperty(value, bool(parameter_separator))


def _split_property_line(line: str) -> tuple[str, str]:
    """Split an ICS property at its first unquoted value separator (RFC 5545)."""
    quoted = False
    escaped = False
    for index, character in enumerate(line):
        if character == "\\" and not escaped:
            escaped = True
            continue
        if character == '"' and not escaped:
            quoted = not quoted
        elif character == ":" and not quoted and not escaped:
            return line[:index], line[index + 1 :]
        escaped = False
    return "", ""


def _candidate_from_event(
    event: _IcsEvent,
    source: CatalogSource,
    publisher: _LibCalPublisher,
    now: datetime,
    horizon_end: datetime,
) -> CandidateEvent | None:
    """Normalize one timed, physical source occurrence without trusting its URL field (FR-3.1/FR-3.7)."""
    event_id = _event_id(event.properties.get("UID"), publisher)
    title = _text(_property_value(event, "SUMMARY"))
    start_at = _parse_utc_timestamp(event.properties.get("DTSTART"))
    location = _text(_property_value(event, "LOCATION"))
    status = _text(_property_value(event, "STATUS"))
    if (
        event_id is None
        or title is None
        or start_at is None
        or not _has_matching_source_url(event.properties.get("URL"), event_id, publisher)
        or location not in publisher.physical_locations
        or _CANCELLED.search(title) is not None
        or (status is not None and status.casefold() == "cancelled")
    ):
        _log.warning("libcal_event_incomplete", source_key=source.source_key)
        return None
    if start_at < now or start_at >= horizon_end:
        return None
    end_at = _parse_utc_timestamp(event.properties.get("DTEND"))
    if end_at is not None and end_at <= start_at:
        end_at = None
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"libcal:{source.source_key}:{event_id}:{start_at.isoformat()}",
        title=title,
        start_at=start_at,
        registration_url=_handoff_url(event_id, publisher),
        end_at=end_at,
        venue_name=location,
        description=(_text(_property_value(event, "DESCRIPTION")) or "")[:4_000],
        price_status=PriceStatus.UNKNOWN,
        raw={"uid": f"{publisher.uid_prefix}{event_id}", "status": status or ""},
    )


def _property_value(event: _IcsEvent, name: str) -> str | None:
    """Return a source property only when it did not add ambiguous parameters (FR-3.7)."""
    property_value = event.properties.get(name)
    if property_value is None or property_value.has_parameters:
        return None
    return property_value.value


def _event_id(value: _IcsProperty | None, publisher: _LibCalPublisher) -> str | None:
    """Accept only a positive publisher uid that can safely form the official handoff (FR-3.8)."""
    if value is None or value.has_parameters:
        return None
    uid = value.value.strip()
    if not uid.startswith(publisher.uid_prefix):
        return None
    event_id = uid.removeprefix(publisher.uid_prefix)
    return event_id if _POSITIVE_EVENT_ID.fullmatch(event_id) is not None else None


def _has_matching_source_url(
    value: _IcsProperty | None, event_id: str, publisher: _LibCalPublisher
) -> bool:
    """Require the published URL to agree with the validated UID while never following it (FR-3.8)."""
    if (
        value is None
        or value.has_parameters
        or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value.value)
    ):
        return False
    try:
        parsed = urlsplit(value.value.strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold() == publisher.handoff_host
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == f"/event/{event_id}"
        and not parsed.query
        and not parsed.fragment
    )


def _parse_utc_timestamp(value: _IcsProperty | None) -> datetime | None:
    """Accept only the publisher's unambiguous UTC timed-event representation (FR-3.7)."""
    if value is None or value.has_parameters or _UTC_TIMESTAMP.fullmatch(value.value) is None:
        return None
    try:
        return datetime.strptime(value.value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _handoff_url(event_id: str, publisher: _LibCalPublisher) -> str:
    """Build the matching official handoff from a validated UID without requesting it (FR-3.1)."""
    return urlunsplit(("https", publisher.handoff_host, f"/event/{event_id}", "", ""))


def _horizon_end(now: datetime, horizon_days: int = _HORIZON_DAYS) -> datetime:
    """Return the source-specific local half-open 90-day end in UTC (FR-3.1)."""
    local_start = datetime.combine(
        now.astimezone(_LOCAL_TIME_ZONE).date(), time.min, _LOCAL_TIME_ZONE
    )
    return (local_start + timedelta(days=horizon_days)).astimezone(UTC)


def _text(value: str | None) -> str | None:
    """Unescape and sanitize bounded source text without retaining raw rich content (FR-3.7/NFR-8)."""
    if value is None or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
        return None
    text = " ".join(HTMLParser(_unescape_ics(value)).text(separator=" ", strip=True).split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


def _unescape_ics(value: str) -> str:
    """Decode the RFC 5545 text escapes used in the reviewed public calendar (FR-3.7)."""
    return re.sub(
        r"\\([nN,;\\])",
        lambda match: {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}[match.group(1)],
        value,
    )


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """Parse a valid HTTP Retry-After value without inventing a provider delay (AC-73)."""
    if value is None:
        return None
    header = value.strip()
    if _RETRY_AFTER_SECONDS.fullmatch(header) is not None:
        try:
            seconds = float(header)
        except OverflowError:
            return None
        return seconds if isfinite(seconds) else None
    try:
        retry_at = parsedate_to_datetime(header)
    except (IndexError, OverflowError, TypeError, ValueError):
        return None
    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        return None
    seconds = (retry_at.astimezone(UTC) - now).total_seconds()
    return max(seconds, 0.0) if isfinite(seconds) else None


class LibCalIcsFetchError(RuntimeError):
    """A whole-feed failure that leaves the durable catalog refresh retryable (NFR-8)."""
