"""Approved-origin Berkeley Public Library Communico catalog adapter (FR-3.1/FR-3.7/FR-10.3/10.4).

Berkeley Public Library publishes its official public calendar through an anonymous, date-bounded
Communico list endpoint. This adapter accepts only that closed source in a fixed local 90-day
window, emits physical/hybrid discovery handoffs, and never follows an event link, signs in,
registers, purchases, or mutates the publisher.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent
from ...infra.logging import get_logger

_log = get_logger("communico.source")

_FETCH_TIMEOUT_S = 15.0
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_ITEMS = 600
_HORIZON_DAYS = 90
_REQUEST_DAYS = _HORIZON_DAYS + 1
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_CANCELLED = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
_CONTROL_CHARACTER_LIMIT = 32


@dataclass(frozen=True, slots=True)
class _CommunicoPublisher:
    """One closed list/handoff pair; registry data cannot open a new Communico client (FR-10.3)."""

    source_key: str
    api_host: str
    api_path: str
    handoff_host: str


_COMMUNICO_PUBLISHERS = {
    "berkeley-public-library-events": _CommunicoPublisher(
        source_key="berkeley-public-library-events",
        api_host="berkeleypubliclibrary.libnet.info",
        api_path="/eeventcaldata",
        handoff_host="berkeleypubliclibrary.libnet.info",
    ),
}


class CommunicoCatalogFetcher:
    """Fetch a closed official library calendar at a human cadence (FR-10.3/FR-10.4)."""

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
        """Return future physical/hybrid library occurrences in the reviewed local horizon (FR-3.1)."""
        if source.mode is not CatalogSourceMode.COMMUNICO_JSON:
            raise ValueError(f"unsupported Communico source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("Communico catalog sources must remain handoff-only")
        publisher = _publisher_for_source(source)
        if publisher is None:
            raise ValueError("Communico source must use a reviewed public events endpoint")

        now = _as_local_time(self._now())
        horizon_start = datetime.combine(now.date(), datetime.min.time(), _LOCAL_TIME_ZONE)
        horizon_end = horizon_start + timedelta(days=_HORIZON_DAYS)
        response = await self._response_or_error(source, publisher, horizon_start)
        events = _events_from_response(response, source.source_key)
        candidates: list[CandidateEvent] = []
        for event in events:
            candidate = _candidate_from_event(
                event, source, publisher, now, horizon_start, horizon_end
            )
            if candidate is not None:
                candidates.append(candidate)
        return _deduplicate(candidates)

    async def _response_or_error(
        self, source: CatalogSource, publisher: _CommunicoPublisher, horizon_start: datetime
    ) -> httpx.Response:
        """Request only the exact public list endpoint; redirects are source failures (FR-10.3)."""
        url = _request_url(source.seed_url, horizon_start)
        if not _is_approved_endpoint_url(source, publisher, url):
            raise CommunicoFetchError("Communico request left the approved endpoint")
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
                raise CommunicoFetchError(
                    f"Communico source {source.source_key} request failed: {exc}"
                ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(
            source, publisher, str(response.url)
        ):
            raise CommunicoFetchError("Communico request left the approved endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CommunicoFetchError(
                f"Communico source {source.source_key} request failed: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise CommunicoFetchError(
                "Communico response exceeded its reviewed response-size limit"
            )
        return response

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host floor before the anonymous source-list GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("Communico URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _publisher_for_source(source: CatalogSource) -> _CommunicoPublisher | None:
    """Return the sole reviewed publisher only when its key and seed agree exactly (FR-10.3)."""
    publisher = _COMMUNICO_PUBLISHERS.get(source.source_key)
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


def _is_approved_endpoint_url(
    source: CatalogSource, publisher: _CommunicoPublisher, url: str
) -> bool:
    """Require registry approval plus the exact reviewed list path (FR-10.3)."""
    parsed = urlsplit(url)
    return (
        source.allows_url(url)
        and parsed.netloc.casefold() == publisher.api_host
        and parsed.path == publisher.api_path
    )


def _request_url(seed_url: str, horizon_start: datetime) -> str:
    """Construct the fixed local request without accepting a source-returned URL (FR-10.3)."""
    request = json.dumps(
        {"private": False, "date": horizon_start.date().isoformat(), "days": _REQUEST_DAYS},
        separators=(",", ":"),
    )
    parsed = urlsplit(seed_url)
    parameters = (("event_type", "0"), ("req", request))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(parameters), ""))


def _events_from_response(response: httpx.Response, source_key: str) -> list[dict[str, object]]:
    """Validate the bounded JSON array before any event can enter the catalog (NFR-8)."""
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise CommunicoFetchError(
            f"Communico response returned invalid JSON for {source_key}"
        ) from exc
    if not isinstance(payload, list) or len(payload) >= _MAX_ITEMS:
        raise CommunicoFetchError(
            f"Communico response returned an invalid or capped array for {source_key}"
        )
    events: list[dict[str, object]] = []
    for raw_event in payload:
        event = _as_object_dict(raw_event)
        if event is None:
            raise CommunicoFetchError(
                f"Communico response contained a non-object event for {source_key}"
            )
        events.append(event)
    return events


def _candidate_from_event(
    event: dict[str, object],
    source: CatalogSource,
    publisher: _CommunicoPublisher,
    now: datetime,
    horizon_start: datetime,
    horizon_end: datetime,
) -> CandidateEvent | None:
    """Normalize one public physical occurrence, failing closed on identity, time, or venue (FR-3.7)."""
    title = _text(event.get("title"))
    event_id = _event_id(event.get("id"))
    start_at = _parse_local_datetime(event.get("raw_start_time"))
    if (
        title is None
        or event_id is None
        or start_at is None
        or _is_cancelled(event, title)
        or _is_true(event.get("private_event"))
        or not _is_physical_or_hybrid(event.get("event_type"))
    ):
        _log.warning("communico_event_incomplete", source_key=source.source_key)
        return None
    if start_at < now or not horizon_start <= start_at < horizon_end:
        return None
    venue_name = _venue_name(event)
    if venue_name is None:
        _log.warning("communico_event_without_physical_venue", source_key=source.source_key)
        return None
    end_at = _parse_local_datetime(event.get("raw_end_time"))
    if end_at is not None and end_at <= start_at:
        end_at = None
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"communico:{source.source_key}:{event_id}:{start_at.isoformat()}",
        title=title,
        start_at=start_at,
        registration_url=_handoff_url(event_id, publisher),
        end_at=end_at,
        venue_name=venue_name,
        description=_description(event),
        price_status=PriceStatus.UNKNOWN,
        raw={
            "id": event_id,
            "event_type": event.get("event_type"),
            "changed": event.get("changed"),
        },
    )


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Narrow untrusted source JSON to a string-keyed object (FR-3.7)."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _event_id(value: object) -> str | None:
    """Keep only the positive publisher event id used by the public handoff path (FR-3.8)."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return value
    return None


