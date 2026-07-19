"""Midpeninsula Regional Open Space District adapter coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from html import escape

import httpx
import pytest

from events_concierge.adapters.midpen.source import MidpenCatalogFetcher, MidpenFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_SEED_URL = "https://www.openspace.org/get-involved/events-activities?page=0"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="midpen-events",
        display_name="Midpeninsula Regional Open Space District Events & Activities",
        publisher="Midpeninsula Regional Open Space District",
        seed_url=_SEED_URL,
        approved_origins=("https://www.openspace.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.MIDPEN_HTML,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=9,
    )


def _page(
    rows: list[str],
    *,
    page_index: int = 0,
    page_count: int = 1,
    include_next: bool | None = None,
    disabled_next: bool = False,
    visible_page_indices: list[int] | None = None,
) -> str:
    pager = ""
    if page_count > 1 or include_next is not None:
        page_indices = sorted(
            set(visible_page_indices if visible_page_indices is not None else range(page_count))
            | {page_index}
        )
        number_items = "".join(
            (
                f'<li class="pager__item pager__item--active"><span class="pager__number">{index + 1}</span></li>'
                if index == page_index
                else f'<li class="pager__item"><a class="number" href="?page={index}#pager-content">{index + 1}</a></li>'
            )
            for index in page_indices
        )
        has_next = page_index < page_count - 1 if include_next is None else include_next
        next_item = ""
        if disabled_next:
            next_item = '<li class="pager__item pager__item--next"><a href="">Next</a></li>'
        elif has_next:
            next_item = f'<li class="pager__item pager__item--next"><a href="?page={page_index + 1}#pager-content">Next</a></li>'
        pager = f'<nav class="pager"><ul class="pager__list">{number_items}{next_item}</ul></nav>'
    return "\n".join(
        (
            "<!doctype html><html><body>",
            '<table class="views-table views-view-table cols-6"><tbody>',
            *rows,
            "</tbody></table>",
            pager,
            "</body></html>",
        )
    )


def _row(
    identifier: str,
    *,
    title: str = "Summer Walk",
    event_date: str = "Saturday, Jul 18, 2026",
    event_time: str = "09:00 a.m. - 11:00 a.m.",
    preserve: str | None = "Ravenswood Preserve",
    href: str | None = None,
    activity_type: str = "Guided Activity",
    subtype: str = "Hike",
    miles: str = "4.25",
) -> str:
    event_link = href or f"/events/guided-activities/{identifier}"
    link = (
        f'<a href="{escape(event_link, quote=True)}">{escape(title)}</a>'
        if href is not None or identifier
        else ""
    )
    preserve_text = escape(preserve) if preserve is not None else ""
    return "\n".join(
        (
            "<tr>",
            f'<td class="views-field-type">{escape(activity_type)}</td>',
            f'<td class="views-field-title">{link}</td>',
            '<td class="views-field-aggregated-dates is-active">',
            f'<div class="activity-search-date">{escape(event_date)}</div>',
            f'<div class="activity-search-time">{escape(event_time)}</div>',
            "</td>",
            f'<td class="views-field-field-activity-type"><span class="icon-link__name">{escape(subtype)}</span></td>',
            f'<td class="views-field-field-preserve-term-1">{preserve_text}</td>',
            f'<td class="views-field-field-aprox-total-miles">{escape(miles)}</td>',
            "</tr>",
        )
    )


async def test_midpen_maps_only_future_timed_reviewed_physical_rows() -> None:
    """The closed list derives times and handoffs from source rows without following event pages (FR-3.1/FR-3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_page(
                [
                    _row("summer-walk"),
                    _row("online", preserve="Online"),
                    _row("cancelled", title="Summer Walk Cancelled"),
                    _row("no-preserve", preserve=None),
                    _row("bad-time", event_time="All day"),
                    _row("past", event_date="Friday, Jul 17, 2026", event_time="01:00 am"),
                    _row(
                        "unsafe",
                        href="/about-us/meetings/board-meeting",
                    ),
                ]
            ),
            request=request,
        )

    fetcher = MidpenCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "midpen:midpen-events:/events/guided-activities/summer-walk:2026-07-18T16:00:00+00:00"
    ]
    candidate = candidates[0]
    assert candidate.title == "Summer Walk"
    assert (
        candidate.registration_url
        == "https://www.openspace.org/events/guided-activities/summer-walk"
    )
    assert candidate.start_at == datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
    assert candidate.end_at == datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
    assert candidate.venue_name == "Ravenswood Preserve"
    assert candidate.city is None
    assert candidate.geo is None
    assert candidate.description == "Guided Activity — Hike — 4.25 miles"
    assert candidate.price_status is PriceStatus.UNKNOWN
    assert requested == [httpx.URL(_SEED_URL)]


