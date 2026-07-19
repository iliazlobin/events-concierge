"""PostgreSQL handoff-TTL queue and guarded-expiry integration tests (FR-6.6, ADR-007)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.postgres.handoff_expiry import PostgresHandoffExpiryRepository
from events_concierge.adapters.postgres.tenant_repos import (
    PostgresHandoffRepository,
    PostgresLifecycleRepository,
    PostgresTenantRepository,
)
from events_concierge.application.handoff_expiry import HandoffExpiryWorker
from events_concierge.application.reconciliation import HandoffExpiryStatus
from events_concierge.composition import build_container
from events_concierge.config import get_settings
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import HandoffReason, HandoffState, LifecycleState, Source
from events_concierge.domain.ids import registration_workflow_id
from events_concierge.domain.lifecycle import HandoffTask, Lifecycle
from events_concierge.infra.db import system_session_scope, tenant_session_scope
from events_concierge.ports.handoff_expiry import HandoffExpiryRepairOutcome

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class _HandoffExpiryEffectSnapshot:
    """Complete mutable effect projection for stale orphan-repair assertions (ADR-007)."""

    lifecycle_state: str
    task_state: str
    queue_next_attempt_at: datetime
    queue_attempt_count: int
    queue_lease_token: str | None
    queue_lease_expires_at: datetime | None
    queue_resolved_at: datetime | None
    queue_last_error: str | None
    transition_count: int
    expired_outbox_count: int


class _BlockingClosedLiveness:
    """Pause exactly after the worker's liveness decision to model a lease-expiry race."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def is_open(self, workflow_id: str) -> bool:
        self.calls.append(workflow_id)
        self.entered.set()
        await self.release.wait()
        return False


async def test_expiry_queue_uses_five_minute_grace_and_exact_leases(db: None) -> None:
    """The sweeper cannot claim inside grace, and an opaque lease can retry/ack exactly once."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    expiry_repo = PostgresHandoffExpiryRepository()

    recent_lifecycle, recent_task = await _create_handoff_task(
        lifecycle_repo,
        handoff_repo,
        ttl_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    async with system_session_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT ttl_expires_at, eligible_at, resolved_at
                    FROM handoff_expiry_queue
                    WHERE task_id = :task_id
                    """
                ),
                {"task_id": recent_task.task_id},
            )
        ).one()
    assert row.eligible_at == row.ttl_expires_at + timedelta(minutes=5)
    assert row.eligible_at > datetime.now(UTC)
    assert row.resolved_at is None

    # The authoritative Temporal timer can expire at TTL without waiting for the repair grace.
    await _expire(lifecycle_repo, recent_lifecycle, recent_task)

    # A deliberately ancient fixture sorts ahead of any existing local queue row, making the
    # leased-control-plane assertions independent from unrelated integration fixtures.
    ancient_lifecycle, ancient_task = await _create_handoff_task(
        lifecycle_repo,
        handoff_repo,
        ttl_expires_at=datetime(1900, 1, 1, tzinfo=UTC),
    )
    [leased] = await expiry_repo.claim_batch(limit=1, lease_seconds=30)
    assert leased.task_id == ancient_task.task_id
    assert leased.tenant_id == ancient_task.tenant_id
    assert leased.workflow_id == ancient_task.workflow_id
    assert leased.expiry_transition_id == ancient_task.resolved_expiry_transition_id()
    assert leased.attempt_count == 1

    retry_at = datetime(1900, 1, 1, 0, 6, tzinfo=UTC)
    assert await expiry_repo.reschedule(leased, retry_at=retry_at, error="fixture retry")
    [reclaimed] = await expiry_repo.claim_batch(limit=1, lease_seconds=30)
    assert reclaimed.task_id == ancient_task.task_id
    assert reclaimed.attempt_count == 2
    assert await expiry_repo.acknowledge(reclaimed)
    assert not await expiry_repo.acknowledge(reclaimed)
    await _expire(lifecycle_repo, ancient_lifecycle, ancient_task)


