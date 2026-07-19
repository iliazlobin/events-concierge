"""Oakland Legistar adapter coverage (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from events_concierge.adapters.legistar.source import LegistarCatalogFetcher, LegistarFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.ports.sources import SourceAccessDeniedError, SourceRateLimitedError

_SEED_URL = "https://webapi.legistar.com/v1/Oakland/Events"
_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "sources" / "oakland_legistar_events.json"
_NOW = datetime(2026, 7, 17, 19, 0, tzinfo=UTC)


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="oakland-legistar-meetings",
        display_name="Oakland Public Meetings",
        publisher="City of Oakland City Clerk",
        seed_url=_SEED_URL,
        approved_origins=("https://webapi.legistar.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.OAKLAND_LEGISTAR,
        enabled=True,
        reviewed_at=_NOW,
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=5,
    )


def _fixture_records() -> list[dict[str, object]]:
    """Load an API-shaped, no-network Oakland event page fixture (FR-3.7)."""
    payload: object = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise AssertionError("Oakland Legistar fixture must be a JSON list")
    records: list[dict[str, object]] = []
    for raw_record in payload:
        if not isinstance(raw_record, dict):
            raise AssertionError("Oakland Legistar fixture must contain objects")
        record: dict[str, object] = {}
        for key, value in raw_record.items():
            if not isinstance(key, str):
                raise AssertionError("Oakland Legistar fixture keys must be strings")
            record[key] = value
        records.append(record)
    return records


async def test_oakland_legistar_maps_physical_and_hybrid_fixture_meetings_only() -> None:
    """Keep source-physical/hybrid meetings and reject known virtual-label variants (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, json=_fixture_records(), request=request)

    fetcher = LegistarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "oakland-legistar:oakland-legistar-meetings:9560:2026-07-21T15:30:00-07:00",
        "oakland-legistar:oakland-legistar-meetings:9561:2026-07-22T09:30:00-07:00",
    ]
    first, second = candidates
    assert (
        first.title
        == "Concurrent Meeting of the Oakland Redevelopment Successor Agency and the City Council"
    )
    assert first.registration_url == (
        "https://oakland.legistar.com/MeetingDetail.aspx?LEGID=9560&GID=134&"
        "G=15529D0E-EFE8-4C09-817E-CE554309072E"
    )
    assert first.venue_name == "City Council Chamber, 3rd Floor"
    assert first.description == "Regular public meeting"
    assert second.venue_name == "City Council Chamber, 3rd Floor / Tele-Conference"
    assert second.description == "Hybrid public meeting"
    assert all(candidate.price_status is PriceStatus.UNKNOWN for candidate in candidates)
    assert len(requested) == 1
    assert requested[0].path == "/v1/Oakland/Events"
    assert requested[0].params["$filter"] == (
        "EventDate ge datetime'2026-07-17T00:00:00' and EventDate lt datetime'2026-10-15T00:00:00'"
    )
    assert requested[0].params["$orderby"] == "EventDate asc,EventId asc"
    assert requested[0].params["$top"] == "100"
    assert requested[0].params["$skip"] == "0"


async def test_oakland_paged_call_preserves_filtered_remote_ids_and_uses_no_local_sleep() -> None:
    """P15e sends one exact page after its shared Pacer lease (NFR-1/NFR-8, ADR-003/005)."""
    requested: list[httpx.URL] = []
    slept: list[float] = []
    responses = [
        httpx.Response(200, json=_fixture_records()),
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
        now=lambda: _NOW,
        clock=lambda: 0.0,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    page = await fetcher.fetch_page(_source(), window_start_day=_NOW.date(), page_number=1)

    assert page.page_number == 1
    assert page.raw_count == 15
    assert page.source_event_ids == (
        "9560",
        "9561",
        "9562",
        "9563",
        "9564",
        "9565",
        "9566",
        "9567",
        "9568",
        "9569",
        "9570",
        "9571",
        "9572",
        "9573",
    )
    assert [candidate.source_event_id for candidate in page.candidates] == [
        "oakland-legistar:oakland-legistar-meetings:9560:2026-07-21T15:30:00-07:00",
        "oakland-legistar:oakland-legistar-meetings:9561:2026-07-22T09:30:00-07:00",
    ]
    assert requested[0].params["$skip"] == "100"
    assert slept == []

    with pytest.raises(SourceRateLimitedError, match="HTTP 429") as throttled:
        await fetcher.fetch_page(_source(), window_start_day=_NOW.date(), page_number=0)
    with pytest.raises(SourceAccessDeniedError):
        await fetcher.fetch_page(_source(), window_start_day=_NOW.date(), page_number=0)
    with pytest.raises(LegistarFetchError, match="429"):
        await fetcher.fetch_page(_source(), window_start_day=_NOW.date(), page_number=0)

    assert throttled.value.retry_after_seconds == 45.0


async def test_oakland_legistar_refuses_a_tampered_profile_before_requesting() -> None:
    """Registry data cannot repurpose Oakland's closed API/client profile (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=[], request=request)

    fetcher = LegistarCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))
    unsafe_sources = (
        replace(_source(), source_key="unreviewed-oakland-legistar"),
        replace(_source(), seed_url="https://webapi.legistar.com/v1/OaklandCA/Events"),
        replace(_source(), seed_url=f"{_SEED_URL}?unreviewed=true"),
        replace(_source(), mode=CatalogSourceMode.ALAMEDA_LEGISTAR),
        replace(_source(), handoff_only=False),
    )

    for unsafe_source in unsafe_sources:
        with pytest.raises(ValueError, match="Legistar"):
            await fetcher.fetch(unsafe_source)
    assert requested == []
