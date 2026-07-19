"""SCCLD all-physical-branches BiblioCommons adapter coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

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
_LOCATION_IDS = ("CA", "CU", "GI", "LA", "MI", "MH", "SA", "WO")
_VIRTUAL_LOCATION_ID = "BC_VIRTUAL"
_UNKNOWN_OFFSITE_LOCATION_ID = "unreviewed-offsite"


def _seed(location_ids: tuple[str, ...]) -> str:
    return "https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?" + urlencode(
        [("locations", location_id) for location_id in location_ids]
    )


_SEED_URL = _seed(_LOCATION_IDS)
_MILPITAS_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=MI"


def _source(
    *,
    seed_url: str = _SEED_URL,
    page_limit: int = 50,
    min_interval_ms: int = 5_000,
) -> CatalogSource:
    return CatalogSource(
        source_key="sccld-all-physical-branches-events",
        display_name="Santa Clara County Library District: All Physical Branch Events",
        publisher="Santa Clara County Library District",
        seed_url=seed_url,
        approved_origins=("https://gateway.bibliocommons.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.BIBLIOCOMMONS_RSS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=min_interval_ms,
        page_limit=page_limit,
    )


def _milpitas_source() -> CatalogSource:
    """Return the legacy SCCLD profile used to prove shared-host pacing (FR-10.4)."""
    return CatalogSource(
        source_key="sccld-milpitas-events",
        display_name="SCCLD Milpitas Library Events",
        publisher="Santa Clara County Library District",
        seed_url=_MILPITAS_SEED_URL,
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
    location_id: str | None = "CA",
    location_name: str = "Campbell Library",
    city: str = "Campbell",
    physical: bool = True,
    guid_identifier: str | None = None,
    link_identifier: str | None = None,
) -> str:
    """Return one fixture-only RSS event instance with SCCLD's published field shape (FR-3.7)."""
    guid_id = guid_identifier or identifier
    link_id = link_identifier or identifier
    location = ""
    if location_id is not None:
        physical_fields = (
            f"<bc:name>{location_name}</bc:name><bc:street>77 Harrison Ave.</bc:street>"
            f"<bc:city>{city}</bc:city><bc:latitude>37.2882269</bc:latitude>"
            "<bc:longitude>-121.9432483</bc:longitude>"
            "<bc:location_details>Community Room</bc:location_details>"
            if physical
            else ""
        )
        location = f"<bc:location><bc:id>{location_id}</bc:id>{physical_fields}</bc:location>"
    return f"""
    <item><title><![CDATA[SCCLD Family Workshop]]></title>
    <description><![CDATA[<p>Learn <em>together</em>.</p>]]></description>
    <link>https://sccl.bibliocommons.com/events/{link_id}</link>
    <guid isPermaLink="true">https://sccl.bibliocommons.com/events/{guid_id}</guid>
    <bc:start_date>{start}</bc:start_date><bc:end_date>{end}</bc:end_date>
    <bc:is_cancelled>{cancelled}</bc:is_cancelled><bc:is_virtual>{virtual}</bc:is_virtual>{location}</item>
    """


