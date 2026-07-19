"""Alameda County Library all-physical-branch RSS coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

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
_LOCATION_IDS = ("ALB", "CSV", "CTV", "CHY", "DUB", "FRM", "NWK", "NLS", "SLZ", "UCY")
_MOBILE_LIBRARY_LOCATION_ID = "MOS"
_VIRTUAL_LOCATION_ID = "BC_VIRTUAL"
_UNKNOWN_OFFSITE_LOCATION_ID = "unreviewed-offsite"


def _seed(location_ids: tuple[str, ...]) -> str:
    return "https://gateway.bibliocommons.com/v2/libraries/aclibrary/rss/events?" + urlencode(
        [("locations", location_id) for location_id in location_ids]
    )


_SEED_URL = _seed(_LOCATION_IDS)
_FREMONT_SEED_URL = (
    "https://gateway.bibliocommons.com/v2/libraries/aclibrary/rss/events?locations=FRM"
)


def _source(
    *,
    seed_url: str = _SEED_URL,
    page_limit: int = 40,
    min_interval_ms: int = 5_000,
) -> CatalogSource:
    return CatalogSource(
        source_key="alameda-county-library-all-physical-branches-events",
        display_name="Alameda County Library: All Physical Branch Events",
        publisher="Alameda County Library",
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


def _fremont_source() -> CatalogSource:
    """Return the retained legacy profile used to prove gateway-wide pacing (FR-10.4)."""
    return CatalogSource(
        source_key="alameda-county-library-fremont-events",
        display_name="Alameda County Library: Fremont Events",
        publisher="Alameda County Library",
        seed_url=_FREMONT_SEED_URL,
        approved_origins=("https://gateway.bibliocommons.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.BIBLIOCOMMONS_RSS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=7,
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
    location_id: str | None = "ALB",
    location_name: str = "Albany Library",
    city: str = "Albany",
    physical: bool = True,
    guid_identifier: str | None = None,
    link_identifier: str | None = None,
) -> str:
    """Return one fixture-only RSS instance in Alameda's published field shape (FR-3.7)."""
    guid_id = guid_identifier or identifier
    link_id = link_identifier or identifier
    location = ""
    if location_id is not None:
        physical_fields = (
            f"<bc:name>{location_name}</bc:name><bc:street>1247 Marin Ave</bc:street>"
            f"<bc:city>{city}</bc:city><bc:latitude>37.887855</bc:latitude>"
            "<bc:longitude>-122.2931965</bc:longitude>"
            "<bc:location_details>Community Room</bc:location_details>"
            if physical
            else ""
        )
        location = f"<bc:location><bc:id>{location_id}</bc:id>{physical_fields}</bc:location>"
    return f"""
    <item><title><![CDATA[Alameda Library Workshop]]></title>
    <description><![CDATA[<p>Learn <em>together</em>.</p>]]></description>
    <link>https://aclibrary.bibliocommons.com/events/{link_id}</link>
    <guid isPermaLink="true">https://aclibrary.bibliocommons.com/events/{guid_id}</guid>
    <bc:start_date>{start}</bc:start_date><bc:end_date>{end}</bc:end_date>
    <bc:is_cancelled>{cancelled}</bc:is_cancelled><bc:is_virtual>{virtual}</bc:is_virtual>{location}</item>
    """


