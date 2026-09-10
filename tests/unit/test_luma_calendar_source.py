"""Closed-contract tests for the reviewed public Luma calendar cursor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from tests.support.ingestion_telemetry import capture_collection_progress

from events_concierge.adapters.luma_calendar.source import (
    LumaCalendarCatalogFetcher,
    LumaCalendarFetchError,
)
from events_concierge.adapters.luma_common import luma_public_entity_profiles
from events_concierge.domain.catalog_sources import CatalogCollectionWindow, CatalogSource
from events_concierge.domain.enums import (
    CatalogSourceMode,
    PriceStatus,
    RegistrationStatus,
    Source,
)

_CALENDAR_API_ID = "cal-JTdFQadEz0AOxyV"
#: Listings this module has handed out, so the shared transport can answer their detail request.
_LISTED: dict[str, dict[str, object]] = {}
_SEED_URL = (
    "https://api.luma.com/calendar/get-items"
    f"?calendar_api_id={_CALENDAR_API_ID}&pagination_limit=20&period=future"
)
_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
_ORIGINS = ("https://api.luma.com", "https://api2.luma.com")


def _source(
    *, page_limit: int = 10, approved_origins: tuple[str, ...] | None = None
) -> CatalogSource:
    return CatalogSource(
        source_key="luma-genai-sf",
        display_name="Generative AI SF",
        publisher="Generative AI SF",
        seed_url=_SEED_URL,
        approved_origins=approved_origins or _ORIGINS,
        region="bay_area_9_county",
        mode=CatalogSourceMode.LUMA_CALENDAR_JSON,
        enabled=True,
        reviewed_at=_NOW,
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=page_limit,
        handoff_only=True,
        source_revision=2,
    )


def _entry(
    suffix: str,
    *,
    start_at: str = "2026-07-25T01:00:00.000Z",
    coordinate: object = None,
    address: object = None,
    ticket_info: object = None,
    status: object = "approved",
    platform: object = "luma",
) -> dict[str, object]:
    entry: dict[str, object] = {
        "calendar_api_id": _CALENDAR_API_ID,
        "status": status,
        "platform": platform,
        "ticket_info": ticket_info,
        "calendar": {"api_id": _CALENDAR_API_ID, "name": "Reviewed Calendar"},
        "event": {
            "api_id": f"evt-{suffix}",
            "calendar_api_id": _CALENDAR_API_ID,
            "name": f"Luma event {suffix}",
            "url": f"luma-event-{suffix}",
            "visibility": "public",
            "start_at": start_at,
            "end_at": "2026-07-25T03:00:00.000Z",
            "coordinate": coordinate,
            "geo_address_info": address,
        },
    }
    _LISTED[f"evt-{suffix}"] = entry
    return entry


def _detail_payload(entry: dict[str, object]) -> dict[str, object]:
    """The minimum record ``enrichment_from_detail`` will accept for this listing."""
    event = entry["event"]
    calendar = entry["calendar"]
    assert isinstance(event, dict)
    assert isinstance(calendar, dict)
    return {
        "api_id": event["api_id"],
        "calendar": {"api_id": calendar["api_id"], "name": calendar["name"]},
        "description_mirror": None,
        "event": {
            "api_id": event["api_id"],
            "calendar_api_id": event["calendar_api_id"],
            "name": event["name"],
            "url": event["url"],
            "visibility": event["visibility"],
        },
    }


def _with_details(
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    """Answer the detail lane from the listing, so page tests stay about pagination.

    Every retained event now costs one ``api2.luma.com`` GET. Tests that are about cursors, caps,
    or admission should not have to restate that; the ones that are about enrichment build their
    own responses instead.
    """

    async def routed(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            event_api_id = request.url.params.get("event_api_id")
            listed = _LISTED[str(event_api_id)]
            return httpx.Response(200, json=_detail_payload(listed), request=request)
        return await handler(request)

    return routed


def _payload(
    entries: list[object],
    *,
    has_more: object,
    next_cursor: object = None,
) -> dict[str, object]:
    return {
        "entries": entries,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


async def test_luma_calendar_walks_every_cursor_and_normalizes_public_handoffs() -> None:
    pages = {
        None: _payload(
            [
                _entry(
                    "free",
                    coordinate={"latitude": 37.78, "longitude": -122.4},
                    address={
                        "city": "San Francisco",
                        "address": "Reviewed Venue",
                        "full_address": "Private detail is not needed",
                    },
                    ticket_info={"is_free": True, "price": None, "max_price": None},
                ),
                _entry(
                    "mixed",
                    ticket_info={
                        "is_free": True,
                        "price": None,
                        "max_price": {"cents": 8_000, "currency": "usd"},
                    },
                ),
            ],
            has_more=True,
            next_cursor="cursor-one",
        ),
        "cursor-one": _payload(
            [
                _entry(
                    "paid",
                    ticket_info={
                        "is_free": False,
                        "price": {"cents": 2_500, "currency": "usd"},
                        "max_price": {"cents": 2_500, "currency": "usd"},
                    },
                ),
                _entry(
                    "paid-mixed-currency",
                    ticket_info={
                        "is_free": False,
                        "price": {"cents": 2_500, "currency": "usd"},
                        "max_price": {"cents": 5_000, "currency": "eur"},
                    },
                ),
            ],
            has_more=True,
            next_cursor="cursor-two",
        ),
        "cursor-two": _payload(
            [_entry("past", start_at="2026-07-23T01:00:00.000Z")],
            has_more=False,
        ),
    }
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        cursor = request.url.params.get("pagination_cursor")
        return httpx.Response(200, json=pages[cursor], request=request)

    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    async with capture_collection_progress(_source().source_key) as progress:
        events = await fetcher.fetch(_source())

    assert progress[-1].request_count == 7
    assert progress[-1].page_count == 3
    assert progress[-1].candidate_count == 4

    assert [event.source_event_id for event in events] == [
        "https://luma.com/luma-event-free",
        "https://luma.com/luma-event-mixed",
        "https://luma.com/luma-event-paid",
        "https://luma.com/luma-event-paid-mixed-currency",
    ]
    assert all(event.source is Source.PUBLIC_JSONLD for event in events)
    assert [event.price_status for event in events] == [
        PriceStatus.FREE,
        PriceStatus.UNKNOWN,
        PriceStatus.PAID,
        PriceStatus.PAID,
    ]
    assert [
        (event.price_min_cents, event.price_max_cents, event.price_currency)
        for event in events
    ] == [
        (None, None, None),
        (None, None, None),
        (2_500, 2_500, "USD"),
        (None, None, None),
    ]
    assert events[0].geo is not None
    assert (events[0].geo.lat, events[0].geo.lon) == (37.78, -122.4)
    assert (events[0].venue_name, events[0].city) == (
        "Reviewed Venue",
        "San Francisco",
    )
    assert [request.url.params.get("pagination_cursor") for request in requests] == [
        None,
        "cursor-one",
        "cursor-two",
    ]
    assert all(request.headers["referer"] == "https://luma.com/" for request in requests)
    # Pacing is per host and now covers both lanes: 3 listing GETs on api.luma.com (2 waits) and
    # 4 detail GETs on api2.luma.com (3 waits).
    assert slept == [1.5] * 5


async def test_luma_calendar_extracts_only_structured_direct_public_profiles() -> None:
    entry = _entry("profiles")
    entry.update(
        {
            "calendar": {
                "api_id": _CALENDAR_API_ID,
                "name": "Generative AI SF",
                "is_personal": False,
                "linkedin_handle": "/company/generative-ai-sf",
                "website": "https://genai.example.test",
            },
            "hosts": [
                {
                    "name": "Ada Lovelace",
                    "linkedin_handle": "/in/ada-lovelace",
                    "website": "https://ada.example.test",
                },
                {
                    "name": "Search Result",
                    "linkedin_handle": (
                        "https://www.linkedin.com/search/results/people/"
                        "?keywords=Search%20Result"
                    ),
                },
            ],
            # Attendee/featured-guest records are never passed to profile extraction.
            "featured_guests": [
                {
                    "name": "Private Attendee",
                    "linkedin_handle": "/in/private-attendee",
                }
            ],
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            # The detail record repeats the same public calendar/host structures, and it is what
            # the enrichment reads; the listing's copy is superseded.
            detail = _detail_payload(entry)
            detail["calendar"] = entry["calendar"]
            detail["hosts"] = entry["hosts"]
            detail["featured_guests"] = entry["featured_guests"]
            return httpx.Response(200, json=detail, request=request)
        return httpx.Response(
            200,
            json=_payload([entry], has_more=False),
            request=request,
        )

    [event] = await LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    ).fetch(_source())

    assert event.organizer_name == "Generative AI SF"
    assert event.host_names == ("Ada Lovelace", "Search Result")
    assert [profile.as_payload() for profile in event.entity_profiles] == [
        {
            "name": "Generative AI SF",
            "role": "organizer",
            "kind": "organization",
            "profile_url": "https://www.linkedin.com/company/generative-ai-sf",
        },
        {
            "name": "Ada Lovelace",
            "role": "host",
            "kind": "person",
            "profile_url": "https://www.linkedin.com/in/ada-lovelace",
        },
    ]
    assert "Private Attendee" not in (
        event.organizer_name,
        *event.host_names,
        *(profile.name for profile in event.entity_profiles),
    )


def test_luma_profiles_reject_wrong_kind_handle_and_use_explicit_organization_site() -> None:
    profiles = luma_public_entity_profiles(
        calendar_value={
            "name": "Mission Athletic Club",
            "is_personal": False,
            "linkedin_handle": "/in/themissionathleticclub",
            "website": "https://www.missionathletic.club/#events",
        },
        hosts_value=(),
        sessions_value=(),
        organizer_name="Mission Athletic Club",
        host_names=(),
        speaker_names=(),
    )

    assert [profile.as_payload() for profile in profiles] == [
        {
            "name": "Mission Athletic Club",
            "role": "organizer",
            "kind": "organization",
            "profile_url": "https://www.missionathletic.club/",
        }
    ]


def test_luma_profiles_omit_wrong_kind_handle_without_explicit_site() -> None:
    profiles = luma_public_entity_profiles(
        calendar_value={
            "name": "Mission Athletic Club",
            "is_personal": False,
            "linkedin_handle": "/in/themissionathleticclub",
        },
        hosts_value=(),
        sessions_value=(),
        organizer_name="Mission Athletic Club",
        host_names=(),
        speaker_names=(),
    )

    assert profiles == ()


async def test_luma_calendar_rejects_a_repeated_cursor() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=_payload(
                [_entry(str(calls))],
                has_more=True,
                next_cursor="same-cursor",
            ),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    with pytest.raises(LumaCalendarFetchError, match="repeated a pagination cursor"):
        await fetcher.fetch(_source())

    assert calls == 2


async def test_luma_calendar_fails_instead_of_publishing_a_capped_partial_cursor() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("pagination_cursor")
        return httpx.Response(
            200,
            json=_payload(
                [_entry(cursor or "first")],
                has_more=True,
                next_cursor=f"after-{cursor or 'first'}",
            ),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    with pytest.raises(LumaCalendarFetchError, match="2-page cap"):
        await fetcher.fetch(_source(page_limit=2))


@pytest.mark.parametrize(
    "page,error",
    [
        ({"entries": None, "has_more": False}, "malformed pagination"),
        ({"entries": [], "has_more": 1}, "malformed pagination"),
        (
            {"entries": [], "has_more": True, "next_cursor": "cursor"},
            "incomplete cursor",
        ),
        (
            {
                "entries": [{"status": "approved", "platform": "luma"}],
                "has_more": False,
            },
            "returned no event object",
        ),
    ],
)
async def test_luma_calendar_rejects_malformed_page_or_event(
    page: dict[str, object],
    error: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page, request=request)

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    with pytest.raises(LumaCalendarFetchError, match=error):
        await fetcher.fetch(_source())


async def test_luma_calendar_rejects_duplicate_events_across_pages() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("pagination_cursor")
        return httpx.Response(
            200,
            json=(
                _payload([_entry("duplicate")], has_more=True, next_cursor="next")
                if cursor is None
                else _payload([_entry("duplicate")], has_more=False)
            ),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    with pytest.raises(LumaCalendarFetchError, match="repeated an event identity"):
        await fetcher.fetch(_source())


async def test_luma_calendar_excludes_every_entry_not_explicitly_approved() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(
                [
                    _entry("approved"),
                    _entry("cancelled", status="cancelled"),
                    _entry("pending", status="pending"),
                ],
                has_more=False,
            ),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    events = await fetcher.fetch(_source())

    assert [event.source_event_id for event in events] == ["https://luma.com/luma-event-approved"]


async def test_luma_calendar_accepts_approved_syndicated_events_from_the_reviewed_listing() -> None:
    syndicated = _entry("syndicated")
    event = syndicated["event"]
    assert isinstance(event, dict)
    event["calendar_api_id"] = "cal-cohosted-calendar"
    # A cross-posted entry names its OWNER calendar in the event and in the calendar block; only
    # the entry-level calendar_api_id is the listing calendar. Measured on the live cursor.
    syndicated["calendar"] = {"api_id": "cal-cohosted-calendar", "name": "Co-hosted Calendar"}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload([syndicated], has_more=False),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    events = await fetcher.fetch(_source())

    assert [event.source_event_id for event in events] == ["https://luma.com/luma-event-syndicated"]


async def test_luma_calendar_rejects_an_entry_not_listed_by_the_reviewed_calendar() -> None:
    unreviewed = _entry("unreviewed")
    unreviewed["calendar_api_id"] = "cal-other-calendar"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload([unreviewed], has_more=False),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    with pytest.raises(LumaCalendarFetchError, match="event from another calendar"):
        await fetcher.fetch(_source())


async def test_luma_calendar_rejects_a_malformed_publication_status() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload([_entry("missing-status", status=None)], has_more=False),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    with pytest.raises(LumaCalendarFetchError, match="invalid publication status"):
        await fetcher.fetch(_source())


async def test_luma_calendar_requires_the_exact_reviewed_endpoint_and_origin() -> None:
    fetcher = LumaCalendarCatalogFetcher(user_agent="test", now=lambda: _NOW)
    extra_origin = _source(approved_origins=("https://api.luma.com", "https://other.example.test"))

    with pytest.raises(ValueError, match="approve only"):
        await fetcher.fetch(extra_origin)


@pytest.mark.parametrize(
    "seed_url",
    [
        # Another host, another path, another scheme: never the reviewed cursor.
        "https://luma.com/calendar/get-items"
        "?calendar_api_id=cal-JTdFQadEz0AOxyV&pagination_limit=20&period=future",
        "https://api.luma.com/calendar/list-items"
        "?calendar_api_id=cal-JTdFQadEz0AOxyV&pagination_limit=20&period=future",
        "http://api.luma.com/calendar/get-items"
        "?calendar_api_id=cal-JTdFQadEz0AOxyV&pagination_limit=20&period=future",
        "https://api.luma.com:8443/calendar/get-items"
        "?calendar_api_id=cal-JTdFQadEz0AOxyV&pagination_limit=20&period=future",
        # A page size or period this adapter does not pace or bound.
        "https://api.luma.com/calendar/get-items"
        "?calendar_api_id=cal-JTdFQadEz0AOxyV&pagination_limit=200&period=future",
        "https://api.luma.com/calendar/get-items"
        "?calendar_api_id=cal-JTdFQadEz0AOxyV&pagination_limit=20&period=past",
        # A calendar identity that is not a calendar identity.
        "https://api.luma.com/calendar/get-items"
        "?calendar_api_id=usr-Pkw0QQy2QRm3nLU&pagination_limit=20&period=future",
        "https://api.luma.com/calendar/get-items"
        "?calendar_api_id=cal-has%20space&pagination_limit=20&period=future",
        "https://api.luma.com/calendar/get-items"
        "?calendar_api_id=&pagination_limit=20&period=future",
        # A parameter the reviewed contract does not carry, or one carried twice.
        "https://api.luma.com/calendar/get-items"
        "?calendar_api_id=cal-JTdFQadEz0AOxyV&pagination_limit=20&period=future&member=1",
        "https://api.luma.com/calendar/get-items?calendar_api_id=cal-JTdFQadEz0AOxyV"
        "&calendar_api_id=cal-other&pagination_limit=20&period=future",
        "https://api.luma.com/calendar/get-items?calendar_api_id=cal-JTdFQadEz0AOxyV&period=future",
        # A cursor may only ever be appended by this adapter, never carried by a reviewed row.
        "https://api.luma.com/calendar/get-items?calendar_api_id=cal-JTdFQadEz0AOxyV"
        "&pagination_limit=20&period=future&pagination_cursor=seeded",
    ],
)
async def test_luma_calendar_rejects_a_seed_that_is_not_the_reviewed_cursor(seed_url: str) -> None:
    """The calendar identity is read off the reviewed row, so the row's shape is the fence.

    An off-origin or non-HTTPS seed is refused one layer earlier, by the ``CatalogSource``
    invariant itself; either way no request is ever made.
    """
    fetcher = LumaCalendarCatalogFetcher(user_agent="test", now=lambda: _NOW)

    with pytest.raises(
        ValueError,
        match=r"reviewed public cursor endpoint|owner-approved|must use HTTPS",
    ):
        await fetcher.fetch(replace(_source(), seed_url=seed_url))


async def test_luma_calendar_serves_any_reviewed_host_calendar_row() -> None:
    """A second reviewed host calendar needs a source row, not an edit to this module.

    A city Discover feed lists about one event per calendar, so host coverage can only come from
    host calendars. Adding one must therefore be an owner-reviewed registry change.
    """
    other_calendar = "cal-ahTi4ptrN9WCYkg"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entry = _entry("commons")
        entry["calendar_api_id"] = other_calendar
        entry["calendar"] = {"api_id": other_calendar, "name": "The Commons"}
        event = entry["event"]
        assert isinstance(event, dict)
        event["calendar_api_id"] = other_calendar
        return httpx.Response(
            200,
            json=_payload([entry], has_more=False),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(_with_details(handler)),
    )
    source = replace(
        _source(),
        source_key="luma-thecommons-sf",
        seed_url=(
            "https://api.luma.com/calendar/get-items"
            f"?calendar_api_id={other_calendar}&pagination_limit=20&period=future"
        ),
    )

    events = await fetcher.fetch(source)

    assert [event.source_event_id for event in events] == ["https://luma.com/luma-event-commons"]
    assert requests[0].url.params.get("calendar_api_id") == other_calendar


async def test_luma_calendar_still_refuses_a_source_that_is_not_handoff_only() -> None:
    fetcher = LumaCalendarCatalogFetcher(user_agent="test", now=lambda: _NOW)

    with pytest.raises(ValueError, match="handoff-only"):
        await fetcher.fetch(replace(_source(), handoff_only=False))


async def test_luma_calendar_never_follows_a_redirect() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/events"},
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    with pytest.raises(LumaCalendarFetchError, match="left its reviewed endpoint"):
        await fetcher.fetch(_source())

    assert requests == [_SEED_URL]


async def _no_sleep(_delay: float) -> None:
    return None


async def test_luma_calendar_skips_off_platform_listings_without_failing_the_walk() -> None:
    """A calendar may list an external event; it has no Luma identity and is not a candidate.

    Measured on the live fleet, 7 of Postman Developer Events' 17 future entries are
    ``platform: "external"`` listings that carry no ``api_id``, no ``calendar_api_id``, no
    ``visibility``, and a third-party absolute URL. Before this skip, one of them failed the whole
    refresh and the calendar published nothing at all.
    """
    external = {
        "calendar_api_id": _CALENDAR_API_ID,
        "status": "approved",
        "platform": "external",
        "event": {
            "name": "apidays Toronto",
            "url": "https://www.apidays.global/events/toronto",
            "start_at": "2026-07-25T13:00:00.000Z",
            "host": "apidays",
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload([external, _entry("native"), external], has_more=False),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    events = await fetcher.fetch(_source())

    assert [event.source_event_id for event in events] == ["https://luma.com/luma-event-native"]


@pytest.mark.parametrize("platform", [None, 17, ["luma"]])
async def test_luma_calendar_refuses_an_entry_that_declares_no_platform(platform: object) -> None:
    """An undeclared hosting platform is a surface change, not something to guess at."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload([_entry("unknown", platform=platform)], has_more=False),
            request=request,
        )

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(_with_details(handler)),
    )

    with pytest.raises(LumaCalendarFetchError, match="no hosting platform"):
        await fetcher.fetch(_source())


