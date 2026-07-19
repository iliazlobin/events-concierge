"""Berkeley Repertory Theatre show-list adapter coverage (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

import events_concierge.adapters.berkeley_rep.source as berkeley_rep_source
from events_concierge.adapters.berkeley_rep.source import (
    BerkeleyRepCatalogFetcher,
    BerkeleyRepFetchError,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.domain.events import CandidateEvent

_SEED_URL = "https://www.berkeleyrep.org/shows"
_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "berkeley_rep" / "shows.html"
_NOW = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="berkeley-rep-shows",
        display_name="Berkeley Repertory Theatre Shows",
        publisher="Berkeley Repertory Theatre",
        seed_url=_SEED_URL,
        approved_origins=("https://www.berkeleyrep.org", "https://tickets.berkeleyrep.org"),
        region="bay_area_9_county",
        mode=CatalogSourceMode.BERKELEY_REP_HTML,
        enabled=True,
        reviewed_at=_NOW,
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=5_000,
        page_limit=1,
    )


def _fixture() -> str:
    return _FIXTURE_PATH.read_text(encoding="utf-8")


def _occurrence(
    *,
    event_date: str = "Fri, Sep 4",
    event_time: str = "8:00PM",
    venue: str = "Peet\u2019s Theatre",
    ticket: str | None = "https://tickets.berkeleyrep.org/19511/19530",
    detail: str = "",
) -> str:
    ticket_markup = (
        f'<a class="btn btn-order" href="{ticket}">Tickets</a>' if ticket is not None else ""
    )
    return (
        '<li class="subshow">'
        '<div class="dateTime">'
        f'<div class="date"><div class="start">{event_date}</div></div>'
        f'<div class="time"><span class="start">{event_time}</span></div>'
        "</div>"
        f'<div class="locationBox"><div class="location">{venue}</div></div>'
        f'<div class="status">{detail}</div>'
        f"{ticket_markup}"
        "</li>"
    )


def _card(
    entry_id: int,
    *,
    title: str = "Future Production",
    show_path: str | None = None,
    range_start: str = "Fri, Sep 4, 2026",
    range_end: str = "Sun, Oct 11, 2026",
    occurrences: str | None = None,
) -> str:
    path = show_path if show_path is not None else f"/shows/future-production-{entry_id}"
    subshows = occurrences if occurrences is not None else _occurrence()
    return (
        f'<li class="eventCard" data-entry-id="{entry_id}">'
        f'<a class="desc" href="{path}"><h3 class="title">{title}</h3></a>'
        '<div class="top-date">'
        f'<span class="start">{range_start}</span><span class="end">{range_end}</span>'
        "</div>"
        '<div class="tagline">A source-supplied production description.</div>'
        f'<ul id="sub-show-list{entry_id}">{subshows}</ul>'
        "</li>"
    )


def _page(cards: str, *, suffix: str = "") -> str:
    return f'<html><body><ul class="listItems">{cards}</ul>{suffix}</body></html>'


async def _fetch_html(
    html: str,
    *,
    now: datetime = _NOW,
) -> list[CandidateEvent]:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    fetcher = BerkeleyRepCatalogFetcher(
        user_agent="test",
        now=lambda: now,
        transport=httpx.MockTransport(handler),
    )
    return await fetcher.fetch(_source())


async def test_berkeley_rep_maps_only_scoped_ticketable_performances_without_detail_fetch() -> None:
    """The one approved SSR list yields direct ticket handoffs, never show or ticket reads (FR-3.1/FR-3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, text=_fixture(), request=request)

    fetcher = BerkeleyRepCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "berkeley-rep:berkeley-rep-shows:36:19530:2026-09-04T20:00:00-07:00"
    ]
    candidate = candidates[0]
    assert candidate.title == "The Cook"
    assert candidate.registration_url == "https://tickets.berkeleyrep.org/19511/19530"
    assert candidate.start_at == datetime(2026, 9, 4, 20, 0, tzinfo=_LOCAL_TIME_ZONE)
    assert candidate.end_at is None
    assert candidate.venue_name == "Peet\u2019s Theatre"
    assert candidate.city is None
    assert candidate.geo is None
    assert (
        candidate.description
        == "A cook\u2019s forty-year vow becomes a portrait of loyalty and loss."
    )
    assert candidate.price_status is PriceStatus.UNKNOWN
    assert candidate.raw == {
        "entry_id": "36",
        "show_path": "/shows/the-cook-b51d",
        "ticket_id": "19530",
        "date": "Fri, Sep 4",
        "time": "8:00PM",
        "venue": "Peet\u2019s Theatre",
    }
    assert requested == [httpx.URL(_SEED_URL)]


