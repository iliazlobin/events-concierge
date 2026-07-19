"""Sunnyvale Legistar adapter coverage (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.legistar.source import LegistarCatalogFetcher, LegistarFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.ports.sources import SourceAccessDeniedError, SourceRateLimitedError


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="sunnyvale-legistar-meetings",
        display_name="Sunnyvale Public Meetings",
        publisher="City of Sunnyvale City Clerk",
        seed_url="https://webapi.legistar.com/v1/SunnyvaleCA/Events",
        approved_origins=("https://webapi.legistar.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.SUNNYVALE_LEGISTAR,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=5,
    )


def _event(
    event_id: int,
    *,
    title: str = "City Council",
    event_date: str = "2026-07-20T00:00:00",
    event_time: str = "7:00 PM",
    location: str = "Council Chambers, 456 W Olive Avenue",
    comment: str | None = "Regular Meeting",
    agenda_status: str = "Final",
    handoff_url: str | None = None,
) -> dict[str, object]:
    return {
        "EventId": event_id,
        "EventGuid": "B0B0B0B0-0000-4000-8000-000000000001",
        "EventBodyName": title,
        "EventDate": event_date,
        "EventTime": event_time,
        "EventLocation": location,
        "EventComment": comment,
        "EventAgendaStatusName": agenda_status,
        "EventInSiteURL": handoff_url
        or (
            "https://SunnyvaleCA.legistar.com/MeetingDetail.aspx?"
            f"LEGID={event_id}&GID=317&G=920296E4-80BE-4CA2-A78F-32C5EFCF78AF"
        ),
    }


async def test_sunnyvale_legistar_keeps_public_physical_and_hybrid_meetings_only() -> None:
    """Hidden, cancelled, virtual, past, and unsafe records never become public handoffs (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            json=[
                _event(5001),
                _event(
                    5002,
                    location="Online and Council Chambers, 456 W Olive Avenue",
                    comment="Public hybrid meeting",
                ),
                _event(5003, agenda_status="Hidden"),
                _event(5004, comment="Meeting Canceled"),
                _event(5005, location="Virtual Meeting - https://example.test/meeting"),
                _event(5006, event_date="2026-07-17T00:00:00", event_time="9:00 AM"),
                _event(
                    5007,
                    handoff_url="https://unapproved.example.test/MeetingDetail.aspx?LEGID=5007",
                ),
            ],
            request=request,
        )

    fetcher = LegistarCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 19, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "sunnyvale-legistar:sunnyvale-legistar-meetings:5001:2026-07-20T19:00:00-07:00",
        "sunnyvale-legistar:sunnyvale-legistar-meetings:5002:2026-07-20T19:00:00-07:00",
    ]
    assert candidates[0].registration_url == (
        "https://sunnyvaleca.legistar.com/MeetingDetail.aspx?"
        "LEGID=5001&GID=317&G=920296E4-80BE-4CA2-A78F-32C5EFCF78AF"
    )
    assert candidates[1].venue_name == "Online and Council Chambers, 456 W Olive Avenue"
    assert all(candidate.price_status is PriceStatus.UNKNOWN for candidate in candidates)
    assert len(requested) == 1
    assert requested[0].path == "/v1/SunnyvaleCA/Events"
    assert requested[0].params["$filter"] == (
        "EventDate ge datetime'2026-07-17T00:00:00' and EventDate lt datetime'2026-10-15T00:00:00'"
    )
    assert requested[0].params["$orderby"] == "EventDate asc,EventId asc"


async def test_sunnyvale_paged_call_preserves_filtered_remote_ids_and_uses_no_local_sleep() -> None:
    """P15c admits one exact page only after its shared Pacer lease (NFR-1/NFR-8, ADR-003/005)."""
    requested: list[httpx.URL] = []
    slept: list[float] = []
    responses = [
        httpx.Response(200, json=[_event(6001), _event(6002, agenda_status="Hidden")]),
        httpx.Response(429, headers={"Retry-After": "45"}),
        httpx.Response(403),
        httpx.Response(429),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        response = responses.pop(0)
        response.request = request
        return response

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    fetcher = LegistarCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        clock=lambda: 0.0,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    page = await fetcher.fetch_page(
        _source(), window_start_day=datetime(2026, 7, 18).date(), page_number=1
    )

    assert page.page_number == 1
    assert page.raw_count == 2
    assert page.source_event_ids == ("6001", "6002")
    assert [candidate.source_event_id for candidate in page.candidates] == [
        "sunnyvale-legistar:sunnyvale-legistar-meetings:6001:2026-07-20T19:00:00-07:00"
    ]
    assert requested[0].params["$skip"] == "100"
    assert slept == []

    with pytest.raises(SourceRateLimitedError, match="HTTP 429") as throttled:
        await fetcher.fetch_page(
            _source(), window_start_day=datetime(2026, 7, 18).date(), page_number=0
        )
    with pytest.raises(SourceAccessDeniedError):
        await fetcher.fetch_page(
            _source(), window_start_day=datetime(2026, 7, 18).date(), page_number=0
        )
    with pytest.raises(LegistarFetchError, match="429"):
        await fetcher.fetch_page(
            _source(), window_start_day=datetime(2026, 7, 18).date(), page_number=0
        )

    assert throttled.value.retry_after_seconds == 45.0


async def test_sunnyvale_legistar_refuses_an_unreviewed_source_key_before_requesting() -> None:
    """The shared adapter cannot be turned into an arbitrary Legistar client by registry data (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=[], request=request)

    source = CatalogSource(
        source_key="unreviewed-sunnyvale-legistar",
        display_name="Unreviewed Legistar source",
        publisher="Events Concierge tests",
        seed_url="https://webapi.legistar.com/v1/SunnyvaleCA/Events",
        approved_origins=("https://webapi.legistar.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.SUNNYVALE_LEGISTAR,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    fetcher = LegistarCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed public Events"):
        await fetcher.fetch(source)
    assert requested == []
