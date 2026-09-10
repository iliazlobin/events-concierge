"""Approved-origin LiveWhale JSON catalog adapter (FR-3.1/FR-10.3/FR-10.4).

The adapter is deliberately discovery-only.  It follows a reviewed publisher's anonymous JSON
pagination at a human cadence, emits handoff-only ``PUBLIC_JSONLD`` candidates, and never signs in,
opens a browser, RSVPs, purchases, or fetches a registration URL.  Records explicitly marked
``Online only`` are excluded from this physical ``bay_area_9_county`` catalog; hybrid local events
remain eligible.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.catalog_window import collection_reference_time
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent, GeoPoint, aggregate_price_status
from ...infra.logging import get_logger

_log = get_logger("livewhale.source")

_FETCH_TIMEOUT_S = 15.0
_MAX_REDIRECTS = 5
_FREE_TEXT = re.compile(r"\b(?:free|complimentary|no\s+charge)\b", re.IGNORECASE)
_CURRENCY_AMOUNT = re.compile(
    r"(?:[$€£]\s*|\b(?:usd|dollars?)\s*)(\d+(?:\.\d+)?)"
    r"|(\d+(?:\.\d+)?)\s*(?:usd|dollars?)\b",
    re.IGNORECASE,
)
_EXACT_NUMBER = re.compile(r"^(\d+(?:\.\d+)?)$")


class LiveWhaleCatalogFetcher:
    """Fetch a bounded reviewed LiveWhale feed without an authenticated session (FR-10.3/10.4)."""

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
        """Return future, physical candidates from no more than the reviewed page cap (FR-3.1)."""
        if source.mode is not CatalogSourceMode.LIVEWHALE_JSON:
            raise ValueError(f"unsupported LiveWhale source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("LiveWhale catalog sources must remain handoff-only")

        candidates = await self._fetch_pages(source)
        now = collection_reference_time(source, self._now())
        return [candidate for candidate in _deduplicate(candidates) if candidate.start_at >= now]

    async def _fetch_pages(self, source: CatalogSource) -> list[CandidateEvent]:
        candidates: list[CandidateEvent] = []
        url = source.seed_url
        visited_urls: set[str] = set()
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            for _ in range(source.page_limit):
                if url in visited_urls:
                    _log.warning("livewhale_pagination_loop", source_key=source.source_key, url=url)
                    break
                if not source.allows_url(url):
                    _log.warning("livewhale_page_rejected", source_key=source.source_key, url=url)
                    break
                visited_urls.add(url)
                try:
                    response = await self._get_approved_response(client, source, url)
                except httpx.HTTPError as exc:
                    _log.warning(
                        "livewhale_fetch_failed",
                        source_key=source.source_key,
                        url=url,
                        error=str(exc),
                    )
                    break
                if response is None:
                    break
                try:
                    payload = _as_object_dict(response.json())
                except ValueError as exc:
                    _log.warning(
                        "livewhale_json_failed",
                        source_key=source.source_key,
                        url=str(response.url),
                        error=str(exc),
                    )
                    break
                if payload is None:
                    _log.warning(
                        "livewhale_payload_invalid",
                        source_key=source.source_key,
                        url=str(response.url),
                    )
                    break
                data = payload.get("data")
                if not isinstance(data, list):
                    _log.warning(
                        "livewhale_data_invalid",
                        source_key=source.source_key,
                        url=str(response.url),
                    )
                    break
                for raw_event in data:
                    event = _as_object_dict(raw_event)
                    if event is None or _as_bool(event.get("is_canceled")):
                        continue
                    # A publisher's remote/streamed record is not physical Bay Area inventory.
                    if _is_online_only(event):
                        continue
                    candidate = _candidate_from_event(event, source, str(response.url))
                    if candidate is not None:
                        candidates.append(candidate)

                next_url = _next_page_url(payload, response.url)
                if next_url is None:
                    break
                if not source.allows_url(next_url):
                    _log.warning(
                        "livewhale_next_rejected", source_key=source.source_key, url=next_url
                    )
                    break
                url = next_url
        return candidates

    async def _get_approved_response(
        self, client: httpx.AsyncClient, source: CatalogSource, url: str
    ) -> httpx.Response | None:
        """Follow only in-allowlist redirects; make no request after an origin escape (FR-10.3)."""
        current_url = url
        for _ in range(_MAX_REDIRECTS):
            if not source.allows_url(current_url):
                _log.warning(
                    "livewhale_redirect_rejected", source_key=source.source_key, url=current_url
                )
                return None
            await self._wait_for_host_slot(current_url, source.min_interval_ms)
            response = await client.get(current_url, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location")
                next_url = str(response.url.join(location)) if location else ""
                if not location or not source.allows_url(next_url):
                    _log.warning(
                        "livewhale_redirect_rejected", source_key=source.source_key, url=next_url
                    )
                    return None
                current_url = next_url
                continue
            if not source.allows_url(str(response.url)):
                _log.warning(
                    "livewhale_final_url_rejected",
                    source_key=source.source_key,
                    url=str(response.url),
                )
                return None
            response.raise_for_status()
            return response
        _log.warning("livewhale_redirect_limit", source_key=source.source_key, seed_url=url)
        return None

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host human-cadence floor before each source API request (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("LiveWhale URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _as_object_dict(value: object) -> dict[str, object] | None:
    """Narrow untrusted JSON to the dictionary shape used by the source ACL (FR-3.7)."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _candidate_from_event(
    event: dict[str, object], source: CatalogSource, base_url: str
) -> CandidateEvent | None:
    """Normalize one LiveWhale record, failing closed on identity, time, or handoff URL (FR-3.7)."""
    title = _text(event.get("title"))
    start_at = _parse_iso(event.get("date_iso"))
    registration_url = _registration_url(event.get("url"), base_url)
    if title is None or start_at is None or registration_url is None:
        _log.warning("livewhale_event_incomplete", source_key=source.source_key)
        return None

    end_at = _parse_iso(event.get("date2_iso"))
    venue_name, city, geo = _location(event)
    external_id = _event_identifier(event.get("id"), source, title, start_at, registration_url)
    price_status = _price_status(event.get("cost"))
    description = (
        _first_text(event.get("description_text"), event.get("description"), event.get("summary"))
        or ""
    )
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        # ``Source`` remains the discovery-only policy class.  Prefixing the publisher key is
        # necessary because event_source_links keys on (source, source_event_id), not source_key.
        source_event_id=f"livewhale:{source.source_key}:{external_id}:{start_at.isoformat()}",
        title=title,
        start_at=start_at,
        registration_url=registration_url,
        end_at=end_at,
        venue_name=venue_name,
        city=city,
        geo=geo,
        description=description,
        price_status=price_status,
        raw=dict(event),
    )


