"""LiveWhale public-catalog adapter tests (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from events_concierge.adapters.livewhale.source import LiveWhaleCatalogFetcher
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.domain.events import GeoPoint


def _source(*, page_limit: int = 2) -> CatalogSource:
    return CatalogSource(
        source_key="reviewed-livewhale",
        display_name="Reviewed LiveWhale",
        publisher="Events Concierge tests",
        seed_url="https://events.example.test/live/json/events/",
        approved_origins=("https://events.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.LIVEWHALE_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=720,
        min_interval_ms=1_500,
        page_limit=page_limit,
    )


async def test_livewhale_fetches_a_bounded_paced_page_sequence_and_parses_price_truth() -> None:
    """Pagination retains paid/unknown discovery while excluding canceled and online-only records."""
    requested: list[str] = []
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 104,
                            "title": "Zero-cost workshop",
                            "url": "/event/zero-cost",
                            "date_iso": "2026-07-20T18:00:00-07:00",
                            "cost": 0,
                        }
                    ],
                    "links": {"next": "/live/json/events/?page=3"},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 101,
                        "title": "<b>Free &amp; open</b>",
                        "url": "/event/free",
                        "date_iso": "2026-07-17T18:00:00-07:00",
                        "date2_iso": "2026-07-17T20:00:00-07:00",
                        "location": "<i>Sproul Plaza</i>",
                        "location_title": "Sproul Plaza",
                        "location_latitude": "37.87",
                        "location_longitude": "-122.26",
                        "city": "Berkeley",
                        "summary": "<p>Hosted by the campus.</p>",
                        "cost": "Free",
                    },
                    {
                        "id": 102,
                        "title": "Paid exhibit",
                        "url": "/event/paid",
                        "date_iso": "2026-07-18T18:00:00-07:00",
                        "cost": "$18",
                    },
                    {
                        "id": 103,
                        "title": "Tiered event",
                        "url": "/event/tiered",
                        "date_iso": "2026-07-19T18:00:00-07:00",
                        "cost": "<span>Free for students; $25 general admission</span>",
                    },
                    {
                        "id": 105,
                        "title": "Cancelled event",
                        "url": "/event/cancelled",
                        "date_iso": "2026-07-19T18:00:00-07:00",
                        "is_canceled": True,
                        "cost": "Free",
                    },
                    {
                        "id": 106,
                        "title": "Online event",
                        "url": "/event/online",
                        "date_iso": "2026-07-19T18:00:00-07:00",
                        "is_online": 1,
                        "online_type": "Online only",
                        "cost": "Free",
                    },
                    {
                        "id": 107,
                        "title": "Hybrid campus event",
                        "url": "/event/hybrid",
                        "date_iso": "2026-07-19T19:00:00-07:00",
                        "is_online": 1,
                        "online_type": "Hybrid",
                        "cost": "Free",
                    },
                ],
                "links": {"next": "/live/json/events/?page=2"},
            },
            request=request,
        )

    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "livewhale:reviewed-livewhale:101:2026-07-17T18:00:00-07:00",
        "livewhale:reviewed-livewhale:102:2026-07-18T18:00:00-07:00",
        "livewhale:reviewed-livewhale:103:2026-07-19T18:00:00-07:00",
        "livewhale:reviewed-livewhale:107:2026-07-19T19:00:00-07:00",
        "livewhale:reviewed-livewhale:104:2026-07-20T18:00:00-07:00",
    ]
    assert [candidate.price_status for candidate in candidates] == [
        PriceStatus.FREE,
        PriceStatus.PAID,
        PriceStatus.UNKNOWN,
        PriceStatus.FREE,
        PriceStatus.FREE,
    ]
    assert candidates[0].title == "Free & open"
    assert candidates[0].description == "Hosted by the campus."
    assert candidates[0].registration_url == "https://events.example.test/event/free"
    assert candidates[0].venue_name == "Sproul Plaza"
    assert candidates[0].city == "Berkeley"
    assert candidates[0].geo == GeoPoint(lat=37.87, lon=-122.26)
    assert requested == [
        "https://events.example.test/live/json/events/",
        "https://events.example.test/live/json/events/?page=2",
    ]
    assert slept == [1.5]


async def test_livewhale_rejects_an_unapproved_redirect_before_requesting_it() -> None:
    """A reviewed feed cannot redirect the worker to a different publisher origin (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/live/json/events/"},
            request=request,
        )

    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        transport=httpx.MockTransport(handler),
    )

    assert await fetcher.fetch(_source(page_limit=1)) == []
    assert requested == ["https://events.example.test/live/json/events/"]


async def test_livewhale_rejects_an_unapproved_next_page_without_requesting_it() -> None:
    """A pagination link is subject to the same origin allowlist as a redirect (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 201,
                        "title": "Approved first page",
                        "url": "/event/approved",
                        "date_iso": "2026-07-20T18:00:00-07:00",
                        "cost": "Free",
                    }
                ],
                "links": {"next": "https://unapproved.example.test/live/json/events/?page=2"},
            },
            request=request,
        )

    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.title for candidate in candidates] == ["Approved first page"]
    assert requested == ["https://events.example.test/live/json/events/"]
