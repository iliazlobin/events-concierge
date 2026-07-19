"""Approved-origin University of San Francisco Main Campus list adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

The University of San Francisco publishes an anonymous, server-rendered Main Campus calendar.
This adapter accepts only the reviewed one-page campus-filtered list, emits physical discovery
handoffs, and never follows event-detail, registration, or external application links; signs in;
purchases; or mutates the source.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser, Node

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent

_FETCH_TIMEOUT_S = 15.0
_MAX_RESPONSE_BYTES = 1_000_000
_CONTROL_CHARACTER_LIMIT = 32
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_EVENT_PATH = re.compile(r"^/event/[a-z0-9]+(?:-[a-z0-9]+)*/(?P<event_id>[1-9][0-9]*)$")
_EVENT_TIME = re.compile(
    r"^(?P<date>[A-Z][a-z]+ [1-9][0-9]?, [0-9]{4}) "
    r"(?P<start>(?:[1-9]|1[0-2]):[0-5][0-9](?:AM|PM)) - "
    r"(?P<end>(?:[1-9]|1[0-2]):[0-5][0-9](?:AM|PM))$"
)
_CANCELLED_OR_POSTPONED = re.compile(r"\b(?:cancell?ed|postponed)\b", re.IGNORECASE)
_VIRTUAL_OR_ONLINE = re.compile(r"\b(?:online|virtual|zoom)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _UsfcaPublisher:
    """One closed Main Campus list/handoff pair; registry data cannot open a generic Drupal client (FR-10.3)."""

    source_key: str
    api_host: str
    api_path: str
    fixed_query: tuple[tuple[str, str], ...]
    page_limit: int


@dataclass(frozen=True, slots=True)
class _UsfcaRow:
    """One compact event card as published on the reviewed list (FR-3.7)."""

    href: str | None
    title: str | None
    time_string: str | None
    venue_name: str | None


_PUBLISHERS = {
    "usfca-main-campus-events": _UsfcaPublisher(
        source_key="usfca-main-campus-events",
        api_host="www.usfca.edu",
        api_path="/life-at-usf/events",
        fixed_query=(("field_campus[179]", "179"),),
        page_limit=1,
    ),
}


class UsfcaCatalogFetcher:
    """Fetch USFCA's closed Main Campus event list at a human cadence (FR-10.3/10.4)."""

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
        """Return future timed Main Campus rows without following their event handoffs (FR-3.1)."""
        if source.mode is not CatalogSourceMode.USFCA_HTML:
            raise ValueError(f"unsupported USFCA source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("USFCA catalog source must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("USFCA source must use the reviewed Main Campus events endpoint")

        response = await self._response_or_error(source, publisher)
        rows = _rows_from_response(response, source.source_key)
        now = _as_utc(self._now())
        candidates_by_source_id: dict[str, CandidateEvent] = {}
        for row in rows:
            candidate = _candidate_from_row(row, source, publisher, now)
            if candidate is None:
                continue
            existing = candidates_by_source_id.get(candidate.source_event_id)
            if existing is None:
                candidates_by_source_id[candidate.source_event_id] = candidate
                continue
            if candidate != existing:
                raise UsfcaFetchError(
                    f"USFCA list conflicted on {candidate.source_event_id} for {source.source_key}"
                )
        return list(candidates_by_source_id.values())

    async def _response_or_error(
        self, source: CatalogSource, publisher: _UsfcaPublisher
    ) -> httpx.Response:
        """Read only the exact anonymous list; redirects and endpoint changes fail closed (FR-10.3)."""
        url = source.seed_url
        if not _is_approved_endpoint_url(source, publisher, url):
            raise UsfcaFetchError("USFCA request left the approved Main Campus endpoint")
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
                raise UsfcaFetchError(
                    f"USFCA source {source.source_key} request failed: {exc}"
                ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url)
        ):
            raise UsfcaFetchError("USFCA request left the approved Main Campus endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UsfcaFetchError(
                f"USFCA source {source.source_key} request failed: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise UsfcaFetchError("USFCA response exceeded its reviewed response-size limit")
        return response

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host floor before every anonymous list GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("USFCA URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _UsfcaPublisher | None:
    """Return the reviewed profile only when key, cap, and exact seed agree (FR-10.3)."""
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


def _is_approved_endpoint_url(source: CatalogSource, publisher: _UsfcaPublisher, url: str) -> bool:
    """Require registry approval plus the one reviewed campus-filtered list URL (FR-10.3)."""
    parsed = urlsplit(url)
    return (
        source.allows_url(url)
        and parsed.netloc.casefold() == publisher.api_host
        and parsed.path == publisher.api_path
        and parse_qsl(parsed.query, keep_blank_values=True) == list(publisher.fixed_query)
        and not parsed.fragment
    )


def _rows_from_response(response: httpx.Response, source_key: str) -> list[_UsfcaRow]:
    """Extract reviewed list cards and reject the appearance of a newly paginated surface (NFR-8)."""
    try:
        payload = response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsfcaFetchError(f"USFCA response was not UTF-8 for {source_key}") from exc
    document = HTMLParser(payload)
    containers = document.css("div.cc--events-listing")
    if len(containers) != 1:
        raise UsfcaFetchError(
            f"USFCA response returned no unique reviewed event-list container for {source_key}"
        )
    container = containers[0]
    _reject_pagination(container, source_key)
    title_links = container.css(".f--cta-title a[href]")
    if not title_links:
        raise UsfcaFetchError(f"USFCA response returned no reviewed event cards for {source_key}")
    return [_row_from_title_link(link) for link in title_links]


def _reject_pagination(container: Node, source_key: str) -> None:
    """A second list page is an unreviewed contract change, never a silent truncation (NFR-8)."""
    if container.css("nav.pager, .pager, .pagination"):
        raise UsfcaFetchError(f"USFCA source {source_key} exposed unreviewed pagination")
    for link in container.css("a[href]"):
        href = link.attributes.get("href")
        if href is None:
            continue
        try:
            parameters = parse_qsl(urlsplit(href).query, keep_blank_values=True)
        except ValueError:
            continue
        if any(key == "page" for key, _value in parameters):
            raise UsfcaFetchError(f"USFCA source {source_key} exposed unreviewed pagination")


def _row_from_title_link(link: Node) -> _UsfcaRow:
    """Keep only the closest reviewed card fields; this adapter never requests a detail page (FR-3.7)."""
    card = _card_for_title_link(link)
    return _UsfcaRow(
        href=link.attributes.get("href"),
        title=_node_text(link),
        time_string=_node_text(card.css_first(".f--time-string")) if card is not None else None,
        venue_name=_node_text(card.css_first(".event-location")) if card is not None else None,
    )


def _card_for_title_link(link: Node) -> Node | None:
    """Find the nearest one-title card so a missing field cannot borrow a neighboring event's value (FR-3.7)."""
    current = link.parent
    while current is not None:
        title_links = current.css(".f--cta-title a[href]")
        has_reviewed_field = (
            current.css_first(".f--time-string") is not None
            or current.css_first(".event-location") is not None
        )
        if len(title_links) == 1 and has_reviewed_field:
            return current
        if _is_listing_container(current):
            return None
        current = current.parent
    return None


def _is_listing_container(element: Node) -> bool:
    """Recognize the exact source-list boundary while traversing an event card (FR-10.3)."""
    classes = element.attributes.get("class")
    return classes is not None and "cc--events-listing" in classes.split()


def _candidate_from_row(
    row: _UsfcaRow, source: CatalogSource, publisher: _UsfcaPublisher, now: datetime
) -> CandidateEvent | None:
    """Normalize one complete future physical card without inventing location, price, city, or geo (FR-3.7)."""
    if (
        row.title is None
        or row.time_string is None
        or row.venue_name is None
        or _CANCELLED_OR_POSTPONED.search(row.title) is not None
        or _VIRTUAL_OR_ONLINE.search(row.title) is not None
        or _VIRTUAL_OR_ONLINE.search(row.venue_name) is not None
    ):
        return None
    reference = _handoff_reference(row.href, publisher)
    start_at, end_at = _event_times(row.time_string)
    if reference is None or start_at is None or end_at is None or start_at < now:
        return None
    path, event_id, handoff_url = reference
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"usfca:{source.source_key}:{event_id}:{start_at.isoformat()}",
        title=row.title,
        start_at=start_at,
        registration_url=handoff_url,
        end_at=end_at,
        venue_name=row.venue_name,
        description="",
        price_status=PriceStatus.UNKNOWN,
        raw={"path": path, "time_string": row.time_string, "location": row.venue_name},
    )


def _handoff_reference(
    value: str | None, publisher: _UsfcaPublisher
) -> tuple[str, str, str] | None:
    """Construct only a same-origin USFCA event handoff, without requesting it (FR-3.1/FR-10.3)."""
    if value is None or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    match = _EVENT_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.hostname is not None
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        return None
    event_id = match.group("event_id")
    path = parsed.path
    return path, event_id, urlunsplit(("https", publisher.api_host, path, "", ""))


def _event_times(value: str) -> tuple[datetime | None, datetime | None]:
    """Parse the exact published same-day full-date AM/PM range (FR-3.7)."""
    match = _EVENT_TIME.fullmatch(value)
    if match is None:
        return None, None
    start_at = _timestamp(match.group("date"), match.group("start"))
    end_at = _timestamp(match.group("date"), match.group("end"))
    if start_at is None or end_at is None or end_at <= start_at:
        return None, None
    return start_at, end_at


def _timestamp(event_date: str, event_clock: str) -> datetime | None:
    """Attach the reviewed local timezone only to a fully parsed public timestamp (FR-3.7)."""
    try:
        local = datetime.strptime(f"{event_date} {event_clock}", "%B %d, %Y %I:%M%p").replace(
            tzinfo=_LOCAL_TIME_ZONE
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def _node_text(element: Node | None) -> str | None:
    return _text(element.text(separator=" ", strip=True)) if element is not None else None


def _text(value: str | None) -> str | None:
    if value is None or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
        return None
    text = " ".join(value.split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


class UsfcaFetchError(RuntimeError):
    """A whole-list error that leaves the durable catalog refresh retryable (NFR-8)."""
