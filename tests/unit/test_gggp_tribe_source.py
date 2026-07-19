"""Gardens of Golden Gate Park The Events Calendar profile coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.tribe.source import TribeEventsCatalogFetcher
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_SEED_URL = "https://gggp.org/wp-json/tribe/events/v1/events"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="gardens-golden-gate-park-events",
        display_name="Gardens of Golden Gate Park Events",
        publisher="Gardens of Golden Gate Park",
        seed_url=_SEED_URL,
        approved_origins=("https://gggp.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.TRIBE_EVENTS_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=5,
    )


def _event(
    identifier: int,
    *,
    title: str = "Summer Garden Workshop",
    cost: str = "Free",
    cost_details: object = None,
    status: str = "publish",
    hide_from_listings: object = False,
    is_virtual: object = False,
    all_day: object = False,
    timezone: str = "America/Los_Angeles",
    start_date: str = "2026-07-18 13:30:00",
    end_date: str = "2026-07-18 15:00:00",
    utc_start_date: str = "2026-07-18 20:30:00",
    utc_end_date: str = "2026-07-18 22:00:00",
    venue_name: str | None = "Conservatory of Flowers",
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
        "url": url or f"https://gggp.org/event/summer-workshop-{identifier}/",
        "cost": cost,
        "cost_details": cost_details,
        "excerpt": "<p>Hands-on <em>garden</em> learning.</p>",
        "rest_url": f"https://gggp.org/wp-json/tribe/events/v1/events/{identifier}",
        "website": "https://tickets.example.test/gardens",
    }


def _page(events: list[dict[str, object]], *, total: int) -> dict[str, object]:
    return {"events": events, "total": total, "total_pages": 1}


async def test_gggp_profile_maps_physical_events_and_structured_price_ranges() -> None:
    """The reviewed source accepts only its host, handoffs, local timestamps, and price truth (FR-3.1/FR-3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            json=_page(
                [
                    _event(1001, cost_details={"values": ["0"]}),
                    _event(
                        1002,
                        cost="$55 \N{EN DASH} $75",
                        cost_details={"values": ["55", "75"]},
                    ),
                    _event(1003, cost="Free for Members"),
                    _event(1004, venue_name=None),
                    _event(1005, timezone="UTC+0"),
                    _event(1006, is_virtual=True),
                    _event(1007, all_day=True),
                    _event(1008, url="https://unapproved.example.test/event/unsafe/"),
                ],
                total=8,
            ),
            request=request,
        )

    fetcher = TribeEventsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "tec:gardens-golden-gate-park-events:1001:2026-07-18T20:30:00+00:00",
        "tec:gardens-golden-gate-park-events:1002:2026-07-18T20:30:00+00:00",
        "tec:gardens-golden-gate-park-events:1003:2026-07-18T20:30:00+00:00",
    ]
    assert [candidate.price_status for candidate in candidates] == [
        PriceStatus.FREE,
        PriceStatus.PAID,
        PriceStatus.UNKNOWN,
    ]
    first = candidates[0]
    assert first.registration_url == "https://gggp.org/event/summer-workshop-1001/"
    assert first.venue_name == "Conservatory of Flowers"
    assert first.city is None
    assert first.geo is None
    assert first.description == "Hands-on garden learning."
    assert first.end_at == datetime(2026, 7, 18, 22, 0, tzinfo=UTC)
    assert len(requested) == 1
    assert requested[0].path == "/wp-json/tribe/events/v1/events"
    assert dict(requested[0].params) == {
        "start_date": "2026-07-17",
        "end_date": "2026-10-15",
        "per_page": "50",
        "page": "1",
        "status": "publish",
    }


async def test_gggp_profile_refuses_a_tampered_or_unreviewed_seed_before_requesting() -> None:
    """Registry data cannot turn the closed profile into another publisher's The Events Calendar client (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=_page([], total=0), request=request)

    fetcher = TribeEventsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))
    unreviewed = replace(_source(), source_key="unreviewed-gardens-events")
    with pytest.raises(ValueError, match="reviewed public events"):
        await fetcher.fetch(unreviewed)

    tampered = replace(_source(), seed_url=f"{_SEED_URL}?page=2")
    with pytest.raises(ValueError, match="reviewed public events"):
        await fetcher.fetch(tampered)
    assert requested == []
