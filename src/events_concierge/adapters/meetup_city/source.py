"""Bounded anonymous ingestion of reviewed public Meetup city pages.

Meetup's city pages publish a small server-rendered schema.org ``Event`` catalog alongside a much
larger Next.js application payload.  This adapter reads only root ``application/ld+json`` Event
blocks from the city page, then may enrich the first forty events from the exact public event URLs
asserted by those blocks. Detail enrichment trusts only an identity-matched root Event JSON-LD
object, bounded visible ``p``/``li`` text beneath a heading whose text is exactly ``Details``, and
the event page's dedicated visible ``Hosted by`` assertion. It never parses application state,
attendee cards/rosters, RSVP data, signs in, or uses Meetup's OAuth API.

The fetch contract is closed to two owner-reviewed city-page identities and forty canonical detail
pages. Every same-host GET is paced, refuses redirects, and has a decoded response-size cap. A
detail failure cannot discard the validated city snapshot and is retained only as a closed status
code; raw HTML and arbitrary provider errors never cross the adapter boundary.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.enums import CatalogSourceMode, PriceStatus, RegistrationStatus, Source
from ...domain.events import MAX_PUBLIC_PRICE_CENTS, CandidateEvent, GeoPoint

_FETCH_TIMEOUT_S = 20.0
_MAX_RESPONSE_BYTES = 2_000_000
_REVIEWED_REQUEST_UNIT_CAP = 41
_MAX_DETAIL_REQUESTS = _REVIEWED_REQUEST_UNIT_CAP - 1
_MAX_DESCRIPTION_CHARS = 12_000
_MAX_VISIBLE_DETAILS_NODES = 64
_MAX_VISIBLE_DETAILS_NODE_CHARS = 2_000
_MAX_TITLE_CHARS = 500
_MAX_VENUE_CHARS = 500
_MAX_ADDRESS_CHARS = 1_000
_MAX_IMAGE_URL_CHARS = 2_048
_MAX_EVENT_URL_CHARS = 2_048
_MAX_ORGANIZER_NAME_CHARS = 160
_MAX_VISIBLE_HOSTS = 8
_ABBREVIATED_HOST_PARTS = 2
_INITIAL_WITH_PERIOD_CHARS = 2
_MAX_ORGANIZER_URL_CHARS = 2_048
_MAX_TOPIC_LABELS = 32
_MAX_TOPIC_LABEL_CHARS = 80
_LATITUDE_LIMIT = 90.0
_LONGITUDE_LIMIT = 180.0
_CURRENCY_CODE_LENGTH = 3
_MIN_PRINTABLE_CODEPOINT = 0x20
_DELETE_CODEPOINT = 0x7F
_EVENT_PATH = re.compile(
    r"^/(?P<group>[a-z0-9][a-z0-9_-]{0,119})/events/(?P<event_id>[1-9][0-9]*)/$"
)
_GROUP_PATH = re.compile(r"^/[a-z0-9][a-z0-9_-]{0,119}/$")
_IMAGE_HOSTS = frozenset(
    {
        "www.meetup.com",
        "secure.meetupstatic.com",
        "secure-content.meetupstatic.com",
    }
)
_CANCELLED_STATUSES = frozenset({"eventcancelled", "eventpostponed"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_HIDDEN_STYLE = re.compile(r"(?:^|;)(?:display:none|visibility:hidden)(?:;|$)")
_STRONG_FREE_LINE = re.compile(
    # Accept a provider's explanatory sentence only after a punctuated, explicit price label.
    # ``Cost: free trial`` therefore remains unknown, while ``COST: FREE! Donations welcome``
    # is still strong public evidence that the event itself has no admission price.
    r"^(?:cost|admission)\s*:\s*free(?:(?:[!.])(?:\s+.*)?|)$",
    re.IGNORECASE,
)
_STRONG_USD_LINE = re.compile(
    r"^(?:cost|admission)\s*:\s*\$(?P<amount>[0-9]{1,7}(?:\.[0-9]{1,2})?)"
    r"(?:\s*(?:/pp|per\s+person))?(?:(?:[,.!])(?:\s+.*)?|)$",
    re.IGNORECASE,
)
_WHERE_LINE = re.compile(r"^where\s*:\s*(?P<value>.+)$", re.IGNORECASE)
_HOSTED_BY_LABEL = re.compile(r"^hosted by\s+(?P<name>.+)$", re.IGNORECASE)
_HOST_IMAGE_ALT = re.compile(r"^photo of the user\s+(?P<name>.+)$", re.IGNORECASE)
_STREET_ADDRESS = re.compile(
    r"^\d{1,6}(?:-\d{1,6})?\s+.{1,160}\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|"
    r"drive|dr|lane|ln|court|ct|way|place|pl|parkway|pkwy|highway|hwy|terrace|ter)\.?$",
    re.IGNORECASE,
)
_REGION_AND_POSTAL = re.compile(r"^(?P<region>[A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?$")
_DETAIL_STATUS_CODES = frozenset(
    {
        "enriched",
        "validated_no_new_evidence",
        "request_cap_not_fetched",
        "request_failed",
        "redirect_refused",
        "http_error",
        "invalid_content_type",
        "response_too_large",
        "invalid_utf8",
        "missing_event_jsonld",
        "malformed_event_jsonld",
        "identity_mismatch",
        "invalid_event_jsonld",
    }
)


class MeetupCityFetchError(RuntimeError):
    """The reviewed Meetup page could not be fetched or normalized as one safe snapshot."""


class _MeetupDetailFetchError(RuntimeError):
    """One best-effort detail request failed with a bounded operator-safe code."""

    def __init__(self, code: str) -> None:
        if code not in _DETAIL_STATUS_CODES:
            raise ValueError("unknown Meetup detail status code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _MeetupCityProfile:
    source_key: str
    city_url: str


_PROFILES = {
    "meetup-sf": _MeetupCityProfile(
        source_key="meetup-sf",
        city_url="https://www.meetup.com/find/us--ca--san-francisco/",
    ),
    "meetup-nyc": _MeetupCityProfile(
        source_key="meetup-nyc",
        city_url="https://www.meetup.com/find/us--ny--new-york/",
    ),
}


@dataclass(frozen=True, slots=True)
class _Location:
    venue_name: str | None
    city: str | None
    geo: GeoPoint | None
    street_address: str | None
    address_region: str | None
    address_country: str | None


@dataclass(frozen=True, slots=True)
class _Offer:
    status: PriceStatus
    min_cents: int | None
    max_cents: int | None
    currency: str | None
    registration_status: RegistrationStatus


@dataclass(frozen=True, slots=True)
class _VisibleWhere:
    venue_name: str | None
    city: str | None
    street_address: str
    address_region: str
    address_country: str


@dataclass(frozen=True, slots=True)
class _VisibleDetails:
    description: str
    offer: _Offer
    where: _VisibleWhere | None
    evidence_codes: tuple[str, ...]


class MeetupCityCatalogFetcher:
    """Fetch one exact city page and a bounded set of identity-checked event details."""

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
        """Return the city snapshot with best-effort, request-capped public enrichment."""
        profile = _profile_for_source(source)
        headers = {
            "Accept": "text/html",
            "User-Agent": self._user_agent,
        }
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            html = await self._fetch_city_html(client, source, profile)
            candidates = _candidates_from_html(
                html,
                source_key=source.source_key,
                now=_aware_utc(self._now()),
            )
            enriched: list[CandidateEvent] = []
            for detail_number, candidate in enumerate(candidates, start=1):
                if detail_number > _MAX_DETAIL_REQUESTS:
                    enriched.append(_with_detail_status(candidate, "request_cap_not_fetched"))
                    continue
                try:
                    detail_html = await self._fetch_detail_html(
                        client,
                        source,
                        candidate.registration_url,
                    )
                except _MeetupDetailFetchError as exc:
                    enriched.append(_with_detail_status(candidate, exc.code))
                    continue
                enriched.append(_enrich_from_detail(candidate, detail_html))
            return enriched

    async def _fetch_city_html(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        profile: _MeetupCityProfile,
    ) -> str:
        """Perform one response-capped GET; redirects are contract failures, never followed."""
        url = source.seed_url
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            async with client.stream("GET", url, follow_redirects=False) as response:
                _validate_response_envelope(response, source, profile)
                content = await _read_capped_city_response(response, source.source_key)
        except MeetupCityFetchError:
            raise
        except httpx.HTTPError as exc:
            raise MeetupCityFetchError(
                f"Meetup city source {source.source_key} request failed: {exc}"
            ) from exc
        try:
            return bytes(content).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MeetupCityFetchError(
                f"Meetup city source {source.source_key} was not UTF-8"
            ) from exc

    async def _fetch_detail_html(
        self,
        client: httpx.AsyncClient,
        source: CatalogSource,
        event_url: str,
    ) -> str:
        """Read one already-validated canonical event URL without widening the origin."""
        if not source.allows_url(event_url) or _event_reference(event_url) is None:
            # This should be unreachable because only city JSON-LD candidates enter this method.
            # Keep it best-effort so one corrupt in-memory value still cannot discard the snapshot.
            raise _MeetupDetailFetchError("identity_mismatch")
        try:
            await self._wait_for_host_slot(event_url, source.min_interval_ms)
            async with client.stream(
                "GET",
                event_url,
                follow_redirects=False,
            ) as response:
                if response.is_redirect or str(response.url) != event_url:
                    raise _MeetupDetailFetchError("redirect_refused")
                try:
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise _MeetupDetailFetchError("http_error") from exc
                media_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                )
                if media_type != "text/html":
                    raise _MeetupDetailFetchError("invalid_content_type")
                content_length = _declared_detail_content_length(response)
                if content_length is not None and content_length > _MAX_RESPONSE_BYTES:
                    raise _MeetupDetailFetchError("response_too_large")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise _MeetupDetailFetchError("response_too_large")
        except _MeetupDetailFetchError:
            raise
        except httpx.HTTPError as exc:
            raise _MeetupDetailFetchError("request_failed") from exc
        try:
            return bytes(content).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _MeetupDetailFetchError("invalid_utf8") from exc

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host pacing floor before every city/detail GET."""
        host = urlsplit(url).netloc.casefold()
        if not host:
            raise ValueError("Meetup city URL must include a host")
        interval_s = min_interval_ms / 1_000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _profile_for_source(source: CatalogSource) -> _MeetupCityProfile:
    """Require the exact reviewed city identity and 41-request-unit enrichment cap."""
    if source.mode is not CatalogSourceMode.MEETUP_CITY_JSONLD:
        raise ValueError(f"unsupported Meetup city source mode: {source.mode.value}")
    if not source.handoff_only:
        raise ValueError("Meetup city sources must remain handoff-only")
    if source.page_limit != _REVIEWED_REQUEST_UNIT_CAP:
        raise ValueError("Meetup city sources must retain their reviewed 41-request-unit cap")
    profile = _PROFILES.get(source.source_key)
    if (
        profile is None
        or source.seed_url != profile.city_url
        or source.approved_origins != ("https://www.meetup.com",)
        or not _is_city_url(source.seed_url, profile)
    ):
        raise ValueError("Meetup city source must use its exact reviewed public page")
    return profile


