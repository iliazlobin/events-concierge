"""Fixture-only durable watch-poll cadence and staleness contracts (ADR-008/NFR-17)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from events_concierge.adapters.mock.change_detection import (
    FixtureWatchPollDetector,
    InMemoryChangeDetectionRepository,
    InMemoryWatchPollRepository,
    MockOrganizerChangeFanout,
)
from events_concierge.application.change_detection import ChangeDetectionService
from events_concierge.application.watch_polling import WatchPollScheduler
from events_concierge.domain.enums import EventStatus, Source
from events_concierge.ports.change_detection import (
    DetectedEventChange,
    LifecycleWatch,
    WatchedEvent,
    WatchPollCadence,
    WatchPollHealthStatus,
    WatchPollPlan,
    assess_watch_poll_health,
    organizer_change_fingerprint,
)

_ACTIVE_SINCE = datetime(2026, 7, 1, tzinfo=UTC)


def _cadence(*, failures: int = 2) -> WatchPollCadence:
    """Use an explicit fixture cadence; no owner-pending production default is implied."""
    return WatchPollCadence(
        poll_interval=timedelta(hours=1),
        stale_after=timedelta(hours=2),
        failure_alarm_threshold=failures,
    )


def _plan(*, failures: int = 2) -> WatchPollPlan:
    """Provide the test's declared Luma cadence through the generic plan seam."""
    return WatchPollPlan({Source.LUMA: _cadence(failures=failures)})


def _change(canonical_event_id: UUID) -> DetectedEventChange:
    """Create one public cancellation observation that matches a Luma fixture watch."""
    return DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id,
            EventStatus.CANCELLED,
            None,
            None,
        ),
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.CANCELLED,
    )


async def _register(
    repository: InMemoryChangeDetectionRepository,
    canonical_event_id: UUID,
    tenant_id: UUID,
) -> None:
    """Register a fixture active lifecycle subscription into the distinct global watch registry."""
    assert await repository.register(
        LifecycleWatch(
            tenant_id=tenant_id,
            workflow_id=f"{tenant_id}:{canonical_event_id}",
            canonical_event_id=canonical_event_id,
            source=Source.LUMA,
            active_since=_ACTIVE_SINCE,
        )
    )


async def test_one_distinct_watch_polls_once_and_fans_out_to_all_subscribers() -> None:
    """Two tenants sharing one watched event consume one poll but receive two durable signals."""
    now = [datetime(2026, 7, 18, 12, 0, tzinfo=UTC)]
    watch_created_at = now[0]
    canonical_event_id = uuid4()
    changes = InMemoryChangeDetectionRepository(now=lambda: now[0])
    await _register(changes, canonical_event_id, uuid4())
    await _register(changes, canonical_event_id, uuid4())
    fanout = MockOrganizerChangeFanout()
    detector = FixtureWatchPollDetector(
        {(canonical_event_id, Source.LUMA): [_change(canonical_event_id)]}
    )
    service = ChangeDetectionService(changes, changes, fanout)
    scheduler = WatchPollScheduler(
        changes,
        InMemoryWatchPollRepository(now=lambda: now[0]),
        detector,
        service,
        _plan(),
        now=lambda: now[0],
    )

    result = await scheduler.run_once()

    assert (result.watched, result.planned, result.claimed, result.polled, result.succeeded) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert result.failed == 0
    assert result.detector.inserted == 1
    assert result.detector.queued_deliveries == 2
    assert len(detector.calls) == 1
    assert detector.calls[0].subscriber_count == 2
    assert detector.calls[0].created_at == watch_created_at
    assert fanout.deliveries == []
    delivered = await service.fanout_pending()
    assert delivered.delivered == 2
    assert len(fanout.deliveries) == 2
    assert result.health.watches[0].status is WatchPollHealthStatus.HEALTHY


