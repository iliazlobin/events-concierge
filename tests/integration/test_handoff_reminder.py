"""PostgreSQL handoff-reminder guard tests (FR-6.6, ADR-007/009)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.postgres.tenant_repos import (
    PostgresHandoffRepository,
    PostgresLifecycleRepository,
)
from events_concierge.domain.enums import (
    HandoffReason,
    HandoffReminderKind,
    HandoffReminderStatus,
    LifecycleState,
)
from events_concierge.domain.ids import registration_workflow_id
from events_concierge.domain.lifecycle import HandoffTask, Lifecycle
from events_concierge.infra.db import system_session_scope, tenant_session_scope

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("reminder_kind", "created_age"),
    [
        (HandoffReminderKind.T24H, timedelta(hours=25)),
        (HandoffReminderKind.T5D, timedelta(days=6)),
    ],
)
async def test_due_reminder_replay_creates_one_ledger_and_outbox_effect(
    db: None, reminder_kind: HandoffReminderKind, created_age: timedelta
) -> None:
    """Each cadence uses persisted task creation time and replays exactly once (FR-6.6)."""
    lifecycle, task = await _create_handoff_task(
        ttl_expires_at=datetime.now(UTC) + timedelta(days=2)
    )
    repository = PostgresHandoffRepository()
    persisted = await repository.get(lifecycle.tenant_id, task.task_id)
    assert persisted is not None
    assert persisted.created_at is not None

    await _set_task_created_at(task.task_id, datetime.now(UTC) - created_age)
    reminder_id = f"{lifecycle.workflow_id}:handoff-reminder:{reminder_kind.value}:{uuid4().hex}"

    first = await repository.enqueue_reminder(
        lifecycle.tenant_id,
        task.task_id,
        lifecycle.workflow_id,
        lifecycle.canonical_event_id,
        task.resolved_expiry_transition_id(),
        reminder_kind,
        reminder_id,
    )
    replay = await repository.enqueue_reminder(
        lifecycle.tenant_id,
        task.task_id,
        lifecycle.workflow_id,
        lifecycle.canonical_event_id,
        task.resolved_expiry_transition_id(),
        reminder_kind,
        reminder_id,
    )

    assert first.status is HandoffReminderStatus.ENQUEUED
    assert replay.status is HandoffReminderStatus.ALREADY_ENQUEUED
    assert await _ledger_count(task.task_id, reminder_kind) == 1
    async with system_session_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS count,
                           min(payload ->> 'task_id') AS task_id,
                           min(payload ->> 'reminder_kind') AS reminder_kind,
                           min(payload ->> 'expiry_transition_id') AS expiry_transition_id
                    FROM outbox
                    WHERE topic = 'handoff.reminder'
                      AND payload ->> 'reminder_id' = :reminder_id
                    """
                ),
                {"reminder_id": reminder_id},
            )
        ).one()
    assert int(row.count) == 1
    assert row.task_id == task.task_id
    assert row.reminder_kind == reminder_kind.value
    assert row.expiry_transition_id == task.resolved_expiry_transition_id()

    # The ledger is not an app-role side channel around the guarded task/lifecycle checks.
    with pytest.raises(Exception, match="permission denied"):
        async with tenant_session_scope(lifecycle.tenant_id) as session:
            await session.execute(
                text("SELECT reminder_id FROM handoff_reminder_ledger WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )


@pytest.mark.parametrize("reminder_kind", list(HandoffReminderKind))
async def test_reminder_is_not_enqueued_before_persisted_due_time(
    db: None, reminder_kind: HandoffReminderKind
) -> None:
    """A fresh task produces no ledger or outbox row before either durable cadence is due."""
    lifecycle, task = await _create_handoff_task(
        ttl_expires_at=datetime.now(UTC) + timedelta(days=8)
    )
    reminder_id = f"{lifecycle.workflow_id}:early-reminder:{reminder_kind.value}:{uuid4().hex}"

    result = await PostgresHandoffRepository().enqueue_reminder(
        lifecycle.tenant_id,
        task.task_id,
        lifecycle.workflow_id,
        lifecycle.canonical_event_id,
        task.resolved_expiry_transition_id(),
        reminder_kind,
        reminder_id,
    )

    assert result.status is HandoffReminderStatus.NOT_DUE
    assert await _ledger_count(task.task_id, reminder_kind) == 0
    assert await _reminder_outbox_count(reminder_id) == 0


async def test_reminder_refuses_a_terminalized_handoff_and_an_empty_tenant_guc(db: None) -> None:
    """Terminal state and missing tenant context both fail closed without a visible reminder."""
    lifecycle, task = await _create_handoff_task(
        ttl_expires_at=datetime.now(UTC) + timedelta(days=2)
    )
    reminder_id = f"{lifecycle.workflow_id}:inactive-reminder:{uuid4().hex}"
    lifecycle_repo = PostgresLifecycleRepository()
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.CANCELLED,
        f"{lifecycle.workflow_id}:cancel-handoff:{task.task_id}",
        {"task_id": task.task_id, "workflow_id": lifecycle.workflow_id},
    )

    result = await PostgresHandoffRepository().enqueue_reminder(
        lifecycle.tenant_id,
        task.task_id,
        lifecycle.workflow_id,
        lifecycle.canonical_event_id,
        task.resolved_expiry_transition_id(),
        HandoffReminderKind.T24H,
        reminder_id,
    )

    assert result.status is HandoffReminderStatus.INACTIVE
    assert await _ledger_count(task.task_id, HandoffReminderKind.T24H) == 0
    assert await _reminder_outbox_count(reminder_id) == 0
    with pytest.raises(Exception, match="tenant context"):
        async with tenant_session_scope(None) as session:
            await session.execute(
                text(
                    """
                    SELECT public.fn_enqueue_handoff_reminder(
                        :task_id,
                        :workflow_id,
                        :canonical_event_id,
                        :expiry_transition_id,
                        :reminder_kind,
                        :reminder_id
                    )
                    """
                ),
                {
                    "task_id": task.task_id,
                    "workflow_id": lifecycle.workflow_id,
                    "canonical_event_id": lifecycle.canonical_event_id,
                    "expiry_transition_id": task.resolved_expiry_transition_id(),
                    "reminder_kind": HandoffReminderKind.T24H.value,
                    "reminder_id": f"{reminder_id}:no-context",
                },
            )


async def _create_handoff_task(*, ttl_expires_at: datetime) -> tuple[Lifecycle, HandoffTask]:
    """Create a real open task coupled to its matching active lifecycle through the SQL guard."""
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    task_id = f"{workflow_id}:reminder:{uuid4().hex}"
    task = HandoffTask(
        task_id=task_id,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link="https://example.test/handoff-reminder",
        event_summary="Handoff reminder fixture",
        ttl_expires_at=ttl_expires_at,
        expiry_transition_id=f"{workflow_id}:handoff-expiry:{task_id}",
    )
    await handoff_repo.create_and_transition(
        task,
        lifecycle,
        LifecycleState.HANDOFF,
        f"{workflow_id}:handoff:{task_id}",
        {"task_id": task_id, "workflow_id": workflow_id},
    )
    return lifecycle, task


async def _set_task_created_at(task_id: str, created_at: datetime) -> None:
    """Use the migration-owner connection only to create a deterministic elapsed-time fixture."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.handoff_tasks
                    SET created_at = :created_at
                    WHERE task_id = :task_id
                    """
                ),
                {"task_id": task_id, "created_at": created_at},
            )
    finally:
        await owner_engine.dispose()


async def _ledger_count(task_id: str, reminder_kind: HandoffReminderKind) -> int:
    """Inspect the private ledger through the owner-only test fixture connection."""
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
                        FROM public.handoff_reminder_ledger
                        WHERE task_id = :task_id
                          AND reminder_kind = :reminder_kind
                        """
                    ),
                    {"task_id": task_id, "reminder_kind": reminder_kind.value},
                )
            ).scalar_one()
    finally:
        await owner_engine.dispose()
    return int(count)


async def _reminder_outbox_count(reminder_id: str) -> int:
    """Count exactly the outbox projection addressed by one once-minted reminder identity."""
    async with system_session_scope() as session:
        count = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM outbox
                    WHERE topic = 'handoff.reminder'
                      AND payload ->> 'reminder_id' = :reminder_id
                    """
                ),
                {"reminder_id": reminder_id},
            )
        ).scalar_one()
    return int(count)
