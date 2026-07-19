"""Yerba Buena Center for the Arts calendar adapter coverage (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

import events_concierge.adapters.ybca.source as ybca_source
from events_concierge.adapters.ybca.source import YbcaCatalogFetcher, YbcaFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.domain.events import CandidateEvent

_SEED_URL = "https://ybca.org/calendar/"
_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "ybca" / "calendar.html"
_NOW = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)
_LOCAL_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_EN_DASH = "\N{EN DASH}"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="ybca-calendar",
        display_name="Yerba Buena Center for the Arts Calendar",
        publisher="Yerba Buena Center for the Arts",
        seed_url=_SEED_URL,
        approved_origins=("https://ybca.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.YBCA_HTML,
        enabled=True,
        reviewed_at=_NOW,
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=10_000,
        page_limit=1,
    )


def _fixture() -> str:
    return _FIXTURE_PATH.read_text(encoding="utf-8")


def _card(
    *,
    title: str = "Future Event",
    href: str = "https://ybca.org/event/future-event/",
    event_time: str = "Wednesday, July 22, 2026, 6 PM",
    venue: str = "Grand Lobby, YBCA",
    detail: str = "",
    ticket_label: str | None = None,
) -> str:
    ticket_label_markup = (
        f"<p><strong>{ticket_label}</strong>Free with registration</p>"
        if ticket_label is not None
        else ""
    )
    return (
        '<div class="feature-event-wrap">'
        '<div class="copy"><div>'
        f'<h3><a href="{href}">{title}</a></h3>'
        f"<p>{detail}</p>"
        "</div></div>"
        '<div class="tickets">'
        f'<p class="date">{event_time}</p>'
        f"<p><strong>{venue}</strong></p>"
        f"{ticket_label_markup}"
        '<p><a href="https://ybca.ticketing.veevartapp.com/tickets/999">Tickets</a></p>'
        "</div>"
        "</div>"
    )


def _page(cards: str, *, feature: str = "", suffix: str = "") -> str:
    return f'<html><body>{feature}<div class="events">{cards}</div>{suffix}</body></html>'


async def _fetch_html(
    html: str,
    *,
    now: datetime = _NOW,
) -> list[CandidateEvent]:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    fetcher = YbcaCatalogFetcher(
        user_agent="test",
        now=lambda: now,
        transport=httpx.MockTransport(handler),
    )
    return await fetcher.fetch(_source())


async def test_ybca_maps_only_scoped_future_physical_cards_without_detail_or_ticket_fetch() -> None:
    """The reviewed list excludes its separate feature and produces event handoffs only (FR-3.1/FR-3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, text=_fixture(), request=request)

    fetcher = YbcaCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "ybca:ybca-calendar:/event/world-of-black-film/:2026-07-17T18:30:00-07:00",
        "ybca:ybca-calendar:/event/future-gallery-talk/:2026-07-22T14:00:00-07:00",
    ]
    first, second = candidates
    assert first.title == "The World of Black Film"
    assert first.registration_url == "https://ybca.org/event/world-of-black-film/"
    assert first.start_at == datetime(2026, 7, 17, 18, 30, tzinfo=_LOCAL_TIME_ZONE)
    assert first.end_at is None
    assert first.venue_name == "Screening Room, YBCA"
    assert first.city == "San Francisco"
    assert first.geo is None
    assert first.description == ""
    assert first.price_status is PriceStatus.UNKNOWN
    assert first.raw == {
        "path": "/event/world-of-black-film/",
        "time_string": "Friday, July 17, 2026, 6:30 PM",
        "location": "Screening Room, YBCA",
    }
    assert second.end_at == datetime(2026, 7, 22, 16, 0, tzinfo=_LOCAL_TIME_ZONE)
    assert requested == [httpx.URL(_SEED_URL)]


async def test_ybca_uses_only_the_date_adjacent_venue_when_ticket_labels_are_strong() -> None:
    """A later ticket/price label cannot be mistaken for the reviewed physical venue (FR-3.7/NFR-8)."""
    candidates = await _fetch_html(
        _page(_card(title="Price-labelled event", ticket_label="Tickets:"))
    )

    assert len(candidates) == 1
    assert candidates[0].venue_name == "Grand Lobby, YBCA"


