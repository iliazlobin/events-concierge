"""Approved-origin The Events Calendar list adapters (FR-3.1/FR-3.7/FR-10.3/10.4).

Reviewed Bay Area publishers expose official calendars through anonymous The Events Calendar list
APIs. This adapter accepts only its closed publisher map in a fixed local 90-day window, emits
physical discovery handoffs, and never follows returned REST/detail, ticket, website, or
registration URLs; signs in; purchases; or mutates the source.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent
from ...infra.logging import get_logger

_log = get_logger("tribe.source")

_FETCH_TIMEOUT_S = 15.0
_MAX_RESPONSE_BYTES = 5_000_000
_PAGE_SIZE = 50
_HORIZON_DAYS = 90
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_CONTROL_CHARACTER_LIMIT = 32
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_POSITIVE_COST = re.compile(r"^\s*(?:[$€£]\s*)?[1-9]\d*(?:\.\d{1,2})?\s*$")
_POSITIVE_COST_VALUE = re.compile(r"^\s*[1-9]\d*(?:\.\d{1,2})?\s*$")


@dataclass(frozen=True, slots=True)
class _TribePublisher:
    """One closed list/handoff pair; registry data cannot open another TEC publisher (FR-10.3)."""

    source_key: str
    api_host: str
    api_path: str
    handoff_host: str


_TRIBE_PUBLISHERS = {
    "omca-events": _TribePublisher(
        source_key="omca-events",
        api_host="museumca.org",
        api_path="/wp-json/tribe/events/v1/events",
        handoff_host="museumca.org",
    ),
    "gardens-golden-gate-park-events": _TribePublisher(
        source_key="gardens-golden-gate-park-events",
        api_host="gggp.org",
        api_path="/wp-json/tribe/events/v1/events",
        handoff_host="gggp.org",
    ),
}


@dataclass(frozen=True, slots=True)
class _TribePage:
    """One validated source page and the publisher-declared finite page sequence (NFR-8)."""

    events: list[dict[str, object]]
    total: int
    total_pages: int


class TribeEventsCatalogFetcher:
    """Fetch a closed official The Events Calendar list API at a human cadence (FR-10.3/10.4)."""

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
        self._now = now or (lambda: datetime.now(_LOCAL_TIME_ZONE))
        self._clock = clock or monotonic
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._last_request_at: dict[str, float] = {}
        self._pacing_lock = asyncio.Lock()

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        """Return future timed physical occurrences in the reviewed local horizon (FR-3.1)."""
        if source.mode is not CatalogSourceMode.TRIBE_EVENTS_JSON:
            raise ValueError(f"unsupported The Events Calendar source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("The Events Calendar sources must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError(
                "The Events Calendar source must use a reviewed public events endpoint"
            )

        now = _as_local_time(self._now())
        horizon_start = datetime.combine(now.date(), datetime.min.time(), _LOCAL_TIME_ZONE)
        horizon_end = horizon_start + timedelta(days=_HORIZON_DAYS)
        events = await self._fetch_pages(
            source, publisher, horizon_start.date(), horizon_end.date()
        )
        candidates: list[CandidateEvent] = []
        seen_source_ids: set[str] = set()
        for event in events:
            candidate = _candidate_from_event(
                event, source, publisher, now, horizon_start, horizon_end
            )
            if candidate is None:
                continue
            if candidate.source_event_id in seen_source_ids:
                raise TribeEventsFetchError(
                    f"The Events Calendar page sequence repeated an occurrence for {source.source_key}"
                )
            seen_source_ids.add(candidate.source_event_id)
            candidates.append(candidate)
        return candidates

    async def _fetch_pages(
        self, source: CatalogSource, publisher: _TribePublisher, start_day: date, end_day: date
    ) -> list[dict[str, object]]:
        """Read every publisher-declared page, failing rather than returning a partial calendar (NFR-8)."""
        events: list[dict[str, object]] = []
        expected_total: int | None = None
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
                if expected_total is None or expected_total_pages is None:
                    expected_total = page.total
                    expected_total_pages = page.total_pages
                    if expected_total_pages > source.page_limit:
                        raise TribeEventsFetchError(
                            f"The Events Calendar source {source.source_key} exceeds its reviewed "
                            f"{source.page_limit}-page cap"
                        )
                    if expected_total_pages == 0:
                        if expected_total != 0 or page.events:
                            raise TribeEventsFetchError(
                                f"The Events Calendar source {source.source_key} returned inconsistent empty metadata"
                            )
                        return []
                elif page.total != expected_total or page.total_pages != expected_total_pages:
                    raise TribeEventsFetchError(
                        f"The Events Calendar source {source.source_key} changed its totals mid-refresh"
                    )
                if not page.events:
                    raise TribeEventsFetchError(
                        f"The Events Calendar page {page_number} was unexpectedly empty for {source.source_key}"
                    )
                if page_number < expected_total_pages and len(page.events) != _PAGE_SIZE:
                    raise TribeEventsFetchError(
                        f"The Events Calendar page {page_number} was short before its declared final page"
                    )
                events.extend(page.events)
                if page_number == expected_total_pages:
                    if len(events) != expected_total:
                        raise TribeEventsFetchError(
                            f"The Events Calendar source {source.source_key} returned an inconsistent event total"
                        )
                    return events
        raise TribeEventsFetchError(
            f"The Events Calendar source {source.source_key} exceeded its reviewed page sequence"
        )

    async def _response_or_error(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        publisher: _TribePublisher,
        url: str,
        page: int,
    ) -> httpx.Response:
        """Request only the exact public list endpoint; redirects are source failures (FR-10.3)."""
        if not _is_approved_endpoint_url(source, publisher, url):
            raise TribeEventsFetchError(
                f"The Events Calendar page {page} left the approved endpoint"
            )
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise TribeEventsFetchError(
                f"The Events Calendar page {page} failed for {source.source_key}: {exc}"
            ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url)
        ):
            raise TribeEventsFetchError(
                f"The Events Calendar page {page} left the approved endpoint"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TribeEventsFetchError(
                f"The Events Calendar page {page} failed for {source.source_key}: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise TribeEventsFetchError(
                f"The Events Calendar page {page} exceeded its reviewed response-size limit"
            )
        return response

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host floor before every anonymous source-list GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("The Events Calendar URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _TribePublisher | None:
    """Return a publisher only when its source key and seed agree exactly (FR-10.3)."""
    publisher = _TRIBE_PUBLISHERS.get(source.source_key)
    if publisher is None:
        return None
    parsed = urlsplit(source.seed_url)
    return (
        publisher
        if (
            parsed.scheme.casefold() == "https"
            and parsed.netloc.casefold() == publisher.api_host
            and parsed.path == publisher.api_path
            and not parsed.query
            and not parsed.fragment
        )
        else None
    )


def _is_approved_endpoint_url(source: CatalogSource, publisher: _TribePublisher, url: str) -> bool:
    """Require registry approval and the exact reviewed list resource path (FR-10.3)."""
    parsed = urlsplit(url)
    return (
        source.allows_url(url)
        and parsed.netloc.casefold() == publisher.api_host
        and parsed.path == publisher.api_path
    )


def _request_url(seed_url: str, start_day: date, end_day: date, page: int) -> str:
    """Construct the fixed bounded query without trusting a publisher-returned next URL (FR-10.3)."""
    if page <= 0:
        raise ValueError("The Events Calendar page must be positive")
    parameters = (
        ("start_date", start_day.isoformat()),
        ("end_date", end_day.isoformat()),
        ("per_page", str(_PAGE_SIZE)),
        ("page", str(page)),
        ("status", "publish"),
    )
    parsed = urlsplit(seed_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(parameters), ""))


def _page_from_response(response: httpx.Response, source_key: str, page_number: int) -> _TribePage:
    """Validate one list envelope before a batch can enter the catalog (NFR-8)."""
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise TribeEventsFetchError(
            f"The Events Calendar page {page_number} returned invalid JSON for {source_key}"
        ) from exc
    root = _as_object_dict(payload)
    raw_events = root.get("events") if root is not None else None
    total = _nonnegative_int(root.get("total")) if root is not None else None
    total_pages = _nonnegative_int(root.get("total_pages")) if root is not None else None
    if (
        not isinstance(raw_events, list)
        or total is None
        or total_pages is None
        or len(raw_events) > _PAGE_SIZE
    ):
        raise TribeEventsFetchError(
            f"The Events Calendar page {page_number} returned an invalid object shape for {source_key}"
        )
    events: list[dict[str, object]] = []
    for raw_event in raw_events:
        event = _as_object_dict(raw_event)
        if event is None:
            raise TribeEventsFetchError(
                f"The Events Calendar page {page_number} contained a non-object event for {source_key}"
            )
        events.append(event)
    return _TribePage(events, total, total_pages)


def _candidate_from_event(
    event: dict[str, object],
    source: CatalogSource,
    publisher: _TribePublisher,
    now: datetime,
    horizon_start: datetime,
    horizon_end: datetime,
) -> CandidateEvent | None:
    """Normalize one timed physical listing, failing closed on source truth (FR-3.7)."""
    event_id = _positive_id(event.get("id"))
    title = _text(event.get("title"))
    start_at = _parse_utc_timestamp(event.get("utc_start_date"))
    end_at = _parse_utc_timestamp(event.get("utc_end_date"))
    local_start = _parse_local_timestamp(event.get("start_date"))
    local_end = _parse_local_timestamp(event.get("end_date"))
    venue_name = _venue_name(event)
    registration_url = _handoff_url(event.get("url"), publisher)
    if (
        event_id is None
        or title is None
        or start_at is None
        or end_at is None
        or local_start is None
        or local_end is None
        or venue_name is None
        or registration_url is None
        or not _is_eligible(event)
        or start_at != local_start.astimezone(UTC)
        or end_at != local_end.astimezone(UTC)
        or end_at <= start_at
    ):
        _log.warning("tribe_event_incomplete", source_key=source.source_key)
        return None
    if start_at < now or not horizon_start <= start_at < horizon_end:
        return None
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"tec:{source.source_key}:{event_id}:{start_at.isoformat()}",
        title=title,
        start_at=start_at,
        registration_url=registration_url,
        end_at=end_at,
        venue_name=venue_name,
        description=_description(event),
        price_status=_price_status(event),
        raw={
            "id": event_id,
            "status": event.get("status"),
            "all_day": event.get("all_day"),
            "is_virtual": event.get("is_virtual"),
            "cost": event.get("cost"),
        },
    )


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Narrow untrusted source JSON to a string-keyed object (FR-3.7)."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _positive_id(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return value
    return None


def _parse_utc_timestamp(value: object) -> datetime | None:
    """Parse the publisher's UTC occurrence field without guessing a time zone (FR-3.7)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_local_timestamp(value: object) -> datetime | None:
    """Parse the publisher's local occurrence field only when it is a naive local time (FR-3.7)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=_LOCAL_TIME_ZONE) if parsed.tzinfo is None else None


def _is_eligible(event: dict[str, object]) -> bool:
    """Keep only explicit public, timed, physical publisher listings (FR-3.1/FR-10.3)."""
    status = _plain_string(event.get("status"))
    timezone = _plain_string(event.get("timezone"))
    return (
        status is not None
        and status.casefold() == "publish"
        and timezone == "America/Los_Angeles"
        and event.get("hide_from_listings") is False
        and event.get("is_virtual") is False
        and event.get("all_day") is False
    )


def _venue_name(event: dict[str, object]) -> str | None:
    """Require the publisher's named physical venue without inventing spatial metadata (FR-3.7)."""
    venue = _as_object_dict(event.get("venue"))
    return _text(venue.get("venue")) if venue is not None else None


