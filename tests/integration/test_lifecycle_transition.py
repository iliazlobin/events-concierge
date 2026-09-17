"""ADR-007 guard tests for lifecycle, ledger, task, and outbox consistency under retries."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.mock.notification_secrets import (
    DevelopmentNotificationSecretProtector,
)
from events_concierge.adapters.postgres.tenant_repos import (
    PostgresHandoffRepository,
    PostgresLifecycleRepository,
    PostgresTenantRepository,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import HandoffReason, HandoffState, LifecycleState, Source
from events_concierge.domain.ids import registration_workflow_id
from events_concierge.domain.lifecycle import HandoffTask, Lifecycle
from events_concierge.infra.db import system_session_scope, tenant_session_scope

pytestmark = pytest.mark.integration


async def test_guarded_transition_concurrent_retry_converges_to_one_ledger_and_outbox(
    db: None,
) -> None:
    """Concurrent activity retries share one transition reservation and never raise (ADR-007, NFR-8)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    transition_id = f"{workflow_id}:handoff:1"
    lifecycle_repo = PostgresLifecycleRepository()
    first = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    second = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    barrier = asyncio.Barrier(2)

    async def transition_once(lifecycle: Lifecycle) -> None:
        await barrier.wait()
        await lifecycle_repo.transition(
            lifecycle,
            LifecycleState.HANDOFF,
            transition_id,
            {"workflow_id": workflow_id, "event_summary": "Concurrent retry fixture"},
        )

    await asyncio.gather(transition_once(first), transition_once(second))

    async with tenant_session_scope(tenant_id) as session:
        lifecycle_state = (
            (
                await session.execute(
                    text("SELECT state FROM lifecycle WHERE workflow_id = :workflow_id"),
                    {"workflow_id": workflow_id},
                )
            )
            .one()
            .state
        )
        ledger_count = (
            (
                await session.execute(
                    text(
                        """SELECT count(*) AS n FROM transition_ledger
                       WHERE transition_id = :transition_id"""
                    ),
                    {"transition_id": transition_id},
                )
            )
            .one()
            .n
        )
        outbox_count = (
            (
                await session.execute(
                    text(
                        """SELECT count(*) AS n FROM outbox
                       WHERE payload ->> 'transition_id' = :transition_id"""
                    ),
                    {"transition_id": transition_id},
                )
            )
            .one()
            .n
        )

    assert first.state is LifecycleState.HANDOFF
    assert second.state is LifecycleState.HANDOFF
    assert lifecycle_state == LifecycleState.HANDOFF.value
    assert int(ledger_count) == 1
    assert int(outbox_count) == 1


