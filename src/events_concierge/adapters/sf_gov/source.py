"""Approved-origin SF.gov related-events adapter (FR-3.1/FR-10.3/FR-10.4).

SF.gov publishes anonymous, date-grouped civic events through its official API.  This adapter
retrieves only an owner-reviewed bounded page sequence and produces discovery-only handoff
candidates.  It never follows an event link, signs in, RSVPs, purchases, or mutates the source.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.catalog_window import collection_reference_time
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent
from ...infra.logging import get_logger

_log = get_logger("sf_gov.source")

_FETCH_TIMEOUT_S = 15.0
_MAX_REDIRECTS = 5
_SF_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")


class SfGovCatalogFetcher:
    """Fetch the reviewed SF.gov date-grouped event API at a human cadence (FR-10.3/10.4)."""

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
        self._now = now or (lambda: datetime.now(_SF_TIME_ZONE))
        self._clock = clock or monotonic
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._last_request_at: dict[str, float] = {}
        self._pacing_lock = asyncio.Lock()

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        """Return future civic candidates from no more than the reviewed page cap (FR-3.1)."""
        if source.mode is not CatalogSourceMode.SF_GOV_JSON:
            raise ValueError(f"unsupported SF.gov source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("SF.gov catalog sources must remain handoff-only")
        if not _is_supported_seed(source.seed_url):
            raise ValueError("SF.gov source must use the reviewed upcoming related-events endpoint")
        now = collection_reference_time(source, self._now())
        candidates = await self._fetch_pages(source)
        return [candidate for candidate in _deduplicate(candidates) if candidate.start_at >= now]

    async def _fetch_pages(self, source: CatalogSource) -> list[CandidateEvent]:
        candidates: list[CandidateEvent] = []
        expected_total: int | None = None
        observed_records = 0
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            for page in range(1, source.page_limit + 1):
                url = _with_page(source.seed_url, page)
                if not source.allows_url(url):
                    _log.warning("sf_gov_page_rejected", source_key=source.source_key, url=url)
                    break
                try:
                    response = await self._get_approved_response(client, source, url)
                except httpx.HTTPError as exc:
                    raise SfGovFetchError(
                        f"SF.gov page {page} failed for {source.source_key}: {exc}"
                    ) from exc
                if response is None:
                    raise SfGovFetchError(f"SF.gov page {page} left the approved origin allowlist")
                try:
                    payload = _as_object_dict(response.json())
                except ValueError as exc:
                    raise SfGovFetchError(
                        f"SF.gov page {page} returned invalid JSON for {source.source_key}"
                    ) from exc
                if payload is None:
                    raise SfGovFetchError(
                        f"SF.gov page {page} returned an invalid object for {source.source_key}"
                    )
                buckets = payload.get("events")
                if not isinstance(buckets, list):
                    raise SfGovFetchError(
                        f"SF.gov page {page} returned no date-grouped events for {source.source_key}"
                    )
                if expected_total is None:
                    expected_total = _nonnegative_int(payload.get("total"))

                record_count = 0
                for raw_event in _flatten_buckets(buckets):
                    record_count += 1
                    if _is_false(raw_event.get("live")) or _is_true(raw_event.get("cancelled")):
                        continue
                    if _is_remote_only(raw_event):
                        continue
                    candidate = _candidate_from_event(raw_event, source)
                    if candidate is not None:
                        candidates.append(candidate)
                observed_records += record_count
                if record_count == 0:
                    if expected_total is None or observed_records >= expected_total:
                        break
                    raise SfGovFetchError(
                        f"SF.gov page {page} ended before its reported total for {source.source_key}"
                    )
                if expected_total is not None and observed_records >= expected_total:
                    break
        if expected_total is not None and observed_records < expected_total:
            raise SfGovFetchError(
                f"SF.gov source {source.source_key} exceeds its reviewed {source.page_limit}-page cap"
            )
        return candidates

    async def _get_approved_response(
        self, client: httpx.AsyncClient, source: CatalogSource, url: str
    ) -> httpx.Response | None:
        """Follow only explicitly approved HTTPS redirects before accepting a source page (FR-10.3)."""
        current_url = url
        for _ in range(_MAX_REDIRECTS):
            if not source.allows_url(current_url):
                _log.warning(
                    "sf_gov_redirect_rejected", source_key=source.source_key, url=current_url
                )
                return None
            await self._wait_for_host_slot(current_url, source.min_interval_ms)
            response = await client.get(current_url, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location")
                next_url = str(response.url.join(location)) if location else ""
                if not location or not source.allows_url(next_url):
                    _log.warning(
                        "sf_gov_redirect_rejected", source_key=source.source_key, url=next_url
                    )
                    return None
                current_url = next_url
                continue
            if not source.allows_url(str(response.url)):
                _log.warning(
                    "sf_gov_final_url_rejected", source_key=source.source_key, url=str(response.url)
                )
                return None
            response.raise_for_status()
            return response
        _log.warning("sf_gov_redirect_limit", source_key=source.source_key, seed_url=url)
        return None

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host human-cadence floor before every public API request (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("SF.gov URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _with_page(seed_url: str, page: int) -> str:
    """Set a positive SF.gov API page while preserving the reviewed query/filter set (FR-10.3)."""
    if page <= 0:
        raise ValueError("SF.gov page must be positive")
    parsed = urlsplit(seed_url)
    parameters = [(key, value) for key, value in parse_qsl(parsed.query) if key != "page"]
    parameters.append(("page", str(page)))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(parameters), parsed.fragment)
    )


def _is_supported_seed(seed_url: str) -> bool:
    """Constrain this typed adapter to its reviewed public API shape (FR-10.3)."""
    parsed = urlsplit(seed_url)
    parameters = dict(parse_qsl(parsed.query))
    return (
        parsed.path == "/api/related-events/"
        and parameters.get("list") == "upcoming"
        and parameters.get("locale") == "en"
        and parameters.get("groupby") == "date"
    )


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Narrow untrusted source JSON to a string-keyed object (FR-3.7)."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _flatten_buckets(buckets: list[object]) -> list[dict[str, object]]:
    """Flatten SF.gov's documented date-bucket response shape without accepting arbitrary nesting."""
    events: list[dict[str, object]] = []
    for raw_bucket in buckets:
        bucket = _as_object_dict(raw_bucket)
        values = bucket.get("events") if bucket is not None else None
        if not isinstance(values, list):
            continue
        for raw_event in values:
            event = _as_object_dict(raw_event)
            if event is not None:
                events.append(event)
    return events


