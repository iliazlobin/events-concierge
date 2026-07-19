"""Approved-origin Bay Area civic Legistar catalog adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

The reviewed San José, Sunnyvale, Alameda, and Oakland City Clerk calendars publish anonymous
Granicus Legistar meeting lists. This adapter accepts only their closed endpoint set through a
fixed local 90-day window, emits physical meeting candidates for human handoff, and never follows
a meeting link, signs in, RSVPs, purchases, or mutates the source.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from math import isfinite
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource, CatalogSourcePage
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent
from ...domain.policy import SourceQuarantineSignal
from ...infra.logging import get_logger
from ...ports.sources import SourceAccessDeniedError, SourceRateLimitedError

_log = get_logger("legistar.source")

_FETCH_TIMEOUT_S = 15.0
_HANDOFF_PATH = "/MeetingDetail.aspx"
_PAGE_SIZE = 100
_HORIZON_DAYS = 90
_CONTROL_CHARACTER_LIMIT = 32
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_CANCELLED = re.compile(r"\bcancell?ed\b|\bcancellation\b", re.IGNORECASE)
_CLOSED_SESSION = re.compile(r"\bclosed\s+session\b", re.IGNORECASE)
_VIRTUAL_ONLY = re.compile(
    r"^\s*(?:virtual|online|remote|tele\s*[-\u2013\u2014]?\s*conference)\s*"
    r"(?:meeting|hearing)?\s*(?:[-\u2013\u2014:/]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _LegistarPublisher:
    """One closed civic API / handoff pair; registry data cannot open another client (FR-10.3)."""

    source_key: str
    api_path: str
    handoff_host: str
    source_id_prefix: str
    excludes_hidden: bool


_LEGISTAR_PUBLISHERS = {
    CatalogSourceMode.SAN_JOSE_LEGISTAR: _LegistarPublisher(
        source_key="san-jose-legistar-meetings",
        api_path="/v1/SanJose/Events",
        handoff_host="sanjose.legistar.com",
        source_id_prefix="san-jose-legistar",
        excludes_hidden=False,
    ),
    CatalogSourceMode.SUNNYVALE_LEGISTAR: _LegistarPublisher(
        source_key="sunnyvale-legistar-meetings",
        api_path="/v1/SunnyvaleCA/Events",
        handoff_host="sunnyvaleca.legistar.com",
        source_id_prefix="sunnyvale-legistar",
        excludes_hidden=True,
    ),
    CatalogSourceMode.ALAMEDA_LEGISTAR: _LegistarPublisher(
        source_key="alameda-legistar-meetings",
        api_path="/v1/Alameda/Events",
        handoff_host="alameda.legistar.com",
        source_id_prefix="alameda-legistar",
        excludes_hidden=True,
    ),
    CatalogSourceMode.OAKLAND_LEGISTAR: _LegistarPublisher(
        source_key="oakland-legistar-meetings",
        api_path="/v1/Oakland/Events",
        handoff_host="oakland.legistar.com",
        source_id_prefix="oakland-legistar",
        excludes_hidden=True,
    ),
}


class LegistarCatalogFetcher:
    """Fetch a closed official civic-meeting feed at a human cadence (FR-10.3/10.4)."""

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
        """Return future physical city-meeting candidates inside the reviewed 90-day window (FR-3.1)."""
        if not source.handoff_only:
            raise ValueError("Legistar catalog sources must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("Legistar source must use a reviewed public Events endpoint")

        now = _as_local_time(self._now())
        candidates: list[CandidateEvent] = []
        seen_event_ids: set[str] = set()
        for page_number in range(source.page_limit):
            page = await self._fetch_page(
                source,
                publisher,
                now,
                page_number,
                apply_local_pacing=True,
            )
            for event_id in page.source_event_ids:
                if event_id in seen_event_ids:
                    raise LegistarFetchError(
                        f"Legistar page sequence repeated an event id for {source.source_key}"
                    )
                seen_event_ids.add(event_id)
            candidates.extend(page.candidates)
            if page.raw_count < _PAGE_SIZE:
                return _deduplicate(candidates)
        raise LegistarFetchError(
            f"Legistar source {source.source_key} exceeds its reviewed {source.page_limit}-page cap"
        )

    async def fetch_page(
        self,
        source: CatalogSource,
        *,
        window_start_day: date,
        page_number: int,
    ) -> CatalogSourcePage:
        """Fetch one P15b page after its shared Pacer lease, without a process-local sleep.

        The durable service owns the once-per-origin Redis admission and the frozen local day.
        This narrow method deliberately issues one exact GET; it never follows a page, sleeps, or
        stores the remote JSON payload (FR-10.3/10.4, NFR-8, ADR-003/005).
        """
        if not source.handoff_only:
            raise ValueError("Legistar catalog sources must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("Legistar source must use a reviewed public Events endpoint")
        if page_number < 0 or page_number >= source.page_limit:
            raise ValueError("Legistar page number is outside the reviewed source page cap")
        now = _as_local_time(self._now())
        return await self._fetch_page(
            source,
            publisher,
            now,
            page_number,
            window_start_day=window_start_day,
            apply_local_pacing=False,
        )

    async def _fetch_page(
        self,
        source: CatalogSource,
        publisher: _LegistarPublisher,
        now: datetime,
        page_number: int,
        *,
        window_start_day: date | None = None,
        apply_local_pacing: bool,
    ) -> CatalogSourcePage:
        """Read/normalize exactly one fixed offset page for legacy or P15b callers (NFR-8)."""
        start_day = window_start_day or now.date()
        page = page_number + 1
        offset = page_number * _PAGE_SIZE
        url = _request_url(source.seed_url, start_day, offset)
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            response = await self._response_or_error(
                client,
                source,
                publisher,
                url,
                page,
                apply_local_pacing=apply_local_pacing,
            )
        records = _records_from_response(response, source.source_key, page)
        source_event_ids: list[str] = []
        for record in records:
            event_id = _event_id(record.get("EventId"))
            if event_id is not None:
                if event_id in source_event_ids:
                    raise LegistarFetchError(
                        f"Legistar page {page} repeated an event id for {source.source_key}"
                    )
                source_event_ids.append(event_id)
        candidates = tuple(
            candidate
            for record in records
            if (candidate := _candidate_from_event(record, source, publisher, now)) is not None
        )
        return CatalogSourcePage(
            page_number=page_number,
            raw_count=len(records),
            candidates=candidates,
            source_event_ids=tuple(source_event_ids),
        )

    async def _fetch_pages(
        self, source: CatalogSource, publisher: _LegistarPublisher, start_day: date
    ) -> list[dict[str, object]]:
        """Compatibility helper retained for direct adapter callers while P15b stages one page."""
        records: list[dict[str, object]] = []
        seen_event_ids: set[str] = set()
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            for page in range(source.page_limit):
                offset = page * _PAGE_SIZE
                url = _request_url(source.seed_url, start_day, offset)
                response = await self._response_or_error(client, source, publisher, url, page + 1)
                rows = _records_from_response(response, source.source_key, page + 1)
                for row in rows:
                    event_id = _event_id(row.get("EventId"))
                    if event_id is not None:
                        if event_id in seen_event_ids:
                            raise LegistarFetchError(
                                "Legistar page sequence repeated an event id for "
                                f"{source.source_key}"
                            )
                        seen_event_ids.add(event_id)
                records.extend(rows)
                if len(rows) < _PAGE_SIZE:
                    return records
        raise LegistarFetchError(
            f"Legistar source {source.source_key} exceeds its reviewed {source.page_limit}-page cap"
        )

    async def _response_or_error(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        publisher: _LegistarPublisher,
        url: str,
        page: int,
        *,
        apply_local_pacing: bool = True,
    ) -> httpx.Response:
        """Request only the exact public list endpoint; redirects are source failures (FR-10.3)."""
        if not _is_approved_endpoint_url(source, publisher, url):
            raise LegistarFetchError(f"Legistar page {page} left the approved endpoint")
        try:
            if apply_local_pacing:
                await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise LegistarFetchError(
                f"Legistar page {page} failed for {source.source_key}: {exc}"
            ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url)
        ):
            raise LegistarFetchError(f"Legistar page {page} left the approved endpoint")
        if response.status_code == httpx.codes.FORBIDDEN:
            raise SourceAccessDeniedError(SourceQuarantineSignal.FORBIDDEN)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            retry_after_seconds = _retry_after_seconds(
                response.headers.get("Retry-After"), _as_utc(self._now())
            )
            if retry_after_seconds is not None:
                raise SourceRateLimitedError(
                    "Legistar source returned HTTP 429",
                    retry_after_seconds=retry_after_seconds,
                )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LegistarFetchError(
                f"Legistar page {page} failed for {source.source_key}: {exc}"
            ) from exc
        return response

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host floor before every anonymous API GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("Legistar URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _LegistarPublisher | None:
    """Return a civic publisher only when the source key, mode, and endpoint agree exactly (FR-10.3)."""
    publisher = _LEGISTAR_PUBLISHERS.get(source.mode)
    if publisher is None or source.source_key != publisher.source_key:
        return None
    parsed = urlsplit(source.seed_url)
    return (
        publisher
        if (
            parsed.scheme.casefold() == "https"
            and parsed.netloc.casefold() == "webapi.legistar.com"
            and parsed.path == publisher.api_path
            and not parsed.query
            and not parsed.fragment
        )
        else None
    )


def _is_approved_endpoint_url(
    source: CatalogSource, publisher: _LegistarPublisher, url: str
) -> bool:
    """Require both registry approval and the exact fixed Granicus resource path (FR-10.3)."""
    parsed = urlsplit(url)
    return (
        source.allows_url(url)
        and parsed.netloc.casefold() == "webapi.legistar.com"
        and parsed.path == publisher.api_path
    )


def _request_url(seed_url: str, start_day: date, offset: int) -> str:
    """Construct the fixed, locally bounded page query without accepting user filter text (FR-10.3)."""
    if offset < 0 or offset % _PAGE_SIZE != 0:
        raise ValueError("Legistar offset must be a non-negative page boundary")
    parsed = urlsplit(seed_url)
    parameters = (
        ("$filter", _filter_clause(start_day)),
        ("$orderby", "EventDate asc,EventId asc"),
        ("$top", str(_PAGE_SIZE)),
        ("$skip", str(offset)),
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(parameters), ""))


def _filter_clause(start_day: date) -> str:
    """Return the owner-reviewed local 90-day date predicate (FR-3.1)."""
    end_exclusive = start_day + timedelta(days=_HORIZON_DAYS)
    return (
        f"EventDate ge datetime'{start_day.isoformat()}T00:00:00' and "
        f"EventDate lt datetime'{end_exclusive.isoformat()}T00:00:00'"
    )


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """Parse only positive finite Retry-After seconds or an HTTP date (FR-10.4)."""
    if value is None:
        return None
    text = value.strip()
    if text.isdecimal():
        seconds = float(text)
        return seconds if isfinite(seconds) and seconds >= 0.0 else None
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    seconds = (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
    return max(seconds, 0.0) if isfinite(seconds) else None


def _as_utc(value: datetime) -> datetime:
    """Normalize a source/test clock for HTTP-date throttle calculations (FR-10.4)."""
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _records_from_response(
    response: httpx.Response, source_key: str, page: int
) -> list[dict[str, object]]:
    """Narrow one JSON array page and reject schema drift before a partial ingest (NFR-8)."""
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise LegistarFetchError(
            f"Legistar page {page} returned invalid JSON for {source_key}"
        ) from exc
    if not isinstance(payload, list) or len(payload) > _PAGE_SIZE:
        raise LegistarFetchError(
            f"Legistar page {page} returned an invalid page shape for {source_key}"
        )
    records: list[dict[str, object]] = []
    for raw_record in payload:
        record = _as_object_dict(raw_record)
        if record is None:
            raise LegistarFetchError(
                f"Legistar page {page} contained a non-object event for {source_key}"
            )
        records.append(record)
    return records


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Narrow untrusted source JSON to a string-keyed object (FR-3.7)."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _candidate_from_event(
    event: dict[str, object],
    source: CatalogSource,
    publisher: _LegistarPublisher,
    now: datetime,
) -> CandidateEvent | None:
    """Normalize one civic meeting, failing closed on identity, time, location, or handoff (FR-3.7)."""
    title = _text(event.get("EventBodyName"))
    location = _text(event.get("EventLocation"))
    comment = _text(event.get("EventComment"))
    agenda_status = _text(event.get("EventAgendaStatusName"))
    if (
        _is_cancelled(title, location, comment)
        or _is_closed_session(title, comment)
        or _is_hidden(agenda_status, publisher)
        or location is None
        or _is_virtual_only(location)
    ):
        return None
    event_id = _event_id(event.get("EventId"))
    start_at = _parse_local_start(event.get("EventDate"), event.get("EventTime"))
    registration_url = _handoff_url(event.get("EventInSiteURL"), event_id, publisher)
    if title is None or event_id is None or start_at is None or registration_url is None:
        _log.warning("legistar_event_incomplete", source_key=source.source_key)
        return None
    if start_at < now:
        return None
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=(
            f"{publisher.source_id_prefix}:{source.source_key}:{event_id}:{start_at.isoformat()}"
        ),
        title=title,
        start_at=start_at,
        registration_url=registration_url,
        venue_name=location,
        description=comment or "",
        price_status=PriceStatus.UNKNOWN,
        raw=dict(event),
    )


def _event_id(value: object) -> str | None:
    """Keep only a positive publisher event id as the stable source identity (FR-3.8)."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return value
    return None