def _validate_response_envelope(
    response: httpx.Response,
    source: CatalogSource,
    profile: _MeetupCityProfile,
) -> None:
    """Reject redirects, endpoint drift, non-HTML bodies, and declared oversized bodies."""
    if (
        response.is_redirect
        or not source.allows_url(str(response.url))
        or not _is_city_url(str(response.url), profile)
    ):
        raise MeetupCityFetchError("Meetup city request left its reviewed endpoint")
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MeetupCityFetchError(
            f"Meetup city source {source.source_key} request failed: {exc}"
        ) from exc
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if media_type != "text/html":
        raise MeetupCityFetchError("Meetup city response was not HTML")
    content_length = _declared_content_length(response)
    if content_length is not None and content_length > _MAX_RESPONSE_BYTES:
        raise MeetupCityFetchError(
            f"Meetup city source {source.source_key} exceeded its response-size limit"
        )


async def _read_capped_city_response(
    response: httpx.Response,
    source_key: str,
) -> bytearray:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > _MAX_RESPONSE_BYTES:
            raise MeetupCityFetchError(
                f"Meetup city source {source_key} exceeded its response-size limit"
            )
    return content


def _declared_content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise MeetupCityFetchError(
            "Meetup city response declared an invalid content length"
        ) from exc
    if parsed < 0:
        raise MeetupCityFetchError("Meetup city response declared an invalid content length")
    return parsed


