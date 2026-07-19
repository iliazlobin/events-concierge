"""Approved-origin Midpeninsula Regional Open Space District list adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

Midpeninsula Regional Open Space District publishes its Events & Activities list as an anonymous,
server-rendered calendar. This adapter accepts only that closed nine-page publisher endpoint,
emits physical discovery handoffs, and never follows event-detail, registration, or external
links; signs in; purchases; or mutates the source.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser, Node

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent
from ...infra.logging import get_logger

_log = get_logger("midpen.source")

_FETCH_TIMEOUT_S = 15.0
_MAX_RESPONSE_BYTES = 1_000_000
_ROWS_PER_PAGE = 15
_TIME_RANGE_WITH_END = 2
_CONTROL_CHARACTER_LIMIT = 32
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_EVENT_PATH = re.compile(r"^/events/[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+$")
_EVENT_DATE = re.compile(r"^[A-Z][a-z]+, [A-Z][a-z]{2} [1-9][0-9]?, [0-9]{4}$")
_EVENT_TIME = re.compile(r"^(?:0?[1-9]|1[0-2]):[0-5][0-9] (?:am|pm)$", re.IGNORECASE)
_CANCELLED_OR_POSTPONED = re.compile(r"\b(?:cancelled|canceled|postponed)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _MidpenPublisher:
    """One closed list/handoff pair; registry data cannot open a generic Drupal client (FR-10.3)."""

    source_key: str
    api_host: str
    api_path: str
    page_limit: int
    physical_locations: frozenset[str]


@dataclass(frozen=True, slots=True)
class _MidpenRow:
    """One source-list table row, before physical/time eligibility is applied (FR-3.7)."""

    href: str | None
    title: str | None
    event_date: str | None
    event_time: str | None
    venue_name: str | None
    activity_type: str | None
    subtype: str | None
    miles: str | None


@dataclass(frozen=True, slots=True)
class _MidpenPage:
    """One source page plus its validated immediate-next state (NFR-8)."""

    rows: list[_MidpenRow]
    has_next: bool


_PUBLISHERS = {
    "midpen-events": _MidpenPublisher(
        source_key="midpen-events",
        api_host="www.openspace.org",
        api_path="/get-involved/events-activities",
        page_limit=9,
        physical_locations=frozenset(
            {
                "Bear Creek Redwoods Preserve",
                "Cloverdale Ranch Preserve",
                "El Corte de Madera Creek Preserve",
                "El Sereno Preserve",
                "Fremont Older Preserve",
                "La Honda Creek Preserve",
                "Long Ridge Preserve",
                "Los Trancos Preserve",
                "Monte Bello Preserve",
                "Pulgas Ridge Preserve",
                "Purisima Creek Redwoods Preserve",
                "Rancho San Antonio Preserve",
                "Ravenswood Preserve",
                "Russian Ridge Preserve",
                "Sierra Azul Preserve",
                "Skyline Ridge Preserve",
                "St. Joseph's Hill Preserve",
                "Thornewood Preserve",
                "Windy Hill Preserve",
            }
        ),
    ),
}


class MidpenCatalogFetcher:
    """Fetch Midpen's closed public event list at a human cadence (FR-10.3/10.4)."""

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
        """Return future timed physical preserve activities from the reviewed page sequence (FR-3.1)."""
        if source.mode is not CatalogSourceMode.MIDPEN_HTML:
            raise ValueError(f"unsupported Midpen source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("Midpen catalog source must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("Midpen source must use its reviewed public events endpoint")

        pages = await self._fetch_pages(source, publisher)
        now = _as_utc(self._now())
        candidates_by_source_id: dict[str, CandidateEvent] = {}
        for page in pages:
            for row in page.rows:
                candidate = _candidate_from_row(row, source, publisher, now)
                if candidate is None:
                    continue
                existing = candidates_by_source_id.get(candidate.source_event_id)
                if existing is None:
                    candidates_by_source_id[candidate.source_event_id] = candidate
                    continue
                if candidate != existing:
                    raise MidpenFetchError(
                        f"Midpen page sequence conflicted on {candidate.source_event_id} "
                        f"for {source.source_key}"
                    )
        return list(candidates_by_source_id.values())

    async def _fetch_pages(
        self, source: CatalogSource, publisher: _MidpenPublisher
    ) -> list[_MidpenPage]:
        """Walk the validated immediate-next chain, failing closed on a partial list (NFR-8)."""
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            pages: list[_MidpenPage] = []
            for page_index in range(publisher.page_limit):
                page = await self._fetch_page(client, source, publisher, page_index)
                pages.append(page)
                if not page.has_next:
                    return pages
        raise MidpenFetchError(
            f"Midpen source {source.source_key} exceeds its reviewed {publisher.page_limit}-page cap"
        )

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        publisher: _MidpenPublisher,
        page_index: int,
    ) -> _MidpenPage:
        """Read one exact source page and validate its local table/pager contract (FR-10.3/NFR-8)."""
        url = _page_url(source.seed_url, page_index)
        if not _is_approved_endpoint_url(source, publisher, url, page_index):
            raise MidpenFetchError(f"Midpen page {page_index} left the approved endpoint")
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise MidpenFetchError(
                f"Midpen page {page_index} failed for {source.source_key}: {exc}"
            ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url), page_index
        ):
            raise MidpenFetchError(f"Midpen page {page_index} left the approved endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MidpenFetchError(
                f"Midpen page {page_index} failed for {source.source_key}: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise MidpenFetchError(
                f"Midpen page {page_index} exceeded its reviewed response-size limit"
            )
        page = _page_from_response(
            response,
            source.source_key,
            page_index,
            page_limit=publisher.page_limit,
        )
        _validate_page_rows(page, source.source_key, page_index)
        return page

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host floor before every anonymous source-list GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("Midpen URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _MidpenPublisher | None:
    """Return the reviewed publisher only when source key, seed, and page cap agree exactly (FR-10.3)."""
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
            and parse_qsl(parsed.query, keep_blank_values=True) == [("page", "0")]
            and not parsed.fragment
        )
        else None
    )


