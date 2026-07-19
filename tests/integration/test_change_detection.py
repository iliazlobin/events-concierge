"""PostgreSQL contract tests for ADR-008's fixture-only central detector control plane."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from events_concierge.adapters.postgres.calendar_repair import (
    PostgresClosedWorkflowCalendarRepairRepository,
)
from events_concierge.adapters.postgres.change_detection import PostgresChangeDetectionRepository
from events_concierge.adapters.postgres.tenant_repos import (
    PostgresLifecycleRepository,
    PostgresTenantRepository,
)
from events_concierge.adapters.postgres.watch_projection import (
    PostgresLifecycleWatchProjectionOutbox,
)
from events_concierge.application.watch_projection import (
    LifecycleWatchProjectionRelay,
    WatchProjectionStats,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import EventStatus, LifecycleState, Source
from events_concierge.infra.db import get_engine, system_session_scope
from events_concierge.ports.calendar_repair import CalendarRepairRecord
from events_concierge.ports.change_detection import (
    DetectedEventChange,
    LifecycleWatch,
    OrganizerChangeDelivery,
    organizer_change_fingerprint,
)
from events_concierge.ports.watch_projection import (
    WatchProjectionAction,
    WatchProjectionRecord,
)

pytestmark = pytest.mark.integration


class _RaiseBeforeFirstProjectionAck:
    """Lose one post-effect acknowledgement while retaining the real outbox capability.

    The second acknowledgement first proves that the expired lease's token can no longer mutate
    the row, then acknowledges the recovered lease.  The registry effect itself remains the real
    tenant-scoped PostgreSQL adapter throughout (ADR-008).
    """

    def __init__(self, delegate: PostgresLifecycleWatchProjectionOutbox) -> None:
        self._delegate = delegate
        self._lost_first_ack = False
        self.first_ack_record: WatchProjectionRecord | None = None
        self.claimed: list[WatchProjectionRecord] = []
        self.stale_marked: bool | None = None
        self.stale_rescheduled: bool | None = None

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[WatchProjectionRecord]:
        """Lease through the real guarded capability and retain each opaque token for assertions."""
        records = await self._delegate.claim_batch(limit, lease_seconds)
        self.claimed.extend(records)
        return records

    async def has_live_lease(self, record: WatchProjectionRecord) -> bool:
        """Keep the real pre-registry authority projection in the ACK-loss composition test."""
        return await self._delegate.has_live_lease(record)

    async def mark_delivered(self, record: WatchProjectionRecord) -> bool:
        """Raise before the first ACK, then reject that stale token before the fresh ACK."""
        if not self._lost_first_ack:
            self._lost_first_ack = True
            self.first_ack_record = record
            raise RuntimeError("simulated watch projection acknowledgement loss")

        first = self.first_ack_record
        assert first is not None
        if self.stale_marked is None:
            self.stale_marked = await self._delegate.mark_delivered(first)
            self.stale_rescheduled = await self._delegate.reschedule(
                first,
                error="stale watch projection lease after acknowledgement loss",
            )
        return await self._delegate.mark_delivered(record)

    async def reschedule(self, record: WatchProjectionRecord, *, error: str) -> bool:
        """Keep pre-effect failure behavior on the actual exact-lease capability."""
        return await self._delegate.reschedule(record, error=error)


async def test_global_watch_registry_fans_a_canonical_change_across_sources_and_leases_once_per_workflow(
    db: None,
) -> None:
    """One public change fans out across source watches, then converges on delayed retry.

    The registration check intentionally runs under each tenant's RLS context; a caller cannot
    subscribe another tenant's workflow merely by knowing its deterministic workflow id (ADR-008).
    """
    tag = uuid4().hex
    canonical_event_id = uuid4()
    first_tenant, second_tenant = uuid4(), uuid4()
    first_workflow = f"{first_tenant}:{canonical_event_id}"
    second_workflow = f"{second_tenant}:{canonical_event_id}"
    repository = PostgresChangeDetectionRepository()
    lifecycle_repository = PostgresLifecycleRepository()
    tenant_repository = PostgresTenantRepository()
    event_start = datetime(2099, 1, 1, 18, tzinfo=UTC)
    active_since = datetime(2026, 1, 1, tzinfo=UTC)

    await _insert_public_event_with_luma_and_meetup_links(canonical_event_id, tag, event_start)
    await tenant_repository.add(
        Tenant(
            first_tenant,
            f"oidc|detector-first-{tag}",
            f"{tag}-first@example.test",
            f"{tag}-first@u.test",
        )
    )
    await tenant_repository.add(
        Tenant(
            second_tenant,
            f"oidc|detector-second-{tag}",
            f"{tag}-second@example.test",
            f"{tag}-second@u.test",
        )
    )
    await _schedule(
        lifecycle_repository,
        first_tenant,
        canonical_event_id,
        first_workflow,
        Source.LUMA,
    )
    await _schedule(
        lifecycle_repository,
        second_tenant,
        canonical_event_id,
        second_workflow,
        Source.MEETUP,
    )

    first_watch = LifecycleWatch(
        first_tenant, first_workflow, canonical_event_id, Source.LUMA, active_since
    )
    second_watch = LifecycleWatch(
        second_tenant, second_workflow, canonical_event_id, Source.MEETUP, active_since
    )
    await _register_watch_pair(
        repository,
        first_watch,
        second_watch,
        LifecycleWatch(
            second_tenant,
            first_workflow,
            canonical_event_id,
            Source.LUMA,
            active_since,
        ),
        LifecycleWatch(
            first_tenant,
            first_workflow,
            canonical_event_id,
            Source.MEETUP,
            active_since,
        ),
    )

    rescheduled_start = event_start + timedelta(days=1)
    rescheduled_end = rescheduled_start + timedelta(hours=2)
    change = DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id,
            EventStatus.RESCHEDULED,
            rescheduled_start,
            rescheduled_end,
        ),
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.RESCHEDULED,
        start_at=rescheduled_start,
        end_at=rescheduled_end,
        time_zone="America/Los_Angeles",
        title="Rescheduled fixture",
        venue_name="Fixture venue",
    )
    first_record = await repository.record(change)
    replay_record = await repository.record(replace(change, source=Source.MEETUP))
    await _owner_execute(
        """
        UPDATE public.event_change_deliveries
        SET created_at = TIMESTAMPTZ '1900-01-01 00:00:00+00'
        WHERE fingerprint = :fingerprint
        """,
        {"fingerprint": change.fingerprint},
    )
    claimed = [
        delivery
        for delivery in await repository.claim_deliveries(limit=1000, lease_seconds=60)
        if delivery.change.fingerprint == change.fingerprint
    ]

    assert first_record.inserted is True
    assert first_record.queued_deliveries == 2
    assert replay_record.inserted is False
    assert replay_record.queued_deliveries == 0
    assert len(claimed) == 2
    assert {delivery.tenant_id for delivery in claimed} == {first_tenant, second_tenant}
    assert {delivery.workflow_id for delivery in claimed} == {first_workflow, second_workflow}
    assert all(delivery.change == change for delivery in claimed)
    assert all(delivery.attempt_count == 1 for delivery in claimed)

    assert await repository.mark_delivered(claimed[0]) is True
    assert (
        await repository.release_delivery(claimed[1], error="fixture Temporal unavailable") is True
    )
    assert [
        delivery
        for delivery in await repository.claim_deliveries(limit=1000, lease_seconds=60)
        if delivery.change.fingerprint == change.fingerprint
    ] == []
    await _assert_released_delivery_is_delayed_and_make_it_ready(claimed[1])
    retry = [
        delivery
        for delivery in await repository.claim_deliveries(limit=1000, lease_seconds=60)
        if delivery.change.fingerprint == change.fingerprint
    ]

    assert len(retry) == 1
    assert retry[0].change.fingerprint == change.fingerprint
    assert retry[0].attempt_count == 2
    assert await repository.mark_delivered(retry[0]) is True
    assert [
        delivery
        for delivery in await repository.claim_deliveries(limit=1000, lease_seconds=60)
        if delivery.change.fingerprint == change.fingerprint
    ] == []

    await _cancel_and_unregister_pair(repository, lifecycle_repository, first_watch, second_watch)
    await _assert_global_watch_registry_has_no_tenant_columns()
    await _assert_app_role_cannot_directly_mutate_watches()


async def test_transition_record_then_delayed_projection_backfills_only_the_active_workflow(
    db: None,
) -> None:
    """A detector record between activation and watch projection cannot be lost (ADR-008)."""
    tag = uuid4().hex
    canonical_event_id = uuid4()
    tenant_id = uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    repository = PostgresChangeDetectionRepository()
    lifecycle_repository = PostgresLifecycleRepository()
    tenant_repository = PostgresTenantRepository()
    event_start = datetime(2099, 2, 1, 18, tzinfo=UTC)
    active_since = datetime(2026, 1, 1, tzinfo=UTC)

    await _insert_public_event_with_luma_and_meetup_links(canonical_event_id, tag, event_start)
    await tenant_repository.add(
        Tenant(
            tenant_id, f"oidc|detector-gap-{tag}", f"{tag}-gap@example.test", f"{tag}-gap@u.test"
        )
    )
    await _schedule(
        lifecycle_repository,
        tenant_id,
        canonical_event_id,
        workflow_id,
        Source.MEETUP,
    )
    historic_start = event_start + timedelta(days=1)
    historic_end = historic_start + timedelta(hours=2)
    historic_change = DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id,
            EventStatus.RESCHEDULED,
            historic_start,
            historic_end,
        ),
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.RESCHEDULED,
        start_at=historic_start,
        end_at=historic_end,
        time_zone="America/Los_Angeles",
        title="Historical projection fixture",
        venue_name="Fixture venue",
    )
    rescheduled_start = event_start + timedelta(days=2)
    rescheduled_end = rescheduled_start + timedelta(hours=2)
    change = DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id,
            EventStatus.RESCHEDULED,
            rescheduled_start,
            rescheduled_end,
        ),
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.RESCHEDULED,
        start_at=rescheduled_start,
        end_at=rescheduled_end,
        time_zone="America/Los_Angeles",
        title="Delayed projection fixture",
        venue_name="Fixture venue",
    )

    historic_recorded = await repository.record(historic_change)
    await _owner_execute(
        """
        UPDATE public.event_changes
        SET detected_at = :detected_at
        WHERE fingerprint = :fingerprint
        """,
        {
            "detected_at": datetime(2025, 12, 31, tzinfo=UTC),
            "fingerprint": historic_change.fingerprint,
        },
    )
    recorded = await repository.record(change)
    projected = await repository.register(
        LifecycleWatch(
            tenant_id,
            workflow_id,
            canonical_event_id,
            Source.MEETUP,
            active_since,
        )
    )
    await _owner_execute(
        """
        UPDATE public.event_change_deliveries
        SET created_at = TIMESTAMPTZ '1900-01-01 00:00:00+00'
        WHERE fingerprint = :fingerprint
        """,
        {"fingerprint": change.fingerprint},
    )
    claimed = await repository.claim_deliveries(limit=10, lease_seconds=60)

    assert historic_recorded.inserted is True
    assert historic_recorded.queued_deliveries == 0
    assert recorded.inserted is True
    assert recorded.queued_deliveries == 0
    assert projected is True
    assert len(claimed) == 1
    assert claimed[0].tenant_id == tenant_id
    assert claimed[0].workflow_id == workflow_id
    assert claimed[0].change == change
    await _cancel_active_lifecycle(lifecycle_repository, tenant_id, canonical_event_id, workflow_id)
    assert (
        await repository.unregister(
            LifecycleWatch(
                tenant_id,
                workflow_id,
                canonical_event_id,
                Source.MEETUP,
                active_since,
            )
        )
        is True
    )
    assert await repository.mark_delivered(claimed[0]) is False
    assert await repository.claim_deliveries(limit=10, lease_seconds=60) == []


async def test_record_transaction_started_before_activation_uses_its_post_lock_timestamp(
    db: None,
) -> None:
    """The activation cutoff orders a pre-started detector transaction by its shared lock (ADR-008)."""
    tag = uuid4().hex
    canonical_event_id = uuid4()
    tenant_id = uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    repository = PostgresChangeDetectionRepository()
    lifecycle_repository = PostgresLifecycleRepository()
    tenant_repository = PostgresTenantRepository()
    event_start = datetime(2099, 3, 1, 18, tzinfo=UTC)
    rescheduled_start = event_start + timedelta(days=1)
    rescheduled_end = rescheduled_start + timedelta(hours=2)
    change = DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id,
            EventStatus.RESCHEDULED,
            rescheduled_start,
            rescheduled_end,
        ),
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.RESCHEDULED,
        start_at=rescheduled_start,
        end_at=rescheduled_end,
        time_zone="America/Los_Angeles",
        title="Post-lock timestamp fixture",
        venue_name="Fixture venue",
    )

    await _insert_public_event_with_luma_and_meetup_links(canonical_event_id, tag, event_start)
    await tenant_repository.add(
        Tenant(
            tenant_id,
            f"oidc|detector-clock-{tag}",
            f"{tag}-clock@example.test",
            f"{tag}-clock@u.test",
        )
    )
    lifecycle = await lifecycle_repository.get_or_create(tenant_id, canonical_event_id, workflow_id)

    async with get_engine().connect() as record_connection:
        transaction = await record_connection.begin()
        try:
            # Pin this detector transaction before activation.  ``now()`` would remain here even
            # when the shared canonical lock is acquired later, which is the race this test guards.
            await record_connection.execute(text("SELECT 1"))
            await asyncio.sleep(0.01)
            await lifecycle_repository.transition(
                lifecycle,
                LifecycleState.REGISTERED,
                f"{workflow_id}:registered",
                {
                    "workflow_id": workflow_id,
                    "event_summary": "Detector clock fixture",
                    "registration_source": Source.LUMA.value,
                },
            )
            await record_connection.execute(
                text(
                    """
                    SELECT *
                    FROM public.fn_record_event_change(
                        :fingerprint, :canonical_event_id, :source, :event_status, :start_at,
                        :end_at, :time_zone, :title, :venue_name
                    )
                    """
                ),
                {
                    "fingerprint": change.fingerprint,
                    "canonical_event_id": change.canonical_event_id,
                    "source": change.source.value,
                    "event_status": change.event_status.value,
                    "start_at": change.start_at,
                    "end_at": change.end_at,
                    "time_zone": change.time_zone,
                    "title": change.title,
                    "venue_name": change.venue_name,
                },
            )
            await transaction.commit()
        except BaseException:
            await transaction.rollback()
            raise

    active_since, detected_at = await _owner_record_clock_values(
        lifecycle.lifecycle_id, change.fingerprint
    )
    assert detected_at >= active_since

    watch = LifecycleWatch(
        tenant_id,
        workflow_id,
        canonical_event_id,
        Source.LUMA,
        active_since,
    )
    assert await repository.register(watch) is True
    await _owner_execute(
        """
        UPDATE public.event_change_deliveries
        SET created_at = TIMESTAMPTZ '1900-01-01 00:00:00+00'
        WHERE fingerprint = :fingerprint
        """,
        {"fingerprint": change.fingerprint},
    )
    claimed = await repository.claim_deliveries(limit=10, lease_seconds=60)

    assert len(claimed) == 1
    assert claimed[0].change == change
    assert await repository.mark_delivered(claimed[0]) is True
    await _cancel_active_lifecycle(lifecycle_repository, tenant_id, canonical_event_id, workflow_id)
    assert await repository.unregister(watch) is True


async def test_global_queue_capabilities_reject_malformed_or_unbounded_inputs_without_rows(
    db: None,
) -> None:
    """Guarded global capabilities fail closed without exposing a raw FK/existence oracle (ADR-008)."""
    async with system_session_scope() as session:
        rejected = (
            await session.execute(
                text(
                    """
                    SELECT *
                    FROM public.fn_record_event_change(
                        :fingerprint, :canonical_event_id, :source, :event_status,
                        NULL, NULL, NULL, NULL, NULL
                    )
                    """
                ),
                {
                    "fingerprint": "capability-invalid-fixture",
                    "canonical_event_id": uuid4(),
                    "source": Source.LUMA.value,
                    "event_status": EventStatus.CANCELLED.value,
                },
            )
        ).one()
        unbounded_claims = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM public.fn_claim_event_change_deliveries(NULL::integer, 60, 'invalid-limit')
                    """
                )
            )
        ).scalar_one()

    assert rejected.inserted is False
    assert rejected.queued_deliveries == 0
    assert int(unbounded_claims) == 0


