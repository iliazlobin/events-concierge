"""City of Oakland sitemap/detail adapter coverage (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

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


def _daily_occurrences(start: datetime, count: int) -> str:
    occurrences: list[str] = []
    for offset in range(count):
        occurrence_start = start + timedelta(days=offset)
        occurrence_end = occurrence_start + timedelta(hours=1)
        occurrences.append(
            _occurrence(
                start=(
                    occurrence_start.year,
                    occurrence_start.month,
                    occurrence_start.day,
                    occurrence_start.hour,
                    occurrence_start.minute,
                ),
                end=(
                    occurrence_end.year,
                    occurrence_end.month,
                    occurrence_end.day,
                    occurrence_end.hour,
                    occurrence_end.minute,
                ),
            )
        )
    return "".join(occurrences)


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


def _address_only_location(
    *,
    address: str = "2633 Telegraph Ave, Suite #109, Oakland, CA 94612",
    coordinates: str = "37.8155572,-122.2682634",
) -> str:
    return "".join(
        (
            '<div class="gmap-marker">',
            f'<div class="gmap-address">{address}</div>',
            f'<div class="gmap-latlong">{coordinates}</div>',
            f'<div class="gmap-info"><p>{address}</p></div>',
            '<div class="gmap-title">Event title is not a venue name</div>',
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


async def test_oakland_skips_expired_detail_before_requiring_current_location_markup() -> None:
    """A stale sitemap detail cannot fail a current run solely because its old venue markup drifted."""
    stale_event = "https://www.oaklandca.gov/Event-Calendar/EMSD/OFD-157"
    past = _occurrence(
        start=(2026, 3, 13, 17, 0),
        end=(2026, 3, 13, 20, 0),
    )
    incomplete_location = "".join(
        (
            '<div class="gmap-marker">',
            '<div class="gmap-info"><p>150 Frank H. Ogawa Plaza</p></div>',
            '<div class="gmap-address">150 Frank H. Ogawa Plaza, Oakland, CA 94612</div>',
            '<div class="gmap-latlong">37.8055,-122.2727</div>',
            "</div>",
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEED_URL:
            return httpx.Response(200, text=_sitemap(stale_event), request=request)
        if str(request.url) == stale_event:
            return httpx.Response(
                200,
                text=_detail(
                    title="Expired Oakland fire event",
                    canonical_url=stale_event,
                    occurrences=past,
                    location=incomplete_location,
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    candidates = await OaklandCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    ).fetch(_source())

    assert candidates == []


async def test_oakland_keeps_address_only_physical_location_without_guessing_venue() -> None:
    """An explicit address and coordinate establish place without promoting the event title to venue."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEED_URL:
            return httpx.Response(200, text=_sitemap(_EVENT_ONE), request=request)
        if str(request.url) == _EVENT_ONE:
            return httpx.Response(
                200,
                text=_detail(
                    title="Future Oakland event",
                    canonical_url=_EVENT_ONE,
                    occurrences=_occurrence(),
                    location=_address_only_location(),
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    [candidate] = await OaklandCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    ).fetch(_source())

    assert candidate.venue_name is None
    assert candidate.city == "Oakland"
    assert candidate.geo is not None
    assert (candidate.geo.lat, candidate.geo.lon) == (37.8155572, -122.2682634)
    assert candidate.raw["venue_name"] is None
    assert candidate.raw["address"] == "2633 Telegraph Ave, Suite #109, Oakland, CA 94612"


async def test_oakland_keeps_incomplete_address_only_marker_fail_closed() -> None:
    """Omitting the coordinate from an address-only marker cannot silently establish a physical event."""
    incomplete_location = "".join(
        (
            '<div class="gmap-marker">',
            '<div class="gmap-address">2633 Telegraph Ave, Oakland, CA 94612</div>',
            '<div class="gmap-info"><p>2633 Telegraph Ave, Oakland, CA 94612</p></div>',
            "</div>",
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEED_URL:
            return httpx.Response(200, text=_sitemap(_EVENT_ONE), request=request)
        if str(request.url) == _EVENT_ONE:
            return httpx.Response(
                200,
                text=_detail(
                    title="Future Oakland event",
                    canonical_url=_EVENT_ONE,
                    occurrences=_occurrence(),
                    location=incomplete_location,
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with pytest.raises(OaklandFetchError, match="physical-location contract"):
        await OaklandCatalogFetcher(
            user_agent="test",
            now=lambda: _NOW,
            transport=httpx.MockTransport(handler),
        ).fetch(_source())


async def test_oakland_bounds_raw_scan_separately_from_future_candidate_emission() -> None:
    """Historical recurrences may expand the scan, while the emitted future set remains capped."""
    event_url = (
        "https://www.oaklandca.gov/Event-Calendar/Public-Works/KONO-Ambassador-Neighborhood-Cleanup"
    )
    occurrences = _daily_occurrences(datetime(2026, 4, 6, 7, 0), 102) + _daily_occurrences(
        datetime(2026, 7, 18, 7, 0), 153
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEED_URL:
            return httpx.Response(200, text=_sitemap(event_url), request=request)
        if str(request.url) == event_url:
            return httpx.Response(
                200,
                text=_detail(
                    title="KONO Ambassador Neighborhood Cleanup",
                    canonical_url=event_url,
                    occurrences=occurrences,
                    location=_address_only_location(),
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async def no_sleep(_: float) -> None:
        return None

    candidates = await OaklandCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=lambda: 0.0,
        sleep=no_sleep,
        transport=httpx.MockTransport(handler),
    ).fetch(_source())

    assert len(candidates) == 153
    assert candidates[0].start_at.isoformat() == "2026-07-18T07:00:00-07:00"
    assert candidates[-1].start_at.isoformat() == "2026-12-17T07:00:00-08:00"


@pytest.mark.parametrize(
    ("occurrences", "expected_error"),
    (
        (_daily_occurrences(datetime(2025, 1, 1, 7, 0), 401), "400 raw-occurrence"),
        (_daily_occurrences(datetime(2026, 7, 18, 7, 0), 201), "200 future-occurrence"),
    ),
)
async def test_oakland_rejects_raw_scan_or_future_candidate_overflow(
    occurrences: str,
    expected_error: str,
) -> None:
    """The larger scan allowance does not make either input work or candidate output unbounded."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEED_URL:
            return httpx.Response(200, text=_sitemap(_EVENT_ONE), request=request)
        if str(request.url) == _EVENT_ONE:
            return httpx.Response(
                200,
                text=_detail(
                    title="Bounded recurring event",
                    canonical_url=_EVENT_ONE,
                    occurrences=occurrences,
                    location=_physical_location(),
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async def no_sleep(_: float) -> None:
        return None

    with pytest.raises(OaklandFetchError, match=expected_error):
        await OaklandCatalogFetcher(
            user_agent="test",
            now=lambda: _NOW,
            clock=lambda: 0.0,
            sleep=no_sleep,
            transport=httpx.MockTransport(handler),
        ).fetch(_source())


async def test_oakland_skips_text_only_calendar_landing_without_dropping_one_segment_event() -> (
    None
):
    """A sitemap category stub is empty, while a real one-segment event still produces a candidate."""
    landing_url = "https://www.oaklandca.gov/Event-Calendar/EWD"
    nested_landing_url = (
        "https://www.oaklandca.gov/Event-Calendar/Police/Recruiting-Background-Unit"
    )
    event_url = "https://www.oaklandca.gov/Event-Calendar/GPU-D1-Workshop"
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if str(request.url) == _SEED_URL:
            return httpx.Response(
                200,
                text=_sitemap(landing_url, nested_landing_url, event_url),
                request=request,
            )
        if str(request.url) == landing_url:
            return httpx.Response(
                200,
                text=(
                    "<html><head>"
                    f'<link rel="canonical" href="{landing_url}">'
                    "</head><body>"
                    '<div id="main-content" class="main-container clearfix">'
                    "<!-- OC Layout Element Content Template -->"
                    "<!--normalTemplateStart-->EWD<!--normalTemplateEnd-->"
                    "</div></body></html>"
                ),
                request=request,
            )
        if str(request.url) == nested_landing_url:
            return httpx.Response(
                200,
                text=(
                    "<html><head>"
                    f'<link rel="canonical" href="{nested_landing_url}">'
                    "</head><body>"
                    '<div id="main-content" class="main-container clearfix">'
                    "<!-- OC Layout Element Content Template -->"
                    "<!--normalTemplateStart-->"
                    "Recruiting &amp; Background Unit"
                    "<!--normalTemplateEnd-->"
                    "</div></body></html>"
                ),
                request=request,
            )
        if str(request.url) == event_url:
            return httpx.Response(
                200,
                text=_detail(
                    title="District 1 General Plan Update Community Workshop",
                    canonical_url=event_url,
                    occurrences=_occurrence(),
                    location=_physical_location(),
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async def no_sleep(_: float) -> None:
        return None

    candidates = await OaklandCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=lambda: 0.0,
        sleep=no_sleep,
        transport=httpx.MockTransport(handler),
    ).fetch(_source())

    assert [candidate.title for candidate in candidates] == [
        "District 1 General Plan Update Community Workshop"
    ]
    assert requested == [_SEED_URL, landing_url, nested_landing_url, event_url]


async def test_oakland_keeps_structured_title_occurrence_drift_fail_closed() -> None:
    """Missing event selectors are skippable only for the exact text-only calendar-root shape."""
    landing_url = "https://www.oaklandca.gov/Event-Calendar/EWD"

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEED_URL:
            return httpx.Response(200, text=_sitemap(landing_url), request=request)
        if str(request.url) == landing_url:
            return httpx.Response(
                200,
                text=(
                    '<html><body><div id="main-content"><section>EWD</section></div></body></html>'
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with pytest.raises(OaklandFetchError, match="title/occurrence contract"):
        await OaklandCatalogFetcher(
            user_agent="test",
            now=lambda: _NOW,
            transport=httpx.MockTransport(handler),
        ).fetch(_source())


async def test_oakland_keeps_text_only_landing_word_mismatch_fail_closed() -> None:
    """Punctuation may differ from a slug, but an extra factual word cannot be discarded."""
    landing_url = "https://www.oaklandca.gov/Event-Calendar/Police/Recruiting-Background-Unit"

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEED_URL:
            return httpx.Response(200, text=_sitemap(landing_url), request=request)
        if str(request.url) == landing_url:
            return httpx.Response(
                200,
                text=(
                    "<html><body>"
                    '<div id="main-content">Recruiting and Background Unit</div>'
                    "</body></html>"
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with pytest.raises(OaklandFetchError, match="title/occurrence contract"):
        await OaklandCatalogFetcher(
            user_agent="test",
            now=lambda: _NOW,
            transport=httpx.MockTransport(handler),
        ).fetch(_source())


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
