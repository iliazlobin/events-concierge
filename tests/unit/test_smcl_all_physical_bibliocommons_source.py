"""SMCL all-physical-branches BiblioCommons adapter coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

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
    "1A",
    "1B",
    "1R",
    "1E",
    "1F",
    "1H",
    "1M",
    "1N",
    "1Z",
    "1P",
    "1V",
    "1S",
    "1W",
)
_BOOKMOBILE_LOCATION_ID = "0K"


def _seed(location_ids: tuple[str, ...]) -> str:
    return "https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events?" + urlencode(
        [("locations", location_id) for location_id in location_ids]
    )


_SEED_URL = _seed(_LOCATION_IDS)
_MILLBRAE_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events?locations=1M"


def _source(
    *,
    seed_url: str = _SEED_URL,
    page_limit: int = 150,
    min_interval_ms: int = 5_000,
) -> CatalogSource:
    return CatalogSource(
        source_key="smcl-all-physical-branches-events",
        display_name="San Mateo County Libraries: All Physical Branch Events",
        publisher="San Mateo County Libraries",
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


def _millbrae_source() -> CatalogSource:
    """Return the unchanged legacy SMCL profile used to prove shared-host pacing (FR-10.4)."""
    return CatalogSource(
        source_key="smcl-millbrae-events",
        display_name="San Mateo County Libraries: Millbrae Events",
        publisher="San Mateo County Libraries",
        seed_url=_MILLBRAE_SEED_URL,
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
    location_id: str | None = "1A",
    location_name: str = "Atherton Library",
    physical: bool = True,
    guid_identifier: str | None = None,
    link_identifier: str | None = None,
) -> str:
    """Return one fixture-only RSS event instance with SMCL's published field shape (FR-3.7)."""
    guid_id = guid_identifier or identifier
    link_id = link_identifier or identifier
    location = ""
    if location_id is not None:
        physical_fields = (
            f"<bc:name>{location_name}</bc:name><bc:street>2 Dinkelspiel Station Lane</bc:street>"
            "<bc:city>Atherton</bc:city><bc:latitude>37.4614</bc:latitude>"
            "<bc:longitude>-122.1976</bc:longitude>"
            "<bc:location_details>Community Room</bc:location_details>"
            if physical
            else ""
        )
        location = f"<bc:location><bc:id>{location_id}</bc:id>{physical_fields}</bc:location>"
    return f"""
    <item><title><![CDATA[SMCL Family Workshop]]></title>
    <description><![CDATA[<p>Learn <em>together</em>.</p>]]></description>
    <link>https://smcl.bibliocommons.com/events/{link_id}</link>
    <guid isPermaLink="true">https://smcl.bibliocommons.com/events/{guid_id}</guid>
    <bc:start_date>{start}</bc:start_date><bc:end_date>{end}</bc:end_date>
    <bc:is_cancelled>{cancelled}</bc:is_cancelled><bc:is_virtual>{virtual}</bc:is_virtual>{location}</item>
    """


async def test_smcl_uses_closed_locations_window_and_recurring_occurrence_identity() -> None:
    """SMCL retains physical branches and distinct published recurrence instances (FR-3.1/FR-3.8)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item("physical")
                + _item(
                    "hybrid", location_id="1W", location_name="Woodside Library", virtual="true"
                )
                + _item("virtual-only", virtual="true", physical=False)
                + _item(
                    "bookmobile",
                    location_id=_BOOKMOBILE_LOCATION_ID,
                    location_name="Bookmobile",
                )
                + _item("cancelled", cancelled="true")
                + _item("past", start="2026-07-17T11:59:00Z")
                + _item(
                    "at-boundary",
                    start="2026-10-15T07:00:00Z",
                    end="2026-10-15T08:00:00Z",
                )
                + _item("mismatched", guid_identifier="guid-id", link_identifier="link-id")
                # BiblioCommons publishes a distinct event ID for each weekly occurrence.
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
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "bibliocommons:smcl-all-physical-branches-events:physical:2026-07-18T17:30:00+00:00",
        "bibliocommons:smcl-all-physical-branches-events:hybrid:2026-07-18T17:30:00+00:00",
        "bibliocommons:smcl-all-physical-branches-events:weekly-20260718:2026-07-18T20:00:00+00:00",
        "bibliocommons:smcl-all-physical-branches-events:weekly-20260725:2026-07-25T20:00:00+00:00",
    ]
    physical = candidates[0]
    assert physical.registration_url == "https://smcl.bibliocommons.com/events/physical"
    assert physical.venue_name == "Atherton Library — Community Room"
    assert physical.city == "Atherton"
    assert physical.geo is not None
    assert (physical.geo.lat, physical.geo.lon) == (37.4614, -122.1976)
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


async def test_smcl_pages_at_a_five_second_cadence_with_repeated_location_parameters() -> None:
    """SMCL preserves its reviewed query order and five-second gateway pace (FR-10.3/FR-10.4)."""
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
    assert slept == [5.0]


async def test_smcl_stricter_pace_carries_to_following_legacy_request_on_the_same_gateway() -> None:
    """A five-second SMCL GET keeps the next 1.5-second SMCL feed GET at five seconds (FR-10.4)."""
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
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    assert await fetcher.fetch(_source()) == []
    assert await fetcher.fetch(_millbrae_source()) == []

    assert [list(url.params.multi_items()) for url in requested] == [
        [
            *(("locations", location_id) for location_id in _LOCATION_IDS),
            ("startDate", "2026-07-17"),
            ("endDate", "2026-10-15"),
        ],
        [
            ("locations", "1M"),
            ("startDate", "2026-07-17"),
            ("endDate", "2026-10-15"),
        ],
    ]
    assert slept == [5.0]


async def test_smcl_rejects_query_pace_or_page_cap_tampering_before_a_request() -> None:
    """Registry data cannot widen SMCL's branches or weaken its reviewed request bounds (FR-10.3/10.4)."""
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
        await fetcher.fetch(replace(_source(), page_limit=149))
    with pytest.raises(ValueError, match="reviewed publisher RSS"):
        await fetcher.fetch(_source(min_interval_ms=1_500))
    assert requested == []


async def test_smcl_fails_closed_when_all_one_hundred_fifty_reviewed_pages_are_full() -> None:
    """A full SMCL cap stays retryable instead of publishing a partial discovery window (NFR-8)."""
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
    assert len(requested) == 150
    assert requested[-1].params.get("page") == "150"
