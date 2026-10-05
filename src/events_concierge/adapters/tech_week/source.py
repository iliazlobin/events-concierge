"""Walk the official anonymous calendar through its read-only MCP search tool.

Only two reviewed 2026 city profiles and one fixed HTTP endpoint are admitted. No remote
instructions, arbitrary tools, registration actions, links, or images are executed/fetched.
Every page and provider identity must reconcile before the existing fenced publication path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

from ...application.ingestion_telemetry import record_ingestion_collection_progress
from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, RegistrationStatus, Source
from ...domain.events import CandidateEvent
from ...domain.policy import SourceQuarantineSignal
from ...ports.sources import SourceAccessDeniedError, SourceRateLimitedError, SourceTransientError
from ..meetup_city.source import public_retry_after

_ORIGIN = "https://www.tech-week.com"
_ENDPOINT = _ORIGIN + "/api/mcp"
_ZONE = ZoneInfo("America/Los_Angeles")
_PAGE_SIZE = 75
_MAX_PAGES = 40
_MAX_BYTES = 2_000_000
_TIMEOUT_SECONDS = 20
_MIN_INTERVAL_MS = 1500
_HORIZON_DAYS = 15
_MAX_ORGANIZATIONS = 32
_MAX_TIMESTAMP_CHARS = 40
_MAX_URL_CHARS = 2048
_COLLECTION_START = datetime(2026, 10, 5, 7, tzinfo=UTC)
_COLLECTION_END = datetime(2026, 10, 20, 7, tzinfo=UTC)


class TechWeekFetchError(RuntimeError):
    """Calendar completeness, identity or its closed read contract was not proven."""


@dataclass(frozen=True, slots=True)
class _Profile:
    city: str
    city_name: str
    start: datetime
    end: datetime


_PROFILES = {
    "tech-week-sf-2026": _Profile(
        "sf", "San Francisco", datetime(2026, 10, 5, 7, tzinfo=UTC),
        datetime(2026, 10, 12, 7, tzinfo=UTC),
    ),
    "tech-week-la-2026": _Profile(
        "la", "Los Angeles", datetime(2026, 10, 12, 7, tzinfo=UTC),
        datetime(2026, 10, 19, 7, tzinfo=UTC),
    ),
}


class TechWeekCatalogFetcher:
    """Collect a whole edition, including earlier starts, without treating missing prices as free."""

    def __init__(
        self, *, user_agent: str, now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._now = now or (lambda: datetime.now(UTC))
        self._clock = clock or monotonic
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._last_request_at: float | None = None
        self._pacing_lock = asyncio.Lock()

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        profile = _reviewed_profile(source)
        candidates: list[CandidateEvent] = []
        seen: set[str] = set()
        expected_total: int | None = None
        async with httpx.AsyncClient(
            headers={"Accept": "application/json", "User-Agent": self._user_agent},
            timeout=_TIMEOUT_SECONDS, follow_redirects=False, transport=self._transport,
        ) as client:
            for page in range(1, source.page_limit + 1):
                payload = await self._read_page(client, source, profile, page)
                entries, total, has_more = _page(payload, page, source.page_limit)
                if expected_total is not None and total != expected_total:
                    raise TechWeekFetchError("Tech Week calendar changed totals during pagination")
                expected_total = total
                for entry in entries:
                    candidate = _candidate(entry, profile)
                    if candidate.source_event_id in seen:
                        raise TechWeekFetchError("Tech Week calendar repeated an event identity")
                    seen.add(candidate.source_event_id)
                    candidates.append(candidate)
                await record_ingestion_collection_progress(
                    source_key=source.source_key, request_completed=True, page_completed=True,
                    candidate_count=len(entries),
                )
                if not has_more:
                    if len(candidates) != total:
                        raise TechWeekFetchError("Tech Week calendar did not reconcile its total")
                    return candidates
        raise TechWeekFetchError("Tech Week calendar exceeds its reviewed page limit")

    async def _read_page(  # noqa: PLR0912 -- HTTP/RPC boundary signals remain distinct.
        self, client: httpx.AsyncClient, source: CatalogSource, profile: _Profile, page: int,
    ) -> dict[str, object]:
        # One shared same-origin gate also paces SF -> LA on a reused worker instance.
        async with self._pacing_lock:
            if self._last_request_at is not None:
                wait = source.min_interval_ms / 1000 - (self._clock() - self._last_request_at)
                if wait > 0:
                    await self._sleep(wait)
            self._last_request_at = self._clock()
        body = {
            "jsonrpc": "2.0", "id": page, "method": "tools/call",
            "params": {"name": "search_events", "arguments": {
                "city": [profile.city], "limit": _PAGE_SIZE, "page": page,
                "sort": "time", "direction": "asc",
            }},
        }
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS), client.stream(
                "POST", _ENDPOINT, json=body, follow_redirects=False,
            ) as response:
                if response.is_redirect or str(response.url) != _ENDPOINT:
                    raise TechWeekFetchError("Tech Week endpoint redirected or changed identity")
                if response.status_code in (401, 403):
                    raise SourceAccessDeniedError(SourceQuarantineSignal.FORBIDDEN)
                if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                    raise SourceRateLimitedError(
                        "Tech Week calendar is rate limited",
                        retry_after_seconds=public_retry_after(response.headers.get("Retry-After"), self._now()),
                    )
                if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
                    raise SourceTransientError("Tech Week calendar is temporarily unavailable", retry_after_seconds=30)
                response.raise_for_status()
                if response.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
                    raise TechWeekFetchError("Tech Week response is not JSON")
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > _MAX_BYTES):
                    raise TechWeekFetchError("Tech Week response exceeds its byte limit")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > _MAX_BYTES:
                        raise TechWeekFetchError("Tech Week response exceeds its byte limit")
                    content.extend(chunk)
            envelope = _object(json.loads(content))
            if (envelope.get("jsonrpc") != "2.0" or type(envelope.get("id")) is not int
                or envelope["id"] != page or "error" in envelope):
                raise TechWeekFetchError("Tech Week RPC response does not match the request")
            result = _object(envelope.get("result"))
            if result.get("isError", False) is not False:
                raise TechWeekFetchError("Tech Week search tool failed")
            return _object(result.get("structuredContent"))
        except (httpx.TransportError, TimeoutError) as exc:
            raise SourceTransientError("Tech Week calendar transport is temporarily unavailable", retry_after_seconds=30) from exc
        except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
            raise TechWeekFetchError("Tech Week response or encoding is invalid") from exc


def _reviewed_profile(source: CatalogSource) -> _Profile:
    profile = _PROFILES.get(source.source_key)
    if (profile is None or source.mode is not CatalogSourceMode.TECH_WEEK_MCP
        or source.seed_url != f"{_ORIGIN}/calendar/{profile.city}"
        or source.approved_origins != (_ORIGIN,) or not source.handoff_only
        or type(source.page_limit) is not int or not 1 <= source.page_limit <= _MAX_PAGES
        or source.min_interval_ms < _MIN_INTERVAL_MS or source.collection_horizon_days != _HORIZON_DAYS):
        raise ValueError("Tech Week source must retain its reviewed 2026 city contract")
    window = source.collection_window
    if window is not None and (window.start_at != _COLLECTION_START or window.end_at != _COLLECTION_END):
        raise ValueError("Tech Week collection requires its database-frozen edition window")
    return profile


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TechWeekFetchError("Tech Week response contains an invalid object")
    return value


def _page(payload: dict[str, object], number: int, page_limit: int) -> tuple[list[dict[str, object]], int, bool]:
    entries = payload.get("events")
    total = payload.get("total")
    has_more = payload.get("hasMore")
    if (type(payload.get("page")) is not int or payload["page"] != number
        or type(payload.get("perPage")) is not int or payload["perPage"] != _PAGE_SIZE
        or payload.get("totalIsUpperBound") is not False or type(total) is not int
        or not 0 <= total <= page_limit * _PAGE_SIZE or type(has_more) is not bool
        or not isinstance(entries, list) or len(entries) > _PAGE_SIZE):
        raise TechWeekFetchError("Tech Week pagination metadata is invalid or exceeds the cap")
    expected = min(_PAGE_SIZE, max(0, total - (number - 1) * _PAGE_SIZE))
    if len(entries) != expected or has_more != (number * _PAGE_SIZE < total):
        raise TechWeekFetchError("Tech Week page is incomplete or has an inconsistent continuation")
    return [_object(entry) for entry in entries], total, has_more


def _text(value: object, maximum: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TechWeekFetchError("Tech Week public text has an invalid type")
    clean = " ".join(value.split())
    if not clean or len(clean) > maximum:
        raise TechWeekFetchError("Tech Week public text is empty or too long")
    return clean


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_ORGANIZATIONS:
        raise TechWeekFetchError("Tech Week public organization list is invalid")
    names: dict[str, str] = {}
    for item in value:
        name = _text(item, 160)
        assert name is not None
        names.setdefault(name.casefold(), name)
    return tuple(names.values())


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > _MAX_TIMESTAMP_CHARS:
        raise TechWeekFetchError("Tech Week event time is missing or invalid")
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("floating timestamp")
        return timestamp.astimezone(UTC)
    except ValueError as exc:
        raise TechWeekFetchError("Tech Week event time is invalid") from exc


def _candidate(entry: dict[str, object], profile: _Profile) -> CandidateEvent:
    identity = entry.get("id")
    try:
        if not isinstance(identity, str) or str(UUID(identity)) != identity:
            raise ValueError("UUID")
    except ValueError as exc:
        raise TechWeekFetchError("Tech Week event identity is invalid") from exc
    url = entry.get("eventUrl")
    if not isinstance(url, str) or len(url) > _MAX_URL_CHARS:
        raise TechWeekFetchError("Tech Week event link is invalid")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "www.tech-week.com" or parsed.fragment
        or not parsed.path.startswith(f"/calendar/{profile.city}/events/")
        or not parsed.path.endswith("-" + identity)
        or "/" in parsed.path.removeprefix(f"/calendar/{profile.city}/events/")
        or parse_qsl(parsed.query, keep_blank_values=True) != [("src", "mcp")]):
        raise TechWeekFetchError("Tech Week event link or city identity is inconsistent")
    start = _timestamp(entry.get("startsAt"))
    end = _timestamp(entry["endsAt"]) if entry.get("endsAt") is not None else None
    if (entry.get("citySlug") != profile.city or entry.get("city") != profile.city_name
        or entry.get("timeZone") != _ZONE.key
        or entry.get("date") != start.astimezone(_ZONE).date().isoformat()
        or not profile.start <= start < profile.end or (end is not None and end <= start)):
        raise TechWeekFetchError("Tech Week event dates, edition or timezone are inconsistent")
    registration = entry.get("registration")
    if registration not in ("open", "waitlist", "full", "closed", "unknown", "invite_only"):
        raise TechWeekFetchError("Tech Week registration state is invalid")
    status = {"open": RegistrationStatus.OPEN, "waitlist": RegistrationStatus.WAITLIST,
              "full": RegistrationStatus.SOLD_OUT}.get(str(registration), RegistrationStatus.UNKNOWN)
    title = _text(entry.get("name"), 500)
    assert title is not None
    description = _text(entry.get("excerpt") or entry.get("summary"), 20_000, optional=True) or ""
    hosts = _names(entry.get("hosts"))
    sponsors = _names(entry.get("sponsors"))
    categories = tuple(dict.fromkeys((*_names(entry.get("themes")), *_names(entry.get("formats")))))
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD, source_event_id="tech-week:" + identity, title=title,
        start_at=start, end_at=end, registration_url=url, city=profile.city_name,
        venue_name=_text(entry.get("venue"), 500, optional=True), description=description,
        price_status=PriceStatus.UNKNOWN, organizer_name=hosts[0] if hosts else None,
        host_names=hosts, partner_names=sponsors, registration_status=status,
        raw={"adapter": "tech_week_mcp", "event_url": url, "edition": 2026,
             "calendar_city": profile.city, "provider_registration": registration,
             "categories": list(categories)},
    )