async def test_sccld_uses_closed_locations_window_and_recurring_occurrence_identity() -> None:
    """SCCLD retains reviewed physical branches and distinct recurrence instances (FR-3.1/FR-3.8)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item("physical")
                + _item(
                    "hybrid",
                    location_id="WO",
                    location_name="Woodland Branch Library",
                    city="Los Altos",
                    virtual="true",
                )
                + _item(
                    "virtual-only",
                    location_id=_VIRTUAL_LOCATION_ID,
                    physical=False,
                    virtual="true",
                )
                + _item(
                    "unknown-offsite",
                    location_id=_UNKNOWN_OFFSITE_LOCATION_ID,
                    location_name="Bookmobile or offsite venue",
                )
                + _item("cancelled", cancelled="true")
                + _item("past", start="2026-07-18T11:59:00Z")
                + _item(
                    "at-boundary",
                    start="2026-10-16T07:00:00Z",
                    end="2026-10-16T08:00:00Z",
                )
                + _item("mismatched", guid_identifier="guid-id", link_identifier="link-id")
                + _item(
                    "weekly-20260718",
                    start="2026-07-18T20:00:00Z",
                    end="2026-07-18T21:00:00Z",
                )
                + _item(
                    "weekly-20260725",
                    start="2026-07-25T20:00:00Z",
                    end="2026-07-25T21:00:00Z",
                )
            ),
            request=request,
        )

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "bibliocommons:sccld-all-physical-branches-events:physical:2026-07-18T17:30:00+00:00",
        "bibliocommons:sccld-all-physical-branches-events:hybrid:2026-07-18T17:30:00+00:00",
        "bibliocommons:sccld-all-physical-branches-events:weekly-20260718:2026-07-18T20:00:00+00:00",
        "bibliocommons:sccld-all-physical-branches-events:weekly-20260725:2026-07-25T20:00:00+00:00",
    ]
    physical = candidates[0]
    assert physical.registration_url == "https://sccl.bibliocommons.com/events/physical"
    assert physical.venue_name == "Campbell Library — Community Room"
    assert physical.city == "Campbell"
    assert physical.geo is not None
    assert (physical.geo.lat, physical.geo.lon) == (37.2882269, -121.9432483)
    assert physical.description == "Learn together."
    assert physical.price_status is PriceStatus.UNKNOWN
    assert [list(url.params.multi_items()) for url in requested] == [
        [
            *(("locations", location_id) for location_id in _LOCATION_IDS),
            ("startDate", "2026-07-18"),
            ("endDate", "2026-10-16"),
        ]
    ]
    assert all(url.host == "gateway.bibliocommons.com" for url in requested)


async def test_sccld_pages_at_a_five_second_cadence_with_repeated_location_parameters() -> None:
    """SCCLD preserves its reviewed query order and five-second gateway pace (FR-10.3/FR-10.4)."""
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
        now=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    expected = [
        *(("locations", location_id) for location_id in _LOCATION_IDS),
        ("startDate", "2026-07-18"),
        ("endDate", "2026-10-16"),
    ]
    assert len(candidates) == 26
    assert [list(url.params.multi_items()) for url in requested] == [
        expected,
        [*expected, ("page", "2")],
    ]
    assert slept == [5.0]


async def test_sccld_stricter_pace_carries_to_a_legacy_request_on_the_same_gateway() -> None:
    """A five-second SCCLD GET keeps a following legacy SCCLD GET at five seconds (FR-10.4)."""
    requested: list[httpx.URL] = []
    slept: list[float] = []
    current = 100.0

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, text=_feed(""), request=request)

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    assert await fetcher.fetch(_source()) == []
    assert await fetcher.fetch(_milpitas_source()) == []

    assert [list(url.params.multi_items()) for url in requested] == [
        [
            *(("locations", location_id) for location_id in _LOCATION_IDS),
            ("startDate", "2026-07-18"),
            ("endDate", "2026-10-16"),
        ],
        [("locations", "MI")],
    ]
    assert slept == [5.0]


async def test_sccld_rejects_query_pace_or_page_cap_tampering_before_a_request() -> None:
    """Registry data cannot widen SCCLD's branches or weaken reviewed request bounds (FR-10.3/10.4)."""
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
        await fetcher.fetch(replace(_source(), page_limit=49))
    with pytest.raises(ValueError, match="reviewed publisher RSS"):
        await fetcher.fetch(_source(min_interval_ms=1_500))
    assert requested == []


async def test_sccld_fails_closed_when_all_fifty_reviewed_pages_are_full() -> None:
    """A full SCCLD cap stays retryable instead of publishing a partial discovery window (NFR-8)."""
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
        now=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BiblioCommonsFetchError, match="exceeds"):
        await fetcher.fetch(_source())
    assert len(requested) == 50
    assert requested[-1].params.get("page") == "50"
