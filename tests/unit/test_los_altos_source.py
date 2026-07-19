"""Los Altos CivicEngage RSS profile coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

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

_SEED_URL = "https://www.losaltosca.gov/RSSFeed.aspx?CID=All-calendar.xml&ModID=58"
_NAMESPACE = "https://www.losaltosca.gov/Calendar.aspx"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="los-altos-events",
        display_name="City of Los Altos Events",
        publisher="City of Los Altos",
        seed_url=_SEED_URL,
        approved_origins=("https://www.losaltosca.gov",),
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
            f'<rss version="2.0" xmlns:calendarEvent="{_NAMESPACE}">',
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
    title: str = "Free Summer Concert",
    event_date: str = "July 21, 2026",
    event_times: str = "06:00 PM - 08:00 PM",
    location: str = "City Hall<br>1 N. San Antonio RoadLos Altos, CA 94022",
    link: str | None = None,
    guid: str | None = None,
    guid_perma_link: str = "false",
) -> str:
    event_link = link or f"https://www.losaltosca.gov/Calendar.aspx?EID={event_id}"
    event_guid = guid or f"{event_link}/{occurrence_id}"
    return "\n".join(
        (
            "<item>",
            f"<title>{escape(title)}</title>",
            f"<link>{escape(event_link)}</link>",
            "<description><![CDATA[<p>Outdoor <em>community</em> music.</p>]]></description>",
            f"<calendarEvent:EventDates>{escape(event_date)}</calendarEvent:EventDates>",
            f"<calendarEvent:EventTimes>{escape(event_times)}</calendarEvent:EventTimes>",
            f"<calendarEvent:Location>{escape(location)}</calendarEvent:Location>",
            f'<guid isPermaLink="{guid_perma_link}">{escape(event_guid)}</guid>',
            "</item>",
        )
    )


async def test_los_altos_maps_only_future_named_physical_rows() -> None:
    """The closed profile strips only duplicated source locality and never invents a venue (FR-3.1/FR-3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item(5001, 639170000000000000),
                _item(
                    5002,
                    639170000010000000,
                    location=(
                        "Community Center<br>97 Hillview Ave.,Los Altos, CA 94022"
                        "Los Altos, CA 94022"
                    ),
                ),
                _item(5003, 639170000020000000, location="OnlineLos Altos, CA 94024"),
                _item(5004, 639170000030000000, location="Los Altos, CA 94022"),
                _item(5005, 639170000040000000, title="Summer Concert Cancelled"),
                _item(5006, 639170000050000000, event_times="All day"),
                _item(
                    5007,
                    639170000060000000,
                    event_date="July 17, 2026",
                    event_times="01:00 AM - 02:00 AM",
                ),
                _item(5008, 639170000070000000, location="DowntownPalo Alto, CA 94301"),
                _item(
                    5009,
                    639170000080000000,
                    link="https://www.losaltosca.gov/Calendar.aspx?EID=5009&extra=1",
                ),
                _item(
                    5010,
                    639170000090000000,
                    guid="https://www.losaltosca.gov/Calendar.aspx?EID=5010/not-a-tick",
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
        "los-altos:los-altos-events:5001:639170000000000000",
        "los-altos:los-altos-events:5002:639170000010000000",
    ]
    first, repeated_locality = candidates
    assert first.registration_url == "https://www.losaltosca.gov/Calendar.aspx?EID=5001"
    assert first.start_at == datetime(2026, 7, 22, 1, 0, tzinfo=UTC)
    assert first.end_at == datetime(2026, 7, 22, 3, 0, tzinfo=UTC)
    assert first.venue_name == "City Hall 1 N. San Antonio Road"
    assert first.city == "Los Altos"
    assert first.geo is None
    assert first.description == "Outdoor community music."
    assert first.price_status is PriceStatus.UNKNOWN
    assert repeated_locality.venue_name == "Community Center 97 Hillview Ave."
    assert requested == [httpx.URL(_SEED_URL)]


async def test_los_altos_fails_closed_for_duplicate_occurrences_and_item_cap() -> None:
    """The unpaged public document cannot silently exceed its reviewed source-specific bound (NFR-8)."""
    payloads = iter(
        (
            _feed(_item(5100, 639170100000000000), _item(5100, 639170100000000000)),
            _feed(*(_item(5200 + index, 639170200000000000 + index) for index in range(50))),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(payloads), request=request)

    async def no_sleep(_delay: float) -> None:
        return None

    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test",
        clock=lambda: 100.0,
        sleep=no_sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CivicEngageRssFetchError, match="repeated an occurrence"):
        await fetcher.fetch(_source())
    with pytest.raises(CivicEngageRssFetchError, match="50-item cap"):
        await fetcher.fetch(_source())


async def test_los_altos_rejects_invalid_payloads_redirects_and_tampered_profiles() -> None:
    """Seed data cannot widen this publisher-specific feed, namespace, or handoff boundary (NFR-8)."""
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

    unreviewed = replace(_source(), source_key="unreviewed-los-altos-events")
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(unreviewed)
    reordered_seed = replace(
        _source(),
        seed_url="https://www.losaltosca.gov/RSSFeed.aspx?ModID=58&CID=All-calendar.xml",
    )
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(reordered_seed)
    tampered_cap = replace(_source(), page_limit=2)
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(tampered_cap)
    assert requested == [_SEED_URL]