async def test_handoff_task_commits_with_its_guarded_lifecycle_transition(db: None) -> None:
    """A handoff task, state change, ledger row, and outbox row become visible together (ADR-007)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    transition_id = f"{workflow_id}:handoff:1"
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    task = HandoffTask(
        task_id=f"handoff-task-{uuid4().hex}",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.NO_AUTONOMOUS_LANE,
        deep_link="https://example.test/handoff",
        event_summary="Atomic handoff fixture",
        ttl_expires_at=datetime.now(UTC) + timedelta(days=1),
        state=HandoffState.OPEN,
    )

    await handoff_repo.create_and_transition(
        task,
        lifecycle,
        LifecycleState.HANDOFF,
        transition_id,
        {
            "task_id": task.task_id,
            "reason": task.reason.value,
            "workflow_id": workflow_id,
            "event_summary": task.event_summary,
            "deep_link": task.deep_link,
        },
    )

    async with tenant_session_scope(tenant_id) as session:
        task_count = (
            (
                await session.execute(
                    text("SELECT count(*) AS n FROM handoff_tasks WHERE task_id = :task_id"),
                    {"task_id": task.task_id},
                )
            )
            .one()
            .n
        )
        transition_count = (
            (
                await session.execute(
                    text(
                        """SELECT count(*) AS n FROM transition_ledger
                       WHERE transition_id = :transition_id"""
                    ),
                    {"transition_id": transition_id},
                )
            )
            .one()
            .n
        )
        outbox_count = (
            (
                await session.execute(
                    text(
                        """SELECT count(*) AS n FROM outbox
                       WHERE payload ->> 'transition_id' = :transition_id"""
                    ),
                    {"transition_id": transition_id},
                )
            )
            .one()
            .n
        )

    assert lifecycle.state is LifecycleState.HANDOFF
    assert int(task_count) == 1
    assert int(transition_count) == 1
    assert int(outbox_count) == 1


async def test_verified_handoff_completion_atomically_consumes_capability_and_task(
    db: None,
) -> None:
    """A verified retry converges on one receipt/transition and retires the expiry instruction."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    token = secrets.token_urlsafe(32)
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    await _seed_registered_source(tenant_id, canonical_event_id, Source.MEETUP)
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    task = HandoffTask(
        task_id=f"{workflow_id}:handoff",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link=f"https://meetup.test/{canonical_event_id}",
        event_summary="Verified completion fixture",
        ttl_expires_at=datetime.now(UTC) + timedelta(days=1),
        state=HandoffState.OPEN,
        completion_token=token,
    )
    await handoff_repo.create_and_transition(
        task,
        lifecycle,
        LifecycleState.HANDOFF,
        f"{workflow_id}:handoff:1",
        {
            "task_id": task.task_id,
            "workflow_id": workflow_id,
            "event_summary": task.event_summary,
            "deep_link": task.deep_link,
            "protected_completion_url": (
                await DevelopmentNotificationSecretProtector().protect_completion_url(
                    tenant_id,
                    f"/v1/tasks/{token}/done",
                )
            ),
        },
    )
    target = await handoff_repo.resolve_completion_token(token)
    assert target is not None
    assert target.status == "active"

    completion_id = f"{workflow_id}:handoff-completion:{task.task_id}:1"
    replay_lifecycle = await lifecycle_repo.get_or_create(
        tenant_id,
        canonical_event_id,
        workflow_id,
    )
    replay_task = await handoff_repo.get(tenant_id, task.task_id)
    assert replay_task is not None
    barrier = asyncio.Barrier(2)

    async def complete_once(
        candidate_task: HandoffTask,
        candidate_lifecycle: Lifecycle,
    ) -> bool:
        await barrier.wait()
        return await handoff_repo.complete_verified(
            candidate_task,
            candidate_lifecycle,
            transition_id=f"{workflow_id}:registered:1",
            completion_id=completion_id,
            registration_source=Source.MEETUP,
            conflict_warning=True,
            outbox_payload={
                "workflow_id": workflow_id,
                "event_summary": task.event_summary,
                "registration_source": Source.MEETUP.value,
            },
        )

    first, replay = await asyncio.gather(
        complete_once(task, lifecycle),
        complete_once(replay_task, replay_lifecycle),
    )

    resolved = await handoff_repo.resolve_completion_token(token)
    receipt = await handoff_repo.get_completion_attempt(
        tenant_id,
        task.task_id,
        completion_id,
    )
    attacker_tenant_id = uuid4()
    cross_tenant_receipt = await handoff_repo.get_completion_attempt(
        attacker_tenant_id,
        task.task_id,
        completion_id,
    )
    async with tenant_session_scope(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT lifecycle.state,
                           lifecycle.conflict_warning,
                           task.state AS task_state,
                           attempt.outcome,
                           expiry.resolved_at
                    FROM lifecycle
                    JOIN handoff_tasks AS task
                      ON task.workflow_id = lifecycle.workflow_id
                    JOIN handoff_completion_attempts AS attempt
                      ON attempt.task_id = task.task_id
                    JOIN handoff_expiry_queue AS expiry
                      ON expiry.task_id = task.task_id
                    WHERE lifecycle.workflow_id = :workflow_id
                    """
                ),
                {"workflow_id": workflow_id},
            )
        ).one()

    assert sorted((first, replay)) == [False, True]
    assert lifecycle.state is LifecycleState.REGISTERED
    assert task.state is HandoffState.COMPLETED
    assert resolved is not None and resolved.status == "used"
    assert receipt is not None
    assert receipt.outcome.value == "verified"
    assert receipt.registration_source is Source.MEETUP
    assert receipt.conflict_warning is True
    assert cross_tenant_receipt is None
    assert row.state == LifecycleState.REGISTERED.value
    assert row.conflict_warning is True
    assert row.task_state == HandoffState.COMPLETED.value
    assert row.outcome == "verified"
    assert row.resolved_at is not None

    # SECURITY DEFINER functions bypass FORCE RLS. The tenant predicate must therefore
    # be explicit even on the idempotent-replay path; otherwise an exact victim receipt
    # acts as a cross-tenant existence oracle.
    with pytest.raises(DBAPIError) as denied:
        async with tenant_session_scope(attacker_tenant_id) as session:
            await session.execute(
                text(
                    """
                    SELECT public.fn_complete_verified_handoff(
                        :task_id,
                        :lifecycle_id,
                        :transition_id,
                        :completion_id,
                        :registration_source,
                        :conflict_warning,
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "task_id": task.task_id,
                    "lifecycle_id": lifecycle.lifecycle_id,
                    "transition_id": f"{workflow_id}:registered:1",
                    "completion_id": completion_id,
                    "registration_source": Source.MEETUP.value,
                    "conflict_warning": True,
                    "payload": json.dumps(
                        {
                            "workflow_id": workflow_id,
                            "event_summary": task.event_summary,
                            "registration_source": Source.MEETUP.value,
                        }
                    ),
                },
            )
    assert getattr(denied.value.orig, "sqlstate", None) == "22023"