def _declared_detail_content_length(response: httpx.Response) -> int | None:
    """Parse a detail Content-Length without leaking an arbitrary header into evidence."""
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise _MeetupDetailFetchError("response_too_large") from exc
    if parsed < 0:
        raise _MeetupDetailFetchError("response_too_large")
    return parsed


def _is_city_url(value: str, profile: _MeetupCityProfile) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        expected = urlsplit(profile.city_url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.meetup.com"
        and port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == expected.path
        and not parsed.query
        and not parsed.fragment
    )


def _candidates_from_html(
    html: str,
    *,
    source_key: str,
    now: datetime,
) -> list[CandidateEvent]:
    """Parse only root Event JSON-LD blocks; application/member JSON is never traversed."""
    document = HTMLParser(html)
    scripts = document.css('script[type="application/ld+json"]')
    if not scripts:
        raise MeetupCityFetchError(
            f"Meetup city source {source_key} returned no public JSON-LD blocks"
        )
    candidates_by_url: dict[str, CandidateEvent] = {}
    for script_number, script in enumerate(scripts, start=1):
        raw = script.text(deep=True, strip=False)
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise MeetupCityFetchError(
                f"Meetup city source {source_key} returned malformed JSON-LD block {script_number}"
            ) from exc
        for event in _root_event_objects(payload):
            candidate = _candidate_from_event(event)
            if candidate is None or candidate.start_at < now:
                continue
            existing = candidates_by_url.get(candidate.registration_url)
            if existing is None:
                candidates_by_url[candidate.registration_url] = candidate
                continue
            if (existing.title, existing.start_at) != (candidate.title, candidate.start_at):
                raise MeetupCityFetchError(
                    f"Meetup city source {source_key} conflicted on an event URL"
                )
            if _metadata_score(candidate) > _metadata_score(existing):
                candidates_by_url[candidate.registration_url] = candidate
    return sorted(
        candidates_by_url.values(),
        key=lambda candidate: (
            candidate.start_at,
            candidate.registration_url,
            candidate.source_event_id,
            candidate.title,
        ),
    )


def _root_event_objects(payload: object) -> Iterable[dict[str, Any]]:
    """Yield only root/list/@graph Event objects, never arbitrary application JSON."""
    objects: list[object]
    if isinstance(payload, list):
        objects = payload
    elif isinstance(payload, dict):
        graph = payload.get("@graph")
        objects = graph if isinstance(graph, list) else [payload]
    else:
        return ()
    return (
        item for item in objects if isinstance(item, dict) and _has_event_type(item.get("@type"))
    )


def _has_event_type(value: object) -> bool:
    if isinstance(value, str):
        return value == "Event" or value.endswith("/Event")
    if isinstance(value, list):
        return any(_has_event_type(item) for item in value)
    return False