async def test_ybca_accepts_only_verified_single_day_time_grammar_horizon_and_dst() -> None:
    """Unknown date forms, horizon-edge rows, and ambiguous local times cannot become candidates (FR-3.7)."""
    assert ybca_source._event_times("Wednesday, July 22, 2026, 6 PM") == (
        datetime(2026, 7, 22, 18, 0, tzinfo=_LOCAL_TIME_ZONE),
        None,
    )
    assert ybca_source._event_times(f"Wednesday, July 22, 2026, 2{_EN_DASH}4 PM") == (
        datetime(2026, 7, 22, 14, 0, tzinfo=_LOCAL_TIME_ZONE),
        datetime(2026, 7, 22, 16, 0, tzinfo=_LOCAL_TIME_ZONE),
    )
    for invalid_time in (
        f"July 11{_EN_DASH}19, 2026",
        f"Wednesday, July 22, 2026, 2 PM{_EN_DASH}4 PM",
        "Wednesday, July 22, 2026, 2-4 PM",
        f"Wednesday, July 22, 2026, 2{_EN_DASH}4 PM PST",
        "Tuesday, July 22, 2026, 6 PM",
        "Sunday, March 8, 2026, 2:30 AM",
        "Sunday, November 1, 2026, 1:30 AM",
    ):
        assert ybca_source._event_times(invalid_time) == (None, None)

    candidates = await _fetch_html(
        _page(
            _card(event_time="Friday, July 17, 2026, 11 AM")
            + _card(
                title="Horizon edge",
                href="https://ybca.org/event/horizon-edge/",
                event_time="Thursday, October 15, 2026, 6 PM",
            )
        )
    )

    assert candidates == []


async def test_ybca_excludes_unsafe_and_nonphysical_handoffs_and_fails_on_card_shape_drift() -> (
    None
):
    """Only complete physical YBCA cards can hand off; a changed card contract fails closed (FR-3.7/NFR-8)."""
    candidates = await _fetch_html(
        _page(
            _card(title="Kept", href="https://ybca.org/event/kept/")
            + _card(
                title="Query link",
                href="https://ybca.org/event/query-link/?ticket=1",
            )
            + _card(
                title="External link",
                href="https://unapproved.example.test/event/external-link/",
            )
            + _card(title="Virtual", venue="Online, YBCA")
            + _card(title="Wrong venue", venue="Grand Lobby, YBCA Annex")
        )
    )

    assert [candidate.title for candidate in candidates] == ["Kept"]

    malformed = (
        '<div class="feature-event-wrap"><div class="copy"><h3>'
        '<a href="https://ybca.org/event/malformed/">Malformed</a></h3></div>'
        '<div class="tickets"><p class="date">Wednesday, July 22, 2026, 6 PM</p></div></div>'
    )
    with pytest.raises(YbcaFetchError, match="reviewed title/date/location"):
        await _fetch_html(_page(malformed))


async def test_ybca_rejects_tampered_profiles_redirects_pagers_caps_and_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry widening and incomplete/contradictory list contracts stay retryable (FR-10.3/NFR-8)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_fixture(), request=request)

    fetcher = YbcaCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))
    for unsafe_source in (
        replace(_source(), source_key="unreviewed-ybca-calendar"),
        replace(_source(), seed_url=f"{_SEED_URL}?page=2"),
        replace(
            _source(), approved_origins=("https://ybca.org", "https://unapproved.example.test")
        ),
        replace(_source(), min_interval_ms=1_500),
        replace(_source(), page_limit=2),
    ):
        with pytest.raises(ValueError, match="reviewed public calendar endpoint"):
            await fetcher.fetch(unsafe_source)
    assert requested == []

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/calendar/"},
            request=request,
        )

    redirect_fetcher = YbcaCatalogFetcher(
        user_agent="test",
        transport=httpx.MockTransport(redirect_handler),
    )
    with pytest.raises(YbcaFetchError, match="approved calendar endpoint"):
        await redirect_fetcher.fetch(_source())

    for incomplete_page in (
        "<html><body></body></html>",
        _page(""),
        (
            "<html><body>"
            f'<div class="events">{_card()}</div><div class="events">{_card()}</div>'
            "</body></html>"
        ),
        _page(_card(), suffix='<nav class="pager"><a href="?page=2">Next</a></nav>'),
        _page(_card(), suffix='<a href="https://ybca.org/calendar/?page=2">Next</a>'),
        _page("".join(_card(title=f"Card {index}") for index in range(40))),
    ):
        with pytest.raises(YbcaFetchError):
            await _fetch_html(incomplete_page)

    monkeypatch.setattr(ybca_source, "_MAX_RESPONSE_BYTES", 1)
    with pytest.raises(YbcaFetchError, match="response-size limit"):
        await _fetch_html(_page(_card()))

    monkeypatch.setattr(ybca_source, "_MAX_RESPONSE_BYTES", 250_000)
    duplicate_cards = _card(title="First title") + _card(title="Second title")
    with pytest.raises(YbcaFetchError, match="conflicted"):
        await _fetch_html(_page(duplicate_cards))


async def test_ybca_paces_repeated_list_reads_at_the_robots_floor() -> None:
    """Every list GET observes YBCA's static ten-second Crawl-delay (FR-10.4)."""
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

    fetcher = YbcaCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    await fetcher.fetch(_source())
    await fetcher.fetch(_source())

    assert requested == [httpx.URL(_SEED_URL), httpx.URL(_SEED_URL)]
    assert slept == [10.0]