async def test_luma_calendar_describes_events_through_the_shared_detail_lane() -> None:
    """A host calendar must describe an event exactly as the Discover shelf does.

    Both sources can hold the same event, and the catalog keeps whichever fetch ran most recently.
    If only one of them followed the detail lane, the reader's view of an event would depend on
    which source refreshed last.
    """
    entry = _entry("described")
    detail_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            detail_requests.append(request)
            detail = _detail_payload(entry)
            detail["description_mirror"] = {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "An evening of reviewed prose."}],
                    }
                ],
            }
            detail["hosts"] = [{"name": "Ada Lovelace", "linkedin_handle": "/in/ada-lovelace"}]
            detail["guest_count"] = 69
            detail["registration_availability"] = "waitlist"
            return httpx.Response(200, json=detail, request=request)
        return httpx.Response(200, json=_payload([entry], has_more=False), request=request)

    [event] = await LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    ).fetch(_source())

    assert event.description == "An evening of reviewed prose."
    assert event.host_names == ("Ada Lovelace",)
    assert event.attendance_count == 69
    assert event.registration_status is RegistrationStatus.WAITLIST
    assert [request.url.params.get("event_api_id") for request in detail_requests] == [
        "evt-described"
    ]
    assert all(request.url.host == "api2.luma.com" for request in detail_requests)