async def test_due_handoff_expiry_commits_one_terminal_effect_and_replays_safely(db: None) -> None:
    """A task-bound ADR-007 expiry makes one ledger/outbox effect even after an ACK-loss retry."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    task = HandoffTask(
        task_id=f"handoff-expiry-{uuid4().hex}",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link="https://example.test/expiry",
        event_summary="Expired handoff fixture",
        ttl_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        expiry_transition_id=f"{workflow_id}:handoff-expired:1",
    )
    await handoff_repo.create_and_transition(
        task,
        lifecycle,
        LifecycleState.HANDOFF,
        f"{workflow_id}:handoff:1",
        {
            "task_id": task.task_id,
            "reason": task.reason.value,
            "workflow_id": workflow_id,
            "event_summary": task.event_summary,
            "deep_link": task.deep_link,
        },
    )
    payload: dict[str, object] = {
        "task_id": task.task_id,
        "expiry_transition_id": task.resolved_expiry_transition_id(),
        "workflow_id": workflow_id,
        "event_summary": task.event_summary,
        "expiry_reason": "handoff_ttl",
    }
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.EXPIRED,
        task.resolved_expiry_transition_id(),
        payload,
    )

    # A retry after the guarded commit but before an activity acknowledgement must be a no-op,
    # despite replaying the former from-state and complete task identity.
    async with tenant_session_scope(tenant_id) as session:
        replayed = (
            await session.execute(
                text(
                    """SELECT public.fn_transition(
                           :lifecycle_id, 'handoff', 'expired', :transition_id,
                           NULL, false, CAST(:payload AS jsonb), NULL
                       ) AS applied"""
                ),
                {
                    "lifecycle_id": lifecycle.lifecycle_id,
                    "transition_id": task.resolved_expiry_transition_id(),
                    "payload": json.dumps(payload),
                },
            )
        ).scalar_one()
        task_state = (
            await session.execute(
                text("SELECT state FROM handoff_tasks WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )
        ).scalar_one()
        queue_resolved = (
            await session.execute(
                text(
                    """SELECT resolved_at IS NOT NULL FROM handoff_expiry_queue
                       WHERE task_id = :task_id"""
                ),
                {"task_id": task.task_id},
            )
        ).scalar_one()
        ledger_count = (
            await session.execute(
                text(
                    """SELECT count(*) FROM transition_ledger
                       WHERE transition_id = :transition_id"""
                ),
                {"transition_id": task.resolved_expiry_transition_id()},
            )
        ).scalar_one()
        outbox_count = (
            await session.execute(
                text(
                    """SELECT count(*) FROM outbox
                       WHERE tenant_id = :tenant_id AND topic = 'lifecycle.expired'
                         AND payload ->> 'transition_id' = :transition_id"""
                ),
                {
                    "tenant_id": tenant_id,
                    "transition_id": task.resolved_expiry_transition_id(),
                },
            )
        ).scalar_one()

    assert replayed is False
    assert lifecycle.state is LifecycleState.EXPIRED
    assert task_state == HandoffState.EXPIRED.value
    assert queue_resolved is True
    assert int(ledger_count) == 1
    assert int(outbox_count) == 1


async def test_handoff_expiry_cannot_run_before_its_persisted_task_deadline(db: None) -> None:
    """The database refuses an early Temporal/sweeper expiry, rather than trusting caller time (ADR-007)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    task = HandoffTask(
        task_id=f"handoff-not-due-{uuid4().hex}",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link="https://example.test/not-due",
        event_summary="Not due handoff fixture",
        ttl_expires_at=datetime.now(UTC) + timedelta(hours=1),
        expiry_transition_id=f"{workflow_id}:handoff-expired:1",
    )
    await handoff_repo.create_and_transition(
        task,
        lifecycle,
        LifecycleState.HANDOFF,
        f"{workflow_id}:handoff:1",
        {
            "task_id": task.task_id,
            "reason": task.reason.value,
            "workflow_id": workflow_id,
            "event_summary": task.event_summary,
            "deep_link": task.deep_link,
        },
    )

    with pytest.raises(Exception, match="handoff task expiry is not due"):
        await lifecycle_repo.transition(
            lifecycle,
            LifecycleState.EXPIRED,
            task.resolved_expiry_transition_id(),
            {
                "task_id": task.task_id,
                "expiry_transition_id": task.resolved_expiry_transition_id(),
                "workflow_id": workflow_id,
                "event_summary": task.event_summary,
                "expiry_reason": "handoff_ttl",
            },
        )

    async with tenant_session_scope(tenant_id) as session:
        ledger_count = (
            await session.execute(
                text(
                    """SELECT count(*) FROM transition_ledger
                       WHERE transition_id = :transition_id"""
                ),
                {"transition_id": task.resolved_expiry_transition_id()},
            )
        ).scalar_one()
    assert lifecycle.state is LifecycleState.HANDOFF
    assert int(ledger_count) == 0


