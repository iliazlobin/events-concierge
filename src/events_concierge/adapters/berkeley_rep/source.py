"""Approved-origin Berkeley Repertory Theatre show-list adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

Berkeley Rep publishes an anonymous, server-rendered production list with each ticketable
performance expanded in the list itself. This adapter accepts only that closed one-page list,
emits official ticket handoffs, and never follows show, ticket, or registration links; signs in;
purchases; or mutates the source.
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
_MAX_RESPONSE_BYTES = 1_000_000
_CARD_LIMIT = 9
_OCCURRENCE_LIMIT = 400
_HORIZON_DAYS = 90
_MAX_SHOW_RANGE_DAYS = 370
_CONTROL_CHARACTER_LIMIT = 32
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")
_SHOW_PATH = re.compile(r"^/shows/[a-z0-9]+(?:-[a-z0-9]+)*$")
_TICKET_PATH = re.compile(r"^/[1-9][0-9]*/(?P<ticket_id>[1-9][0-9]*)$")
_FULL_DATE = re.compile(
    r"^(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun), "
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?P<day>[1-9]|[12][0-9]|3[01]), (?P<year>[0-9]{4})$"
)
_SHORT_DATE = re.compile(
    r"^(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun), "
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?P<day>[1-9]|[12][0-9]|3[01])$"
)
_EVENT_TIME = re.compile(r"^(?:[1-9]|1[0-2]):[0-5][0-9](?:AM|PM)$")
_CANCELLED_OR_POSTPONED = re.compile(r"\b(?:cancell?ed|postponed)\b", re.IGNORECASE)
_INVITATION_ONLY = re.compile(r"\bby invitation only\b", re.IGNORECASE)
_VIRTUAL_OR_ONLINE = re.compile(r"\b(?:virtual|online|zoom)\b", re.IGNORECASE)
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


@dataclass(frozen=True, slots=True)
class _BerkeleyRepPublisher:
    """One closed list/ticket pair; registry data cannot open a generic theatre client (FR-10.3)."""

    source_key: str
    list_host: str
    list_path: str
    ticket_host: str
    page_limit: int
    min_interval_ms: int


@dataclass(frozen=True, slots=True)
class _BerkeleyRepCard:
    """The source-list fields shared by every listed performance of one production (FR-3.7)."""

    entry_id: str
    title: str
    show_path: str
    tagline: str
    range_start: date
    range_end: date


_PUBLISHERS = {
    "berkeley-rep-shows": _BerkeleyRepPublisher(
        source_key="berkeley-rep-shows",
        list_host="www.berkeleyrep.org",
        list_path="/shows",
        ticket_host="tickets.berkeleyrep.org",
        page_limit=1,
        min_interval_ms=5_000,
    ),
}


class BerkeleyRepCatalogFetcher:
    """Fetch Berkeley Rep's closed SSR show list at the source-mandated cadence (FR-10.3/10.4)."""

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
        """Return future physical ticketable performances without requesting a show or ticket page (FR-3.1)."""
        if source.mode is not CatalogSourceMode.BERKELEY_REP_HTML:
            raise ValueError(f"unsupported Berkeley Rep source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("Berkeley Rep catalog source must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("Berkeley Rep source must use the reviewed public show-list endpoint")

        document = await self._document_or_error(source, publisher)
        card_nodes = _card_nodes(document, source.source_key)
        _validate_raw_occurrence_bound(card_nodes, source.source_key)
        now = _as_local_time(self._now())
        horizon_start = datetime.combine(now.date(), time.min, tzinfo=_LOCAL_TIME_ZONE)
        horizon_end = horizon_start + timedelta(days=_HORIZON_DAYS)
        candidates_by_source_id: dict[str, CandidateEvent] = {}
        for card_node in card_nodes:
            card = _card_from_node(card_node, publisher)
            if card is None:
                continue
            for occurrence in _occurrence_nodes(card_node, card.entry_id):
                candidate = _candidate_from_occurrence(
                    occurrence,
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
                    raise BerkeleyRepFetchError(
                        f"Berkeley Rep list conflicted on {candidate.source_event_id} "
                        f"for {source.source_key}"
                    )
        return list(candidates_by_source_id.values())

    async def _document_or_error(
        self, source: CatalogSource, publisher: _BerkeleyRepPublisher
    ) -> HTMLParser:
        """Read only the exact anonymous show list; redirects and endpoint drift fail closed (FR-10.3)."""
        url = source.seed_url
        if not _is_approved_endpoint_url(source, publisher, url):
            raise BerkeleyRepFetchError("Berkeley Rep request left the approved show-list endpoint")
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
                raise BerkeleyRepFetchError(
                    f"Berkeley Rep source {source.source_key} request failed: {exc}"
                ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url)
        ):
            raise BerkeleyRepFetchError("Berkeley Rep request left the approved show-list endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BerkeleyRepFetchError(
                f"Berkeley Rep source {source.source_key} request failed: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise BerkeleyRepFetchError(
                "Berkeley Rep response exceeded its reviewed response-size limit"
            )
        try:
            payload = response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BerkeleyRepFetchError(
                f"Berkeley Rep response was not UTF-8 for {source.source_key}"
            ) from exc
        return HTMLParser(payload)

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Honor Berkeley Rep's published five-second crawl delay before every list GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("Berkeley Rep URL must include a host")
        interval_s = min_interval_ms / 1_000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _BerkeleyRepPublisher | None:
    """Return the one reviewed profile only when host, origins, cap, and pacing agree (FR-10.3/10.4)."""
    publisher = _PUBLISHERS.get(source.source_key)
    if publisher is None:
        return None
    return (
        publisher
        if (
            source.page_limit == publisher.page_limit
            and source.min_interval_ms == publisher.min_interval_ms
            and source.approved_origins
            == (f"https://{publisher.list_host}", f"https://{publisher.ticket_host}")
            and _has_exact_https_authority(source.seed_url, publisher.list_host)
            and _exact_list_path(source.seed_url, publisher.list_path)
        )
        else None
    )


def _is_approved_endpoint_url(
    source: CatalogSource, publisher: _BerkeleyRepPublisher, url: str
) -> bool:
    """Permit only the constructed, query-free source list; ticket origins are handoff-only (FR-10.3)."""
    return (
        source.allows_url(url)
        and _has_exact_https_authority(url, publisher.list_host)
        and _exact_list_path(url, publisher.list_path)
    )


def _has_exact_https_authority(url: str, host: str) -> bool:
    """Reject userinfo, ports, controls, and lookalikes even when parsed hostname matches (FR-10.3)."""
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
    """Keep source requests query-free because the publisher robots policy disallows generic queries (FR-10.3)."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.path == path and not parsed.query and not parsed.fragment


def _card_nodes(document: HTMLParser, source_key: str) -> list[Node]:
    """Scope extraction to the one reviewed production list and reject a new pager contract (NFR-8)."""
    _reject_pagination(document, source_key)
    listings = document.css("ul.listItems")
    if len(listings) != 1:
        raise BerkeleyRepFetchError(
            f"Berkeley Rep response returned no unique reviewed show-list container for {source_key}"
        )
    cards = document.css("ul.listItems > li.eventCard[data-entry-id]")
    if not cards:
        raise BerkeleyRepFetchError(
            f"Berkeley Rep response returned no reviewed show cards for {source_key}"
        )
    if len(cards) >= _CARD_LIMIT:
        raise BerkeleyRepFetchError(
            f"Berkeley Rep source {source_key} reached its reviewed {_CARD_LIMIT}-card cap"
        )
    return cards


def _reject_pagination(document: HTMLParser, source_key: str) -> None:
    """A second list page is an unreviewed contract change, never a silent truncation (NFR-8)."""
    if document.css("nav.pager, .pager, .pagination, a[rel='next']"):
        raise BerkeleyRepFetchError(
            f"Berkeley Rep source {source_key} exposed unreviewed pagination"
        )
    for link in document.css("a[href]"):
        href = link.attributes.get("href")
        if href is None:
            continue
        try:
            parameters = parse_qsl(urlsplit(href).query, keep_blank_values=True)
        except ValueError:
            continue
        if any(key == "page" for key, _value in parameters):
            raise BerkeleyRepFetchError(
                f"Berkeley Rep source {source_key} exposed unreviewed pagination"
            )


def _validate_raw_occurrence_bound(card_nodes: list[Node], source_key: str) -> None:
    """Fail before normalization if the reviewed one-page list grows to its raw occurrence ceiling (NFR-8)."""
    raw_occurrences = sum(len(card.css("li.subshow")) for card in card_nodes)
    if raw_occurrences >= _OCCURRENCE_LIMIT:
        raise BerkeleyRepFetchError(
            f"Berkeley Rep source {source_key} reached its reviewed {_OCCURRENCE_LIMIT}-occurrence cap"
        )


def _card_from_node(node: Node, publisher: _BerkeleyRepPublisher) -> _BerkeleyRepCard | None:
    """Parse only source-list production fields; malformed cards cannot borrow another production's data (FR-3.7)."""
    entry_id = _positive_id(node.attributes.get("data-entry-id"))
    title_links = node.css("a.desc[href]")
    top_dates = node.css("div.top-date")
    if len(title_links) != 1 or len(top_dates) != 1:
        raise BerkeleyRepFetchError(
            "Berkeley Rep card no longer matches its reviewed title/date contract"
        )
    if entry_id is None:
        return None
    title_nodes = title_links[0].css("h3.title")
    starts = top_dates[0].css("span.start")
    ends = top_dates[0].css("span.end")
    if len(title_nodes) != 1 or len(starts) != 1 or len(ends) != 1:
        raise BerkeleyRepFetchError(
            "Berkeley Rep card no longer matches its reviewed title/date contract"
        )
    title = _node_text(title_nodes[0])
    show_reference = _show_reference(title_links[0].attributes.get("href"))
    range_start = _full_date(_node_text(starts[0]))
    range_end = _full_date(_node_text(ends[0]))
    if (
        title is None
        or show_reference is None
        or range_start is None
        or range_end is None
        or range_end < range_start
        or (range_end - range_start).days > _MAX_SHOW_RANGE_DAYS
    ):
        return None
    taglines = node.css("div.tagline")
    tagline = _node_text(taglines[0]) if len(taglines) == 1 else ""
    return _BerkeleyRepCard(
        entry_id=entry_id,
        title=title,
        show_path=show_reference,
        tagline=tagline or "",
        range_start=range_start,
        range_end=range_end,
    )


def _occurrence_nodes(card: Node, entry_id: str) -> list[Node]:
    """Use only the card's ID-bound direct subshow list, never adjacent cards or featured content (FR-3.7)."""
    lists = card.css(f"ul#sub-show-list{entry_id}")
    if len(lists) != 1:
        return []
    return card.css(f"ul#sub-show-list{entry_id} > li.subshow")


def _candidate_from_occurrence(
    occurrence: Node,
    card: _BerkeleyRepCard,
    source: CatalogSource,
    publisher: _BerkeleyRepPublisher,
    now: datetime,
    horizon_start: datetime,
    horizon_end: datetime,
) -> CandidateEvent | None:
    """Normalize one ticketable physical occurrence without inferring city, geo, price, or end time (FR-3.7)."""
    date_values = occurrence.css(".dateTime .date .start")
    time_values = occurrence.css(".dateTime .time .start")
    venues = occurrence.css(".locationBox .location")
    ticket_links = occurrence.css("a.btn-order[href]")
    if len(date_values) != 1 or len(time_values) != 1 or len(venues) != 1 or len(ticket_links) != 1:
        return None
    occurrence_date = _node_text(date_values[0])
    occurrence_time = _node_text(time_values[0])
    venue_name = _node_text(venues[0])
    ticket = _ticket_reference(ticket_links[0].attributes.get("href"), publisher)
    start_at = _occurrence_timestamp(
        occurrence_date,
        occurrence_time,
        card.range_start,
        card.range_end,
    )
    occurrence_text = _node_text(occurrence)
    if (
        occurrence_date is None
        or occurrence_time is None
        or venue_name is None
        or ticket is None
        or start_at is None
        or not source.allows_url(ticket[1])
        or _CANCELLED_OR_POSTPONED.search(card.title) is not None
        or _CANCELLED_OR_POSTPONED.search(occurrence_text or "") is not None
        or _INVITATION_ONLY.search(occurrence_text or "") is not None
        or _VIRTUAL_OR_ONLINE.search(card.title) is not None
        or _VIRTUAL_OR_ONLINE.search(venue_name) is not None
        or _VIRTUAL_OR_ONLINE.search(occurrence_text or "") is not None
        or start_at <= now
        or not horizon_start <= start_at < horizon_end
    ):
        return None
    ticket_id, handoff_url = ticket
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=(
            f"berkeley-rep:{source.source_key}:{card.entry_id}:{ticket_id}:{start_at.isoformat()}"
        ),
        title=card.title,
        start_at=start_at,
        registration_url=handoff_url,
        venue_name=venue_name,
        description=card.tagline,
        price_status=PriceStatus.UNKNOWN,
        raw={
            "entry_id": card.entry_id,
            "show_path": card.show_path,
            "ticket_id": ticket_id,
            "date": occurrence_date,
            "time": occurrence_time,
            "venue": venue_name,
        },
    )


