"""Campbell CivicEngage RSS profile coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from xml.sax.saxutils import escape

import httpx
import pytest

from events_concierge.adapters.civic_engage.source import (
    CivicEngageRssCatalogFetcher,
    CivicEngageRssFetchError,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus

_SEED_URL = "https://www.campbellca.gov/RSSFeed.aspx?CID=Recreation-Community-Services-29&ModID=58"
_NAMESPACE = "https://www.campbellca.gov/Calendar.aspx"
_CITY_SEED_URL = "https://www.campbellca.gov/RSSFeed.aspx?CID=All-calendar.xml&ModID=58"
_CITY_NAMESPACE = "https://www.campbellca.gov/m/calendar"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="campbell-events",
        display_name="Campbell Recreation & Community Services Events",
        publisher="City of Campbell Recreation & Community Services",
        seed_url=_SEED_URL,
        approved_origins=("https://www.campbellca.gov",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.CIVIC_ENGAGE_RSS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=1,
    )


def _feed(*items: str) -> str:
    return "\n".join(
        (
            '<?xml version="1.0"?>',
            f'<rss version="2.0" xmlns:calendarEvent="{_NAMESPACE}">',
            "<channel>",
            *items,
            "</channel>",
            "</rss>",
        )
    )


def _item(
    event_id: int,
    occurrence_id: int,
    *,
    title: str = "Family Fun at the Museum",
    event_date: str = "July 18, 2026",
    event_times: str = "11:00 AM - 03:00 PM",
    location: str = "Campbell, CA 95008",
    link: str | None = None,
    guid: str | None = None,
    guid_perma_link: str = "false",
) -> str:
    event_link = link or f"https://www.campbellca.gov/Calendar.aspx?EID={event_id}"
    event_guid = guid or f"{event_link}/{occurrence_id}"
    return "\n".join(
        (
            "<item>",
            f"<title>{escape(title)}</title>",
            f"<link>{escape(event_link)}</link>",
            "<description><![CDATA[<p>Neighborhood <em>music</em> with friends.</p>]]></description>",
            f"<calendarEvent:EventDates>{escape(event_date)}</calendarEvent:EventDates>",
            f"<calendarEvent:EventTimes>{escape(event_times)}</calendarEvent:EventTimes>",
            f"<calendarEvent:Location>{escape(location)}</calendarEvent:Location>",
            f'<guid isPermaLink="{guid_perma_link}">{escape(event_guid)}</guid>',
            "</item>",
        )
    )


async def test_campbell_maps_city_only_and_html_separated_physical_rows() -> None:
    """The closed profile retains explicit Campbell locality without inventing a venue (FR-3.1/FR-3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item(3987, 639160147220000000, title="Free Family Fun at the Museum"),
                _item(
                    3988,
                    639160150490000000,
                    location="51 N. First St.<br>Room BCampbell, CA 95008",
                ),
                _item(3989, 639160150940000000, location="OnlineCampbell, CA 95008"),
                _item(3990, 639160151490000000, title="Pop Up Art Gallery Cancelled"),
                _item(3991, 639160151780000000, event_times="All day"),
                _item(
                    3992,
                    639160152000000000,
                    event_date="July 17, 2026",
                    event_times="01:00 AM - 02:00 AM",
                ),
                _item(3993, 639160153650000000, location="DowntownSan Jose, CA 95113"),
                _item(
                    3994,
                    639160154640000000,
                    link="https://www.campbellca.gov/Calendar.aspx?EID=3994&extra=1",
                ),
                _item(
                    3995,
                    639160155010000000,
                    guid="https://www.campbellca.gov/Calendar.aspx?EID=3995/not-a-tick",
                ),
            ),
            request=request,
        )

    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "campbell:campbell-events:3987:639160147220000000",
        "campbell:campbell-events:3988:639160150490000000",
    ]
    city_only, named_venue = candidates
    assert city_only.registration_url == "https://www.campbellca.gov/Calendar.aspx?EID=3987"
    assert city_only.start_at == datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
    assert city_only.end_at == datetime(2026, 7, 18, 22, 0, tzinfo=UTC)
    assert city_only.venue_name is None
    assert city_only.city == "Campbell"
    assert city_only.geo is None
    assert city_only.description == "Neighborhood music with friends."
    assert city_only.price_status is PriceStatus.UNKNOWN
    assert named_venue.venue_name == "51 N. First St. Room B"
    assert requested == [httpx.URL(_SEED_URL)]


