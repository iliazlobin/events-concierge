"""Cal Performances collection adapter coverage (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

import events_concierge.adapters.calperformances.source as calperformances_source
from events_concierge.adapters.calperformances.source import (
    CalPerformancesCatalogFetcher,
    CalPerformancesFetchError,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_SEED_URL = "https://calperformances.org/wp-json/wp/v2/cp_event?per_page=100&page=1"
_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "calperformances" / "collection_page.json"
_NOW = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="calperformances-events",
        display_name="Cal Performances Events",
        publisher="Cal Performances",
        seed_url=_SEED_URL,
        approved_origins=("https://calperformances.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.CAL_PERFORMANCES_JSON,
        enabled=True,
        reviewed_at=_NOW,
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=4,
    )


def _fixture_records() -> list[dict[str, object]]:
    payload: object = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise AssertionError("Cal Performances fixture must be a JSON list")
    records: list[dict[str, object]] = []
    for raw_record in payload:
        if not isinstance(raw_record, dict):
            raise AssertionError("Cal Performances fixture must contain objects")
        record: dict[str, object] = {}
        for key, value in raw_record.items():
            if not isinstance(key, str):
                raise AssertionError("Cal Performances fixture keys must be strings")
            record[key] = value
        records.append(record)
    return records


def _headers(total: int, total_pages: int) -> dict[str, str]:
    return {"X-WP-Total": str(total), "X-WP-TotalPages": str(total_pages)}


def _block(
    *,
    title: str,
    start: str,
    end: str,
    timezone: str = "America/Los_Angeles",
    location: str = "Hertz Hall",
) -> str:
    return (
        '<div class="addeventatc">'
        f'<span class="title">{title}</span>'
        f'<span class="start">{start}</span>'
        f'<span class="end">{end}</span>'
        f'<span class="timezone">{timezone}</span>'
        f'<span class="location">{location}</span>'
        "</div>"
    )


def _content(
    *,
    title: str = "Future Performance",
    start: str = "09/27/2026 03:00 pm",
    end: str = "09/27/2026 04:30 pm",
    timezone: str = "America/Los_Angeles",
    location: str = "Hertz Hall",
    second_start: str | None = None,
    price: str | None = "Tickets start at $75",
    duplicate_blocks: bool = True,
) -> str:
    first = _block(
        title=title,
        start=start,
        end=end,
        timezone=timezone,
        location=location,
    )
    price_block = f'<div class="event-price-block">{price}</div>' if price is not None else ""
    if not duplicate_blocks:
        return f"<div>{first}{price_block}</div>"
    second = _block(
        title=title,
        start=second_start or start,
        end=end,
        timezone=timezone,
        location=location,
    )
    return f"<div>{first}{price_block}</div><div>{second}{price_block}</div>"


def _record(
    identifier: int,
    *,
    title: str = "Future Performance",
    link: str | None = None,
    content: str | None = None,
) -> dict[str, object]:
    return {
        "id": identifier,
        "link": (
            link
            if link is not None
            else f"https://calperformances.org/events/2026-27/recital/future-performance-{identifier}/"
        ),
        "title": {"rendered": title},
        "content": {"rendered": content if content is not None else _content(title=title)},
    }


async def test_calperformances_maps_fixture_primary_occurrence_without_detail_fetch() -> None:
    """The exact collection query yields a same-origin handoff, never an event-detail GET (FR-3.1/FR-3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            headers=_headers(1, 1),
            json=_fixture_records(),
            request=request,
        )

    fetcher = CalPerformancesCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert requested == [httpx.URL(_SEED_URL)]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert (
        candidate.source_event_id
        == "calperformances:calperformances-events:82473:2026-09-25T19:30:00-07:00"
    )
    assert candidate.title == "The Australian Ballet: Oscar"
    assert candidate.registration_url == (
        "https://calperformances.org/events/2026-27/dance/the-australian-ballet-oscar/"
    )
    assert candidate.start_at == datetime(
        2026, 9, 25, 19, 30, tzinfo=ZoneInfo("America/Los_Angeles")
    )
    assert candidate.end_at == datetime(2026, 9, 25, 21, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert candidate.venue_name == "Zellerbach Hall"
    assert candidate.city is None
    assert candidate.geo is None
    assert candidate.description == ""
    assert candidate.price_status is PriceStatus.PAID
    assert candidate.raw == {
        "id": "82473",
        "path": "/events/2026-27/dance/the-australian-ballet-oscar/",
        "calendar_start": "09/25/2026 07:30 pm",
        "calendar_end": "09/25/2026 09:00 pm",
        "timezone": "America/Los_Angeles",
        "location": "Zellerbach Hall",
    }


async def test_calperformances_constructs_every_declared_page_and_paces_each_get() -> None:
    """The fixed ``per_page=100`` collection reads all header-declared pages at the source floor (NFR-8/FR-10.4)."""
    first_records = [_record(identifier) for identifier in range(1, 101)]
    second_records = [_record(101)]
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
        page = request.url.params["page"]
        records = first_records if page == "1" else second_records
        return httpx.Response(200, headers=_headers(101, 2), json=records, request=request)

    fetcher = CalPerformancesCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert requested == [
        httpx.URL("https://calperformances.org/wp-json/wp/v2/cp_event?per_page=100&page=1"),
        httpx.URL("https://calperformances.org/wp-json/wp/v2/cp_event?per_page=100&page=2"),
    ]
    assert len(candidates) == 101
    assert slept == [1.5]


async def test_calperformances_keeps_only_future_physical_agreeing_structured_blocks() -> None:
    """Virtual, cancelled, malformed, unsafe, past, ambiguous, and out-of-window rows are excluded (FR-3.7)."""
    records = [
        _record(1),
        _record(
            2,
            content=_content(
                start="09/25/2026 07:30 pm",
                end="09/25/2026 09:00 pm",
                price="Tickets start at $0",
            ),
        ),
        _record(3, content=_content(location="Online")),
        _record(4, title="Cancelled future performance"),
        _record(5, content=_content(second_start="09/27/2026 04:00 pm")),
        _record(6, content=_content(start="07/16/2026 03:00 pm", end="07/16/2026 04:30 pm")),
        _record(7, content=_content(start="10/15/2026 03:00 pm", end="10/15/2026 04:30 pm")),
        _record(8, link="https://tickets.example.test/events/future-performance/"),
        _record(9, content=_content(timezone="America/New_York")),
        _record(10, content=_content(duplicate_blocks=False)),
        _record(11, content=_content(start="11/01/2026 01:30 am", end="11/01/2026 02:30 am")),
        _record(
            12,
            link="https://calperformances.org/events/2026-27/recital/future-performance-12/\n",
        ),
        _record(13, title="Different Performance", content=_content(title="Future Performance")),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_headers(13, 1), json=records, request=request)

    fetcher = CalPerformancesCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "calperformances:calperformances-events:1:2026-09-27T15:00:00-07:00",
        "calperformances:calperformances-events:2:2026-09-25T19:30:00-07:00",
    ]
    assert [candidate.price_status for candidate in candidates] == [
        PriceStatus.PAID,
        PriceStatus.UNKNOWN,
    ]