def _parse_local_start(date_value: object, time_value: object) -> datetime | None:
    """Combine the publisher's naive local date/time fields without guessing an end time (FR-3.7)."""
    if not isinstance(date_value, str) or not isinstance(time_value, str):
        return None
    source_day = _parse_source_day(date_value)
    if source_day is None:
        return None
    time_text = time_value.strip().upper()
    for format_string in ("%I:%M %p", "%I %p"):
        try:
            source_time = datetime.strptime(time_text, format_string).time()
        except ValueError:
            continue
        return datetime.combine(source_day, source_time, _LOCAL_TIME_ZONE)
    return None


def _parse_source_day(value: str) -> date | None:
    """Accept only a date or a timezone-less midnight datetime from the declared API field."""
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None or parsed.time() != time.min:
        return None
    return parsed.date()


def _handoff_url(value: object, event_id: str | None, publisher: _LegistarPublisher) -> str | None:
    """Keep only the matching official event-detail URL as a user handoff, never a fetch target (FR-3.1)."""
    if event_id is None or not isinstance(value, str):
        return None
    if any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
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
        or parsed.path.casefold() != _HANDOFF_PATH.casefold()
        or parsed.fragment
    ):
        return None
    legids = [
        item for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key == "LEGID"
    ]
    if legids != [event_id]:
        return None
    return urlunsplit(("https", publisher.handoff_host, _HANDOFF_PATH, parsed.query, ""))


