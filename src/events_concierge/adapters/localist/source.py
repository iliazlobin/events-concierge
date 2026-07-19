"""Approved-origin Localist catalog adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

Stanford, San José State University, and UCSF publish their official Events calendars through
anonymous Localist APIs. This adapter accepts only their closed, reviewed endpoint set in a fixed
Bay Area 90-day window, emits discovery-only handoff candidates, and never follows an event,
ticket, or registration URL; signs in; RSVPs; purchases; or mutates the source.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from time import monotonic
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent, GeoPoint
from ...infra.logging import get_logger

_log = get_logger("localist.source")

_FETCH_TIMEOUT_S = 15.0
_MAX_RESPONSE_BYTES = 5_000_000
_API_PATH = "/api/2/events"
_EVENT_PATH_PREFIX = "/event/"
_PAGE_SIZE = 100
_HORIZON_DAYS = 90
_CONTROL_CHARACTER_LIMIT = 32
_STANFORD_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_POSITIVE_COST = re.compile(
    r"(?:[$€£]\s*|\b(?:usd|dollars?)\s*)([1-9]\d*(?:\.\d{1,2})?)"
    r"|\b([1-9]\d*(?:\.\d{1,2})?)\s*(?:usd|dollars?)\b",
    re.IGNORECASE,
)
_BAY_AREA_BOUNDS = (37.0, -123.0, 38.5, -121.5)


@dataclass(frozen=True, slots=True)
class _LocalistPublisher:
    """One closed publisher endpoint/hand-off pair; registry data cannot open a new host (FR-10.3)."""

    source_key: str
    api_host: str
    handoff_host: str


_LOCALIST_PUBLISHERS = {
    "stanford-events": _LocalistPublisher(
        source_key="stanford-events",
        api_host="events.stanford.edu",
        handoff_host="events.stanford.edu",
    ),
    "sjsu-events": _LocalistPublisher(
        source_key="sjsu-events",
        api_host="events.sjsu.edu",
        handoff_host="events.sjsu.edu",
    ),
    "ucsf-events": _LocalistPublisher(
        source_key="ucsf-events",
        api_host="calendar.ucsf.edu",
        handoff_host="calendar.ucsf.edu",
    ),
}


@dataclass(frozen=True, slots=True)
class _LocalistPage:
    """One validated publisher page plus the declared finite page count (NFR-8)."""

    events: list[dict[str, object]]
    total_pages: int


class LocalistCatalogFetcher:
    """Fetch a closed reviewed Localist occurrence feed at a human cadence (FR-10.3/10.4)."""

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
        self._now = now or (lambda: datetime.now(_STANFORD_TIME_ZONE))
        self._clock = clock or monotonic
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._last_request_at: dict[str, float] = {}
        self._pacing_lock = asyncio.Lock()

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        """Return future physical/hybrid occurrences in the reviewed local horizon (FR-3.1)."""
        if source.mode is not CatalogSourceMode.LOCALIST_JSON:
            raise ValueError(f"unsupported Localist source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("Localist catalog sources must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("Localist source must use one of the reviewed public events endpoints")

        now = _as_stanford_time(self._now())
        horizon_start = datetime.combine(now.date(), datetime.min.time(), _STANFORD_TIME_ZONE)
        horizon_end = horizon_start + timedelta(days=_HORIZON_DAYS)
        events = await self._fetch_pages(
            source, publisher, horizon_start.date(), horizon_end.date()
        )
        candidates: list[CandidateEvent] = []
        seen_source_ids: set[str] = set()
        for event in events:
            for candidate in _candidates_from_event(
                event, source, publisher, now, horizon_start, horizon_end
            ):
                if candidate.source_event_id in seen_source_ids:
                    raise LocalistFetchError(
                        f"Localist page sequence repeated an event occurrence for {source.source_key}"
                    )
                seen_source_ids.add(candidate.source_event_id)
                candidates.append(candidate)
        return candidates

    async def _fetch_pages(
        self, source: CatalogSource, publisher: _LocalistPublisher, start_day: date, end_day: date
    ) -> list[dict[str, object]]:
        """Read every publisher-declared page, failing rather than returning a truncated catalog (NFR-8)."""
        events: list[dict[str, object]] = []
        expected_total_pages: int | None = None
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            for page_number in range(1, source.page_limit + 1):
                url = _request_url(source.seed_url, start_day, end_day, page_number)
                response = await self._response_or_error(
                    client, source, publisher, url, page_number
                )
                page = _page_from_response(response, source.source_key, page_number)
                if expected_total_pages is None:
                    expected_total_pages = page.total_pages
                    if expected_total_pages > source.page_limit:
                        raise LocalistFetchError(
                            f"Localist source {source.source_key} exceeds its reviewed "
                            f"{source.page_limit}-page cap"
                        )
                    if expected_total_pages == 0:
                        return []
                elif page.total_pages != expected_total_pages:
                    raise LocalistFetchError(
                        f"Localist source {source.source_key} changed its page total mid-refresh"
                    )
                if not page.events:
                    raise LocalistFetchError(
                        f"Localist page {page_number} was unexpectedly empty for {source.source_key}"
                    )
                if page_number < expected_total_pages and len(page.events) != _PAGE_SIZE:
                    raise LocalistFetchError(
                        f"Localist page {page_number} was short before its declared final page"
                    )
                events.extend(page.events)
                if page_number == expected_total_pages:
                    return events
        raise LocalistFetchError(
            f"Localist source {source.source_key} exceeded its reviewed page sequence"
        )

    async def _response_or_error(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        publisher: _LocalistPublisher,
        url: str,
        page: int,
    ) -> httpx.Response:
        """Request only the reviewed Localist list resource; redirects are source failures (FR-10.3)."""
        if not _is_approved_endpoint_url(source, publisher, url):
            raise LocalistFetchError(f"Localist page {page} left the approved endpoint")
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise LocalistFetchError(
                f"Localist page {page} failed for {source.source_key}: {exc}"
            ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url)
        ):
            raise LocalistFetchError(f"Localist page {page} left the approved endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LocalistFetchError(
                f"Localist page {page} failed for {source.source_key}: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise LocalistFetchError(
                f"Localist page {page} exceeded its reviewed response-size limit"
            )
        return response

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host floor before every anonymous Localist GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("Localist URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _LocalistPublisher | None:
    """Return a reviewed publisher only when its registry key and endpoint agree exactly (FR-10.3)."""
    publisher = _LOCALIST_PUBLISHERS.get(source.source_key)
    if publisher is None:
        return None
    parsed = urlsplit(source.seed_url)
    return (
        publisher
        if (
            parsed.scheme.casefold() == "https"
            and parsed.netloc.casefold() == publisher.api_host
            and parsed.path == _API_PATH
            and not parsed.query
            and not parsed.fragment
        )
        else None
    )


def _is_approved_endpoint_url(
    source: CatalogSource, publisher: _LocalistPublisher, url: str
) -> bool:
    """Require registry approval and the exact Localist list resource path (FR-10.3)."""
    parsed = urlsplit(url)
    return (
        source.allows_url(url)
        and parsed.netloc.casefold() == publisher.api_host
        and parsed.path == _API_PATH
    )


def _request_url(seed_url: str, start_day: date, end_day: date, page: int) -> str:
    """Construct the fixed bounded Localist query without trusting source response links (FR-10.3)."""
    if page <= 0:
        raise ValueError("Localist page must be positive")
    min_latitude, min_longitude, max_latitude, max_longitude = _BAY_AREA_BOUNDS
    parameters = (
        ("start", start_day.isoformat()),
        ("end", end_day.isoformat()),
        ("bounds", f"{min_latitude},{min_longitude},{max_latitude},{max_longitude}"),
        ("pp", str(_PAGE_SIZE)),
        ("sort", "date"),
        ("direction", "asc"),
        ("page", str(page)),
    )
    parsed = urlsplit(seed_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(parameters), ""))


def _page_from_response(
    response: httpx.Response, source_key: str, page_number: int
) -> _LocalistPage:
    """Validate the documented wrapper/page shape before a batch can enter the catalog (NFR-8)."""
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise LocalistFetchError(
            f"Localist page {page_number} returned invalid JSON for {source_key}"
        ) from exc
    root = _as_object_dict(payload)
    page = _as_object_dict(root.get("page")) if root is not None else None
    date_summary = _as_object_dict(root.get("date")) if root is not None else None
    raw_events = root.get("events") if root is not None else None
    if page is None or date_summary is None or not isinstance(raw_events, list):
        raise LocalistFetchError(
            f"Localist page {page_number} returned an invalid object shape for {source_key}"
        )
    if (
        _positive_int(page.get("current")) != page_number
        or _positive_int(page.get("size")) != _PAGE_SIZE
    ):
        raise LocalistFetchError(
            f"Localist page {page_number} returned inconsistent pagination for {source_key}"
        )
    total_pages = _nonnegative_int(page.get("total"))
    if (
        total_pages is None
        or not _date_summary_is_valid(date_summary)
        or len(raw_events) > _PAGE_SIZE
    ):
        raise LocalistFetchError(
            f"Localist page {page_number} returned invalid metadata for {source_key}"
        )
    events: list[dict[str, object]] = []
    for raw_wrapper in raw_events:
        wrapper = _as_object_dict(raw_wrapper)
        event = _as_object_dict(wrapper.get("event")) if wrapper is not None else None
        if event is None:
            raise LocalistFetchError(
                f"Localist page {page_number} contained an invalid event wrapper for {source_key}"
            )
        events.append(event)
    return _LocalistPage(events, total_pages)


def _candidates_from_event(
    event: dict[str, object],
    source: CatalogSource,
    publisher: _LocalistPublisher,
    now: datetime,
    horizon_start: datetime,
    horizon_end: datetime,
) -> list[CandidateEvent]:
    """Expand one publisher occurrence wrapper only when its public local metadata is complete (FR-3.7)."""
    event_id = _positive_id(event.get("id"))
    title = _text(event.get("title"))
    venue_name, city, geo = _location(event)
    registration_url = _handoff_url(event.get("localist_url"), publisher)
    if (
        event_id is None
        or title is None
        or venue_name is None
        or geo is None
        or registration_url is None
        or not _is_eligible_event(event)
    ):
        _log.warning("localist_event_incomplete", source_key=source.source_key)
        return []
    raw_instances = event.get("event_instances")
    if not isinstance(raw_instances, list) or not raw_instances:
        _log.warning("localist_event_instances_invalid", source_key=source.source_key)
        return []
    candidates: list[CandidateEvent] = []
    for raw_wrapper in raw_instances:
        wrapper = _as_object_dict(raw_wrapper)
        instance = _as_object_dict(wrapper.get("event_instance")) if wrapper is not None else None
        candidate = _candidate_from_instance(
            event,
            instance,
            source,
            publisher,
            event_id,
            title,
            venue_name,
            city,
            geo,
            registration_url,
            now,
            horizon_start,
            horizon_end,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _candidate_from_instance(
    event: dict[str, object],
    instance: dict[str, object] | None,
    source: CatalogSource,
    publisher: _LocalistPublisher,
    event_id: str,
    title: str,
    venue_name: str,
    city: str | None,
    geo: GeoPoint,
    registration_url: str,
    now: datetime,
    horizon_start: datetime,
    horizon_end: datetime,
) -> CandidateEvent | None:
    """Normalize one occurrence with a stable instance identity and exact local horizon (FR-3.7/3.8)."""
    if instance is None or _positive_id(instance.get("event_id")) != event_id:
        return None
    instance_id = _positive_id(instance.get("id"))
    start_at = _parse_timestamp(instance.get("start"))
    end_at = _parse_timestamp(instance.get("end")) if instance.get("end") is not None else None
    if instance_id is None or start_at is None:
        return None
    if end_at is not None and end_at <= start_at:
        return None
    if start_at < now or not horizon_start <= start_at < horizon_end:
        return None
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"localist:{source.source_key}:{event_id}:{instance_id}",
        title=title,
        start_at=start_at,
        registration_url=registration_url,
        end_at=end_at,
        venue_name=venue_name,
        city=city,
        geo=geo,
        description=_text(event.get("description_text")) or "",
        price_status=_price_status(event),
        raw={
            "event_id": event_id,
            "instance_id": instance_id,
            "status": event.get("status"),
            "experience": event.get("experience"),
            "free": event.get("free"),
            "ticket_cost": event.get("ticket_cost"),
        },
    )


def _is_eligible_event(event: dict[str, object]) -> bool:
    """Keep only explicitly public, live, physical/hybrid Localist listings (FR-3.1/FR-10.3)."""
    status = _plain_string(event.get("status"))
    experience = _plain_string(event.get("experience"))
    return (
        status is not None
        and status.casefold() == "live"
        and experience is not None
        and experience.casefold() in {"inperson", "hybrid"}
        and event.get("private") is False
        and event.get("rejected") is False
    )


def _location(event: dict[str, object]) -> tuple[str | None, str | None, GeoPoint | None]:
    """Retain only source-provided physical venue/city/coordinates inside the reviewed bounds (FR-3.7)."""
    geo = _as_object_dict(event.get("geo"))
    latitude = _coordinate(geo.get("latitude") if geo is not None else None, -90.0, 90.0)
    longitude = _coordinate(geo.get("longitude") if geo is not None else None, -180.0, 180.0)
    if latitude is None or longitude is None or not _inside_bay_area(latitude, longitude):
        return None, None, None
    name = _text(event.get("location_name"))
    address = _text(event.get("address")) or (_text(geo.get("street")) if geo is not None else None)
    venue_name = (
        f"{name} — {address}" if name is not None and address is not None else name or address
    )
    return (
        venue_name,
        _text(geo.get("city")) if geo is not None else None,
        GeoPoint(latitude, longitude),
    )


def _inside_bay_area(latitude: float, longitude: float) -> bool:
    min_latitude, min_longitude, max_latitude, max_longitude = _BAY_AREA_BOUNDS
    return min_latitude <= latitude <= max_latitude and min_longitude <= longitude <= max_longitude


def _handoff_url(value: object, publisher: _LocalistPublisher) -> str | None:
    """Keep only the publisher's event-detail URL as a handoff, never a fetch target (FR-3.1)."""
    if not isinstance(value, str) or any(
        ord(character) < _CONTROL_CHARACTER_LIMIT for character in value
    ):
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
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith(_EVENT_PATH_PREFIX)
        or parsed.path == _EVENT_PATH_PREFIX
    ):
        return None
    return urlunsplit(("https", publisher.handoff_host, parsed.path, parsed.query, ""))


