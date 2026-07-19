"""Mountain View Public Library LibCal ICS adapter coverage (FR-3.1/FR-3.7/FR-10.3/10.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.libcal.source import LibCalIcsCatalogFetcher, LibCalIcsFetchError
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus
from events_concierge.domain.policy import SourceQuarantineSignal
from events_concierge.ports.sources import SourceAccessDeniedError, SourceRateLimitedError

_SEED_URL = "https://mountainview.libcal.com/ical_subscribe.php?src=p&cid=8800"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="mountain-view-library-events",
        display_name="Mountain View Public Library Events",
        publisher="Mountain View Public Library",
        seed_url=_SEED_URL,
        approved_origins=("https://mountainview.libcal.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.LIBCAL_ICS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
    )


def _calendar(*events: str) -> str:
    return "\r\n".join(
        (
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "X-WR-TIMEZONE:America/Los_Angeles",
            *events,
            "END:VCALENDAR",
            "",
        )
    )


def _event(
    identifier: int,
    *,
    summary: str = "Family Art Time",
    description: str = "Read\\nwith <em>neighbors</em>\\, together.",
    start: str = "20260718T170000Z",
    end: str = "20260718T180000Z",
    start_property: str = "DTSTART",
    end_property: str = "DTEND",
    location: str | None = "1st Floor Program Room",
    status: str | None = None,
    url: str | None = None,
    recurrence: str | None = None,
    folded_description: bool = False,
) -> str:
    properties = [
        "BEGIN:VEVENT",
        f"UID:LibCal-8800-{identifier}",
        f"URL:{url or f'https://mountainview.libcal.com/event/{identifier}'}",
        f"SUMMARY:{summary}",
    ]
    if folded_description:
        properties.extend(("DESCRIPTION:Read\\nwith <em>neighbors</em>\\, ", " together."))
    else:
        properties.append(f"DESCRIPTION:{description}")
    properties.extend((f"{start_property}:{start}", f"{end_property}:{end}"))
    if location is not None:
        properties.append(f"LOCATION:{location}")
    if status is not None:
        properties.append(f"STATUS:{status}")
    if recurrence is not None:
        properties.append(recurrence)
    properties.append("END:VEVENT")
    return "\r\n".join(properties)


async def test_libcal_maps_only_future_timed_reviewed_physical_events() -> None:
    """The closed feed builds handoffs from validated UID data and preserves source truth (FR-3.1/FR-3.7)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            200,
            text=_calendar(
                _event(1001, summary="Family\\, Art\\; Time", folded_description=True),
                _event(1002, location="Online"),
                _event(1003, location="Offsite"),
                _event(1004, location=None),
                _event(1005, status="CANCELLED"),
                _event(1011, summary="Board of Library Trustees Meeting Canceled."),
                _event(1006, start="20260717T110000Z"),
                _event(1007, start="20261015T070000Z", end="20261015T080000Z"),
                _event(
                    1008,
                    start="20260719",
                    end="20260720",
                    start_property="DTSTART;VALUE=DATE",
                    end_property="DTEND;VALUE=DATE",
                ),
                _event(
                    1009,
                    start="20260719T100000",
                    end="20260719T110000",
                    start_property="DTSTART;TZID=America/Los_Angeles",
                    end_property="DTEND;TZID=America/Los_Angeles",
                ),
                _event(1010, url="https://unapproved.example.test/event/1010"),
            ),
            request=request,
        )

    fetcher = LibCalIcsCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        transport=httpx.MockTransport(handler),
    )

    candidates = await fetcher.fetch(_source())

    assert [candidate.source_event_id for candidate in candidates] == [
        "libcal:mountain-view-library-events:1001:2026-07-18T17:00:00+00:00"
    ]
    candidate = candidates[0]
    assert candidate.title == "Family, Art; Time"
    assert candidate.registration_url == "https://mountainview.libcal.com/event/1001"
    assert candidate.venue_name == "1st Floor Program Room"
    assert candidate.city is None
    assert candidate.geo is None
    assert candidate.description == "Read with neighbors, together."
    assert candidate.end_at == datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
    assert candidate.price_status is PriceStatus.UNKNOWN
    assert requested == [httpx.URL(_SEED_URL)]