async def test_calperformances_fails_closed_on_missing_or_excess_pagination_metadata() -> None:
    """A missing header or collection above the reviewed four-page cap cannot appear complete (NFR-8)."""

    async def missing_header_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-WP-Total": "0"}, json=[], request=request)

    missing_header_fetcher = CalPerformancesCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(missing_header_handler)
    )
    with pytest.raises(CalPerformancesFetchError, match="invalid collection envelope"):
        await missing_header_fetcher.fetch(_source())

    async def cap_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_headers(401, 5), json=[], request=request)

    cap_fetcher = CalPerformancesCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(cap_handler)
    )
    with pytest.raises(CalPerformancesFetchError, match="exceeds or contradicts"):
        await cap_fetcher.fetch(_source())


async def test_calperformances_fails_closed_when_pages_change_or_repeat_wordpress_ids() -> None:
    """Changing headers or a repeated raw ID invalidates the entire declared collection (NFR-8)."""
    first_records = [_record(identifier) for identifier in range(1, 101)]

    async def changed_totals_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return httpx.Response(
                200, headers=_headers(101, 2), json=first_records, request=request
            )
        return httpx.Response(200, headers=_headers(100, 1), json=[_record(101)], request=request)

    changed_totals_fetcher = CalPerformancesCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(changed_totals_handler)
    )
    with pytest.raises(CalPerformancesFetchError, match="changed pagination totals"):
        await changed_totals_fetcher.fetch(_source())

    async def duplicate_id_handler(request: httpx.Request) -> httpx.Response:
        records = first_records if request.url.params["page"] == "1" else [_record(1)]
        return httpx.Response(200, headers=_headers(101, 2), json=records, request=request)

    duplicate_id_fetcher = CalPerformancesCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(duplicate_id_handler)
    )
    with pytest.raises(CalPerformancesFetchError, match="repeated WordPress ID"):
        await duplicate_id_fetcher.fetch(_source())


async def test_calperformances_rejects_tampering_redirects_and_oversized_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile drift, redirecting list endpoints, and oversized bodies make no unsafe discovery write (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, headers=_headers(1, 1), json=_fixture_records(), request=request)

    fetcher = CalPerformancesCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(handler)
    )
    for unsafe_source in (
        replace(_source(), source_key="unreviewed-calperformances-events"),
        replace(_source(), seed_url=f"{_SEED_URL}&context=view"),
        replace(_source(), seed_url=f"{_SEED_URL}\n"),
        replace(
            _source(),
            approved_origins=("https://calperformances.org", "https://other.example.test"),
        ),
        replace(_source(), page_limit=3),
        replace(_source(), min_interval_ms=1_000),
    ):
        with pytest.raises(ValueError, match="reviewed public collection endpoint"):
            await fetcher.fetch(unsafe_source)
    assert requested == []

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/events"},
            request=request,
        )

    redirect_fetcher = CalPerformancesCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(redirect_handler)
    )
    with pytest.raises(CalPerformancesFetchError, match="approved collection endpoint"):
        await redirect_fetcher.fetch(_source())

    monkeypatch.setattr(calperformances_source, "_MAX_RESPONSE_BYTES", 1)

    async def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_headers(0, 0), content=b"[]", request=request)

    oversized_fetcher = CalPerformancesCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(oversized_handler)
    )
    with pytest.raises(CalPerformancesFetchError, match="response-size limit"):
        await oversized_fetcher.fetch(_source())
