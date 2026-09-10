"""Approved-origin CivicEngage RSS adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

Each reviewed CivicEngage publisher exposes an anonymous RSS calendar. This adapter accepts only
its closed source-key/host/path/query profiles, emits physical discovery handoffs from their
rolling near-term windows, and never follows event, enclosure, ticket, or registration URLs;
signs in; purchases; or mutates a source.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.catalog_window import collection_reference_time
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent
from ...infra.logging import get_logger

_log = get_logger("civic_engage.source")

_FETCH_TIMEOUT_S = 15.0
_MAX_RESPONSE_BYTES = 1_000_000
_TIME_RANGE_PARTS = 2
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_CONTROL_CHARACTER_LIMIT = 32
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_POSITIVE_ID = re.compile(r"^[1-9][0-9]*$")
_EVENT_DATE = re.compile(r"^[A-Z][a-z]+ [1-9][0-9]?, [0-9]{4}$")
_EVENT_TIME = re.compile(r"^(?:0[1-9]|1[0-2]):[0-5][0-9] [AP]M$")
_CANCELLED_OR_POSTPONED = re.compile(r"\b(?:cancell?ed|postponed)\b", re.IGNORECASE)
# CivicEngage appends the publisher locality after the venue without a reliable separator.  A
# remote marker at the beginning of that resulting venue is therefore not physical evidence,
# even when a local city suffix follows it.  Do not search the entire venue: a physical-first
# hybrid label (for example, ``City Hall — Zoom available``) remains an in-person handoff.
_VIRTUAL_LOCATION_PREFIX = re.compile(r"^(?:online|virtual|zoom|remote)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _CivicEngagePublisher:
    """One closed RSS/handoff pair; registry data cannot open a generic CivicEngage client (FR-10.3)."""

    source_key: str
    api_host: str
    api_path: str
    fixed_query: tuple[tuple[str, str], ...]
    handoff_path: str
    calendar_namespace: str
    city: str
    location_suffix: re.Pattern[str]
    source_id_prefix: str
    item_limit: int
    page_limit: int
    allows_city_only: bool
    strips_repeated_locality: bool


_PUBLISHERS = {
    "sf-rec-park-events": _CivicEngagePublisher(
        source_key="sf-rec-park-events",
        api_host="sfrecpark.org",
        api_path="/RSSFeed.aspx",
        fixed_query=(("CID", "Main-Calendar-14"), ("ModID", "58")),
        handoff_path="/Calendar.aspx",
        calendar_namespace="https://sfrecpark.org/Calendar.aspx",
        city="San Francisco",
        location_suffix=re.compile(
            r"San Francisco,\s*CA(?:\s+[0-9]{5}(?:-[0-9]{4})?)?\s*$", re.IGNORECASE
        ),
        source_id_prefix="sf-rec-park",
        item_limit=200,
        page_limit=1,
        allows_city_only=False,
        strips_repeated_locality=False,
    ),
    "campbell-events": _CivicEngagePublisher(
        source_key="campbell-events",
        api_host="www.campbellca.gov",
        api_path="/RSSFeed.aspx",
        fixed_query=(("CID", "Recreation-Community-Services-29"), ("ModID", "58")),
        handoff_path="/Calendar.aspx",
        calendar_namespace="https://www.campbellca.gov/Calendar.aspx",
        city="Campbell",
        location_suffix=re.compile(r"Campbell,\s*CA\s+95008\s*$", re.IGNORECASE),
        source_id_prefix="campbell",
        item_limit=50,
        page_limit=1,
        allows_city_only=True,
        strips_repeated_locality=False,
    ),
    "los-altos-events": _CivicEngagePublisher(
        source_key="los-altos-events",
        api_host="www.losaltosca.gov",
        api_path="/RSSFeed.aspx",
        fixed_query=(("CID", "All-calendar.xml"), ("ModID", "58")),
        handoff_path="/Calendar.aspx",
        calendar_namespace="https://www.losaltosca.gov/Calendar.aspx",
        city="Los Altos",
        location_suffix=re.compile(r"Los Altos,\s*CA\s+94(?:022|024)\s*$", re.IGNORECASE),
        source_id_prefix="los-altos",
        item_limit=50,
        page_limit=1,
        allows_city_only=False,
        strips_repeated_locality=True,
    ),
}


class CivicEngageRssCatalogFetcher:
    """Fetch reviewed CivicEngage RSS calendars at a human cadence (FR-10.3/10.4)."""

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
        self._clock = clock or monotonic
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._last_request_at: dict[str, float] = {}
        self._pacing_lock = asyncio.Lock()

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        """Return future timed physical rows from one reviewed publisher's rolling RSS window (FR-3.1)."""
        if source.mode is not CatalogSourceMode.CIVIC_ENGAGE_RSS:
            raise ValueError(f"unsupported CivicEngage mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("CivicEngage catalog sources must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("CivicEngage source must use its reviewed RSS endpoint")

        response = await self._response_or_error(source, publisher)
        items = _items_from_response(response, source.source_key, publisher)
        now = _as_utc(collection_reference_time(source, self._now()))
        candidates: list[CandidateEvent] = []
        seen_occurrences: set[str] = set()
        for item in items:
            reference = _event_reference(item, publisher)
            if reference is None:
                _log.warning("civic_engage_event_identity_invalid", source_key=source.source_key)
                continue
            event_id, occurrence_id, handoff_url = reference
            source_event_id = (
                f"{publisher.source_id_prefix}:{source.source_key}:{event_id}:{occurrence_id}"
            )
            if source_event_id in seen_occurrences:
                raise CivicEngageRssFetchError(
                    f"CivicEngage feed repeated an occurrence for {source.source_key}"
                )
            seen_occurrences.add(source_event_id)
            candidate = _candidate_from_item(
                item,
                source,
                publisher,
                now,
                source_event_id,
                handoff_url,
                event_id,
                occurrence_id,
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def _response_or_error(
        self, source: CatalogSource, publisher: _CivicEngagePublisher
    ) -> httpx.Response:
        """Request only the exact anonymous feed; redirects and endpoint changes fail closed (FR-10.3)."""
        url = source.seed_url
        if not _is_approved_endpoint_url(source, publisher, url):
            raise CivicEngageRssFetchError("CivicEngage request left the approved endpoint")
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            try:
                await self._wait_for_host_slot(url, source.min_interval_ms)
                response = await client.get(url, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise CivicEngageRssFetchError(
                    f"CivicEngage source {source.source_key} request failed: {exc}"
                ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url)
        ):
            raise CivicEngageRssFetchError("CivicEngage request left the approved endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CivicEngageRssFetchError(
                f"CivicEngage source {source.source_key} request failed: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise CivicEngageRssFetchError(
                "CivicEngage response exceeded its reviewed response-size limit"
            )
        return response

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host floor before each anonymous feed GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("CivicEngage URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _CivicEngagePublisher | None:
    """Return the reviewed publisher only when its key, cap, and exact seed agree (FR-10.3)."""
    publisher = _PUBLISHERS.get(source.source_key)
    if publisher is None:
        return None
    parsed = urlsplit(source.seed_url)
    return (
        publisher
        if (
            source.page_limit == publisher.page_limit
            and parsed.scheme.casefold() == "https"
            and parsed.netloc.casefold() == publisher.api_host
            and parsed.path == publisher.api_path
            and parse_qsl(parsed.query, keep_blank_values=True) == list(publisher.fixed_query)
            and not parsed.fragment
        )
        else None
    )


def _is_approved_endpoint_url(
    source: CatalogSource, publisher: _CivicEngagePublisher, url: str
) -> bool:
    """Require registry approval plus the exact published RSS resource and query (FR-10.3)."""
    parsed = urlsplit(url)
    return (
        source.allows_url(url)
        and parsed.netloc.casefold() == publisher.api_host
        and parsed.path == publisher.api_path
        and parse_qsl(parsed.query, keep_blank_values=True) == list(publisher.fixed_query)
        and not parsed.fragment
    )


def _items_from_response(
    response: httpx.Response, source_key: str, publisher: _CivicEngagePublisher
) -> list[ElementTree.Element]:
    """Parse one bounded RSS 2.0 document without accepting XML entity expansion (NFR-8)."""
    payload = response.content
    upper_payload = payload.upper()
    if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
        raise CivicEngageRssFetchError(
            f"CivicEngage feed contained unsupported XML declarations for {source_key}"
        )
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise CivicEngageRssFetchError(
            f"CivicEngage feed returned invalid XML for {source_key}"
        ) from exc
    if root.tag != "rss":
        raise CivicEngageRssFetchError(f"CivicEngage feed returned no RSS root for {source_key}")
    channel = root.find("channel")
    if channel is None:
        raise CivicEngageRssFetchError(f"CivicEngage feed returned no channel for {source_key}")
    items = list(channel.findall("item"))
    if len(items) >= publisher.item_limit:
        raise CivicEngageRssFetchError(
            f"CivicEngage feed reached its reviewed {publisher.item_limit}-item cap"
        )
    return items


def _event_reference(
    item: ElementTree.Element, publisher: _CivicEngagePublisher
) -> tuple[str, str, str] | None:
    """Require the publisher's matched event link and occurrence GUID before materializing a row (FR-3.8)."""
    link = _singleton_text(item, "link")
    validated_link = _handoff_reference(link, publisher)
    guid = _singleton_element(item, "guid")
    if (
        validated_link is None
        or guid is None
        or guid.attrib.get("isPermaLink", "").casefold() != "false"
    ):
        return None
    event_id, handoff_url = validated_link
    guid_value = _text_value(guid.text)
    if guid_value is None:
        return None
    prefix = f"{handoff_url}/"
    occurrence_id = guid_value.removeprefix(prefix)
    if guid_value == prefix or _POSITIVE_ID.fullmatch(occurrence_id) is None:
        return None
    return event_id, occurrence_id, handoff_url


def _handoff_reference(
    value: str | None, publisher: _CivicEngagePublisher
) -> tuple[str, str] | None:
    """Validate and reconstruct a human handoff, without ever requesting it (FR-3.1/FR-10.3)."""
    if value is None or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != publisher.api_host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != publisher.handoff_path
        or parsed.fragment
    ):
        return None
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    if (
        len(parameters) != 1
        or parameters[0][0] != "EID"
        or _POSITIVE_ID.fullmatch(parameters[0][1]) is None
    ):
        return None
    event_id = parameters[0][1]
    handoff_url = urlunsplit(
        ("https", publisher.api_host, publisher.handoff_path, f"EID={event_id}", "")
    )
    return event_id, handoff_url


def _candidate_from_item(
    item: ElementTree.Element,
    source: CatalogSource,
    publisher: _CivicEngagePublisher,
    now: datetime,
    source_event_id: str,
    handoff_url: str,
    event_id: str,
    occurrence_id: str,
) -> CandidateEvent | None:
    """Normalize one complete future physical RSS occurrence without inventing source fields (FR-3.7)."""
    title = _text(_singleton_text(item, "title"))
    description = _text(_singleton_text(item, "description"))
    event_date = _singleton_text(item, _calendar_tag(publisher, "EventDates"))
    event_times = _singleton_text(item, _calendar_tag(publisher, "EventTimes"))
    location = _text(_singleton_text(item, _calendar_tag(publisher, "Location")))
    if title is None or event_date is None or event_times is None or location is None:
        _log.warning("civic_engage_event_incomplete", source_key=source.source_key)
        return None
    if _CANCELLED_OR_POSTPONED.search(title) is not None:
        return None
    physical_location = _physical_location(location, publisher)
    if physical_location is None:
        return None
    start_at, end_at = _event_times(event_date, event_times)
    if start_at is None or end_at is None or end_at <= start_at or start_at < now:
        return None
    venue_name, city = physical_location
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=source_event_id,
        title=title,
        start_at=start_at,
        registration_url=handoff_url,
        end_at=end_at,
        venue_name=venue_name,
        city=city,
        description=(description or "")[:4_000],
        price_status=PriceStatus.UNKNOWN,
        raw={
            "event_id": event_id,
            "occurrence_id": occurrence_id,
            "event_date": event_date,
            "event_times": event_times,
            "location": location,
        },
    )


def _event_times(event_date: str, event_times: str) -> tuple[datetime | None, datetime | None]:
    """Parse only an exact source-declared single day and paired 12-hour local time range (FR-3.7)."""
    if _EVENT_DATE.fullmatch(event_date) is None:
        return None, None
    time_parts = event_times.split(" - ")
    if len(time_parts) != _TIME_RANGE_PARTS or any(
        _EVENT_TIME.fullmatch(part) is None for part in time_parts
    ):
        return None, None
    try:
        start_at = datetime.strptime(f"{event_date} {time_parts[0]}", "%B %d, %Y %I:%M %p")
        end_at = datetime.strptime(f"{event_date} {time_parts[1]}", "%B %d, %Y %I:%M %p")
    except ValueError:
        return None, None
    return (
        start_at.replace(tzinfo=_LOCAL_TIME_ZONE).astimezone(UTC),
        end_at.replace(tzinfo=_LOCAL_TIME_ZONE).astimezone(UTC),
    )


def _physical_location(
    location: str, publisher: _CivicEngagePublisher
) -> tuple[str | None, str] | None:
    """Keep source-declared physical locations, rejecting a leading virtual-only label (FR-3.1)."""
    match = publisher.location_suffix.search(location)
    if match is None:
        return None
    venue_text = location[: match.start()]
    if publisher.strips_repeated_locality:
        venue_text = _without_repeated_locality(venue_text, publisher.location_suffix)
    venue_name = _text(venue_text)
    if venue_name is not None and _VIRTUAL_LOCATION_PREFIX.match(venue_name) is not None:
        return None
    if venue_name is None and not publisher.allows_city_only:
        return None
    return venue_name, publisher.city


def _without_repeated_locality(value: str, locality_suffix: re.Pattern[str]) -> str:
    """Remove only an adjacent duplicate publisher locality before normalizing a venue (FR-3.7)."""
    result = value
    while (match := locality_suffix.search(result)) is not None and match.end() == len(result):
        result = result[: match.start()].rstrip(" ,;")
    return result


def _singleton_element(element: ElementTree.Element, tag: str) -> ElementTree.Element | None:
    """Reject ambiguous repeated source fields instead of arbitrarily selecting one (NFR-8)."""
    elements = element.findall(tag)
    return elements[0] if len(elements) == 1 else None


def _singleton_text(element: ElementTree.Element, tag: str) -> str | None:
    child = _singleton_element(element, tag)
    return _text_value(child.text) if child is not None else None


def _calendar_tag(publisher: _CivicEngagePublisher, local_name: str) -> str:
    return f"{{{publisher.calendar_namespace}}}{local_name}"


def _text_value(value: str | None) -> str | None:
    return value.strip() if value is not None and value.strip() else None


def _text(value: str | None) -> str | None:
    """Normalize bounded publisher HTML/text without preserving unsafe rich markup (FR-3.7)."""
    if value is None or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
        return None
    text = " ".join(HTMLParser(value).text(separator=" ", strip=True).split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


class CivicEngageRssFetchError(RuntimeError):
    """A whole-feed error that leaves the durable catalog refresh retryable (NFR-8)."""