async def test_libcal_makes_one_closed_get_per_fetch_without_process_local_pacing() -> None:
    """Pacer admission is upstream; this bounded adapter makes one exact source GET (FR-10.3/FR-10.4)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, text=_calendar(), request=request)

    fetcher = LibCalIcsCatalogFetcher(
        user_agent="test",
        transport=httpx.MockTransport(handler),
    )

    assert await fetcher.fetch(_source()) == []
    assert requested == [httpx.URL(_SEED_URL)]


async def test_libcal_normalizes_a_valid_retry_after_without_a_second_get() -> None:
    """A usable 429 delay reaches the shared Pacer rather than becoming an opaque feed error (AC-73)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(429, headers={"Retry-After": "45"}, request=request)

    fetcher = LibCalIcsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(SourceRateLimitedError) as raised:
        await fetcher.fetch(_source())

    assert raised.value.retry_after_seconds == 45.0
    assert requested == [httpx.URL(_SEED_URL)]


@pytest.mark.parametrize("headers", ({}, {"Retry-After": "soon"}))
async def test_libcal_keeps_headerless_or_malformed_429_as_a_retryable_feed_error(
    headers: dict[str, str],
) -> None:
    """Only an explicit valid delay may influence shared-Pacer backoff state (AC-73)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(429, headers=headers, request=request)

    fetcher = LibCalIcsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(LibCalIcsFetchError, match="request failed"):
        await fetcher.fetch(_source())

    assert requested == [httpx.URL(_SEED_URL)]


async def test_libcal_normalizes_forbidden_without_a_second_get() -> None:
    """A closed HTTP 403 signal trips source quarantine instead of retrying the public feed (FR-10.3)."""
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(403, request=request)

    fetcher = LibCalIcsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(SourceAccessDeniedError) as raised:
        await fetcher.fetch(_source())

    assert raised.value.signal is SourceQuarantineSignal.FORBIDDEN
    assert requested == [httpx.URL(_SEED_URL)]


async def test_libcal_fails_closed_for_unsupported_recurrence_duplicate_uid_and_event_cap() -> None:
    """Source-shape changes cannot be silently treated as a complete single-instance calendar (NFR-8)."""
    payloads = iter(
        (
            _calendar(_event(2001, recurrence="RRULE:FREQ=DAILY")),
            _calendar(_event(2002) + "\r\n" + _event(2002)),
            _calendar(*(_event(3000 + index) for index in range(300))),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(payloads), request=request)

    fetcher = LibCalIcsCatalogFetcher(user_agent="test", transport=httpx.MockTransport(handler))

    with pytest.raises(LibCalIcsFetchError, match="unsupported recurrence"):
        await fetcher.fetch(_source())
    with pytest.raises(LibCalIcsFetchError, match="repeated an event uid"):
        await fetcher.fetch(_source())
    with pytest.raises(LibCalIcsFetchError, match="event-count limit"):
        await fetcher.fetch(_source())


async def test_libcal_rejects_invalid_payloads_redirects_and_unreviewed_sources_before_catalog_effects() -> (
    None
):
    """Malformed/unreviewed source inputs leave the durable refresh retryable and make no widened request (NFR-8)."""
    requested: list[str] = []

    async def invalid_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"\xff", request=request)

    fetcher = LibCalIcsCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(invalid_handler)
    )
    with pytest.raises(LibCalIcsFetchError, match="not UTF-8"):
        await fetcher.fetch(_source())
    assert requested == [_SEED_URL]

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/calendar"},
            request=request,
        )

    redirect_fetcher = LibCalIcsCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(redirect_handler)
    )
    with pytest.raises(LibCalIcsFetchError, match="approved endpoint"):
        await redirect_fetcher.fetch(_source())

    unreviewed = CatalogSource(
        source_key="unreviewed-libcal-library",
        display_name="Unreviewed LibCal Library",
        publisher="Events Concierge tests",
        seed_url=_SEED_URL,
        approved_origins=("https://mountainview.libcal.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.LIBCAL_ICS,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    unreviewed_fetcher = LibCalIcsCatalogFetcher(
        user_agent="test", transport=httpx.MockTransport(invalid_handler)
    )
    with pytest.raises(ValueError, match="reviewed public calendar"):
        await unreviewed_fetcher.fetch(unreviewed)
    assert requested == [_SEED_URL]
