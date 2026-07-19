"""Offline ADR-008 central-detector and fanout durability tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from events_concierge.adapters.mock.change_detection import (
    FixtureOrganizerChangeDetector,
    InMemoryChangeDetectionRepository,
    MockOrganizerChangeFanout,
)
from events_concierge.application.change_detection import ChangeDetectionService
from events_concierge.domain.enums import EventStatus, Source
from events_concierge.ports.change_detection import (
    DetectedEventChange,
    LifecycleWatch,
    WatchedEvent,
    fanout_retry_delay_seconds,
    organizer_change_fingerprint,
)

_DEFAULT_ACTIVE_SINCE = datetime(2026, 7, 1, tzinfo=UTC)


def _change(
    canonical_event_id: UUID,
    source: Source,
    *,
    start_at: datetime | None = None,
) -> DetectedEventChange:
    """Build a public reschedule fixture with ADR-008's source-agnostic fingerprint."""
    rescheduled_start = start_at or datetime(2026, 8, 1, 18, tzinfo=UTC)
    end_at = rescheduled_start + timedelta(hours=2)
    return DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id, EventStatus.RESCHEDULED, rescheduled_start, end_at
        ),
        canonical_event_id=canonical_event_id,
        source=source,
        event_status=EventStatus.RESCHEDULED,
        start_at=rescheduled_start,
        end_at=end_at,
        time_zone="America/Los_Angeles",
        title="Fixture rescheduled event",
        venue_name="Fixture venue",
    )


async def test_fixture_detector_deduplicates_one_public_change_and_recovers_one_lost_fanout_ack() -> (
    None
):
    """Two tenant workflows receive one event change each despite detector replay and lost ACK (ADR-008)."""
    canonical_event_id = uuid4()
    first_tenant, second_tenant = uuid4(), uuid4()
    first = LifecycleWatch(
        first_tenant,
        f"{first_tenant}:{canonical_event_id}",
        canonical_event_id,
        Source.LUMA,
        _DEFAULT_ACTIVE_SINCE,
    )
    second = LifecycleWatch(
        second_tenant,
        f"{second_tenant}:{canonical_event_id}",
        canonical_event_id,
        Source.LUMA,
        _DEFAULT_ACTIVE_SINCE,
    )
    clock = [datetime(2026, 7, 17, tzinfo=UTC)]
    watch_created_at = clock[0]
    repository = InMemoryChangeDetectionRepository(now=lambda: clock[0])
    fanout = MockOrganizerChangeFanout(raise_after_effect_once=True)
    service = ChangeDetectionService(repository, repository, fanout)

    assert await service.register_watch(first) is True
    assert await service.register_watch(second) is True
    assert await service.register_watch(first) is False
    assert await repository.list_watches() == [
        WatchedEvent(
            canonical_event_id, Source.LUMA, subscriber_count=2, created_at=watch_created_at
        )
    ]

    detector = FixtureOrganizerChangeDetector([_change(canonical_event_id, Source.LUMA)])
    first_run = await service.run_detector(detector)
    immediate_retry = await service.run_detector(detector)
    clock[0] += timedelta(seconds=1)
    retry_run = await service.run_detector(detector)

    assert first_run.observed == 1
    assert first_run.inserted == 1
    assert first_run.queued_deliveries == 2
    assert first_run.fanout.claimed == 2
    assert first_run.fanout.delivered == 1
    assert first_run.fanout.deferred == 1
    assert immediate_retry.observed == 1
    assert immediate_retry.inserted == 0
    assert immediate_retry.queued_deliveries == 0
    assert immediate_retry.fanout.claimed == 0
    assert immediate_retry.fanout.delivered == 0
    assert retry_run.observed == 1
    assert retry_run.inserted == 0
    assert retry_run.queued_deliveries == 0
    assert retry_run.fanout.claimed == 1
    assert retry_run.fanout.delivered == 1
    assert repository.change_count == 1
    assert repository.pending_delivery_count == 0
    assert repository.delivered_delivery_count == 2
    assert detector.calls == 3
    # The first fanout effect reached its receiver before an acknowledgement was lost. Its durable
    # retry deliberately repeats the same fingerprint; the long-lived workflow's queue dedups it.
    assert len(fanout.deliveries) == 3
    assert {delivery.change.fingerprint for delivery in fanout.deliveries} == {
        organizer_change_fingerprint(
            canonical_event_id,
            EventStatus.RESCHEDULED,
            datetime(2026, 8, 1, 18, tzinfo=UTC),
            datetime(2026, 8, 1, 20, tzinfo=UTC),
        )
    }
    assert {delivery.tenant_id for delivery in fanout.deliveries} == {first_tenant, second_tenant}

    assert await service.unregister_watch(first) is True
    assert await repository.list_watches() == [
        WatchedEvent(
            canonical_event_id, Source.LUMA, subscriber_count=1, created_at=watch_created_at
        )
    ]
    assert await service.unregister_watch(second) is True
    assert await repository.list_watches() == []


