"""SF.gov related-events adapter tests (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from events_concierge.adapters.sf_gov.source import SfGovCatalogFetcher, SfGovFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus


def _source(*, page_limit: int = 3) -> CatalogSource:
    return CatalogSource(
        source_key="sf-gov-related-events",
        display_name="SF.gov Citywide Events",
        publisher="City and County of San Francisco",
        seed_url=(
            "https://api.sf.gov/api/related-events/?list=upcoming&locale=en&page=1&groupby=date"
        ),
        approved_origins=("https://api.sf.gov",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.SF_GOV_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=page_limit,
    )


async def test_sf_gov_flattens_bounded_pages_and_maps_physical_hybrid_civic_events() -> None:
    """Only physical/hybrid future records survive, retaining official paths and price uncertainty."""
    requested: list[httpx.URL] = []
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        page = request.url.params["page"]
        pages: dict[str, list[dict[str, object]]] = {
            "1": [
                {
                    "id": 1,
                    "live": True,
                    "title": "<b>Physical civic event</b>",
                    "description": "<p>Official <em>description</em>.</p>",
                    "start_datetime": "2026-07-17T09:00:00-07:00",
                    "end_datetime": "2026-07-17T10:00:00-07:00",
                    "meta": {"url_path": "/home/event-physical"},
                    "location": [
                        {
                            "type": "address",
                            "value": {
                                "address_title": "City Hall",
                                "city": "San Francisco",
                            },
                        }
                    ],
                },
                {
                    "id": 2,
                    "live": True,
                    "title": "Remote-only meeting",
                    "start_datetime": "2026-07-17T10:00:00-07:00",
                    "meta": {"url_path": "/home/remote-only"},
                    "meeting_location": [{"type": "online", "value": {"description": "Zoom"}}],
                },
            ],
            "2": [
                {
                    "id": 3,
                    "live": 1,
                    "title": "Hybrid meeting",
                    "overview": "<p>Attend in person or online.</p>",
                    "start_datetime": "2026-07-18T10:00:00",
                    "end_datetime": "2026-07-18T11:00:00",
                    "meta": {"url_path": "/home/hybrid"},
                    "meeting_location": [
                        {
                            "type": "address",
                            "value": {
                                "location_name": "Civic Center",
                                "city": "San Francisco",
                            },
                        },
                        {"type": "online", "value": {"description": "WebEx"}},
                    ],
                },
                {
                    "id": 4,
                    "live": True,
                    "cancelled": True,
                    "title": "Cancelled meeting",
                    "start_datetime": "2026-07-18T12:00:00-07:00",
                    "meta": {"url_path": "/home/cancelled"},
                },
            ],
            "3": [
                {
                    "id": 5,
                    "live": True,
                    "title": "No declared end time",
                    "start_datetime": "2026-07-19T09:00:00-07:00",
                    "end_datetime": "2026-07-19T23:59:59-07:00",
                    "date_time": [
                        {
                            "type": "date_time",
                            "value": {"include_end_date_time": "no"},
                        }
                    ],
                    "meta": {"url_path": "/home/no-end"},
                    "location": [
                        {
                            "type": "address",
                            "value": {
                                "line1": "1 Dr Carlton B Goodlett Pl",
                                "city": "San Francisco",
                            },
                        }
                    ],
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "total": 5,
                "events": [{"year": 2026, "month": 7, "day": 17, "events": pages[page]}],
            },
            request=request,
        )

    fetcher = SfGovCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "sf-gov:sf-gov-related-events:1:2026-07-17T09:00:00-07:00",
        "sf-gov:sf-gov-related-events:3:2026-07-18T10:00:00-07:00",
        "sf-gov:sf-gov-related-events:5:2026-07-19T09:00:00-07:00",
    ]
    assert [candidate.price_status for candidate in candidates] == [
        PriceStatus.UNKNOWN,
        PriceStatus.UNKNOWN,
        PriceStatus.UNKNOWN,
    ]
    assert candidates[0].title == "Physical civic event"
    assert candidates[0].description == "Official description."
    assert candidates[0].registration_url == "https://www.sf.gov/event-physical"
    assert candidates[0].venue_name == "City Hall"
    assert candidates[0].city == "San Francisco"
    assert candidates[1].start_at.tzinfo == ZoneInfo("America/Los_Angeles")
    assert candidates[1].venue_name == "Civic Center"
    assert candidates[1].description == "Attend in person or online."
    assert candidates[2].end_at is None
    assert [request.params["page"] for request in requested] == ["1", "2", "3"]
    assert all(request.params["list"] == "upcoming" for request in requested)
    assert all(request.params["locale"] == "en" for request in requested)
    assert all(request.params["groupby"] == "date" for request in requested)
    assert slept == [1.5, 1.5]


async def test_sf_gov_rejects_an_unapproved_redirect_before_requesting_it() -> None:
    """The civic API cannot bounce a reviewed worker onto another origin (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/events"},
            request=request,
        )

    fetcher = SfGovCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(SfGovFetchError, match="allowlist"):
        await fetcher.fetch(_source(page_limit=1))
    assert requested == [
        "https://api.sf.gov/api/related-events/?list=upcoming&locale=en&groupby=date&page=1"
    ]


async def test_sf_gov_fails_closed_when_the_reviewed_page_cap_would_truncate_results() -> None:
    """A reported total beyond the configured cap is retryable, never silently partial (NFR-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total": 2,
                "events": [
                    {
                        "year": 2026,
                        "month": 7,
                        "day": 17,
                        "events": [
                            {
                                "id": 1,
                                "live": True,
                                "title": "First event",
                                "start_datetime": "2026-07-17T09:00:00-07:00",
                                "meta": {"url_path": "/home/first"},
                            }
                        ],
                    }
                ],
            },
            request=request,
        )

    fetcher = SfGovCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(SfGovFetchError, match="exceeds"):
        await fetcher.fetch(_source(page_limit=1))


async def test_sf_gov_rejects_an_unreviewed_endpoint_shape_before_a_request() -> None:
    """An origin allowlist alone cannot repurpose this typed adapter (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={"total": 0, "events": []}, request=request)

    source = CatalogSource(
        source_key="wrong-sf-gov-endpoint",
        display_name="Wrong SF.gov endpoint",
        publisher="Events Concierge tests",
        seed_url="https://api.sf.gov/api/other/?page=1",
        approved_origins=("https://api.sf.gov",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.SF_GOV_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    fetcher = SfGovCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed upcoming"):
        await fetcher.fetch(source)
    assert requested == []