def _candidate_from_event(event: dict[str, object], source: CatalogSource) -> CandidateEvent | None:
    """Normalize one official civic record, failing closed on identity, time, or handoff URL (FR-3.7)."""
    title = _text(event.get("title"))
    start_at = _parse_iso(event.get("start_datetime"))
    registration_url = _handoff_url(event)
    if title is None or start_at is None or registration_url is None:
        _log.warning("sf_gov_event_incomplete", source_key=source.source_key)
        return None
    end_at = _end_at(event)
    if end_at is not None and end_at <= start_at:
        end_at = None
    venue_name, city = _physical_location(event)
    description = (
        _first_text(
            event.get("description"), event.get("overview"), _meta_text(event, "search_description")
        )
        or ""
    )
    external_id = _event_identifier(event.get("id"), source, title, start_at, registration_url)
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"sf-gov:{source.source_key}:{external_id}:{start_at.isoformat()}",
        title=title,
        start_at=start_at,
        registration_url=registration_url,
        end_at=end_at,
        venue_name=venue_name,
        city=city,
        description=description,
        price_status=PriceStatus.UNKNOWN,
        raw=dict(event),
    )


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_SF_TIME_ZONE)


def _handoff_url(event: dict[str, object]) -> str | None:
    """Build a user-facing HTTPS handoff URL from the publisher's relative public path (FR-3.1)."""
    meta = _as_object_dict(event.get("meta"))
    raw_path = meta.get("url_path") if meta is not None else None
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        return None
    path = raw_path.removeprefix("/home")
    return urlunsplit(("https", "www.sf.gov", path or "/", "", ""))


def _meta_text(event: dict[str, object], key: str) -> object:
    meta = _as_object_dict(event.get("meta"))
    return meta.get(key) if meta is not None else None


def _end_at(event: dict[str, object]) -> datetime | None:
    """Reject SF.gov's synthetic end-of-day value when no end was actually supplied (FR-3.7)."""
    date_time = event.get("date_time")
    if isinstance(date_time, list):
        for raw_entry in date_time:
            entry = _as_object_dict(raw_entry)
            value = _as_object_dict(entry.get("value")) if entry is not None else None
            if value is None:
                continue
            include_end = _text(value.get("include_end_date_time"))
            if include_end is not None and include_end.casefold() == "no":
                return None
            break
    return _parse_iso(event.get("end_datetime"))


def _event_identifier(
    value: object, source: CatalogSource, title: str, start_at: datetime, registration_url: str
) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    material = "|".join((source.source_key, title, start_at.isoformat(), registration_url))
    return f"derived:{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _is_remote_only(event: dict[str, object]) -> bool:
    """Exclude a remote meeting only when SF.gov publishes no physical address alongside it (FR-3.1)."""
    addresses, has_online = _location_records(event)
    return has_online and not addresses


def _physical_location(event: dict[str, object]) -> tuple[str | None, str | None]:
    addresses, _ = _location_records(event)
    if not addresses:
        return None, None
    address = addresses[0]
    return (
        _first_text(
            address.get("address_title"),
            address.get("location_name"),
            address.get("addressee"),
            address.get("line1"),
        ),
        _text(address.get("city")),
    )


def _location_records(event: dict[str, object]) -> tuple[list[dict[str, object]], bool]:
    """Return published physical addresses plus the presence of an online-only location marker."""
    addresses: list[dict[str, object]] = []
    has_online = False
    for field in ("location", "meeting_location"):
        raw_locations = event.get(field)
        if not isinstance(raw_locations, list):
            continue
        for raw_location in raw_locations:
            location = _as_object_dict(raw_location)
            location_type = _text(location.get("type")) if location is not None else None
            value = _as_object_dict(location.get("value")) if location is not None else None
            if location_type == "address" and value is not None:
                addresses.append(value)
            elif location_type == "online":
                has_online = True
    return addresses, has_online


def _is_true(value: object) -> bool:
    return (
        value is True
        or (isinstance(value, int) and not isinstance(value, bool) and value == 1)
        or (isinstance(value, str) and value.strip().lower() in {"true", "1"})
    )


def _is_false(value: object) -> bool:
    return (
        value is False
        or (isinstance(value, int) and not isinstance(value, bool) and value == 0)
        or (isinstance(value, str) and value.strip().lower() in {"false", "0"})
    )


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(HTMLParser(value).text(separator=" ", strip=True).split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


def _first_text(*values: object) -> str | None:
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return None


def _deduplicate(candidates: list[CandidateEvent]) -> list[CandidateEvent]:
    """Keep one exact source occurrence if a date page repeats it (FR-3.8)."""
    unique: dict[str, CandidateEvent] = {}
    for candidate in candidates:
        unique.setdefault(candidate.source_event_id, candidate)
    return list(unique.values())


class SfGovFetchError(RuntimeError):
    """A whole-page source failure that must leave the durable refresh retryable (NFR-8)."""
