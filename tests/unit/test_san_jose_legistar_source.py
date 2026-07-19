"""San José Legistar adapter tests (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.legistar.source import (
    SanJoseLegistarCatalogFetcher,
    SanJoseLegistarFetchError,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.ports.sources import SourceAccessDeniedError, SourceRateLimitedError


def _source(*, page_limit: int = 5) -> CatalogSource:
    return CatalogSource(
        source_key="san-jose-legistar-meetings",
        display_name="San José Public Meetings",
        publisher="City of San José City Clerk",
        seed_url="https://webapi.legistar.com/v1/SanJose/Events",
        approved_origins=("https://webapi.legistar.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.SAN_JOSE_LEGISTAR,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=page_limit,
    )


def _event(
    event_id: int,
    *,
    title: str = "Civil Service Commission",
    event_date: str = "2026-07-20T00:00:00",
    event_time: str = "5:30 PM",
    location: str = "City Hall Tower, 14th Floor, Room T-1446",
    comment: str | None = "Special Meeting / Hearing",
    handoff_url: str | None = None,
) -> dict[str, object]:
    return {
        "EventId": event_id,
        "EventGuid": "A0A0A0A0-0000-4000-8000-000000000001",
        "EventBodyName": title,
        "EventDate": event_date,
        "EventTime": event_time,
        "EventLocation": location,
        "EventComment": comment,
        "EventAgendaStatusName": "Final",
        "EventInSiteURL": handoff_url
        or (
            "https://SanJose.legistar.com/MeetingDetail.aspx?"
            f"LEGID={event_id}&GID=317&G=920296E4-80BE-4CA2-A78F-32C5EFCF78AF"
        ),
    }


async def test_san_jose_legistar_maps_physical_and_hybrid_meetings_but_excludes_nonlocal_records() -> (
    None
):
    """Only future physical/hybrid meetings survive with source truth and no invented city or price."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            json=[
                _event(1001),
                _event(
                    1002,
                    location="Council Chambers with virtual viewing available",
                    comment="Public hybrid meeting",
                ),
                _event(1003, location="Virtual Meeting - https://sanjoseca.zoom.us/j/123"),
                _event(1004, location="Meeting Cancelled", comment="CANCELLED"),
                _event(1005, event_date="2026-07-17T00:00:00", event_time="9:00 AM"),
                _event(
                    1006,
                    handoff_url="https://unapproved.example.test/MeetingDetail.aspx?LEGID=1006",
                ),
                _event(
                    1007,
                    handoff_url=(
                        "https://sanjose.legistar.com/MeetingDetail.aspx?"
                        "LEGID=9999&GID=317&G=920296E4-80BE-4CA2-A78F-32C5EFCF78AF"
                    ),
                ),
                _event(1008, event_time="after lunch"),
                _event(1009, title="City Council Closed Session"),
            ],
            request=request,
        )

    fetcher = SanJoseLegistarCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 19, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "san-jose-legistar:san-jose-legistar-meetings:1001:2026-07-20T17:30:00-07:00",
        "san-jose-legistar:san-jose-legistar-meetings:1002:2026-07-20T17:30:00-07:00",
    ]
    assert candidates[0].title == "Civil Service Commission"
    assert candidates[0].description == "Special Meeting / Hearing"
    assert candidates[0].registration_url == (
        "https://sanjose.legistar.com/MeetingDetail.aspx?"
        "LEGID=1001&GID=317&G=920296E4-80BE-4CA2-A78F-32C5EFCF78AF"
    )
    assert candidates[0].venue_name == "City Hall Tower, 14th Floor, Room T-1446"
    assert candidates[0].city is None
    assert candidates[0].end_at is None
    assert all(candidate.price_status is PriceStatus.UNKNOWN for candidate in candidates)
    assert len(requested) == 1