async def test_fanout_skips_a_stale_pre_signal_lease_until_a_fresh_claim() -> None:
    """A delivery observed stale at the final fence emits no Temporal signal (NFR-8, ADR-008)."""
    canonical_event_id = uuid4()
    tenant_id = uuid4()
    clock = [datetime(2026, 7, 17, tzinfo=UTC)]
    repository = InMemoryChangeDetectionRepository(
        lose_live_delivery_lease_once=True,
        now=lambda: clock[0],
    )
    fanout = MockOrganizerChangeFanout()
    service = ChangeDetectionService(repository, repository, fanout)
    watch = LifecycleWatch(
        tenant_id,
        f"{tenant_id}:{canonical_event_id}",
        canonical_event_id,
        Source.LUMA,
        _DEFAULT_ACTIVE_SINCE,
    )
    assert await service.register_watch(watch) is True

    first = await service.ingest(_change(canonical_event_id, Source.LUMA))

    assert first.fanout.claimed == 1
    assert first.fanout.delivered == 0
    assert first.fanout.deferred == 0
    assert first.fanout.repair_queued == 0
    assert first.fanout.lost_leases == 1
    assert fanout.deliveries == []

    retry = await service.fanout_pending()

    assert retry.claimed == 1
    assert retry.delivered == 1
    assert retry.deferred == 0
    assert retry.repair_queued == 0
    assert retry.lost_leases == 0
    assert len(fanout.deliveries) == 1
    assert fanout.deliveries[0].attempt_count == 2
    assert repository.pending_delivery_count == 0
    assert repository.delivered_delivery_count == 1


async def test_in_memory_delivery_terminal_operations_reject_an_expired_exact_lease() -> None:
    """The offline fanout queue preserves P22's live-lease terminal-write contract (ADR-008)."""
    canonical_event_id = uuid4()
    tenant_id = uuid4()
    clock = [datetime(2026, 7, 17, tzinfo=UTC)]
    repository = InMemoryChangeDetectionRepository(now=lambda: clock[0])
    watch = LifecycleWatch(
        tenant_id,
        f"{tenant_id}:{canonical_event_id}",
        canonical_event_id,
        Source.LUMA,
        _DEFAULT_ACTIVE_SINCE,
    )
    assert await repository.register(watch) is True
    assert (await repository.record(_change(canonical_event_id, Source.LUMA))).queued_deliveries == 1
    [stale] = await repository.claim_deliveries(limit=1, lease_seconds=1)
    clock[0] += timedelta(seconds=1)

    assert await repository.has_live_delivery_lease(stale) is False
    assert await repository.mark_delivered(stale) is False
    assert await repository.release_delivery(stale, error="expired exact lease") is False
    assert repository.pending_delivery_count == 1

    [fresh] = await repository.claim_deliveries(limit=1, lease_seconds=60)
    assert fresh.lease_token != stale.lease_token
    assert fresh.attempt_count == stale.attempt_count + 1
    assert await repository.mark_delivered(fresh) is True


async def test_canonical_change_fans_out_across_distinct_source_subscriptions() -> None:
    """One Luma observation reaches active Luma and Meetup workflows for its canonical event."""
    canonical_event_id = uuid4()
    luma_tenant, meetup_tenant = uuid4(), uuid4()
    repository = InMemoryChangeDetectionRepository()
    fanout = MockOrganizerChangeFanout()
    service = ChangeDetectionService(repository, repository, fanout)
    luma_watch = LifecycleWatch(
        luma_tenant,
        f"{luma_tenant}:{canonical_event_id}",
        canonical_event_id,
        Source.LUMA,
        _DEFAULT_ACTIVE_SINCE,
    )
    meetup_watch = LifecycleWatch(
        meetup_tenant,
        f"{meetup_tenant}:{canonical_event_id}",
        canonical_event_id,
        Source.MEETUP,
        _DEFAULT_ACTIVE_SINCE,
    )
    await service.register_watch(luma_watch)
    await service.register_watch(meetup_watch)

    result = await service.ingest(_change(canonical_event_id, Source.LUMA))
    replay = await service.ingest(_change(canonical_event_id, Source.MEETUP))

    assert result.inserted is True
    assert result.queued_deliveries == 2
    assert result.fanout.claimed == 2
    assert result.fanout.delivered == 2
    assert {delivery.tenant_id for delivery in fanout.deliveries} == {luma_tenant, meetup_tenant}
    assert {delivery.change.source for delivery in fanout.deliveries} == {Source.LUMA}
    assert replay.inserted is False
    assert replay.queued_deliveries == 0
    assert replay.fanout.claimed == 0