@pytest.mark.parametrize(
    "target_state",
    [LifecycleState.SCHEDULED, LifecycleState.RECONCILED],
)
async def test_calendar_recovery_task_is_cancelled_when_factual_scheduling_resumes(
    db: None,
    target_state: LifecycleState,
) -> None:
    """A successful write/reschedule retires only its obsolete calendar task (ADR-007/008)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    await _seed_registered_source(tenant_id, canonical_event_id, Source.LUMA)
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:registered",
        {
            "workflow_id": workflow_id,
            "event_summary": "Calendar recovery resolution fixture",
            "registration_source": Source.LUMA.value,
        },
    )
    task = HandoffTask(
        task_id=f"{workflow_id}:calendar-recovery:{target_state.value}",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.CALENDAR_WRITE_FAILED,
        deep_link="https://example.test/calendar-recovery",
        event_summary="Calendar recovery resolution fixture",
        ttl_expires_at=datetime.now(UTC) + timedelta(days=1),
        state=HandoffState.OPEN,
    )
    await handoff_repo.create_calendar_recovery(
        task,
        {
            "task_id": task.task_id,
            "workflow_id": workflow_id,
            "canonical_event_id": str(canonical_event_id),
            "reason": task.reason.value,
            "event_summary": task.event_summary,
            "deep_link": task.deep_link,
        },
    )

    await lifecycle_repo.transition(
        lifecycle,
        target_state,
        f"{workflow_id}:{target_state.value}",
        {"workflow_id": workflow_id, "event_summary": task.event_summary},
    )

    async with tenant_session_scope(tenant_id) as session:
        task_state = (
            await session.execute(
                text("SELECT state FROM handoff_tasks WHERE task_id = :task_id"),
                {"task_id": task.task_id},
            )
        ).scalar_one()
        queue_resolved = (
            await session.execute(
                text(
                    """SELECT resolved_at IS NOT NULL FROM handoff_expiry_queue
                       WHERE task_id = :task_id"""
                ),
                {"task_id": task.task_id},
            )
        ).scalar_one()

    assert lifecycle.state is target_state
    assert task_state == HandoffState.CANCELLED.value
    assert queue_resolved is True


async def test_application_role_cannot_bypass_the_guarded_lifecycle_state_machine(db: None) -> None:
    """The non-owner app role has no direct lifecycle UPDATE path around fn_transition (ADR-007)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle_repo = PostgresLifecycleRepository()
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)

    with pytest.raises(Exception):  # noqa: B017 -- PostgreSQL permission denial is the contract
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """UPDATE lifecycle SET state = 'handoff'
                       WHERE lifecycle_id = :lifecycle_id"""
                ),
                {"lifecycle_id": lifecycle.lifecycle_id},
            )

    refreshed = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    assert refreshed.state is LifecycleState.FOUND