def _is_cancelled(*values: str | None) -> bool:
    """Exclude only explicit publisher cancellation text, never agenda/minutes state (FR-3.7)."""
    return any(value is not None and _CANCELLED.search(value) is not None for value in values)


def _is_closed_session(title: str | None, comment: str | None) -> bool:
    """Exclude the publisher's explicit non-attendable closed-session label (FR-3.1)."""
    return any(
        value is not None and _CLOSED_SESSION.search(value) is not None
        for value in (title, comment)
    )


def _is_hidden(agenda_status: str | None, publisher: _LegistarPublisher) -> bool:
    """Honor a publisher's explicit non-public status without widening another profile (FR-3.1)."""
    return (
        publisher.excludes_hidden
        and agenda_status is not None
        and agenda_status.casefold() == "hidden"
    )


def _is_virtual_only(location: str) -> bool:
    """Exclude explicit virtual-only location text while retaining a physical/hybrid venue (FR-3.1)."""
    return _VIRTUAL_ONLY.search(location) is not None


def _as_local_time(value: datetime) -> datetime:
    """Treat a test-supplied naive clock as local, preserving source-local calendar dates (FR-3.7)."""
    return (
        value.astimezone(_LOCAL_TIME_ZONE)
        if value.tzinfo is not None
        else value.replace(tzinfo=_LOCAL_TIME_ZONE)
    )


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(HTMLParser(value).text(separator=" ", strip=True).split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


def _deduplicate(candidates: list[CandidateEvent]) -> list[CandidateEvent]:
    """Keep one exact publisher occurrence if an API page repeats it (FR-3.8)."""
    unique: dict[str, CandidateEvent] = {}
    for candidate in candidates:
        unique.setdefault(candidate.source_event_id, candidate)
    return list(unique.values())


class LegistarFetchError(RuntimeError):
    """A whole-feed failure that leaves the durable catalog refresh retryable (NFR-8)."""


# Stable aliases keep the prior San José adapter import usable while both reviewed civic publishers share it.
SanJoseLegistarCatalogFetcher = LegistarCatalogFetcher
SanJoseLegistarFetchError = LegistarFetchError