async def test_berkeley_rep_resolves_a_short_date_once_across_a_year_boundary() -> None:
    """A weekday/date is retained only when its parent range supplies one unambiguous year (FR-3.7)."""
    candidates = await _fetch_html(
        _page(
            _card(
                42,
                range_start="Tue, Dec 29, 2026",
                range_end="Sun, Jan 3, 2027",
                occurrences=_occurrence(
                    event_date="Sat, Jan 2",
                    event_time="7:30PM",
                    ticket="https://tickets.berkeleyrep.org/20001/20002",
                ),
            )
        ),
        now=datetime(2026, 12, 1, 18, 0, tzinfo=UTC),
    )

    assert len(candidates) == 1
    assert candidates[0].start_at == datetime(2027, 1, 2, 19, 30, tzinfo=_LOCAL_TIME_ZONE)


async def test_berkeley_rep_rejects_unsafe_missing_and_nonphysical_occurrences() -> None:
    """Invalid, invitation-only, virtual, cancelled, past, and out-of-window rows cannot enter discovery (FR-3.7)."""
    candidates = await _fetch_html(
        _page(
            "".join(
                (
                    _card(
                        36,
                        occurrences="".join(
                            (
                                _occurrence(),
                                _occurrence(
                                    event_date="Sat, Sep 5",
                                    ticket=None,
                                ),
                                _occurrence(
                                    event_date="Sun, Sep 6",
                                    ticket="https://tickets.berkeleyrep.org/19511/19531",
                                    detail="By Invitation Only",
                                ),
                                _occurrence(event_date="Tue, Sep 8", detail="Online stream"),
                                _occurrence(event_date="Wed, Sep 9", detail="Postponed"),
                                _occurrence(event_date="Thu, Sep 10", event_time="19:30"),
                                _occurrence(
                                    event_date="Fri, Sep 11",
                                    ticket="https://tickets.berkeleyrep.org/19511/19532?waitlist=1",
                                ),
                                _occurrence(
                                    event_date="Sat, Sep 12",
                                    ticket="https://tickets.berkeleyrep.org.evil.test/19511/19533",
                                ),
                                _occurrence(event_date="Thu, Sep 4"),
                            )
                        ),
                    ),
                    _card(
                        37,
                        title="Cancelled Production",
                        range_start="Fri, Sep 4, 2026",
                        range_end="Sun, Oct 11, 2026",
                        occurrences=_occurrence(
                            event_date="Sat, Sep 5",
                            ticket="https://tickets.berkeleyrep.org/19512/19533",
                        ),
                    ),
                    _card(
                        38,
                        range_start="Fri, Jul 10, 2026",
                        range_end="Sun, Jul 12, 2026",
                        occurrences=_occurrence(
                            event_date="Fri, Jul 10",
                            ticket="https://tickets.berkeleyrep.org/19513/19534",
                        ),
                    ),
                    _card(
                        39,
                        range_start="Mon, Oct 19, 2026",
                        range_end="Sun, Oct 25, 2026",
                        occurrences=_occurrence(
                            event_date="Mon, Oct 19",
                            ticket="https://tickets.berkeleyrep.org/19514/19535",
                        ),
                    ),
                    _card(
                        40,
                        show_path="/shows/unsafe-trailing/",
                        occurrences=_occurrence(
                            event_date="Sat, Sep 5",
                            ticket="https://tickets.berkeleyrep.org/19515/19536",
                        ),
                    ),
                )
            )
        )
    )

    assert [candidate.source_event_id for candidate in candidates] == [
        "berkeley-rep:berkeley-rep-shows:36:19530:2026-09-04T20:00:00-07:00"
    ]