async def test_calendar_repair_capability_preserves_exact_lease_recovery(
    db: None,
) -> None:
    """A repair can only be queued, retried, and acknowledged by its exact guarded lease (ADR-008)."""
    tag = uuid4().hex
    canonical_event_id, tenant_id = uuid4(), uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    event_start = datetime(2099, 4, 1, 18, tzinfo=UTC)
    repository = PostgresChangeDetectionRepository()
    repairs = PostgresClosedWorkflowCalendarRepairRepository()
    lifecycle_repository = PostgresLifecycleRepository()
    tenant_repository = PostgresTenantRepository()
    await _insert_public_event_with_luma_and_meetup_links(canonical_event_id, tag, event_start)
    await tenant_repository.add(
        Tenant(
            tenant_id,
            f"oidc|repair-{tag}",
            f"{tag}-repair@example.test",
            f"{tag}-repair@u.test",
        )
    )
    await _schedule(
        lifecycle_repository,
        tenant_id,
        canonical_event_id,
        workflow_id,
        Source.LUMA,
    )
    watch = LifecycleWatch(
        tenant_id,
        workflow_id,
        canonical_event_id,
        Source.LUMA,
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert await repository.register(watch) is True
    rescheduled_start = event_start + timedelta(days=1)
    rescheduled_end = rescheduled_start + timedelta(hours=2)
    change = DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id,
            EventStatus.RESCHEDULED,
            rescheduled_start,
            rescheduled_end,
        ),
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.RESCHEDULED,
        start_at=rescheduled_start,
        end_at=rescheduled_end,
        time_zone="America/Los_Angeles",
        title="Repair capability fixture",
    )
    assert (await repository.record(change)).queued_deliveries == 1
    await _owner_execute(
        """
        UPDATE public.event_change_deliveries
        SET created_at = TIMESTAMPTZ '1900-01-01 00:00:00+00'
        WHERE fingerprint = :fingerprint
        """,
        {"fingerprint": change.fingerprint},
    )
    deliveries = [
        delivery
        for delivery in await repository.claim_deliveries(limit=1000, lease_seconds=60)
        if delivery.change.fingerprint == change.fingerprint
    ]
    assert len(deliveries) == 1
    assert await repairs.enqueue(deliveries[0], error="closed workflow fixture") is True
    assert await repairs.enqueue(deliveries[0], error="stale lease fixture") is False
    await _owner_execute(
        """
        UPDATE public.event_change_calendar_repairs
        SET next_attempt_at = TIMESTAMPTZ '1900-01-01 00:00:00+00'
        WHERE fingerprint = :fingerprint
          AND tenant_id = :tenant_id
          AND workflow_id = :workflow_id
        """,
        {
            "fingerprint": change.fingerprint,
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
        },
    )
    claimed_repairs = [
        record
        for record in await repairs.claim_batch(limit=1000, lease_seconds=60)
        if record.change.fingerprint == change.fingerprint
    ]
    assert len(claimed_repairs) == 1
    first_repair = claimed_repairs[0]
    assert await repairs.organizer_change_applied(first_repair) is False
    assert (
        await repairs.mark_repaired(replace(first_repair, lease_token="stale-repair-lease"))
        is False
    )
    assert await repairs.reschedule(first_repair, error="retry repair fixture") is True
    await _owner_execute(
        """
        UPDATE public.event_change_calendar_repairs
        SET next_attempt_at = TIMESTAMPTZ '1900-01-01 00:00:00+00'
        WHERE repair_id = :repair_id
        """,
        {"repair_id": first_repair.repair_id},
    )
    retried_repairs = [
        record
        for record in await repairs.claim_batch(limit=1000, lease_seconds=60)
        if record.change.fingerprint == change.fingerprint
    ]
    assert len(retried_repairs) == 1
    assert retried_repairs[0].attempt_count == 2
    assert await repairs.mark_repaired(retried_repairs[0]) is True