def _price_status(event: dict[str, object]) -> PriceStatus:
    """Avoid treating Localist's false/blank free flag as a positive paid assertion (FR-3.7/FR-4.6)."""
    cost = _text(event.get("ticket_cost"))
    has_positive_cost = cost is not None and _POSITIVE_COST.search(cost) is not None
    if event.get("free") is True:
        return PriceStatus.UNKNOWN if has_positive_cost else PriceStatus.FREE
    return PriceStatus.PAID if has_positive_cost else PriceStatus.UNKNOWN


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _date_summary_is_valid(value: dict[str, object]) -> bool:
    first = _plain_string(value.get("first"))
    last = _plain_string(value.get("last"))
    if first is None or last is None:
        return False
    try:
        return date.fromisoformat(first) <= date.fromisoformat(last)
    except ValueError:
        return False


def _positive_id(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return value
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _coordinate(value: object, minimum: float, maximum: float) -> float | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        coordinate = float(value)
    except ValueError:
        return None
    if not math.isfinite(coordinate) or not minimum <= coordinate <= maximum:
        return None
    return coordinate


def _as_object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _plain_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_stanford_time(value: datetime) -> datetime:
    """Treat a test-supplied naive clock as Pacific local time (FR-3.7)."""
    return (
        value.astimezone(_STANFORD_TIME_ZONE)
        if value.tzinfo is not None
        else value.replace(tzinfo=_STANFORD_TIME_ZONE)
    )


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(HTMLParser(value).text(separator=" ", strip=True).split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


class LocalistFetchError(RuntimeError):
    """A whole-feed error that leaves the durable catalog refresh retryable (NFR-8)."""