async def test_luma_calendar_refuses_a_detail_record_for_another_event() -> None:
    """The detail record must re-assert the identity the listing already validated."""
    entry = _entry("swapped")
    other = _entry("other")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            return httpx.Response(200, json=_detail_payload(other), request=request)
        return httpx.Response(200, json=_payload([entry], has_more=False), request=request)

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LumaCalendarFetchError, match="mismatched public identity"):
        await fetcher.fetch(_source())


async def test_luma_calendar_never_leaves_the_two_reviewed_origins() -> None:
    entry = _entry("origins")
    hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "api2.luma.com":
            return httpx.Response(200, json=_detail_payload(entry), request=request)
        return httpx.Response(200, json=_payload([entry], has_more=False), request=request)

    await LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    ).fetch(_source())

    assert set(hosts) == {"api.luma.com", "api2.luma.com"}


async def test_luma_calendar_refuses_a_row_that_does_not_approve_the_detail_origin() -> None:
    """A row left on the listing origin refuses to run rather than skipping enrichment."""
    fetcher = LumaCalendarCatalogFetcher(user_agent="test", now=lambda: _NOW)

    with pytest.raises(ValueError, match="reviewed API origins"):
        await fetcher.fetch(_source(approved_origins=("https://api.luma.com",)))


@pytest.mark.parametrize(
    ("slug", "admitted"),
    [
        ("fw.models.nyc", True),
        ("pubyhw73", True),
        ("a.b", True),
        ("..", False),
        ("../etc/passwd", False),
        ("-leading-dash", False),
        ("has/slash", False),
        ("has:colon", False),
    ],
)
async def test_luma_calendar_admits_a_dotted_custom_slug_but_nothing_that_leaves_the_path(
    slug: str,
    admitted: bool,
) -> None:
    """https://luma.com/fw.models.nyc is a live public event; rejecting it failed a whole calendar.

    A dot cannot reach the host and cannot begin a slug, so admitting one does not widen where the
    registration handoff can point.
    """
    entry = _entry("slugged")
    event = entry["event"]
    assert isinstance(event, dict)
    event["url"] = slug

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            return httpx.Response(200, json=_detail_payload(entry), request=request)
        return httpx.Response(200, json=_payload([entry], has_more=False), request=request)

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    )

    if not admitted:
        with pytest.raises(LumaCalendarFetchError, match="unsafe event URL"):
            await fetcher.fetch(_source())
        return
    [event_out] = await fetcher.fetch(_source())
    assert event_out.registration_url == f"https://luma.com/{slug}"


