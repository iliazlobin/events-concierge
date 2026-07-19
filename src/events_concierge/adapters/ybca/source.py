"""Approved-origin Yerba Buena Center for the Arts calendar adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

Yerba Buena Center for the Arts publishes an anonymous, server-rendered calendar. This adapter
accepts only the reviewed one-page list, emits same-origin event handoffs, and never follows event
detail or Veevart ticket links; signs in; purchases; or mutates the source.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser, Node

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent

_FETCH_TIMEOUT_S = 15.0
_MAX_RESPONSE_BYTES = 250_000
_CARD_LIMIT = 40
_HORIZON_DAYS = 90
_CONTROL_CHARACTER_LIMIT = 32
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_EVENT_PATH = re.compile(r"^/event/[a-z0-9]+(?:-[a-z0-9]+)*/$")
_EVENT_TIME = re.compile(
    r"^(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"(?P<month>January|February|March|April|May|June|July|August|September|"
    r"October|November|December) "
    r"(?P<day>[1-9]|[12][0-9]|3[01]), (?P<year>[0-9]{4}), "
    r"(?P<start>(?:[1-9]|1[0-2])(?::[0-5][0-9])?)"
    r"(?:\u2013(?P<end>(?:[1-9]|1[0-2])(?::[0-5][0-9])?))? "
    r"(?P<meridiem>AM|PM)$"
)
_CANCELLED_OR_POSTPONED = re.compile(r"\b(?:cancell?ed|postponed)\b", re.IGNORECASE)
_VIRTUAL_OR_ONLINE = re.compile(r"\b(?:virtual|online|zoom)\b", re.IGNORECASE)
_MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


@dataclass(frozen=True, slots=True)
class _YbcaPublisher:
    """One closed YBCA list/handoff pair; registry data cannot open a generic client (FR-10.3)."""

    source_key: str
    list_host: str
    list_path: str
    page_limit: int
    min_interval_ms: int


@dataclass(frozen=True, slots=True)
class _YbcaCard:
    """One complete reviewed list card, retained without using its ticket link (FR-3.7)."""

    href: str | None
    title: str | None
    time_string: str | None
    venue_name: str | None
    card_text: str | None


_PUBLISHERS = {
    "ybca-calendar": _YbcaPublisher(
        source_key="ybca-calendar",
        list_host="ybca.org",
        list_path="/calendar/",
        page_limit=1,
        min_interval_ms=10_000,
    ),
}


class YbcaCatalogFetcher:
    """Fetch YBCA's closed SSR calendar at its published ten-second cadence (FR-10.3/10.4)."""

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
        """Return future physical YBCA events without fetching details or ticket links (FR-3.1/FR-3.7)."""
        if source.mode is not CatalogSourceMode.YBCA_HTML:
            raise ValueError(f"unsupported YBCA source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("YBCA catalog source must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("YBCA source must use the reviewed public calendar endpoint")

        document = await self._document_or_error(source, publisher)
        cards = _card_nodes(document, source.source_key)
        now = _as_local_time(self._now())
        horizon_start = datetime.combine(now.date(), time.min, tzinfo=_LOCAL_TIME_ZONE)
        horizon_end = horizon_start + timedelta(days=_HORIZON_DAYS)
        candidates_by_source_id: dict[str, CandidateEvent] = {}
        for node in cards:
            card = _card_from_node(node)
            candidate = _candidate_from_card(
                card,
                source,
                publisher,
                now,
                horizon_start,
                horizon_end,
            )
            if candidate is None:
                continue
            existing = candidates_by_source_id.get(candidate.source_event_id)
            if existing is None:
                candidates_by_source_id[candidate.source_event_id] = candidate
                continue
            if existing != candidate:
                raise YbcaFetchError(
                    f"YBCA list conflicted on {candidate.source_event_id} for {source.source_key}"
                )
        return list(candidates_by_source_id.values())

    async def _document_or_error(
        self, source: CatalogSource, publisher: _YbcaPublisher
    ) -> HTMLParser:
        """Read the one approved anonymous list; redirects and endpoint drift fail closed (FR-10.3)."""
        url = source.seed_url
        if not _is_approved_endpoint_url(source, publisher, url):
            raise YbcaFetchError("YBCA request left the approved calendar endpoint")
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
                raise YbcaFetchError(
                    f"YBCA source {source.source_key} request failed: {exc}"
                ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url)
        ):
            raise YbcaFetchError("YBCA request left the approved calendar endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise YbcaFetchError(f"YBCA source {source.source_key} request failed: {exc}") from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise YbcaFetchError("YBCA response exceeded its reviewed response-size limit")
        try:
            payload = response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise YbcaFetchError(f"YBCA response was not UTF-8 for {source.source_key}") from exc
        return HTMLParser(payload)

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Honor YBCA's robots Crawl-delay: 10 before each anonymous source GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("YBCA URL must include a host")
        interval_s = min_interval_ms / 1_000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _YbcaPublisher | None:
    """Return the locked reviewed profile only when its seed, sole origin, cap, and pace agree (FR-10.3)."""
    publisher = _PUBLISHERS.get(source.source_key)
    if publisher is None:
        return None
    return (
        publisher
        if (
            source.page_limit == publisher.page_limit
            and source.min_interval_ms == publisher.min_interval_ms
            and source.approved_origins == (f"https://{publisher.list_host}",)
            and _has_exact_https_authority(source.seed_url, publisher.list_host)
            and _exact_list_path(source.seed_url, publisher.list_path)
        )
        else None
    )


def _is_approved_endpoint_url(source: CatalogSource, publisher: _YbcaPublisher, url: str) -> bool:
    """Permit only the exact query-free YBCA list on its approved origin (FR-10.3)."""
    return (
        source.allows_url(url)
        and _has_exact_https_authority(url, publisher.list_host)
        and _exact_list_path(url, publisher.list_path)
    )


def _has_exact_https_authority(url: str, host: str) -> bool:
    """Reject controls, ports, userinfo, and lookalikes even when parsed hostname matches (FR-10.3)."""
    if _has_control_characters(url):
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold() == host
        and parsed.netloc.casefold() == host
        and parsed.username is None
        and parsed.password is None
        and port is None
    )


