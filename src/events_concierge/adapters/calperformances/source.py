"""Approved-origin Cal Performances collection adapter (FR-3.1/FR-3.7/FR-10.3/FR-10.4).

Cal Performances exposes its public event collection through a WordPress ``cp_event`` endpoint.
This adapter accepts only that closed, finite collection, constructs every declared list page itself,
and emits same-origin event-page handoffs. It never follows event, ticket, or registration links;
signs in; purchases; or mutates the source.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser, Node

from ...domain.catalog_sources import CatalogSource
from ...domain.catalog_window import collection_end_at, collection_reference_time
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent

_FETCH_TIMEOUT_S = 15.0
_MAX_RESPONSE_BYTES = 20_000_000
_PAGE_SIZE = 100
_HORIZON_DAYS = 90
_RESPONSIVE_BLOCK_COUNT = 2
_CONTROL_CHARACTER_LIMIT = 32
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_NONNEGATIVE_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")
_TITLE_IDENTITY_TRANSLATION = str.maketrans(
    {"\N{LEFT SINGLE QUOTATION MARK}": "'", "\N{RIGHT SINGLE QUOTATION MARK}": "'"}
)
_EVENT_PATH = re.compile(r"^/events/(?:[a-z0-9]+(?:-[a-z0-9]+)*/)+$")
_CALENDAR_TIMESTAMP = re.compile(
    r"^(?:0[1-9]|1[0-2])/(?:0[1-9]|[12][0-9]|3[01])/[0-9]{4} "
    r"(?:0[1-9]|1[0-2]):[0-5][0-9] (?:am|pm)$"
)
_TICKETS_START_AT = re.compile(r"^Tickets start at \$(?:[1-9][0-9]*)(?:\.[0-9]{1,2})?$")
_CANCELLED_OR_POSTPONED = re.compile(r"\b(?:cancell?ed|postponed)\b", re.IGNORECASE)
_VIRTUAL_OR_ONLINE = re.compile(r"\b(?:virtual|online|zoom)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _CalPerformancesPublisher:
    """One closed list/handoff pair; registry data cannot create a generic WordPress client (FR-10.3)."""

    source_key: str
    api_host: str
    api_path: str
    handoff_host: str
    page_limit: int
    min_interval_ms: int


@dataclass(frozen=True, slots=True)
class _CalPerformancesPage:
    """One verified WordPress collection page and its mandatory pagination metadata (NFR-8)."""

    records: list[dict[str, object]]
    total: int
    total_pages: int


@dataclass(frozen=True, slots=True)
class _CalendarBlock:
    """The fixture-proven structured event snippet duplicated for responsive layouts (FR-3.7)."""

    title: str
    start: str
    end: str
    timezone: str
    location: str


_PUBLISHERS = {
    "calperformances-events": _CalPerformancesPublisher(
        source_key="calperformances-events",
        api_host="calperformances.org",
        api_path="/wp-json/wp/v2/cp_event",
        handoff_host="calperformances.org",
        page_limit=4,
        min_interval_ms=1_500,
    ),
}


class CalPerformancesCatalogFetcher:
    """Fetch Cal Performances' closed public collection at a human cadence (FR-10.3/FR-10.4)."""

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
        """Return future physical primary occurrences without fetching their handoff pages (FR-3.1)."""
        if source.mode is not CatalogSourceMode.CAL_PERFORMANCES_JSON:
            raise ValueError(f"unsupported Cal Performances source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("Cal Performances catalog source must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError(
                "Cal Performances source must use the reviewed public collection endpoint"
            )

        now = _as_local_time(collection_reference_time(source, self._now()))
        horizon_start = datetime.combine(now.date(), datetime.min.time(), _LOCAL_TIME_ZONE)
        horizon_end = collection_end_at(
            source, horizon_start + timedelta(days=source.collection_horizon_days),
        ).astimezone(_LOCAL_TIME_ZONE)
        records = await self._fetch_pages(source, publisher)
        candidates: list[CandidateEvent] = []
        for record in records:
            candidate = _candidate_from_record(
                record, source, publisher, now, horizon_start, horizon_end
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def _fetch_pages(
        self, source: CatalogSource, publisher: _CalPerformancesPublisher
    ) -> list[dict[str, object]]:
        """Read every header-declared collection page or fail rather than publish a partial list (NFR-8)."""
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            first_page = await self._fetch_page(client, source, publisher, 1)
            _validate_first_page_metadata(first_page, source.source_key, publisher.page_limit)
            if first_page.total == 0:
                return []

            records: list[dict[str, object]] = []
            seen_identifiers: set[str] = set()
            for page_number in range(1, first_page.total_pages + 1):
                page = (
                    first_page
                    if page_number == 1
                    else await self._fetch_page(client, source, publisher, page_number)
                )
                if page.total != first_page.total or page.total_pages != first_page.total_pages:
                    raise CalPerformancesFetchError(
                        f"Cal Performances page {page_number} changed pagination totals for "
                        f"{source.source_key}"
                    )
                expected_count = _expected_page_count(first_page.total, page_number)
                if len(page.records) != expected_count:
                    raise CalPerformancesFetchError(
                        f"Cal Performances page {page_number} returned an inconsistent record count for "
                        f"{source.source_key}"
                    )
                _append_unique_records(records, seen_identifiers, page.records, source.source_key)
            if len(records) != first_page.total:
                raise CalPerformancesFetchError(
                    f"Cal Performances collection returned an inconsistent total for {source.source_key}"
                )
            return records

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        publisher: _CalPerformancesPublisher,
        page_number: int,
    ) -> _CalPerformancesPage:
        """Read exactly one constructed collection page; redirect or endpoint drift is a source failure (FR-10.3)."""
        url = _page_url(publisher, page_number)
        if not _is_approved_endpoint_url(source, publisher, url, page_number):
            raise CalPerformancesFetchError(
                f"Cal Performances page {page_number} left the approved collection endpoint"
            )
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise CalPerformancesFetchError(
                f"Cal Performances page {page_number} failed for {source.source_key}: {exc}"
            ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url), page_number
        ):
            raise CalPerformancesFetchError(
                f"Cal Performances page {page_number} left the approved collection endpoint"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CalPerformancesFetchError(
                f"Cal Performances page {page_number} failed for {source.source_key}: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise CalPerformancesFetchError(
                f"Cal Performances page {page_number} exceeded its reviewed response-size limit"
            )
        return _page_from_response(response, source.source_key, page_number)

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host floor before every anonymous collection GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("Cal Performances URL must include a host")
        interval_s = min_interval_ms / 1_000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _CalPerformancesPublisher | None:
    """Return the sole reviewed profile only when origin, query, cap, and pacing agree (FR-10.3/10.4)."""
    publisher = _PUBLISHERS.get(source.source_key)
    if publisher is None:
        return None
    try:
        parsed = urlsplit(source.seed_url)
    except ValueError:
        return None
    expected_query = (("per_page", str(_PAGE_SIZE)), ("page", "1"))
    return (
        publisher
        if (
            source.page_limit == publisher.page_limit
            and source.min_interval_ms == publisher.min_interval_ms
            and source.approved_origins == (f"https://{publisher.api_host}",)
            and _has_exact_https_authority(source.seed_url, publisher.api_host)
            and parsed.path == publisher.api_path
            and parse_qsl(parsed.query, keep_blank_values=True) == list(expected_query)
            and not parsed.fragment
        )
        else None
    )


def _is_approved_endpoint_url(
    source: CatalogSource,
    publisher: _CalPerformancesPublisher,
    url: str,
    page_number: int,
) -> bool:
    """Require registry approval plus the exact internally constructed collection page (FR-10.3)."""
    if page_number <= 0 or page_number > publisher.page_limit:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    expected_query = (("per_page", str(_PAGE_SIZE)), ("page", str(page_number)))
    return (
        source.allows_url(url)
        and _has_exact_https_authority(url, publisher.api_host)
        and parsed.path == publisher.api_path
        and parse_qsl(parsed.query, keep_blank_values=True) == list(expected_query)
        and not parsed.fragment
    )


def _has_exact_https_authority(url: str, host: str) -> bool:
    """Reject userinfo, explicit ports, and lookalike hosts even if URL parsing finds the same hostname (FR-10.3)."""
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


def _page_url(publisher: _CalPerformancesPublisher, page_number: int) -> str:
    """Construct the fixed collection query rather than following publisher-supplied pagination links (FR-10.3)."""
    if page_number <= 0:
        raise ValueError("Cal Performances page number must be positive")
    query = urlencode((("per_page", str(_PAGE_SIZE)), ("page", str(page_number))))
    return urlunsplit(("https", publisher.api_host, publisher.api_path, query, ""))


def _page_from_response(
    response: httpx.Response, source_key: str, page_number: int
) -> _CalPerformancesPage:
    """Validate the bounded JSON collection and mandatory WordPress pagination headers (NFR-8)."""
    total = _header_nonnegative_int(response.headers.get("X-WP-Total"))
    total_pages = _header_nonnegative_int(response.headers.get("X-WP-TotalPages"))
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise CalPerformancesFetchError(
            f"Cal Performances page {page_number} returned invalid JSON for {source_key}"
        ) from exc
    if (
        total is None
        or total_pages is None
        or not isinstance(payload, list)
        or len(payload) > _PAGE_SIZE
    ):
        raise CalPerformancesFetchError(
            f"Cal Performances page {page_number} returned an invalid collection envelope for {source_key}"
        )
    records: list[dict[str, object]] = []
    for raw_record in payload:
        record = _as_object_dict(raw_record)
        if record is None:
            raise CalPerformancesFetchError(
                f"Cal Performances page {page_number} contained a non-object record for {source_key}"
            )
        records.append(record)
    return _CalPerformancesPage(records, total, total_pages)


def _header_nonnegative_int(value: str | None) -> int | None:
    if value is None or _NONNEGATIVE_INTEGER.fullmatch(value) is None:
        return None
    return int(value)


def _validate_first_page_metadata(
    page: _CalPerformancesPage, source_key: str, page_limit: int
) -> None:
    """Require a finite complete collection shape before any records are normalized (NFR-8)."""
    if page.total == 0:
        if page.total_pages != 0 or page.records:
            raise CalPerformancesFetchError(
                f"Cal Performances source {source_key} returned inconsistent empty pagination metadata"
            )
        return
    expected_pages = (page.total + _PAGE_SIZE - 1) // _PAGE_SIZE
    if page.total_pages != expected_pages or page.total_pages <= 0 or page.total_pages > page_limit:
        raise CalPerformancesFetchError(
            f"Cal Performances source {source_key} exceeds or contradicts its reviewed page sequence"
        )


def _expected_page_count(total: int, page_number: int) -> int:
    """Return the exact number of records the fixed WordPress collection page must contain (NFR-8)."""
    remaining = total - ((page_number - 1) * _PAGE_SIZE)
    return min(_PAGE_SIZE, remaining)


def _append_unique_records(
    records: list[dict[str, object]],
    seen_identifiers: set[str],
    page_records: list[dict[str, object]],
    source_key: str,
) -> None:
    """Prove raw collection completeness through globally unique positive WordPress IDs (NFR-8)."""
    for record in page_records:
        identifier = _positive_id(record.get("id"))
        if identifier is None:
            raise CalPerformancesFetchError(
                f"Cal Performances collection contained an invalid WordPress ID for {source_key}"
            )
        if identifier in seen_identifiers:
            raise CalPerformancesFetchError(
                f"Cal Performances collection repeated WordPress ID {identifier} for {source_key}"
            )
        seen_identifiers.add(identifier)
        records.append(record)


def _candidate_from_record(
    record: dict[str, object],
    source: CatalogSource,
    publisher: _CalPerformancesPublisher,
    now: datetime,
    horizon_start: datetime,
    horizon_end: datetime,
) -> CandidateEvent | None:
    """Normalize only a complete, future physical primary occurrence from source-structured fields (FR-3.7)."""
    identifier = _positive_id(record.get("id"))
    outer_title = _rendered_text(record.get("title"))
    content = _rendered_markup(record.get("content"))
    handoff = _handoff_reference(record.get("link"), publisher)
    if identifier is None or outer_title is None or content is None or handoff is None:
        return None
    document = HTMLParser(content)
    calendar = _calendar_from_document(document)
    if calendar is None:
        return None
    start_at = _calendar_timestamp(calendar.start)
    end_at = _calendar_timestamp(calendar.end)
    if (
        start_at is None
        or end_at is None
        or end_at <= start_at
        or calendar.timezone != _LOCAL_TIME_ZONE.key
        or not _titles_agree(outer_title, calendar.title)
        or _CANCELLED_OR_POSTPONED.search(outer_title) is not None
        or _CANCELLED_OR_POSTPONED.search(calendar.title) is not None
        or _CANCELLED_OR_POSTPONED.search(calendar.location) is not None
        or _VIRTUAL_OR_ONLINE.search(outer_title) is not None
        or _VIRTUAL_OR_ONLINE.search(calendar.title) is not None
        or _VIRTUAL_OR_ONLINE.search(calendar.location) is not None
        or start_at <= now
        or not horizon_start <= start_at < horizon_end
    ):
        return None
    path, handoff_url = handoff
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"calperformances:{source.source_key}:{identifier}:{start_at.isoformat()}",
        title=calendar.title,
        start_at=start_at,
        registration_url=handoff_url,
        end_at=end_at,
        venue_name=calendar.location,
        description="",
        price_status=_price_status(document),
        raw={
            "id": identifier,
            "path": path,
            "calendar_start": calendar.start,
            "calendar_end": calendar.end,
            "timezone": calendar.timezone,
            "location": calendar.location,
        },
    )


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Narrow an untrusted JSON value to a string-keyed object before source-field access (FR-3.7)."""
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _positive_id(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and _POSITIVE_INTEGER.fullmatch(value) is not None:
        return value
    return None


def _rendered_text(value: object) -> str | None:
    rendered = _rendered_value(value)
    return (
        _text(HTMLParser(rendered).text(separator=" ", strip=True))
        if rendered is not None
        else None
    )


def _rendered_markup(value: object) -> str | None:
    """Keep markup only to parse the reviewed structured blocks, never as event description (FR-3.7)."""
    return _rendered_value(value)


def _rendered_value(value: object) -> str | None:
    rendered_object = _as_object_dict(value)
    rendered = rendered_object.get("rendered") if rendered_object is not None else None
    return (
        rendered
        if isinstance(rendered, str)
        and rendered.strip()
        and not _has_unsafe_control_characters(rendered)
        else None
    )


def _calendar_from_document(document: HTMLParser) -> _CalendarBlock | None:
    """Accept only the two agreeing responsive ``addeventatc`` blocks verified in the source fixture (FR-3.7)."""
    blocks = document.css("div.addeventatc")
    if len(blocks) != _RESPONSIVE_BLOCK_COUNT:
        return None
    first = _calendar_block(blocks[0])
    second = _calendar_block(blocks[1])
    return first if first is not None and first == second else None


def _calendar_block(block: Node) -> _CalendarBlock | None:
    title = _single_span_text(block, "title")
    start = _single_span_text(block, "start")
    end = _single_span_text(block, "end")
    timezone = _single_span_text(block, "timezone")
    location = _single_span_text(block, "location")
    if title is None or start is None or end is None or timezone is None or location is None:
        return None
    return _CalendarBlock(
        title=title,
        start=start,
        end=end,
        timezone=timezone,
        location=location,
    )


def _single_span_text(block: Node, class_name: str) -> str | None:
    spans = block.css(f"span.{class_name}")
    return _node_text(spans[0]) if len(spans) == 1 else None


def _calendar_timestamp(value: str) -> datetime | None:
    """Attach the exact source timezone only to a valid, unambiguous published local time (FR-3.7)."""
    if _CALENDAR_TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        naive = datetime.strptime(value, "%m/%d/%Y %I:%M %p")
    except ValueError:
        return None
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


def _titles_agree(outer_title: str, calendar_title: str) -> bool:
    """Require source-title identity, allowing only the captured curly-versus-ASCII apostrophe rendering (FR-3.7)."""
    return outer_title.translate(_TITLE_IDENTITY_TRANSLATION) == calendar_title.translate(
        _TITLE_IDENTITY_TRANSLATION
    )


def _price_status(document: HTMLParser) -> PriceStatus:
    """Assert paid only for two agreeing source blocks with the exact reviewed price phrase (FR-3.7/FR-4.6)."""
    prices = [_node_text(node) for node in document.css("div.event-price-block")]
    if len(prices) != _RESPONSIVE_BLOCK_COUNT:
        return PriceStatus.UNKNOWN
    first, second = prices
    return (
        PriceStatus.PAID
        if first is not None and first == second and _TICKETS_START_AT.fullmatch(first) is not None
        else PriceStatus.UNKNOWN
    )


def _handoff_reference(
    value: object, publisher: _CalPerformancesPublisher
) -> tuple[str, str] | None:
    """Keep only a safe same-origin ``/events/...`` human handoff and never request it (FR-3.1/FR-10.3)."""
    if not isinstance(value, str) or _has_control_characters(value):
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != publisher.handoff_host
        or parsed.netloc.casefold() != publisher.handoff_host
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _EVENT_PATH.fullmatch(parsed.path) is None
    ):
        return None
    return parsed.path, urlunsplit(("https", publisher.handoff_host, parsed.path, "", ""))


def _node_text(element: Node | None) -> str | None:
    return _text(element.text(separator=" ", strip=True)) if element is not None else None


def _text(value: str | None) -> str | None:
    if value is None or _has_unsafe_control_characters(value):
        return None
    normalized = " ".join(value.split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", normalized) or None


def _has_unsafe_control_characters(value: str) -> bool:
    """Allow ordinary HTML line formatting but reject all other C0 control characters (FR-10.3)."""
    return any(
        ord(character) < _CONTROL_CHARACTER_LIMIT and character not in "\t\n\r"
        for character in value
    )


def _has_control_characters(value: str) -> bool:
    """Reject every C0 character in a URL; unlike HTML whitespace, it has no safe URL meaning (FR-10.3)."""
    return any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value)


def _as_local_time(value: datetime) -> datetime:
    """Treat a test-supplied naive clock as source-local time (FR-3.7)."""
    return (
        value.astimezone(_LOCAL_TIME_ZONE)
        if value.tzinfo is not None
        else value.replace(tzinfo=_LOCAL_TIME_ZONE)
    )


class CalPerformancesFetchError(RuntimeError):
    """A complete-list failure that leaves the durable catalog refresh retryable (NFR-8)."""
