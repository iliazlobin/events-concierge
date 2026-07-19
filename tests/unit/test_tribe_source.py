"""Oakland Museum of California The Events Calendar adapter tests (FR-3.1/FR-3.7/FR-10.3/10.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.tribe.source import TribeEventsCatalogFetcher, TribeEventsFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus


def _source(*, page_limit: int = 5) -> CatalogSource:
    return CatalogSource(
        source_key="omca-events",
        display_name="Oakland Museum of California Events",
        publisher="Oakland Museum of California",
        seed_url="https://museumca.org/wp-json/tribe/events/v1/events",
        approved_origins=("https://museumca.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.TRIBE_EVENTS_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=page_limit,
    )


def _event(
    identifier: int,
    *,
    title: str = "Friday Nights at OMCA",
    cost: str = "Free",
    status: str = "publish",
    hide_from_listings: object = False,
    is_virtual: object = False,
    all_day: object = False,
    timezone: str = "America/Los_Angeles",
    start_date: str = "2026-07-18 17:00:00",
    end_date: str = "2026-07-18 21:00:00",
    utc_start_date: str = "2026-07-19 00:00:00",
    utc_end_date: str = "2026-07-19 04:00:00",
    venue_name: str | None = "OMCA campus",
    url: str | None = None,
) -> dict[str, object]:
    return {
        "id": identifier,
        "title": f"<strong>{title}</strong>",
        "status": status,
        "hide_from_listings": hide_from_listings,
        "is_virtual": is_virtual,
        "all_day": all_day,
        "timezone": timezone,
        "start_date": start_date,
        "end_date": end_date,
        "utc_start_date": utc_start_date,
        "utc_end_date": utc_end_date,
        "venue": {"venue": venue_name} if venue_name is not None else [],
        "url": url or f"https://museumca.org/event/friday-nights-{identifier}/",
        "cost": cost,
        "excerpt": "<p>Live <em>music</em> and community.</p>",
        "rest_url": f"https://museumca.org/wp-json/tribe/events/v1/events/{identifier}",
        "website": "https://tickets.example.test/omca",
    }


def _page(events: list[dict[str, object]], *, total: int, total_pages: int) -> dict[str, object]:
    return {"events": events, "total": total, "total_pages": total_pages}


async def test_tribe_maps_only_future_timed_physical_omca_events_with_price_truth() -> None:
    """Virtual, hidden, all-day, malformed, past, and unsafe records never become handoffs (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            json=_page(
                [
                    _event(5001),
                    _event(5002, cost="$10"),
                    _event(5003, cost="Free for Members"),
                    _event(5004, is_virtual=True),
                    _event(5005, all_day=True),
                    _event(5006, hide_from_listings=True),
                    _event(5007, venue_name=None),
                    _event(5008, url="https://unapproved.example.test/event/unsafe/"),
                    _event(
                        5009,
                        start_date="2026-07-17 09:00:00",
                        end_date="2026-07-17 10:00:00",
                        utc_start_date="2026-07-17 16:00:00",
                        utc_end_date="2026-07-17 17:00:00",
                    ),
                    _event(
                        5010,
                        start_date="2026-10-15 01:00:00",
                        end_date="2026-10-15 02:00:00",
                        utc_start_date="2026-10-15 08:00:00",
                        utc_end_date="2026-10-15 09:00:00",
                    ),
                ],
                total=10,
                total_pages=1,
            ),
            request=request,
        )

    fetcher = TribeEventsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 19, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "tec:omca-events:5001:2026-07-19T00:00:00+00:00",
        "tec:omca-events:5002:2026-07-19T00:00:00+00:00",
        "tec:omca-events:5003:2026-07-19T00:00:00+00:00",
    ]
    assert [candidate.price_status for candidate in candidates] == [
        PriceStatus.FREE,
        PriceStatus.PAID,
        PriceStatus.UNKNOWN,
    ]
    free = candidates[0]
    assert free.registration_url == "https://museumca.org/event/friday-nights-5001/"
    assert free.venue_name == "OMCA campus"
    assert free.city is None
    assert free.geo is None
    assert free.description == "Live music and community."
    assert free.end_at == datetime(2026, 7, 19, 4, 0, tzinfo=UTC)
    assert len(requested) == 1
    assert requested[0].path == "/wp-json/tribe/events/v1/events"
    assert dict(requested[0].params) == {
        "start_date": "2026-07-17",
        "end_date": "2026-10-15",
        "per_page": "50",
        "page": "1",
        "status": "publish",
    }


async def test_tribe_completes_declared_pages_with_per_host_pacing() -> None:
    """The adapter owns fixed page URLs and verifies a finite source-declared total (NFR-8/FR-10.4)."""
    requested: list[httpx.URL] = []
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    first_page = [_event(6000 + index) for index in range(50)]

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        if request.url.params["page"] == "1":
            payload = _page(first_page, total=51, total_pages=2)
        else:
            payload = _page([_event(6050)], total=51, total_pages=2)
        return httpx.Response(200, json=payload, request=request)

    fetcher = TribeEventsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert len(candidates) == 51
    assert [request.params["page"] for request in requested] == ["1", "2"]
    assert slept == [1.5]


async def test_tribe_fails_closed_on_a_declared_page_cap_overflow() -> None:
    """A source that exceeds its reviewed page ceiling cannot enter the catalog partially (NFR-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page([_event(7001)], total=6, total_pages=6),
            request=request,
        )

    fetcher = TribeEventsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(TribeEventsFetchError, match="exceeds"):
        await fetcher.fetch(_source(page_limit=5))


async def test_tribe_refuses_an_unreviewed_source_key_before_requesting() -> None:
    """The typed adapter cannot be widened into a generic arbitrary The Events Calendar client (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=_page([], total=0, total_pages=0), request=request)

    source = CatalogSource(
        source_key="unreviewed-tribe-events",
        display_name="Unreviewed The Events Calendar",
        publisher="Events Concierge tests",
        seed_url="https://museumca.org/wp-json/tribe/events/v1/events",
        approved_origins=("https://museumca.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.TRIBE_EVENTS_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    fetcher = TribeEventsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed public events"):
        await fetcher.fetch(source)
    assert requested == []
