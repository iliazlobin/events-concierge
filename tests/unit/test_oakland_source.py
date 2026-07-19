"""City of Oakland sitemap/detail adapter coverage (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.oakland.source import OaklandCatalogFetcher, OaklandFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_SEED_URL = "https://www.oaklandca.gov/sitemap.xml"
_EVENT_ONE = "https://www.oaklandca.gov/Event-Calendar/Community/Future-Physical-Event"
_EVENT_TWO = "https://www.oaklandca.gov/Event-Calendar/Community/Remote-Event"
_NOW = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="oakland-city-events",
        display_name="City of Oakland Events",
        publisher="City of Oakland",
        seed_url=_SEED_URL,
        approved_origins=("https://www.oaklandca.gov",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.OAKLAND_HTML,
        enabled=True,
        reviewed_at=_NOW,
        review_expires_at=None,
        refresh_interval_minutes=1_440,
        min_interval_ms=5_000,
        page_limit=160,
    )


def _sitemap(*urls: str) -> str:
    entries = "".join(
        f"<url><loc>{url}</loc><lastmod>2026-07-17T12:00:00Z</lastmod></url>" for url in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</urlset>"
    )


def _detail(
    *,
    title: str,
    canonical_url: str,
    occurrences: str,
    location: str = "",
    cost: str = "",
    categories: str = "",
) -> str:
    cost_markup = (
        f'<div class="event-snapshot"><p class="side-box-cost">{cost}</p></div>' if cost else ""
    )
    category_markup = (
        '<div class="categories-list-container"><ul class="categories-list">'
        f"{categories}</ul></div>"
        if categories
        else ""
    )
    return "".join(
        (
            "<html><head>",
            f'<link rel="canonical" href="{canonical_url}">',
            "</head><body>",
            '<main id="main-content">',
            f'<h1 class="oc-page-title">{title}</h1>',
            '<ul class="multi-date-list future-events-list">',
            occurrences,
            "</ul>",
            location,
            cost_markup,
            category_markup,
            # Deliberate decoys: discovery must not traverse or retain these elements.
            "<p>Long event description that must not be indexed.</p>",
            '<a href="https://registration.example.test/event">Register externally</a>',
            '<div class="contact-email">events@example.test</div>',
            '<img src="https://images.example.test/event.jpg">',
            "</main></body></html>",
        )
    )


def _occurrence(
    *,
    start: tuple[int, int, int, int, int] = (2026, 7, 20, 18, 0),
    end: tuple[int, int, int, int, int] | None = (2026, 7, 20, 20, 0),
) -> str:
    end_values = end if end is not None else ("", "", "", "", "")
    return (
        '<li class="multi-date-item" '
        f"data-start-year='{start[0]}' data-start-month='{start[1]:02}' "
        f"data-start-day='{start[2]:02}' data-start-hour='{start[3]:02}' "
        f"data-start-mins='{start[4]:02}' "
        f"data-end-year='{end_values[0]}' data-end-month='{end_values[1]}' "
        f"data-end-day='{end_values[2]}' data-end-hour='{end_values[3]}' "
        f"data-end-mins='{end_values[4]}'>ignored display text</li>"
    )


def _physical_location(
    *,
    venue_name: str = "Oakland Main Library",
    address: str = "125 14th Street, Oakland, CA 94612",
    coordinates: str = "37.8011,-122.2633",
) -> str:
    return "".join(
        (
            '<div class="gmap-marker">',
            f'<div class="gmap-info"><h2>{venue_name}</h2></div>',
            f'<div class="gmap-address">{address}</div>',
            f'<div class="gmap-latlong">{coordinates}</div>',
            '<div class="gmap-title">This title is deliberately ignored</div>',
            "</div>",
        )
    )


async def test_oakland_maps_only_future_physical_sitemap_occurrences_without_external_traversal() -> (
    None
):
    """The closed source keeps only factual local fields and never follows registration/contact/image links."""
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
        if str(request.url) == _SEED_URL:
            return httpx.Response(200, text=_sitemap(_EVENT_ONE, _EVENT_TWO), request=request)
        if str(request.url) == _EVENT_ONE:
            return httpx.Response(
                200,
                text=_detail(
                    title="Oakland civic workshop",
                    canonical_url=_EVENT_ONE,
                    occurrences=(_occurrence() + _occurrence(start=(2026, 7, 17, 10, 0), end=None)),
                    location=_physical_location(),
                    cost="Free",
                    categories='<li><a href="https://www.oaklandca.gov/Event-Calendar?category=community">Community events</a></li>',
                ),
                request=request,
            )
        if str(request.url) == _EVENT_TWO:
            return httpx.Response(
                200,
                text=_detail(
                    title="Virtual event",
                    canonical_url=_EVENT_TWO,
                    occurrences=_occurrence(),
                    location=_physical_location(),
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    fetcher = OaklandCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "oakland:oakland-city-events:"
        "https://www.oaklandca.gov/Event-Calendar/Community/Future-Physical-Event:"
        "2026-07-20T18:00:00-07:00"
    ]
    candidate = candidates[0]
    assert candidate.registration_url == _EVENT_ONE
    assert candidate.title == "Oakland civic workshop"
    assert candidate.start_at.isoformat() == "2026-07-20T18:00:00-07:00"
    assert (
        candidate.end_at is not None and candidate.end_at.isoformat() == "2026-07-20T20:00:00-07:00"
    )
    assert candidate.venue_name == "Oakland Main Library"
    assert candidate.city == "Oakland"
    assert candidate.geo is not None and (candidate.geo.lat, candidate.geo.lon) == (
        37.8011,
        -122.2633,
    )
    assert candidate.description == ""
    assert candidate.price_status is PriceStatus.FREE
    assert candidate.raw == {
        "canonical_url": _EVENT_ONE,
        "venue_name": "Oakland Main Library",
        "address": "125 14th Street, Oakland, CA 94612",
        "latitude": 37.8011,
        "longitude": -122.2633,
        "cost": "Free",
        "categories": ["Community events"],
    }
    assert requested == [httpx.URL(_SEED_URL), httpx.URL(_EVENT_ONE), httpx.URL(_EVENT_TWO)]
    assert slept == [5.0, 5.0]


async def test_oakland_keeps_an_explicit_non_oakland_bay_area_city_without_assuming_publisher_city() -> (
    None
):
    """A Berkeley source address remains Berkeley; a City of Oakland feed is not a city inference rule."""
    event_url = "https://www.oaklandca.gov/Event-Calendar/Community/Berkeley-Workshop"

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEED_URL:
            return httpx.Response(200, text=_sitemap(event_url), request=request)
        if str(request.url) == event_url:
            return httpx.Response(
                200,
                text=_detail(
                    title="Bay Area workshop",
                    canonical_url="/Event-Calendar/Community/Berkeley-Workshop",
                    occurrences=_occurrence(),
                    location=_physical_location(
                        venue_name="Berkeley Community Center",
                        address="1901 Hearst Avenue, Berkeley, CA 94709",
                        coordinates="37.8738,-122.2756",
                    ),
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    fetcher = OaklandCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    [candidate] = await fetcher.fetch(_source())

    assert candidate.registration_url == event_url
    assert candidate.city == "Berkeley"
    assert candidate.venue_name == "Berkeley Community Center"


async def test_oakland_fails_closed_before_detail_requests_on_cap_redirect_or_malformed_sitemap() -> (
    None
):
    """A source cap, redirect, or malformed XML never produces a partial catalog effect (NFR-8)."""
    cap_urls = tuple(
        f"https://www.oaklandca.gov/Event-Calendar/Community/Event-{index}" for index in range(160)
    )

    async def cap_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sitemap(*cap_urls), request=request)

    cap_fetcher = OaklandCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(cap_handler)
    )
    with pytest.raises(OaklandFetchError, match="160-detail cap"):
        await cap_fetcher.fetch(_source())

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/sitemap.xml"},
            request=request,
        )

    redirect_fetcher = OaklandCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(redirect_handler)
    )
    with pytest.raises(OaklandFetchError, match="approved endpoint"):
        await redirect_fetcher.fetch(_source())

    async def malformed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<!DOCTYPE urlset [<!ENTITY entity 'unsafe'>]><urlset />",
            request=request,
        )

    malformed_fetcher = OaklandCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(malformed_handler)
    )
    with pytest.raises(OaklandFetchError, match="unsupported XML declarations"):
        await malformed_fetcher.fetch(_source())

    async def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sitemap("https://www.oaklandca.gov/Departments/Events"),
            request=request,
        )

    empty_fetcher = OaklandCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(empty_handler)
    )
    with pytest.raises(OaklandFetchError, match="no approved event detail URLs"):
        await empty_fetcher.fetch(_source())


async def test_oakland_rejects_tampered_registry_profile_without_a_request() -> None:
    """Registry edits cannot widen the reviewed sitemap, cadence, origin, or request budget (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_sitemap(_EVENT_ONE), request=request)

    fetcher = OaklandCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))
    for unsafe_source in (
        replace(_source(), source_key="unreviewed-oakland-calendar"),
        replace(_source(), seed_url=f"{_SEED_URL}?page=2"),
        replace(
            _source(),
            approved_origins=("https://www.oaklandca.gov", "https://example.test"),
        ),
        replace(_source(), refresh_interval_minutes=60),
        replace(_source(), min_interval_ms=1_500),
        replace(_source(), page_limit=159),
    ):
        with pytest.raises(ValueError, match="reviewed sitemap"):
            await fetcher.fetch(unsafe_source)
    assert requested == []