async def test_successful_empty_poll_waits_until_its_explicit_next_cadence_slot() -> None:
    """An empty success is still a fresh source observation and prevents a hot-loop retry."""
    now = [datetime(2026, 7, 18, 12, 0, tzinfo=UTC)]
    canonical_event_id = uuid4()
    changes = InMemoryChangeDetectionRepository(now=lambda: now[0])
    await _register(changes, canonical_event_id, uuid4())
    detector = FixtureWatchPollDetector()
    scheduler = WatchPollScheduler(
        changes,
        InMemoryWatchPollRepository(now=lambda: now[0]),
        detector,
        ChangeDetectionService(changes, changes, MockOrganizerChangeFanout()),
        _plan(),
        now=lambda: now[0],
    )

    first = await scheduler.run_once()
    same_slot = await scheduler.run_once()
    now[0] += timedelta(hours=1)
    next_slot = await scheduler.run_once()

    assert (first.claimed, first.succeeded) == (1, 1)
    assert (same_slot.claimed, same_slot.succeeded) == (0, 0)
    assert (next_slot.claimed, next_slot.succeeded) == (1, 1)
    assert len(detector.calls) == 2


async def test_consecutive_fixture_failures_alarm_without_removing_the_watch() -> None:
    """A failed source poll retains the calendar/watch and becomes visible at plan threshold."""
    now = [datetime(2026, 7, 18, 12, 0, tzinfo=UTC)]
    watch_created_at = now[0]
    canonical_event_id = uuid4()
    changes = InMemoryChangeDetectionRepository(now=lambda: now[0])
    await _register(changes, canonical_event_id, uuid4())
    poll_state = InMemoryWatchPollRepository(now=lambda: now[0])
    scheduler = WatchPollScheduler(
        changes,
        poll_state,
        FixtureWatchPollDetector(failures={(canonical_event_id, Source.LUMA)}),
        ChangeDetectionService(changes, changes, MockOrganizerChangeFanout()),
        _plan(failures=2),
        now=lambda: now[0],
    )

    first = await scheduler.run_once()
    now[0] += timedelta(hours=1)
    second = await scheduler.run_once()
    states = await poll_state.list_watch_poll_states()

    assert first.failed == 1
    assert first.health.watches[0].status is WatchPollHealthStatus.PENDING_INITIAL_SUCCESS
    assert second.failed == 1
    assert second.health.watches[0].status is WatchPollHealthStatus.FAILURE_ALARM
    assert second.health.requires_attention is True
    assert states[0].consecutive_failures == 2
    assert states[0].last_error_type == "RuntimeError"
    assert await changes.list_watches() == [
        WatchedEvent(
            canonical_event_id, Source.LUMA, subscriber_count=1, created_at=watch_created_at
        )
    ]


async def test_watch_registered_before_a_worker_outage_is_stale_on_its_first_failed_claim() -> None:
    """Cursor initialization preserves registry age instead of masking an unpolled outage."""
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    watch = WatchedEvent(
        uuid4(),
        Source.LUMA,
        subscriber_count=1,
        created_at=now - timedelta(hours=3),
    )
    poll_state = InMemoryWatchPollRepository(now=lambda: now)

    lease = await poll_state.claim_watch_poll(watch, now=now, lease_seconds=60)

    assert lease is not None
    assert (
        await poll_state.release_watch_poll(
            lease,
            completed_at=now,
            next_due_at=now + timedelta(hours=1),
            error_type="RuntimeError",
        )
        is True
    )
    state = (await poll_state.list_watch_poll_states())[0]
    report = assess_watch_poll_health([watch], [state], _plan(), now=now)

    assert state.first_seen_at == watch.created_at
    assert report.watches[0].status is WatchPollHealthStatus.STALE
    assert report.requires_attention is True


