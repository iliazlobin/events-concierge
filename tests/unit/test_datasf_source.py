"""DataSF Our415 adapter tests (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from events_concierge.adapters.datasf.source import DataSfFetchError, DataSfOur415CatalogFetcher
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus


def _source(*, page_limit: int = 25) -> CatalogSource:
    return CatalogSource(
        source_key="datasf-our415-events",
        display_name="DataSF Our415 Activities",
        publisher="City and County of San Francisco",
        seed_url="https://data.sfgov.org/resource/8i3s-ih2a.json",
        approved_origins=("https://data.sfgov.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.DATASF_OUR415,
        enabled=True,
        reviewed_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=page_limit,
    )


async def test_datasf_materializes_bounded_recurring_occurrences_and_preserves_source_truth() -> (
    None
):
    """Recurring and one-off public rows become local handoff occurrences without price guessing."""
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
        if request.url.params["$select"] == "count(*) as total":
            return httpx.Response(200, json=[{"total": "6"}], request=request)
        return httpx.Response(
            200,
            json=[
                {
                    ":id": "row-family",
                    "id": "44976_rpd",
                    "event_name": "<b>Drop-in: Family Swim</b>",
                    "event_description": "<p>Public <em>family</em> swim.</p>",
                    "event_start_date": "2026-06-30T00:00:00.000",
                    "event_end_date": "2026-08-15T00:00:00.000",
                    "days_of_week": "Tue-Sat",
                    "start_time": "13:00:00",
                    "end_time": "01:00:00",
                    "more_info": "sfrecpark.org/register",
                    "fee": True,
                    "site_location_name": "SAVA SWIMMING POOL",
                    "site_address": "2699 19th Ave",
                    "point": {"type": "Point", "coordinates": [-122.47539263, 37.737520575]},
                },
                {
                    ":id": "row-duplicate",
                    "id": "44976_rpd",
                    "event_name": "Second program sharing a publisher id",
                    "event_start_date": "2026-07-20T00:00:00.000",
                    "event_end_date": "2026-07-20T00:00:00.000",
                    "days_of_week": "M",
                    "start_time": "09:00:00",
                    "end_time": "10:00:00",
                    "more_info": "https://example.test/duplicate",
                    "fee": False,
                },
                {
                    ":id": "row-storytime",
                    "id": "138610_sfpl",
                    "event_name": "Storytime: For Toddlers",
                    "event_start_date": "2026-08-04T00:00:00.000",
                    "event_end_date": "2026-08-04T00:00:00.000",
                    "start_time": "10:30:00",
                    "end_time": "11:00:00",
                    "more_info": "https://sfpl.org/events/2026/08/04/storytime-toddlers-7",
                    "fee": False,
                    "site_location_name": "Parkside",
                    "latitude": "37.743093",
                    "longitude": "-122.479407",
                },
                {
                    ":id": "row-long",
                    "id": "long-series",
                    "event_name": "Ninety-day boundary",
                    "event_start_date": "2026-07-01T00:00:00.000",
                    "event_end_date": "2026-12-01T00:00:00.000",
                    "days_of_week": "T",
                    "start_time": "09:00:00",
                    "end_time": "10:00:00",
                    "more_info": "https://example.test/long-series",
                    "fee": None,
                },
                {
                    ":id": "row-tbd",
                    "id": "unparseable-series",
                    "event_name": "TBD series",
                    "event_start_date": "2026-07-16T00:00:00.000",
                    "event_end_date": "2026-08-01T00:00:00.000",
                    "days_of_week": "TBD",
                    "start_time": "12:00:00",
                    "more_info": "https://example.test/tbd",
                },
                {
                    ":id": "row-unsafe-url",
                    "id": "unsafe-handoff",
                    "event_name": "Unsafe handoff",
                    "event_start_date": "2026-07-18T00:00:00.000",
                    "event_end_date": "2026-07-18T00:00:00.000",
                    "start_time": "12:00:00",
                    "more_info": "https://[malformed",
                },
            ],
            request=request,
        )

    fetcher = DataSfOur415CatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    family = candidates[0]
    assert family.source_event_id == (
        "datasf-our415:datasf-our415-events:44976_rpd:row-family:2026-07-16T13:00:00-07:00"
    )
    assert family.title == "Drop-in: Family Swim"
    assert family.description == "Public family swim."
    assert family.registration_url == "https://sfrecpark.org/register"
    assert family.price_status is PriceStatus.PAID
    assert family.venue_name == "SAVA SWIMMING POOL"
    assert family.geo is not None
    assert (family.geo.lat, family.geo.lon) == (37.737520575, -122.47539263)
    assert family.end_at is None
    assert family.start_at.tzinfo == ZoneInfo("America/Los_Angeles")
    assert any("44976_rpd:row-duplicate" in candidate.source_event_id for candidate in candidates)
    storytime = next(
        candidate for candidate in candidates if candidate.title == "Storytime: For Toddlers"
    )
    assert storytime.price_status is PriceStatus.FREE
    assert storytime.geo is not None
    assert (storytime.geo.lat, storytime.geo.lon) == (37.743093, -122.479407)
    assert all(candidate.title != "TBD series" for candidate in candidates)
    assert all(candidate.title != "Unsafe handoff" for candidate in candidates)
    long_series = [
        candidate for candidate in candidates if candidate.title == "Ninety-day boundary"
    ]
    assert long_series[-1].start_at.date().isoformat() == "2026-10-13"
    assert requested[0].params["$select"] == "count(*) as total"
    assert requested[0].params["$where"] == (
        "event_start_date < '2026-10-14T00:00:00.000' AND "
        "(event_end_date >= '2026-07-16T00:00:00.000' OR "
        "(event_end_date IS NULL AND event_start_date >= '2026-07-16T00:00:00.000'))"
    )
    assert requested[1].params["$order"] == ":id ASC"
    assert requested[1].params["$limit"] == "100"
    assert requested[1].params["$offset"] == "0"
    assert ":id" in requested[1].params["$select"].split(",")
    assert slept == [1.5]


async def test_datasf_rejects_a_redirect_before_requesting_an_unapproved_target() -> None:
    """The Socrata resource cannot bounce a reviewed worker to another host (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/events"},
            request=request,
        )

    fetcher = DataSfOur415CatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(DataSfFetchError, match="approved endpoint"):
        await fetcher.fetch(_source())
    assert len(requested) == 1
    assert requested[0].startswith("https://data.sfgov.org/resource/8i3s-ih2a.json?")