def _show_reference(value: str | None) -> str | None:
    """Validate the same-origin show identity while never requesting its detail page (FR-3.1/FR-10.3)."""
    if value is None or _has_control_characters(value):
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.hostname is not None
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _SHOW_PATH.fullmatch(parsed.path) is None
    ):
        return None
    return parsed.path


def _ticket_reference(
    value: str | None, publisher: _BerkeleyRepPublisher
) -> tuple[str, str] | None:
    """Keep only the official direct ticket handoff, without fetching it (FR-3.1/FR-10.3)."""
    if value is None or _has_control_characters(value):
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    match = _TICKET_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != publisher.ticket_host
        or parsed.netloc.casefold() != publisher.ticket_host
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        return None
    ticket_id = match.group("ticket_id")
    handoff_url = urlunsplit(("https", publisher.ticket_host, parsed.path, "", ""))
    return ticket_id, handoff_url


def _full_date(value: str | None) -> date | None:
    """Parse a source parent's exact weekday-bearing full date without accepting loose prose (FR-3.7)."""
    if value is None:
        return None
    match = _FULL_DATE.fullmatch(value)
    if match is None:
        return None
    try:
        parsed = date(
            int(match.group("year")),
            _MONTHS[match.group("month")],
            int(match.group("day")),
        )
    except ValueError:
        return None
    return parsed if parsed.strftime("%a") == match.group("weekday") else None