async def test_poll_success_does_not_depend_on_a_later_fanout_failure() -> None:
    """The durable ledger is enough to ack a poll; the fanout worker owns its own retry."""
    now = [datetime(2026, 7, 18, 12, 0, tzinfo=UTC)]
    canonical_event_id = uuid4()
    changes = InMemoryChangeDetectionRepository(now=lambda: now[0])
    await _register(changes, canonical_event_id, uuid4())
    fanout = MockOrganizerChangeFanout(raise_after_effect_once=True)
    service = ChangeDetectionService(changes, changes, fanout)
    scheduler = WatchPollScheduler(
        changes,
        InMemoryWatchPollRepository(now=lambda: now[0]),
        FixtureWatchPollDetector(
            {(canonical_event_id, Source.LUMA): [_change(canonical_event_id)]}
        ),
        service,
        _plan(),
        now=lambda: now[0],
    )

    result = await scheduler.run_once()

    assert result.succeeded == 1
    assert (
        result.preflight_health.watches[0].status is WatchPollHealthStatus.PENDING_INITIAL_SUCCESS
    )
    assert result.health.watches[0].status is WatchPollHealthStatus.HEALTHY
    assert changes.change_count == 1
    assert fanout.deliveries == []
    fanout_result = await service.fanout_pending()
    assert fanout_result.deferred == 1
    assert len(fanout.deliveries) == 1


async def test_lost_cursor_ack_repolls_but_keeps_one_durable_change_and_delivery() -> None:
    """A crash after the ledger write safely repeats the read without duplicating its effect."""
    now = [datetime(2026, 7, 18, 12, 0, tzinfo=UTC)]
    canonical_event_id = uuid4()
    changes = InMemoryChangeDetectionRepository(now=lambda: now[0])
    await _register(changes, canonical_event_id, uuid4())
    fanout = MockOrganizerChangeFanout()
    service = ChangeDetectionService(changes, changes, fanout)
    scheduler = WatchPollScheduler(
        changes,
        InMemoryWatchPollRepository(lose_success_ack_once=True, now=lambda: now[0]),
        FixtureWatchPollDetector(
            {(canonical_event_id, Source.LUMA): [_change(canonical_event_id)]}
        ),
        service,
        _plan(),
        now=lambda: now[0],
    )

    lost_ack = await scheduler.run_once()
    now[0] += timedelta(seconds=61)
    recovered = await scheduler.run_once()

    assert lost_ack.succeeded == 0
    assert lost_ack.lost_leases == 1
    assert recovered.succeeded == 1
    assert changes.change_count == 1
    assert changes.pending_delivery_count == 1
    assert (await service.fanout_pending()).delivered == 1
    assert len(fanout.deliveries) == 1


async def test_lost_pre_poll_lease_skips_detector_and_recovers_without_a_ledger_effect() -> None:
    """A reclaimed watch-poll lease is fenced before the fixture detector boundary (NFR-8)."""
    now = [datetime(2026, 7, 18, 12, 0, tzinfo=UTC)]
    canonical_event_id = uuid4()
    changes = InMemoryChangeDetectionRepository(now=lambda: now[0])
    await _register(changes, canonical_event_id, uuid4())
    detector = FixtureWatchPollDetector(
        {(canonical_event_id, Source.LUMA): [_change(canonical_event_id)]}
    )
    poll_state = InMemoryWatchPollRepository(
        lose_live_lease_once=True,
        now=lambda: now[0],
    )
    scheduler = WatchPollScheduler(
        changes,
        poll_state,
        detector,
        ChangeDetectionService(changes, changes, MockOrganizerChangeFanout()),
        _plan(),
        now=lambda: now[0],
    )

    stale = await scheduler.run_once()

    assert (stale.claimed, stale.polled, stale.succeeded, stale.failed, stale.lost_leases) == (
        1,
        0,
        0,
        0,
        1,
    )
    assert detector.calls == []
    assert changes.change_count == 0
    assert changes.pending_delivery_count == 0

    recovered = await scheduler.run_once()

    assert detector.calls == [
        WatchedEvent(canonical_event_id, Source.LUMA, subscriber_count=1, created_at=now[0])
    ]
    assert changes.change_count == 1
    assert changes.pending_delivery_count == 1
    assert (recovered.claimed, recovered.polled, recovered.succeeded) == (1, 1, 1)