async def test_expired_expiry_queue_writes_require_a_live_lease_before_reclaim(db: None) -> None:
    """A stale orphan-repair worker cannot resolve or reschedule its expired lease (ADR-007)."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    expiry_repo = PostgresHandoffExpiryRepository()
    lifecycle, task = await _create_handoff_task(
        lifecycle_repo,
        handoff_repo,
        ttl_expires_at=datetime(1900, 1, 1, tzinfo=UTC),
    )

    [stale] = await expiry_repo.claim_batch(limit=1, lease_seconds=60)
    assert stale.task_id == task.task_id
    async with system_session_scope() as session:
        expired = await session.execute(
            text(
                """
                UPDATE handoff_expiry_queue
                SET lease_expires_at = pg_catalog.clock_timestamp() - INTERVAL '1 second'
                WHERE task_id = :task_id
                  AND lease_token = :lease_token
                RETURNING task_id
                """
            ),
            {"task_id": stale.task_id, "lease_token": stale.lease_token},
        )
        assert expired.scalar_one() == stale.task_id
        before = tuple(
            (
                await session.execute(
                    text(
                        """
                        SELECT next_attempt_at, attempt_count, lease_token, lease_expires_at,
                               resolved_at, last_error
                        FROM handoff_expiry_queue
                        WHERE task_id = :task_id
                        """
                    ),
                    {"task_id": stale.task_id},
                )
            ).one()
        )

    assert await expiry_repo.acknowledge(stale) is False
    assert (
        await expiry_repo.reschedule(
            stale,
            retry_at=datetime(1900, 1, 1, 0, 6, tzinfo=UTC),
            error="stale retry after lease expiry",
        )
        is False
    )
    async with system_session_scope() as session:
        after = tuple(
            (
                await session.execute(
                    text(
                        """
                        SELECT next_attempt_at, attempt_count, lease_token, lease_expires_at,
                               resolved_at, last_error
                        FROM handoff_expiry_queue
                        WHERE task_id = :task_id
                        """
                    ),
                    {"task_id": stale.task_id},
                )
            ).one()
        )
    assert after == before

    [fresh] = await expiry_repo.claim_batch(limit=1, lease_seconds=60)
    assert fresh.task_id == stale.task_id
    assert fresh.lease_token != stale.lease_token
    assert await expiry_repo.reschedule(
        fresh,
        retry_at=datetime(1900, 1, 1, 0, 6, tzinfo=UTC),
        error="fresh retry after stale-owner recovery",
    )
    [reclaimed] = await expiry_repo.claim_batch(limit=1, lease_seconds=60)
    assert reclaimed.lease_token != fresh.lease_token
    assert await expiry_repo.acknowledge(reclaimed)
    await _expire(lifecycle_repo, lifecycle, task)


async def test_orphan_expiry_effect_requires_a_live_lease_before_reclaim(db: None) -> None:
    """A stale orphan worker cannot create a terminal lifecycle effect before recovery (ADR-007)."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    expiry_repo = PostgresHandoffExpiryRepository()
    lifecycle, task = await _create_handoff_task(
        lifecycle_repo,
        handoff_repo,
        ttl_expires_at=datetime(1900, 1, 1, tzinfo=UTC),
    )

    [stale] = await expiry_repo.claim_batch(limit=1, lease_seconds=60)
    assert stale.task_id == task.task_id
    await _expire_owner_handoff_expiry_lease(stale.task_id, stale.lease_token)
    before = await _handoff_expiry_effect_snapshot(lifecycle, task)

    assert await expiry_repo.expire_orphan(stale) is HandoffExpiryRepairOutcome.LEASE_LOST
    assert await _handoff_expiry_effect_snapshot(lifecycle, task) == before

    [fresh] = await expiry_repo.claim_batch(limit=1, lease_seconds=60)
    assert fresh.task_id == stale.task_id
    assert fresh.lease_token != stale.lease_token
    assert await expiry_repo.expire_orphan(fresh) is HandoffExpiryRepairOutcome.EXPIRED

    after = await _handoff_expiry_effect_snapshot(lifecycle, task)
    assert after.lifecycle_state == LifecycleState.EXPIRED.value
    assert after.task_state == HandoffState.EXPIRED.value
    assert after.queue_lease_token is None
    assert after.queue_lease_expires_at is None
    assert after.queue_resolved_at is not None
    assert after.queue_last_error is None
    assert after.transition_count == 1
    assert after.expired_outbox_count == 1
    payload = await _expired_outbox_payload(lifecycle, task)
    serialized_payload = json.dumps(payload, sort_keys=True)
    assert stale.lease_token not in serialized_payload
    assert fresh.lease_token not in serialized_payload


