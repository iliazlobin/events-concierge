"""Discovery lifecycle and distinct-choice behavior, including deliberately similar sessions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from events_concierge.application import feed as feed_module
from events_concierge.application.discovery_results import arrange, eligible, freshness, lifecycle
from events_concierge.application.feed import FeedService
from events_concierge.domain.enums import EventStatus, Source
from events_concierge.domain.events import CanonicalEvent, EventSourceLink, GeoPoint
from events_concierge.domain.request import (
    EventRequest,
    GeoConstraint,
    RequestConstraints,
    TimeWindow,
)

NOW = datetime(2026, 9, 8, 18, tzinfo=UTC)


def event(title="Story time", **kwargs):
    defaults = {
        "canonical_event_id": uuid4(),
        "title": title,
        "start_at": NOW + timedelta(days=1),
        "organizer_name": "Local Library",
        "venue_name": "Main branch",
        "city_norm": "village",
        "source_links": [
            EventSourceLink(
                Source.PUBLIC_JSONLD, str(uuid4()), "https://library.example/events", NOW
            )
        ],
    }
    defaults.update(kwargs)
    return CanonicalEvent(**defaults)


def test_default_discovery_excludes_past_cancelled_and_invalid_intervals():
    for e in [
        event(start_at=NOW),
        event(event_status=EventStatus.CANCELLED),
        event(end_at=NOW),
        event(start_at=NOW.replace(tzinfo=None)),
    ]:
        assert not eligible(e, RequestConstraints(), NOW)
    ongoing = event(start_at=NOW - timedelta(hours=1), end_at=NOW + timedelta(hours=1))
    assert eligible(ongoing, RequestConstraints(), NOW)
    assert lifecycle(ongoing, NOW) == "ongoing"


def test_explicit_history_is_half_open_and_does_not_admit_cancellations():
    constraints = RequestConstraints(time_window=TimeWindow(NOW - timedelta(days=2), NOW))
    assert eligible(event(start_at=NOW - timedelta(days=1)), constraints, NOW)
    assert not eligible(event(start_at=NOW), constraints, NOW)
    assert not eligible(
        event(start_at=NOW - timedelta(days=3), end_at=NOW - timedelta(days=2)), constraints, NOW
    )
    assert not eligible(
        event(start_at=NOW - timedelta(days=1), event_status=EventStatus.CANCELLED),
        constraints,
        NOW,
    )


def test_sessions_preserve_ids_and_different_venues_or_content_stay_separate():
    first = event()
    second = event(start_at=NOW + timedelta(days=2))
    other_venue = event(venue_name="East branch")
    other_content = event(description="Special author session")
    arranged = arrange(
        [(e, 1.0) for e in [first, second, other_venue, other_content]],
        RequestConstraints(),
        now=NOW,
    )
    assert len(arranged) == 3
    assert arranged[0][0].canonical_event_id == first.canonical_event_id
    assert arranged[0][2] == (second,)
    assert len({e.canonical_event_id for e in [first, second, other_venue, other_content]}) == 4


def test_missing_organizer_does_not_merge_generic_titles():
    assert (
        len(
            arrange(
                [(event(organizer_name=None), 1), (event(organizer_name=None), 1)],
                RequestConstraints(),
            )
        )
        == 2
    )


def test_broad_request_diversifies_and_library_intent_preserves_relevance_order():
    rows = [
        (event(f"Activity {i}", organizer_name="Library", topics=("family",)), 1 - i * 0.01)
        for i in range(4)
    ]
    different = (event("Community walk", organizer_name="Walking club", topics=("outdoors",)), 0.96)
    ranked = [*rows, different]
    broad = arrange(ranked, RequestConstraints(), now=NOW)
    narrow = arrange(ranked, RequestConstraints(categories=("library", "family")), now=NOW)
    assert broad.index(next(r for r in broad if r[0] is different[0])) < 4
    assert [r[0] for r in narrow] == [r[0] for r in ranked]


def test_freshness_is_an_observation_label_not_a_confirmation():
    assert freshness(event(), NOW) == "recent"
    assert freshness(event(source_links=[]), NOW) == "unknown"
    e = event(
        source_links=[
            EventSourceLink(
                Source.PUBLIC_JSONLD, "old", "https://library.example", NOW - timedelta(days=8)
            )
        ]
    )
    assert freshness(e, NOW) == "stale"


async def test_feed_filters_before_ranker_and_preserves_group_dates(monkeypatch):

    monkeypatch.setattr(feed_module, "datetime", SimpleNamespace(now=lambda tz: NOW))
    first, second = event(), event(start_at=NOW + timedelta(days=2))
    ended = event(start_at=NOW - timedelta(days=1))
    cancelled = event(event_status=EventStatus.CANCELLED)
    catalog = SimpleNamespace(retrieve=AsyncMock(return_value=[first, second, ended, cancelled]))
    ranker = SimpleNamespace(rerank=AsyncMock(return_value=[(first, 1.0), (second, 0.9)]))
    calendar = SimpleNamespace(free_busy=AsyncMock(return_value=[]))
    service = FeedService(catalog, ranker, calendar, {})
    request = EventRequest(uuid4(), uuid4(), "something this week", RequestConstraints())
    result = await service.build_feed(request)
    assert ranker.rerank.call_args.args[1] == [first, second]
    assert len(result.items) == 1
    assert result.items[0].additional_dates == (second,)
    assert result.signals["distinct_choices_at_20"] == 1
    assert result.next_cursor is None


async def test_historical_feed_never_offers_registration_or_reads_busy_calendar(monkeypatch):

    monkeypatch.setattr(feed_module, "datetime", SimpleNamespace(now=lambda tz: NOW))
    past = event(start_at=NOW - timedelta(days=1))
    catalog = SimpleNamespace(retrieve=AsyncMock(return_value=[past]))
    ranker = SimpleNamespace(rerank=AsyncMock(return_value=[(past, 1.0)]))
    calendar = SimpleNamespace(free_busy=AsyncMock(return_value=[]))
    service = FeedService(catalog, ranker, calendar, {})
    request = EventRequest(
        uuid4(),
        uuid4(),
        "yesterday",
        RequestConstraints(time_window=TimeWindow(NOW - timedelta(days=2), NOW)),
    )
    result = await service.build_feed(request)
    assert result.items[0].discovery_state == "past"
    assert not result.items[0].registerable
    assert result.items[0].lane_plan == ()
    calendar.free_busy.assert_not_called()


def test_quality_tie_breaks_consider_distance_without_overriding_strong_relevance():

    constraints = RequestConstraints(
        categories=("library",), geo=GeoConstraint(GeoPoint(37, -122), 100)
    )
    near = event("Nearby", geo=GeoPoint(37, -122))
    far = event("Farther", geo=GeoPoint(37.5, -122))
    assert arrange([(far, 1), (near, 1)], constraints, now=NOW)[0][0] is near
    assert arrange([(far, 1), (near, 0.5)], constraints, now=NOW)[0][0] is far
