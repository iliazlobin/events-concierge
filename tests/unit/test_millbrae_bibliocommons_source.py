"""San Mateo County Libraries Millbrae BiblioCommons adapter coverage (FR-3.1/FR-3.7/FR-10.3)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.bibliocommons.source import BiblioCommonsCatalogFetcher
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_BC_NAMESPACE = "http://bibliocommons.com/rss/1.0/modules/event/"
_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events?locations=1M"


def _source(*, seed_url: str = _SEED_URL) -> CatalogSource:
    return CatalogSource(
        source_key="smcl-millbrae-events",
        display_name="San Mateo County Libraries: Millbrae Events",
        publisher="San Mateo County Libraries",
        seed_url=seed_url,
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
    start: str = "2026-07-18T17:30:00Z",
    end: str = "2026-07-18T18:30:00Z",
    cancelled: str = "false",
    virtual: str = "false",
    location_id: str | None = "1M",
) -> str:
    location = ""
    if location_id is not None:
        location = f"""
        <bc:location><bc:id>{location_id}</bc:id><bc:name>Millbrae Library</bc:name>
        <bc:street>1 Library Avenue</bc:street><bc:city>Millbrae</bc:city>
        <bc:latitude>37.5999</bc:latitude><bc:longitude>-122.3863</bc:longitude>
        <bc:location_details>Community Room</bc:location_details></bc:location>"""
    return f"""
    <item><title><![CDATA[Family Craft Time]]></title>
    <description><![CDATA[<p>Make <em>art</em> together.</p>]]></description>
    <link>https://smcl.bibliocommons.com/events/{identifier}</link>
    <guid isPermaLink="true">https://smcl.bibliocommons.com/events/{identifier}</guid>
    <bc:start_date>{start}</bc:start_date><bc:end_date>{end}</bc:end_date>
    <bc:is_cancelled>{cancelled}</bc:is_cancelled><bc:is_virtual>{virtual}</bc:is_virtual>{location}</item>
    """


async def test_millbrae_uses_only_its_closed_feed_location_and_local_window() -> None:
    """Millbrae retains approved physical/hybrid rows and constructs its exact date-bounded request (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item("physical")
                + _item("hybrid", virtual="true")
                + _item("virtual-only", virtual="true", location_id=None)
                + _item("wrong-location", location_id="1B")
                + _item("cancelled", cancelled="true")
                + _item(
                    "at-boundary",
                    start="2026-10-15T07:00:00Z",
                    end="2026-10-15T08:00:00Z",
                )
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
        "bibliocommons:smcl-millbrae-events:physical:2026-07-18T17:30:00+00:00",
        "bibliocommons:smcl-millbrae-events:hybrid:2026-07-18T17:30:00+00:00",
    ]
    physical = candidates[0]
    assert physical.registration_url == "https://smcl.bibliocommons.com/events/physical"
    assert physical.venue_name == "Millbrae Library — Community Room"
    assert physical.city == "Millbrae"
    assert physical.geo is not None
    assert (physical.geo.lat, physical.geo.lon) == (37.5999, -122.3863)
    assert physical.description == "Make art together."
    assert physical.price_status is PriceStatus.UNKNOWN
    assert requested == [
        httpx.URL(
            "https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events?"
            "locations=1M&startDate=2026-07-17&endDate=2026-10-15"
        )
    ]


async def test_millbrae_refuses_registry_supplied_date_parameters_before_requesting() -> None:
    """The fixed Millbrae location/date query cannot be changed through source-registry data (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_feed(""), request=request)

    fetcher = BiblioCommonsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed publisher RSS"):
        await fetcher.fetch(_source(seed_url=f"{_SEED_URL}&startDate=2020-01-01"))
    assert requested == []