def _candidate_from_event(event: dict[str, Any]) -> CandidateEvent | None:
    """Normalize a single public Event object without fetching its detail page."""
    title = _text(event.get("name"), _MAX_TITLE_CHARS)
    event_reference = _event_reference(event.get("url"))
    start_at = _timestamp(event.get("startDate"))
    event_status = _schema_term(event.get("eventStatus"))
    if (
        title is None
        or event_reference is None
        or start_at is None
        or event_status in _CANCELLED_STATUSES
    ):
        return None
    event_id, event_url = event_reference
    end_at = _timestamp(event.get("endDate"))
    if end_at is not None and end_at <= start_at:
        end_at = None
    location = _location(event.get("location"))
    organizer_name, organizer_url = _organizer(event.get("organizer"))
    description = _text(event.get("description"), _MAX_DESCRIPTION_CHARS) or ""
    image_url = _image_url(event.get("image"), event_url)
    offer = _offer(event)
    raw = _safe_evidence(
        event_id=event_id,
        event_url=event_url,
        event_status=event_status,
        attendance_mode=_schema_term(event.get("eventAttendanceMode")),
        image_url=image_url,
        organizer_url=organizer_url,
        keywords=_topic_labels(event.get("keywords")),
        location=location,
    )
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"meetup:{event_id}",
        title=title,
        start_at=start_at,
        registration_url=event_url,
        end_at=end_at,
        venue_name=location.venue_name,
        geo=location.geo,
        city=location.city,
        description=description,
        price_status=offer.status,
        price_min_cents=offer.min_cents,
        price_max_cents=offer.max_cents,
        price_currency=offer.currency,
        organizer_name=organizer_name,
        registration_status=offer.registration_status,
        raw=raw,
    )


def _enrich_from_detail(  # noqa: PLR0912, PLR0915 -- precedence is field-explicit by design
    candidate: CandidateEvent,
    html: str,
) -> CandidateEvent:
    """Trust detail metadata only after its root Event identity matches the city record."""
    document = HTMLParser(html)
    event, identity_status = _identity_matched_detail_event(document, candidate)
    if event is None:
        return _with_detail_status(candidate, identity_status)
    detail_candidate = _candidate_from_event(event)
    if detail_candidate is None:
        return _with_detail_status(candidate, "invalid_event_jsonld")

    visible = _visible_details(document)
    evidence_codes = {"identity:root_event_jsonld", *visible.evidence_codes}
    field_count = 0

    # Detail JSON-LD is frequently a short teaser while the visible, server-rendered Details
    # section contains the complete public description. Identity validation occurs before this
    # comparison, so selecting the longest bounded text improves completeness without trusting
    # application state. Structured text wins deterministic ties.
    description_options = (
        (len(candidate.description), 3, candidate.description, None),
        (
            len(detail_candidate.description),
            2,
            detail_candidate.description,
            "description:detail_event_jsonld",
        ),
        (
            len(visible.description),
            1,
            visible.description,
            "description:visible_details",
        ),
    )
    _, _, description, description_evidence = max(description_options)
    if description != candidate.description:
        if description_evidence is not None:
            evidence_codes.add(description_evidence)
        field_count += 1

    offer = _offer_from_candidate(candidate)
    if offer.status is PriceStatus.UNKNOWN:
        detail_offer = _offer_from_candidate(detail_candidate)
        if detail_offer.status is not PriceStatus.UNKNOWN:
            offer = detail_offer
            evidence_codes.add("price:detail_event_jsonld")
            field_count += 1
        elif visible.offer.status is not PriceStatus.UNKNOWN:
            offer = visible.offer
            evidence_codes.add(
                "price:visible_details_exact_free"
                if offer.status is PriceStatus.FREE
                else "price:visible_details_exact_usd"
            )
            field_count += 1

    registration_status = candidate.registration_status
    if (
        registration_status is RegistrationStatus.UNKNOWN
        and detail_candidate.registration_status is not RegistrationStatus.UNKNOWN
    ):
        registration_status = detail_candidate.registration_status
        evidence_codes.add("registration:detail_event_jsonld")
        field_count += 1

    venue_name = candidate.venue_name
    city = candidate.city
    geo = candidate.geo
    if venue_name is None and detail_candidate.venue_name is not None:
        venue_name = detail_candidate.venue_name
        evidence_codes.add("location:detail_event_jsonld")
        field_count += 1
    if city is None and detail_candidate.city is not None:
        city = detail_candidate.city
        evidence_codes.add("location:detail_event_jsonld")
        field_count += 1
    if geo is None and detail_candidate.geo is not None:
        geo = detail_candidate.geo
        evidence_codes.add("location:detail_event_jsonld")
        field_count += 1

    raw = dict(candidate.raw)
    detail_raw = detail_candidate.raw
    for key in ("street_address", "address_region", "address_country", "image_url"):
        if key not in raw and key in detail_raw:
            raw[key] = detail_raw[key]
            evidence_codes.add("location:detail_event_jsonld")
            field_count += 1

    visible_where = visible.where
    if visible_where is not None:
        if venue_name is None and visible_where.venue_name is not None:
            venue_name = visible_where.venue_name
            evidence_codes.add("location:visible_details_where")
            field_count += 1
        if city is None and visible_where.city is not None:
            city = visible_where.city
            evidence_codes.add("location:visible_details_where")
            field_count += 1
        where_values = {
            "street_address": visible_where.street_address,
            "address_region": visible_where.address_region,
            "address_country": visible_where.address_country,
        }
        for key, value in where_values.items():
            if key not in raw:
                raw[key] = value
                evidence_codes.add("location:visible_details_where")
                field_count += 1

    organizer_name = candidate.organizer_name
    if organizer_name is None and detail_candidate.organizer_name is not None:
        organizer_name = detail_candidate.organizer_name
        evidence_codes.add("organizer:detail_event_jsonld")
        field_count += 1

    host_names = tuple(
        dict.fromkeys((*candidate.host_names, *_visible_host_names(document, candidate)))
    )
    if host_names != candidate.host_names:
        evidence_codes.add("host:visible_hosted_by")
        field_count += len(host_names) - len(candidate.host_names)

    end_at = candidate.end_at
    if end_at is None and detail_candidate.end_at is not None:
        end_at = detail_candidate.end_at
        evidence_codes.add("schedule:detail_event_jsonld")
        field_count += 1

    raw["detail_enrichment_status"] = "enriched" if field_count else "validated_no_new_evidence"
    raw["detail_evidence_codes"] = sorted(evidence_codes)
    raw["detail_request_attempted"] = 1
    raw["detail_request_succeeded"] = 1
    raw["detail_identity_matched"] = 1
    raw["detail_details_section_found"] = int("visible_details:bounded_section" in evidence_codes)
    raw["detail_applied_field_count"] = field_count
    return replace(
        candidate,
        end_at=end_at,
        venue_name=venue_name,
        geo=geo,
        city=city,
        description=description,
        is_free=offer.status.is_free,
        price_status=offer.status,
        price_min_cents=offer.min_cents,
        price_max_cents=offer.max_cents,
        price_currency=offer.currency,
        organizer_name=organizer_name,
        host_names=host_names,
        registration_status=registration_status,
        raw=raw,
    )