async def test_orphan_expiry_rolls_back_when_its_lease_expires_during_the_guarded_effect(
    db: None,
) -> None:
    """The final live-lease fence rolls back lifecycle/outbox work that crossed expiry (ADR-007)."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    expiry_repo = PostgresHandoffExpiryRepository()
    lifecycle, task = await _create_handoff_task(
        lifecycle_repo,
        handoff_repo,
        ttl_expires_at=datetime(1900, 1, 1, tzinfo=UTC),
    )
    [stale] = await expiry_repo.claim_batch(limit=1, lease_seconds=1)
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    effect: asyncio.Task[HandoffExpiryRepairOutcome] | None = None
    try:
        async with owner.begin() as connection:
            locked_task_id = (
                await connection.execute(
                    text(
                        """
                        SELECT task_id
                        FROM public.handoff_expiry_queue
                        WHERE task_id = :task_id
                          AND lease_token = :lease_token
                        FOR UPDATE
                        """
                    ),
                    {"task_id": stale.task_id, "lease_token": stale.lease_token},
                )
            ).scalar_one()
            assert locked_task_id == stale.task_id
            effect = asyncio.create_task(expiry_repo.expire_orphan(stale))
            await asyncio.sleep(1.2)
            assert not effect.done()
        assert effect is not None
        assert await effect is HandoffExpiryRepairOutcome.LEASE_LOST
    finally:
        if effect is not None and not effect.done():
            effect.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await effect
        await owner.dispose()

    after_stale = await _handoff_expiry_effect_snapshot(lifecycle, task)
    assert after_stale.lifecycle_state == LifecycleState.HANDOFF.value
    assert after_stale.task_state == HandoffState.OPEN.value
    assert after_stale.queue_lease_token == stale.lease_token
    assert after_stale.queue_resolved_at is None
    assert after_stale.transition_count == 0
    assert after_stale.expired_outbox_count == 0

    [fresh] = await expiry_repo.claim_batch(limit=1, lease_seconds=60)
    assert fresh.lease_token != stale.lease_token
    assert await expiry_repo.expire_orphan(fresh) is HandoffExpiryRepairOutcome.EXPIRED
    after_fresh = await _handoff_expiry_effect_snapshot(lifecycle, task)
    assert after_fresh.transition_count == 1
    assert after_fresh.expired_outbox_count == 1


async def test_stale_orphan_worker_cannot_clear_a_fresh_reclaimed_queue_lease(db: None) -> None:
    """A lease lost after liveness cannot terminalize or disturb the fresh repair owner (ADR-007)."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    expiry_repo = PostgresHandoffExpiryRepository()
    lifecycle, task = await _create_handoff_task(
        lifecycle_repo,
        handoff_repo,
        ttl_expires_at=datetime(1900, 1, 1, tzinfo=UTC),
    )
    liveness = _BlockingClosedLiveness()
    worker = HandoffExpiryWorker(expiry_repo, liveness, lease_seconds=60)
    stale_cycle = asyncio.create_task(worker.expire_once(limit=1))
    await asyncio.wait_for(liveness.entered.wait(), timeout=2)
    stale_token = await _handoff_expiry_queue_lease_token(task.task_id)
    await _expire_owner_handoff_expiry_lease(task.task_id, stale_token)
    [fresh] = await expiry_repo.claim_batch(limit=1, lease_seconds=60)
    assert fresh.task_id == task.task_id
    assert fresh.lease_token != stale_token
    before_stale_resume = await _handoff_expiry_effect_snapshot(lifecycle, task)

    liveness.release.set()
    stale_stats = await asyncio.wait_for(stale_cycle, timeout=2)
    assert stale_stats.claimed == 1
    assert stale_stats.expired == 0
    assert stale_stats.acknowledged == 0
    assert stale_stats.retried == 0
    assert stale_stats.lost_leases == 1
    assert await _handoff_expiry_effect_snapshot(lifecycle, task) == before_stale_resume

    assert await expiry_repo.expire_orphan(fresh) is HandoffExpiryRepairOutcome.EXPIRED
    after = await _handoff_expiry_effect_snapshot(lifecycle, task)
    assert after.lifecycle_state == LifecycleState.EXPIRED.value
    assert after.task_state == HandoffState.EXPIRED.value
    assert after.queue_resolved_at is not None
    assert after.transition_count == 1
    assert after.expired_outbox_count == 1


