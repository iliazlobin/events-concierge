"""Oakland Public Library BiblioCommons adapter coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
import pytest

from events_concierge.adapters.bibliocommons.source import (
    BiblioCommonsCatalogFetcher,
    BiblioCommonsFetchError,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_BC_NAMESPACE = "http://bibliocommons.com/rss/1.0/modules/event/"
_LOCATION_IDS = (
    "81A",
    "AAA",
    "ASA",
    "BRA",
    "CCA",
    "DMA",
    "EAA",
    "ELA",
    "GGA",
    "KGA",
    "LVA",
    "MEA",
    "MOA",
    "OHR",
    "PMA",
    "RRA",
    "TMA",
    "WAS",
    "XXA",
    "XXJ",
    "XXY",
    "676de3ef74596c36004dc6bd",
    "6a0c99a4e8af4a2f00739408",
    "6a517185e9de6536001ad4d7",
)


def _seed(location_ids: tuple[str, ...]) -> str:
    return "https://gateway.bibliocommons.com/v2/libraries/oaklandlibrary/rss/events?" + urlencode(
        [("locations", location_id) for location_id in location_ids]
    )


_SEED_URL = _seed(_LOCATION_IDS)


def _source(*, seed_url: str = _SEED_URL, page_limit: int = 60) -> CatalogSource:
    return CatalogSource(
        source_key="oakland-public-library-events",
        display_name="Oakland Public Library Events",
        publisher="Oakland Public Library",
        seed_url=seed_url,
        approved_origins=("https://gateway.bibliocommons.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.BIBLIOCOMMONS_RSS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=page_limit,
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
    location_id: str | None = "81A",
    physical: bool = True,
    guid_identifier: str | None = None,
    link_identifier: str | None = None,
) -> str:
    guid_id = guid_identifier or identifier
    link_id = link_identifier or identifier
    location = ""
    if location_id is not None:
        physical_fields = (
            "<bc:name>Main Library</bc:name><bc:street>125 14th Street</bc:street>"
            "<bc:city>Oakland</bc:city><bc:latitude>37.8025</bc:latitude>"
            "<bc:longitude>-122.2636</bc:longitude>"
            "<bc:location_details>Community Room</bc:location_details>"
            if physical
            else ""
        )
        location = f"<bc:location><bc:id>{location_id}</bc:id>{physical_fields}</bc:location>"
    return f"""
    <item><title><![CDATA[Oakland Game Night]]></title>
    <description><![CDATA[<p>Play <em>together</em>.</p>]]></description>
    <link>https://oaklandlibrary.bibliocommons.com/events/{link_id}</link>
    <guid isPermaLink="true">https://oaklandlibrary.bibliocommons.com/events/{guid_id}</guid>
    <bc:start_date>{start}</bc:start_date><bc:end_date>{end}</bc:end_date>
    <bc:is_cancelled>{cancelled}</bc:is_cancelled><bc:is_virtual>{virtual}</bc:is_virtual>{location}</item>
    """


async def test_oakland_uses_its_closed_locations_and_local_window() -> None:
    """Oakland retains only reviewed physical/hybrid events and keeps the exact repeated query (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item("physical")
                + _item("hybrid-coliseum", location_id="676de3ef74596c36004dc6bd", virtual="true")
                + _item("virtual-only", virtual="true", physical=False)
                + _item("wrong-location", location_id="elsewhere")
                + _item("cancelled", cancelled="true")
                + _item("past", start="2026-07-17T11:59:00Z")
                + _item(
                    "at-boundary",
                    start="2026-10-15T07:00:00Z",
                    end="2026-10-15T08:00:00Z",
                )
                + _item("mismatched", guid_identifier="guid-id", link_identifier="link-id")
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
        "bibliocommons:oakland-public-library-events:physical:2026-07-18T17:30:00+00:00",
        "bibliocommons:oakland-public-library-events:hybrid-coliseum:2026-07-18T17:30:00+00:00",
    ]
    physical = candidates[0]
    assert physical.registration_url == "https://oaklandlibrary.bibliocommons.com/events/physical"
    assert physical.venue_name == "Main Library — Community Room"
    assert physical.city == "Oakland"
    assert physical.geo is not None
    assert (physical.geo.lat, physical.geo.lon) == (37.8025, -122.2636)
    assert physical.description == "Play together."
    assert physical.price_status is PriceStatus.UNKNOWN
    assert [list(url.params.multi_items()) for url in requested] == [
        [
            *(("locations", location_id) for location_id in _LOCATION_IDS),
            ("startDate", "2026-07-17"),
            ("endDate", "2026-10-15"),
        ]
    ]
    assert all(url.host == "gateway.bibliocommons.com" for url in requested)


async def test_oakland_pages_at_a_human_cadence_with_repeated_location_parameters() -> None:
    """The adapter retains ordered duplicate query keys while it walks the reviewed page sequence (FR-10.4)."""
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
        body = _feed(first_page if request.url.params.get("page") is None else _item("final"))
        return httpx.Response(200, text=body, request=request)

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    expected = [
        *(("locations", location_id) for location_id in _LOCATION_IDS),
        ("startDate", "2026-07-17"),
        ("endDate", "2026-10-15"),
    ]
    assert len(candidates) == 26
    assert [list(url.params.multi_items()) for url in requested] == [
        expected,
        [*expected, ("page", "2")],
    ]
    assert slept == [1.5]


async def test_oakland_rejects_query_or_page_cap_tampering_before_a_request() -> None:
    """Registry data cannot widen Oakland's reviewed locations, order, or bounded request budget (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_feed(""), request=request)

    fetcher = BiblioCommonsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))
    unsafe_seeds = (
        _seed(_LOCATION_IDS[1:] + _LOCATION_IDS[:1]),
        _seed(_LOCATION_IDS[:-1]),
        _seed((*_LOCATION_IDS, _LOCATION_IDS[0])),
    )

    for seed_url in unsafe_seeds:
        with pytest.raises(ValueError, match="reviewed publisher RSS"):
            await fetcher.fetch(_source(seed_url=seed_url))
    with pytest.raises(ValueError, match="reviewed publisher RSS"):
        await fetcher.fetch(replace(_source(), page_limit=59))
    assert requested == []


async def test_oakland_fails_closed_when_all_sixty_reviewed_pages_are_full() -> None:
    """A full Oakland cap remains retryable instead of publishing a silently partial 90-day window (NFR-8)."""
    requested: list[httpx.URL] = []
    current = 100.0

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        current += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        page = request.url.params.get("page") or "1"
        return httpx.Response(
            200,
            text=_feed("".join(_item(f"{page}-{index}") for index in range(25))),
            request=request,
        )

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BiblioCommonsFetchError, match="exceeds"):
        await fetcher.fetch(_source())
    assert len(requested) == 60
    assert requested[-1].params.get("page") == "60"