def _with_detail_status(candidate: CandidateEvent, status: str) -> CandidateEvent:
    """Attach only a closed best-effort outcome; never retain an arbitrary provider error."""
    if status not in _DETAIL_STATUS_CODES:
        raise ValueError("unknown Meetup detail status code")
    raw = dict(candidate.raw)
    raw["detail_enrichment_status"] = status
    raw["detail_request_attempted"] = int(status != "request_cap_not_fetched")
    raw["detail_request_succeeded"] = 0
    raw["detail_identity_matched"] = 0
    raw["detail_details_section_found"] = 0
    raw["detail_applied_field_count"] = 0
    return replace(candidate, raw=raw)


def _identity_matched_detail_event(
    document: HTMLParser,
    candidate: CandidateEvent,
) -> tuple[dict[str, Any] | None, str]:
    """Find an exact root Event match without traversing application/member JSON."""
    scripts = document.css('script[type="application/ld+json"]')
    saw_malformed = False
    saw_event = False
    matches: list[tuple[tuple[int, ...], dict[str, Any]]] = []
    for script in scripts:
        raw = script.text(deep=True, strip=False)
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            saw_malformed = True
            continue
        for event in _root_event_objects(payload):
            saw_event = True
            if not _detail_identity_matches(event, candidate):
                continue
            normalized = _candidate_from_event(event)
            if normalized is None:
                continue
            matches.append((_metadata_score(normalized), event))
    if matches:
        return max(matches, key=lambda item: item[0])[1], "matched"
    if saw_event:
        return None, "identity_mismatch"
    if saw_malformed:
        return None, "malformed_event_jsonld"
    return None, "missing_event_jsonld"


def _detail_identity_matches(event: dict[str, Any], candidate: CandidateEvent) -> bool:
    source_prefix = "meetup:"
    if not candidate.source_event_id.startswith(source_prefix):
        return False
    reference = _event_reference(event.get("url"))
    title = _text(event.get("name"), _MAX_TITLE_CHARS)
    start_at = _timestamp(event.get("startDate"))
    return (
        reference
        == (
            candidate.source_event_id.removeprefix(source_prefix),
            candidate.registration_url,
        )
        and title == candidate.title
        and start_at == candidate.start_at
    )


def _offer_from_candidate(candidate: CandidateEvent) -> _Offer:
    return _Offer(
        candidate.price_status,
        candidate.price_min_cents,
        candidate.price_max_cents,
        candidate.price_currency,
        candidate.registration_status,
    )