def _handoff_url(value: object, publisher: _TribePublisher) -> str | None:
    """Keep only the publisher's public event page as a human handoff, never a fetch target (FR-3.1)."""
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
        or not parsed.path.startswith("/event/")
        or parsed.path == "/event/"
        or parsed.query
        or parsed.fragment
    ):
        return None
    return urlunsplit(("https", publisher.handoff_host, parsed.path, "", ""))


def _price_status(event: dict[str, object]) -> PriceStatus:
    """Preserve only unambiguous public price truth; member/ambiguous offers remain unknown (FR-3.7/4.6)."""
    cost = _plain_string(event.get("cost"))
    if cost is not None and cost.casefold() == "free":
        return PriceStatus.FREE
    if cost is not None and _POSITIVE_COST.fullmatch(cost) is not None:
        return PriceStatus.PAID
    return PriceStatus.PAID if _has_positive_cost_value(event) else PriceStatus.UNKNOWN


def _has_positive_cost_value(event: dict[str, object]) -> bool:
    """Recognize a publisher's structured positive price range without inferring from prose (FR-3.7)."""
    cost_details = _as_object_dict(event.get("cost_details"))
    values = cost_details.get("values") if cost_details is not None else None
    return isinstance(values, list) and any(
        isinstance(value, str) and _POSITIVE_COST_VALUE.fullmatch(value) is not None
        for value in values
    )


def _description(event: dict[str, object]) -> str:
    """Use the source-list excerpt rather than oversized rich event bodies (FR-3.7/NFR-8)."""
    return (_text(event.get("excerpt")) or "")[:4_000]


def _plain_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(HTMLParser(value).text(separator=" ", strip=True).split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


def _as_local_time(value: datetime) -> datetime:
    """Treat a test-supplied naive clock as publisher-local time (FR-3.7)."""
    return (
        value.astimezone(_LOCAL_TIME_ZONE)
        if value.tzinfo is not None
        else value.replace(tzinfo=_LOCAL_TIME_ZONE)
    )


class TribeEventsFetchError(RuntimeError):
    """A whole-feed failure that leaves the durable catalog refresh retryable (NFR-8)."""
