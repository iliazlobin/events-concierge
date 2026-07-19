"""San Francisco Recreation & Parks RSS adapter coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from xml.sax.saxutils import escape

import httpx
import pytest

from events_concierge.adapters.civic_engage.source import (
    CivicEngageRssCatalogFetcher,
    CivicEngageRssFetchError,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_SEED_URL = "https://sfrecpark.org/RSSFeed.aspx?CID=Main-Calendar-14&ModID=58"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="sf-rec-park-events",
        display_name="San Francisco Recreation & Parks Events",
        publisher="San Francisco Recreation & Park Department",
        seed_url=_SEED_URL,
        approved_origins=("https://sfrecpark.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.CIVIC_ENGAGE_RSS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=1,
    )


def _feed(*items: str) -> str:
    return "\n".join(
        (
            '<?xml version="1.0"?>',
            '<rss version="2.0" xmlns:calendarEvent="https://sfrecpark.org/Calendar.aspx">',
            "<channel>",
            *items,
            "</channel>",
            "</rss>",
        )
    )


def _item(
    event_id: int,
    occurrence_id: int,
    *,
    title: str = "Friday Chess Lessons",
    event_date: str = "July 18, 2026",
    event_times: str = "11:30 AM - 01:30 PM",
    location: str = "Post and StocktonSan Francisco, CA 94108",
    link: str | None = None,
    guid: str | None = None,
    guid_perma_link: str = "false",
) -> str:
    event_link = link or f"https://sfrecpark.org/Calendar.aspx?EID={event_id}"
    event_guid = guid or f"{event_link}/{occurrence_id}"
    return "\n".join(
        (
            "<item>",
            f"<title>{title}</title>",
            f"<link>{escape(event_link)}</link>",
            "<description><![CDATA[<p>Neighborhood <em>music</em> with friends.</p>]]></description>",
            f"<calendarEvent:EventDates>{event_date}</calendarEvent:EventDates>",
            f"<calendarEvent:EventTimes>{event_times}</calendarEvent:EventTimes>",
            f"<calendarEvent:Location>{location}</calendarEvent:Location>",
            f'<guid isPermaLink="{guid_perma_link}">{escape(event_guid)}</guid>',
            "</item>",
        )
    )


async def test_sf_rec_park_maps_only_future_timed_physical_rows() -> None:
    """The closed feed uses source date/time/location and unvisited official handoffs (FR-3.1/FR-3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item(10179, 639119408880000000),
                _item(10180, 639119408890000000, location="Online"),
                _item(10181, 639119408900000000, title="Concert Cancelled"),
                _item(10182, 639119408910000000, event_times="All day"),
                _item(
                    10183,
                    639119408920000000,
                    event_date="July 17, 2026",
                    event_times="02:00 AM - 03:00 AM",
                ),
                _item(10184, 639119408930000000, location="Civic CenterOakland, CA 94612"),
                _item(
                    10185,
                    639119408940000000,
                    link="https://sfrecpark.org/Calendar.aspx?EID=10185&unexpected=1",
                ),
                _item(
                    10186,
                    639119408950000000,
                    guid="https://sfrecpark.org/Calendar.aspx?EID=10186/not-a-tick",
                ),
            ),
            request=request,
        )

    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "sf-rec-park:sf-rec-park-events:10179:639119408880000000"
    ]
    candidate = candidates[0]
    assert candidate.title == "Friday Chess Lessons"
    assert candidate.registration_url == "https://sfrecpark.org/Calendar.aspx?EID=10179"
    assert candidate.start_at == datetime(2026, 7, 18, 18, 30, tzinfo=UTC)
    assert candidate.end_at == datetime(2026, 7, 18, 20, 30, tzinfo=UTC)
    assert candidate.venue_name == "Post and Stockton"
    assert candidate.city == "San Francisco"
    assert candidate.geo is None
    assert candidate.description == "Neighborhood music with friends."
    assert candidate.price_status is PriceStatus.UNKNOWN
    assert requested == [httpx.URL(_SEED_URL)]


async def test_sf_rec_park_applies_a_per_host_pacing_floor() -> None:
    """Repeated feed requests honor the source's anonymous human-cadence floor (FR-10.4)."""
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_feed(), request=request)

    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test",
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    assert await fetcher.fetch(_source()) == []
    assert await fetcher.fetch(_source()) == []
    assert slept == [1.5]


async def test_sf_rec_park_fails_closed_for_duplicate_occurrences_and_item_cap() -> None:
    """A changed unpaged feed cannot be claimed complete after duplicate or capped data (NFR-8)."""
    payloads = iter(
        (
            _feed(
                _item(10200, 639119500000000000),
                _item(10200, 639119500000000000),
            ),
            _feed(*(_item(10300 + index, 639119600000000000 + index) for index in range(200))),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(payloads), request=request)

    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(CivicEngageRssFetchError, match="repeated an occurrence"):
        await fetcher.fetch(_source())
    with pytest.raises(CivicEngageRssFetchError, match="200-item cap"):
        await fetcher.fetch(_source())


async def test_sf_rec_park_rejects_invalid_payloads_redirects_and_unreviewed_endpoints() -> None:
    """Bad XML or registry tampering cannot widen the source boundary or catalog effect (NFR-8)."""
    requested: list[str] = []

    async def invalid_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            content=b"<!DOCTYPE rss [<!ENTITY test 'unsafe'>]><rss />",
            request=request,
        )

    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(invalid_handler)
    )
    with pytest.raises(CivicEngageRssFetchError, match="unsupported XML declarations"):
        await fetcher.fetch(_source())
    assert requested == [_SEED_URL]

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/feed"},
            request=request,
        )

    redirect_fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(redirect_handler)
    )
    with pytest.raises(CivicEngageRssFetchError, match="approved endpoint"):
        await redirect_fetcher.fetch(_source())

    unreviewed = replace(_source(), source_key="unreviewed-sf-rec-park-events")
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(unreviewed)
    tampered_seed = replace(_source(), seed_url=f"{_SEED_URL}&page=2")
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(tampered_seed)
    tampered_cap = replace(_source(), page_limit=2)
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(tampered_cap)
    assert requested == [_SEED_URL]
