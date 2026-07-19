"""Berkeley Public Library Communico adapter tests (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.communico.source import CommunicoCatalogFetcher, CommunicoFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="berkeley-public-library-events",
        display_name="Berkeley Public Library Events",
        publisher="Berkeley Public Library",
        seed_url="https://berkeleypubliclibrary.libnet.info/eeventcaldata",
        approved_origins=("https://berkeleypubliclibrary.libnet.info",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.COMMUNICO_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
    )


def _event(
    identifier: int,
    *,
    title: str = "Community Board Game Night",
    start: str = "2026-07-18 18:30:00",
    end: str = "2026-07-18 20:30:00",
    event_type: str = "INPERSON",
    library: str | None = "West Branch",
    location: str | None = "West Branch",
    venues: str | None = "Community Meeting Room",
    description: str = "<p>Play <em>together</em>.</p>",
    private: object = "0",
    changed_reason: str | None = None,
) -> dict[str, object]:
    return {
        "id": str(identifier),
        "new_event_id": None,
        "title": title,
        "raw_start_time": start,
        "raw_end_time": end,
        "event_type": event_type,
        "library": library,
        "location": location,
        "venues": venues,
        "description": description,
        "long_description": "<p>Longer publisher description.</p>",
        "private_event": private,
        "changed_reason": changed_reason,
        "url": f"https://berkeleypubliclibrary.libnet.info/event/{identifier}",
        "allow_reg": "1",
        "registration_cost": "0",
    }


async def test_communico_maps_only_future_physical_or_hybrid_library_events() -> None:
    """The fixed source-list request retains source truth and never trusts registration fields (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            json=[
                _event(1001),
                _event(
                    1002,
                    title="All-Day Community Art",
                    start="2026-07-19 00:00:00",
                    end="2026-07-19 23:59:00",
                    event_type="HYBRID",
                    library="Central Library",
                    venues=None,
                ),
                _event(1003, event_type="ONLINE"),
                _event(1004, private=True),
                _event(1005, title="Community Board Game Night Cancelled"),
                _event(1006, library=None, location=None),
                _event(1007, start="2026-07-17 09:00:00", end="2026-07-17 10:00:00"),
                _event(1008, start="2026-10-15 01:00:00", end="2026-10-15 02:00:00"),
                _event(1009, start="not a timestamp"),
                _event(1001),
            ],
            request=request,
        )

    fetcher = CommunicoCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 19, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "communico:berkeley-public-library-events:1001:2026-07-18T18:30:00-07:00",
        "communico:berkeley-public-library-events:1002:2026-07-19T00:00:00-07:00",
    ]
    physical, all_day = candidates
    assert physical.registration_url == "https://berkeleypubliclibrary.libnet.info/event/1001"
    assert physical.description == "Play together."
    assert physical.venue_name == "West Branch \u2014 Community Meeting Room"
    assert physical.city is None
    assert physical.geo is None
    assert physical.price_status is PriceStatus.UNKNOWN
    assert all_day.end_at == datetime(2026, 7, 19, 23, 59, tzinfo=all_day.start_at.tzinfo)
    assert len(requested) == 1
    assert requested[0].path == "/eeventcaldata"
    assert requested[0].params["event_type"] == "0"
    assert json.loads(requested[0].params["req"]) == {
        "private": False,
        "date": "2026-07-17",
        "days": 91,
    }


async def test_communico_fails_closed_on_a_capped_unpaged_result() -> None:
    """A source response at the reviewed row ceiling cannot be mistaken for a complete calendar (NFR-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[_event(index + 1) for index in range(600)], request=request
        )

    fetcher = CommunicoCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(CommunicoFetchError, match="capped"):
        await fetcher.fetch(_source())


async def test_communico_refuses_an_unreviewed_source_key_before_requesting() -> None:
    """The Communico adapter cannot be widened into a generic arbitrary-library client (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=[], request=request)

    source = CatalogSource(
        source_key="unreviewed-communico-library",
        display_name="Unreviewed Communico Library",
        publisher="Events Concierge tests",
        seed_url="https://berkeleypubliclibrary.libnet.info/eeventcaldata",
        approved_origins=("https://berkeleypubliclibrary.libnet.info",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.COMMUNICO_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    fetcher = CommunicoCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed public events"):
        await fetcher.fetch(source)
    assert requested == []