async def test_calendar_repair_pre_reconcile_lease_check_reads_database_clock_after_row_wait(
    db: None,
) -> None:
    """A stale repair lease cannot remain authorized after a blocked final effect fence (NFR-8)."""
    fixture = await _prepare_terminal_lease_fixture()
    repairs = fixture.repairs
    delivery = fixture.enqueue_delivery
    assert await repairs.enqueue(delivery, error="pre-reconcile lease fixture") is True
    await _owner_execute(
        """
        UPDATE public.event_change_calendar_repairs
        SET created_at = TIMESTAMPTZ '0001-01-01 00:00:00+00',
            next_attempt_at = TIMESTAMPTZ '0001-01-01 00:00:00+00'
        WHERE fingerprint = :fingerprint
          AND tenant_id = :tenant_id
          AND workflow_id = :workflow_id
          AND repaired_at IS NULL
        """,
        {
            "fingerprint": delivery.change.fingerprint,
            "tenant_id": delivery.tenant_id,
            "workflow_id": delivery.workflow_id,
        },
    )
    claimed = [
        record
        for record in await repairs.claim_batch(limit=1000, lease_seconds=2)
        if (
            record.change.fingerprint == delivery.change.fingerprint
            and record.tenant_id == delivery.tenant_id
            and record.workflow_id == delivery.workflow_id
        )
    ]
    assert len(claimed) == 1
    stale = claimed[0]
    before = await _owner_repair_state(stale)

    async with _owner_calendar_repair_lock(stale.repair_id) as owner:
        stale_check = asyncio.create_task(repairs.has_live_repair_lease(stale))
        await asyncio.sleep(0.1)
        assert not stale_check.done()
        await _wait_for_calendar_repair_lease_expiry(owner, stale.repair_id)

    assert await asyncio.wait_for(stale_check, timeout=5) is False
    after = await _owner_repair_state(stale)
    assert after.repaired_at is None
    assert after.lease_token == stale.lease_token
    assert after.attempt_count == before.attempt_count
    assert after.next_attempt_at == before.next_attempt_at

    reclaimed = [
        record
        for record in await repairs.claim_batch(limit=1000, lease_seconds=60)
        if record.repair_id == stale.repair_id
    ]
    assert len(reclaimed) == 1
    fresh = reclaimed[0]
    assert fresh.lease_token != stale.lease_token
    assert fresh.attempt_count == stale.attempt_count + 1
    assert await repairs.has_live_repair_lease(stale) is False
    assert await repairs.has_live_repair_lease(fresh) is True

    async with system_session_scope() as session:
        malformed = (
            await session.execute(
                text("SELECT public.fn_has_live_calendar_repair_lease(NULL::bigint, '')")
            )
        ).scalar_one()
        oversized = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_has_live_calendar_repair_lease(
                        :repair_id, :lease_token
                    )
                    """
                ),
                {"repair_id": fresh.repair_id, "lease_token": "x" * 129},
            )
        ).scalar_one()
    assert malformed is False
    assert oversized is False


async def test_change_delivery_pre_signal_lease_check_reads_database_clock_after_row_wait(
    db: None,
) -> None:
    """A stale fanout lease cannot remain authorized after a blocked final signal fence (NFR-8)."""
    fixture = await _prepare_terminal_lease_fixture()
    repository = fixture.repository
    stale = fixture.mark_delivery
    await _owner_execute(
        """
        UPDATE public.event_change_deliveries
        SET lease_expires_at = pg_catalog.clock_timestamp() + INTERVAL '2 seconds'
        WHERE fingerprint = :fingerprint
          AND tenant_id = :tenant_id
          AND workflow_id = :workflow_id
          AND lease_token = :lease_token
          AND delivered_at IS NULL
        """,
        {
            "fingerprint": stale.change.fingerprint,
            "tenant_id": stale.tenant_id,
            "workflow_id": stale.workflow_id,
            "lease_token": stale.lease_token,
        },
    )
    before = await _owner_delivery_state(stale)

    async with _owner_event_change_delivery_lock(stale) as owner:
        stale_check = asyncio.create_task(repository.has_live_delivery_lease(stale))
        await asyncio.sleep(0.1)
        assert not stale_check.done()
        await _wait_for_event_change_delivery_lease_expiry(owner, stale)

    assert await asyncio.wait_for(stale_check, timeout=5) is False
    after = await _owner_delivery_state(stale)
    assert after.delivered_at is None
    assert after.lease_token == stale.lease_token
    assert after.attempt_count == before.attempt_count
    assert after.next_attempt_at == before.next_attempt_at

    reclaimed = await _claim_fixture_deliveries(repository, {stale.change.fingerprint})
    assert set(reclaimed) == {stale.change.fingerprint}
    fresh = reclaimed[stale.change.fingerprint]
    assert fresh.lease_token != stale.lease_token
    assert fresh.attempt_count == stale.attempt_count + 1
    assert await repository.has_live_delivery_lease(stale) is False
    assert await repository.has_live_delivery_lease(fresh) is True

    async with system_session_scope() as session:
        malformed = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_has_live_event_change_delivery_lease(
                        NULL::text, NULL::uuid, '', ''
                    )
                    """
                )
            )
        ).scalar_one()
        oversized = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_has_live_event_change_delivery_lease(
                        :fingerprint, :tenant_id, :workflow_id, :lease_token
                    )
                    """
                ),
                {
                    "fingerprint": fresh.change.fingerprint,
                    "tenant_id": fresh.tenant_id,
                    "workflow_id": fresh.workflow_id,
                    "lease_token": "x" * 129,
                },
            )
        ).scalar_one()
    assert malformed is False
    assert oversized is False


async def test_change_delivery_and_calendar_repair_terminal_writes_require_live_leases(
    db: None,
) -> None:
    """Expired ADR-008 leases cannot terminally mutate before a fresh claimant recovers them."""
    fixture = await _prepare_terminal_lease_fixture()
    recovered = await _assert_expired_delivery_terminal_writes_are_noops(fixture)
    await _assert_expired_repair_terminal_writes_are_noops(fixture.repairs, recovered)


async def _prepare_terminal_lease_fixture() -> _TerminalLeaseFixture:
    """Create four isolated global delivery rows with owner-set bounded claim priority."""
    tag = uuid4().hex
    canonical_event_id, tenant_id = uuid4(), uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    event_start = datetime(2099, 7, 1, 18, tzinfo=UTC)
    repository = PostgresChangeDetectionRepository()
    repairs = PostgresClosedWorkflowCalendarRepairRepository()
    lifecycle_repository = PostgresLifecycleRepository()
    tenant_repository = PostgresTenantRepository()
    await _insert_public_event_with_luma_and_meetup_links(canonical_event_id, tag, event_start)
    await tenant_repository.add(
        Tenant(
            tenant_id,
            f"oidc|terminal-lease-{tag}",
            f"{tag}-terminal-lease@example.test",
            f"{tag}-terminal-lease@u.test",
        )
    )
    await _schedule(
        lifecycle_repository,
        tenant_id,
        canonical_event_id,
        workflow_id,
        Source.LUMA,
    )
    assert (
        await repository.register(
            LifecycleWatch(
                tenant_id,
                workflow_id,
                canonical_event_id,
                Source.LUMA,
                datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        is True
    )

    changes: list[DetectedEventChange] = []
    for day in range(1, 5):
        start_at = event_start + timedelta(days=day)
        end_at = start_at + timedelta(hours=2)
        change = DetectedEventChange(
            fingerprint=organizer_change_fingerprint(
                canonical_event_id,
                EventStatus.RESCHEDULED,
                start_at,
                end_at,
            ),
            canonical_event_id=canonical_event_id,
            source=Source.LUMA,
            event_status=EventStatus.RESCHEDULED,
            start_at=start_at,
            end_at=end_at,
            time_zone="America/Los_Angeles",
            title=f"Terminal lease fixture {day}",
        )
        assert (await repository.record(change)).queued_deliveries == 1
        await _owner_execute(
            """
            UPDATE public.event_change_deliveries
            SET created_at = TIMESTAMPTZ '0001-01-01 00:00:00+00',
                next_attempt_at = TIMESTAMPTZ '0001-01-01 00:00:00+00'
            WHERE fingerprint = :fingerprint
              AND tenant_id = :tenant_id
              AND workflow_id = :workflow_id
            """,
            {
                "fingerprint": change.fingerprint,
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
            },
        )
        changes.append(change)

    initial = await _claim_fixture_deliveries(
        repository,
        {change.fingerprint for change in changes},
    )
    assert set(initial) == {change.fingerprint for change in changes}
    return _TerminalLeaseFixture(
        repository=repository,
        repairs=repairs,
        mark_delivery=initial[changes[0].fingerprint],
        release_delivery=initial[changes[1].fingerprint],
        enqueue_delivery=initial[changes[2].fingerprint],
        repair_reschedule_delivery=initial[changes[3].fingerprint],
    )


async def _assert_expired_delivery_terminal_writes_are_noops(
    fixture: _TerminalLeaseFixture,
) -> _RecoveredDeliveryLeases:
    """Prove each delivery terminal capability rejects expiry before its next claim (ADR-008)."""
    mark_delivery = fixture.mark_delivery
    release_delivery = fixture.release_delivery
    enqueue_delivery = fixture.enqueue_delivery
    repair_reschedule_delivery = fixture.repair_reschedule_delivery

    await _owner_expire_delivery_lease(mark_delivery)
    expired_mark_delivery = await _owner_delivery_state(mark_delivery)
    assert expired_mark_delivery.lease_expired is True
    assert await _call_mark_event_change_delivery(mark_delivery) is False
    assert await _owner_delivery_state(mark_delivery) == expired_mark_delivery

    await _owner_expire_delivery_lease(release_delivery)
    expired_release_delivery = await _owner_delivery_state(release_delivery)
    assert expired_release_delivery.lease_expired is True
    assert (
        await _call_release_event_change_delivery(
            release_delivery,
            error="expired delivery release before reclaim",
        )
        is False
    )
    assert await _owner_delivery_state(release_delivery) == expired_release_delivery

    await _owner_expire_delivery_lease(enqueue_delivery)
    expired_enqueue_delivery = await _owner_delivery_state(enqueue_delivery)
    assert expired_enqueue_delivery.lease_expired is True
    assert await _owner_repair_count(enqueue_delivery) == 0
    assert (
        await _call_enqueue_closed_workflow_calendar_repair(
            enqueue_delivery,
            error="expired delivery enqueue before reclaim",
        )
        is False
    )
    assert await _owner_delivery_state(enqueue_delivery) == expired_enqueue_delivery
    assert await _owner_repair_count(enqueue_delivery) == 0

    reclaimed_deliveries = await _claim_fixture_deliveries(
        fixture.repository,
        {
            mark_delivery.change.fingerprint,
            release_delivery.change.fingerprint,
            enqueue_delivery.change.fingerprint,
        },
    )
    assert set(reclaimed_deliveries) == {
        mark_delivery.change.fingerprint,
        release_delivery.change.fingerprint,
        enqueue_delivery.change.fingerprint,
    }
    fresh_mark_delivery = reclaimed_deliveries[mark_delivery.change.fingerprint]
    fresh_release_delivery = reclaimed_deliveries[release_delivery.change.fingerprint]
    fresh_enqueue_delivery = reclaimed_deliveries[enqueue_delivery.change.fingerprint]
    assert fresh_mark_delivery.lease_token != mark_delivery.lease_token
    assert fresh_release_delivery.lease_token != release_delivery.lease_token
    assert fresh_enqueue_delivery.lease_token != enqueue_delivery.lease_token
    assert await _call_mark_event_change_delivery(fresh_mark_delivery) is True
    assert (
        await _call_release_event_change_delivery(
            fresh_release_delivery,
            error="fresh delivery release after reclaim",
        )
        is True
    )
    assert (
        await _call_enqueue_closed_workflow_calendar_repair(
            fresh_enqueue_delivery,
            error="fresh delivery enqueue after reclaim",
        )
        is True
    )
    assert (
        await _call_enqueue_closed_workflow_calendar_repair(
            repair_reschedule_delivery,
            error="fresh delivery enqueue for reschedule repair",
        )
        is True
    )

    await _owner_execute(
        """
        UPDATE public.event_change_deliveries
        SET created_at = TIMESTAMPTZ '0001-01-01 00:00:00+00',
            next_attempt_at = TIMESTAMPTZ '0001-01-01 00:00:00+00'
        WHERE fingerprint = :fingerprint
          AND tenant_id = :tenant_id
          AND workflow_id = :workflow_id
          AND delivered_at IS NULL
        """,
        {
            "fingerprint": fresh_release_delivery.change.fingerprint,
            "tenant_id": fresh_release_delivery.tenant_id,
            "workflow_id": fresh_release_delivery.workflow_id,
        },
    )
    released_delivery_recoveries = await _claim_fixture_deliveries(
        fixture.repository,
        {fresh_release_delivery.change.fingerprint},
    )
    released_delivery_recovery = released_delivery_recoveries[
        fresh_release_delivery.change.fingerprint
    ]
    assert await _call_mark_event_change_delivery(released_delivery_recovery) is True

    return _RecoveredDeliveryLeases(
        fresh_enqueue_delivery=fresh_enqueue_delivery,
        repair_reschedule_delivery=repair_reschedule_delivery,
    )


async def _assert_expired_repair_terminal_writes_are_noops(
    repairs: PostgresClosedWorkflowCalendarRepairRepository,
    recovered: _RecoveredDeliveryLeases,
) -> None:
    """Prove each repair terminal capability rejects expiry before its next claim (ADR-008)."""
    fresh_enqueue_delivery = recovered.fresh_enqueue_delivery
    repair_reschedule_delivery = recovered.repair_reschedule_delivery

    for delivery in (fresh_enqueue_delivery, repair_reschedule_delivery):
        await _owner_execute(
            """
            UPDATE public.event_change_calendar_repairs
            SET created_at = TIMESTAMPTZ '0001-01-01 00:00:00+00',
                next_attempt_at = TIMESTAMPTZ '0001-01-01 00:00:00+00'
            WHERE fingerprint = :fingerprint
              AND tenant_id = :tenant_id
              AND workflow_id = :workflow_id
              AND repaired_at IS NULL
            """,
            {
                "fingerprint": delivery.change.fingerprint,
                "tenant_id": delivery.tenant_id,
                "workflow_id": delivery.workflow_id,
            },
        )
    initial_repairs = await _claim_fixture_repairs(
        repairs,
        {
            fresh_enqueue_delivery.change.fingerprint,
            repair_reschedule_delivery.change.fingerprint,
        },
    )
    assert set(initial_repairs) == {
        fresh_enqueue_delivery.change.fingerprint,
        repair_reschedule_delivery.change.fingerprint,
    }
    mark_repair = initial_repairs[fresh_enqueue_delivery.change.fingerprint]
    reschedule_repair = initial_repairs[repair_reschedule_delivery.change.fingerprint]

    await _owner_expire_repair_lease(mark_repair)
    expired_mark_repair = await _owner_repair_state(mark_repair)
    assert expired_mark_repair.lease_expired is True
    assert await _call_mark_calendar_repair(mark_repair) is False
    assert await _owner_repair_state(mark_repair) == expired_mark_repair

    await _owner_expire_repair_lease(reschedule_repair)
    expired_reschedule_repair = await _owner_repair_state(reschedule_repair)
    assert expired_reschedule_repair.lease_expired is True
    assert (
        await _call_reschedule_calendar_repair(
            reschedule_repair,
            error="expired repair reschedule before reclaim",
        )
        is False
    )
    assert await _owner_repair_state(reschedule_repair) == expired_reschedule_repair

    reclaimed_repairs = await _claim_fixture_repairs(
        repairs,
        {
            mark_repair.change.fingerprint,
            reschedule_repair.change.fingerprint,
        },
    )
    assert set(reclaimed_repairs) == {
        mark_repair.change.fingerprint,
        reschedule_repair.change.fingerprint,
    }
    fresh_mark_repair = reclaimed_repairs[mark_repair.change.fingerprint]
    fresh_reschedule_repair = reclaimed_repairs[reschedule_repair.change.fingerprint]
    assert fresh_mark_repair.lease_token != mark_repair.lease_token
    assert fresh_reschedule_repair.lease_token != reschedule_repair.lease_token
    assert await _call_mark_calendar_repair(fresh_mark_repair) is True
    assert (
        await _call_reschedule_calendar_repair(
            fresh_reschedule_repair,
            error="fresh repair reschedule after reclaim",
        )
        is True
    )
    await _owner_execute(
        """
        UPDATE public.event_change_calendar_repairs
        SET created_at = TIMESTAMPTZ '0001-01-01 00:00:00+00',
            next_attempt_at = TIMESTAMPTZ '0001-01-01 00:00:00+00'
        WHERE repair_id = :repair_id
          AND repaired_at IS NULL
        """,
        {"repair_id": fresh_reschedule_repair.repair_id},
    )
    repaired_after_reschedules = await _claim_fixture_repairs(
        repairs,
        {fresh_reschedule_repair.change.fingerprint},
    )
    repaired_after_reschedule = repaired_after_reschedules[
        fresh_reschedule_repair.change.fingerprint
    ]
    assert await _call_mark_calendar_repair(repaired_after_reschedule) is True


async def test_watch_projection_capability_preserves_exact_lease_recovery(
    db: None,
) -> None:
    """Projection instructions cannot be suppressed or retried without their guarded lease (ADR-008)."""
    tag = uuid4().hex
    canonical_event_id, tenant_id = uuid4(), uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    event_start = datetime(2099, 5, 1, 18, tzinfo=UTC)
    lifecycle_repository = PostgresLifecycleRepository()
    tenant_repository = PostgresTenantRepository()
    projections = PostgresLifecycleWatchProjectionOutbox()
    await _insert_public_event_with_luma_and_meetup_links(canonical_event_id, tag, event_start)
    await tenant_repository.add(
        Tenant(
            tenant_id,
            f"oidc|projection-{tag}",
            f"{tag}-projection@example.test",
            f"{tag}-projection@u.test",
        )
    )
    lifecycle = await lifecycle_repository.get_or_create(tenant_id, canonical_event_id, workflow_id)
    await lifecycle_repository.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:registered",
        {
            "workflow_id": workflow_id,
            "event_summary": "Projection capability fixture",
            "registration_source": Source.LUMA.value,
        },
    )
    await _owner_execute(
        """
        UPDATE public.lifecycle_watch_projection_outbox
        SET created_at = TIMESTAMPTZ '1900-01-01 00:00:00+00'
        WHERE workflow_id = :workflow_id
          AND action = 'register'
        """,
        {"workflow_id": workflow_id},
    )
    claimed_projections = [
        record
        for record in await projections.claim_batch(limit=1000, lease_seconds=60)
        if record.workflow_id == workflow_id and record.action is WatchProjectionAction.REGISTER
    ]
    assert len(claimed_projections) == 1
    first_projection = claimed_projections[0]
    assert (
        await projections.mark_delivered(
            replace(first_projection, lease_token="stale-projection-lease")
        )
        is False
    )
    assert await projections.reschedule(first_projection, error="retry projection fixture") is True
    await _owner_execute(
        """
        UPDATE public.lifecycle_watch_projection_outbox
        SET next_attempt_at = clock_timestamp() - INTERVAL '1 second'
        WHERE projection_id = :projection_id
        """,
        {"projection_id": first_projection.projection_id},
    )
    retried_projections = [
        record
        for record in await projections.claim_batch(limit=1000, lease_seconds=60)
        if record.projection_id == first_projection.projection_id
    ]
    assert len(retried_projections) == 1
    assert retried_projections[0].attempt_count == 2
    assert await projections.mark_delivered(retried_projections[0]) is True


async def test_watch_projection_pre_registry_lease_check_reads_database_clock_after_row_wait(
    db: None,
) -> None:
    """A stale projection lease cannot remain authorized after a blocked registry-entry fence."""
    tag = uuid4().hex
    canonical_event_id, tenant_id = uuid4(), uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    event_start = datetime(2099, 5, 2, 18, tzinfo=UTC)
    lifecycle_repository = PostgresLifecycleRepository()
    tenant_repository = PostgresTenantRepository()
    projections = PostgresLifecycleWatchProjectionOutbox()
    await _insert_public_event_with_luma_and_meetup_links(canonical_event_id, tag, event_start)
    await tenant_repository.add(
        Tenant(
            tenant_id,
            f"oidc|projection-pre-registry-{tag}",
            f"{tag}-projection-pre-registry@example.test",
            f"{tag}-projection-pre-registry@u.test",
        )
    )
    lifecycle = await lifecycle_repository.get_or_create(tenant_id, canonical_event_id, workflow_id)
    await lifecycle_repository.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:registered",
        {
            "workflow_id": workflow_id,
            "event_summary": "Projection pre-registry lease fixture",
            "registration_source": Source.LUMA.value,
        },
    )
    await _owner_execute(
        """
        UPDATE public.lifecycle_watch_projection_outbox
        SET created_at = TIMESTAMPTZ '0001-01-01 00:00:00+00',
            next_attempt_at = TIMESTAMPTZ '0001-01-01 00:00:00+00'
        WHERE workflow_id = :workflow_id
          AND action = 'register'
          AND delivered_at IS NULL
        """,
        {"workflow_id": workflow_id},
    )
    claimed = [
        record
        for record in await projections.claim_batch(limit=1000, lease_seconds=2)
        if record.workflow_id == workflow_id and record.action is WatchProjectionAction.REGISTER
    ]
    assert len(claimed) == 1
    stale = claimed[0]
    before = await _owner_projection_state(stale.projection_id)

    async with _owner_watch_projection_lock(stale.projection_id) as owner:
        stale_check = asyncio.create_task(projections.has_live_lease(stale))
        await asyncio.sleep(0.1)
        assert not stale_check.done()
        await _wait_for_watch_projection_lease_expiry(owner, stale.projection_id)

    assert await asyncio.wait_for(stale_check, timeout=5) is False
    after = await _owner_projection_state(stale.projection_id)
    assert after.delivered_at is None
    assert after.lease_token == stale.lease_token
    assert after.attempt_count == before.attempt_count
    assert after.next_attempt_at == before.next_attempt_at

    reclaimed = [
        record
        for record in await projections.claim_batch(limit=1000, lease_seconds=60)
        if record.projection_id == stale.projection_id
    ]
    assert len(reclaimed) == 1
    fresh = reclaimed[0]
    assert fresh.lease_token != stale.lease_token
    assert fresh.attempt_count == stale.attempt_count + 1
    assert await projections.has_live_lease(stale) is False
    assert await projections.has_live_lease(fresh) is True

    async with system_session_scope() as session:
        malformed = (
            await session.execute(
                text("SELECT public.fn_has_live_watch_projection_lease(NULL::bigint, '')")
            )
        ).scalar_one()
        oversized = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_has_live_watch_projection_lease(
                        :projection_id, :lease_token
                    )
                    """
                ),
                {"projection_id": fresh.projection_id, "lease_token": "x" * 129},
            )
        ).scalar_one()
    assert malformed is False
    assert oversized is False


