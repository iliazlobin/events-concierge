"""Stanford Localist adapter tests (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.localist.source import LocalistCatalogFetcher, LocalistFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus


def _source(*, page_limit: int = 20) -> CatalogSource:
    return CatalogSource(
        source_key="stanford-events",
        display_name="Stanford Events Localist Calendar",
        publisher="Stanford University",
        seed_url="https://events.stanford.edu/api/2/events",
        approved_origins=("https://events.stanford.edu",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.LOCALIST_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=page_limit,
    )


def _sjsu_source() -> CatalogSource:
    return CatalogSource(
        source_key="sjsu-events",
        display_name="SJSU Events Localist Calendar",
        publisher="San José State University",
        seed_url="https://events.sjsu.edu/api/2/events",
        approved_origins=("https://events.sjsu.edu",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.LOCALIST_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=20,
    )


def _ucsf_source() -> CatalogSource:
    return CatalogSource(
        source_key="ucsf-events",
        display_name="UCSF Events Localist Calendar",
        publisher="University of California, San Francisco",
        seed_url="https://calendar.ucsf.edu/api/2/events",
        approved_origins=("https://calendar.ucsf.edu",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.LOCALIST_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=20,
    )


def _instance(
    instance_id: int,
    event_id: int,
    *,
    start: str = "2026-07-20T17:30:00-07:00",
    end: str | None = "2026-07-20T19:00:00-07:00",
) -> dict[str, object]:
    return {
        "event_instance": {
            "id": instance_id,
            "event_id": event_id,
            "start": start,
            "end": end,
        }
    }


def _event(
    event_id: int,
    instance_id: int,
    *,
    title: str = "Stanford public event",
    experience: str = "inperson",
    status: str = "live",
    private: bool = False,
    rejected: bool = False,
    free: bool | None = True,
    ticket_cost: str | None = None,
    location_name: str | None = "Memorial Auditorium",
    address: str | None = "551 Serra Mall",
    latitude: str = "37.427500",
    longitude: str = "-122.169700",
    city: str = "Stanford",
    localist_url: str | None = None,
    description: str = "<p>Official <em>description</em>.</p>",
    instances: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": event_id,
        "title": title,
        "status": status,
        "experience": experience,
        "private": private,
        "rejected": rejected,
        "free": free,
        "ticket_cost": ticket_cost,
        "location_name": location_name,
        "address": address,
        "geo": {
            "latitude": latitude,
            "longitude": longitude,
            "street": address,
            "city": city,
        },
        "description_text": description,
        "localist_url": localist_url or f"https://events.stanford.edu/event/event-{event_id}",
        "event_instances": instances or [_instance(instance_id, event_id)],
    }


def _page(
    events: list[dict[str, object]], *, current: int = 1, total: int = 1
) -> dict[str, object]:
    return {
        "events": [{"event": event} for event in events],
        "page": {"current": current, "size": 100, "total": total},
        "date": {"first": "2026-07-17", "last": "2026-10-15"},
    }


async def test_localist_expands_eligible_occurrences_and_preserves_conservative_source_truth() -> (
    None
):
    """Physical/hybrid future events map per instance; virtual, private, invalid, and out-of-range rows do not."""
    requested: list[httpx.URL] = []
    recurring_instances = [
        _instance(5001, 1001),
        _instance(
            5002,
            1001,
            start="2026-07-21T17:30:00-07:00",
            end="2026-07-21T19:00:00-07:00",
        ),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            json=_page(
                [
                    _event(1001, 5001, instances=recurring_instances),
                    _event(1002, 5003, experience="hybrid", free=False, ticket_cost="$25"),
                    _event(1003, 5004, free=False, ticket_cost=None),
                    _event(1004, 5005, experience="virtual"),
                    _event(1005, 5006, private=True),
                    _event(1006, 5007, latitude="40.0", longitude="-122.0"),
                    _event(
                        1007,
                        5008,
                        instances=[
                            _instance(
                                5008,
                                1007,
                                start="2026-10-15T00:00:00-07:00",
                            )
                        ],
                    ),
                    _event(
                        1008,
                        5009,
                        localist_url="https://unapproved.example.test/event/event-1008",
                    ),
                    _event(
                        1009,
                        5010,
                        instances=[
                            _instance(
                                5010,
                                1009,
                                start="2026-07-20T19:00:00-07:00",
                                end="2026-07-20T18:00:00-07:00",
                            )
                        ],
                    ),
                ]
            ),
            request=request,
        )

    fetcher = LocalistCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 19, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "localist:stanford-events:1001:5001",
        "localist:stanford-events:1001:5002",
        "localist:stanford-events:1002:5003",
        "localist:stanford-events:1003:5004",
    ]
    assert [candidate.price_status for candidate in candidates] == [
        PriceStatus.FREE,
        PriceStatus.FREE,
        PriceStatus.PAID,
        PriceStatus.UNKNOWN,
    ]
    assert candidates[0].description == "Official description."
    assert candidates[0].venue_name == "Memorial Auditorium — 551 Serra Mall"
    assert candidates[0].city == "Stanford"
    assert candidates[0].geo is not None
    assert (candidates[0].geo.lat, candidates[0].geo.lon) == (37.4275, -122.1697)
    assert candidates[0].registration_url == "https://events.stanford.edu/event/event-1001"
    assert len(requested) == 1


async def test_localist_constructs_every_declared_page_and_applies_per_host_pacing() -> None:
    """The adapter owns Localist bounds/order/page query parameters and checks declared pagination (FR-10.3)."""
    requested: list[httpx.URL] = []
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    first_page = [_event(2000 + offset, 3000 + offset) for offset in range(100)]

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=_page(first_page, current=1, total=2), request=request)
        return httpx.Response(
            200, json=_page([_event(2100, 3100)], current=2, total=2), request=request
        )

    fetcher = LocalistCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert len(candidates) == 101
    assert [request.params["page"] for request in requested] == ["1", "2"]
    assert all(request.params["start"] == "2026-07-17" for request in requested)
    assert all(request.params["end"] == "2026-10-15" for request in requested)
    assert all(request.params["bounds"] == "37.0,-123.0,38.5,-121.5" for request in requested)
    assert all(request.params["pp"] == "100" for request in requested)
    assert all(request.params["sort"] == "date" for request in requested)
    assert all(request.params["direction"] == "asc" for request in requested)
    assert slept == [1.5]


async def test_localist_uses_the_closed_sjsu_publisher_spec_and_its_own_handoff_host() -> None:
    """A second reviewed Localist publisher works without allowing an arbitrary registry host (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            json=_page(
                [
                    _event(
                        2500,
                        3500,
                        localist_url="https://events.sjsu.edu/event/sjsu-event-2500",
                    )
                ]
            ),
            request=request,
        )

    fetcher = LocalistCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_sjsu_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "localist:sjsu-events:2500:3500"
    ]
    assert candidates[0].registration_url == "https://events.sjsu.edu/event/sjsu-event-2500"
    assert requested[0].startswith("https://events.sjsu.edu/api/2/events?")