async def test_campbell_rejects_virtual_prefixes_and_retains_physical_first_hybrids() -> None:
    """A leading virtual label is not physical evidence, while a named venue remains source truth (FR-3.1)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_feed(
                _item(3996, 639160155020000000),
                _item(
                    3997,
                    639160155030000000,
                    location="Campbell Community Center — Zoom availableCampbell, CA 95008",
                ),
                _item(3998, 639160155040000000, location="Online MeetingCampbell, CA 95008"),
                _item(3999, 639160155050000000, location="Virtual MeetingCampbell, CA 95008"),
                _item(4000, 639160155060000000, location="Zoom MeetingCampbell, CA 95008"),
                _item(4001, 639160155070000000, location="Remote MeetingCampbell, CA 95008"),
            ),
            request=request,
        )

    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "campbell:campbell-events:3996:639160155020000000",
        "campbell:campbell-events:3997:639160155030000000",
    ]
    city_only, physical_first_hybrid = candidates
    assert city_only.venue_name is None
    assert physical_first_hybrid.venue_name == "Campbell Community Center — Zoom available"


async def test_campbell_applies_pacing_and_fails_closed_for_duplicates_or_cap() -> None:
    """The unpaged publisher document remains paced and bounded before a catalog effect (NFR-8/FR-10.4)."""
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    async def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_feed(), request=request)

    paced_fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test",
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(empty_handler),
    )
    assert await paced_fetcher.fetch(_source()) == []
    assert await paced_fetcher.fetch(_source()) == []
    assert slept == [1.5]

    payloads = iter(
        (
            _feed(_item(4000, 639160200000000000), _item(4000, 639160200000000000)),
            _feed(*(_item(4100 + index, 639160300000000000 + index) for index in range(50))),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(payloads), request=request)

    async def no_sleep(_delay: float) -> None:
        return None

    bounded_fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test",
        clock=lambda: 100.0,
        sleep=no_sleep,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CivicEngageRssFetchError, match="repeated an occurrence"):
        await bounded_fetcher.fetch(_source())
    with pytest.raises(CivicEngageRssFetchError, match="50-item cap"):
        await bounded_fetcher.fetch(_source())


async def test_campbell_rejects_bad_payloads_redirects_and_tampered_profiles() -> None:
    """A Campbell registry record cannot widen the fixed feed or safe handoff boundary (NFR-8)."""
    requested: list[str] = []

    async def invalid_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            content=b"<!DOCTYPE rss [<!ENTITY test 'unsafe'>]><rss />",
            request=request,
        )

    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(invalid_handler)
    )
    with pytest.raises(CivicEngageRssFetchError, match="unsupported XML declarations"):
        await fetcher.fetch(_source())
    assert requested == [_SEED_URL]

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/feed"},
            request=request,
        )

    redirect_fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(redirect_handler)
    )
    with pytest.raises(CivicEngageRssFetchError, match="approved endpoint"):
        await redirect_fetcher.fetch(_source())

    unreviewed = replace(_source(), source_key="unreviewed-campbell-events")
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(unreviewed)
    tampered_seed = replace(_source(), seed_url=f"{_SEED_URL}&page=2")
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(tampered_seed)
    reordered_seed = replace(
        _source(),
        seed_url="https://www.campbellca.gov/RSSFeed.aspx?ModID=58&CID=Recreation-Community-Services-29",
    )
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(reordered_seed)
    tampered_cap = replace(_source(), page_limit=2)
    with pytest.raises(ValueError, match="reviewed RSS endpoint"):
        await fetcher.fetch(tampered_cap)
    assert requested == [_SEED_URL]


async def test_city_wide_feed_parses_current_calendar_paths_and_occurrence_guids() -> None:
    """The public city calendar includes other categories and changed its RSS identity shape."""
    requested: list[str] = []
    payload = _feed(_item(3970, 639234528270000000, title="Public theatre event"))
    payload = payload.replace(_NAMESPACE, _CITY_NAMESPACE).replace(
        "/m/calendar?EID=3970", "/m/calendar/event/detail/3970"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=payload)

    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )
    candidates = await fetcher.fetch(replace(_source(), seed_url=_CITY_SEED_URL))
    assert len(candidates) == 1
    assert candidates[0].registration_url == (
        "https://www.campbellca.gov/m/calendar/event/detail/3970"
    )
    assert candidates[0].source_event_id == "campbell:campbell-events:3970:639234528270000000"
    assert candidates[0].city == "Campbell"
    assert requested == [_CITY_SEED_URL]


@pytest.mark.parametrize(
    "handoff",
    [
        "https://unapproved.example.test/m/calendar/event/detail/3970",
        "https://www.campbellca.gov/m/calendar/event/detail/not-an-id",
        "https://www.campbellca.gov/m/calendar/event/detail/3970/extra",
        "https://www.campbellca.gov/m/calendar/event/detail/3970?extra=1",
    ],
)
async def test_city_wide_feed_does_not_widen_handoff_authority(handoff: str) -> None:
    payload = _feed(_item(3970, 639234528270000000, link=handoff))
    payload = payload.replace(_NAMESPACE, _CITY_NAMESPACE)
    fetcher = CivicEngageRssCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, tzinfo=UTC),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=payload)),
    )
    assert await fetcher.fetch(replace(_source(), seed_url=_CITY_SEED_URL)) == []