def _parse_iso(value: object) -> datetime | None:
    """Parse a LiveWhale ISO timestamp, assuming UTC only for a malformed-naive publisher value."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _registration_url(value: object, base_url: str) -> str | None:
    """Keep a usable handoff URL but never invent one from a malformed event record (FR-3.1)."""
    if not isinstance(value, str) or not value.strip():
        return None
    url = urljoin(base_url, value.strip())
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _event_identifier(
    value: object, source: CatalogSource, title: str, start_at: datetime, registration_url: str
) -> str:
    """Use the publisher ID when present; retain a stable fallback for a malformed public record."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    material = "|".join((source.source_key, title, start_at.isoformat(), registration_url))
    return f"derived:{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _location(event: dict[str, object]) -> tuple[str | None, str | None, GeoPoint | None]:
    """Extract only published location fields; do not manufacture a city from publisher scope (FR-3.7)."""
    location = _as_object_dict(event.get("location"))
    if location is None:
        venue_name = _first_text(
            event.get("location"), event.get("location_title"), event.get("location_name")
        )
        city = _text(event.get("city"))
        return venue_name, city, _geo(event)
    venue_name = _first_text(
        location.get("name"), location.get("title"), event.get("location_name")
    )
    city = _first_text(location.get("city"), event.get("city"))
    return venue_name, city, _geo(location) or _geo(event)


def _geo(value: dict[str, object]) -> GeoPoint | None:
    """Map optional numeric coordinates without treating booleans as latitude/longitude."""
    lat = _as_float(value.get("latitude"))
    if lat is None:
        lat = _as_float(value.get("lat"))
    if lat is None:
        lat = _as_float(value.get("location_latitude"))
    lon = _as_float(value.get("longitude"))
    if lon is None:
        lon = _as_float(value.get("lon"))
    if lon is None:
        lon = _as_float(value.get("lng"))
    if lon is None:
        lon = _as_float(value.get("location_longitude"))
    return GeoPoint(lat=lat, lon=lon) if lat is not None and lon is not None else None


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_bool(value: object) -> bool:
    """Interpret the source's documented booleans without treating arbitrary nonempty text as true."""
    return (
        value is True
        or (isinstance(value, int) and not isinstance(value, bool) and value == 1)
        or (isinstance(value, str) and value.strip().lower() == "true")
    )


def _is_online_only(event: dict[str, object]) -> bool:
    """Exclude remote-only records while retaining explicitly hybrid local events (FR-3.1)."""
    online_type = _text(event.get("online_type"))
    if online_type is not None:
        return "online only" in online_type.casefold()
    return _as_bool(event.get("is_online"))


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(HTMLParser(value).text(separator=" ", strip=True).split()) or None


def _first_text(*values: object) -> str | None:
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return None


def _price_status(value: object) -> PriceStatus:
    """Classify the public cost field conservatively; mixed tiers and absent prices remain unknown."""
    if isinstance(value, list):
        return aggregate_price_status(_price_status(item) for item in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return PriceStatus.FREE if value == 0 else PriceStatus.PAID
    text = _text(value)
    if text is None:
        return PriceStatus.UNKNOWN

    exact = _EXACT_NUMBER.fullmatch(text)
    if exact is not None:
        return PriceStatus.FREE if float(exact.group(1)) == 0 else PriceStatus.PAID
    amounts = [
        float(first or second)
        for first, second in _CURRENCY_AMOUNT.findall(text)
        if first or second
    ]
    has_free = _FREE_TEXT.search(text) is not None or any(amount == 0 for amount in amounts)
    has_paid = any(amount > 0 for amount in amounts)
    if has_free and has_paid:
        return PriceStatus.UNKNOWN
    if has_free:
        return PriceStatus.FREE
    if has_paid:
        return PriceStatus.PAID
    return PriceStatus.UNKNOWN


def _next_page_url(payload: dict[str, object], response_url: httpx.URL) -> str | None:
    """Resolve a documented ``links.next`` value without trusting a cross-origin target (FR-10.3)."""
    links = _as_object_dict(payload.get("links"))
    if links is None:
        return None
    raw_next = links.get("next")
    if isinstance(raw_next, str) and raw_next.strip():
        return str(response_url.join(raw_next.strip()))
    next_object = _as_object_dict(raw_next)
    href = next_object.get("href") if next_object is not None else None
    return str(response_url.join(href.strip())) if isinstance(href, str) and href.strip() else None


def _deduplicate(candidates: list[CandidateEvent]) -> list[CandidateEvent]:
    """Keep one observation per publisher occurrence if a feed repeats it across pages (FR-3.8)."""
    unique: dict[str, CandidateEvent] = {}
    for candidate in candidates:
        unique.setdefault(candidate.source_event_id, candidate)
    return list(unique.values())