def _visible_details(document: HTMLParser) -> _VisibleDetails:
    """Read only bounded visible p/li text in the exact server-rendered Details section."""
    lines: list[str] = []
    root = document.root
    if root is None:
        return _empty_visible_details()
    nodes = list(root.traverse(include_text=False))
    heading_index: int | None = None
    heading_level = 6
    for index, node in enumerate(nodes):
        if (
            node.tag in _HEADING_TAGS
            and _node_is_visible(node)
            and _node_text(node, _MAX_VISIBLE_DETAILS_NODE_CHARS) == "Details"
        ):
            heading_index = index
            heading_level = int(node.tag[1])
            break
    if heading_index is None:
        return _empty_visible_details()

    for node in nodes[heading_index + 1 :]:
        if node.tag in _HEADING_TAGS and _node_is_visible(node):
            if int(node.tag[1]) <= heading_level:
                break
            continue
        if node.tag not in {"p", "li"} or not _node_is_visible(node):
            continue
        # A list item containing paragraphs would otherwise repeat their text.
        if node.tag == "li" and node.css_first("p") is not None:
            continue
        text = _node_text(node, _MAX_VISIBLE_DETAILS_NODE_CHARS)
        if text is None:
            continue
        lines.append(text)
        if len(lines) >= _MAX_VISIBLE_DETAILS_NODES:
            break

    offer, price_codes = _visible_offer(lines)
    where = _visible_where(lines)
    description_lines = [
        line
        for line in lines
        if _STRONG_FREE_LINE.fullmatch(line) is None
        and _STRONG_USD_LINE.fullmatch(line) is None
        and _WHERE_LINE.fullmatch(line) is None
    ]
    description = "\n\n".join(description_lines)[:_MAX_DESCRIPTION_CHARS].rstrip()
    evidence_codes = {"visible_details:bounded_section", *price_codes}
    if where is not None:
        evidence_codes.add("visible_details:where_us_address")
    return _VisibleDetails(
        description=description,
        offer=offer,
        where=where,
        evidence_codes=tuple(sorted(evidence_codes)),
    )


def _empty_visible_details() -> _VisibleDetails:
    return _VisibleDetails(
        description="",
        offer=_Offer(
            PriceStatus.UNKNOWN,
            None,
            None,
            None,
            RegistrationStatus.UNKNOWN,
        ),
        where=None,
        evidence_codes=(),
    )


def _visible_host_names(document: HTMLParser, candidate: CandidateEvent) -> tuple[str, ...]:
    """Read only the event header's explicit host assertion, never attendee cards."""
    expected_href = f"{candidate.registration_url}attendees/"
    names: list[str] = []
    for link in document.css('a[data-event-label="hosted-by"]')[:_MAX_VISIBLE_HOSTS]:
        if not _node_is_visible(link):
            continue
        href = _text(link.attributes.get("href"), _MAX_EVENT_URL_CHARS)
        if href is None or urljoin(candidate.registration_url, href) != expected_href:
            continue
        aria_label = _text(link.attributes.get("aria-label"), _MAX_ORGANIZER_NAME_CHARS + 16)
        label_match = _HOSTED_BY_LABEL.fullmatch(aria_label or "")
        if label_match is None:
            continue
        displayed_name = _text(label_match.group("name"), _MAX_ORGANIZER_NAME_CHARS)
        if displayed_name is None:
            continue

        full_names: list[str] = []
        for image in link.css("img"):
            alt = _text(image.attributes.get("alt"), _MAX_ORGANIZER_NAME_CHARS + 24)
            image_match = _HOST_IMAGE_ALT.fullmatch(alt or "")
            if image_match is None:
                continue
            full_name = _text(image_match.group("name"), _MAX_ORGANIZER_NAME_CHARS)
            if full_name is not None and _host_name_matches(displayed_name, full_name):
                full_names.append(full_name)
        for name in full_names or [displayed_name]:
            if name not in names:
                names.append(name)
    return tuple(names)


def _host_name_matches(displayed_name: str, full_name: str) -> bool:
    """Accept an exact public name or Meetup's first-name/last-initial abbreviation."""
    if displayed_name.casefold() == full_name.casefold():
        return True
    displayed = displayed_name.split()
    full = full_name.split()
    return (
        len(displayed) == _ABBREVIATED_HOST_PARTS
        and len(full) >= _ABBREVIATED_HOST_PARTS
        and displayed[0].casefold() == full[0].casefold()
        and len(displayed[1]) == _INITIAL_WITH_PERIOD_CHARS
        and displayed[1].endswith(".")
        and displayed[1][0].casefold() == full[-1][0].casefold()
    )


def _visible_offer(lines: list[str]) -> tuple[_Offer, tuple[str, ...]]:
    offers: list[_Offer] = []
    codes: set[str] = set()
    for line in lines:
        if _STRONG_FREE_LINE.fullmatch(line) is not None:
            offers.append(
                _Offer(
                    PriceStatus.FREE,
                    None,
                    None,
                    None,
                    RegistrationStatus.UNKNOWN,
                )
            )
            codes.add("visible_details:exact_free")
            continue
        paid = _STRONG_USD_LINE.fullmatch(line)
        if paid is None:
            continue
        cents = _price_cents(paid.group("amount"))
        if cents is not None and cents > 0:
            offers.append(
                _Offer(
                    PriceStatus.PAID,
                    cents,
                    cents,
                    "USD",
                    RegistrationStatus.UNKNOWN,
                )
            )
            codes.add("visible_details:exact_usd")
    if not offers:
        return _empty_visible_details().offer, ()
    identities = {
        (offer.status, offer.min_cents, offer.max_cents, offer.currency) for offer in offers
    }
    if len(identities) != 1:
        return _empty_visible_details().offer, ()
    return offers[0], tuple(sorted(codes))


def _visible_where(lines: list[str]) -> _VisibleWhere | None:
    parsed: list[_VisibleWhere] = []
    for line in lines:
        match = _WHERE_LINE.fullmatch(line)
        if match is None:
            continue
        where = _parse_visible_where(match.group("value"))
        if where is not None:
            parsed.append(where)
    if not parsed or any(item != parsed[0] for item in parsed[1:]):
        return None
    return parsed[0]