def _occurrence_timestamp(
    event_date: str | None,
    event_time: str | None,
    range_start: date,
    range_end: date,
) -> datetime | None:
    """Resolve a no-year list date only when it has one weekday-valid date in its parent range (FR-3.7)."""
    if event_date is None or event_time is None or _EVENT_TIME.fullmatch(event_time) is None:
        return None
    match = _SHORT_DATE.fullmatch(event_date)
    if match is None:
        return None
    candidates: list[date] = []
    for year in range(range_start.year, range_end.year + 1):
        try:
            candidate = date(year, _MONTHS[match.group("month")], int(match.group("day")))
        except ValueError:
            continue
        if range_start <= candidate <= range_end and candidate.strftime("%a") == match.group(
            "weekday"
        ):
            candidates.append(candidate)
    if len(candidates) != 1:
        return None
    try:
        clock = datetime.strptime(event_time, "%I:%M%p").time()
    except ValueError:
        return None
    naive = datetime.combine(candidates[0], clock)
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


def _positive_id(value: str | None) -> str | None:
    return value if value is not None and _POSITIVE_INTEGER.fullmatch(value) is not None else None


def _node_text(element: Node | None) -> str | None:
    return _text(element.text(separator=" ", strip=True)) if element is not None else None


def _text(value: str | None) -> str | None:
    if value is None or _has_unsafe_control_characters(value):
        return None
    normalized = " ".join(value.split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", normalized) or None


def _has_unsafe_control_characters(value: str) -> bool:
    """Allow ordinary HTML layout whitespace but reject other C0 controls in source content (FR-10.3)."""
    return any(
        ord(character) < _CONTROL_CHARACTER_LIMIT and character not in "\t\n\r"
        for character in value
    )


def _has_control_characters(value: str) -> bool:
    """Reject every C0 character in a URL because it has no safe URL meaning (FR-10.3)."""
    return any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value)


def _as_local_time(value: datetime) -> datetime:
    """Treat a test-supplied naive clock as source-local time (FR-3.7)."""
    return (
        value.astimezone(_LOCAL_TIME_ZONE)
        if value.tzinfo is not None
        else value.replace(tzinfo=_LOCAL_TIME_ZONE)
    )


class BerkeleyRepFetchError(RuntimeError):
    """A complete-list error that leaves the durable catalog refresh retryable (NFR-8)."""
