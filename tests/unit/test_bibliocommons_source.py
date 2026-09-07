"""SCCLD Milpitas BiblioCommons RSS adapter tests (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.bibliocommons.source import (
    BiblioCommonsCatalogFetcher,
    BiblioCommonsFetchError,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.ports.sources import SourceTransientError

_BC_NAMESPACE = "http://bibliocommons.com/rss/1.0/modules/event/"


def _source(*, min_interval_ms: int = 1_500) -> CatalogSource:
    return CatalogSource(
        source_key="sccld-milpitas-events",
        display_name="SCCLD Milpitas Library Events",
        publisher="Santa Clara County Library District",
        seed_url="https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=MI",
        approved_origins=("https://gateway.bibliocommons.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.BIBLIOCOMMONS_RSS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=min_interval_ms,
        page_limit=15,
    )


def _feed(items: str) -> str:
    return f'<rss xmlns:bc="{_BC_NAMESPACE}" version="2.0"><channel>{items}</channel></rss>'


def _item(
    identifier: str,
    *,
    start: str = "2026-07-18T22:30:00Z",
    end: str = "2026-07-18T23:30:00Z",
    cancelled: str = "false",
    virtual: str = "false",
    location_id: str | None = "MI",
    guid_identifier: str | None = None,
    link_identifier: str | None = None,
    title: str = "Cupcake Wars for Grades K-5",
) -> str:
    guid_id = guid_identifier or identifier
    link_id = link_identifier or identifier
    location = ""
    if location_id is not None:
        location = f"""
        <bc:location><bc:id>{location_id}</bc:id><bc:name>Milpitas Library</bc:name>
        <bc:city>Milpitas</bc:city><bc:latitude>37.4324496</bc:latitude>
        <bc:longitude>-121.9065079</bc:longitude><bc:location_details>Activity Room</bc:location_details>
        </bc:location>"""
    return f"""
    <item><title><![CDATA[{title}]]></title><description><![CDATA[<p>Public <em>library</em> event.</p>]]></description>
    <link>https://sccl.bibliocommons.com/events/{link_id}</link>
    <guid isPermaLink="true">https://sccl.bibliocommons.com/events/{guid_id}</guid>
    <bc:start_date>{start}</bc:start_date><bc:end_date>{end}</bc:end_date>
    <bc:is_cancelled>{cancelled}</bc:is_cancelled><bc:is_virtual>{virtual}</bc:is_virtual>{location}</item>
    """


async def test_bibliocommons_maps_only_reviewed_physical_milpitas_events() -> None:
    """RSS event instances retain publisher truth while cancelled, virtual, wrong-location, and mismatched rows skip."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_feed(
                _item("6a0764bac6a1dc3d00d3f67b")
                + _item("virtual", virtual="true", location_id=None)
                + _item("cancelled", cancelled="true", virtual="true", location_id=None)
                + _item("wrong-location", location_id="SC")
                + _item("hybrid", virtual="true")
                + _item("mismatched", guid_identifier="guid-id", link_identifier="link-id")
            ),
            request=request,
        )

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.title for candidate in candidates] == [
        "Cupcake Wars for Grades K-5",
        "Cupcake Wars for Grades K-5",
    ]
    physical = candidates[0]
    assert physical.source_event_id == (
        "bibliocommons:sccld-milpitas-events:6a0764bac6a1dc3d00d3f67b:2026-07-18T22:30:00+00:00"
    )
    assert (
        physical.registration_url
        == "https://sccl.bibliocommons.com/events/6a0764bac6a1dc3d00d3f67b"
    )
    assert physical.description == "Public library event."
    assert physical.price_status is PriceStatus.UNKNOWN
    assert physical.venue_name == "Milpitas Library — Activity Room"
    assert physical.city == "Milpitas"
    assert physical.geo is not None
    assert (physical.geo.lat, physical.geo.lon) == (37.4324496, -121.9065079)
    assert physical.start_at.tzinfo is UTC
    assert requested == [
        httpx.URL("https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=MI")
    ]