def _parse_local_datetime(value: object) -> datetime | None:
    """Parse only a publisher-provided naive local occurrence timestamp (FR-3.7)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=_LOCAL_TIME_ZONE) if parsed.tzinfo is None else None


def _is_cancelled(event: dict[str, object], title: str) -> bool:
    """Exclude only explicit cancellation text carried by the public occurrence (FR-3.7)."""
    reason = _text(event.get("changed_reason"))
    return _CANCELLED.search(title) is not None or (
        reason is not None and _CANCELLED.search(reason) is not None
    )


def _is_true(value: object) -> bool:
    return (
        value is True
        or (isinstance(value, int) and not isinstance(value, bool) and value == 1)
        or (isinstance(value, str) and value.strip().casefold() in {"true", "1"})
    )


def _is_physical_or_hybrid(value: object) -> bool:
    """Keep only publisher-labelled physical/hybrid events (FR-3.1)."""
    return isinstance(value, str) and value.strip().casefold() in {"inperson", "hybrid"}


def _venue_name(event: dict[str, object]) -> str | None:
    """Use only the publisher's physical library/room text and never infer a city (FR-3.7)."""
    library = _text(event.get("library")) or _text(event.get("location"))
    room = _text(event.get("venues")) or _text(event.get("venue_name"))
    if library is None:
        return None
    return f"{library} — {room}" if room is not None and room != library else library


def _handoff_url(event_id: str, publisher: _CommunicoPublisher) -> str:
    """Build the matching official event detail handoff without requesting it (FR-3.1)."""
    return urlunsplit(("https", publisher.handoff_host, f"/event/{event_id}", "", ""))


def _description(event: dict[str, object]) -> str:
    """Retain bounded source description text while avoiding oversized rich bodies (FR-3.7/NFR-8)."""
    for field in ("description", "long_description"):
        description = _text(event.get(field))
        if description is not None:
            return description[:4_000]
    return ""


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if any(ord(character) < _CONTROL_CHARACTER_LIMIT for character in value):
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


def _deduplicate(candidates: list[CandidateEvent]) -> list[CandidateEvent]:
    """Collapse an exact repeated list row without losing distinct recurring occurrences (FR-3.8)."""
    unique: dict[str, CandidateEvent] = {}
    for candidate in candidates:
        unique.setdefault(candidate.source_event_id, candidate)
    return list(unique.values())


class CommunicoFetchError(RuntimeError):
    """A whole-feed failure that leaves the durable catalog refresh retryable (NFR-8)."""