def _exact_list_path(url: str, path: str) -> bool:
    """Keep the reviewed public list query-free, because generic paging is unreviewed (FR-10.3)."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.path == path and not parsed.query and not parsed.fragment


def _card_nodes(document: HTMLParser, source_key: str) -> list[Node]:
    """Scope cards to the unique reviewed list, excluding separate featured content (NFR-8)."""
    _reject_pagination(document, source_key)
    containers = document.css("div.events")
    if len(containers) != 1:
        raise YbcaFetchError(
            f"YBCA response returned no unique reviewed calendar container for {source_key}"
        )
    cards = document.css("div.events > div.feature-event-wrap")
    if not cards:
        raise YbcaFetchError(f"YBCA response returned no reviewed event cards for {source_key}")
    if len(cards) >= _CARD_LIMIT:
        raise YbcaFetchError(
            f"YBCA source {source_key} reached its reviewed {_CARD_LIMIT}-card cap"
        )
    return cards


def _reject_pagination(document: HTMLParser, source_key: str) -> None:
    """A pager or page link is an unreviewed complete-list contract change (NFR-8)."""
    if document.css("nav.pager, .pager, .pagination, a[rel='next']"):
        raise YbcaFetchError(f"YBCA source {source_key} exposed unreviewed pagination")
    for link in document.css("a[href]"):
        href = link.attributes.get("href")
        if href is None:
            continue
        try:
            parameters = parse_qsl(urlsplit(href).query, keep_blank_values=True)
        except ValueError:
            continue
        if any(key == "page" for key, _value in parameters):
            raise YbcaFetchError(f"YBCA source {source_key} exposed unreviewed pagination")


def _card_from_node(node: Node) -> _YbcaCard:
    """Require the exact card-local title, time, and physical-location selectors (FR-3.7/NFR-8)."""
    title_links = node.css(".copy h3 > a[href]")
    date_values = node.css(".tickets > p.date")
    venue_values = node.css(".tickets > p.date + p > strong")
    if len(title_links) != 1 or len(date_values) != 1 or len(venue_values) != 1:
        raise YbcaFetchError(
            "YBCA card no longer matches its reviewed title/date/location contract"
        )
    return _YbcaCard(
        href=title_links[0].attributes.get("href"),
        title=_node_text(title_links[0]),
        time_string=_node_text(date_values[0]),
        venue_name=_node_text(venue_values[0]),
        card_text=_node_text(node),
    )


def _candidate_from_card(
    card: _YbcaCard,
    source: CatalogSource,
    publisher: _YbcaPublisher,
    now: datetime,
    horizon_start: datetime,
    horizon_end: datetime,
) -> CandidateEvent | None:
    """Normalize only verified future physical cards without inventing description, price, or geo (FR-3.7)."""
    if (
        card.title is None
        or card.time_string is None
        or card.venue_name is None
        or card.card_text is None
        or not card.venue_name.endswith(", YBCA")
        or _CANCELLED_OR_POSTPONED.search(card.card_text) is not None
        or _VIRTUAL_OR_ONLINE.search(card.card_text) is not None
    ):
        return None
    reference = _handoff_reference(card.href, publisher)
    start_at, end_at = _event_times(card.time_string)
    if (
        reference is None
        or start_at is None
        or not source.allows_url(reference[1])
        or start_at <= now
        or not horizon_start <= start_at < horizon_end
    ):
        return None
    path, handoff_url = reference
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"ybca:{source.source_key}:{path}:{start_at.isoformat()}",
        title=card.title,
        start_at=start_at,
        registration_url=handoff_url,
        end_at=end_at,
        venue_name=card.venue_name,
        city="San Francisco",
        description="",
        price_status=PriceStatus.UNKNOWN,
        raw={"path": path, "time_string": card.time_string, "location": card.venue_name},
    )


def _handoff_reference(value: str | None, publisher: _YbcaPublisher) -> tuple[str, str] | None:
    """Keep only a safe absolute same-origin event handoff, without requesting it (FR-3.1/FR-10.3)."""
    if value is None or _has_control_characters(value):
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != publisher.list_host
        or parsed.netloc.casefold() != publisher.list_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or _EVENT_PATH.fullmatch(parsed.path) is None
    ):
        return None
    return parsed.path, urlunsplit(("https", publisher.list_host, parsed.path, "", ""))


def _event_times(value: str) -> tuple[datetime | None, datetime | None]:
    """Parse only YBCA's verified single-day local grammar and its final-meridiem range form (FR-3.7)."""
    match = _EVENT_TIME.fullmatch(value)
    if match is None:
        return None, None
    event_date = _event_date(match)
    if event_date is None:
        return None, None
    start_at = _timestamp(event_date, match.group("start"), match.group("meridiem"))
    end_clock = match.group("end")
    if start_at is None:
        return None, None
    if end_clock is None:
        return start_at, None
    end_at = _timestamp(event_date, end_clock, match.group("meridiem"))
    if end_at is None or end_at <= start_at:
        return None, None
    return start_at, end_at