async def test_delayed_watch_projection_catches_up_a_change_recorded_after_lifecycle_activation() -> (
    None
):
    """Transition -> record -> delayed project still signals the newly active workflow (ADR-008)."""
    canonical_event_id = uuid4()
    tenant_id = uuid4()
    active_since = datetime(2026, 7, 17, tzinfo=UTC)
    clock = [active_since + timedelta(seconds=1)]
    repository = InMemoryChangeDetectionRepository(now=lambda: clock[0])
    fanout = MockOrganizerChangeFanout()
    service = ChangeDetectionService(repository, repository, fanout)

    # Model the short interval after the lifecycle became active but before its outbox projector
    # registered the source watch.  The Luma feeder has already observed the canonical change.
    recorded = await service.ingest(_change(canonical_event_id, Source.LUMA))
    watch = LifecycleWatch(
        tenant_id,
        f"{tenant_id}:{canonical_event_id}",
        canonical_event_id,
        Source.MEETUP,
        active_since,
    )
    projected = await service.register_watch(watch)
    catch_up = await service.fanout_pending()

    assert recorded.inserted is True
    assert recorded.queued_deliveries == 0
    assert recorded.fanout.claimed == 0
    assert projected is True
    assert catch_up.claimed == 1
    assert catch_up.delivered == 1
    assert len(fanout.deliveries) == 1
    assert fanout.deliveries[0].tenant_id == tenant_id
    assert fanout.deliveries[0].change.source is Source.LUMA


async def test_delayed_watch_projection_does_not_backfill_changes_before_lifecycle_activation() -> (
    None
):
    """Catch-up excludes historical public changes that predate this lifecycle activation (ADR-008)."""
    canonical_event_id = uuid4()
    tenant_id = uuid4()
    active_since = datetime(2026, 7, 17, tzinfo=UTC)
    clock = [active_since - timedelta(seconds=1)]
    repository = InMemoryChangeDetectionRepository(now=lambda: clock[0])
    fanout = MockOrganizerChangeFanout()
    service = ChangeDetectionService(repository, repository, fanout)

    historic = await service.ingest(
        _change(
            canonical_event_id,
            Source.LUMA,
            start_at=datetime(2026, 6, 1, 18, tzinfo=UTC),
        )
    )
    clock[0] = active_since + timedelta(seconds=1)
    projected = await service.register_watch(
        LifecycleWatch(
            tenant_id,
            f"{tenant_id}:{canonical_event_id}",
            canonical_event_id,
            Source.MEETUP,
            active_since,
        )
    )
    catch_up = await service.fanout_pending()

    assert historic.inserted is True
    assert historic.queued_deliveries == 0
    assert projected is True
    assert catch_up.claimed == 0
    assert fanout.deliveries == []


async def test_unregister_retires_a_leased_change_delivery_after_the_last_source_watch_leaves() -> (
    None
):
    """A terminal lifecycle cannot retain a lost-ACK change delivery for perpetual retry."""
    canonical_event_id = uuid4()
    tenant_id = uuid4()
    clock = [datetime(2026, 7, 17, tzinfo=UTC)]
    repository = InMemoryChangeDetectionRepository(now=lambda: clock[0])
    watch = LifecycleWatch(
        tenant_id,
        f"{tenant_id}:{canonical_event_id}",
        canonical_event_id,
        Source.LUMA,
        _DEFAULT_ACTIVE_SINCE,
    )
    assert await repository.register(watch) is True
    inserted = await repository.record(_change(canonical_event_id, Source.LUMA))
    claimed = await repository.claim_deliveries(limit=1, lease_seconds=60)

    assert inserted.queued_deliveries == 1
    assert len(claimed) == 1
    assert await repository.unregister(watch) is True
    assert repository.pending_delivery_count == 0
    assert await repository.mark_delivered(claimed[0]) is False
    assert await repository.claim_deliveries(limit=1, lease_seconds=60) == []


def test_fanout_retry_delay_is_bounded_exponential() -> None:
    """Fanout recovery makes progress without turning a persistent failure into a hot loop."""
    assert [fanout_retry_delay_seconds(attempt) for attempt in (1, 2, 3, 9, 10, 100)] == [
        1,
        2,
        4,
        256,
        300,
        300,
    ]


def test_fingerprint_collapses_the_same_canonical_status_and_dates_across_detector_modalities() -> (
    None
):
    """A poll and relay observation share one public event_change identity (ADR-008)."""
    canonical_event_id = uuid4()
    start_at = datetime(2026, 8, 1, 18, tzinfo=UTC)
    end_at = start_at + timedelta(hours=2)

    first = organizer_change_fingerprint(
        canonical_event_id, EventStatus.RESCHEDULED, start_at, end_at
    )
    second = organizer_change_fingerprint(
        canonical_event_id,
        EventStatus.RESCHEDULED,
        start_at.astimezone(UTC),
        end_at.astimezone(UTC),
    )

    assert first == second