def _is_approved_endpoint_url(
    source: CatalogSource, publisher: _MidpenPublisher, url: str, page_index: int
) -> bool:
    """Require registry approval plus the exact reviewed list path and one constructed page query (FR-10.3)."""
    parsed = urlsplit(url)
    return (
        source.allows_url(url)
        and parsed.netloc.casefold() == publisher.api_host
        and parsed.path == publisher.api_path
        and parse_qsl(parsed.query, keep_blank_values=True) == [("page", str(page_index))]
        and not parsed.fragment
    )


def _page_url(seed_url: str, page_index: int) -> str:
    """Construct one fixed nonnegative page query without trusting publisher pagination links (FR-10.3)."""
    if page_index < 0:
        raise ValueError("Midpen page index must be nonnegative")
    parsed = urlsplit(seed_url)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode((("page", str(page_index)),)), "")
    )


def _page_from_response(
    response: httpx.Response,
    source_key: str,
    page_index: int,
    *,
    page_limit: int,
) -> _MidpenPage:
    """Extract one bounded, source-specific table and safe immediate-next state (NFR-8)."""
    try:
        payload = response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MidpenFetchError(f"Midpen page {page_index} was not UTF-8 for {source_key}") from exc
    document = HTMLParser(payload)
    tables = document.css("table.views-table.views-view-table.cols-6")
    if len(tables) != 1:
        raise MidpenFetchError(
            f"Midpen page {page_index} returned no reviewed event table for {source_key}"
        )
    body = tables[0].css_first("tbody")
    if body is None:
        raise MidpenFetchError(
            f"Midpen page {page_index} returned no event table body for {source_key}"
        )
    rows = [_row_from_element(element) for element in body.css("tr")]
    has_next = _pager_has_next(document, source_key, page_index, page_limit)
    return _MidpenPage(rows, has_next)