async def test_watch_projection_relay_recovers_post_effect_ack_loss_once(
    db: None,
) -> None:
    """A real projected watch survives ACK loss without duplicating its catch-up delivery (ADR-008)."""
    tag = uuid4().hex
    canonical_event_id, tenant_id = uuid4(), uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    repository = PostgresChangeDetectionRepository()
    lifecycle_repository = PostgresLifecycleRepository()
    tenant_repository = PostgresTenantRepository()
    projections = PostgresLifecycleWatchProjectionOutbox()
    await _insert_public_event_with_luma_and_meetup_links(
        canonical_event_id,
        tag,
        datetime(2099, 6, 1, 18, tzinfo=UTC),
    )
    await tenant_repository.add(
        Tenant(
            tenant_id,
            f"oidc|projection-ack-loss-{tag}",
            f"{tag}-projection-ack-loss@example.test",
            f"{tag}-projection-ack-loss@u.test",
        )
    )
    lifecycle = await lifecycle_repository.get_or_create(tenant_id, canonical_event_id, workflow_id)
    await lifecycle_repository.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:registered",
        {
            "workflow_id": workflow_id,
            "event_summary": "Projection acknowledgement-loss fixture",
            "registration_source": Source.LUMA.value,
        },
    )
    change = DetectedEventChange(
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
    recorded = await repository.record(change)
    await _owner_execute(
        """
        UPDATE public.lifecycle_watch_projection_outbox
        SET created_at = TIMESTAMPTZ '0001-01-01 00:00:00+00',
            next_attempt_at = clock_timestamp() - INTERVAL '1 second'
        WHERE workflow_id = :workflow_id
          AND action = 'register'
          AND delivered_at IS NULL
        """,
        {"workflow_id": workflow_id},
    )
    lossy_outbox = _RaiseBeforeFirstProjectionAck(projections)
    relay = LifecycleWatchProjectionRelay(lossy_outbox, repository, lease_seconds=60)

    assert recorded.inserted is True
    assert recorded.queued_deliveries == 0
    with pytest.raises(RuntimeError, match="simulated watch projection acknowledgement loss"):
        await relay.relay_once(limit=1)

    first = lossy_outbox.first_ack_record
    assert first is not None
    first_state = await _owner_projection_state(first.projection_id)
    _assert_pending_projection_state(first_state, first)
    assert await _owner_watch_projection_counts(
        canonical_event_id,
        Source.LUMA,
        tenant_id,
        workflow_id,
        change.fingerprint,
    ) == (1, 1, 1)

    await _owner_execute(
        """
        UPDATE public.lifecycle_watch_projection_outbox
        SET lease_expires_at = clock_timestamp() - INTERVAL '1 second'
        WHERE projection_id = :projection_id
          AND lease_token = :lease_token
          AND delivered_at IS NULL
        """,
        {"projection_id": first.projection_id, "lease_token": first.lease_token},
    )
    expired_before_reclaim = await _owner_projection_state(first.projection_id)
    assert expired_before_reclaim.lease_expires_at is not None
    assert expired_before_reclaim.lease_expired is True
    assert (
        await projections.reschedule(
            first,
            error="expired watch projection lease before reclaim",
        )
        is False
    )
    assert await projections.mark_delivered(first) is False
    assert await _owner_projection_state(first.projection_id) == expired_before_reclaim
    replayed = await relay.relay_once(limit=1)

    assert replayed == WatchProjectionStats(claimed=1, acknowledged=1)
    assert len(lossy_outbox.claimed) == 2
    reclaimed = lossy_outbox.claimed[1]
    assert reclaimed.projection_id == first.projection_id
    assert reclaimed.lease_token != first.lease_token
    assert reclaimed.attempt_count == 2
    assert reclaimed.active_since == first.active_since
    assert lossy_outbox.stale_marked is False
    assert lossy_outbox.stale_rescheduled is False
    final_state = await _owner_projection_state(first.projection_id)
    assert final_state.active_since == first.active_since
    assert final_state.delivered_at is not None
    assert final_state.lease_token is None
    assert final_state.attempt_count == 2
    assert await _owner_watch_projection_counts(
        canonical_event_id,
        Source.LUMA,
        tenant_id,
        workflow_id,
        change.fingerprint,
    ) == (1, 1, 1)


async def _assert_global_watch_registry_has_no_tenant_columns() -> None:
    """Keep the distinct source-watch registry free of tenant identifiers and PII (ADR-008)."""
    async with system_session_scope() as session:
        columns = (
            (
                await session.execute(
                    text(
                        """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'watch_registry'
                    ORDER BY column_name
                    """
                    )
                )
            )
            .scalars()
            .all()
        )
    assert columns == ["canonical_event_id", "created_at", "source", "updated_at"]


async def _owner_execute(statement: str, parameters: dict[str, object]) -> None:
    """Apply a deterministic fixture mutation through the migration owner, never the app role."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(text(statement), parameters)
    finally:
        await owner_engine.dispose()


async def _owner_expire_delivery_lease(delivery: OrganizerChangeDelivery) -> None:
    """Force only this fixture's held delivery lease past its authority boundary (ADR-008)."""
    await _owner_execute(
        """
        UPDATE public.event_change_deliveries
        SET lease_expires_at = pg_catalog.clock_timestamp() - INTERVAL '1 second'
        WHERE fingerprint = :fingerprint
          AND tenant_id = :tenant_id
          AND workflow_id = :workflow_id
          AND lease_token = :lease_token
          AND delivered_at IS NULL
        """,
        {
            "fingerprint": delivery.change.fingerprint,
            "tenant_id": delivery.tenant_id,
            "workflow_id": delivery.workflow_id,
            "lease_token": delivery.lease_token,
        },
    )


async def _owner_expire_repair_lease(repair: CalendarRepairRecord) -> None:
    """Force only this fixture's held repair lease past its authority boundary (ADR-008)."""
    await _owner_execute(
        """
        UPDATE public.event_change_calendar_repairs
        SET lease_expires_at = pg_catalog.clock_timestamp() - INTERVAL '1 second'
        WHERE repair_id = :repair_id
          AND lease_token = :lease_token
          AND repaired_at IS NULL
        """,
        {"repair_id": repair.repair_id, "lease_token": repair.lease_token},
    )


@asynccontextmanager
async def _owner_event_change_delivery_lock(
    delivery: OrganizerChangeDelivery,
) -> AsyncIterator[AsyncConnection]:
    """Hold one delivery row while an app-role capability waits on PostgreSQL's clock."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection, connection.begin():
            found = (
                await connection.execute(
                    text(
                        """
                        SELECT fingerprint
                        FROM public.event_change_deliveries
                        WHERE fingerprint = :fingerprint
                          AND tenant_id = :tenant_id
                          AND workflow_id = :workflow_id
                        FOR UPDATE
                        """
                    ),
                    {
                        "fingerprint": delivery.change.fingerprint,
                        "tenant_id": delivery.tenant_id,
                        "workflow_id": delivery.workflow_id,
                    },
                )
            ).scalar_one_or_none()
            if found is None:
                raise RuntimeError("delivery fixture owner lock did not find its delivery")
            yield connection
    finally:
        await owner.dispose()


async def _wait_for_event_change_delivery_lease_expiry(
    connection: AsyncConnection,
    delivery: OrganizerChangeDelivery,
) -> None:
    """Wait on PostgreSQL's clock while the app-role pre-signal check is lock-blocked."""
    for _ in range(240):
        expired = (
            await connection.execute(
                text(
                    """
                    SELECT lease_expires_at <= pg_catalog.clock_timestamp()
                    FROM public.event_change_deliveries
                    WHERE fingerprint = :fingerprint
                      AND tenant_id = :tenant_id
                      AND workflow_id = :workflow_id
                    """
                ),
                {
                    "fingerprint": delivery.change.fingerprint,
                    "tenant_id": delivery.tenant_id,
                    "workflow_id": delivery.workflow_id,
                },
            )
        ).scalar_one()
        if bool(expired):
            return
        await asyncio.sleep(0.025)
    raise RuntimeError("delivery fixture lease did not expire while the app check was blocked")


@asynccontextmanager
async def _owner_watch_projection_lock(projection_id: int) -> AsyncIterator[AsyncConnection]:
    """Hold one projection row while an app-role capability waits on PostgreSQL's clock."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection, connection.begin():
            found = (
                await connection.execute(
                    text(
                        """
                        SELECT projection_id
                        FROM public.lifecycle_watch_projection_outbox
                        WHERE projection_id = :projection_id
                        FOR UPDATE
                        """
                    ),
                    {"projection_id": projection_id},
                )
            ).scalar_one_or_none()
            if found is None:
                raise RuntimeError("projection fixture owner lock did not find its projection")
            yield connection
    finally:
        await owner.dispose()


async def _wait_for_watch_projection_lease_expiry(
    connection: AsyncConnection, projection_id: int
) -> None:
    """Wait on PostgreSQL's clock while the app-role pre-registry check is lock-blocked."""
    for _ in range(240):
        expired = (
            await connection.execute(
                text(
                    """
                    SELECT lease_expires_at <= pg_catalog.clock_timestamp()
                    FROM public.lifecycle_watch_projection_outbox
                    WHERE projection_id = :projection_id
                    """
                ),
                {"projection_id": projection_id},
            )
        ).scalar_one()
        if bool(expired):
            return
        await asyncio.sleep(0.025)
    raise RuntimeError("projection fixture lease did not expire while the app check was blocked")


@asynccontextmanager
async def _owner_calendar_repair_lock(repair_id: int) -> AsyncIterator[AsyncConnection]:
    """Hold one repair row while an app-role capability waits on PostgreSQL's clock."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection, connection.begin():
            found = (
                await connection.execute(
                    text(
                        """
                        SELECT repair_id
                        FROM public.event_change_calendar_repairs
                        WHERE repair_id = :repair_id
                        FOR UPDATE
                        """
                    ),
                    {"repair_id": repair_id},
                )
            ).scalar_one_or_none()
            if found is None:
                raise RuntimeError("calendar-repair fixture owner lock did not find its repair")
            yield connection
    finally:
        await owner.dispose()


async def _wait_for_calendar_repair_lease_expiry(
    connection: AsyncConnection, repair_id: int
) -> None:
    """Wait on PostgreSQL's clock while the app-role pre-effect check is lock-blocked."""
    for _ in range(240):
        expired = (
            await connection.execute(
                text(
                    """
                    SELECT lease_expires_at <= pg_catalog.clock_timestamp()
                    FROM public.event_change_calendar_repairs
                    WHERE repair_id = :repair_id
                    """
                ),
                {"repair_id": repair_id},
            )
        ).scalar_one()
        if bool(expired):
            return
        await asyncio.sleep(0.025)
    raise RuntimeError(
        "calendar-repair fixture lease did not expire while the app check was blocked"
    )


async def _claim_fixture_deliveries(
    repository: PostgresChangeDetectionRepository,
    fingerprints: set[str],
) -> dict[str, OrganizerChangeDelivery]:
    """Claim fixture-targeted global fanout rows after their owner-set year-0001 priority."""
    return {
        delivery.change.fingerprint: delivery
        for delivery in await repository.claim_deliveries(
            limit=len(fingerprints),
            lease_seconds=300,
        )
        if delivery.change.fingerprint in fingerprints
    }


async def _claim_fixture_repairs(
    repairs: PostgresClosedWorkflowCalendarRepairRepository,
    fingerprints: set[str],
) -> dict[str, CalendarRepairRecord]:
    """Claim fixture-targeted repairs after their owner-set year-0001 ready time."""
    return {
        repair.change.fingerprint: repair
        for repair in await repairs.claim_batch(
            limit=len(fingerprints),
            lease_seconds=300,
        )
        if repair.change.fingerprint in fingerprints
    }


async def _call_mark_event_change_delivery(delivery: OrganizerChangeDelivery) -> bool:
    """Invoke the delivery acknowledgement capability directly, without worker semantics."""
    async with system_session_scope() as session:
        marked = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_mark_event_change_delivery(
                        :fingerprint, :tenant_id, :workflow_id, :lease_token
                    ) AS marked
                    """
                ),
                {
                    "fingerprint": delivery.change.fingerprint,
                    "tenant_id": delivery.tenant_id,
                    "workflow_id": delivery.workflow_id,
                    "lease_token": delivery.lease_token,
                },
            )
        ).scalar_one()
    return bool(marked)


async def _call_release_event_change_delivery(
    delivery: OrganizerChangeDelivery,
    *,
    error: str,
) -> bool:
    """Invoke the delivery retry capability directly, without worker semantics."""
    async with system_session_scope() as session:
        released = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_release_event_change_delivery(
                        :fingerprint, :tenant_id, :workflow_id, :lease_token, :error
                    ) AS released
                    """
                ),
                {
                    "fingerprint": delivery.change.fingerprint,
                    "tenant_id": delivery.tenant_id,
                    "workflow_id": delivery.workflow_id,
                    "lease_token": delivery.lease_token,
                    "error": error,
                },
            )
        ).scalar_one()
    return bool(released)


