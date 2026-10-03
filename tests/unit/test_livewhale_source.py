"""LiveWhale public-catalog adapter tests (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.livewhale.source import LiveWhaleCatalogFetcher, LiveWhaleFetchError
from events_concierge.domain.catalog_sources import CatalogCollectionWindow, CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.domain.events import GeoPoint
from events_concierge.ports.sources import SourceTransientError

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_WINDOW_URL = (
    "https://events.example.test/live/json/events/start_date/2026-07-16/end_date/2026-10-15"
)


async def _no_sleep(_: float) -> None:
    pass


def _event(event_id: int = 201) -> dict[str, object]:
    return {
        "id": event_id,
        "title": "Public campus event",
        "url": f"/event/{event_id}",
        "date_iso": "2026-07-20T18:00:00-07:00",
    }


def _page(
    data: list[dict[str, object]],
    *,
    page: int = 1,
    total: int | None = None,
    per_page: int = 1,
    next_link: str | None = None,
) -> dict[str, object]:
    total = len(data) if total is None else total
    return {
        "meta": {
            "page": page,
            "total_results": total,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        },
        "data": data,
        "links": {"next": next_link},
    }


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
                    "meta": {"page": 2, "total_results": 7, "per_page": 6, "total_pages": 2},
                    "data": [
                        {
                            "id": 104,
                            "title": "Zero-cost workshop",
                            "url": "/event/zero-cost",
                            "date_iso": "2026-07-20T18:00:00-07:00",
                            "cost": 0,
                        }
                    ],
                    "links": {"next": None},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "meta": {"page": 1, "total_results": 7, "per_page": 6, "total_pages": 2},
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
                "links": {"next": "?page=2"},
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
        _WINDOW_URL,
        f"{_WINDOW_URL}?page=2",
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
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LiveWhaleFetchError, match="approved dated resource"):
        await fetcher.fetch(_source(page_limit=1))
    assert requested == [_WINDOW_URL]


async def test_livewhale_rejects_an_unapproved_next_page_without_requesting_it() -> None:
    """A pagination link is subject to the same origin allowlist as a redirect (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "meta": {"page": 1, "total_results": 2, "per_page": 1, "total_pages": 2},
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

    with pytest.raises(LiveWhaleFetchError, match="approved dated resource"):
        await fetcher.fetch(_source())
    assert requested == [_WINDOW_URL]


@pytest.mark.parametrize("failure", ["http", "timeout", "json", "shape", "missing_meta"])
@pytest.mark.parametrize("failed_page", [1, 2])
async def test_failed_page_never_returns_an_empty_or_partial_success(
    failure: str,
    failed_page: int,
) -> None:
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        requested.append(page)
        if page != failed_page:
            return httpx.Response(200, json=_page([_event()], total=2, next_link="?page=2"))
        if failure == "timeout":
            raise httpx.ReadTimeout("source timeout", request=request)
        if failure == "http":
            return httpx.Response(503)
        if failure == "json":
            return httpx.Response(200, content=b"not json")
        if failure == "shape":
            return httpx.Response(200, json={"data": {"unexpected": True}})
        return httpx.Response(200, json={"data": [], "links": {"next": None}})

    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    )
    expected_error = SourceTransientError if failure in {"http", "timeout"} else LiveWhaleFetchError
    with pytest.raises(expected_error) as raised:
        await fetcher.fetch(_source())
    if isinstance(raised.value, SourceTransientError):
        assert raised.value.retry_after_seconds == 30.0
    assert requested == list(range(1, failed_page + 1))


async def test_declared_page_total_over_cap_fails_before_fetching_extra_pages() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=_page([_event()], total=3, next_link="?page=2"))

    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LiveWhaleFetchError, match=r"2-page cap \(requires 3\)"):
        await fetcher.fetch(_source())
    assert requested == [_WINDOW_URL]


@pytest.mark.parametrize("failure", ["repeat", "wrong_page", "changed_total", "short_page"])
async def test_inconsistent_pagination_fails_instead_of_deduplicating_a_partial_feed(
    failure: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") is None:
            return httpx.Response(200, json=_page([_event()], total=2, next_link="?page=2"))
        payload = _page([_event(202)], page=2, total=2)
        if failure == "repeat":
            payload["data"] = [_event()]
        elif failure == "wrong_page":
            payload = _page([_event(202)], page=1, total=2)
        elif failure == "changed_total":
            payload = _page([_event(202)], page=2, total=3)
        else:
            payload["data"] = []
        return httpx.Response(200, json=payload)

    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LiveWhaleFetchError):
        await fetcher.fetch(_source())


@pytest.mark.parametrize("next_link", [None, "?page=1", "?page=3", "/live/json/events/?page=2"])
async def test_next_link_cannot_stop_early_repeat_skip_or_drop_the_window(
    next_link: str | None,
) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=_page([_event()], total=2, next_link=next_link))

    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LiveWhaleFetchError):
        await fetcher.fetch(_source())
    assert requested == [_WINDOW_URL]


async def test_valid_empty_feed_is_a_success() -> None:
    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_page([]))),
    )
    assert await fetcher.fetch(_source()) == []


async def test_frozen_window_controls_query_and_exact_candidate_bounds() -> None:
    window = CatalogCollectionWindow(
        source_revision=1,
        horizon_days=1,
        start_at=_NOW,
        end_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        attempt_count=1,
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        events = [
            dict(_event(i), date_iso=date)
            for i, date in enumerate(
                [
                    "2026-07-16T11:59:59Z",
                    "2026-07-16T12:00:00Z",
                    "2026-07-17T12:00:00Z",
                ]
            )
        ]
        return httpx.Response(200, json=_page(events, per_page=3))

    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 8, 1, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )
    candidates = await fetcher.fetch(replace(_source(), collection_window=window))
    assert requested == [
        "https://events.example.test/live/json/events/start_date/2026-07-16/end_date/2026-07-18"
    ]
    assert [candidate.start_at for candidate in candidates] == [_NOW]


async def test_response_size_limit_is_a_failure() -> None:
    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b" " * 2_000_001)),
    )
    with pytest.raises(LiveWhaleFetchError, match="response-size limit"):
        await fetcher.fetch(_source())