def _pager_has_next(
    document: HTMLParser,
    source_key: str,
    page_index: int,
    page_limit: int,
) -> bool:
    """Validate a compact pager and accept only its immediate safe successor (NFR-8)."""
    pager = document.css_first("nav.pager")
    if pager is None:
        if page_index != 0:
            raise MidpenFetchError(
                f"Midpen page {page_index} lost its reviewed pager for {source_key}"
            )
        return False
    active = pager.css_first("li.pager__item--active .pager__number")
    active_index = _active_page_index(active)
    if active_index != page_index:
        raise MidpenFetchError(
            f"Midpen page {page_index} returned an inconsistent active pager for {source_key}"
        )
    page_indices = {active_index}
    for link in pager.css("a.number"):
        linked_index = _pager_link_index(link)
        if linked_index is None:
            raise MidpenFetchError(
                f"Midpen page {page_index} returned an unsafe pager link for {source_key}"
            )
        if linked_index >= page_limit:
            raise MidpenFetchError(
                f"Midpen source {source_key} exceeds its reviewed {page_limit}-page cap"
            )
        page_indices.add(linked_index)
    next_link = pager.css_first("li.pager__item--next a")
    next_href = next_link.attributes.get("href") if next_link is not None else None
    if next_link is not None and next_href not in {None, ""}:
        next_index = _pager_link_index(next_link)
        if next_index is None:
            raise MidpenFetchError(
                f"Midpen page {page_index} returned an unsafe next link for {source_key}"
            )
        if next_index >= page_limit:
            raise MidpenFetchError(
                f"Midpen source {source_key} exceeds its reviewed {page_limit}-page cap"
            )
        if next_index != page_index + 1:
            raise MidpenFetchError(
                f"Midpen page {page_index} returned an inconsistent next link for {source_key}"
            )
        return True
    if page_index + 1 in page_indices:
        # Midpen sometimes renders an inactive Next control before the last page. Its exact
        # adjacent numeric link remains a safe, closed-list continuation.
        return True
    if any(index > page_index for index in page_indices):
        raise MidpenFetchError(
            f"Midpen page {page_index} returned an inconsistent next link for {source_key}"
        )
    return False


def _active_page_index(element: Node | None) -> int | None:
    value = _text(element.text(strip=True)) if element is not None else None
    if value is None or not value.isdecimal() or int(value) <= 0:
        return None
    return int(value) - 1


def _pager_link_index(element: Node) -> int | None:
    href = element.attributes.get("href")
    if href is None:
        return None
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.path
        or parsed.fragment not in {"", "pager-content"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    if len(parameters) != 1 or parameters[0][0] != "page" or not parameters[0][1].isdecimal():
        return None
    return int(parameters[0][1])


def _validate_page_rows(page: _MidpenPage, source_key: str, page_index: int) -> None:
    """Reject a changed table width or a short non-final page before it can look complete (NFR-8)."""
    row_count = len(page.rows)
    if row_count > _ROWS_PER_PAGE:
        raise MidpenFetchError(
            f"Midpen page {page_index} exceeded its reviewed row limit for {source_key}"
        )
    if page.has_next and row_count != _ROWS_PER_PAGE:
        raise MidpenFetchError(f"Midpen page {page_index} was short before the declared final page")
    if page_index > 0 and not page.has_next and row_count == 0:
        raise MidpenFetchError(f"Midpen page {page_index} was unexpectedly empty for {source_key}")


def _row_from_element(element: Node) -> _MidpenRow:
    """Retain only source-list fields; no event detail URL is requested (FR-3.7)."""
    title_link = element.css_first("td.views-field-title a[href]")
    date_node = element.css_first("td.views-field-aggregated-dates.is-active .activity-search-date")
    time_node = element.css_first("td.views-field-aggregated-dates.is-active .activity-search-time")
    return _MidpenRow(
        href=title_link.attributes.get("href") if title_link is not None else None,
        title=_node_text(title_link),
        event_date=_node_text(date_node),
        event_time=_node_text(time_node),
        venue_name=_node_text(element.css_first("td.views-field-field-preserve-term-1")),
        activity_type=_node_text(element.css_first("td.views-field-type")),
        subtype=_node_text(
            element.css_first("td.views-field-field-activity-type .icon-link__name")
        ),
        miles=_node_text(element.css_first("td.views-field-field-aprox-total-miles")),
    )


def _candidate_from_row(
    row: _MidpenRow, source: CatalogSource, publisher: _MidpenPublisher, now: datetime
) -> CandidateEvent | None:
    """Normalize one named-preserve timed activity without inferring missing location or price (FR-3.7)."""
    if (
        row.title is None
        or row.event_date is None
        or row.event_time is None
        or row.venue_name is None
        or _CANCELLED_OR_POSTPONED.search(row.title) is not None
        or row.venue_name not in publisher.physical_locations
    ):
        return None
    reference = _handoff_reference(row.href, publisher)
    start_at, end_at = _event_times(row.event_date, row.event_time)
    if reference is None or start_at is None or start_at < now:
        return None
    path, handoff_url = reference
    description = _description(row.activity_type, row.subtype, row.miles)
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"midpen:{source.source_key}:{path}:{start_at.isoformat()}",
        title=row.title,
        start_at=start_at,
        registration_url=handoff_url,
        end_at=end_at,
        venue_name=row.venue_name,
        description=description,
        price_status=PriceStatus.UNKNOWN,
        raw={
            "activity_type": row.activity_type,
            "subtype": row.subtype,
            "miles": row.miles,
        },
    )