async def _call_enqueue_closed_workflow_calendar_repair(
    delivery: OrganizerChangeDelivery,
    *,
    error: str,
) -> bool:
    """Invoke the atomic delivery-retirement and repair-enqueue capability directly."""
    async with system_session_scope() as session:
        enqueued = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_enqueue_closed_workflow_calendar_repair(
                        :fingerprint, :tenant_id, :workflow_id, :lease_token, :error
                    ) AS enqueued
                    """
                ),
                {
                    "fingerprint": delivery.change.fingerprint,
                    "tenant_id": delivery.tenant_id,
                    "workflow_id": delivery.workflow_id,
                    "lease_token": delivery.lease_token,
                    "error": error,
                },
            )
        ).scalar_one()
    return bool(enqueued)


async def _call_mark_calendar_repair(repair: CalendarRepairRecord) -> bool:
    """Invoke the repair acknowledgement capability directly, without worker semantics."""
    async with system_session_scope() as session:
        marked = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_mark_calendar_repair(:repair_id, :lease_token) AS marked
                    """
                ),
                {"repair_id": repair.repair_id, "lease_token": repair.lease_token},
            )
        ).scalar_one()
    return bool(marked)


async def _call_reschedule_calendar_repair(
    repair: CalendarRepairRecord,
    *,
    error: str,
) -> bool:
    """Invoke the repair retry capability directly, without worker semantics."""
    async with system_session_scope() as session:
        released = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_reschedule_calendar_repair(
                        :repair_id, :lease_token, :error
                    ) AS released
                    """
                ),
                {
                    "repair_id": repair.repair_id,
                    "lease_token": repair.lease_token,
                    "error": error,
                },
            )
        ).scalar_one()
    return bool(released)


@dataclass(frozen=True, slots=True)
class _TerminalLeaseFixture:
    """Independent delivery records used to isolate every ADR-008 terminal capability."""

    repository: PostgresChangeDetectionRepository
    repairs: PostgresClosedWorkflowCalendarRepairRepository
    mark_delivery: OrganizerChangeDelivery
    release_delivery: OrganizerChangeDelivery
    enqueue_delivery: OrganizerChangeDelivery
    repair_reschedule_delivery: OrganizerChangeDelivery


@dataclass(frozen=True, slots=True)
class _RecoveredDeliveryLeases:
    """Fresh delivery leases whose normal enqueue path creates the two repair test records."""

    fresh_enqueue_delivery: OrganizerChangeDelivery
    repair_reschedule_delivery: OrganizerChangeDelivery


@dataclass(frozen=True, slots=True)
class _DeliveryQueueState:
    """Owner-visible delivery facts needed to prove an expired mutation is a no-op."""

    created_at: datetime
    delivered_at: datetime | None
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_expired: bool | None
    next_attempt_at: datetime
    last_error: str | None
    attempt_count: int


async def _owner_delivery_state(delivery: OrganizerChangeDelivery) -> _DeliveryQueueState:
    """Read one opaque fanout row's full mutable state through the migration-owner connection."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT created_at, delivered_at, lease_token, lease_expires_at,
                               lease_expires_at <= pg_catalog.clock_timestamp() AS lease_expired,
                               next_attempt_at, last_error, attempt_count
                        FROM public.event_change_deliveries
                        WHERE fingerprint = :fingerprint
                          AND tenant_id = :tenant_id
                          AND workflow_id = :workflow_id
                        """
                    ),
                    {
                        "fingerprint": delivery.change.fingerprint,
                        "tenant_id": delivery.tenant_id,
                        "workflow_id": delivery.workflow_id,
                    },
                )
            ).one()
    finally:
        await owner_engine.dispose()
    return _DeliveryQueueState(
        created_at=row.created_at,
        delivered_at=row.delivered_at,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        lease_expired=row.lease_expired,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        attempt_count=int(row.attempt_count),
    )


async def _owner_repair_count(delivery: OrganizerChangeDelivery) -> int:
    """Count this delivery's repair rows without exposing the global queue to the app role."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.connect() as connection:
            count = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM public.event_change_calendar_repairs
                        WHERE fingerprint = :fingerprint
                          AND tenant_id = :tenant_id
                          AND workflow_id = :workflow_id
                        """
                    ),
                    {
                        "fingerprint": delivery.change.fingerprint,
                        "tenant_id": delivery.tenant_id,
                        "workflow_id": delivery.workflow_id,
                    },
                )
            ).scalar_one()
    finally:
        await owner_engine.dispose()
    return int(count)


@dataclass(frozen=True, slots=True)
class _RepairQueueState:
    """Owner-visible repair facts needed to prove an expired mutation is a no-op."""

    created_at: datetime
    repaired_at: datetime | None
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_expired: bool | None
    next_attempt_at: datetime
    last_error: str | None
    attempt_count: int


async def _owner_repair_state(repair: CalendarRepairRecord) -> _RepairQueueState:
    """Read one opaque repair row's full mutable state through the migration-owner connection."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT created_at, repaired_at, lease_token, lease_expires_at,
                               lease_expires_at <= pg_catalog.clock_timestamp() AS lease_expired,
                               next_attempt_at, last_error, attempt_count
                        FROM public.event_change_calendar_repairs
                        WHERE repair_id = :repair_id
                        """
                    ),
                    {"repair_id": repair.repair_id},
                )
            ).one()
    finally:
        await owner_engine.dispose()
    return _RepairQueueState(
        created_at=row.created_at,
        repaired_at=row.repaired_at,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        lease_expired=row.lease_expired,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        attempt_count=int(row.attempt_count),
    )


@dataclass(frozen=True, slots=True)
class _ProjectionQueueState:
    """Owner-visible state needed to prove a guarded projection's recovery invariant."""

    active_since: datetime
    delivered_at: datetime | None
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_expired: bool | None
    next_attempt_at: datetime
    last_error: str | None
    attempt_count: int