async def test_datasf_fails_closed_when_the_raw_page_cap_would_truncate_results() -> None:
    """A count over the reviewed capacity is retryable, never a silent partial ingest (NFR-8)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=[{"total": "101"}], request=request)

    fetcher = DataSfOur415CatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(DataSfFetchError, match="exceeds"):
        await fetcher.fetch(_source(page_limit=1))
    assert len(requested) == 1


async def test_datasf_fails_closed_on_a_short_page() -> None:
    """The count and every offset page must agree before the adapter returns candidates (NFR-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["$select"] == "count(*) as total":
            return httpx.Response(200, json=[{"total": "2"}], request=request)
        return httpx.Response(
            200,
            json=[
                {
                    ":id": "row-only",
                    "id": "one",
                    "event_name": "Only row",
                    "event_start_date": "2026-07-18T00:00:00.000",
                    "event_end_date": "2026-07-18T00:00:00.000",
                    "start_time": "09:00:00",
                    "more_info": "https://example.test/one",
                }
            ],
            request=request,
        )

    fetcher = DataSfOur415CatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(DataSfFetchError, match="short"):
        await fetcher.fetch(_source())


async def test_datasf_fails_closed_when_occurrence_expansion_exceeds_its_cap() -> None:
    """A large series never returns a partially expanded candidate list (FR-3.7/NFR-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["$select"] == "count(*) as total":
            return httpx.Response(200, json=[{"total": "1"}], request=request)
        return httpx.Response(
            200,
            json=[
                {
                    ":id": "row-cap",
                    "id": "cap-series",
                    "event_name": "Cap series",
                    "event_start_date": "2026-07-16T00:00:00.000",
                    "event_end_date": "2026-07-17T00:00:00.000",
                    "days_of_week": "Th,F",
                    "start_time": "13:00:00",
                    "more_info": "https://example.test/cap",
                }
            ],
            request=request,
        )

    fetcher = DataSfOur415CatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
        max_occurrences=1,
    )

    with pytest.raises(DataSfFetchError, match="occurrence cap"):
        await fetcher.fetch(_source())


async def test_datasf_rejects_an_unreviewed_endpoint_shape_before_a_request() -> None:
    """The typed Socrata fetcher cannot be repurposed to an arbitrary DataSF resource (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=[], request=request)

    source = CatalogSource(
        source_key="wrong-datasf-endpoint",
        display_name="Wrong DataSF endpoint",
        publisher="Events Concierge tests",
        seed_url="https://data.sfgov.org/resource/other.json",
        approved_origins=("https://data.sfgov.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.DATASF_OUR415,
        enabled=True,
        reviewed_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    fetcher = DataSfOur415CatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed Our415"):
        await fetcher.fetch(source)
    assert requested == []
