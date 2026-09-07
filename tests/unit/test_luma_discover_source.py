"""Closed-contract tests for reviewed public Luma regional Discover cursors."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TypedDict

import httpx
import pytest

from events_concierge.adapters.luma_discover.source import (
    LumaDiscoverCatalogFetcher,
    LumaDiscoverFetchError,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import (
    CatalogSourceMode,
    PriceStatus,
    RegistrationStatus,
    Source,
)

_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
_ORIGINS = ("https://api.luma.com", "https://api2.luma.com")


class _RegionProfile(TypedDict):
    display_name: str
    place_id: str
    public_url: str
    region: str
    revision: int


_REGIONS: dict[str, _RegionProfile] = {
    "luma-sf": {
        "display_name": "Luma Bay Area",
        "place_id": "discplace-BDj7GNbGlsF7Cka",
        "public_url": "https://luma.com/sf",
        "region": "bay_area_9_county",
        "revision": 2,
    },
    "luma-nyc": {
        "display_name": "Luma New York",
        "place_id": "discplace-Izx1rQVSh8njYpP",
        "public_url": "https://luma.com/nyc",
        "region": "new_york_metro",
        "revision": 1,
    },
}


def _source(
    *,
    source_key: str = "luma-sf",
    page_limit: int = 40,
    approved_origins: tuple[str, ...] | None = None,
) -> CatalogSource:
    profile = _REGIONS[source_key]
    place_id = profile["place_id"]
    return CatalogSource(
        source_key=source_key,
        display_name=profile["display_name"],
        publisher="Luma Discover",
        seed_url=(
            "https://api.luma.com/discover/get-paginated-events"
            f"?discover_place_api_id={place_id}&pagination_limit=25"
        ),
        approved_origins=_ORIGINS if approved_origins is None else approved_origins,
        region=profile["region"],
        mode=CatalogSourceMode.LUMA_DISCOVER_JSON,
        enabled=True,
        reviewed_at=_NOW,
        review_expires_at=None,
        refresh_interval_minutes=120,
        min_interval_ms=1_500,
        page_limit=page_limit,
        handoff_only=True,
        source_revision=profile["revision"],
    )


def _entry(
    suffix: str,
    *,
    start_at: str = "2026-07-28T01:00:00.000Z",
    visibility: object = "public",
    entry_api_id: str | None = None,
    calendar_api_id: str | None = None,
    listed_calendar_api_id: str | None = None,
    coordinate: object = None,
    ticket_info: object = None,
    calendar_name: object = "AI Builders",
    registration_availability: object = "open",
    guest_count: object = 24,
    city: str = "San Francisco",
) -> dict[str, object]:
    event_api_id = f"evt-{suffix}"
    owner_calendar_api_id = calendar_api_id or f"cal-owner-{suffix}"
    return {
        "api_id": entry_api_id or event_api_id,
        "calendar": {
            "api_id": listed_calendar_api_id or owner_calendar_api_id,
            "name": calendar_name,
        },
        "guest_count": guest_count,
        "hosts": [{"name": f"Host {suffix}"}],
        "registration_availability": registration_availability,
        "ticket_info": ticket_info,
        "event": {
            "api_id": event_api_id,
            "calendar_api_id": owner_calendar_api_id,
            "visibility": visibility,
            "name": f"Luma event {suffix}",
            "url": f"luma-event-{suffix}",
            "start_at": start_at,
            "end_at": "2026-07-28T03:00:00.000Z",
            "coordinate": coordinate,
            "geo_address_info": {
                "city": city,
                "address": "Reviewed Venue",
            },
        },
    }


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


def _detail_payload(
    entry: dict[str, object],
    *,
    description_mirror: object = None,
    event_api_id: str | None = None,
) -> dict[str, object]:
    event = entry["event"]
    calendar = entry["calendar"]
    assert isinstance(event, dict)
    assert isinstance(calendar, dict)
    return {
        "api_id": event_api_id or event["api_id"],
        "calendar": {"api_id": calendar["api_id"]},
        "description_mirror": description_mirror,
        "event": {
            "api_id": event["api_id"],
            "calendar_api_id": event["calendar_api_id"],
            "name": event["name"],
            "url": event["url"],
            "visibility": event["visibility"],
        },
    }


def _description(*paragraphs: str) -> dict[str, object]:
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": paragraph}],
            }
            for paragraph in paragraphs
        ],
    }


async def test_luma_discover_walks_cursor_and_enriches_public_future_handoffs() -> None:
    free = _entry(
        "free",
        coordinate={"latitude": 37.78, "longitude": -122.4},
        ticket_info={"is_free": True, "price": None, "max_price": None},
    )
    paid = _entry(
        "paid",
        registration_availability="waitlist",
        guest_count=0,
        ticket_info={
            "is_free": False,
            "price": {"cents": 2_500, "currency": "usd"},
        },
    )
    pages = {
        None: _payload(
            [free, _entry("unlisted", visibility="unlisted")],
            has_more=True,
            next_cursor="cursor-one",
        ),
        "cursor-one": _payload(
            [paid, _entry("past", start_at="2026-07-26T01:00:00.000Z")],
            has_more=False,
        ),
    }
    by_api_id = {"evt-free": free, "evt-paid": paid}
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api2.luma.com":
            event_api_id = request.url.params["event_api_id"]
            entry = by_api_id[event_api_id]
            mirror = (
                _description(
                    "Luma event free",
                    "Useful details for free.",
                    "Second paragraph.",
                )
                if event_api_id == "evt-free"
                else None
            )
            return httpx.Response(
                200,
                json=_detail_payload(entry, description_mirror=mirror),
                request=request,
            )
        return httpx.Response(
            200,
            json=pages[request.url.params.get("pagination_cursor")],
            request=request,
        )

    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    events = await fetcher.fetch(_source())

    assert [event.source_event_id for event in events] == [
        "https://luma.com/luma-event-free",
        "https://luma.com/luma-event-paid",
    ]
    assert all(event.source is Source.PUBLIC_JSONLD for event in events)
    assert [event.price_status for event in events] == [PriceStatus.FREE, PriceStatus.PAID]
    assert (
        events[0].price_min_cents,
        events[0].price_max_cents,
        events[0].price_currency,
    ) == (None, None, None)
    assert (
        events[1].price_min_cents,
        events[1].price_max_cents,
        events[1].price_currency,
    ) == (2_500, 2_500, "USD")
    assert events[0].geo is not None
    assert (events[0].geo.lat, events[0].geo.lon) == (37.78, -122.4)
    assert (events[0].venue_name, events[0].city) == ("Reviewed Venue", "San Francisco")
    assert events[0].description == "Useful details for free.\n\nSecond paragraph."
    assert events[1].description == ""
    assert events[0].organizer_name == "AI Builders"
    assert events[0].host_names == ("Host free",)
    assert events[0].attendance_count == 24
    assert events[0].registration_status is RegistrationStatus.OPEN
    assert events[1].attendance_count is None
    assert events[1].registration_status is RegistrationStatus.WAITLIST
    assert [request.url.host for request in requests] == [
        "api.luma.com",
        "api.luma.com",
        "api2.luma.com",
        "api2.luma.com",
    ]
    assert [request.url.params.get("pagination_cursor") for request in requests[:2]] == [
        None,
        "cursor-one",
    ]
    assert all(request.headers["referer"] == "https://luma.com/sf" for request in requests[:2])
    assert requests[2].headers["x-luma-web-url"] == "https://luma.com/luma-event-free"
    assert requests[3].headers["x-luma-client-type"] == "luma-web"
    assert slept == [1.5, 1.5]


async def test_luma_discover_extracts_bounded_public_roles_and_attendance() -> None:
    entry = _entry("roles", guest_count=12)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            payload = _detail_payload(
                entry,
                description_mirror=_description(
                    "Hosted with Bolt, Workato, and Antler VC.",
                    "• Ada Lovelace (Founder, Analytical Engines)",
                    "6:05 PM Bolt Introduction",
                    "Eric Simons CEO at Bolt",
                    "6:10 PM Apify Introduction",
                    "Petros Hong Dev Community Manager at Apify",
                    "• 10:30 - Meet at Cha Cha Matcha (optional)",
                    "Join us to hear a lecture by Marko Jukic author of the featured essay.",
                ),
            )
            payload.update(
                {
                    "calendar": {
                        "api_id": "cal-owner-roles",
                        "name": "Detail Organizer",
                        "is_personal": False,
                        "linkedin_handle": "/company/detail-organizer",
                    },
                    "hosts": [
                        {
                            "name": "Primary Host",
                            "linkedin_handle": "/in/primary-host",
                        },
                        {
                            "name": "Community Host",
                            "linkedin_handle": (
                                "https://www.linkedin.com/search/results/people/"
                                "?keywords=Community%20Host"
                            ),
                        },
                        {"name": None, "first_name": None, "last_name": None},
                    ],
                    "guest_count": 142,
                    "registration_availability": "sold-out",
                    "sessions": [
                        {
                            "speakers": [
                                {
                                    "name": "Grace Hopper",
                                    "linkedin_handle": "/in/grace-hopper",
                                }
                            ],
                            "hosts": [
                                {
                                    "name": "Session Host",
                                    "linkedin_handle": "/company/session-host",
                                }
                            ],
                        }
                    ],
                    "featured_infos": [
                        {"type": "calendar", "name": "Build Club"},
                        {"type": "discover", "name": "San Francisco"},
                    ],
                    # Deliberately ignored: attendee identities are not part of our projection.
                    "featured_guests": [
                        {
                            "name": "Private Attendee",
                            "linkedin_handle": "/in/private-attendee",
                        }
                    ],
                }
            )
            return httpx.Response(200, json=payload, request=request)
        return httpx.Response(
            200,
            json=_payload([entry], has_more=False),
            request=request,
        )

    fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    [event] = await fetcher.fetch(_source())

    assert event.organizer_name == "Detail Organizer"
    assert event.host_names == ("Primary Host", "Community Host")
    assert event.speaker_names == (
        "Grace Hopper",
        "Session Host",
        "Ada Lovelace",
        "Eric Simons",
        "Petros Hong",
        "Marko Jukic",
    )
    assert "10:30 - Meet at Cha Cha Matcha" not in event.speaker_names
    assert event.partner_names == ("Bolt", "Workato", "Antler VC")
    assert [profile.as_payload() for profile in event.entity_profiles] == [
        {
            "name": "Detail Organizer",
            "role": "organizer",
            "kind": "organization",
            "profile_url": "https://www.linkedin.com/company/detail-organizer",
        },
        {
            "name": "Primary Host",
            "role": "host",
            "kind": "person",
            "profile_url": "https://www.linkedin.com/in/primary-host",
        },
        {
            "name": "Grace Hopper",
            "role": "speaker",
            "kind": "person",
            "profile_url": "https://www.linkedin.com/in/grace-hopper",
        },
        {
            "name": "Session Host",
            "role": "speaker",
            "kind": "organization",
            "profile_url": "https://www.linkedin.com/company/session-host",
        },
    ]
    assert event.attendance_count == 142
    assert event.registration_status is RegistrationStatus.SOLD_OUT
    assert "Private Attendee" not in (
        event.organizer_name,
        *event.host_names,
        *event.speaker_names,
        *event.partner_names,
        *(profile.name for profile in event.entity_profiles),
    )


async def test_luma_discover_uses_visible_ticket_types_and_explicit_host_organizations() -> None:
    entry = _entry(
        "clean",
        ticket_info={
            "is_free": False,
            "price": None,
            "max_price": None,
        },
    )
    event = entry["event"]
    assert isinstance(event, dict)
    event["name"] = "Corgi x Briq: French Founder Meetup 🇫🇷"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            payload = _detail_payload(
                entry,
                description_mirror=_description(
                    "Corgi x Briq: French Founder Meetup",
                    "Your hosts:",
                    "Corgi — the full-stack insurance carrier.",
                    "Briq — helping tech brands build trust.",
                    "Come for the croissant, stay for the conversation.",
                ),
            )
            payload.update(
                {
                    "ticket_info": {
                        "is_free": False,
                        "price": None,
                        "max_price": None,
                    },
                    "ticket_types": [
                        {
                            "type": "free",
                            "is_disabled": False,
                            "is_hidden": False,
                        }
                    ],
                    # Discovery attribution is not partner/vendor evidence.
                    "featured_infos": [
                        {"type": "calendar", "name": "Bond AI - San Francisco"}
                    ],
                }
            )
            return httpx.Response(200, json=payload, request=request)
        return httpx.Response(
            200,
            json=_payload([entry], has_more=False),
            request=request,
        )

    fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    [event_candidate] = await fetcher.fetch(_source())

    assert event_candidate.price_status is PriceStatus.FREE
    assert event_candidate.is_free is True
    assert event_candidate.description == (
        "Your hosts:\n\n"
        "Corgi — the full-stack insurance carrier.\n\n"
        "Briq — helping tech brands build trust.\n\n"
        "Come for the croissant, stay for the conversation."
    )
    assert event_candidate.partner_names == ("Corgi", "Briq")
    assert "Bond AI - San Francisco" not in event_candidate.partner_names


async def test_luma_discover_accepts_only_the_two_reviewed_region_profiles() -> None:
    entry = _entry("nyc", city="New York")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api2.luma.com":
            return httpx.Response(
                200,
                json=_detail_payload(entry, description_mirror=_description("NYC details")),
                request=request,
            )
        return httpx.Response(
            200,
            json=_payload([entry], has_more=False),
            request=request,
        )

    fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    events = await fetcher.fetch(_source(source_key="luma-nyc"))

    assert len(events) == 1
    assert events[0].city == "New York"
    assert requests[0].url.params["discover_place_api_id"] == "discplace-Izx1rQVSh8njYpP"
    assert requests[0].headers["referer"] == "https://luma.com/nyc"


async def test_luma_discover_rejects_repeated_cursor_and_capped_partial_set() -> None:
    calls = 0

    async def repeated_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=_payload([_entry(str(calls))], has_more=True, next_cursor="same"),
            request=request,
        )

    repeated_fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(repeated_handler),
    )
    with pytest.raises(LumaDiscoverFetchError, match="repeated a pagination cursor"):
        await repeated_fetcher.fetch(_source())
    assert calls == 2

    async def capped_handler(request: httpx.Request) -> httpx.Response:
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

    capped_fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(capped_handler),
    )
    with pytest.raises(LumaDiscoverFetchError, match="2-page cap"):
        await capped_fetcher.fetch(_source(page_limit=2))


@pytest.mark.parametrize(
    "entry,error",
    [
        (_entry("visibility", visibility=None), "invalid event visibility"),
        (_entry("entry-id", entry_api_id="evt-other"), "mismatched Discover event identity"),
        (
            _entry("calendar-id", listed_calendar_api_id="cal-other"),
            "mismatched Discover calendar identity",
        ),
    ],
)
async def test_luma_discover_rejects_malformed_public_identity(
    entry: dict[str, object],
    error: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload([entry], has_more=False),
            request=request,
        )

    fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LumaDiscoverFetchError, match=error):
        await fetcher.fetch(_source())


async def test_luma_discover_rejects_duplicate_events_before_detail_enrichment() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
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

    fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        sleep=_no_sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LumaDiscoverFetchError, match="repeated an event identity"):
        await fetcher.fetch(_source())
    assert all(request.url.host == "api.luma.com" for request in requests)


@pytest.mark.parametrize(
    "detail_payload,error",
    [
        ({"api_id": "evt-other"}, "mismatched public identity"),
        (
            {
                "type": "doc",
                "content": "not-a-node-list",
            },
            "invalid event description content",
        ),
    ],
)
async def test_luma_discover_rejects_mismatched_or_malformed_detail(
    detail_payload: dict[str, object],
    error: str,
) -> None:
    entry = _entry("detail")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api2.luma.com":
            payload = (
                detail_payload
                if "api_id" in detail_payload
                else _detail_payload(entry, description_mirror=detail_payload)
            )
            return httpx.Response(200, json=payload, request=request)
        return httpx.Response(
            200,
            json=_payload([entry], has_more=False),
            request=request,
        )

    fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LumaDiscoverFetchError, match=error):
        await fetcher.fetch(_source())


async def test_luma_discover_requires_exact_endpoint_mode_and_api_origins() -> None:
    fetcher = LumaDiscoverCatalogFetcher(user_agent="test", now=lambda: _NOW)
    extra_origin = _source(approved_origins=(*_ORIGINS, "https://other.example.test"))
    wrong_place = replace(
        _source(),
        seed_url=(
            "https://api.luma.com/discover/get-paginated-events"
            "?discover_place_api_id=discplace-unreviewed&pagination_limit=25"
        ),
    )
    wrong_mode = replace(_source(), mode=CatalogSourceMode.LUMA_CALENDAR_JSON)

    with pytest.raises(ValueError, match="approve only"):
        await fetcher.fetch(extra_origin)
    with pytest.raises(ValueError, match="reviewed public cursor endpoint"):
        await fetcher.fetch(wrong_place)
    with pytest.raises(ValueError, match="unsupported Luma Discover"):
        await fetcher.fetch(wrong_mode)


@pytest.mark.parametrize("redirect_host", ["api.luma.com", "api2.luma.com"])
async def test_luma_discover_never_follows_list_or_detail_redirects(
    redirect_host: str,
) -> None:
    entry = _entry("redirect")
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == redirect_host:
            return httpx.Response(
                302,
                headers={"location": "https://unapproved.example.test/events"},
                request=request,
            )
        return httpx.Response(
            200,
            json=_payload([entry], has_more=False),
            request=request,
        )

    fetcher = LumaDiscoverCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LumaDiscoverFetchError, match="left its reviewed endpoint"):
        await fetcher.fetch(_source())
    assert requests


async def _no_sleep(_delay: float) -> None:
    return None