async def test_lifecycle_repository_rejects_a_non_deterministic_workflow_identity(db: None) -> None:
    """A caller cannot create a second active lifecycle by choosing a different workflow id (ADR-003)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()

    with pytest.raises(ValueError, match="deterministic tenant:event"):
        await PostgresLifecycleRepository().get_or_create(
            tenant_id, canonical_event_id, f"arbitrary:{uuid4()}"
        )


async def test_registered_transition_requires_the_actual_linked_registration_source(
    db: None,
) -> None:
    """A lifecycle cannot become watchable-but-uncovered through a malformed payload (ADR-008)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    await _seed_registered_source(tenant_id, canonical_event_id, Source.LUMA)
    lifecycle_repo = PostgresLifecycleRepository()
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)

    with pytest.raises(Exception, match="registration_source"):
        await lifecycle_repo.transition(
            lifecycle,
            LifecycleState.REGISTERED,
            f"{workflow_id}:registered",
            {"workflow_id": workflow_id, "event_summary": "Missing source fixture"},
        )

    assert lifecycle.state is LifecycleState.FOUND


async def test_reconciled_lifecycle_accepts_later_change_then_organizer_cancellation(
    db: None,
) -> None:
    """P3 keeps a watched reconcile active for a second update/cancel (FR-8.7, ADR-008)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle_repo = PostgresLifecycleRepository()
    await _seed_registered_source(tenant_id, canonical_event_id, Source.LUMA)
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)

    for transition_id, state in (
        (f"{workflow_id}:registered", LifecycleState.REGISTERED),
        (f"{workflow_id}:scheduled", LifecycleState.SCHEDULED),
        (f"{workflow_id}:reconcile-v1", LifecycleState.RECONCILED),
        (f"{workflow_id}:reconcile-v2", LifecycleState.RECONCILED),
        (f"{workflow_id}:cancel", LifecycleState.CANCELLED),
    ):
        payload: dict[str, object] = {
            "workflow_id": workflow_id,
            "event_summary": "Reconcile lifecycle fixture",
        }
        if state is LifecycleState.REGISTERED:
            payload["registration_source"] = Source.LUMA.value
        await lifecycle_repo.transition(
            lifecycle,
            state,
            transition_id,
            payload,
        )

    assert lifecycle.state is LifecycleState.CANCELLED
    assert await lifecycle_repo.find_active(tenant_id, canonical_event_id) is None
    async with tenant_session_scope(tenant_id) as session:
        topics = (
            await session.execute(
                text(
                    """SELECT topic, count(*) AS n FROM outbox
                       WHERE tenant_id = :tenant_id
                       GROUP BY topic"""
                ),
                {"tenant_id": tenant_id},
            )
        ).all()
    counts = {str(row.topic): int(row.n) for row in topics}
    assert counts["lifecycle.reconciled"] == 2
    assert counts["lifecycle.cancelled"] == 1


async def test_reconcile_repair_reentry_emits_one_organizer_change_notification(db: None) -> None:
    """A lost fanout acknowledgement cannot duplicate the lifecycle notification (ADR-007/008)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    fingerprint = f"reschedule:{canonical_event_id}:fixture"
    lifecycle_repo = PostgresLifecycleRepository()
    await _seed_registered_source(tenant_id, canonical_event_id, Source.LUMA)
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:registered",
        {
            "workflow_id": workflow_id,
            "event_summary": "Repair reentry fixture",
            "registration_source": Source.LUMA.value,
        },
    )
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.SCHEDULED,
        f"{workflow_id}:scheduled",
        {"workflow_id": workflow_id, "event_summary": "Repair reentry fixture"},
    )
    for attempt in ("normal", "repair"):
        await lifecycle_repo.transition(
            lifecycle,
            LifecycleState.RECONCILED,
            f"{workflow_id}:reconcile:{attempt}",
            {
                "workflow_id": workflow_id,
                "event_summary": "Repair reentry fixture",
                "organizer_change_fingerprint": fingerprint,
            },
        )

    async with tenant_session_scope(tenant_id) as session:
        notification_count = (
            (
                await session.execute(
                    text(
                        """
                    SELECT count(*) AS n
                    FROM outbox
                    WHERE tenant_id = :tenant_id
                      AND topic = 'lifecycle.reconciled'
                      AND payload ->> 'organizer_change_fingerprint' = :fingerprint
                    """
                    ),
                    {"tenant_id": tenant_id, "fingerprint": fingerprint},
                )
            )
            .one()
            .n
        )
    ledger_count = await _organizer_change_ledger_count(tenant_id, workflow_id, fingerprint)
    assert lifecycle.state is LifecycleState.RECONCILED
    assert int(notification_count) == 1
    assert int(ledger_count) == 1