async def test_san_jose_legistar_constructs_fixed_pages_and_applies_per_host_pacing() -> None:
    """The adapter owns the date filter/order/offset sequence and does not trust remote paging (FR-10.3)."""
    requested: list[httpx.URL] = []
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    first_page = [_event(2000 + offset) for offset in range(100)]

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        if request.url.params["$skip"] == "0":
            return httpx.Response(200, json=first_page, request=request)
        return httpx.Response(200, json=[_event(2100)], request=request)

    fetcher = SanJoseLegistarCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert len(candidates) == 101
    assert [request.params["$skip"] for request in requested] == ["0", "100"]
    assert all(request.params["$top"] == "100" for request in requested)
    assert all(request.params["$orderby"] == "EventDate asc,EventId asc" for request in requested)
    assert all(
        request.params["$filter"]
        == (
            "EventDate ge datetime'2026-07-17T00:00:00' and "
            "EventDate lt datetime'2026-10-15T00:00:00'"
        )
        for request in requested
    )
    assert slept == [1.5]


async def test_san_jose_legistar_paged_fetch_performs_one_exact_get_without_local_sleep() -> None:
    """P15b owns the Pacer lease; the adapter reads only its requested offset page (ADR-003/005)."""
    requested: list[httpx.URL] = []
    slept: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, json=[_event(2200)], request=request)

    async def sleep(delay: float) -> None:
        slept.append(delay)

    fetcher = SanJoseLegistarCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        clock=lambda: 100.0,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    page = await fetcher.fetch_page(
        _source(), window_start_day=datetime(2026, 7, 17, tzinfo=UTC).date(), page_number=1
    )

    assert page.page_number == 1
    assert page.raw_count == 1
    assert page.source_event_ids == ("2200",)
    assert len(page.candidates) == 1
    assert [request.params["$skip"] for request in requested] == ["100"]
    assert slept == []


async def test_san_jose_legistar_normalizes_429_and_403_for_p15b_policy_boundaries() -> None:
    """A one-page P15b adapter call exposes throttle/ban signals instead of opaque HTTP errors."""
    responses = [
        httpx.Response(429, headers={"Retry-After": "45"}),
        httpx.Response(403),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        response.request = request
        return response

    fetcher = SanJoseLegistarCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SourceRateLimitedError, match="HTTP 429") as throttled:
        await fetcher.fetch_page(
            _source(), window_start_day=datetime(2026, 7, 18).date(), page_number=0
        )
    with pytest.raises(SourceAccessDeniedError):
        await fetcher.fetch_page(
            _source(), window_start_day=datetime(2026, 7, 18).date(), page_number=0
        )

    assert throttled.value.retry_after_seconds == 45.0


async def test_san_jose_legistar_rejects_unapproved_redirects_before_requesting_the_target() -> (
    None
):
    """The reviewed public API cannot bounce the catalog worker onto another origin (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/events"},
            request=request,
        )

    fetcher = SanJoseLegistarCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(SanJoseLegistarFetchError, match="approved endpoint"):
        await fetcher.fetch(_source())
    assert len(requested) == 1


async def test_san_jose_legistar_fails_closed_when_the_page_cap_would_truncate_results() -> None:
    """A full final page is retryable rather than a silent partial city-calendar refresh (NFR-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[_event(3000 + offset) for offset in range(100)], request=request
        )

    fetcher = SanJoseLegistarCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(SanJoseLegistarFetchError, match="exceeds"):
        await fetcher.fetch(_source(page_limit=1))


async def test_san_jose_legistar_fails_closed_on_repeated_event_identity_across_pages() -> None:
    """A repeated publisher id signals unstable pagination and prevents a partial catalog write (NFR-8)."""

    first_page = [_event(4000 + offset) for offset in range(100)]

    async def handler(request: httpx.Request) -> httpx.Response:
        records = first_page if request.url.params["$skip"] == "0" else [_event(4000)]
        return httpx.Response(200, json=records, request=request)

    fetcher = SanJoseLegistarCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(SanJoseLegistarFetchError, match="repeated"):
        await fetcher.fetch(_source(page_limit=2))


async def test_san_jose_legistar_refuses_an_unreviewed_endpoint_before_a_request() -> None:
    """An approved origin alone cannot repurpose the typed adapter to another Legistar resource (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=[], request=request)

    source = CatalogSource(
        source_key="wrong-legistar-endpoint",
        display_name="Wrong Legistar endpoint",
        publisher="Events Concierge tests",
        seed_url="https://webapi.legistar.com/v1/SanJose/Bodies",
        approved_origins=("https://webapi.legistar.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.SAN_JOSE_LEGISTAR,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    fetcher = SanJoseLegistarCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(ValueError, match="reviewed public Events"):
        await fetcher.fetch(source)
    assert requested == []