@pytest.mark.parametrize(
    ("availability", "expected"),
    [
        ("open", RegistrationStatus.OPEN),
        ("waitlist", RegistrationStatus.WAITLIST),
        ("sold-out", RegistrationStatus.SOLD_OUT),
        # Registration has not opened yet. There is no posture for that here, and the honest
        # answer is that we cannot say it is open.
        ("coming-soon", RegistrationStatus.UNKNOWN),
        (None, RegistrationStatus.UNKNOWN),
    ],
)
async def test_luma_calendar_maps_every_public_registration_state(
    availability: object,
    expected: RegistrationStatus,
) -> None:
    """An unmapped state fails the whole refresh, so every live one must be accounted for."""
    entry = _entry("registration")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            detail = _detail_payload(entry)
            detail["registration_availability"] = availability
            return httpx.Response(200, json=detail, request=request)
        return httpx.Response(200, json=_payload([entry], has_more=False), request=request)

    [event] = await LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    ).fetch(_source())

    assert event.registration_status is expected


async def test_luma_calendar_still_refuses_a_registration_state_it_has_never_seen() -> None:
    """A genuinely new state is a surface change and must not be silently flattened."""
    entry = _entry("unmapped")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            detail = _detail_payload(entry)
            detail["registration_availability"] = "invite-only"
            return httpx.Response(200, json=detail, request=request)
        return httpx.Response(200, json=_payload([entry], has_more=False), request=request)

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LumaCalendarFetchError, match="unknown registration state"):
        await fetcher.fetch(_source())


