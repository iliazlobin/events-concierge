"""Adversarial app-role isolation checks for guarded handoff and request-terminal data.

These cases exercise the non-superuser integration connection, including the empty-string GUC
state that ``NULLIF`` must turn into a fail-closed RLS context (FR-1.3/1.4, ADR-007).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from events_concierge.adapters.postgres.tenant_repos import (
    PostgresHandoffRepository,
    PostgresLifecycleRepository,
    PostgresRequestRepository,
    PostgresTenantRepository,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import HandoffReason, LifecycleState
from events_concierge.domain.ids import registration_workflow_id
from events_concierge.domain.lifecycle import HandoffTask
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.infra.db import tenant_session_scope

pytestmark = pytest.mark.integration

_VISIBLE_HANDOFF_TASKS = text(
    """
    SELECT task_id
    FROM handoff_tasks
    WHERE task_id IN (:first_task_id, :second_task_id)
    """
)


async def test_handoff_tasks_are_tenant_isolated_and_empty_guc_fails_closed(db: None) -> None:
    """An app-role query cannot discover another tenant's task, including after ``SET LOCAL ''``."""
    tenant_a, tenant_b = uuid4(), uuid4()
    task_a = await _create_handoff_task(tenant_a)
    task_b = await _create_handoff_task(tenant_b)
    parameters = {"first_task_id": task_a.task_id, "second_task_id": task_b.task_id}

    async with tenant_session_scope(tenant_a) as session:
        owned_ids = set((await session.execute(_VISIBLE_HANDOFF_TASKS, parameters)).scalars())
    async with tenant_session_scope(tenant_b) as session:
        other_ids = set((await session.execute(_VISIBLE_HANDOFF_TASKS, parameters)).scalars())

    assert owned_ids == {task_a.task_id}
    assert other_ids == {task_b.task_id}

    # A context that was never established filters to zero rows.
    async with tenant_session_scope(None) as session:
        unset_ids = set((await session.execute(_VISIBLE_HANDOFF_TASKS, parameters)).scalars())
    assert unset_ids == set()

    # ``set_config(..., '', true)`` reproduces the transaction-local reset value that motivated
    # migration 0003. The policy must use NULLIF rather than attempting ``''::uuid``.
    async with tenant_session_scope(None) as session:
        empty_context = (
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        ).scalar_one()
        empty_ids = set((await session.execute(_VISIBLE_HANDOFF_TASKS, parameters)).scalars())

    assert empty_context == ""
    assert empty_ids == set()


async def test_request_terminal_ledger_is_private_and_its_guard_rejects_foreign_context(
    db: None,
) -> None:
    """Terminal ledger data stays behind its guard, which cannot act across or without a tenant."""
    owner, other = uuid4(), uuid4()
    request_id = uuid4()
    transition_id = f"req:{owner}:{request_id}:failed-no-candidate:rls"
    repository = PostgresRequestRepository()

    await PostgresTenantRepository().add(_tenant(owner, "owner"))
    await PostgresTenantRepository().add(_tenant(other, "other"))
    await repository.add(
        EventRequest(
            request_id=request_id,
            tenant_id=owner,
            raw_text="a request whose terminal ledger must remain private",
            constraints=RequestConstraints(),
        )
    )
    assert await repository.mark_failed_no_candidate(owner, request_id, transition_id)

    # The ledger has RLS enabled but no app-role table grant: neither the owning tenant nor another
    # tenant can turn it into an enumeration side channel outside the SECURITY DEFINER guard.
    for tenant_context in (owner, other, None):
        with pytest.raises(Exception, match="permission denied"):
            async with tenant_session_scope(tenant_context) as session:
                await session.execute(
                    text(
                        """
                        SELECT transition_id
                        FROM request_terminal_ledger
                        WHERE transition_id = :transition_id
                        """
                    ),
                    {"transition_id": transition_id},
                )

    # Calling the sole guard from a different tenant cannot learn or terminalize the owner's
    # request. It must fail before any ledger or outbox effect is made.
    with pytest.raises(Exception, match="not visible"):
        async with tenant_session_scope(other) as session:
            await session.execute(
                text(
                    """
                    SELECT public.fn_terminalize_request_no_candidate(
                        :request_id,
                        :transition_id
                    )
                    """
                ),
                {
                    "request_id": request_id,
                    "transition_id": f"{transition_id}:foreign-context",
                },
            )

    # Empty is an explicit transaction-local value, not merely an omitted set_config call.
    with pytest.raises(Exception, match="active tenant context"):
        async with tenant_session_scope(None) as session:
            empty_context = (
                await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            ).scalar_one()
            assert empty_context == ""
            await session.execute(
                text(
                    """
                    SELECT public.fn_terminalize_request_no_candidate(
                        :request_id,
                        :transition_id
                    )
                    """
                ),
                {
                    "request_id": request_id,
                    "transition_id": f"{transition_id}:empty-context",
                },
            )

    async with tenant_session_scope(owner) as session:
        state = (
            await session.execute(
                text("SELECT state FROM event_requests WHERE request_id = :request_id"),
                {"request_id": request_id},
            )
        ).scalar_one()
        outbox_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM outbox
                    WHERE tenant_id = :tenant_id
                      AND topic = 'request.failed_no_candidate'
                      AND payload ->> 'transition_id' = :transition_id
                    """
                ),
                {"tenant_id": owner, "transition_id": transition_id},
            )
        ).scalar_one()

    assert state == "failed_no_candidate"
    assert outbox_count == 1


async def _create_handoff_task(tenant_id: UUID) -> HandoffTask:
    """Create a visible row only through the application-role guarded lifecycle path."""
    canonical_event_id = uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle_repository = PostgresLifecycleRepository()
    lifecycle = await lifecycle_repository.get_or_create(tenant_id, canonical_event_id, workflow_id)
    task = HandoffTask(
        task_id=f"{workflow_id}:rls:{uuid4().hex}",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link="https://example.test/rls-handoff",
        event_summary="RLS handoff fixture",
        ttl_expires_at=datetime.now(UTC) + timedelta(days=8),
    )
    await PostgresHandoffRepository().create_and_transition(
        task,
        lifecycle,
        LifecycleState.HANDOFF,
        f"{workflow_id}:handoff:{task.task_id}",
        {"task_id": task.task_id, "workflow_id": workflow_id},
    )
    return task


def _tenant(tenant_id: UUID, label: str) -> Tenant:
    """Make the request FK fixture through the ordinary app repository."""
    return Tenant(
        tenant_id=tenant_id,
        oidc_subject=f"oidc|rls-terminal-{label}-{tenant_id}",
        notify_email=f"rls-terminal-{label}-{tenant_id}@example.test",
        relay_inbox=f"rls-terminal-{label}-{tenant_id}@u.example.test",
    )
