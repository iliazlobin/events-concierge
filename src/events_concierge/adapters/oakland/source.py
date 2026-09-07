"""Approved-origin City of Oakland event-calendar adapter (FR-3.1/FR-3.7/FR-10.3/FR-10.4).

The City of Oakland publishes public event detail URLs in its XML sitemap.  This adapter reads
only that exact sitemap and its bounded same-origin ``/Event-Calendar/`` detail URLs.  It emits
factual, physical-event handoffs only; it never follows a registration link, image, map, contact
link, or other page-local URL; signs in; purchases; or mutates the source.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser, Node

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent, GeoPoint

_FETCH_TIMEOUT_S = 15.0
_MAX_SITEMAP_RESPONSE_BYTES = 4_000_000
_MAX_DETAIL_RESPONSE_BYTES = 1_000_000
_RAW_OCCURRENCE_LIMIT = 400
_FUTURE_OCCURRENCE_LIMIT = 200
_CONTROL_CHARACTER_LIMIT = 32
_SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_BAY_AREA_BOUNDS = (37.0, -123.0, 38.5, -121.5)
_MIN_LATITUDE = -90.0
_MAX_LATITUDE = 90.0
_MIN_LONGITUDE = -180.0
_MAX_LONGITUDE = 180.0
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_INTEGER = re.compile(r"^[0-9]+$")
_ADDRESS_CITY = re.compile(
    r",\s*(?P<city>[A-Za-z][A-Za-z .'-]*?),\s*CA(?:\s+[0-9]{5}(?:-[0-9]{4})?)?$",
    re.IGNORECASE,
)
_VIRTUAL_OR_ONLINE = re.compile(r"\b(?:online|virtual|zoom)\b", re.IGNORECASE)
_COORDINATES = re.compile(
    r"^(?P<lat>[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))\s*,\s*"
    r"(?P<lon>[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))$"
)


@dataclass(frozen=True, slots=True)
class _OaklandPublisher:
    """One closed sitemap/detail profile; registry data cannot open a generic HTML client (FR-10.3)."""

    source_key: str
    host: str
    sitemap_path: str
    event_path_prefix: str
    page_limit: int
    min_interval_ms: int
    refresh_interval_minutes: int


@dataclass(frozen=True, slots=True)
class _PhysicalLocation:
    """One explicit, local map marker; no venue or city is inferred from other page text (FR-3.7)."""

    venue_name: str | None
    address: str
    geo: GeoPoint
    city: str | None


_PUBLISHERS = {
    "oakland-city-events": _OaklandPublisher(
        source_key="oakland-city-events",
        host="www.oaklandca.gov",
        sitemap_path="/sitemap.xml",
        event_path_prefix="/Event-Calendar/",
        page_limit=160,
        min_interval_ms=5_000,
        refresh_interval_minutes=1_440,
    ),
}


class OaklandCatalogFetcher:
    """Fetch Oakland's closed sitemap/detail catalog at a human cadence (FR-10.3/FR-10.4)."""

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
        """Return future physical Oakland occurrences from the reviewed sitemap/detail sequence (FR-3.1)."""
        if source.mode is not CatalogSourceMode.OAKLAND_HTML:
            raise ValueError(f"unsupported Oakland source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("Oakland catalog source must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("Oakland source must use the reviewed sitemap and detail cap")

        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            sitemap = await self._sitemap_response_or_error(client, source, publisher)
            detail_urls = _detail_urls_from_sitemap(sitemap, source, publisher)
            now = _as_local_time(self._now())
            candidates_by_source_id: dict[str, CandidateEvent] = {}
            for detail_url in detail_urls:
                response = await self._detail_response_or_error(
                    client, source, publisher, detail_url
                )
                for candidate in _candidates_from_detail(
                    response, source, publisher, detail_url, now
                ):
                    existing = candidates_by_source_id.get(candidate.source_event_id)
                    if existing is None:
                        candidates_by_source_id[candidate.source_event_id] = candidate
                        continue
                    if candidate != existing:
                        raise OaklandFetchError(
                            f"Oakland sitemap/detail sequence conflicted on {candidate.source_event_id} "
                            f"for {source.source_key}"
                        )
        return list(candidates_by_source_id.values())

    async def _sitemap_response_or_error(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        publisher: _OaklandPublisher,
    ) -> httpx.Response:
        """Read only the exact anonymous sitemap; redirects and endpoint drift fail closed (FR-10.3)."""
        url = source.seed_url
        if not _is_approved_sitemap_url(source, publisher, url):
            raise OaklandFetchError("Oakland request left the approved sitemap endpoint")
        return await self._response_or_error(
            client,
            source,
            publisher,
            url,
            is_sitemap=True,
        )

    async def _detail_response_or_error(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        publisher: _OaklandPublisher,
        url: str,
    ) -> httpx.Response:
        """Read one enumerated source detail only; external and pagination links are never requested (FR-3.1)."""
        if not _is_approved_detail_url(source, publisher, url):
            raise OaklandFetchError("Oakland detail request left the approved event endpoint")
        return await self._response_or_error(
            client,
            source,
            publisher,
            url,
            is_sitemap=False,
        )

    async def _response_or_error(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        publisher: _OaklandPublisher,
        url: str,
        *,
        is_sitemap: bool,
    ) -> httpx.Response:
        """Apply cadence and verify the final URL before accepting one bounded anonymous GET (FR-10.3/10.4)."""
        kind = "sitemap" if is_sitemap else "detail"
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise OaklandFetchError(
                f"Oakland {kind} request failed for {source.source_key}: {exc}"
            ) from exc
        is_expected_url = (
            _is_approved_sitemap_url(source, publisher, str(response.url))
            if is_sitemap
            else _is_approved_detail_url(source, publisher, str(response.url))
        )
        if response.is_redirect or not is_expected_url:
            raise OaklandFetchError(f"Oakland {kind} request left the approved endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OaklandFetchError(
                f"Oakland {kind} request failed for {source.source_key}: {exc}"
            ) from exc
        max_bytes = _MAX_SITEMAP_RESPONSE_BYTES if is_sitemap else _MAX_DETAIL_RESPONSE_BYTES
        if len(response.content) > max_bytes:
            raise OaklandFetchError(
                f"Oakland {kind} response exceeded its reviewed response-size limit"
            )
        return response

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host floor before every anonymous sitemap or detail GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("Oakland URL must include a host")
        interval_s = min_interval_ms / 1_000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _OaklandPublisher | None:
    """Return the sole reviewed profile only when all registry limits agree exactly (FR-10.3/FR-10.4)."""
    publisher = _PUBLISHERS.get(source.source_key)
    if publisher is None:
        return None
    return (
        publisher
        if (
            source.page_limit == publisher.page_limit
            and source.min_interval_ms == publisher.min_interval_ms
            and source.refresh_interval_minutes == publisher.refresh_interval_minutes
            and source.approved_origins == (f"https://{publisher.host}",)
            and _has_exact_https_authority(source.seed_url, publisher.host)
            and _exact_path(source.seed_url, publisher.sitemap_path)
        )
        else None
    )


def _is_approved_sitemap_url(source: CatalogSource, publisher: _OaklandPublisher, url: str) -> bool:
    """Permit only the exact query-free public sitemap, not an arbitrary XML endpoint (FR-10.3)."""
    return (
        source.allows_url(url)
        and _has_exact_https_authority(url, publisher.host)
        and _exact_path(url, publisher.sitemap_path)
    )


def _is_approved_detail_url(source: CatalogSource, publisher: _OaklandPublisher, url: str) -> bool:
    """Permit only a same-origin, query-free detail path enumerated by the sitemap (FR-10.3)."""
    if not source.allows_url(url) or not _has_exact_https_authority(url, publisher.host):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        not parsed.query
        and not parsed.fragment
        and _is_event_detail_path(parsed.path, publisher.event_path_prefix)
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


def _exact_path(url: str, path: str) -> bool:
    """Keep the reviewed sitemap query-free because arbitrary query surfaces are unreviewed (FR-10.3)."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.path == path and not parsed.query and not parsed.fragment


def _is_event_detail_path(path: str, prefix: str) -> bool:
    """Accept a nonempty normal detail path, never a traversal-shaped sitemap URL (FR-10.3)."""
    if not path.startswith(prefix):
        return False
    decoded = unquote(path)
    if not decoded.startswith(prefix):
        return False
    segments = decoded.removeprefix(prefix).split("/")
    return bool(segments) and all(segment not in {"", ".", ".."} for segment in segments)


def _detail_urls_from_sitemap(
    response: httpx.Response, source: CatalogSource, publisher: _OaklandPublisher
) -> list[str]:
    """Extract every approved event-detail URL or fail before a partial capped fetch (NFR-8)."""
    payload = response.content
    upper_payload = payload.upper()
    if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
        raise OaklandFetchError(
            f"Oakland sitemap contained unsupported XML declarations for {source.source_key}"
        )
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise OaklandFetchError(
            f"Oakland sitemap returned invalid XML for {source.source_key}"
        ) from exc
    namespace = f"{{{_SITEMAP_NAMESPACE}}}"
    if root.tag != f"{namespace}urlset":
        raise OaklandFetchError(
            f"Oakland sitemap returned no namespaced URL set for {source.source_key}"
        )

    detail_urls: list[str] = []
    seen: set[str] = set()
    for entry in root:
        if entry.tag != f"{namespace}url":
            raise OaklandFetchError(
                f"Oakland sitemap returned an invalid URL entry for {source.source_key}"
            )
        locations = [child for child in entry if child.tag == f"{namespace}loc"]
        if len(locations) != 1:
            raise OaklandFetchError(
                f"Oakland sitemap returned an invalid URL location for {source.source_key}"
            )
        url = _sitemap_event_url(locations[0].text, source, publisher)
        _validate_optional_lastmod(entry, namespace, source.source_key)
        if url is None:
            continue
        if url in seen:
            raise OaklandFetchError(
                f"Oakland sitemap repeated an event detail URL for {source.source_key}"
            )
        seen.add(url)
        detail_urls.append(url)
        if len(detail_urls) >= source.page_limit:
            raise OaklandFetchError(
                f"Oakland source {source.source_key} reached its reviewed {source.page_limit}-detail cap"
            )
    if not detail_urls:
        raise OaklandFetchError(
            f"Oakland sitemap returned no approved event detail URLs for {source.source_key}"
        )
    return detail_urls


def _sitemap_event_url(
    value: str | None, source: CatalogSource, publisher: _OaklandPublisher
) -> str | None:
    """Normalize one sitemap location only when it is an approved concrete event-detail URL (FR-10.3)."""
    if value is None or _has_control_characters(value):
        raise OaklandFetchError(
            f"Oakland sitemap returned an invalid URL location for {source.source_key}"
        )
    url = value.strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise OaklandFetchError(
            f"Oakland sitemap returned an invalid URL location for {source.source_key}"
        ) from exc
    if not parsed.path.startswith(publisher.event_path_prefix):
        return None
    if not _is_approved_detail_url(source, publisher, url):
        raise OaklandFetchError(
            f"Oakland sitemap returned an unapproved event detail URL for {source.source_key}"
        )
    return urlunsplit(("https", publisher.host, parsed.path, "", ""))


def _validate_optional_lastmod(entry: ElementTree.Element, namespace: str, source_key: str) -> None:
    """Accept the optional factual ISO metadata while rejecting a malformed sitemap contract (NFR-8)."""
    lastmods = [child for child in entry if child.tag == f"{namespace}lastmod"]
    if len(lastmods) > 1:
        raise OaklandFetchError(f"Oakland sitemap repeated lastmod metadata for {source_key}")
    if not lastmods:
        return
    value = lastmods[0].text
    if value is None or _has_control_characters(value):
        raise OaklandFetchError(
            f"Oakland sitemap returned invalid lastmod metadata for {source_key}"
        )
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise OaklandFetchError(
            f"Oakland sitemap returned invalid lastmod metadata for {source_key}"
        ) from exc


def _candidates_from_detail(
    response: httpx.Response,
    source: CatalogSource,
    publisher: _OaklandPublisher,
    requested_url: str,
    now: datetime,
) -> list[CandidateEvent]:
    """Map only complete, future physical occurrences; no narrative or external page-local link is read (FR-3.7)."""
    try:
        payload = response.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OaklandFetchError(f"Oakland detail was not UTF-8 for {source.source_key}") from exc
    document = HTMLParser(payload)
    main_nodes = document.css("#main-content")
    if len(main_nodes) != 1:
        raise OaklandFetchError(
            f"Oakland detail returned no unique main content for {source.source_key}"
        )
    main = main_nodes[0]
    title_nodes = main.css("h1.oc-page-title")
    occurrence_lists = main.css("ul.multi-date-list.future-events-list")
    if (
        not title_nodes
        and not occurrence_lists
        and _is_text_only_calendar_landing(main, requested_url)
    ):
        # Oakland's sitemap mixes real details with category roots such as /Event-Calendar/EWD.
        # Those roots render only their path label inside #main-content. Keep genuine one-segment
        # event details, while treating this exact non-event shape as an empty catalog page.
        return []
    if len(title_nodes) != 1 or len(occurrence_lists) != 1:
        raise OaklandFetchError(
            f"Oakland detail no longer matches its reviewed title/occurrence contract for {source.source_key}"
        )
    title = _node_text(title_nodes[0])
    if title is None:
        raise OaklandFetchError(f"Oakland detail returned an invalid title for {source.source_key}")
    occurrence_nodes = main.css("ul.multi-date-list.future-events-list > li.multi-date-item")
    if len(occurrence_nodes) > _RAW_OCCURRENCE_LIMIT:
        raise OaklandFetchError(
            "Oakland detail exceeded its reviewed "
            f"{_RAW_OCCURRENCE_LIMIT} raw-occurrence scan limit for {source.source_key}"
        )
    parsed_occurrences = [_occurrence_times(occurrence) for occurrence in occurrence_nodes]
    future_occurrences: list[tuple[datetime, datetime | None]] = [
        (start_at, end_at)
        for start_at, end_at in parsed_occurrences
        if start_at is not None and start_at > now
    ]
    if len(future_occurrences) > _FUTURE_OCCURRENCE_LIMIT:
        raise OaklandFetchError(
            "Oakland detail exceeded its reviewed "
            f"{_FUTURE_OCCURRENCE_LIMIT} future-occurrence emission limit "
            f"for {source.source_key}"
        )
    if any(start_at is not None for start_at, _ in parsed_occurrences) and not future_occurrences:
        # The sitemap can retain expired detail pages after their final occurrence. Do not require
        # today's location markup for a page that cannot emit a current catalog candidate.
        return []
    canonical_url = _canonical_url(document, source, publisher, requested_url)
    location = _physical_location(main, source.source_key)
    if location is None:
        return []
    if _VIRTUAL_OR_ONLINE.search(title) is not None or (
        location.venue_name is not None
        and _VIRTUAL_OR_ONLINE.search(location.venue_name) is not None
    ):
        return []
    cost = _cost(main, source.source_key)
    categories = _categories(main)
    candidates: list[CandidateEvent] = []
    for start_at, end_at in future_occurrences:
        raw: dict[str, object] = {
            "canonical_url": canonical_url,
            "venue_name": location.venue_name,
            "address": location.address,
            "latitude": location.geo.lat,
            "longitude": location.geo.lon,
            "cost": cost,
            "categories": categories,
        }
        candidates.append(
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id=(
                    f"oakland:{source.source_key}:{canonical_url}:{start_at.isoformat()}"
                ),
                title=title,
                start_at=start_at,
                registration_url=canonical_url,
                end_at=end_at,
                venue_name=location.venue_name,
                geo=location.geo,
                city=location.city,
                description="",
                price_status=PriceStatus.FREE if cost == "Free" else PriceStatus.UNKNOWN,
                raw=raw,
            )
        )
    return candidates


def _is_text_only_calendar_landing(main: Node, requested_url: str) -> bool:
    """Recognize only Oakland's empty calendar-root shape, never a structured detail redesign."""
    if any(node.tag != "_comment" for node in main.iter(include_text=False)):
        return False
    label = _node_text(main)
    try:
        path = urlsplit(requested_url).path.rstrip("/")
    except ValueError:
        return False
    slug = unquote(path.rsplit("/", 1)[-1]) if path else ""
    expected_label = _text(slug.replace("-", " "))
    if label is None or expected_label is None:
        return False
    label_tokens = _calendar_landing_label_tokens(label)
    return bool(label_tokens) and label_tokens == _calendar_landing_label_tokens(expected_label)


def _calendar_landing_label_tokens(value: str) -> tuple[str, ...]:
    """Ignore punctuation separators, but preserve every factual word when matching a path label."""
    return tuple(token.casefold() for token in re.findall(r"[^\W_]+", value))


def _canonical_url(
    document: HTMLParser,
    source: CatalogSource,
    publisher: _OaklandPublisher,
    requested_url: str,
) -> str:
    """Use the declared safe canonical, or only the already-verified final detail URL (FR-3.1/FR-10.3)."""
    links = [
        link
        for link in document.css("link[href]")
        if "canonical" in (link.attributes.get("rel") or "").casefold().split()
    ]
    if not links:
        return requested_url
    if len(links) != 1:
        raise OaklandFetchError(
            f"Oakland detail returned multiple canonical links for {source.source_key}"
        )
    href = links[0].attributes.get("href")
    if href is None or _has_control_characters(href):
        raise OaklandFetchError(
            f"Oakland detail returned an invalid canonical link for {source.source_key}"
        )
    canonical = urljoin(requested_url, href.strip())
    if not _is_approved_detail_url(source, publisher, canonical):
        raise OaklandFetchError("Oakland detail canonical left the approved event endpoint")
    parsed = urlsplit(canonical)
    return urlunsplit(("https", publisher.host, parsed.path, "", ""))


def _physical_location(main: Node, source_key: str) -> _PhysicalLocation | None:
    """Require one explicit Bay Area marker; its venue may remain honestly unknown (FR-3.7)."""
    markers = main.css(".gmap-marker")
    if not markers:
        return None
    if len(markers) != 1:
        raise OaklandFetchError(f"Oakland detail returned multiple map markers for {source_key}")
    marker = markers[0]
    venue_nodes = marker.css(".gmap-info > h2")
    address_nodes = marker.css(".gmap-address")
    coordinate_nodes = marker.css(".gmap-latlong")
    if len(venue_nodes) > 1 or len(address_nodes) != 1 or len(coordinate_nodes) != 1:
        raise OaklandFetchError(
            f"Oakland detail no longer matches its reviewed physical-location contract for {source_key}"
        )
    venue_name = _node_text(venue_nodes[0]) if venue_nodes else None
    address = _node_text(address_nodes[0])
    geo = _coordinates(_node_text(coordinate_nodes[0]))
    if (venue_nodes and venue_name is None) or address is None or geo is None:
        raise OaklandFetchError(
            f"Oakland detail returned invalid physical-location fields for {source_key}"
        )
    if not _inside_bay_area(geo):
        return None
    return _PhysicalLocation(
        venue_name=venue_name,
        address=address,
        geo=geo,
        city=_city_from_address(address),
    )


def _cost(main: Node, source_key: str) -> str | None:
    """Keep the exact published cost text only; only exact ``Free`` establishes free status (FR-3.7)."""
    values = main.css(".event-snapshot p.side-box-cost")
    if len(values) > 1:
        raise OaklandFetchError(f"Oakland detail returned multiple cost values for {source_key}")
    return _node_text(values[0]) if values else None


def _categories(main: Node) -> list[str]:
    """Retain only compact factual category labels, never their filtering or registration URLs (FR-3.7)."""
    categories: list[str] = []
    for node in main.css(".categories-list-container .categories-list > li > a"):
        value = _node_text(node)
        if value is not None and value not in categories:
            categories.append(value)
    return categories


def _occurrence_times(node: Node) -> tuple[datetime | None, datetime | None]:
    """Parse the source's machine-readable local start/end attributes, including optional end (FR-3.7)."""
    start_values: tuple[str | None, str | None, str | None, str | None, str | None] = (
        node.attributes.get("data-start-year"),
        node.attributes.get("data-start-month"),
        node.attributes.get("data-start-day"),
        node.attributes.get("data-start-hour"),
        node.attributes.get("data-start-mins"),
    )
    start = _local_timestamp(start_values)
    if start is None:
        return None, None
    end_values: tuple[str | None, str | None, str | None, str | None, str | None] = (
        node.attributes.get("data-end-year"),
        node.attributes.get("data-end-month"),
        node.attributes.get("data-end-day"),
        node.attributes.get("data-end-hour"),
        node.attributes.get("data-end-mins"),
    )
    if all(value in {None, ""} for value in end_values):
        return start, None
    if any(value in {None, ""} for value in end_values):
        return None, None
    end = _local_timestamp(end_values)
    return (start, end) if end is not None and end > start else (None, None)


def _local_timestamp(
    values: tuple[str | None, str | None, str | None, str | None, str | None],
) -> datetime | None:
    """Attach LA time only to a complete, real, unambiguous local wall-clock timestamp (FR-3.7)."""
    if any(value is None or _INTEGER.fullmatch(value) is None for value in values):
        return None
    year, month, day, hour, minute = values
    if year is None or month is None or day is None or hour is None or minute is None:
        return None
    try:
        naive = datetime(int(year), int(month), int(day), int(hour), int(minute))
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


def _coordinates(value: str | None) -> GeoPoint | None:
    """Parse only one finite source map coordinate pair (FR-3.7)."""
    if value is None:
        return None
    match = _COORDINATES.fullmatch(value)
    if match is None:
        return None
    lat = float(match.group("lat"))
    lon = float(match.group("lon"))
    if not _MIN_LATITUDE <= lat <= _MAX_LATITUDE or not _MIN_LONGITUDE <= lon <= _MAX_LONGITUDE:
        return None
    return GeoPoint(lat=lat, lon=lon)


def _inside_bay_area(geo: GeoPoint) -> bool:
    """Keep the nine-county catalog local without equating the city publisher with Oakland proper."""
    min_latitude, min_longitude, max_latitude, max_longitude = _BAY_AREA_BOUNDS
    return min_latitude <= geo.lat <= max_latitude and min_longitude <= geo.lon <= max_longitude


def _city_from_address(address: str) -> str | None:
    """Return only an explicit ``City, CA`` address component; never assume the publisher's city."""
    match = _ADDRESS_CITY.search(address)
    return _text(match.group("city")) if match is not None else None


def _as_local_time(value: datetime) -> datetime:
    """Make the injected clock comparable with the source's local wall-clock timestamps (FR-3.7)."""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_LOCAL_TIME_ZONE)


def _node_text(element: Node | None) -> str | None:
    return _text(element.text(separator=" ", strip=True)) if element is not None else None


def _text(value: str | None) -> str | None:
    if value is None or _has_unsafe_control_characters(value):
        return None
    normalized = " ".join(value.split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", normalized) or None


def _has_unsafe_control_characters(value: str) -> bool:
    """Allow layout whitespace but reject other C0 controls in source text (FR-10.3)."""
    return any(
        ord(character) < _CONTROL_CHARACTER_LIMIT and character not in "\t\n\r"
        for character in value
    )


def _has_control_characters(value: str) -> bool:
    """Reject every C0 control in a URL because it has no safe URL meaning (FR-10.3)."""
    return any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value)


class OaklandFetchError(RuntimeError):
    """A whole-source error that leaves the durable catalog refresh retryable (NFR-8)."""
