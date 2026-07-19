"""University of San Francisco Main Campus adapter coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from events_concierge.adapters.usfca.source import UsfcaCatalogFetcher, UsfcaFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_SEED_URL = "https://www.usfca.edu/life-at-usf/events?field_campus%5B179%5D=179"
_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "usfca" / "main_campus_events.html"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="usfca-main-campus-events",
        display_name="University of San Francisco Main Campus Events",
        publisher="University of San Francisco",
        seed_url=_SEED_URL,
        approved_origins=("https://www.usfca.edu",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.USFCA_HTML,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=1,
    )


def _fixture() -> str:
    return _FIXTURE_PATH.read_text(encoding="utf-8")


async def test_usfca_maps_only_future_timed_physical_cards_without_fetching_details() -> None:
    """The one approved list produces handoffs but makes no event-detail or application requests (FR-3.1/3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, text=_fixture(), request=request)

    fetcher = UsfcaCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 18, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "usfca:usfca-main-campus-events:48157:2026-07-19T01:30:00+00:00"
    ]
    candidate = candidates[0]
    assert candidate.title == "Generative AI Career Night"
    assert candidate.registration_url == (
        "https://www.usfca.edu/event/generative-ai-career-night/48157"
    )
    assert candidate.start_at == datetime(2026, 7, 19, 1, 30, tzinfo=UTC)
    assert candidate.end_at == datetime(2026, 7, 19, 3, 0, tzinfo=UTC)
    assert candidate.venue_name == "Fromm Hall 120"
    assert candidate.city is None
    assert candidate.geo is None
    assert candidate.description == ""
    assert candidate.price_status is PriceStatus.UNKNOWN
    assert candidate.raw == {
        "path": "/event/generative-ai-career-night/48157",
        "time_string": "July 18, 2026 6:30PM - 8:00PM",
        "location": "Fromm Hall 120",
    }
    assert requested == [httpx.URL(_SEED_URL)]


async def test_usfca_rejects_tampered_profile_redirect_and_new_pagination() -> None:
    """Registry data and a changed one-page contract cannot widen a Main Campus refresh (FR-10.3/NFR-8)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_fixture(), request=request)

    fetcher = UsfcaCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))
    for unsafe_source in (
        replace(_source(), source_key="unreviewed-usfca-events"),
        replace(_source(), seed_url=f"{_SEED_URL}&audience=public"),
        replace(_source(), page_limit=2),
    ):
        with pytest.raises(ValueError, match="reviewed Main Campus events endpoint"):
            await fetcher.fetch(unsafe_source)
    assert requested == []

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/events"},
            request=request,
        )

    redirect_fetcher = UsfcaCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(redirect_handler)
    )
    with pytest.raises(UsfcaFetchError, match="approved Main Campus endpoint"):
        await redirect_fetcher.fetch(_source())

    async def paged_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="""
            <div class=\"cc--events-listing\">
              <article><h2 class=\"f--cta-title\"><a href=\"/event/future-talk/48165\">Future Talk</a></h2>
              <p class=\"f--time-string\">July 20, 2026 3:00PM - 4:00PM</p>
              <p class=\"event-location\">Cowell Hall</p></article>
              <nav class=\"pager\"><a href=\"?page=1\">Next</a></nav>
            </div>
            """,
            request=request,
        )

    paged_fetcher = UsfcaCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(paged_handler)
    )
    with pytest.raises(UsfcaFetchError, match="unreviewed pagination"):
        await paged_fetcher.fetch(_source())


async def test_usfca_paces_repeated_list_reads_at_the_reviewed_floor() -> None:
    """Every anonymous list GET shares the per-host human-cadence floor (FR-10.4)."""
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
        return httpx.Response(200, text=_fixture(), request=request)

    fetcher = UsfcaCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 18, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    await fetcher.fetch(_source())
    await fetcher.fetch(_source())

    assert requested == [httpx.URL(_SEED_URL), httpx.URL(_SEED_URL)]
    assert slept == [1.5]