async def test_alameda_uses_closed_locations_window_and_recurring_occurrence_identity() -> None:
    """Alameda retains reviewed physical branches and separate published recurrences (FR-3.1/FR-3.8)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item("physical")
                + _item(
                    "hybrid",
                    location_id="UCY",
                    location_name="Union City Library",
                    city="Union City",
                    virtual="true",
                )
                + _item(
                    "virtual-only",
                    location_id=_VIRTUAL_LOCATION_ID,
                    location_name="Online Event",
                    physical=False,
                    virtual="true",
                )
                + _item(
                    "mobile-library",
                    location_id=_MOBILE_LIBRARY_LOCATION_ID,
                    location_name="Mobile Library",
                    city="",
                )
                + _item(
                    "unknown-offsite",
                    location_id=_UNKNOWN_OFFSITE_LOCATION_ID,
                    location_name="Unreviewed Offsite Venue",
                    city="Fremont",
                )
                + _item("cancelled", cancelled="true")
                + _item("past", start="2026-07-18T11:59:00Z")
                + _item(
                    "at-boundary",
                    start="2026-10-16T07:00:00Z",
                    end="2026-10-16T08:00:00Z",
                )
                + _item("mismatched", guid_identifier="guid-id", link_identifier="link-id")
                # BiblioCommons publishes a distinct source event ID for each weekly occurrence.
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
        "bibliocommons:alameda-county-library-all-physical-branches-events:physical:2026-07-18T17:30:00+00:00",
        "bibliocommons:alameda-county-library-all-physical-branches-events:hybrid:2026-07-18T17:30:00+00:00",
        "bibliocommons:alameda-county-library-all-physical-branches-events:weekly-20260718:2026-07-18T20:00:00+00:00",
        "bibliocommons:alameda-county-library-all-physical-branches-events:weekly-20260725:2026-07-25T20:00:00+00:00",
    ]
    physical = candidates[0]
    assert physical.registration_url == "https://aclibrary.bibliocommons.com/events/physical"
    assert physical.venue_name == "Albany Library — Community Room"
    assert physical.city == "Albany"
    assert physical.geo is not None
    assert (physical.geo.lat, physical.geo.lon) == (37.887855, -122.2931965)
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


async def test_alameda_pages_at_a_five_second_cadence_with_repeated_location_parameters() -> None:
    """Alameda preserves its reviewed query order and five-second gateway pace (FR-10.3/FR-10.4)."""
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


async def test_alameda_stricter_pace_carries_to_legacy_fremont_request_on_same_gateway() -> None:
    """A five-second Alameda request keeps the following legacy gateway read at five seconds (FR-10.4)."""
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
    assert await fetcher.fetch(_fremont_source()) == []

    assert [list(url.params.multi_items()) for url in requested] == [
        [
            *(("locations", location_id) for location_id in _LOCATION_IDS),
            ("startDate", "2026-07-18"),
            ("endDate", "2026-10-16"),
        ],
        [
            ("locations", "FRM"),
            ("startDate", "2026-07-18"),
            ("endDate", "2026-10-16"),
        ],
    ]
    assert slept == [5.0]


async def test_alameda_rejects_query_pace_or_page_cap_tampering_before_a_request() -> None:
    """Registry data cannot widen Alameda branches or weaken its reviewed bounds (FR-10.3/10.4)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_feed(""), request=request)

    fetcher = BiblioCommonsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))
    unsafe_seeds = (
        _seed(_LOCATION_IDS[1:] + _LOCATION_IDS[:1]),
        _seed(_LOCATION_IDS[:-1]),
        _seed((*_LOCATION_IDS, _LOCATION_IDS[0])),
        f"{_SEED_URL}&startDate=2020-01-01",
    )

    for seed_url in unsafe_seeds:
        with pytest.raises(ValueError, match="reviewed publisher RSS"):
            await fetcher.fetch(_source(seed_url=seed_url))
    with pytest.raises(ValueError, match="reviewed publisher RSS"):
        await fetcher.fetch(replace(_source(), page_limit=39))
    with pytest.raises(ValueError, match="reviewed publisher RSS"):
        await fetcher.fetch(_source(min_interval_ms=1_500))
    assert requested == []


async def test_alameda_fails_closed_when_all_forty_reviewed_pages_are_full() -> None:
    """A full Alameda cap remains retryable instead of publishing a partial discovery window (NFR-8)."""
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
    assert len(requested) == 40
    assert requested[-1].params.get("page") == "40"