async def test_midpen_completes_the_publisher_declared_pages_with_pacing() -> None:
    """The adapter owns fixed page URLs and validates the complete next chain (NFR-8/FR-10.4)."""
    requested: list[httpx.URL] = []
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    first_rows = [_row(f"first-{index}") for index in range(15)]

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        page_index = int(request.url.params["page"])
        rows = first_rows if page_index == 0 else [_row("last")]
        return httpx.Response(
            200,
            text=_page(rows, page_index=page_index, page_count=2),
            request=request,
        )

    fetcher = MidpenCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert len(candidates) == 16
    assert requested == [
        httpx.URL("https://www.openspace.org/get-involved/events-activities?page=0"),
        httpx.URL("https://www.openspace.org/get-involved/events-activities?page=1"),
    ]
    assert slept == [1.5]


async def test_midpen_follows_the_nine_page_compact_next_chain() -> None:
    """Compact Drupal numeric links cannot truncate the reviewed sequential next chain (NFR-8)."""
    current = 100.0

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        current += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        page_index = int(request.url.params["page"])
        return httpx.Response(
            200,
            text=_page(
                (
                    [_row(f"page-{page_index}-{row}") for row in range(15)]
                    if page_index < 8
                    else [_row(f"page-{page_index}-{row}") for row in range(6)]
                ),
                page_index=page_index,
                page_count=9,
                include_next=False if page_index == 6 else None,
                disabled_next=page_index == 8,
                visible_page_indices=[1, 5, 6, 7] if page_index == 4 else None,
            ),
            request=request,
        )

    fetcher = MidpenCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    assert len(await fetcher.fetch(_source())) == 126


async def test_midpen_fails_closed_when_the_next_chain_exceeds_the_reviewed_cap() -> None:
    """A tenth list page cannot look like a complete reviewed catalog (NFR-8)."""
    current = 100.0
    requested: list[int] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        current += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        page_index = int(request.url.params["page"])
        requested.append(page_index)
        return httpx.Response(
            200,
            text=_page(
                [_row(f"page-{page_index}-{row}") for row in range(15)],
                page_index=page_index,
                page_count=9,
                include_next=True,
            ),
            request=request,
        )

    fetcher = MidpenCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(MidpenFetchError, match="exceeds"):
        await fetcher.fetch(_source())
    assert requested == list(range(9))


async def test_midpen_collapses_exact_page_overlap_and_rejects_conflicting_occurrences() -> None:
    """An exact source-list overlap is idempotent, while conflicting occurrence data fails closed (NFR-8)."""

    async def no_sleep(_delay: float) -> None:
        return None

    async def duplicate_handler(request: httpx.Request) -> httpx.Response:
        page_index = int(request.url.params["page"])
        rows = (
            [_row(f"first-{index}") for index in range(15)]
            if page_index == 0
            else [_row("first-0")]
        )
        return httpx.Response(
            200,
            text=_page(rows, page_index=page_index, page_count=2),
            request=request,
        )

    duplicate_fetcher = MidpenCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=lambda: 100.0,
        sleep=no_sleep,
        transport=httpx.MockTransport(duplicate_handler),
    )
    candidates = await duplicate_fetcher.fetch(_source())
    assert len(candidates) == 15

    async def conflict_handler(request: httpx.Request) -> httpx.Response:
        page_index = int(request.url.params["page"])
        rows = (
            [_row(f"first-{index}") for index in range(15)]
            if page_index == 0
            else [_row("first-0", title="Changed source row")]
        )
        return httpx.Response(
            200,
            text=_page(rows, page_index=page_index, page_count=2),
            request=request,
        )

    conflict_fetcher = MidpenCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=lambda: 100.0,
        sleep=no_sleep,
        transport=httpx.MockTransport(conflict_handler),
    )
    with pytest.raises(MidpenFetchError, match="conflicted on midpen:midpen-events"):
        await conflict_fetcher.fetch(_source())


async def test_midpen_fails_closed_for_an_inconsistent_final_pager() -> None:
    """A pager with a later non-adjacent link but no successor fails the whole refresh (NFR-8)."""

    async def inconsistent_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_page(
                [_row(f"first-{index}") for index in range(15)],
                page_count=3,
                include_next=False,
                visible_page_indices=[2],
            ),
            request=request,
        )

    inconsistent_fetcher = MidpenCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(inconsistent_handler),
    )
    with pytest.raises(MidpenFetchError, match="inconsistent next link"):
        await inconsistent_fetcher.fetch(_source())


async def test_midpen_rejects_redirects_and_unreviewed_or_tampered_endpoints() -> None:
    """Redirects and registry data cannot widen this closed source or create a catalog effect (NFR-8)."""
    requested: list[str] = []

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/events"},
            request=request,
        )

    fetcher = MidpenCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(redirect_handler)
    )
    with pytest.raises(MidpenFetchError, match="approved endpoint"):
        await fetcher.fetch(_source())
    assert requested == [_SEED_URL]

    unreviewed = replace(_source(), source_key="unreviewed-midpen-events")
    with pytest.raises(ValueError, match="reviewed public events"):
        await fetcher.fetch(unreviewed)
    tampered = replace(
        _source(), seed_url="https://www.openspace.org/get-involved/events-activities?page=1"
    )
    with pytest.raises(ValueError, match="reviewed public events"):
        await fetcher.fetch(tampered)
    assert requested == [_SEED_URL]
