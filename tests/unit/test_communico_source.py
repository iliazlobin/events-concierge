"""Berkeley Public Library Communico adapter tests (FR-3.1/FR-3.7/FR-10.3/FR-10.4)."""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.communico import source as communico_source
from events_concierge.adapters.communico.source import CommunicoCatalogFetcher, CommunicoFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.ports.sources import SourceTransientError


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="berkeley-public-library-events",
        display_name="Berkeley Public Library Events",
        publisher="Berkeley Public Library",
        seed_url="https://berkeleypubliclibrary.libnet.info/eeventcaldata",
        approved_origins=("https://berkeleypubliclibrary.libnet.info",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.COMMUNICO_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
    )


def _event(
    identifier: int,
    *,
    title: str = "Community Board Game Night",
    start: str = "2026-07-18 18:30:00",
    end: str = "2026-07-18 20:30:00",
    event_type: str = "INPERSON",
    library: str | None = "West Branch",
    location: str | None = "West Branch",
    venues: str | None = "Community Meeting Room",
    description: str = "<p>Play <em>together</em>.</p>",
    private: object = "0",
    changed_reason: str | None = None,
) -> dict[str, object]:
    return {
        "id": str(identifier),
        "new_event_id": None,
        "title": title,
        "raw_start_time": start,
        "raw_end_time": end,
        "event_type": event_type,
        "library": library,
        "location": location,
        "venues": venues,
        "description": description,
        "long_description": "<p>Longer publisher description.</p>",
        "private_event": private,
        "changed_reason": changed_reason,
        "url": f"https://berkeleypubliclibrary.libnet.info/event/{identifier}",
        "allow_reg": "1",
        "registration_cost": "0",
    }


async def test_communico_maps_only_future_physical_or_hybrid_library_events() -> None:
    """The fixed source-list request retains source truth and never trusts registration fields (FR-3.1)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            json=[
                _event(1001),
                _event(
                    1002,
                    title="All-Day Community Art",
                    start="2026-07-19 00:00:00",
                    end="2026-07-19 23:59:00",
                    event_type="HYBRID",
                    library="Central Library",
                    venues=None,
                ),
                _event(1003, event_type="ONLINE"),
                _event(1004, private=True),
                _event(1005, title="Community Board Game Night Cancelled"),
                _event(1006, library=None, location=None),
                _event(1007, start="2026-07-17 09:00:00", end="2026-07-17 10:00:00"),
                _event(1008, start="2026-10-15 01:00:00", end="2026-10-15 02:00:00"),
                _event(1009, start="not a timestamp"),
                _event(1001),
            ],
            request=request,
        )

    fetcher = CommunicoCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 19, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "communico:berkeley-public-library-events:1001:2026-07-18T18:30:00-07:00",
        "communico:berkeley-public-library-events:1002:2026-07-19T00:00:00-07:00",
    ]
    physical, all_day = candidates
    assert physical.registration_url == "https://berkeleypubliclibrary.libnet.info/event/1001"
    assert physical.description == "Play together."
    assert physical.venue_name == "West Branch \u2014 Community Meeting Room"
    assert physical.city is None
    assert physical.geo is None
    assert physical.price_status is PriceStatus.UNKNOWN
    assert all_day.end_at == datetime(2026, 7, 19, 23, 59, tzinfo=all_day.start_at.tzinfo)
    assert len(requested) == 1
    assert requested[0].path == "/eeventcaldata"
    assert requested[0].params["event_type"] == "0"
    assert json.loads(requested[0].params["req"]) == {
        "private": False,
        "date": "2026-07-17",
        "days": 91,
    }


async def test_communico_fails_closed_on_a_capped_unpaged_result() -> None:
    """A source response at the reviewed row ceiling cannot be mistaken for a complete calendar (NFR-8)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_event(index + 1) for index in range(communico_source._MAX_ITEMS)],
            request=request,
        )

    fetcher = CommunicoCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(CommunicoFetchError, match="capped"):
        await fetcher.fetch(_source())