async def test_berkeley_rep_paces_repeated_list_reads_at_the_robot_declared_floor() -> None:
    """Every anonymous list GET shares Berkeley Rep's published five-second crawl delay (FR-10.4)."""
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

    fetcher = BerkeleyRepCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    await fetcher.fetch(_source())
    await fetcher.fetch(_source())

    assert requested == [httpx.URL(_SEED_URL), httpx.URL(_SEED_URL)]
    assert slept == [5.0]


async def test_berkeley_rep_rejects_tampered_profile_and_redirects_before_widening_fetches() -> (
    None
):
    """Registry edits, unreviewed origins, and redirects cannot make this a generic theatre client (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_fixture(), request=request)

    fetcher = BerkeleyRepCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))
    for unsafe_source in (
        replace(_source(), source_key="unreviewed-berkeley-rep"),
        replace(_source(), seed_url=f"{_SEED_URL}?page=1"),
        replace(_source(), approved_origins=("https://www.berkeleyrep.org",)),
        replace(
            _source(),
            approved_origins=(
                "https://www.berkeleyrep.org",
                "https://tickets.berkeleyrep.org",
                "https://unreviewed.example.test",
            ),
        ),
        replace(_source(), min_interval_ms=1_500),
        replace(_source(), page_limit=2),
    ):
        with pytest.raises(ValueError, match="reviewed public show-list endpoint"):
            await fetcher.fetch(unsafe_source)
    with pytest.raises(ValueError, match="handoff-only"):
        await fetcher.fetch(replace(_source(), handoff_only=False))
    assert requested == []

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/shows"},
            request=request,
        )

    redirect_fetcher = BerkeleyRepCatalogFetcher(
        user_agent="test",
        transport=httpx.MockTransport(redirect_handler),
    )
    with pytest.raises(BerkeleyRepFetchError, match="approved show-list endpoint"):
        await redirect_fetcher.fetch(_source())


async def test_berkeley_rep_fails_closed_for_pagination_caps_and_card_contract_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed SSR completeness contract errors before it can silently truncate discovery (NFR-8)."""
    with pytest.raises(BerkeleyRepFetchError, match="unreviewed pagination"):
        await _fetch_html(
            _page(
                _card(36),
                suffix='<nav class="pager"><a href="?page=2">Next</a></nav>',
            )
        )
    with pytest.raises(BerkeleyRepFetchError, match="9-card cap"):
        await _fetch_html(_page("".join(_card(entry_id) for entry_id in range(1, 10))))
    with pytest.raises(BerkeleyRepFetchError, match="400-occurrence cap"):
        await _fetch_html(_page(_card(36, occurrences="".join(_occurrence() for _ in range(400)))))
    with pytest.raises(BerkeleyRepFetchError, match="no reviewed show cards"):
        await _fetch_html(_page(""))
    with pytest.raises(BerkeleyRepFetchError, match="reviewed title/date contract"):
        await _fetch_html(
            _page(
                '<li class="eventCard" data-entry-id="36">'
                '<a class="desc" href="/shows/the-cook-b51d"><span>The Cook</span></a>'
                '<div class="top-date"><span class="start">Fri, Sep 4, 2026</span>'
                '<span class="end">Sun, Oct 11, 2026</span></div>'
                "</li>"
            )
        )
    monkeypatch.setattr(berkeley_rep_source, "_MAX_RESPONSE_BYTES", 1)
    with pytest.raises(BerkeleyRepFetchError, match="response-size limit"):
        await _fetch_html(_fixture())