def _parse_visible_where(value: str) -> _VisibleWhere | None:
    parts = [part.strip() for part in value.split(",")]
    if any(not part for part in parts):
        return None
    street_indexes = [index for index, part in enumerate(parts) if _STREET_ADDRESS.fullmatch(part)]
    if len(street_indexes) != 1:
        return None
    street_index = street_indexes[0]
    if street_index + 2 >= len(parts) or len(parts) > street_index + 4:
        return None
    street_address = _text(parts[street_index], _MAX_ADDRESS_CHARS)
    city = _text(parts[street_index + 1], _MAX_ADDRESS_CHARS)
    region = _REGION_AND_POSTAL.fullmatch(parts[street_index + 2])
    if street_address is None or city is None or region is None:
        return None
    if len(parts) == street_index + 4 and parts[-1].casefold() not in {
        "us",
        "usa",
        "united states",
    }:
        return None
    venue = _text(", ".join(parts[:street_index]), _MAX_VENUE_CHARS)
    return _VisibleWhere(
        venue_name=venue,
        city=city,
        street_address=street_address,
        address_region=region.group("region").upper(),
        address_country="US",
    )


def _node_text(node: Any, max_chars: int) -> str | None:
    return _text(node.text(separator=" ", strip=True), max_chars)


def _node_is_visible(node: Any) -> bool:
    current = node
    while current is not None:
        if current.tag in {"script", "style", "template", "noscript"}:
            return False
        attributes = current.attributes
        aria_hidden = attributes.get("aria-hidden") or ""
        if "hidden" in attributes or aria_hidden.casefold() == "true":
            return False
        # HTML permits valueless attributes (for example ``class`` instead of
        # ``class="..."``). Selectolax represents those values as ``None``;
        # presentation-only markup must not abort the bounded source refresh.
        classes = set((attributes.get("class") or "").casefold().split())
        if classes.intersection({"hidden", "sr-only", "visually-hidden"}):
            return False
        style = "".join((attributes.get("style") or "").casefold().split())
        if _HIDDEN_STYLE.search(style) is not None:
            return False
        current = current.parent
    return True


