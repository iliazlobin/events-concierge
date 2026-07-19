"""SCCLD Saratoga BiblioCommons adapter coverage (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.bibliocommons.source import BiblioCommonsCatalogFetcher
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_BC_NAMESPACE = "http://bibliocommons.com/rss/1.0/modules/event/"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="sccld-saratoga-events",
        display_name="SCCLD Saratoga Library Events",
        publisher="Santa Clara County Library District",
        seed_url="https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=SA",
        approved_origins=("https://gateway.bibliocommons.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.BIBLIOCOMMONS_RSS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=15,
    )


def _feed(items: str) -> str:
    return f'<rss xmlns:bc="{_BC_NAMESPACE}" version="2.0"><channel>{items}</channel></rss>'


def _item(
    identifier: str,
    *,
    location_id: str | None = "SA",
    virtual: str = "false",
    city: str = "Saratoga",
) -> str:
    location = ""
    if location_id is not None:
        location = f"""
        <bc:location><bc:id>{location_id}</bc:id><bc:name>Saratoga Library</bc:name>
        <bc:city>{city}</bc:city><bc:latitude>37.2638</bc:latitude>
        <bc:longitude>-122.0230</bc:longitude><bc:location_details>Community Room</bc:location_details>
        </bc:location>"""
    return f"""
    <item><title><![CDATA[Family Board Game Night]]></title>
    <description><![CDATA[<p>Play <em>together</em>.</p>]]></description>
    <link>https://sccl.bibliocommons.com/events/{identifier}</link>
    <guid isPermaLink="true">https://sccl.bibliocommons.com/events/{identifier}</guid>
    <bc:start_date>2026-07-18T22:30:00Z</bc:start_date>
    <bc:end_date>2026-07-18T23:30:00Z</bc:end_date>
    <bc:is_cancelled>false</bc:is_cancelled><bc:is_virtual>{virtual}</bc:is_virtual>{location}</item>
    """


async def test_saratoga_bibliocommons_keeps_only_its_closed_physical_location() -> None:
    """Saratoga accepts its own physical/hybrid events but cannot ingest another SCCLD location (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item("saratoga-1")
                + _item("saratoga-hybrid", virtual="true")
                + _item("milpitas-1", location_id="MI", city="Milpitas")
                + _item("virtual-only", location_id=None, virtual="true")
            ),
            request=request,
        )

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "bibliocommons:sccld-saratoga-events:saratoga-1:2026-07-18T22:30:00+00:00",
        "bibliocommons:sccld-saratoga-events:saratoga-hybrid:2026-07-18T22:30:00+00:00",
    ]
    physical = candidates[0]
    assert physical.registration_url == "https://sccl.bibliocommons.com/events/saratoga-1"
    assert physical.venue_name == "Saratoga Library \u2014 Community Room"
    assert physical.city == "Saratoga"
    assert physical.geo is not None
    assert (physical.geo.lat, physical.geo.lon) == (37.2638, -122.023)
    assert physical.description == "Play together."
    assert physical.price_status is PriceStatus.UNKNOWN
    assert requested == [
        httpx.URL("https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=SA")
    ]


async def test_saratoga_bibliocommons_refuses_an_unreviewed_source_key_before_requesting() -> None:
    """A registry record cannot point the shared feed adapter to another SCCLD location (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_feed(""), request=request)

    source = CatalogSource(
        source_key="unreviewed-sccld-location",
        display_name="Unreviewed SCCLD Location",
        publisher="Events Concierge tests",
        seed_url="https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=SA",
        approved_origins=("https://gateway.bibliocommons.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.BIBLIOCOMMONS_RSS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    fetcher = BiblioCommonsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed publisher RSS"):
        await fetcher.fetch(source)
    assert requested == []