async def test_normal_temporal_expiry_ignores_the_orphan_queue_lease(db: None) -> None:
    """Temporal retains its direct TTL authority even while the delayed repair row is leased (ADR-007)."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    expiry_repo = PostgresHandoffExpiryRepository()
    lifecycle, task = await _create_handoff_task(
        lifecycle_repo,
        handoff_repo,
        ttl_expires_at=datetime(1900, 1, 1, tzinfo=UTC),
    )
    [leased] = await expiry_repo.claim_batch(limit=1, lease_seconds=60)
    assert leased.task_id == task.task_id

    await _expire(lifecycle_repo, lifecycle, task)

    after = await _handoff_expiry_effect_snapshot(lifecycle, task)
    assert after.lifecycle_state == LifecycleState.EXPIRED.value
    assert after.task_state == HandoffState.EXPIRED.value
    assert after.queue_resolved_at is not None
    assert after.queue_lease_token is None
    assert after.transition_count == 1
    assert after.expired_outbox_count == 1


async def test_expiry_transition_requires_the_due_task_identity_and_cleans_resources(
    db: None,
) -> None:
    """Only the named due task can terminalize its lifecycle; terminal transition resolves its queue."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    lifecycle, task = await _create_handoff_task(
        lifecycle_repo,
        handoff_repo,
        ttl_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    with pytest.raises(Exception, match="permission denied"):
        async with tenant_session_scope(lifecycle.tenant_id) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO handoff_tasks
                        (task_id, tenant_id, workflow_id, canonical_event_id, reason, deep_link,
                         event_summary, ttl_expires_at, state, metadata, expiry_transition_id)
                    VALUES
                        (:task_id, :tenant_id, :workflow_id, :canonical_event_id,
                         'deferred_register', :deep_link, :event_summary, :ttl_expires_at,
                         'open', '{}'::jsonb, :expiry_transition_id)
                    """
                ),
                {
                    "task_id": f"{task.task_id}:direct-insert",
                    "tenant_id": lifecycle.tenant_id,
                    "workflow_id": lifecycle.workflow_id,
                    "canonical_event_id": lifecycle.canonical_event_id,
                    "deep_link": task.deep_link,
                    "event_summary": task.event_summary,
                    "ttl_expires_at": task.ttl_expires_at,
                    "expiry_transition_id": f"{task.resolved_expiry_transition_id()}:direct-insert",
                },
            )
    with pytest.raises(Exception):  # noqa: B017 -- direct task-state mutation must be denied
        async with tenant_session_scope(lifecycle.tenant_id) as session:
            await session.execute(
                text("UPDATE handoff_tasks SET state = 'expired' WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )
    with pytest.raises(Exception):  # noqa: B017 -- direct deletion must not erase the TTL repair row
        async with tenant_session_scope(lifecycle.tenant_id) as session:
            await session.execute(
                text("DELETE FROM handoff_tasks WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )

    with pytest.raises(Exception, match="identity"):
        await lifecycle_repo.transition(
            lifecycle,
            LifecycleState.EXPIRED,
            f"wrong:{uuid4()}",
            {"task_id": task.task_id, "expiry_transition_id": f"wrong:{uuid4()}"},
        )
    assert lifecycle.state is LifecycleState.HANDOFF

    await _expire(lifecycle_repo, lifecycle, task)
    async with tenant_session_scope(lifecycle.tenant_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT task.state, queue.resolved_at
                    FROM handoff_tasks AS task
                    JOIN handoff_expiry_queue AS queue ON queue.task_id = task.task_id
                    WHERE task.task_id = :task_id
                    """
                ),
                {"task_id": task.task_id},
            )
        ).one()
    assert row.state == HandoffState.EXPIRED.value
    assert row.resolved_at is not None