async def test_luma_calendar_never_publishes_an_unlisted_event() -> None:
    """A calendar cursor lists unlisted events; only ``visibility`` says they are not public.

    The "calendar" normalization contract does not assert visibility the way the Discover one
    does, so without an explicit filter this adapter would publish a non-public event whenever its
    detail record happened to agree — and fail the ENTIRE calendar whenever it did not, because the
    shared detail lane requires ``visibility == "public"``.
    """
    unlisted = _entry("unlisted")
    event = unlisted["event"]
    assert isinstance(event, dict)
    event["visibility"] = "unlisted"
    public = _entry("public")
    detail_requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            api_id = str(request.url.params.get("event_api_id"))
            detail_requests.append(api_id)
            return httpx.Response(200, json=_detail_payload(_LISTED[api_id]), request=request)
        return httpx.Response(
            200,
            json=_payload([unlisted, public], has_more=False),
            request=request,
        )

    events = await LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    ).fetch(_source())

    assert [event.source_event_id for event in events] == ["https://luma.com/luma-event-public"]
    # It is also never fetched: an unlisted event costs no request and leaves no trace.
    assert detail_requests == ["evt-public"]


@pytest.mark.parametrize("visibility", [None, 17, ["public"]])
async def test_luma_calendar_refuses_an_entry_with_no_stated_visibility(visibility: object) -> None:
    entry = _entry("visibility")
    event = entry["event"]
    assert isinstance(event, dict)
    event["visibility"] = visibility

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload([entry], has_more=False), request=request)

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LumaCalendarFetchError, match="invalid event visibility"):
        await fetcher.fetch(_source())