async def test_bibliocommons_pages_at_a_human_cadence_until_the_first_short_page() -> None:
    """The adapter constructs bounded page URLs and never trusts a feed-provided next link (FR-10.3/10.4)."""
    requested: list[httpx.URL] = []
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    first_page = "".join(_item(f"first-{index}") for index in range(25))

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        page = request.url.params.get("page")
        body = _feed(first_page if page is None else _item("second-page"))
        return httpx.Response(200, text=body, request=request)

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert len(candidates) == 26
    assert [request.params.get("page") for request in requested] == [None, "2"]
    assert all(request.params["locations"] == "MI" for request in requested)
    assert slept == [1.5]


async def test_bibliocommons_retries_transient_transport_failures_at_a_human_cadence() -> None:
    """A brief DNS/connectivity failure does not make an otherwise healthy source terminal."""
    current = 100.0
    attempts = 0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary DNS failure", request=request)
        return httpx.Response(200, text=_feed(_item("recovered")), request=request)

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source(min_interval_ms=1))

    assert [candidate.title for candidate in candidates] == ["Cupcake Wars for Grades K-5"]
    assert attempts == 3
    assert slept == [0.5, 1.0]


async def test_bibliocommons_defers_after_bounded_transport_retries_are_exhausted() -> None:
    """A longer resolver outage remains durable retry work rather than a terminal source defect."""
    current = 100.0
    attempts = 0

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        current += delay

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("temporary DNS failure", request=request)

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        clock=clock,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SourceTransientError, match="transient transport failure") as raised:
        await fetcher.fetch(_source(min_interval_ms=1))

    assert attempts == 3
    assert raised.value.retry_after_seconds == 15.0


async def test_bibliocommons_rejects_an_unapproved_redirect_before_requesting_it() -> None:
    """The RSS endpoint cannot bounce the worker to an arbitrary origin (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/rss"},
            request=request,
        )

    fetcher = BiblioCommonsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(BiblioCommonsFetchError, match="approved endpoint"):
        await fetcher.fetch(_source())
    assert requested == [
        "https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=MI"
    ]


async def test_bibliocommons_fails_closed_when_all_reviewed_pages_are_full() -> None:
    """Without a count/next link, a full final page is a retryable cap overflow (NFR-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page") or "1"
        full_page = "".join(_item(f"full-{page}-{index}") for index in range(25))
        return httpx.Response(200, text=_feed(full_page), request=request)

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BiblioCommonsFetchError, match="exceeds"):
        await fetcher.fetch(_source(min_interval_ms=1))


async def test_bibliocommons_rejects_unsafe_xml_and_unreviewed_seed_before_a_request() -> None:
    """Entity-bearing XML and a wrong endpoint never become a partial catalog effect (NFR-8/FR-10.3)."""
    requested: list[str] = []

    async def entity_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            text="<!DOCTYPE rss [<!ENTITY unsafe 'x'>]><rss><channel /></rss>",
            request=request,
        )

    fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(entity_handler)
    )
    with pytest.raises(BiblioCommonsFetchError, match="unsupported XML"):
        await fetcher.fetch(_source())
    assert len(requested) == 1

    source = CatalogSource(
        source_key="wrong-bibliocommons-endpoint",
        display_name="Wrong BiblioCommons endpoint",
        publisher="Events Concierge tests",
        seed_url="https://gateway.bibliocommons.com/v2/libraries/sccl/rss/other?locations=MI",
        approved_origins=("https://gateway.bibliocommons.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.BIBLIOCOMMONS_RSS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    wrong_fetcher = BiblioCommonsCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(entity_handler)
    )
    with pytest.raises(ValueError, match="reviewed publisher"):
        await wrong_fetcher.fetch(source)
    assert len(requested) == 1