async def test_expiry_uses_persisted_task_context_when_catalog_is_absent(db: None) -> None:
    """A due task still reaches one terminal expiry when its shared catalog row is unavailable."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    lifecycle, task = await _create_handoff_task(
        lifecycle_repo,
        handoff_repo,
        ttl_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    container = build_container(get_settings())

    result = await container.reconciliation.expire_handoff(
        lifecycle.tenant_id,
        lifecycle.canonical_event_id,
        lifecycle.workflow_id,
        task_id=task.task_id,
        expiry_transition_id=task.resolved_expiry_transition_id(),
    )

    assert result.status is HandoffExpiryStatus.EXPIRED
    async with tenant_session_scope(lifecycle.tenant_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT lifecycle.state, outbox.payload ->> 'event_summary' AS event_summary
                    FROM lifecycle
                    JOIN outbox
                      ON outbox.tenant_id = lifecycle.tenant_id
                     AND outbox.topic = 'lifecycle.expired'
                     AND outbox.payload ->> 'transition_id' = :transition_id
                    WHERE lifecycle.workflow_id = :workflow_id
                    """
                ),
                {
                    "workflow_id": lifecycle.workflow_id,
                    "transition_id": task.resolved_expiry_transition_id(),
                },
            )
        ).one()
    assert row.state == LifecycleState.EXPIRED.value
    assert row.event_summary == task.event_summary


