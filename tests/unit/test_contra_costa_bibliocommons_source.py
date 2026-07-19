"""Contra Costa County Library BiblioCommons adapter coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

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
    "43",
    "7",
    "25",
    "19",
    "14",
    "60",
    "4",
    "21",
    "26",
    "24",
    "11",
    "16",
    "6",
    "55",
    "9",
    "94",
    "1",
    "17",
    "12",
    "15",
    "27",
    "23",
    "8",
    "2",
    "13",
)


def _seed(location_ids: tuple[str, ...]) -> str:
    return "https://gateway.bibliocommons.com/v2/libraries/ccclib/rss/events?" + urlencode(
        [("locations", location_id) for location_id in location_ids]
    )


_SEED_URL = _seed(_LOCATION_IDS)


def _source(*, seed_url: str = _SEED_URL, page_limit: int = 105) -> CatalogSource:
    return CatalogSource(
        source_key="contra-costa-county-library-events",
        display_name="Contra Costa County Library Events",
        publisher="Contra Costa County Library",
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
    location_id: str | None = "43",
    physical: bool = True,
    guid_identifier: str | None = None,
    link_identifier: str | None = None,
) -> str:
    guid_id = guid_identifier or identifier
    link_id = link_identifier or identifier
    location = ""
    if location_id is not None:
        physical_fields = (
            "<bc:name>Central Library</bc:name><bc:street>1500 Monument Boulevard</bc:street>"
            "<bc:city>Concord</bc:city><bc:latitude>37.9779</bc:latitude>"
            "<bc:longitude>-122.0311</bc:longitude>"
            "<bc:location_details>Community Room</bc:location_details>"
            if physical
            else ""
        )
        location = f"<bc:location><bc:id>{location_id}</bc:id>{physical_fields}</bc:location>"
    return f"""
    <item><title><![CDATA[Contra Costa Family Event]]></title>
    <description><![CDATA[<p>Learn <em>together</em>.</p>]]></description>
    <link>https://ccclib.bibliocommons.com/events/{link_id}</link>
    <guid isPermaLink="true">https://ccclib.bibliocommons.com/events/{guid_id}</guid>
    <bc:start_date>{start}</bc:start_date><bc:end_date>{end}</bc:end_date>
    <bc:is_cancelled>{cancelled}</bc:is_cancelled><bc:is_virtual>{virtual}</bc:is_virtual>{location}</item>
    """


async def test_contra_costa_uses_closed_branch_locations_and_a_local_window() -> None:
    """CCCL retains only reviewed physical/hybrid rows and its exact ordered location query (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item("physical")
                + _item("hybrid", location_id="13", virtual="true")
                + _item("virtual-only", virtual="true", physical=False)
                + _item("wrong-location", location_id="BC_VIRTUAL")
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
        "bibliocommons:contra-costa-county-library-events:physical:2026-07-18T17:30:00+00:00",
        "bibliocommons:contra-costa-county-library-events:hybrid:2026-07-18T17:30:00+00:00",
    ]
    physical = candidates[0]
    assert physical.registration_url == "https://ccclib.bibliocommons.com/events/physical"
    assert physical.venue_name == "Central Library — Community Room"
    assert physical.city == "Concord"
    assert physical.geo is not None
    assert (physical.geo.lat, physical.geo.lon) == (37.9779, -122.0311)
    assert physical.description == "Learn together."
    assert physical.price_status is PriceStatus.UNKNOWN
    assert [list(url.params.multi_items()) for url in requested] == [
        [
            *(("locations", location_id) for location_id in _LOCATION_IDS),
            ("startDate", "2026-07-17"),
            ("endDate", "2026-10-15"),
        ]
    ]
    assert all(url.host == "gateway.bibliocommons.com" for url in requested)


async def test_contra_costa_pages_at_a_human_cadence_with_repeated_location_parameters() -> None:
    """The adapter retains each reviewed duplicate query key as it walks the page sequence (FR-10.4)."""
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


async def test_contra_costa_rejects_query_or_page_cap_tampering_before_a_request() -> None:
    """Registry data cannot widen CCCL's reviewed locations, ordering, or source budget (FR-10.3)."""
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
        await fetcher.fetch(replace(_source(), page_limit=104))
    assert requested == []


async def test_contra_costa_fails_closed_when_all_one_hundred_five_reviewed_pages_are_full() -> (
    None
):
    """A full CCCL cap stays retryable instead of publishing a silently partial 90-day window (NFR-8)."""
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
    assert len(requested) == 105
    assert requested[-1].params.get("page") == "105"
