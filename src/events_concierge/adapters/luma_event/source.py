"""Read basic details from an exact admitted Luma link, without enumerating private IDs.

An owner-reviewed seed or an anchor on a reviewed public directory is discoverability evidence.
This is separate from Luma's calendar API, whose unlisted/private records remain excluded.
Withheld locations and guest, form, payment and staff data are never normalized.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from ...application.ingestion_telemetry import record_ingestion_collection_progress
from ...domain.catalog_sources import DIRECTORY_FETCH_TIMEOUT_SECONDS, CatalogSource
from ...domain.catalog_window import collection_reference_time, in_collection_window
from ...domain.enums import CatalogSourceMode
from ...domain.events import CandidateEvent
from ..luma_common import json_object, normalize_luma_entry
from ..luma_detail import enrichment_from_detail, resolved_price

_MAX_DOCUMENT_BYTES = 5_000_000
_MIN_INTERVAL_MS = 1500
_SLUG_PATH = re.compile(r"/[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")


class LumaEventFetchError(RuntimeError):
    """The admitted anonymous event link no longer matches its reviewed contract."""


def is_luma_event_link(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "luma.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and _SLUG_PATH.fullmatch(parsed.path) is not None
        )
    except ValueError:
        return False


class LumaEventCatalogFetcher:
    """One anonymous HTML GET to an exact reviewed event seed; no API or login fallback."""

    def __init__(
        self,
        *,
        user_agent: str,
        now: Callable[[], datetime] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._now = now or (lambda: datetime.now(UTC))
        self._transport = transport

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        if (
            source.mode is not CatalogSourceMode.LUMA_EVENT_HTML
            or not is_luma_event_link(source.seed_url)
            or source.approved_origins != ("https://luma.com",)
            or source.page_limit != 1
            or not source.handoff_only
            or source.min_interval_ms < _MIN_INTERVAL_MS
        ):
            raise LumaEventFetchError("Luma event source must use an exact reviewed page profile")
        async with httpx.AsyncClient(
            headers={"User-Agent": self._user_agent},
            follow_redirects=False,
            timeout=DIRECTORY_FETCH_TIMEOUT_SECONDS,
            transport=self._transport,
        ) as client:
            try:
                async with asyncio.timeout(DIRECTORY_FETCH_TIMEOUT_SECONDS):
                    async with client.stream("GET", source.seed_url) as response:
                        if response.is_redirect or str(response.url) != source.seed_url:
                            raise LumaEventFetchError("Luma event redirect is not admitted")
                        response.raise_for_status()
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > _MAX_DOCUMENT_BYTES:
                                raise LumaEventFetchError("Luma event page exceeded its size limit")
                document = HTMLParser(bytes(content).decode("utf-8"))
            except (httpx.HTTPError, UnicodeDecodeError, TimeoutError) as exc:
                raise LumaEventFetchError("Luma anonymous event page is unavailable") from exc
        candidate = linked_luma_event(document, source.seed_url, source.source_key)
        now = collection_reference_time(source, self._now())
        events = (
            [candidate]
            if candidate.start_at >= now and in_collection_window(source, candidate)
            else []
        )
        await record_ingestion_collection_progress(
            source_key=source.source_key,
            request_completed=True,
            page_completed=True,
            candidate_count=len(events),
        )
        return events


def linked_luma_event(document: HTMLParser, url: str, source_key: str) -> CandidateEvent:
    """Normalize only a supplied/publicly linked page, never an enumerated calendar record."""
    if not is_luma_event_link(url):
        raise LumaEventFetchError("Luma event link is not admitted")
    nodes = document.css("script#__NEXT_DATA__")
    if len(nodes) != 1:
        raise LumaEventFetchError("Luma event page has no unique event payload")
    try:
        payload = json.loads(nodes[0].text())["props"]["pageProps"]["initialData"]
        if payload["kind"] != "event":
            raise LumaEventFetchError("Luma page is not an anonymous event")
        data = payload["data"]
        event, calendar = data["event"], data["calendar"]
        if (
            url != f"https://luma.com/{event['url']}"
            or calendar["api_id"] != event["calendar_api_id"]
            or data.get("api_id") != event["api_id"]
            or event.get("visibility") not in {"public", "unlisted", "private"}
        ):
            raise LumaEventFetchError("Luma event page changed its linked identity")
        visibility = event["visibility"]
        address = json_object(event.get("geo_address_info"))
        # Hidden addresses can still have coordinates embedded in page state. Discard both
        # before invoking any shared normalizer, including for otherwise public events.
        withheld = visibility != "public" or event.get("geo_address_visibility") != "public"
        safe_event = {
            key: event.get(key)
            for key in ("api_id", "calendar_api_id", "url", "name", "start_at", "end_at")
        }
        safe_event["geo_address_info"] = (
            {"city": address.get("city")} if withheld and address is not None else address
        )
        safe_event["coordinate"] = None if withheld else event.get("coordinate")
        safe_calendar = {key: calendar.get(key) for key in ("api_id", "name", "website")}
        entry = {
            "event": safe_event,
            "calendar": safe_calendar,
            "calendar_api_id": calendar["api_id"],
            "ticket_info": data.get("ticket_info"),
        }
        candidate = normalize_luma_entry(
            entry, source_key, contract="calendar", listed_calendar_api_id=calendar["api_id"]
        )
        if visibility != "public":
            # Basic public card metadata only. Guest/host lists, detailed prose, sessions,
            # forms and hidden locations do not enter the candidate, hash or evidence.
            return replace(candidate, visibility=visibility, location_withheld=withheld)
        enrichment = enrichment_from_detail(
            httpx.Response(200, json=data),
            entry=entry,
            source_key=source_key,
            event_api_id=event["api_id"],
            calendar_api_id=calendar["api_id"],
            slug=event["url"],
            title=event["name"],
            detail_number=1,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LumaEventFetchError("Luma event page returned invalid linked metadata") from exc
    price, minimum, maximum, currency = resolved_price(candidate, enrichment)
    return replace(
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
        price_status=price,
        is_free=price.is_free,
        price_min_cents=minimum,
        price_max_cents=maximum,
        price_currency=currency,
        visibility=visibility,
        location_withheld=withheld,
    )