def _event_reference(value: object) -> tuple[str, str] | None:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_EVENT_URL_CHARS
        or _has_control_character(value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    match = _EVENT_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.meetup.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        return None
    return match.group("event_id"), value


def _organizer(value: object) -> tuple[str | None, str | None]:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        return None, None
    name = _text(value.get("name"), _MAX_ORGANIZER_NAME_CHARS)
    url = _group_url(value.get("url"))
    return name, url


def _group_url(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_ORGANIZER_URL_CHARS
        or _has_control_character(value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.meetup.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _GROUP_PATH.fullmatch(parsed.path) is None
    ):
        return None
    return value


def _location(value: object) -> _Location:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        return _Location(None, None, None, None, None, None)
    address = value.get("address")
    address_object = address if isinstance(address, dict) else {}
    street_address = (
        _text(address, _MAX_ADDRESS_CHARS)
        if isinstance(address, str)
        else _text(address_object.get("streetAddress"), _MAX_ADDRESS_CHARS)
    )
    return _Location(
        venue_name=_text(value.get("name"), _MAX_VENUE_CHARS),
        city=_text(address_object.get("addressLocality"), _MAX_ADDRESS_CHARS),
        geo=_geo(value.get("geo")),
        street_address=street_address,
        address_region=_text(address_object.get("addressRegion"), _MAX_ADDRESS_CHARS),
        address_country=_text(address_object.get("addressCountry"), _MAX_ADDRESS_CHARS),
    )


def _geo(value: object) -> GeoPoint | None:
    if not isinstance(value, dict):
        return None
    lat = _finite_float(value.get("latitude"))
    lon = _finite_float(value.get("longitude"))
    if (
        lat is None
        or lon is None
        or not -_LATITUDE_LIMIT <= lat <= _LATITUDE_LIMIT
        or not -_LONGITUDE_LIMIT <= lon <= _LONGITUDE_LIMIT
    ):
        return None
    return GeoPoint(lat=lat, lon=lon)


def _offer(event: dict[str, Any]) -> _Offer:
    values = event.get("offers")
    offer_objects = (
        [item for item in values if isinstance(item, dict)]
        if isinstance(values, list)
        else [values]
        if isinstance(values, dict)
        else []
    )
    parsed: list[_Offer] = []
    for offer_object in offer_objects:
        item = _single_offer(offer_object)
        if item is not None:
            parsed.append(item)
    accessible = event.get("isAccessibleForFree")
    explicit_statuses = {item.status for item in parsed}
    if isinstance(accessible, bool):
        explicit_statuses.add(PriceStatus.FREE if accessible else PriceStatus.PAID)
    status = next(iter(explicit_statuses)) if len(explicit_statuses) == 1 else PriceStatus.UNKNOWN
    registration_status = _aggregate_registration_status(parsed)
    if status is not PriceStatus.PAID:
        return _Offer(status, None, None, None, registration_status)
    paid = [item for item in parsed if item.status is PriceStatus.PAID]
    if not paid or any(item.currency is None for item in paid):
        return _Offer(status, None, None, None, registration_status)
    currencies = {item.currency for item in paid}
    if len(currencies) != 1 or any(
        item.min_cents is None or item.max_cents is None for item in paid
    ):
        return _Offer(status, None, None, None, registration_status)
    min_cents = min(item.min_cents for item in paid if item.min_cents is not None)
    max_cents = max(item.max_cents for item in paid if item.max_cents is not None)
    return _Offer(status, min_cents, max_cents, next(iter(currencies)), registration_status)


def _single_offer(value: dict[str, Any]) -> _Offer | None:
    low = _price_cents(value.get("lowPrice"))
    high = _price_cents(value.get("highPrice"))
    price = _price_cents(value.get("price"))
    if price is not None:
        low = high = price
    elif low is None and high is not None:
        low = high
    elif high is None and low is not None:
        high = low
    availability = _registration_status(value.get("availability"))
    if low is None or high is None or high < low:
        return None
    status = PriceStatus.FREE if high == 0 else PriceStatus.PAID
    currency = _currency(value.get("priceCurrency")) if status is PriceStatus.PAID else None
    return _Offer(status, low, high, currency, availability)


def _aggregate_registration_status(offers: list[_Offer]) -> RegistrationStatus:
    statuses = {offer.registration_status for offer in offers}
    statuses.discard(RegistrationStatus.UNKNOWN)
    if RegistrationStatus.OPEN in statuses:
        return RegistrationStatus.OPEN
    if RegistrationStatus.WAITLIST in statuses:
        return RegistrationStatus.WAITLIST
    if statuses == {RegistrationStatus.SOLD_OUT}:
        return RegistrationStatus.SOLD_OUT
    return RegistrationStatus.UNKNOWN


def _registration_status(value: object) -> RegistrationStatus:
    term = _schema_term(value)
    if term in {"instock", "onlineonly", "preorder"}:
        return RegistrationStatus.OPEN
    if term in {"waitlist", "limitedavailability"}:
        return RegistrationStatus.WAITLIST
    if term in {"soldout", "outofstock", "discontinued"}:
        return RegistrationStatus.SOLD_OUT
    return RegistrationStatus.UNKNOWN


def _price_cents(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    cents = amount * 100
    integral = cents.to_integral_value()
    if cents != integral or integral > MAX_PUBLIC_PRICE_CENTS:
        return None
    return int(integral)


def _currency(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    currency = value.strip().upper()
    return (
        currency
        if len(currency) == _CURRENCY_CODE_LENGTH and currency.isascii() and currency.isalpha()
        else None
    )


def _image_url(value: object, base_url: str) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("contentUrl") or value.get("url")
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_IMAGE_URL_CHARS
        or _has_control_character(value)
    ):
        return None
    resolved = urljoin(base_url, value.strip())
    try:
        parsed = urlsplit(resolved)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _IMAGE_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return resolved


def _safe_evidence(
    *,
    event_id: str,
    event_url: str,
    event_status: str | None,
    attendance_mode: str | None,
    image_url: str | None,
    organizer_url: str | None,
    keywords: tuple[str, ...],
    location: _Location,
) -> dict[str, object]:
    values: dict[str, object | None] = {
        "event_id": event_id,
        "event_url": event_url,
        "event_status": event_status,
        "attendance_mode": attendance_mode,
        "image_url": image_url,
        "organizer_url": organizer_url,
        "keywords": list(keywords) if keywords else None,
        "street_address": location.street_address,
        "address_region": location.address_region,
        "address_country": location.address_country,
    }
    return {key: value for key, value in values.items() if value is not None}


def _topic_labels(value: object) -> tuple[str, ...]:
    """Retain only short, explicit public JSON-LD keywords for bounded normalization."""
    candidates = value if isinstance(value, list) else [value]
    labels: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        for part in re.split(r"[,;|]", candidate):
            label = " ".join(part.split())
            if (
                label
                and len(label) <= _MAX_TOPIC_LABEL_CHARS
                and not _has_control_character(label)
                and label not in labels
            ):
                labels.append(label)
                if len(labels) >= _MAX_TOPIC_LABELS:
                    return tuple(labels)
    return tuple(labels)


def _metadata_score(candidate: CandidateEvent) -> tuple[int, ...]:
    return (
        len(candidate.description),
        int(candidate.end_at is not None),
        int(candidate.venue_name is not None),
        int(candidate.city is not None),
        int(candidate.geo is not None),
        int(candidate.organizer_name is not None),
        int(candidate.price_status is not PriceStatus.UNKNOWN),
        len(candidate.raw),
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or _has_control_character(value):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _schema_term(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().rstrip("/").rsplit("/", 1)[-1].casefold()
    return normalized or None


def _text(value: object, max_chars: int) -> str | None:
    if not isinstance(value, str) or _has_forbidden_text_character(value):
        return None
    normalized = " ".join(value.split())
    return normalized[:max_chars].rstrip() or None


def _has_control_character(value: str) -> bool:
    return any(
        ord(character) < _MIN_PRINTABLE_CODEPOINT or ord(character) == _DELETE_CODEPOINT
        for character in value
    )


def _has_forbidden_text_character(value: str) -> bool:
    return any(
        (ord(character) < _MIN_PRINTABLE_CODEPOINT and character not in {"\t", "\n", "\r"})
        or ord(character) == _DELETE_CODEPOINT
        for character in value
    )


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None
