"""Palo Alto City Library BiblioCommons adapter coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.bibliocommons.source import BiblioCommonsCatalogFetcher
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_BC_NAMESPACE = "http://bibliocommons.com/rss/1.0/modules/event/"
_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/paloalto/rss/events"


def _source(*, seed_url: str = _SEED_URL) -> CatalogSource:
    return CatalogSource(
        source_key="palo-alto-library-events",
        display_name="Palo Alto City Library Events",
        publisher="Palo Alto City Library",
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
    end: str = "2026-07-18T18:00:00Z",
    cancelled: str = "false",
    virtual: str = "false",
    location_id: str | None = "D",
) -> str:
    location = ""
    if location_id is not None:
        location = f"""
        <bc:location><bc:id>{location_id}</bc:id><bc:name>Downtown Library</bc:name>
        <bc:street>270 Forest Ave.</bc:street><bc:city>Palo Alto</bc:city>
        <bc:latitude>37.4438753</bc:latitude><bc:longitude>-122.1591786</bc:longitude>
        <bc:location_details>El Camino Room</bc:location_details></bc:location>"""
    return f"""
    <item><title><![CDATA[Family Storytime]]></title>
    <description><![CDATA[<p>Read <em>together</em>.</p>]]></description>
    <link>https://paloalto.bibliocommons.com/events/{identifier}</link>
    <guid isPermaLink="true">https://paloalto.bibliocommons.com/events/{identifier}</guid>
    <bc:start_date>{start}</bc:start_date><bc:end_date>{end}</bc:end_date>
    <bc:is_cancelled>{cancelled}</bc:is_cancelled><bc:is_virtual>{virtual}</bc:is_virtual>{location}</item>
    """


async def test_palo_alto_uses_a_closed_local_window_and_keeps_only_physical_events() -> None:
    """Palo Alto constructs its reviewed request and preserves only public physical/hybrid truth (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item("physical")
                + _item("hybrid", virtual="true")
                + _item("virtual-only", virtual="true", location_id=None)
                + _item("all-branches", location_id="5a15ef79d207133f00406539")
                + _item("cancelled", cancelled="true")
                + _item("past", start="2026-07-17T11:59:00Z")
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
        "bibliocommons:palo-alto-library-events:physical:2026-07-18T17:30:00+00:00",
        "bibliocommons:palo-alto-library-events:hybrid:2026-07-18T17:30:00+00:00",
    ]
    physical = candidates[0]
    assert physical.registration_url == "https://paloalto.bibliocommons.com/events/physical"
    assert physical.venue_name == "Downtown Library — El Camino Room"
    assert physical.city == "Palo Alto"
    assert physical.geo is not None
    assert (physical.geo.lat, physical.geo.lon) == (37.4438753, -122.1591786)
    assert physical.description == "Read together."
    assert physical.price_status is PriceStatus.UNKNOWN
    assert requested == [httpx.URL(f"{_SEED_URL}?startDate=2026-07-17&endDate=2026-10-15")]


async def test_palo_alto_pages_at_a_human_cadence_and_clips_the_inclusive_end_date() -> None:
    """The inclusive publisher end date cannot violate the local half-open 90-day horizon (FR-3.1/10.4)."""
    requested: list[httpx.URL] = []
    slept: list[float] = []
    current = 100.0

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    first_page = "".join(_item(f"first-{index}") for index in range(25))

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        body = (
            _feed(first_page)
            if request.url.params.get("page") is None
            else _feed(
                _item(
                    "before-boundary",
                    start="2026-10-15T06:59:00Z",
                    end="2026-10-15T07:00:00Z",
                )
                + _item(
                    "at-boundary",
                    start="2026-10-15T07:00:00Z",
                    end="2026-10-15T08:00:00Z",
                )
            )
        )
        return httpx.Response(200, text=body, request=request)

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert len(candidates) == 26
    assert candidates[-1].source_event_id == (
        "bibliocommons:palo-alto-library-events:before-boundary:2026-10-15T06:59:00+00:00"
    )
    assert requested == [
        httpx.URL(f"{_SEED_URL}?startDate=2026-07-17&endDate=2026-10-15"),
        httpx.URL(f"{_SEED_URL}?startDate=2026-07-17&endDate=2026-10-15&page=2"),
    ]
    assert slept == [1.5]


async def test_palo_alto_refuses_a_seed_that_attempts_to_supply_its_own_window() -> None:
    """Registry data cannot redirect the closed Palo Alto source to an arbitrary date query (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_feed(""), request=request)

    fetcher = BiblioCommonsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed publisher RSS"):
        await fetcher.fetch(_source(seed_url=f"{_SEED_URL}?startDate=2020-01-01"))
    assert requested == []
