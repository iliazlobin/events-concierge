"""Domain logic unit tests: calendar id, lane router, conflict gate, dedup, lifecycle transitions."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from events_concierge.domain import dedup, ids
from events_concierge.domain.conflict import BusyBlock, evaluate_conflict
from events_concierge.domain.enums import (
    ConflictVerdict,
    GroupCondition,
    Lane,
    LifecycleState,
    Modality,
    PriceStatus,
    Source,
)
from events_concierge.domain.events import (
    CandidateEvent,
    CanonicalEvent,
    GeoPoint,
    aggregate_price_status,
    merge_price_status,
)
from events_concierge.domain.lifecycle import IllegalTransitionError, Lifecycle
from events_concierge.domain.policy import SourcePolicy
from events_concierge.domain.routing import LaneInput, build_lane_plan, route_single


def test_calendar_id_is_deterministic_and_legal() -> None:
    t, c = uuid4(), uuid4()
    a = ids.calendar_event_id(t, c)
    assert a == ids.calendar_event_id(t, c)  # deterministic
    assert re.fullmatch(r"[a-v0-9]{5,1024}", a)  # google-legal base32hex
    assert a != ids.calendar_event_id(uuid4(), c)  # tenant-sensitive


def test_request_dedup_key_normalizes_whitespace_and_case() -> None:
    t = uuid4()
    assert ids.request_dedup_key(t, "  Jazz   Night ", "2026-07-17T19") == ids.request_dedup_key(
        t, "jazz night", "2026-07-17T19"
    )
    assert ids.intake_request_id(t, "  Jazz   Night ", "2026-07-17T19") == ids.intake_request_id(
        t, "jazz night", "2026-07-17T19"
    )


def test_lane_router_table() -> None:
    meetup = SourcePolicy(source=Source.MEETUP, automation_allowed={Modality.API: True})
    assert (
        route_single(LaneInput(Source.MEETUP, Modality.API, meetup, GroupCondition.MEMBER))
        is Lane.AUTONOMOUS_SLA
    )
    assert (
        route_single(LaneInput(Source.MEETUP, Modality.API, meetup, GroupCondition.APPROVAL_GATED))
        is Lane.HANDOFF
    )
    assert route_single(LaneInput(Source.MEETUP, Modality.BROWSER, meetup)) is Lane.REFUSED
    luma = SourcePolicy(source=Source.LUMA, automation_allowed={Modality.BROWSER: True})
    assert route_single(LaneInput(Source.LUMA, Modality.BROWSER, luma)) is Lane.BROWSER_BEST_EFFORT
    assert route_single(LaneInput(Source.LUMA, Modality.API, luma)) is Lane.REFUSED
    partiful = SourcePolicy(source=Source.PARTIFUL)
    assert route_single(LaneInput(Source.PARTIFUL, Modality.BROWSER, partiful)) is Lane.DISABLED


def test_lane_plan_orders_and_falls_back_to_handoff() -> None:
    meetup = SourcePolicy(source=Source.MEETUP, automation_allowed={Modality.API: True})
    luma = SourcePolicy(source=Source.LUMA, automation_allowed={Modality.BROWSER: True})
    plan = build_lane_plan(
        [
            LaneInput(Source.LUMA, Modality.BROWSER, luma),
            LaneInput(Source.MEETUP, Modality.API, meetup, GroupCondition.MEMBER),
        ]
    )
    assert plan == (Lane.AUTONOMOUS_SLA, Lane.BROWSER_BEST_EFFORT)
    # only refused/disabled -> handoff-only plan
    assert build_lane_plan([LaneInput(Source.MEETUP, Modality.BROWSER, meetup)]) == (Lane.HANDOFF,)


def test_conflict_gate_blocks_hard_overlap_only() -> None:
    start = datetime(2026, 7, 17, 19, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    hard = BusyBlock(start=start + timedelta(minutes=30), end=start + timedelta(minutes=90))
    assert evaluate_conflict(start, end, [hard]) is ConflictVerdict.BLOCKED
    transparent = BusyBlock(start=start, end=end, transparent=True)
    assert evaluate_conflict(start, end, [transparent]) is ConflictVerdict.DEMOTE
    adjacent = BusyBlock(start=end + timedelta(minutes=10), end=end + timedelta(hours=1))
    assert evaluate_conflict(start, end, [adjacent]) is ConflictVerdict.DEMOTE
    assert evaluate_conflict(start, end, []) is ConflictVerdict.OK


def test_dedup_matches_same_event_across_wording() -> None:
    start = datetime(2026, 7, 17, 19, 0, tzinfo=UTC)
    canonical = CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Jazz Night at Blue Note",
        start_at=start,
        geo=GeoPoint(40.73, -74.0),
        city_norm="nyc",
    )
    same = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id="x",
        title="Jazz Night @ Blue Note",
        start_at=start + timedelta(minutes=20),
        registration_url="http://e",
        geo=GeoPoint(40.731, -74.001),
    )
    other = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id="y",
        title="Poetry Slam Downtown",
        start_at=start,
        registration_url="http://e2",
    )
    assert dedup.is_duplicate(same, canonical) is True
    assert dedup.is_duplicate(other, canonical) is False


def test_candidate_price_status_preserves_free_paid_and_unknown_truth() -> None:
    common = {
        "source": Source.PUBLIC_JSONLD,
        "title": "Price test",
        "start_at": datetime(2026, 7, 17, 19, 0, tzinfo=UTC),
        "registration_url": "https://example.test/event",
    }
    free = CandidateEvent(source_event_id="free", is_free=True, **common)
    paid = CandidateEvent(source_event_id="paid", is_free=False, **common)
    unknown = CandidateEvent(source_event_id="unknown", **common)

    assert (free.price_status, free.is_free) == (PriceStatus.FREE, True)
    assert (paid.price_status, paid.is_free) == (PriceStatus.PAID, False)
    assert (unknown.price_status, unknown.is_free) == (PriceStatus.UNKNOWN, None)


def test_price_status_aggregation_requires_complete_agreement() -> None:
    """Only every retained source agreeing on free may unlock the free-only path (FR-5.10)."""
    assert aggregate_price_status((PriceStatus.FREE,)) is PriceStatus.FREE
    assert aggregate_price_status((PriceStatus.PAID,)) is PriceStatus.PAID
    assert aggregate_price_status(()) is PriceStatus.UNKNOWN
    assert aggregate_price_status((PriceStatus.UNKNOWN, PriceStatus.FREE)) is PriceStatus.UNKNOWN
    assert aggregate_price_status((PriceStatus.FREE, PriceStatus.PAID)) is PriceStatus.UNKNOWN
    assert merge_price_status(PriceStatus.UNKNOWN, PriceStatus.FREE) is PriceStatus.UNKNOWN


def test_lifecycle_transition_guard() -> None:
    lc = Lifecycle(uuid4(), uuid4(), uuid4(), "wid", state=LifecycleState.FOUND)
    lc.transition(LifecycleState.REGISTERED)
    lc.transition(LifecycleState.SCHEDULED)
    assert lc.state is LifecycleState.SCHEDULED
    with pytest.raises(IllegalTransitionError):
        lc.transition(LifecycleState.FOUND)  # scheduled -> found is illegal
    assert LifecycleState.COMPLETED.is_terminal and not LifecycleState.FOUND.is_terminal