async def test_calendar_window_skips_outside_details_but_walks_later_unsorted_pages() -> None:
    inside = _entry("inside")
    outside = _entry("outside", start_at="2026-07-27T01:00:00.000Z")
    outside["event"]["end_at"] = "2026-07-27T03:00:00.000Z"
    window = CatalogCollectionWindow(2, 2, _NOW, datetime(2026, 7, 26, 12, tzinfo=UTC), 1)
    requested: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        if request.url.host == "api2.luma.com":
            assert request.url.params["event_api_id"] == "evt-inside"
            return httpx.Response(200, json=_detail_payload(inside), request=request)
        cursor = request.url.params.get("pagination_cursor")
        return httpx.Response(200, json=(
            _payload([outside], has_more=True, next_cursor="next") if cursor is None
            else _payload([inside], has_more=False)
        ), request=request)

    fetcher = LumaCalendarCatalogFetcher(
        user_agent="test", now=lambda: datetime(2026, 8, 15, tzinfo=UTC),
        sleep=_no_sleep, clock=lambda: 0.0, transport=httpx.MockTransport(handler),
    )
    candidates = await fetcher.fetch(replace(_source(), collection_window=window))
    assert len(candidates) == 1
    assert len(requested) == 3
    assert requested[1].params["pagination_cursor"] == "next"
