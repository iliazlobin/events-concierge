"""Bounded ingestion of reviewed public Luma regional Discover cursors.

Luma's city pages load ranked future-event catalogs from an anonymous, cursor-paginated Discover
endpoint. Their list records intentionally omit the event description, so each retained public
event is enriched through Luma's anonymous structured event-detail endpoint. This adapter is closed
to exact reviewed place identifiers and exact list/detail paths, paces every request per host, and
fails atomically if the page sequence or any retained public detail cannot be proven complete.

This remains discovery-only. Every candidate uses a public event handoff URL; no RSVP or account
automation is performed here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode
from ...domain.events import CandidateEvent
from ..luma_common import (
    LumaEntryValidationError,
    is_public_entry,
    json_object,
    normalize_luma_entry,
)
from ..luma_detail import (
    DETAIL_ORIGIN,
    MAX_DETAIL_RESPONSE_BYTES,
    LumaDetailEnrichment,
    LumaDetailValidationError,
    detail_url,
    enrichment_from_detail,
    is_detail_url,
    listed_identity,
    resolved_price,
)

_FETCH_TIMEOUT_S = 30.0
_MAX_LIST_RESPONSE_BYTES = 8_000_000
_PAGE_SIZE = 25
_CURSOR_MAX_LENGTH = 2_048
_APPROVED_ORIGINS = ("https://api.luma.com", DETAIL_ORIGIN)


class LumaDiscoverFetchError(RuntimeError):
    """The reviewed Luma Discover sequence could not be proven complete and safe."""


@dataclass(frozen=True, slots=True)
class _LumaDiscoverProfile:
    source_key: str
    discover_place_api_id: str
    public_discover_url: str


_PROFILES = {
    "luma-sf": _LumaDiscoverProfile(
        source_key="luma-sf",
        discover_place_api_id="discplace-BDj7GNbGlsF7Cka",
        public_discover_url="https://luma.com/sf",
    ),
    "luma-nyc": _LumaDiscoverProfile(
        source_key="luma-nyc",
        discover_place_api_id="discplace-Izx1rQVSh8njYpP",
        public_discover_url="https://luma.com/nyc",
    ),
}


@dataclass(frozen=True, slots=True)
class _LumaPage:
    entries: tuple[dict[str, object], ...]
    has_more: bool
    next_cursor: str | None



class LumaDiscoverCatalogFetcher:
    """Fetch every public page in one reviewed Luma Discover place cursor."""

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
        """Return one complete enriched future cursor or publish no partial result."""
        profile = _profile_for_source(source)
        now = _aware_utc(self._now())
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_source_ids: set[str] = set()
        listed_candidates: list[tuple[dict[str, object], CandidateEvent]] = []
        headers = {
            "Accept": "application/json",
            "Referer": profile.public_discover_url,
            "User-Agent": self._user_agent,
        }

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            for page_number in range(1, source.page_limit + 1):
                url = _request_url(source.seed_url, cursor)
                response = await self._get_page(
                    client,
                    source,
                    profile,
                    url,
                    cursor,
                    page_number,
                )
                page = _page_from_response(response, source.source_key, page_number)
                for entry in page.entries:
                    try:
                        if not is_public_entry(entry, source.source_key):
                            continue
                        candidate = normalize_luma_entry(
                            entry,
                            source.source_key,
                            contract="discover",
                        )
                    except LumaEntryValidationError as exc:
                        raise LumaDiscoverFetchError(str(exc)) from exc
                    if candidate.source_event_id in seen_source_ids:
                        raise LumaDiscoverFetchError(
                            f"Luma Discover {source.source_key} repeated an event identity"
                        )
                    seen_source_ids.add(candidate.source_event_id)
                    if candidate.start_at >= now:
                        listed_candidates.append((entry, candidate))

                if not page.has_more:
                    break
                next_cursor = page.next_cursor
                if next_cursor is None:
                    raise LumaDiscoverFetchError(
                        f"Luma Discover page {page_number} declared more entries without a cursor"
                    )
                if next_cursor in seen_cursors:
                    raise LumaDiscoverFetchError(
                        f"Luma Discover {source.source_key} repeated a pagination cursor"
                    )
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise LumaDiscoverFetchError(
                    f"Luma Discover {source.source_key} exceeds its reviewed "
                    f"{source.page_limit}-page cap"
                )

            candidates: list[CandidateEvent] = []
            for detail_number, (entry, candidate) in enumerate(listed_candidates, start=1):
                enrichment = await self._get_detail_enrichment(
                    client,
                    source,
                    entry,
                    candidate,
                    detail_number,
                )
                (
                    price_status,
                    price_min_cents,
                    price_max_cents,
                    price_currency,
                ) = resolved_price(candidate, enrichment)
                candidates.append(
                    replace(
                        candidate,
                        description=enrichment.description,
                        organizer_name=enrichment.organizer_name,
                        host_names=enrichment.host_names,
                        speaker_names=enrichment.speaker_names,
                        partner_names=enrichment.partner_names,
                        entity_profiles=enrichment.entity_profiles,
                        entity_social_links=enrichment.entity_social_links,
                        attendance_count=enrichment.attendance_count,
                        registration_status=enrichment.registration_status,
                        is_free=price_status.is_free,
                        price_status=price_status,
                        price_min_cents=price_min_cents,
                        price_max_cents=price_max_cents,
                        price_currency=price_currency,
                    )
                )
            return candidates

    async def _get_page(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        profile: _LumaDiscoverProfile,
        url: str,
        cursor: str | None,
        page_number: int,
    ) -> httpx.Response:
        """Make one exact anonymous Discover GET; redirects are never followed."""
        if not source.allows_url(url) or not _is_request_url(url, profile, cursor):
            raise LumaDiscoverFetchError(
                f"Luma Discover page {page_number} left its reviewed endpoint"
            )
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise LumaDiscoverFetchError(
                f"Luma Discover page {page_number} failed for {source.source_key}: {exc}"
            ) from exc
        if (
            response.is_redirect
            or not source.allows_url(str(response.url))
            or not _is_request_url(str(response.url), profile, cursor)
        ):
            raise LumaDiscoverFetchError(
                f"Luma Discover page {page_number} left its reviewed endpoint"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LumaDiscoverFetchError(
                f"Luma Discover page {page_number} failed for {source.source_key}: {exc}"
            ) from exc
        if len(response.content) > _MAX_LIST_RESPONSE_BYTES:
            raise LumaDiscoverFetchError(
                f"Luma Discover page {page_number} exceeded its response-size limit"
            )
        return response

    async def _get_detail_enrichment(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        entry: dict[str, object],
        candidate: CandidateEvent,
        detail_number: int,
    ) -> LumaDetailEnrichment:
        """Fetch one exact anonymous detail record and reduce it to bounded public metadata."""
        try:
            event_api_id, calendar_api_id, slug = listed_identity(
                entry,
                candidate,
                source.source_key,
            )
        except LumaDetailValidationError as exc:
            raise LumaDiscoverFetchError(str(exc)) from exc
        url = detail_url(event_api_id)
        if not source.allows_url(url) or not is_detail_url(url, event_api_id):
            raise LumaDiscoverFetchError(
                f"Luma Discover detail {detail_number} left its reviewed endpoint"
            )
        headers = {
            "Referer": candidate.registration_url,
            "X-Luma-Client-Type": "luma-web",
            "X-Luma-Web-Url": candidate.registration_url,
        }
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(
                url,
                headers=headers,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise LumaDiscoverFetchError(
                f"Luma Discover detail {detail_number} failed for "
                f"{source.source_key}: {exc}"
            ) from exc
        if (
            response.is_redirect
            or not source.allows_url(str(response.url))
            or not is_detail_url(str(response.url), event_api_id)
        ):
            raise LumaDiscoverFetchError(
                f"Luma Discover detail {detail_number} left its reviewed endpoint"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LumaDiscoverFetchError(
                f"Luma Discover detail {detail_number} failed for "
                f"{source.source_key}: {exc}"
            ) from exc
        if len(response.content) > MAX_DETAIL_RESPONSE_BYTES:
            raise LumaDiscoverFetchError(
                f"Luma Discover detail {detail_number} exceeded its response-size limit"
            )
        try:
            return enrichment_from_detail(
                response,
                entry=entry,
                source_key=source.source_key,
                event_api_id=event_api_id,
                calendar_api_id=calendar_api_id,
                slug=slug,
                title=candidate.title,
                detail_number=detail_number,
            )
        except LumaDetailValidationError as exc:
            raise LumaDiscoverFetchError(str(exc)) from exc

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed human-cadence floor before every cursor GET."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("Luma Discover URL must include a host")
        interval_s = min_interval_ms / 1_000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _profile_for_source(source: CatalogSource) -> _LumaDiscoverProfile:
    """Require the exact reviewed source row before constructing a cursor URL."""
    if source.mode is not CatalogSourceMode.LUMA_DISCOVER_JSON:
        raise ValueError(f"unsupported Luma Discover source mode: {source.mode.value}")
    if not source.handoff_only:
        raise ValueError("Luma Discover catalog sources must remain handoff-only")
    profile = _PROFILES.get(source.source_key)
    if profile is None or not _is_request_url(source.seed_url, profile, None):
        raise ValueError("Luma Discover source must use its reviewed public cursor endpoint")
    if source.approved_origins != _APPROVED_ORIGINS:
        raise ValueError("Luma Discover source must approve only the reviewed API origins")
    return profile


def _request_url(seed_url: str, cursor: str | None) -> str:
    parsed = urlsplit(seed_url)
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    if cursor is not None:
        parameters.append(("pagination_cursor", cursor))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(parameters), parsed.fragment)
    )


def _is_request_url(
    value: str,
    profile: _LumaDiscoverProfile,
    cursor: str | None,
) -> bool:
    """Accept only the reviewed host/path and exact immutable Discover-place query."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.luma.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/discover/get-paginated-events"
        or parsed.fragment
    ):
        return False
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _ in pairs}) != len(pairs):
        return False
    parameters = dict(pairs)
    expected = {
        "discover_place_api_id": profile.discover_place_api_id,
        "pagination_limit": str(_PAGE_SIZE),
    }
    if cursor is not None:
        expected["pagination_cursor"] = cursor
    return parameters == expected


