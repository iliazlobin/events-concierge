"""Collect LangChain's listed Luma and Goldcast events without executing page scripts.

The directory renders its complete CMS list, including cards hidden by client pagination.
Its display dates lack offsets, so the linked registration page supplies authoritative instants.
Only reviewed registration hosts are fetched. Conference links on other hosts remain handoffs
outside this collector's coverage. No staff, attendee, registration-form or tracking data survives
normalization. Linked private/unlisted Luma events retain only basic card metadata and visibility.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from selectolax.parser import HTMLParser, Node

from ...application.ingestion_telemetry import record_ingestion_collection_progress
from ...domain.catalog_sources import (
    DIRECTORY_FETCH_TIMEOUT_SECONDS,
    MAX_LINKED_DIRECTORY_EVENTS,
    CatalogSource,
)
from ...domain.catalog_window import (
    collection_end_at,
    collection_reference_time,
    in_collection_window,
)
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent, EventEntityProfile
from ..luma_common import json_object
from ..luma_event.source import LumaEventFetchError, linked_luma_event

_MAX_DOCUMENT_BYTES = 5_000_000
_MAX_CARDS = 500
_MIN_INTERVAL_MS = 1500
_MAX_DESCRIPTION_SOURCE_CHARS = 100_000
_MAX_PUBLIC_ROLE_RECORDS = 128
_MAX_PUBLIC_NAMES = 32
_SEED = "https://www.langchain.com/events"
_ORIGINS = (
    "https://www.langchain.com",
    "https://events.langchain.com",
    "https://luma.com",
)
_DETAIL_HOSTS = frozenset({"events.langchain.com", "luma.com"})
_LUMA_SLUG = re.compile(r"/[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_GOLDCAST_ASSIGNMENT = "window.uberdata = "


class LangChainFetchError(ValueError):
    """The reviewed directory or a supported registration record changed its contract."""


class LangChainCatalogFetcher:
    """Fetch one official directory and a bounded set of its public registration records."""

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
        self._last_request: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        if (
            source.mode is not CatalogSourceMode.LANGCHAIN_HTML
            or source.source_key != "langchain-events"
            or source.seed_url != _SEED
            or source.approved_origins != _ORIGINS
            or source.page_limit != 1
            or not source.handoff_only
            or source.min_interval_ms < _MIN_INTERVAL_MS
        ):
            raise LangChainFetchError("LangChain source must use its reviewed directory profile")
        now = collection_reference_time(source, self._now())
        end = collection_end_at(source, now + timedelta(days=source.collection_horizon_days))
        async with httpx.AsyncClient(
            headers={"User-Agent": self._user_agent},
            follow_redirects=False,
            timeout=DIRECTORY_FETCH_TIMEOUT_SECONDS,
            transport=self._transport,
        ) as client:
            directory = await self._document(client, source, _SEED)
            urls = _registration_urls(directory, now.date(), end.date())
            events: dict[str, CandidateEvent] = {}
            for url, city in urls:
                document = await self._document(client, source, url)
                candidate: CandidateEvent | None
                if urlsplit(url).hostname == "luma.com":
                    candidate = _luma_event(document, url, source.source_key)
                else:
                    candidate = _goldcast_event(document, url, city)
                if candidate is None or candidate.start_at < now:
                    continue
                if not in_collection_window(source, candidate):
                    continue
                prior = events.get(candidate.source_event_id)
                if prior is not None and prior != candidate:
                    raise LangChainFetchError(
                        "LangChain directory returned conflicting event identities"
                    )
                events[candidate.source_event_id] = candidate
                await record_ingestion_collection_progress(
                    source_key=source.source_key,
                    candidate_count=len(events),
                )
            await record_ingestion_collection_progress(
                source_key=source.source_key, page_completed=True
            )
            return list(events.values())

    async def _document(
        self, client: httpx.AsyncClient, source: CatalogSource, url: str
    ) -> HTMLParser:
        if not source.allows_url(url) or not _safe_url(url):
            raise LangChainFetchError("LangChain request left the reviewed registration profile")
        host = urlsplit(url).hostname
        assert host is not None
        async with self._lock:
            previous = self._last_request.get(host)
            if previous is not None:
                delay = source.min_interval_ms / 1000 - (self._clock() - previous)
                if delay > 0:
                    await self._sleep(delay)
            self._last_request[host] = self._clock()
        try:
            client.cookies.clear()
            async with asyncio.timeout(DIRECTORY_FETCH_TIMEOUT_SECONDS):
                async with client.stream("GET", url) as response:
                    if response.is_redirect or str(response.url) != url:
                        raise LangChainFetchError("LangChain registration redirect is not admitted")
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_DOCUMENT_BYTES:
                            raise LangChainFetchError("LangChain document exceeded its size limit")
        except (httpx.HTTPError, TimeoutError) as exc:
            raise LangChainFetchError("LangChain public registration request failed") from exc
        try:
            document = HTMLParser(bytes(content).decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise LangChainFetchError("LangChain document is not UTF-8") from exc
        await record_ingestion_collection_progress(
            source_key=source.source_key, request_completed=True
        )
        return document


def _safe_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return False
    except ValueError:
        return False
    if parsed.hostname == "www.langchain.com":
        return url == _SEED
    if parsed.hostname == "luma.com":
        return _LUMA_SLUG.fullmatch(parsed.path) is not None
    return (
        parsed.hostname in _DETAIL_HOSTS
        and parsed.path.startswith("/")
        and parsed.path != "/"
        and not any(p in {".", ".."} for p in parsed.path.split("/"))
        and "%" not in parsed.path
        and "\\" not in parsed.path
    )


def _registration_urls(
    document: HTMLParser, start: date, end: date
) -> list[tuple[str, str | None]]:
    if document.css_first(".v2-events_cms-list") is None:
        raise LangChainFetchError("LangChain directory has no reviewed event list")
    if any(node.attributes.get("href") for node in document.css("a.w-pagination-next")):
        raise LangChainFetchError("LangChain directory requires an unreviewed server page")
    cards = document.css(".events_cms-item")
    if len(cards) > _MAX_CARDS:
        raise LangChainFetchError("LangChain directory exceeded its card limit")
    urls: dict[str, str | None] = {}
    for card in cards:
        try:
            day = date.fromisoformat(card.attributes.get("data-date") or "")
        except ValueError as exc:
            raise LangChainFetchError("LangChain card has no valid event day") from exc
        # Directory days are only a coarse request bound, not an invented event timezone.
        if not start - timedelta(days=1) <= day <= end + timedelta(days=1):
            continue
        link = card.css_first("a.v2-events_element")
        url = (link.attributes.get("href") or "") if link is not None else ""
        if not _public_https_link(url):
            raise LangChainFetchError("LangChain card has an invalid registration link")
        if urlsplit(url).hostname not in _DETAIL_HOSTS:
            continue  # External conference hosts need their own reviewed contract.
        if not _safe_url(url):
            raise LangChainFetchError("LangChain card left its reviewed registration profile")
        city = _node_text(card.css_first(".event-location"))
        urls[url] = None if city.casefold() in {"virtual", "online", ""} else city
    if len(urls) > MAX_LINKED_DIRECTORY_EVENTS:
        raise LangChainFetchError("LangChain directory exceeded its detail request limit")
    return list(urls.items())


def _public_https_link(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
    except ValueError:
        return False


def _node_text(node: Node | None) -> str:
    return node.text(separator=" ", strip=True) if node is not None else ""


def _goldcast_payload(document: HTMLParser) -> dict[str, object]:
    scripts = [
        node.text().strip()
        for node in document.css("script")
        if node.text().strip().startswith(_GOLDCAST_ASSIGNMENT)
    ]
    if len(scripts) != 1:
        raise LangChainFetchError("LangChain registration has no unique public event payload")
    try:
        value, _ = json.JSONDecoder().raw_decode(scripts[0][len(_GOLDCAST_ASSIGNMENT) :])
    except ValueError as exc:
        raise LangChainFetchError("LangChain public event payload is malformed") from exc
    if not isinstance(value, dict):
        raise LangChainFetchError("LangChain public event payload is not an object")
    return value


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise LangChainFetchError("LangChain event has no offset-aware instant")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LangChainFetchError("LangChain event has an invalid instant") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise LangChainFetchError("LangChain event has no offset-aware instant")
    return instant.astimezone(UTC)


def _text(value: object, *, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise LangChainFetchError("LangChain event has invalid public text")
    return value.strip()


def _goldcast_event(document: HTMLParser, url: str, city: str | None) -> CandidateEvent | None:
    payload = _goldcast_payload(document)
    organization = json_object(payload.get("organization"))
    events = payload.get("event")
    if organization is None or organization.get("name") != "LangChain":
        raise LangChainFetchError("LangChain registration belongs to another organization")
    if not isinstance(events, list) or len(events) != 1 or not isinstance(events[0], dict):
        raise LangChainFetchError("LangChain registration has no unique event")
    event = events[0]
    if event.get("deleted") not in {None, False} or event.get("show_registration_page") is not True:
        return None
    try:
        event_id = str(UUID(_text(event.get("id"))))
    except ValueError as exc:
        raise LangChainFetchError("LangChain event identity is invalid") from exc
    start = _instant(event.get("start_time"))
    end = _instant(event.get("end_time")) if event.get("end_time") is not None else None
    if end is not None and end <= start:
        raise LangChainFetchError("LangChain event ends before it starts")
    description = event.get("description", "")
    if not isinstance(description, str) or len(description) > _MAX_DESCRIPTION_SOURCE_CHARS:
        raise LangChainFetchError("LangChain event description is invalid")
    description = HTMLParser(description).text(separator=" ", strip=True)[:12_000]
    speakers = _public_speakers(payload, event.get("show_speakers_reg_page"))
    paid = event.get("paid")
    price = (
        PriceStatus.FREE
        if paid is False
        else (PriceStatus.PAID if paid is True else PriceStatus.UNKNOWN)
    )
    virtual = event.get("event_type") == "Webinar"
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"goldcast:{event_id}",
        title=_text(event.get("title")),
        registration_url=url,
        start_at=start,
        end_at=end,
        venue_name="Online" if virtual else None,
        city=None if virtual else city,
        description=description,
        price_status=price,
        organizer_name="LangChain",
        speaker_names=tuple(speakers),
        entity_profiles=(
            EventEntityProfile(
                "LangChain", "organizer", "organization", "https://www.langchain.com/"
            ),
        ),
    )


def _luma_event(document: HTMLParser, url: str, source_key: str) -> CandidateEvent:
    try:
        return linked_luma_event(document, url, source_key)
    except LumaEventFetchError as exc:
        raise LangChainFetchError("LangChain linked Luma event metadata is invalid") from exc


def _public_speakers(payload: dict[str, object], show_speakers: object) -> tuple[str, ...]:
    speakers: list[str] = []
    if show_speakers is True:
        values = payload.get("speakers", [])
        if not isinstance(values, list) or len(values) > _MAX_PUBLIC_ROLE_RECORDS:
            raise LangChainFetchError("LangChain public speaker list is invalid")
        for item in values:
            if not isinstance(item, dict) or item.get("hidden") is not False:
                continue
            user = json_object(item.get("user"))
            if user is not None:
                name = f"{_text(user.get('first_name'), limit=100)} {_text(user.get('last_name'), limit=100)}"
                if name not in speakers:
                    speakers.append(name)
    if len(speakers) > _MAX_PUBLIC_NAMES:
        raise LangChainFetchError("LangChain public speaker list exceeds the catalog limit")
    return tuple(speakers)
