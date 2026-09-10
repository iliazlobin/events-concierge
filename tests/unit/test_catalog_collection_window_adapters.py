"""Mocked query families honor a frozen elapsed-time window across retries and DST."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from tests.unit.test_communico_source import _event as communico_event
from tests.unit.test_communico_source import _source as communico_source
from tests.unit.test_datasf_source import _source as datasf_source
from tests.unit.test_libcal_source import _calendar as libcal_calendar
from tests.unit.test_libcal_source import _event as libcal_event
from tests.unit.test_libcal_source import _source as libcal_source
from tests.unit.test_localist_source import _event as localist_event
from tests.unit.test_localist_source import _instance as localist_instance
from tests.unit.test_localist_source import _page as localist_page
from tests.unit.test_localist_source import _source as localist_source
from tests.unit.test_tribe_source import _event as tribe_event
from tests.unit.test_tribe_source import _page as tribe_page
from tests.unit.test_tribe_source import _source as tribe_source

from events_concierge.adapters.communico.source import CommunicoCatalogFetcher
from events_concierge.adapters.datasf.source import DataSfOur415CatalogFetcher
from events_concierge.adapters.libcal.source import LibCalIcsCatalogFetcher
from events_concierge.adapters.localist.source import LocalistCatalogFetcher
from events_concierge.adapters.tribe.source import TribeEventsCatalogFetcher
from events_concierge.domain.catalog_sources import CatalogCollectionWindow, CatalogSource
from events_concierge.ports.sources import SourceRateLimitedError

LOCAL = ZoneInfo("America/Los_Angeles")
SPRING_START = datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # Mar 7, 23:30 PST.
FALL_START = datetime(2026, 10, 31, 19, 0, tzinfo=UTC)  # Oct 31, 12:00 PDT.


def frozen_source(source: CatalogSource, start: datetime) -> CatalogSource:
    """The current 90-day setting must not widen a retried run's recorded one-day interval."""
    return replace(
        source,
        reviewed_at=start - timedelta(days=1),
        source_revision=8,
        collection_horizon_days=90,
        collection_window=CatalogCollectionWindow(
            source_revision=3,
            horizon_days=1,
            start_at=start,
            end_at=start + timedelta(hours=24),
            attempt_count=2,
        ),
    )


@pytest.mark.parametrize("family", ["localist", "tribe", "communico"])
@pytest.mark.parametrize(
    ("start", "query_end", "local_day_count"),
    [(FALL_START, "2026-11-02", 2), (SPRING_START, "2026-03-10", 3)],
    ids=["fall-back", "spring-forward"],
)
async def test_date_queries_cover_the_final_partial_day_and_filter_exact_bounds(
    family: str, start: datetime, query_end: str, local_day_count: int
) -> None:
    """Date-only provider predicates may overfetch, but cannot omit the final in-window second."""
    end = start + timedelta(hours=24)
    boundaries = [start - timedelta(seconds=1), start, end - timedelta(seconds=1), end]
    requested: list[httpx.Request] = []
    if family == "localist":
        source = frozen_source(localist_source(), start)
        body = localist_page(
            [
                localist_event(
                    index,
                    index,
                    instances=[
                        localist_instance(
                            index,
                            index,
                            start=instant.astimezone(LOCAL).isoformat(),
                            end=(instant + timedelta(minutes=30)).astimezone(LOCAL).isoformat(),
                        )
                    ],
                )
                for index, instant in enumerate(boundaries, 1001)
            ]
        )
        fetcher_class = LocalistCatalogFetcher
    elif family == "tribe":
        source = frozen_source(tribe_source(), start)
        body = tribe_page(
            [
                tribe_event(
                    index,
                    start_date=instant.astimezone(LOCAL).strftime("%Y-%m-%d %H:%M:%S"),
                    end_date=(instant + timedelta(minutes=30))
                    .astimezone(LOCAL)
                    .strftime("%Y-%m-%d %H:%M:%S"),
                    utc_start_date=instant.strftime("%Y-%m-%d %H:%M:%S"),
                    utc_end_date=(instant + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                )
                for index, instant in enumerate(boundaries, 1001)
            ],
            total=4,
            total_pages=1,
        )
        fetcher_class = TribeEventsCatalogFetcher
    else:
        source = frozen_source(communico_source(), start)
        body = [
            communico_event(
                index,
                start=instant.astimezone(LOCAL).strftime("%Y-%m-%d %H:%M:%S"),
                end=(instant + timedelta(minutes=30))
                .astimezone(LOCAL)
                .strftime("%Y-%m-%d %H:%M:%S"),
            )
            for index, instant in enumerate(boundaries, 1001)
        ]
        fetcher_class = CommunicoCatalogFetcher

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, json=body)

    fetcher = fetcher_class(
        user_agent="test",
        now=lambda: start + timedelta(days=20),
        transport=httpx.MockTransport(handler),
    )
    candidates = await fetcher.fetch(source)

    # A changed worker clock and current registry horizon cannot rebase the frozen interval.
    assert [candidate.start_at.astimezone(UTC) for candidate in candidates] == [
        start,
        end - timedelta(seconds=1),
    ]
    assert len(requested) == 1
    query = requested[0].url.params
    if family == "communico":
        assert json.loads(query["req"]) == {
            "private": False,
            "date": start.astimezone(LOCAL).date().isoformat(),
            "days": local_day_count,
        }
    else:
        assert (
            query["start" if family == "localist" else "start_date"]
            == start.astimezone(LOCAL).date().isoformat()
        )
        assert query["end" if family == "localist" else "end_date"] == query_end