async def test_communico_refuses_an_unreviewed_source_key_before_requesting() -> None:
    """The Communico adapter cannot be widened into a generic arbitrary-library client (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=[], request=request)

    source = CatalogSource(
        source_key="unreviewed-communico-library",
        display_name="Unreviewed Communico Library",
        publisher="Events Concierge tests",
        seed_url="https://berkeleypubliclibrary.libnet.info/eeventcaldata",
        approved_origins=("https://berkeleypubliclibrary.libnet.info",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.COMMUNICO_JSON,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    fetcher = CommunicoCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="reviewed public events"):
        await fetcher.fetch(source)
    assert requested == []


async def test_communico_accepts_a_complete_calendar_above_the_old_byte_limit() -> None:
    """A large description must not discard an otherwise complete, bounded public calendar."""
    # Preserve the observed regression size, independently of the new reviewed limit.
    payload = json.dumps([_event(1001, description="x" * 2_016_138)]).encode()
    assert 2_000_000 < len(payload) <= communico_source._MAX_RESPONSE_BYTES

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, request=request)

    fetcher = CommunicoCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 19, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    assert len(await fetcher.fetch(_source())) == 1


async def test_communico_decodes_compressed_calendar_only_once() -> None:
    compressed = gzip.compress(json.dumps([_event(1001)]).encode())

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=compressed, headers={"Content-Encoding": "gzip"}, request=request
        )

    fetcher = CommunicoCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 19, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    assert len(await fetcher.fetch(_source())) == 1


async def test_communico_decoded_response_discards_transport_headers() -> None:
    payload = json.dumps([_event(1001)]).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(payload),
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
                "Connection": "keep-alive, X-Hop-Only",
                "Keep-Alive": "timeout=5",
                "X-Hop-Only": "transport metadata",
            },
            request=request,
        )

    source = _source()
    publisher = communico_source._publisher_for_source(source)
    assert publisher is not None
    fetcher = CommunicoCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    response = await fetcher._response_or_error(
        source, publisher, datetime(2026, 7, 17, tzinfo=UTC)
    )

    assert response.json() == [_event(1001)]
    assert response.headers["content-length"] == str(len(payload))
    assert response.headers["content-type"] == "application/json"
    assert all(
        name not in response.headers
        for name in (
            "content-encoding",
            "transfer-encoding",
            "connection",
            "keep-alive",
            "x-hop-only",
        )
    )


async def test_communico_stops_streaming_an_oversized_response_and_closes_it() -> None:
    """Do not read an unbounded payload merely to reject it after downloading."""
    read_chunks: list[int] = []
    closed: list[bool] = []

    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for index in range(
                communico_source._MAX_RESPONSE_BYTES // communico_source._RESPONSE_CHUNK_BYTES + 3
            ):
                read_chunks.append(index)
                yield b" " * communico_source._RESPONSE_CHUNK_BYTES

        async def aclose(self) -> None:
            closed.append(True)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedStream(), request=request)

    fetcher = CommunicoCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(CommunicoFetchError, match="response-size limit") as raised:
        await fetcher.fetch(_source())
    assert _source().source_key in str(raised.value)
    assert f"{len(read_chunks) * communico_source._RESPONSE_CHUNK_BYTES} decoded bytes" in str(
        raised.value
    )
    assert f"{communico_source._MAX_RESPONSE_BYTES} bytes" in str(raised.value)
    assert len(read_chunks) * communico_source._RESPONSE_CHUNK_BYTES <= (
        communico_source._MAX_RESPONSE_BYTES + communico_source._RESPONSE_CHUNK_BYTES
    )
    assert closed == [True]


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
async def test_communico_transport_outage_uses_bounded_retry(
    failure: type[httpx.TransportError],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise failure("Name or service not known", request=request)

    fetcher = CommunicoCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(SourceTransientError) as raised:
        await fetcher.fetch(_source())
    assert raised.value.retry_after_seconds == communico_source._TRANSIENT_RETRY_DELAY_S


@pytest.mark.parametrize("status", [500, 503, 504])
async def test_communico_server_outage_uses_bounded_retry(status: int) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request)

    fetcher = CommunicoCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(SourceTransientError) as raised:
        await fetcher.fetch(_source())
    assert raised.value.retry_after_seconds == communico_source._TRANSIENT_RETRY_DELAY_S


@pytest.mark.parametrize("status", [302, 403, 404])
async def test_communico_does_not_retry_or_follow_rejected_responses(status: int) -> None:
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            status, headers={"Location": "https://unreviewed.example/events"}, request=request
        )

    fetcher = CommunicoCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(CommunicoFetchError):
        await fetcher.fetch(_source())
    assert len(requested) == 1