def _assert_pending_projection_state(
    state: _ProjectionQueueState,
    record: WatchProjectionRecord,
) -> None:
    """Keep post-loss and pre-reclaim facts identical under an expired exact token."""
    assert state.active_since == record.active_since
    assert state.delivered_at is None
    assert state.lease_token == record.lease_token
    assert state.attempt_count == record.attempt_count


async def _owner_projection_state(projection_id: int) -> _ProjectionQueueState:
    """Read one opaque projection's durable recovery facts through the owner-only connection."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT active_since, delivered_at, lease_token, lease_expires_at,
                               lease_expires_at <= pg_catalog.clock_timestamp() AS lease_expired,
                               next_attempt_at, last_error, attempt_count
                        FROM public.lifecycle_watch_projection_outbox
                        WHERE projection_id = :projection_id
                        """
                    ),
                    {"projection_id": projection_id},
                )
            ).one()
    finally:
        await owner_engine.dispose()
    return _ProjectionQueueState(
        active_since=row.active_since,
        delivered_at=row.delivered_at,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        lease_expired=row.lease_expired,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        attempt_count=int(row.attempt_count),
    )


async def _owner_watch_projection_counts(
    canonical_event_id: UUID,
    source: Source,
    tenant_id: UUID,
    workflow_id: str,
    fingerprint: str,
) -> tuple[int, int, int]:
    """Count this fixture's subscription, global watch, and catch-up delivery exactly once."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            (
                                SELECT count(*)
                                FROM public.watch_subscriptions
                                WHERE canonical_event_id = :canonical_event_id
                                  AND source = :source
                                  AND tenant_id = :tenant_id
                                  AND workflow_id = :workflow_id
                            ) AS subscription_count,
                            (
                                SELECT count(*)
                                FROM public.watch_registry
                                WHERE canonical_event_id = :canonical_event_id
                                  AND source = :source
                            ) AS watch_count,
                            (
                                SELECT count(*)
                                FROM public.event_change_deliveries
                                WHERE fingerprint = :fingerprint
                                  AND tenant_id = :tenant_id
                                  AND workflow_id = :workflow_id
                            ) AS delivery_count
                        """
                    ),
                    {
                        "canonical_event_id": canonical_event_id,
                        "source": source.value,
                        "tenant_id": tenant_id,
                        "workflow_id": workflow_id,
                        "fingerprint": fingerprint,
                    },
                )
            ).one()
    finally:
        await owner_engine.dispose()
    return int(row.subscription_count), int(row.watch_count), int(row.delivery_count)