async def test_datasf_query_and_recurrence_include_the_partial_final_local_day() -> None:
    """Ceiling the API date alone is insufficient: recurring materialization must include it too."""
    source = frozen_source(datasf_source(), SPRING_START)
    requested: list[httpx.Request] = []
    recurring = {
        ":id": "daily-session",
        "id": "daily-session",
        "event_name": "Midnight session",
        "event_start_date": "2026-03-01T00:00:00.000",
        "event_end_date": "2026-03-31T00:00:00.000",
        "days_of_week": "Mon-Sun",
        "start_time": "00:15:00",
        "more_info": "https://sfrecpark.org/register",
    }
    at_end = {
        **recurring,
        ":id": "exclusive-boundary",
        "id": "exclusive-boundary",
        "event_name": "At the excluded end",
        "event_start_date": "2026-03-09T00:00:00.000",
        "event_end_date": "2026-03-09T00:00:00.000",
        "days_of_week": "",
        "start_time": "00:30:00",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(
            200,
            json=[{"total": "2"}]
            if request.url.params["$select"] == "count(*) as total"
            else [recurring, at_end],
        )

    async def no_wait(_: float) -> None:
        pass

    fetcher = DataSfOur415CatalogFetcher(
        user_agent="test",
        now=lambda: SPRING_START + timedelta(days=20),
        transport=httpx.MockTransport(handler),
        sleep=no_wait,
    )
    candidates = await fetcher.fetch(source)

    assert [candidate.start_at.astimezone(UTC) for candidate in candidates] == [
        datetime(2026, 3, 8, 8, 15, tzinfo=UTC),
        datetime(2026, 3, 9, 7, 15, tzinfo=UTC),
    ]
    assert all(candidate.title == "Midnight session" for candidate in candidates)
    assert len(requested) == 2
    expected_predicate = (
        "event_start_date < '2026-03-10T00:00:00.000' AND "
        "(event_end_date >= '2026-03-07T00:00:00.000' OR "
        "(event_end_date IS NULL AND event_start_date >= '2026-03-07T00:00:00.000'))"
    )
    assert [request.url.params["$where"] for request in requested] == [expected_predicate] * 2


async def test_libcal_uses_frozen_candidate_bounds_without_modifying_the_reviewed_endpoint() -> (
    None
):
    source = frozen_source(libcal_source(), FALL_START)
    end = FALL_START + timedelta(hours=24)
    boundaries = [FALL_START - timedelta(seconds=1), FALL_START, end - timedelta(seconds=1), end]
    requested: list[httpx.URL] = []
    calendar = libcal_calendar(
        *[
            libcal_event(
                index,
                start=instant.strftime("%Y%m%dT%H%M%SZ"),
                end=(instant + timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ"),
            )
            for index, instant in enumerate(boundaries, 1001)
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, text=calendar)

    fetcher = LibCalIcsCatalogFetcher(
        user_agent="test",
        now=lambda: FALL_START + timedelta(days=20),
        transport=httpx.MockTransport(handler),
    )
    candidates = await fetcher.fetch(source)

    assert [candidate.start_at for candidate in candidates] == [
        FALL_START,
        end - timedelta(seconds=1),
    ]
    assert requested == [httpx.URL(source.seed_url)]


async def test_libcal_retry_after_http_date_uses_actual_clock_not_frozen_run_start() -> None:
    source = frozen_source(libcal_source(), FALL_START)
    provider_now = FALL_START + timedelta(days=20)
    requested: list[httpx.URL] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(
            429,
            headers={
                "Retry-After": format_datetime(provider_now + timedelta(seconds=75), usegmt=True)
            },
        )

    fetcher = LibCalIcsCatalogFetcher(
        user_agent="test", now=lambda: provider_now, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(SourceRateLimitedError) as raised:
        await fetcher.fetch(source)

    assert raised.value.retry_after_seconds == 75.0
    assert requested == [httpx.URL(source.seed_url)]