async def test_direct_handoff_task_insert_enqueues_one_expiry_record(db: None) -> None:
    """The replay/direct task-creation path cannot leave an open task without its TTL queue row."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.HANDOFF,
        f"{workflow_id}:handoff-without-task",
        {"workflow_id": workflow_id},
    )
    task = _task_for(
        lifecycle,
        reason=HandoffReason.DEFERRED_REGISTER,
        ttl_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    await handoff_repo.create(task)
    await handoff_repo.create(task)
    async with system_session_scope() as session:
        queue_count = (
            (
                await session.execute(
                    text(
                        """
                    SELECT count(*) AS n
                    FROM handoff_expiry_queue
                    WHERE task_id = :task_id
                    """
                    ),
                    {"task_id": task.task_id},
                )
            )
            .one()
            .n
        )
    assert int(queue_count) == 1
    await _expire(lifecycle_repo, lifecycle, task)


async def test_registered_and_withdrawing_task_expiry_edges_and_supersession(db: None) -> None:
    """Calendar-recovery and withdrawal tasks expire safely without two active timer obligations."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()

    registered, calendar_task = await _create_registered_calendar_task(lifecycle_repo, handoff_repo)
    await _expire(lifecycle_repo, registered, calendar_task)
    assert registered.state is LifecycleState.EXPIRED

    withdrawing, stale_calendar_task = await _create_registered_calendar_task(
        lifecycle_repo, handoff_repo
    )
    await lifecycle_repo.transition(
        withdrawing,
        LifecycleState.WITHDRAWING,
        f"{withdrawing.workflow_id}:withdrawing",
        {"workflow_id": withdrawing.workflow_id, "event_summary": "Withdrawal expiry fixture"},
    )
    withdrawal_task = _task_for(
        withdrawing,
        reason=HandoffReason.WITHDRAWAL_REQUIRED,
        ttl_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await handoff_repo.create_withdrawal_handoff(
        withdrawal_task,
        {"task_id": withdrawal_task.task_id, "workflow_id": withdrawing.workflow_id},
    )

    async with tenant_session_scope(withdrawing.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT task.task_id, task.state, queue.resolved_at
                    FROM handoff_tasks AS task
                    JOIN handoff_expiry_queue AS queue ON queue.task_id = task.task_id
                    WHERE task.task_id IN (:stale_task_id, :withdrawal_task_id)
                    ORDER BY task.task_id
                    """
                ),
                {
                    "stale_task_id": stale_calendar_task.task_id,
                    "withdrawal_task_id": withdrawal_task.task_id,
                },
            )
        ).all()
    by_task_id = {row.task_id: row for row in rows}
    assert by_task_id[stale_calendar_task.task_id].state == HandoffState.CANCELLED.value
    assert by_task_id[stale_calendar_task.task_id].resolved_at is not None
    assert by_task_id[withdrawal_task.task_id].state == HandoffState.OPEN.value
    assert by_task_id[withdrawal_task.task_id].resolved_at is None

    await _expire(lifecycle_repo, withdrawing, withdrawal_task)
    assert withdrawing.state is LifecycleState.EXPIRED


async def test_stale_task_replay_cannot_supersede_its_newer_active_successor(db: None) -> None:
    """A delayed replay of A returns harmlessly after B supersedes it (NFR-8, ADR-007)."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    lifecycle, stale_task = await _create_registered_calendar_task(lifecycle_repo, handoff_repo)
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.WITHDRAWING,
        f"{lifecycle.workflow_id}:withdrawing",
        {"workflow_id": lifecycle.workflow_id, "event_summary": "Replay fence fixture"},
    )
    successor = _task_for(
        lifecycle,
        reason=HandoffReason.WITHDRAWAL_REQUIRED,
        ttl_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    await handoff_repo.create_withdrawal_handoff(
        successor,
        {"task_id": successor.task_id, "workflow_id": lifecycle.workflow_id},
    )

    # This represents a calendar-recovery activity whose database commit acknowledgement was lost
    # before a later withdrawal created its own active task. The stale replay must not cancel B.
    await handoff_repo.create(stale_task)

    tampered = HandoffTask(
        task_id=stale_task.task_id,
        tenant_id=stale_task.tenant_id,
        workflow_id=stale_task.workflow_id,
        canonical_event_id=stale_task.canonical_event_id,
        reason=stale_task.reason,
        deep_link=stale_task.deep_link,
        event_summary=stale_task.event_summary,
        ttl_expires_at=stale_task.ttl_expires_at,
        expiry_transition_id=f"tampered:{uuid4()}",
    )
    with pytest.raises(Exception, match="already bound"):
        await handoff_repo.create(tampered)

    async with tenant_session_scope(lifecycle.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT task.task_id, task.state, queue.resolved_at
                    FROM handoff_tasks AS task
                    JOIN handoff_expiry_queue AS queue ON queue.task_id = task.task_id
                    WHERE task.task_id IN (:stale_task_id, :successor_task_id)
                    """
                ),
                {"stale_task_id": stale_task.task_id, "successor_task_id": successor.task_id},
            )
        ).all()
    by_task_id = {row.task_id: row for row in rows}
    assert by_task_id[stale_task.task_id].state == HandoffState.CANCELLED.value
    assert by_task_id[stale_task.task_id].resolved_at is not None
    assert by_task_id[successor.task_id].state == HandoffState.OPEN.value
    assert by_task_id[successor.task_id].resolved_at is None


async def _create_handoff_task(
    lifecycle_repo: PostgresLifecycleRepository,
    handoff_repo: PostgresHandoffRepository,
    *,
    ttl_expires_at: datetime,
) -> tuple[Lifecycle, HandoffTask]:
    """Create one task with its coupled FOUND -> HANDOFF transition through the real guard."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    task = _task_for(
        lifecycle,
        reason=HandoffReason.DEFERRED_REGISTER,
        ttl_expires_at=ttl_expires_at,
    )
    await handoff_repo.create_and_transition(
        task,
        lifecycle,
        LifecycleState.HANDOFF,
        f"{workflow_id}:handoff",
        {"task_id": task.task_id, "workflow_id": workflow_id},
    )
    return lifecycle, task


async def _create_registered_calendar_task(
    lifecycle_repo: PostgresLifecycleRepository, handoff_repo: PostgresHandoffRepository
) -> tuple[Lifecycle, HandoffTask]:
    """Create the direct calendar-recovery path attached to a real registered lifecycle."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    await _seed_registration_source(tenant_id, canonical_event_id)
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:registered",
        {
            "workflow_id": workflow_id,
            "registration_source": Source.LUMA.value,
            "event_summary": "Calendar recovery expiry fixture",
        },
    )
    task = _task_for(
        lifecycle,
        reason=HandoffReason.CALENDAR_WRITE_FAILED,
        ttl_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await handoff_repo.create_calendar_recovery(
        task,
        {"task_id": task.task_id, "workflow_id": workflow_id},
    )
    return lifecycle, task


def _task_for(
    lifecycle: Lifecycle, *, reason: HandoffReason, ttl_expires_at: datetime
) -> HandoffTask:
    """Build a task with an explicit workflow-persistable expiry identity for this SQL fixture."""
    task_id = f"{lifecycle.workflow_id}:{reason.value}:{uuid4().hex}"
    return HandoffTask(
        task_id=task_id,
        tenant_id=lifecycle.tenant_id,
        workflow_id=lifecycle.workflow_id,
        canonical_event_id=lifecycle.canonical_event_id,
        reason=reason,
        deep_link="https://example.test/handoff",
        event_summary="Handoff expiry fixture",
        ttl_expires_at=ttl_expires_at,
        expiry_transition_id=f"{lifecycle.workflow_id}:expiry:{task_id}",
    )


async def _expire(
    lifecycle_repo: PostgresLifecycleRepository, lifecycle: Lifecycle, task: HandoffTask
) -> None:
    """Drive the normal durable-timer terminal path with the exact task-bound identity."""
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.EXPIRED,
        task.resolved_expiry_transition_id(),
        {
            "task_id": task.task_id,
            "expiry_transition_id": task.resolved_expiry_transition_id(),
        },
    )


async def _owner_execute(statement: str, parameters: dict[str, object]) -> int:
    """Apply one owner-only race fixture mutation without widening the application role's grants."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            result = await connection.execute(text(statement), parameters)
    finally:
        await owner.dispose()
    return int(getattr(result, "rowcount", 0))


async def _expire_owner_handoff_expiry_lease(task_id: str, lease_token: str) -> None:
    """Expire one exact lease without rotating it, modeling a paused orphan worker (ADR-007)."""
    updated = await _owner_execute(
        """
        UPDATE public.handoff_expiry_queue
        SET lease_expires_at = pg_catalog.clock_timestamp() - INTERVAL '1 second'
        WHERE task_id = :task_id
          AND lease_token = :lease_token
          AND resolved_at IS NULL
        """,
        {"task_id": task_id, "lease_token": lease_token},
    )
    assert updated == 1


async def _handoff_expiry_queue_lease_token(task_id: str) -> str:
    """Read the current opaque lease after a worker reaches its deliberately blocked liveness seam."""
    async with system_session_scope() as session:
        lease_token = (
            await session.execute(
                text(
                    """
                    SELECT lease_token
                    FROM handoff_expiry_queue
                    WHERE task_id = :task_id
                      AND resolved_at IS NULL
                    """
                ),
                {"task_id": task_id},
            )
        ).scalar_one()
    return str(lease_token)


async def _handoff_expiry_effect_snapshot(
    lifecycle: Lifecycle, task: HandoffTask
) -> _HandoffExpiryEffectSnapshot:
    """Read all durable state a stale orphan capability must leave unchanged (ADR-007)."""
    async with tenant_session_scope(lifecycle.tenant_id) as session:
        lifecycle_state = (
            await session.execute(
                text(
                    """
                    SELECT state
                    FROM lifecycle
                    WHERE lifecycle_id = :lifecycle_id
                      AND tenant_id = :tenant_id
                    """
                ),
                {"lifecycle_id": lifecycle.lifecycle_id, "tenant_id": lifecycle.tenant_id},
            )
        ).scalar_one()
        task_state = (
            await session.execute(
                text("SELECT state FROM handoff_tasks WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )
        ).scalar_one()
        transition_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM transition_ledger
                    WHERE tenant_id = :tenant_id
                      AND transition_id = :transition_id
                    """
                ),
                {
                    "tenant_id": lifecycle.tenant_id,
                    "transition_id": task.resolved_expiry_transition_id(),
                },
            )
        ).scalar_one()
        expired_outbox_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM outbox
                    WHERE tenant_id = :tenant_id
                      AND topic = 'lifecycle.expired'
                      AND payload ->> 'transition_id' = :transition_id
                    """
                ),
                {
                    "tenant_id": lifecycle.tenant_id,
                    "transition_id": task.resolved_expiry_transition_id(),
                },
            )
        ).scalar_one()
    async with system_session_scope() as session:
        queue = (
            await session.execute(
                text(
                    """
                    SELECT next_attempt_at, attempt_count, lease_token, lease_expires_at,
                           resolved_at, last_error
                    FROM handoff_expiry_queue
                    WHERE task_id = :task_id
                    """
                ),
                {"task_id": task.task_id},
            )
        ).one()
    return _HandoffExpiryEffectSnapshot(
        lifecycle_state=str(lifecycle_state),
        task_state=str(task_state),
        queue_next_attempt_at=queue.next_attempt_at,
        queue_attempt_count=int(queue.attempt_count),
        queue_lease_token=queue.lease_token,
        queue_lease_expires_at=queue.lease_expires_at,
        queue_resolved_at=queue.resolved_at,
        queue_last_error=queue.last_error,
        transition_count=int(transition_count),
        expired_outbox_count=int(expired_outbox_count),
    )


async def _expired_outbox_payload(lifecycle: Lifecycle, task: HandoffTask) -> object:
    """Read the one terminal payload to prove private lease control data never crosses the outbox."""
    async with tenant_session_scope(lifecycle.tenant_id) as session:
        return (
            await session.execute(
                text(
                    """
                    SELECT payload
                    FROM outbox
                    WHERE tenant_id = :tenant_id
                      AND topic = 'lifecycle.expired'
                      AND payload ->> 'transition_id' = :transition_id
                    """
                ),
                {
                    "tenant_id": lifecycle.tenant_id,
                    "transition_id": task.resolved_expiry_transition_id(),
                },
            )
        ).scalar_one()


async def _seed_registration_source(tenant_id: UUID, canonical_event_id: UUID) -> None:
    """Seed the linked Luma source that 0049 requires before REGISTERED (ADR-008)."""
    tag = f"handoff-expiry-{tenant_id}"
    await PostgresTenantRepository().add(
        Tenant(tenant_id, tag, f"{tag}@example.test", f"{tag}@u.example.test")
    )
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
                "title": "Handoff expiry source fixture",
                "start_at": datetime(2099, 1, 1, tzinfo=UTC),
                "description": "linked source fixture",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO event_source_links
                    (source, source_event_id, canonical_event_id, registration_url, price_status)
                VALUES (:source, :source_event_id, :canonical_event_id, :registration_url, 'free')
                """
            ),
            {
                "source": Source.LUMA.value,
                "source_event_id": f"handoff-expiry-{canonical_event_id}",
                "canonical_event_id": canonical_event_id,
                "registration_url": f"https://luma.test/{canonical_event_id}",
            },
        )