async def _owner_record_clock_values(
    lifecycle_id: UUID, fingerprint: str
) -> tuple[datetime, datetime]:
    """Read opaque queue timing facts through the owner-only test connection."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT projection.active_since, change.detected_at
                        FROM public.lifecycle_watch_projection_outbox AS projection
                        CROSS JOIN public.event_changes AS change
                        WHERE projection.lifecycle_id = :lifecycle_id
                          AND projection.action = 'register'
                          AND change.fingerprint = :fingerprint
                        """
                    ),
                    {"lifecycle_id": lifecycle_id, "fingerprint": fingerprint},
                )
            ).one()
    finally:
        await owner_engine.dispose()
    return row.active_since, row.detected_at


async def _register_watch_pair(
    repository: PostgresChangeDetectionRepository,
    first: LifecycleWatch,
    second: LifecycleWatch,
    cross_tenant: LifecycleWatch,
    wrong_source: LifecycleWatch,
) -> None:
    """Prove an active source-linked lifecycle can subscribe only under its own RLS context."""
    assert await repository.register(first) is True
    assert await repository.register(second) is True
    assert await repository.register(first) is False
    assert await repository.register(cross_tenant) is False
    assert await repository.register(wrong_source) is False
    assert await repository.unregister(first) is False
    assert {
        (watch.canonical_event_id, watch.source, watch.subscriber_count)
        for watch in await repository.list_watches()
        if watch.canonical_event_id == first.canonical_event_id
    } == {
        (first.canonical_event_id, first.source, 1),
        (second.canonical_event_id, second.source, 1),
    }