async def test_localist_uses_the_closed_ucsf_publisher_spec_and_its_own_handoff_host() -> None:
    """UCSF can join the reviewed set without letting registry data select an arbitrary host (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            json=_page(
                [
                    _event(
                        2600,
                        3600,
                        localist_url="https://calendar.ucsf.edu/event/ucsf-event-2600",
                    )
                ]
            ),
            request=request,
        )

    fetcher = LocalistCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_ucsf_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "localist:ucsf-events:2600:3600"
    ]
    assert candidates[0].registration_url == "https://calendar.ucsf.edu/event/ucsf-event-2600"
    assert requested[0].startswith("https://calendar.ucsf.edu/api/2/events?")


async def test_localist_fails_closed_when_its_declared_page_total_exceeds_the_reviewed_cap() -> (
    None
):
    """A publisher page count above the source ceiling never returns a silently partial feed (NFR-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page([_event(3000 + offset, 4000 + offset) for offset in range(100)], total=2),
            request=request,
        )

    fetcher = LocalistCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(LocalistFetchError, match="exceeds"):
        await fetcher.fetch(_source(page_limit=1))


async def test_localist_rejects_an_unapproved_redirect_before_requesting_the_target() -> None:
    """The reviewed Localist API cannot redirect the catalog worker to another origin (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/events"},
            request=request,
        )

    fetcher = LocalistCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(LocalistFetchError, match="approved endpoint"):
        await fetcher.fetch(_source())
    assert len(requested) == 1


async def test_localist_fails_closed_when_occurrence_identity_repeats_across_pages() -> None:
    """Duplicate instance ids across pages signal unstable pagination and prevent a partial ingest (NFR-8)."""
    first_page = [_event(4000 + offset, 5000 + offset) for offset in range(100)]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=_page(first_page, current=1, total=2), request=request)
        return httpx.Response(
            200, json=_page([_event(4000, 5000)], current=2, total=2), request=request
        )

    fetcher = LocalistCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(LocalistFetchError, match="repeated"):
        await fetcher.fetch(_source())


async def test_localist_refuses_an_unreviewed_endpoint_before_a_request() -> None:
    """An approved origin alone cannot repurpose the typed adapter to another Localist resource (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=_page([]), request=request)

    source = CatalogSource(
        source_key="wrong-localist-endpoint",
        display_name="Wrong Localist endpoint",
        publisher="Events Concierge tests",
        seed_url="https://events.stanford.edu/api/2/venues",
        approved_origins=("https://events.stanford.edu",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.LOCALIST_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    fetcher = LocalistCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed public events"):
        await fetcher.fetch(source)
    assert requested == []
