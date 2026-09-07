"""Approved-origin BiblioCommons RSS adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

Reviewed Bay Area library publishers expose anonymous event instances through BiblioCommons RSS.
This adapter accepts only its closed publisher map at a human cadence, emits discovery-only handoff
candidates, and never follows an event link, signs in, registers, purchases, or mutates the source.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent, GeoPoint
from ...infra.logging import get_logger
from ...ports.sources import SourceTransientError

_log = get_logger("bibliocommons.source")

_FETCH_TIMEOUT_S = 15.0
_TRANSPORT_RETRY_ATTEMPTS = 3
_TRANSPORT_RETRY_BASE_DELAY_S = 0.5
_TRANSPORT_EXHAUSTED_RETRY_DELAY_S = 15.0
_ITEMS_PER_PAGE = 25
_MAX_RESPONSE_BYTES = 1_000_000
_BC_NAMESPACE = "http://bibliocommons.com/rss/1.0/modules/event/"
_MAX_EVENT_ID_LENGTH = 32
_CONTROL_CHARACTER_LIMIT = 32
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True, slots=True)
class _BiblioCommonsPublisher:
    """One closed reviewed library feed; registry data cannot open a new publisher (FR-10.3)."""

    source_key: str
    feed_path: str
    handoff_host: str
    location_ids: frozenset[str]
    fixed_query: tuple[tuple[str, str], ...]
    page_limit: int
    require_physical_location: bool = False
    window_days: int | None = None
    min_interval_ms: int | None = None


@dataclass(frozen=True, slots=True)
class _BiblioCommonsWindow:
    """A publisher-owned local date request plus its half-open UTC retention boundary (FR-3.1)."""

    start_date: date
    end_date: date
    horizon_end: datetime


_OAKLAND_LOCATION_IDS = (
    "81A",
    "AAA",
    "ASA",
    "BRA",
    "CCA",
    "DMA",
    "EAA",
    "ELA",
    "GGA",
    "KGA",
    "LVA",
    "MEA",
    "MOA",
    "OHR",
    "PMA",
    "RRA",
    "TMA",
    "WAS",
    "XXA",
    "XXJ",
    "XXY",
    "676de3ef74596c36004dc6bd",
    "6a0c99a4e8af4a2f00739408",
    "6a517185e9de6536001ad4d7",
)
_SAN_JOSE_LOCATION_IDS = (
    "00",
    "01",
    "02",
    "03",
    "04",
    "05",
    "06",
    "07",
    "08",
    "09",
    "10",
    "11",
    "12",
    "14",
    "15",
    "16",
    "17",
    "18",
    "19",
    "21",
    "22",
    "23",
    "24",
    "25",
    "26",
)
_CONTRA_COSTA_LOCATION_IDS = (
    "43",
    "7",
    "25",
    "19",
    "14",
    "60",
    "4",
    "21",
    "26",
    "24",
    "11",
    "16",
    "6",
    "55",
    "9",
    "94",
    "1",
    "17",
    "12",
    "15",
    "27",
    "23",
    "8",
    "2",
    "13",
)
_MARIN_COUNTY_LOCATION_IDS = (
    "MB",
    "MC",
    "MM",
    "MF",
    "MI",
    "MA",
    "MN",
    "MP",
    "MH",
    "MS",
)
_SMCL_ALL_PHYSICAL_LOCATION_IDS = (
    "1A",
    "1B",
    "1R",
    "1E",
    "1F",
    "1H",
    "1M",
    "1N",
    "1Z",
    "1P",
    "1V",
    "1S",
    "1W",
)
_ALAMEDA_COUNTY_ALL_PHYSICAL_LOCATION_IDS = (
    "ALB",
    "CSV",
    "CTV",
    "CHY",
    "DUB",
    "FRM",
    "NWK",
    "NLS",
    "SLZ",
    "UCY",
)
_SCCLD_ALL_PHYSICAL_LOCATION_IDS = (
    "CA",
    "CU",
    "GI",
    "LA",
    "MI",
    "MH",
    "SA",
    "WO",
)


_BIBLIOCOMMONS_PUBLISHERS = {
    "sccld-milpitas-events": _BiblioCommonsPublisher(
        source_key="sccld-milpitas-events",
        feed_path="/v2/libraries/sccl/rss/events",
        handoff_host="sccl.bibliocommons.com",
        location_ids=frozenset({"MI"}),
        fixed_query=(("locations", "MI"),),
        page_limit=15,
    ),
    "sccld-saratoga-events": _BiblioCommonsPublisher(
        source_key="sccld-saratoga-events",
        feed_path="/v2/libraries/sccl/rss/events",
        handoff_host="sccl.bibliocommons.com",
        location_ids=frozenset({"SA"}),
        fixed_query=(("locations", "SA"),),
        page_limit=15,
    ),
    "sccld-all-physical-branches-events": _BiblioCommonsPublisher(
        source_key="sccld-all-physical-branches-events",
        feed_path="/v2/libraries/sccl/rss/events",
        handoff_host="sccl.bibliocommons.com",
        location_ids=frozenset(_SCCLD_ALL_PHYSICAL_LOCATION_IDS),
        fixed_query=tuple(
            ("locations", location_id) for location_id in _SCCLD_ALL_PHYSICAL_LOCATION_IDS
        ),
        # Raised from 50 by migration 0167: the feed reached 1,207 of that cap's 1,250 items and
        # then published nothing at all for six days. The cap is a cliff, not a budget.
        page_limit=120,
        require_physical_location=True,
        window_days=90,
        min_interval_ms=5_000,
    ),
    "palo-alto-library-events": _BiblioCommonsPublisher(
        source_key="palo-alto-library-events",
        feed_path="/v2/libraries/paloalto/rss/events",
        handoff_host="paloalto.bibliocommons.com",
        location_ids=frozenset(
            {
                "C",
                "D",
                "M",
                "R",
                "T",
                "59f90ac2544fb02f009aef14",
                "5abaf8dc0e11f74000fab02c",
                "5aff6473dbec2d340081afb1",
                "61315bb9fef4bf3e00e32d64",
            }
        ),
        fixed_query=(),
        page_limit=15,
        require_physical_location=True,
        window_days=90,
    ),
    "smcl-millbrae-events": _BiblioCommonsPublisher(
        source_key="smcl-millbrae-events",
        feed_path="/v2/libraries/smcl/rss/events",
        handoff_host="smcl.bibliocommons.com",
        location_ids=frozenset({"1M"}),
        fixed_query=(("locations", "1M"),),
        page_limit=15,
        require_physical_location=True,
        window_days=90,
    ),
    "smcl-all-physical-branches-events": _BiblioCommonsPublisher(
        source_key="smcl-all-physical-branches-events",
        feed_path="/v2/libraries/smcl/rss/events",
        handoff_host="smcl.bibliocommons.com",
        location_ids=frozenset(_SMCL_ALL_PHYSICAL_LOCATION_IDS),
        fixed_query=tuple(
            ("locations", location_id) for location_id in _SMCL_ALL_PHYSICAL_LOCATION_IDS
        ),
        page_limit=150,
        require_physical_location=True,
        window_days=90,
        min_interval_ms=5_000,
    ),
    "alameda-county-library-all-physical-branches-events": _BiblioCommonsPublisher(
        source_key="alameda-county-library-all-physical-branches-events",
        feed_path="/v2/libraries/aclibrary/rss/events",
        handoff_host="aclibrary.bibliocommons.com",
        location_ids=frozenset(_ALAMEDA_COUNTY_ALL_PHYSICAL_LOCATION_IDS),
        fixed_query=tuple(
            ("locations", location_id) for location_id in _ALAMEDA_COUNTY_ALL_PHYSICAL_LOCATION_IDS
        ),
        page_limit=40,
        require_physical_location=True,
        window_days=90,
        min_interval_ms=5_000,
    ),
    "alameda-county-library-fremont-events": _BiblioCommonsPublisher(
        source_key="alameda-county-library-fremont-events",
        feed_path="/v2/libraries/aclibrary/rss/events",
        handoff_host="aclibrary.bibliocommons.com",
        location_ids=frozenset({"FRM"}),
        fixed_query=(("locations", "FRM"),),
        page_limit=7,
        require_physical_location=True,
        window_days=90,
    ),
    "oakland-public-library-events": _BiblioCommonsPublisher(
        source_key="oakland-public-library-events",
        feed_path="/v2/libraries/oaklandlibrary/rss/events",
        handoff_host="oaklandlibrary.bibliocommons.com",
        location_ids=frozenset(_OAKLAND_LOCATION_IDS),
        fixed_query=tuple(("locations", location_id) for location_id in _OAKLAND_LOCATION_IDS),
        page_limit=60,
        require_physical_location=True,
        window_days=90,
    ),
    "san-jose-public-library-events": _BiblioCommonsPublisher(
        source_key="san-jose-public-library-events",
        feed_path="/v2/libraries/sjpl/rss/events",
        handoff_host="sjpl.bibliocommons.com",
        location_ids=frozenset(_SAN_JOSE_LOCATION_IDS),
        fixed_query=tuple(("locations", location_id) for location_id in _SAN_JOSE_LOCATION_IDS),
        # Raised from 160 by migration 0167: the feed reached 3,820 of that cap's 4,000 items and
        # then published nothing at all for thirteen days.
        page_limit=320,
        require_physical_location=True,
        window_days=90,
    ),
    "contra-costa-county-library-events": _BiblioCommonsPublisher(
        source_key="contra-costa-county-library-events",
        feed_path="/v2/libraries/ccclib/rss/events",
        handoff_host="ccclib.bibliocommons.com",
        location_ids=frozenset(_CONTRA_COSTA_LOCATION_IDS),
        fixed_query=tuple(("locations", location_id) for location_id in _CONTRA_COSTA_LOCATION_IDS),
        page_limit=105,
        require_physical_location=True,
        window_days=90,
    ),
    "marin-county-free-library-events": _BiblioCommonsPublisher(
        source_key="marin-county-free-library-events",
        feed_path="/v2/libraries/marinlibrary/rss/events",
        handoff_host="marinlibrary.bibliocommons.com",
        location_ids=frozenset(_MARIN_COUNTY_LOCATION_IDS),
        fixed_query=tuple(("locations", location_id) for location_id in _MARIN_COUNTY_LOCATION_IDS),
        page_limit=25,
        require_physical_location=True,
        window_days=90,
        min_interval_ms=5_000,
    ),
}


class BiblioCommonsCatalogFetcher:
    """Fetch a bounded official library RSS feed without an authenticated session (FR-10.3/10.4)."""

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
        self._last_request_by_host: dict[str, tuple[float, float]] = {}
        self._pacing_lock = asyncio.Lock()

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        """Return future physical/hybrid library events from at most the reviewed page cap (FR-3.1)."""
        if source.mode is not CatalogSourceMode.BIBLIOCOMMONS_RSS:
            raise ValueError(f"unsupported BiblioCommons source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("BiblioCommons catalog sources must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("BiblioCommons source must use a reviewed publisher RSS endpoint")

        now = _as_utc(self._now())
        window = _window_for_publisher(publisher, now)
        candidates = await self._fetch_pages(source, publisher, now, window)
        return _deduplicate(candidates)

    async def _fetch_pages(
        self,
        source: CatalogSource,
        publisher: _BiblioCommonsPublisher,
        now: datetime,
        window: _BiblioCommonsWindow | None,
    ) -> list[CandidateEvent]:
        """Walk fixed pages until a short page; a full cap is a retryable source failure (NFR-8)."""
        candidates: list[CandidateEvent] = []
        seen_guids: set[str] = set()
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            for page in range(1, source.page_limit + 1):
                url = _page_url(source, publisher, window, page)
                response = await self._response_or_error(
                    client, source, publisher, window, url, page
                )
                items = _items_from_response(response, source.source_key, page)
                for item in items:
                    reference = _event_reference(item, publisher)
                    if reference is None:
                        _log.warning(
                            "bibliocommons_event_identity_invalid", source_key=source.source_key
                        )
                        continue
                    guid, registration_url = reference
                    if guid in seen_guids:
                        raise BiblioCommonsFetchError(
                            f"BiblioCommons page sequence repeated an event guid for {source.source_key}"
                        )
                    seen_guids.add(guid)
                    candidate = _candidate_from_item(
                        item,
                        source,
                        publisher,
                        now,
                        window.horizon_end if window is not None else None,
                        guid,
                        registration_url,
                    )
                    if candidate is not None:
                        candidates.append(candidate)
                if len(items) < _ITEMS_PER_PAGE:
                    return candidates
        raise BiblioCommonsFetchError(
            f"BiblioCommons source {source.source_key} exceeds its reviewed {source.page_limit}-page cap"
        )

    async def _response_or_error(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        publisher: _BiblioCommonsPublisher,
        window: _BiblioCommonsWindow | None,
        url: str,
        page: int,
    ) -> httpx.Response:
        """Request only the reviewed origin/path; redirects are never followed (FR-10.3)."""
        if not _is_approved_feed_url(source, publisher, window, url, page):
            raise BiblioCommonsFetchError(f"BiblioCommons page {page} left the approved endpoint")
        try:
            response = await self._get_with_transport_retry(
                client,
                source,
                url,
                page,
            )
        except httpx.TransportError as exc:
            raise SourceTransientError(
                f"BiblioCommons page {page} transient transport failure "
                f"for {source.source_key}: {exc}",
                retry_after_seconds=_TRANSPORT_EXHAUSTED_RETRY_DELAY_S,
            ) from exc
        except httpx.HTTPError as exc:
            raise BiblioCommonsFetchError(
                f"BiblioCommons page {page} failed for {source.source_key}: {exc}"
            ) from exc
        if response.is_redirect or not _is_approved_feed_url(
            source, publisher, window, str(response.url), page
        ):
            raise BiblioCommonsFetchError(f"BiblioCommons page {page} left the approved endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BiblioCommonsFetchError(
                f"BiblioCommons page {page} failed for {source.source_key}: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise BiblioCommonsFetchError(
                f"BiblioCommons page {page} exceeded its reviewed response-size limit"
            )
        return response

    async def _get_with_transport_retry(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        url: str,
        page: int,
    ) -> httpx.Response:
        """Retry bounded idempotent GET transport failures while retaining host pacing."""
        for attempt in range(1, _TRANSPORT_RETRY_ATTEMPTS + 1):
            try:
                await self._wait_for_host_slot(url, source.min_interval_ms)
                return await client.get(url, follow_redirects=False)
            except httpx.TransportError as exc:
                if attempt >= _TRANSPORT_RETRY_ATTEMPTS:
                    raise
                retry_delay = _TRANSPORT_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                _log.warning(
                    "bibliocommons_transport_retry",
                    source_key=source.source_key,
                    page=page,
                    attempt=attempt,
                    retry_delay_seconds=retry_delay,
                    error=str(exc),
                )
                await self._sleep(retry_delay)
        raise AssertionError("bounded BiblioCommons transport retry loop exhausted")

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Honor the stricter adjacent reviewed per-host cadence before every feed GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("BiblioCommons URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_by_host.get(host)
            if previous is not None:
                previous_at, previous_interval_s = previous
                required_interval_s = max(previous_interval_s, interval_s)
                remaining = required_interval_s - (self._clock() - previous_at)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_by_host[host] = (self._clock(), interval_s)


def _publisher_for_source(source: CatalogSource) -> _BiblioCommonsPublisher | None:
    """Return an approved publisher only when its source key and static seed agree exactly (FR-10.3)."""
    publisher = _BIBLIOCOMMONS_PUBLISHERS.get(source.source_key)
    if publisher is None:
        return None
    parsed = urlsplit(source.seed_url)
    return (
        publisher
        if (
            parsed.scheme.casefold() == "https"
            and parsed.netloc.casefold() == "gateway.bibliocommons.com"
            and parsed.path == publisher.feed_path
            and parse_qsl(parsed.query, keep_blank_values=True) == list(publisher.fixed_query)
            and source.page_limit == publisher.page_limit
            and (
                publisher.min_interval_ms is None
                or source.min_interval_ms == publisher.min_interval_ms
            )
            and not parsed.fragment
        )
        else None
    )


def _is_approved_feed_url(
    source: CatalogSource,
    publisher: _BiblioCommonsPublisher,
    window: _BiblioCommonsWindow | None,
    url: str,
    page: int,
) -> bool:
    """Require both registry approval and the exact BiblioCommons feed path (FR-10.3)."""
    parsed = urlsplit(url)
    return (
        source.allows_url(url)
        and parsed.netloc.casefold() == "gateway.bibliocommons.com"
        and parsed.path == publisher.feed_path
        and parse_qsl(parsed.query, keep_blank_values=True)
        == _request_parameters(publisher, window, page)
        and not parsed.fragment
    )


def _page_url(
    source: CatalogSource,
    publisher: _BiblioCommonsPublisher,
    window: _BiblioCommonsWindow | None,
    page: int,
) -> str:
    """Build only the closed publisher query and positive page, never trusting a feed next URL (FR-10.3)."""
    parsed = urlsplit(source.seed_url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(_request_parameters(publisher, window, page)),
            "",
        )
    )


def _request_parameters(
    publisher: _BiblioCommonsPublisher, window: _BiblioCommonsWindow | None, page: int
) -> list[tuple[str, str]]:
    """Return the exact reviewed query shape for one page (FR-10.3)."""
    if page <= 0:
        raise ValueError("BiblioCommons page must be positive")
    parameters = list(publisher.fixed_query)
    if window is not None:
        parameters.extend(
            (
                ("startDate", window.start_date.isoformat()),
                ("endDate", window.end_date.isoformat()),
            )
        )
    if page > 1:
        parameters.append(("page", str(page)))
    return parameters


def _window_for_publisher(
    publisher: _BiblioCommonsPublisher, now: datetime
) -> _BiblioCommonsWindow | None:
    """Create a closed local-date discovery window only for publishers reviewed with one (FR-3.1)."""
    if publisher.window_days is None:
        return None
    start_date = now.astimezone(_PACIFIC).date()
    end_date = start_date + timedelta(days=publisher.window_days)
    horizon_end = datetime.combine(end_date, time.min, tzinfo=_PACIFIC).astimezone(UTC)
    return _BiblioCommonsWindow(start_date, end_date, horizon_end)


def _items_from_response(
    response: httpx.Response, source_key: str, page: int
) -> list[ElementTree.Element]:
    """Parse one bounded RSS 2.0 page, rejecting malformed or entity-bearing XML (NFR-8)."""
    payload = response.content
    upper_payload = payload.upper()
    if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
        raise BiblioCommonsFetchError(
            f"BiblioCommons page {page} contained unsupported XML declarations for {source_key}"
        )
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise BiblioCommonsFetchError(
            f"BiblioCommons page {page} returned invalid XML for {source_key}"
        ) from exc
    if root.tag != "rss":
        raise BiblioCommonsFetchError(
            f"BiblioCommons page {page} returned no RSS root for {source_key}"
        )
    channel = root.find("channel")
    if channel is None:
        raise BiblioCommonsFetchError(
            f"BiblioCommons page {page} returned no channel for {source_key}"
        )
    return list(channel.findall("item"))


def _candidate_from_item(
    item: ElementTree.Element,
    source: CatalogSource,
    publisher: _BiblioCommonsPublisher,
    now: datetime,
    horizon_end: datetime | None,
    guid: str,
    registration_url: str,
) -> CandidateEvent | None:
    """Normalize one public event instance, failing closed on identity, time, or handoff (FR-3.7)."""
    if _is_true(_bc_text(item, "is_cancelled")):
        return None
    location = item.find(_bc_tag("location"))
    if not _is_publisher_location(location, publisher):
        return None
    if _is_true(_bc_text(item, "is_virtual")) and not _has_physical_location(location):
        return None
    title = _text(_child_text(item, "title"))
    start_at = _parse_utc(_bc_text(item, "start_date"))
    if title is None or start_at is None:
        _log.warning("bibliocommons_event_incomplete", source_key=source.source_key)
        return None
    if start_at < now or (horizon_end is not None and start_at >= horizon_end):
        return None
    end_at = _parse_utc(_bc_text(item, "end_date"))
    if end_at is not None and end_at <= start_at:
        end_at = None
    venue_name, city, geo = _location(location)
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=(
            f"bibliocommons:{source.source_key}:{_event_identifier(guid)}:{start_at.isoformat()}"
        ),
        title=title,
        start_at=start_at,
        registration_url=registration_url,
        end_at=end_at,
        venue_name=venue_name,
        city=city,
        geo=geo,
        description=_text(_child_text(item, "description")) or "",
        price_status=PriceStatus.UNKNOWN,
        raw={
            "guid": guid,
            "is_cancelled": _bc_text(item, "is_cancelled"),
            "is_virtual": _bc_text(item, "is_virtual"),
        },
    )


def _event_reference(
    item: ElementTree.Element, publisher: _BiblioCommonsPublisher
) -> tuple[str, str] | None:
    """Require matching published guid/link detail URLs before materializing an event (FR-3.8)."""
    guid = _event_url(_child_text(item, "guid"), publisher)
    link = _event_url(_child_text(item, "link"), publisher)
    if guid is None or link is None or _event_identifier(guid) != _event_identifier(link):
        return None
    return guid, link


def _event_url(value: str | None, publisher: _BiblioCommonsPublisher) -> str | None:
    """Keep a safe event-detail handoff but never request it from the catalog worker (FR-3.1)."""
    if value is None or any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() != publisher.handoff_host
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith("/events/")
            or parsed.path == "/events/"
            or parsed.fragment
        ):
            return None
    except ValueError:
        return None
    return urlunsplit(("https", publisher.handoff_host, parsed.path, parsed.query, ""))


def _event_identifier(guid: str) -> str:
    """Prefer the stable event-detail path suffix while retaining a deterministic safe fallback (FR-3.8)."""
    path_suffix = urlsplit(guid).path.rstrip("/").rsplit("/", maxsplit=1)[-1]
    if path_suffix and len(path_suffix) <= _MAX_EVENT_ID_LENGTH:
        return path_suffix
    return hashlib.sha256(guid.encode()).hexdigest()[:_MAX_EVENT_ID_LENGTH]


def _has_physical_location(location: ElementTree.Element | None) -> bool:
    if location is None:
        return False
    return (
        _text(_bc_child_text(location, "name")) is not None
        or _text(_bc_child_text(location, "street")) is not None
    )


def _is_publisher_location(
    location: ElementTree.Element | None, publisher: _BiblioCommonsPublisher
) -> bool:
    if location is None:
        return False
    if _bc_child_text(location, "id") not in publisher.location_ids:
        return False
    return not publisher.require_physical_location or _has_physical_location(location)


def _location(
    location: ElementTree.Element | None,
) -> tuple[str | None, str | None, GeoPoint | None]:
    if location is None:
        return None, None, None
    name = _text(_bc_child_text(location, "name"))
    details = _text(_bc_child_text(location, "location_details"))
    venue_name = (
        f"{name} — {details}" if name is not None and details is not None else name or details
    )
    city = _text(_bc_child_text(location, "city"))
    latitude = _coordinate(_bc_child_text(location, "latitude"), -90.0, 90.0)
    longitude = _coordinate(_bc_child_text(location, "longitude"), -180.0, 180.0)
    geo = GeoPoint(latitude, longitude) if latitude is not None and longitude is not None else None
    return venue_name, city, geo


def _coordinate(value: str | None, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    try:
        coordinate = float(value)
    except ValueError:
        return None
    if not math.isfinite(coordinate) or not minimum <= coordinate <= maximum:
        return None
    return coordinate


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_true(value: str | None) -> bool:
    return value is not None and value.strip().casefold() in {"true", "1"}


def _bc_tag(local_name: str) -> str:
    return f"{{{_BC_NAMESPACE}}}{local_name}"


def _bc_text(element: ElementTree.Element, local_name: str) -> str | None:
    return _text_value(element.findtext(_bc_tag(local_name)))


def _bc_child_text(element: ElementTree.Element, local_name: str) -> str | None:
    return _text_value(element.findtext(_bc_tag(local_name)))


def _child_text(element: ElementTree.Element, local_name: str) -> str | None:
    return _text_value(element.findtext(local_name))


def _text_value(value: str | None) -> str | None:
    return value.strip() if value is not None and value.strip() else None


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(HTMLParser(value).text(separator=" ", strip=True).split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


def _deduplicate(candidates: list[CandidateEvent]) -> list[CandidateEvent]:
    """Keep one observation per exact publisher event if the feed repeats an item (FR-3.8)."""
    unique: dict[str, CandidateEvent] = {}
    for candidate in candidates:
        unique.setdefault(candidate.source_event_id, candidate)
    return list(unique.values())


class BiblioCommonsFetchError(RuntimeError):
    """A whole-feed error that must leave the durable refresh retryable (NFR-8)."""