def _page_from_response(
    response: httpx.Response,
    source_key: str,
    page_number: int,
) -> _LumaPage:
    try:
        payload = json_object(response.json())
    except ValueError as exc:
        raise LumaDiscoverFetchError(
            f"Luma Discover page {page_number} returned invalid JSON for {source_key}"
        ) from exc
    if payload is None:
        raise LumaDiscoverFetchError(
            f"Luma Discover page {page_number} returned an invalid object for {source_key}"
        )
    raw_entries = payload.get("entries")
    has_more = payload.get("has_more")
    if (
        not isinstance(raw_entries, list)
        or not isinstance(has_more, bool)
        or len(raw_entries) > _PAGE_SIZE
    ):
        raise LumaDiscoverFetchError(
            f"Luma Discover page {page_number} returned malformed pagination for {source_key}"
        )
    entries: list[dict[str, object]] = []
    for raw_entry in raw_entries:
        entry = json_object(raw_entry)
        if entry is None:
            raise LumaDiscoverFetchError(
                f"Luma Discover page {page_number} returned a malformed entry for {source_key}"
            )
        entries.append(entry)
    raw_cursor = payload.get("next_cursor")
    next_cursor = (
        raw_cursor.strip()
        if isinstance(raw_cursor, str)
        and raw_cursor.strip()
        and len(raw_cursor.strip()) <= _CURSOR_MAX_LENGTH
        else None
    )
    if has_more and (not entries or next_cursor is None):
        raise LumaDiscoverFetchError(
            f"Luma Discover page {page_number} returned an incomplete cursor for {source_key}"
        )
    return _LumaPage(tuple(entries), has_more, next_cursor)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Luma Discover clock must be timezone-aware")
    return value.astimezone(UTC)