async def _insert_public_event_with_luma_and_meetup_links(
    canonical_event_id: UUID, tag: str, start_at: datetime
) -> None:
    """Seed only public catalog data; the global registry must not depend on tenant event copies."""
    async with system_session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO canonical_events
                    (canonical_event_id, title, start_at, description, price_status)
                VALUES (:canonical_event_id, :title, :start_at, :description, 'free')
                """
            ),
            {
                "canonical_event_id": canonical_event_id,
                "title": f"Change detector fixture {tag}",
                "start_at": start_at,
                "description": "public central detector fixture",
            },
        )
        for source, source_event_id, registration_url in (
            ("luma", f"detector-luma-{tag}", f"https://luma.test/detector-luma-{tag}"),
            ("meetup", f"detector-meetup-{tag}", f"https://meetup.test/detector-meetup-{tag}"),
        ):
            await session.execute(
                text(
                    """
                    INSERT INTO event_source_links
                        (source, source_event_id, canonical_event_id, registration_url, price_status)
                    VALUES (:source, :source_event_id, :canonical_event_id, :registration_url, 'free')
                    """
                ),
                {
                    "source": source,
                    "source_event_id": source_event_id,
                    "canonical_event_id": canonical_event_id,
                    "registration_url": registration_url,
                },
            )


async def _assert_released_delivery_is_delayed_and_make_it_ready(
    delivery: OrganizerChangeDelivery,
) -> None:
    """Verify capability-owned retry timing, then advance this owner-only fixture without sleeping."""
    fingerprint = delivery.change.fingerprint
    tenant_id = delivery.tenant_id
    workflow_id = delivery.workflow_id
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    parameters = {
        "fingerprint": fingerprint,
        "tenant_id": tenant_id,
        "workflow_id": workflow_id,
    }
    try:
        async with owner_engine.begin() as connection:
            delayed = (
                await connection.execute(
                    text(
                        """
                        SELECT next_attempt_at > now()
                        FROM public.event_change_deliveries
                        WHERE fingerprint = :fingerprint
                          AND tenant_id = :tenant_id
                          AND workflow_id = :workflow_id
                        """
                    ),
                    parameters,
                )
            ).scalar_one()
            assert delayed is True
            await connection.execute(
                text(
                    """
                    UPDATE public.event_change_deliveries
                    SET next_attempt_at = now() - INTERVAL '1 second'
                    WHERE fingerprint = :fingerprint
                      AND tenant_id = :tenant_id
                      AND workflow_id = :workflow_id
                    """
                ),
                parameters,
            )
    finally:
        await owner_engine.dispose()


async def _assert_app_role_cannot_directly_mutate_watches() -> None:
    """Keep the global tenant-bearing control plane capability-only (ADR-007/008, NFR-7)."""
    async with system_session_scope() as session:
        watch_mutation_denied = (
            await session.execute(
                text(
                    """
                    SELECT has_table_privilege(current_user, 'public.watch_registry', 'INSERT'),
                           has_table_privilege(current_user, 'public.watch_registry', 'UPDATE'),
                           has_table_privilege(current_user, 'public.watch_registry', 'DELETE'),
                           has_table_privilege(current_user, 'public.watch_subscriptions', 'INSERT'),
                           has_table_privilege(current_user, 'public.watch_subscriptions', 'UPDATE'),
                           has_table_privilege(current_user, 'public.watch_subscriptions', 'DELETE')
                    """
                )
            )
        ).one()
        control_tables_denied = (
            await session.execute(
                text(
                    """
                    SELECT bool_and(
                        NOT has_table_privilege(current_user, table_name, privilege_name)
                    )
                    FROM unnest(
                        ARRAY[
                            'public.event_changes',
                            'public.event_change_deliveries',
                            'public.event_change_calendar_repairs',
                            'public.lifecycle_organizer_change_ledger',
                            'public.lifecycle_watch_projection_outbox',
                            'public.watch_subscriptions'
                        ]
                    ) AS tables(table_name)
                    CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'])
                        AS privileges(privilege_name)
                    """
                )
            )
        ).scalar_one()
        sequences_denied = (
            await session.execute(
                text(
                    """
                    SELECT bool_and(
                        NOT has_sequence_privilege(current_user, sequence_name, privilege_name)
                    )
                    FROM unnest(
                        ARRAY[
                            'public.event_change_calendar_repairs_repair_id_seq',
                            'public.lifecycle_watch_projection_outbox_projection_id_seq'
                        ]
                    ) AS sequences(sequence_name)
                    CROSS JOIN unnest(ARRAY['USAGE', 'SELECT', 'UPDATE'])
                        AS privileges(privilege_name)
                    """
                )
            )
        ).scalar_one()
        capabilities_granted = (
            await session.execute(
                text(
                    """
                    SELECT bool_and(
                        has_function_privilege(current_user, function_identity, 'EXECUTE')
                    )
                    FROM unnest(
                        ARRAY[
                            'public.fn_list_active_change_watches()',
                            'public.fn_record_event_change(text, uuid, text, text, timestamptz, timestamptz, text, text, text)',
                            'public.fn_claim_event_change_deliveries(integer, integer, text)',
                            'public.fn_has_live_event_change_delivery_lease(text, uuid, text, text)',
                            'public.fn_mark_event_change_delivery(text, uuid, text, text)',
                            'public.fn_release_event_change_delivery(text, uuid, text, text, text)',
                            'public.fn_enqueue_closed_workflow_calendar_repair(text, uuid, text, text, text)',
                            'public.fn_claim_calendar_repairs(integer, integer, text)',
                            'public.fn_mark_calendar_repair(bigint, text)',
                            'public.fn_reschedule_calendar_repair(bigint, text, text)',
                            'public.fn_calendar_repair_change_applied(bigint, text)',
                            'public.fn_has_live_calendar_repair_lease(bigint, text)',
                            'public.fn_claim_watch_projections(integer, integer, text)',
                            'public.fn_has_live_watch_projection_lease(bigint, text)',
                            'public.fn_mark_watch_projection_delivered(bigint, text)',
                            'public.fn_watch_projection_reschedule(bigint, text, text)'
                        ]
                    ) AS functions(function_identity)
                    """
                )
            )
        ).scalar_one()
    assert tuple(watch_mutation_denied) == (False, False, False, False, False, False)
    assert control_tables_denied is True
    assert sequences_denied is True
    assert capabilities_granted is True


async def _schedule(
    repository: PostgresLifecycleRepository,
    tenant_id: UUID,
    canonical_event_id: UUID,
    workflow_id: str,
    source: Source,
) -> None:
    """Use a source-bound guarded state machine so the watch guard sees a real lifecycle."""
    lifecycle = await repository.get_or_create(tenant_id, canonical_event_id, workflow_id)
    await repository.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:registered",
        {
            "workflow_id": workflow_id,
            "event_summary": "Detector lifecycle fixture",
            "registration_source": source.value,
        },
    )
    await repository.transition(
        lifecycle,
        LifecycleState.SCHEDULED,
        f"{workflow_id}:scheduled",
        {"workflow_id": workflow_id, "event_summary": "Detector lifecycle fixture"},
    )


async def _cancel_active_lifecycle(
    repository: PostgresLifecycleRepository,
    tenant_id: UUID,
    canonical_event_id: UUID,
    workflow_id: str,
) -> None:
    """Leave the active set through the guard before its terminal unwatch projection applies."""
    lifecycle = await repository.find_active(tenant_id, canonical_event_id)
    assert lifecycle is not None
    assert lifecycle.workflow_id == workflow_id
    await repository.transition(
        lifecycle,
        LifecycleState.CANCELLED,
        f"{workflow_id}:cancelled",
        {"workflow_id": workflow_id, "event_summary": "Detector lifecycle terminal fixture"},
    )


async def _cancel_and_unregister_pair(
    repository: PostgresChangeDetectionRepository,
    lifecycle_repository: PostgresLifecycleRepository,
    first: LifecycleWatch,
    second: LifecycleWatch,
) -> None:
    """Apply terminal projection semantics to both source-distinct fixture subscriptions."""
    await _cancel_active_lifecycle(
        lifecycle_repository,
        first.tenant_id,
        first.canonical_event_id,
        first.workflow_id,
    )
    assert await repository.unregister(first) is True
    watches = [
        watch
        for watch in await repository.list_watches()
        if watch.canonical_event_id == first.canonical_event_id
    ]
    assert [
        (watch.canonical_event_id, watch.source, watch.subscriber_count) for watch in watches
    ] == [(second.canonical_event_id, second.source, 1)]
    await _cancel_active_lifecycle(
        lifecycle_repository,
        second.tenant_id,
        second.canonical_event_id,
        second.workflow_id,
    )
    assert await repository.unregister(second) is True
    assert all(
        watch.canonical_event_id != first.canonical_event_id
        for watch in await repository.list_watches()
    )