def _handoff_reference(value: str | None, publisher: _MidpenPublisher) -> tuple[str, str] | None:
    """Construct only a safe same-publisher event handoff and never fetch it (FR-3.1/FR-10.3)."""
    if value is None or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
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
        or _EVENT_PATH.fullmatch(parsed.path) is None
    ):
        return None
    path = parsed.path
    return path, urlunsplit(("https", publisher.api_host, path, "", ""))


def _event_times(event_date: str, event_time: str) -> tuple[datetime | None, datetime | None]:
    """Parse exact source date and AM/PM times, including an optional same-day end time (FR-3.7)."""
    if _EVENT_DATE.fullmatch(event_date) is None:
        return None, None
    parts = event_time.split(" - ")
    if len(parts) not in {1, _TIME_RANGE_WITH_END}:
        return None, None
    start_clock = _parse_clock(parts[0])
    end_clock = _parse_clock(parts[1]) if len(parts) == _TIME_RANGE_WITH_END else None
    if start_clock is None or (len(parts) == _TIME_RANGE_WITH_END and end_clock is None):
        return None, None
    start_at = _timestamp(event_date, start_clock)
    if start_at is None:
        return None, None
    if end_clock is None:
        return start_at, None
    end_at = _timestamp(event_date, end_clock)
    return (start_at, end_at) if end_at is not None and end_at > start_at else (None, None)


def _timestamp(event_date: str, event_clock: str) -> datetime | None:
    """Attach only the reviewed local timezone to a fully parsed source timestamp (FR-3.7)."""
    try:
        local = datetime.strptime(f"{event_date} {event_clock}", "%A, %b %d, %Y %I:%M %p").replace(
            tzinfo=_LOCAL_TIME_ZONE
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def _parse_clock(value: str) -> str | None:
    normalized = value.casefold().replace(".", "")
    if _EVENT_TIME.fullmatch(normalized) is None:
        return None
    return normalized.upper()


def _description(activity_type: str | None, subtype: str | None, miles: str | None) -> str:
    """Index only compact source-list classification fields; the list provides no narrative description (FR-3.7)."""
    parts = [value for value in (activity_type, subtype) if value is not None]
    if miles is not None:
        parts.append(f"{miles} miles")
    return " — ".join(parts)[:4_000]


def _node_text(element: Node | None) -> str | None:
    return _text(element.text(separator=" ", strip=True)) if element is not None else None


def _text(value: str | None) -> str | None:
    if value is None or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
        return None
    text = " ".join(value.split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


class MidpenFetchError(RuntimeError):
    """A whole-list error that leaves the durable catalog refresh retryable (NFR-8)."""