async def test_active_lookup_and_withdrawal_handoff_are_tenant_scoped_and_atomic(db: None) -> None:
    """The API lookup cannot mint/leak a row; manual withdrawal keeps state truthful (FR-8.8)."""
    tenant_id, other_tenant_id, canonical_event_id = uuid4(), uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    await _seed_registered_source(tenant_id, canonical_event_id, Source.MEETUP)
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:registered",
        {
            "workflow_id": workflow_id,
            "event_summary": "Withdrawal handoff fixture",
            "registration_source": Source.MEETUP.value,
        },
    )
    await lifecycle_repo.transition(
        lifecycle,
        LifecycleState.WITHDRAWING,
        f"{workflow_id}:withdrawing",
        {"workflow_id": workflow_id, "event_summary": "Withdrawal handoff fixture"},
    )
    task = HandoffTask(
        task_id=f"{workflow_id}:task",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.WITHDRAWAL_REQUIRED,
        deep_link="https://example.test/withdraw",
        event_summary="Withdrawal handoff fixture",
        ttl_expires_at=datetime.now(UTC) + timedelta(days=1),
        state=HandoffState.OPEN,
    )
    payload: dict[str, object] = {
        "task_id": task.task_id,
        "workflow_id": workflow_id,
        "event_summary": task.event_summary,
    }

    assert (await lifecycle_repo.find_active(tenant_id, canonical_event_id)) is not None
    assert await lifecycle_repo.find_active(other_tenant_id, canonical_event_id) is None
    await handoff_repo.create_withdrawal_handoff(task, payload)
    await handoff_repo.create_withdrawal_handoff(task, payload)

    async with tenant_session_scope(tenant_id) as session:
        task_count = (
            (
                await session.execute(
                    text("SELECT count(*) AS n FROM handoff_tasks WHERE task_id = :task_id"),
                    {"task_id": task.task_id},
                )
            )
            .one()
            .n
        )
        outbox_count = (
            (
                await session.execute(
                    text(
                        """
                    SELECT count(*) AS n
                    FROM outbox
                    WHERE topic = 'withdrawal_handoff_required'
                      AND payload ->> 'task_id' = :task_id
                    """
                    ),
                    {"task_id": task.task_id},
                )
            )
            .one()
            .n
        )
        lifecycle_state = (
            (
                await session.execute(
                    text("SELECT state FROM lifecycle WHERE workflow_id = :workflow_id"),
                    {"workflow_id": workflow_id},
                )
            )
            .one()
            .state
        )

    assert int(task_count) == 1
    assert int(outbox_count) == 1
    assert lifecycle_state == LifecycleState.WITHDRAWING.value
    retrieved = await handoff_repo.get(tenant_id, task.task_id)
    assert retrieved is not None
    assert retrieved.task_id == task.task_id
    assert await handoff_repo.get(other_tenant_id, task.task_id) is None


async def _organizer_change_ledger_count(
    tenant_id: UUID, workflow_id: str, fingerprint: str
) -> int:
    """Inspect the opaque cross-tenant dedup ledger through the migration-owner test connection."""
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
                        FROM public.lifecycle_organizer_change_ledger
                        WHERE tenant_id = :tenant_id
                          AND workflow_id = :workflow_id
                          AND fingerprint = :fingerprint
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "workflow_id": workflow_id,
                        "fingerprint": fingerprint,
                    },
                )
            ).scalar_one()
    finally:
        await owner_engine.dispose()
    return int(count)


async def _seed_registered_source(
    tenant_id: UUID, canonical_event_id: UUID, source: Source
) -> None:
    """Seed the public source link a guarded REGISTERED transition must name (ADR-008)."""
    tag = f"lifecycle-source-{tenant_id}"
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
                "title": "Guarded registration source fixture",
                "start_at": datetime(2099, 1, 1, tzinfo=UTC),
                "description": "source-bound lifecycle fixture",
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
                "source": source.value,
                "source_event_id": f"guarded-{source.value}-{canonical_event_id}",
                "canonical_event_id": canonical_event_id,
                "registration_url": f"https://{source.value}.test/{canonical_event_id}",
            },
        )