def _event_date(match: re.Match[str]) -> date | None:
    """Validate the weekday-bearing full date rather than accepting any calendar day (FR-3.7)."""
    try:
        event_date = date(
            int(match.group("year")),
            _MONTHS[match.group("month")],
            int(match.group("day")),
        )
    except ValueError:
        return None
    return event_date if event_date.strftime("%A") == match.group("weekday") else None


def _timestamp(event_date: date, clock: str, meridiem: str) -> datetime | None:
    """Attach LA time only to an unambiguous real local wall time (FR-3.7)."""
    normalized_clock = f"{clock}:00" if ":" not in clock else clock
    try:
        event_time = datetime.strptime(f"{normalized_clock} {meridiem}", "%I:%M %p").time()
    except ValueError:
        return None
    naive = datetime.combine(event_date, event_time)
    first = naive.replace(tzinfo=_LOCAL_TIME_ZONE, fold=0)
    second = naive.replace(tzinfo=_LOCAL_TIME_ZONE, fold=1)
    round_trip = first.astimezone(UTC).astimezone(_LOCAL_TIME_ZONE)
    if (
        first.utcoffset() != second.utcoffset()
        or round_trip.replace(tzinfo=None) != naive
        or round_trip.fold != 0
    ):
        return None
    return first


def _node_text(element: Node | None) -> str | None:
    return _text(element.text(separator=" ", strip=True)) if element is not None else None


def _text(value: str | None) -> str | None:
    if value is None or _has_unsafe_control_characters(value):
        return None
    normalized = " ".join(value.split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", normalized) or None


def _has_unsafe_control_characters(value: str) -> bool:
    """Allow ordinary layout whitespace but reject other C0 controls in source text (FR-10.3)."""
    return any(
        ord(character) < _CONTROL_CHARACTER_LIMIT and character not in "\t\n\r"
        for character in value
    )


def _has_control_characters(value: str) -> bool:
    """Reject every C0 control in a URL because it has no safe URL meaning (FR-10.3)."""
    return any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value)


def _as_local_time(value: datetime) -> datetime:
    """Treat a test-supplied naive clock as source-local time (FR-3.7)."""
    return (
        value.astimezone(_LOCAL_TIME_ZONE)
        if value.tzinfo is not None
        else value.replace(tzinfo=_LOCAL_TIME_ZONE)
    )


class YbcaFetchError(RuntimeError):
    """A complete-list error that leaves the durable catalog refresh retryable (NFR-8)."""
