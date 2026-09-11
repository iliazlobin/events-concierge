"""Bounded public Luma-calendar ingestion (FR-3.1/FR-3.7/FR-10.3/10.4).

Luma's calendar page exposes only its first batch in JSON-LD and loads the remaining public
calendar entries through a cursor endpoint. This adapter is closed to the exact anonymous
future-event cursor named by an owner-reviewed source row: the calendar identity is *derived from*
that row's reviewed ``seed_url`` and never assembled from anything the provider returns. It paces
every request and fails the entire refresh instead of publishing a partial calendar.

A city Discover feed lists roughly one event per calendar, so it can never carry a host's real
programme; a host calendar is the only public cursor that does. The reviewed source row is what
admits one, which is why this adapter carries no per-calendar allowlist of its own -- a fleet of
reviewed host calendars is auditable in ``catalog_sources`` (revision-bumped and audited on every
change) in a way a parallel dict in this module would not be.

This is a discovery adapter, not Luma's browser RSVP adapter. Candidates retain
``Source.PUBLIC_JSONLD`` so every registration remains a human handoff.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ...application.ingestion_telemetry import record_ingestion_collection_progress
from ...domain.catalog_sources import CatalogSource
from ...domain.catalog_window import collection_reference_time, in_collection_window
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
_MAX_RESPONSE_BYTES = 5_000_000
_PAGE_SIZE = 20
_CURSOR_MAX_LENGTH = 2_048
_CALENDAR_API_ID = re.compile(r"cal-[A-Za-z0-9_-]{1,200}")
# The cursor endpoint does not vary its response by Referer (verified against the live API), so a
# reviewed row needs to carry only the calendar identity; the site root keeps the header honest.
_PUBLIC_CALENDAR_ORIGIN = "https://luma.com/"
_APPROVED_ORIGINS = ("https://api.luma.com", DETAIL_ORIGIN)
_REVIEWED_HOST = "api.luma.com"
_REVIEWED_PATH = "/calendar/get-items"


class LumaCalendarFetchError(RuntimeError):
    """The reviewed Luma page sequence could not be proven complete and safe."""


@dataclass(frozen=True, slots=True)
class _LumaCalendarProfile:
    source_key: str
    calendar_api_id: str
    public_calendar_url: str


@dataclass(frozen=True, slots=True)
class _LumaPage:
    entries: tuple[dict[str, object], ...]
    has_more: bool
    next_cursor: str | None


class LumaCalendarCatalogFetcher:
    """Fetch every page in one reviewed public Luma calendar cursor."""

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
        """Return the complete current future cursor or raise without publishing anything."""
        profile = _profile_for_source(source)
        now = _aware_utc(collection_reference_time(source, self._now()))
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_source_ids: set[str] = set()
        listed: list[tuple[dict[str, object], CandidateEvent]] = []
        headers = {
            "Accept": "application/json",
            "Referer": profile.public_calendar_url,
            "User-Agent": self._user_agent,
        }

        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            walked = False
            for page_number in range(1, source.page_limit + 1):
                url = _request_url(source.seed_url, cursor)
                response = await self._get_page(client, source, profile, url, cursor, page_number)
                page = _page_from_response(response, source.source_key, page_number)
                for entry in page.entries:
                    if not _is_luma_hosted_entry(entry, source.source_key):
                        continue
                    if not _is_approved_entry(entry, source.source_key):
                        continue
                    # A calendar cursor lists unlisted events too, and the "calendar" normalization
                    # contract does not assert visibility the way the Discover one does. Without
                    # this the adapter would publish a non-public event whenever its detail record
                    # happened to agree, and fail the ENTIRE calendar whenever it did not.
                    try:
                        if not is_public_entry(entry, source.source_key):
                            continue
                    except LumaEntryValidationError as exc:
                        raise LumaCalendarFetchError(str(exc)) from exc
                    candidate = _candidate_from_entry(entry, profile, source.source_key)
                    if candidate.source_event_id in seen_source_ids:
                        raise LumaCalendarFetchError(
                            f"Luma calendar {source.source_key} repeated an event identity"
                        )
                    seen_source_ids.add(candidate.source_event_id)
                    if candidate.start_at >= now and in_collection_window(source, candidate):
                        listed.append((entry, candidate))

                await record_ingestion_collection_progress(
                    source_key=source.source_key, request_completed=True,
                    page_completed=True, candidate_count=len(listed),
                )
                if not page.has_more:
                    walked = True
                    break
                next_cursor = page.next_cursor
                if next_cursor is None:
                    raise LumaCalendarFetchError(
                        f"Luma calendar page {page_number} declared more entries without a cursor"
                    )
                if next_cursor in seen_cursors:
                    raise LumaCalendarFetchError(
                        f"Luma calendar {source.source_key} repeated a pagination cursor"
                    )
                seen_cursors.add(next_cursor)
                cursor = next_cursor

            if not walked:
                raise LumaCalendarFetchError(
                    f"Luma calendar {source.source_key} exceeds its reviewed "
                    f"{source.page_limit}-page cap"
                )
            return await self._enriched(client, source, listed)

    async def _enriched(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        listed: list[tuple[dict[str, object], CandidateEvent]],
    ) -> list[CandidateEvent]:
        """Describe every retained event through the same detail lane Discover uses.

        A listing carries identity, timing, place, and price and nothing else. Without this pass an
        event that reaches the catalog through a host calendar would have no description, no
        speakers or partners, no attendance, and no registration status, while the *same* event
        arriving through the Discover shelf would have all of them -- so which source ran last
        would decide what the reader sees.
        """
        candidates: list[CandidateEvent] = []
        for detail_number, (entry, candidate) in enumerate(listed, start=1):
            enrichment = await self._get_detail_enrichment(
                client,
                source,
                entry,
                candidate,
                detail_number,
            )
            await record_ingestion_collection_progress(
                source_key=source.source_key, request_completed=True,
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
            raise LumaCalendarFetchError(str(exc)) from exc
        url = detail_url(event_api_id)
        if not source.allows_url(url) or not is_detail_url(url, event_api_id):
            raise LumaCalendarFetchError(
                f"Luma calendar detail {detail_number} left its reviewed endpoint"
            )
        headers = {
            "Referer": candidate.registration_url,
            "X-Luma-Client-Type": "luma-web",
            "X-Luma-Web-Url": candidate.registration_url,
        }
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, headers=headers, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise LumaCalendarFetchError(
                f"Luma calendar detail {detail_number} failed for {source.source_key}: {exc}"
            ) from exc
        if (
            response.is_redirect
            or not source.allows_url(str(response.url))
            or not is_detail_url(str(response.url), event_api_id)
        ):
            raise LumaCalendarFetchError(
                f"Luma calendar detail {detail_number} left its reviewed endpoint"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LumaCalendarFetchError(
                f"Luma calendar detail {detail_number} failed for {source.source_key}: {exc}"
            ) from exc
        if len(response.content) > MAX_DETAIL_RESPONSE_BYTES:
            raise LumaCalendarFetchError(
                f"Luma calendar detail {detail_number} exceeded its response-size limit"
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
            raise LumaCalendarFetchError(str(exc)) from exc

    async def _get_page(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        profile: _LumaCalendarProfile,
        url: str,
        cursor: str | None,
        page_number: int,
    ) -> httpx.Response:
        """Make one exact anonymous calendar-list GET; redirects are never followed."""
        if not source.allows_url(url) or not _is_request_url(url, profile, cursor):
            raise LumaCalendarFetchError(
                f"Luma calendar page {page_number} left its reviewed endpoint"
            )
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise LumaCalendarFetchError(
                f"Luma calendar page {page_number} failed for {source.source_key}: {exc}"
            ) from exc
        if (
            response.is_redirect
            or not source.allows_url(str(response.url))
            or not _is_request_url(str(response.url), profile, cursor)
        ):
            raise LumaCalendarFetchError(
                f"Luma calendar page {page_number} left its reviewed endpoint"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LumaCalendarFetchError(
                f"Luma calendar page {page_number} failed for {source.source_key}: {exc}"
            ) from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise LumaCalendarFetchError(
                f"Luma calendar page {page_number} exceeded its response-size limit"
            )
        return response

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed human-cadence floor before every cursor GET."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("Luma calendar URL must include a host")
        interval_s = min_interval_ms / 1_000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _profile_for_source(source: CatalogSource) -> _LumaCalendarProfile:
    """Derive the calendar identity from the reviewed row; never from a provider response."""
    if source.mode is not CatalogSourceMode.LUMA_CALENDAR_JSON:
        raise ValueError(f"unsupported Luma calendar source mode: {source.mode.value}")
    if not source.handoff_only:
        raise ValueError("Luma calendar catalog sources must remain handoff-only")
    if source.approved_origins != _APPROVED_ORIGINS:
        raise ValueError("Luma calendar source must approve only the reviewed API origins")
    calendar_api_id = reviewed_calendar_api_id(source.seed_url)
    if calendar_api_id is None:
        raise ValueError("Luma calendar source must use its reviewed public cursor endpoint")
    profile = _LumaCalendarProfile(
        source_key=source.source_key,
        calendar_api_id=calendar_api_id,
        public_calendar_url=_PUBLIC_CALENDAR_ORIGIN,
    )
    # Re-read the seed through the same predicate every page request is checked against, so a
    # row that parses here can never produce a page URL that would be rejected mid-walk.
    if not _is_request_url(source.seed_url, profile, None):
        raise ValueError("Luma calendar source must use its reviewed public cursor endpoint")
    return profile


def reviewed_calendar_api_id(seed_url: str) -> str | None:
    """Return the calendar a reviewed seed names, or None when the seed is not that exact shape.

    This is the whole registry-to-adapter contract for ``luma_calendar_json``: a row is admissible
    exactly when this returns an identity for its ``seed_url``.  It is public so the registry can
    be checked against it directly, rather than discovering a bad row at its first request.
    """
    try:
        parsed = urlsplit(seed_url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != _REVIEWED_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != _REVIEWED_PATH
        or parsed.fragment
    ):
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _ in pairs}) != len(pairs):
        return None
    parameters = dict(pairs)
    if (
        set(parameters) != {"calendar_api_id", "pagination_limit", "period"}
        or parameters["pagination_limit"] != str(_PAGE_SIZE)
        or parameters["period"] != "future"
        or _CALENDAR_API_ID.fullmatch(parameters["calendar_api_id"]) is None
    ):
        return None
    return parameters["calendar_api_id"]


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
    profile: _LumaCalendarProfile,
    cursor: str | None,
) -> bool:
    """Accept only the reviewed host/path and exact immutable calendar query."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname != _REVIEWED_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != _REVIEWED_PATH
        or parsed.fragment
    ):
        return False
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _ in pairs}) != len(pairs):
        return False
    parameters = dict(pairs)
    expected = {
        "calendar_api_id": profile.calendar_api_id,
        "pagination_limit": str(_PAGE_SIZE),
        "period": "future",
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
        raise LumaCalendarFetchError(
            f"Luma calendar page {page_number} returned invalid JSON for {source_key}"
        ) from exc
    if payload is None:
        raise LumaCalendarFetchError(
            f"Luma calendar page {page_number} returned an invalid object for {source_key}"
        )
    raw_entries = payload.get("entries")
    has_more = payload.get("has_more")
    if (
        not isinstance(raw_entries, list)
        or not isinstance(has_more, bool)
        or len(raw_entries) > _PAGE_SIZE
    ):
        raise LumaCalendarFetchError(
            f"Luma calendar page {page_number} returned malformed pagination for {source_key}"
        )
    entries: list[dict[str, object]] = []
    for raw_entry in raw_entries:
        entry = json_object(raw_entry)
        if entry is None:
            raise LumaCalendarFetchError(
                f"Luma calendar page {page_number} returned a malformed entry for {source_key}"
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
        raise LumaCalendarFetchError(
            f"Luma calendar page {page_number} returned an incomplete cursor for {source_key}"
        )
    return _LumaPage(tuple(entries), has_more, next_cursor)


def _is_luma_hosted_entry(entry: dict[str, object], source_key: str) -> bool:
    """Admit only entries the calendar declares are Luma-hosted events.

    A calendar may also list an *off-platform* event -- ``platform: "external"`` -- which carries
    no Luma event identity at all: no ``api_id``, no ``calendar_api_id``, no ``visibility``, and an
    arbitrary third-party absolute URL in place of the event slug. There is nothing to key such a
    record on and its registration handoff would leave every reviewed origin, so it is not a
    candidate. Measured on the reviewed fleet: 7 of Postman Developer Events' 17 entries, 3 of
    fal's 7, and 2 of OpenRouter's 4 are external listings.

    The declaration is read explicitly rather than inferred from the absent identity, and an entry
    that does not declare one at all fails the refresh -- the same posture ``_is_approved_entry``
    takes toward publication.
    """
    platform = entry.get("platform")
    if not isinstance(platform, str):
        raise LumaCalendarFetchError(
            f"Luma calendar {source_key} returned an entry with no hosting platform"
        )
    return platform == "luma"


def _is_approved_entry(entry: dict[str, object], source_key: str) -> bool:
    """Admit only explicitly public calendar entries; never infer publication from presence."""
    status = entry.get("status")
    if not isinstance(status, str):
        raise LumaCalendarFetchError(
            f"Luma calendar {source_key} returned an invalid publication status"
        )
    return status == "approved"


def _candidate_from_entry(
    entry: dict[str, object],
    profile: _LumaCalendarProfile,
    source_key: str,
) -> CandidateEvent:
    try:
        return normalize_luma_entry(
            entry,
            source_key,
            contract="calendar",
            listed_calendar_api_id=profile.calendar_api_id,
        )
    except LumaEntryValidationError as exc:
        raise LumaCalendarFetchError(str(exc)) from exc


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Luma calendar clock must be timezone-aware")
    return value.astimezone(UTC)