async def test_bounded_pass_prioritizes_unpolled_watches_before_repolling_its_first_uuid() -> None:
    """A batch cap cannot starve later registry keys behind an already-successful first key."""
    now = [datetime(2026, 7, 18, 12, 0, tzinfo=UTC)]
    canonical_event_ids = [uuid4(), uuid4(), uuid4()]
    changes = InMemoryChangeDetectionRepository(now=lambda: now[0])
    for canonical_event_id in canonical_event_ids:
        await _register(changes, canonical_event_id, uuid4())
    detector = FixtureWatchPollDetector()
    scheduler = WatchPollScheduler(
        changes,
        InMemoryWatchPollRepository(now=lambda: now[0]),
        detector,
        ChangeDetectionService(changes, changes, MockOrganizerChangeFanout()),
        _plan(),
        batch_size=1,
        now=lambda: now[0],
    )

    for _ in canonical_event_ids:
        result = await scheduler.run_once()
        assert result.succeeded == 1
        now[0] += timedelta(hours=1)

    assert {watch.canonical_event_id for watch in detector.calls} == set(canonical_event_ids)


async def test_preflight_health_retains_a_missed_deadline_after_a_recovery_poll() -> None:
    """An immediate recovery does not erase the pre-poll stale signal an operator must emit."""
    now = [datetime(2026, 7, 18, 12, 0, tzinfo=UTC)]
    canonical_event_id = uuid4()
    changes = InMemoryChangeDetectionRepository(now=lambda: now[0])
    await _register(changes, canonical_event_id, uuid4())
    scheduler = WatchPollScheduler(
        changes,
        InMemoryWatchPollRepository(now=lambda: now[0]),
        FixtureWatchPollDetector(),
        ChangeDetectionService(changes, changes, MockOrganizerChangeFanout()),
        _plan(),
        now=lambda: now[0],
    )

    initial = await scheduler.run_once()
    now[0] += timedelta(hours=3)
    recovered = await scheduler.run_once()

    assert initial.health.watches[0].status is WatchPollHealthStatus.HEALTHY
    assert recovered.preflight_health.watches[0].status is WatchPollHealthStatus.STALE
    assert recovered.health.watches[0].status is WatchPollHealthStatus.HEALTHY


def test_health_exposes_unplanned_and_aged_untracked_distinct_watches() -> None:
    """A new watch gets its declared grace period; an aged missing cursor is an alarm."""
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    luma_watch = WatchedEvent(uuid4(), Source.LUMA, subscriber_count=1, created_at=now)
    lost_luma_watch = WatchedEvent(
        uuid4(),
        Source.LUMA,
        subscriber_count=1,
        created_at=now - timedelta(hours=3),
    )
    meetup_watch = WatchedEvent(uuid4(), Source.MEETUP, subscriber_count=1, created_at=now)

    report = assess_watch_poll_health(
        [luma_watch, lost_luma_watch, meetup_watch],
        [],
        _plan(),
        now=now,
    )

    statuses = {(item.canonical_event_id, item.source): item.status for item in report.watches}
    assert (
        statuses[(luma_watch.canonical_event_id, Source.LUMA)]
        is WatchPollHealthStatus.PENDING_INITIAL_SUCCESS
    )
    assert (
        statuses[(lost_luma_watch.canonical_event_id, Source.LUMA)]
        is WatchPollHealthStatus.UNTRACKED
    )
    assert (
        statuses[(meetup_watch.canonical_event_id, Source.MEETUP)]
        is WatchPollHealthStatus.UNPLANNED
    )
    assert report.coverage_loss_count == 2
