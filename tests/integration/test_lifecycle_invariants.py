"""PostgreSQL contract coverage for the read-only ADR-007 invariant scanner."""

from __future__ import annotations

import os
from dataclasses import asdict
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.postgres.invariants import PostgresLifecycleInvariantRepository
from events_concierge.infra.db import system_session_scope, tenant_session_scope

pytestmark = pytest.mark.integration

_SNAPSHOT_FIELDS = {
    "active_lifecycles_missing_watch",
    "active_lifecycles_mismatched_watch",
    "terminal_watch_subscriptions",
    "orphan_watch_subscriptions",
    "orphan_watch_registry",
    "active_handoffs_missing_expiry_queue",
    "orphan_active_handoff_tasks",
    "inactive_handoff_expiry_queue",
    "pending_watch_register_projections",
    "pending_watch_unregister_projections",
    "invalid_lifecycle_workflow_identity_count",
}


async def test_global_invariant_functions_bypass_force_rls_only_as_aggregate_and_opaque_worklist(
    db: None,
) -> None:
    """An unset/empty app context stays blind while the narrow privileged scanner remains observable.

    The direct app role cannot see either fixture lifecycle without its tenant GUC.  The migration
    owner-owned functions intentionally can scan FORCE-RLS rows globally, but return only count
    aggregates or deterministic workflow IDs.  A malformed direct INSERT proves the scanner
    catches the load-bearing tenant:event identity rather than assuming repository callers are the
    only writers (FR-1.3/1.4, FR-8.1, ADR-007).
    """
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL is required for owner-only fixture cleanup")
    tenant_id = UUID("00000000-0000-0000-0000-000000000001")
    canonical_event_id = uuid4()
    malformed_event_id = uuid4()
    watchable_event_id = uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    malformed_workflow_id = f"malformed-lifecycle-workflow:{uuid4()}"
    watchable_workflow_id = f"{tenant_id}:{watchable_event_id}"
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)

    try:
        await _assert_global_scanner_contract(
            tenant_id=tenant_id,
            canonical_event_id=canonical_event_id,
            malformed_event_id=malformed_event_id,
            workflow_id=workflow_id,
            malformed_workflow_id=malformed_workflow_id,
            watchable_event_id=watchable_event_id,
            watchable_workflow_id=watchable_workflow_id,
        )
    finally:
        try:
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        DELETE FROM public.lifecycle
                        WHERE workflow_id IN (
                            :workflow_id,
                            :malformed_workflow_id,
                            :watchable_workflow_id
                        )
                        """
                    ),
                    {
                        "workflow_id": workflow_id,
                        "malformed_workflow_id": malformed_workflow_id,
                        "watchable_workflow_id": watchable_workflow_id,
                    },
                )
        finally:
            await owner_engine.dispose()


async def test_snapshot_counts_an_unresolved_queue_for_an_inactive_handoff_task(db: None) -> None:
    """A terminal task with its queue still unresolved is visible to the global scanner.

    The expiry queue's task FK makes a physically missing task impossible in a normal deployment.
    This fixture therefore covers the reachable failure mode: an out-of-band task terminalization
    leaves its opaque expiry instruction unresolved.  The scanner must report it rather than let
    the sweeper retry a task that can no longer transition (FR-6.6, FR-8.1, ADR-007).
    """
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL is required for owner-only fixture setup and cleanup")
    tenant_id = uuid4()
    canonical_event_id = uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    task_id = f"{workflow_id}:invariant-expiry:{uuid4().hex}"
    expiry_transition_id = f"{workflow_id}:handoff-expiry:{uuid4().hex}"
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)

    try:
        before = await _snapshot()
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.lifecycle
                        (lifecycle_id, tenant_id, canonical_event_id, workflow_id, state)
                    VALUES
                        (:lifecycle_id, :tenant_id, :canonical_event_id, :workflow_id, 'found')
                    """
                ),
                {
                    "lifecycle_id": uuid4(),
                    "tenant_id": tenant_id,
                    "canonical_event_id": canonical_event_id,
                    "workflow_id": workflow_id,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.handoff_tasks
                        (task_id, tenant_id, workflow_id, canonical_event_id, reason, deep_link,
                         event_summary, ttl_expires_at, state, metadata, expiry_transition_id)
                    VALUES
                        (:task_id, :tenant_id, :workflow_id, :canonical_event_id,
                         'deferred_register', 'https://example.test/invariant-expiry',
                         'Lifecycle invariant scanner fixture',
                         clock_timestamp() + INTERVAL '1 day', 'open', '{}'::jsonb,
                         :expiry_transition_id)
                    """
                ),
                {
                    "task_id": task_id,
                    "tenant_id": tenant_id,
                    "workflow_id": workflow_id,
                    "canonical_event_id": canonical_event_id,
                    "expiry_transition_id": expiry_transition_id,
                },
            )
            await connection.execute(
                text(
                    """
                    UPDATE public.handoff_tasks
                    SET state = 'cancelled'
                    WHERE task_id = :task_id
                    """
                ),
                {"task_id": task_id},
            )

        after = await _snapshot()
        assert after["inactive_handoff_expiry_queue"] >= before["inactive_handoff_expiry_queue"] + 1
    finally:
        try:
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM public.handoff_tasks WHERE task_id = :task_id"),
                    {"task_id": task_id},
                )
                await connection.execute(
                    text("DELETE FROM public.lifecycle WHERE workflow_id = :workflow_id"),
                    {"workflow_id": workflow_id},
                )
        finally:
            await owner_engine.dispose()


async def _assert_global_scanner_contract(
    *,
    tenant_id: UUID,
    canonical_event_id: UUID,
    malformed_event_id: UUID,
    workflow_id: str,
    malformed_workflow_id: str,
    watchable_event_id: UUID,
    watchable_workflow_id: str,
) -> None:
    """Exercise raw SQL and the repository mapping while disposable fixture rows exist."""
    before = await _snapshot()

    async with tenant_session_scope(tenant_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO lifecycle
                    (lifecycle_id, tenant_id, canonical_event_id, workflow_id, state)
                VALUES
                    (:lifecycle_id, :tenant_id, :canonical_event_id, :workflow_id, 'found')
                """
            ),
            {
                "lifecycle_id": uuid4(),
                "tenant_id": tenant_id,
                "canonical_event_id": canonical_event_id,
                "workflow_id": workflow_id,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO lifecycle
                    (lifecycle_id, tenant_id, canonical_event_id, workflow_id, state)
                VALUES
                    (:lifecycle_id, :tenant_id, :canonical_event_id, :workflow_id, 'found')
                """
            ),
            {
                "lifecycle_id": uuid4(),
                "tenant_id": tenant_id,
                "canonical_event_id": malformed_event_id,
                "workflow_id": malformed_workflow_id,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO lifecycle
                    (lifecycle_id, tenant_id, canonical_event_id, workflow_id, state,
                     registration_source)
                VALUES
                    (:lifecycle_id, :tenant_id, :canonical_event_id, :workflow_id, 'registered',
                     'luma')
                """
            ),
            {
                "lifecycle_id": uuid4(),
                "tenant_id": tenant_id,
                "canonical_event_id": watchable_event_id,
                "workflow_id": watchable_workflow_id,
            },
        )

    async with tenant_session_scope(None) as session:
        direct_without_context = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM lifecycle
                    WHERE workflow_id IN (:workflow_id, :malformed_workflow_id)
                    """
                ),
                {
                    "workflow_id": workflow_id,
                    "malformed_workflow_id": malformed_workflow_id,
                },
            )
        ).scalar_one()
        worklist = (
            (
                await session.execute(
                    text(
                        """
                    SELECT *
                    FROM public.fn_list_nonterminal_lifecycle_workflow_ids(:after_workflow_id, :limit)
                    """
                    ),
                    {
                        "after_workflow_id": "00000000-0000-0000-0000-000000000000",
                        "limit": 1000,
                    },
                )
            )
            .mappings()
            .all()
        )
        function_roles = (
            (
                await session.execute(
                    text(
                        """
                    SELECT routine.proname,
                           routine.prosecdef,
                           owner.rolsuper OR owner.rolbypassrls AS owner_can_bypass
                    FROM pg_catalog.pg_proc AS routine
                    JOIN pg_catalog.pg_roles AS owner ON owner.oid = routine.proowner
                    WHERE routine.oid IN (
                        'public.fn_lifecycle_invariant_snapshot()'::regprocedure,
                        'public.fn_list_nonterminal_lifecycle_workflow_ids(text,integer)'::regprocedure
                    )
                    ORDER BY routine.proname
                    """
                    )
                )
            )
            .mappings()
            .all()
        )
        execute_privileges = (
            (
                await session.execute(
                    text(
                        """
                    SELECT has_function_privilege(
                               current_user,
                               'public.fn_lifecycle_invariant_snapshot()',
                               'EXECUTE'
                           ) AS snapshot_execute,
                           has_function_privilege(
                               current_user,
                               'public.fn_list_nonterminal_lifecycle_workflow_ids(text,integer)',
                               'EXECUTE'
                           ) AS worklist_execute
                    """
                    )
                )
            )
            .mappings()
            .one()
        )

    after = await _snapshot()
    repository = PostgresLifecycleInvariantRepository()
    mapped_snapshot = await repository.database_snapshot()
    mapped_worklist = await repository.nonterminal_workflow_ids(
        after_workflow_id="00000000-0000-0000-0000-000000000000",
        limit=1000,
    )
    assert int(direct_without_context) == 0
    assert all(set(row) == {"workflow_id"} for row in worklist)
    assert workflow_id in {str(row["workflow_id"]) for row in worklist}
    assert all(bool(row["prosecdef"]) and bool(row["owner_can_bypass"]) for row in function_roles)
    assert {str(row["proname"]) for row in function_roles} == {
        "fn_lifecycle_invariant_snapshot",
        "fn_list_nonterminal_lifecycle_workflow_ids",
    }
    assert bool(execute_privileges["snapshot_execute"]) is True
    assert bool(execute_privileges["worklist_execute"]) is True
    assert set(after) == _SNAPSHOT_FIELDS
    assert all(isinstance(value, int) for value in after.values())
    assert asdict(mapped_snapshot) == after
    assert workflow_id in mapped_worklist
    assert after["active_lifecycles_missing_watch"] >= before["active_lifecycles_missing_watch"] + 1
    assert (
        after["invalid_lifecycle_workflow_identity_count"]
        >= before["invalid_lifecycle_workflow_identity_count"] + 1
    )

    async with tenant_session_scope(None) as session:
        await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        direct_with_empty_context = (
            await session.execute(
                text("SELECT count(*) FROM lifecycle WHERE workflow_id = :workflow_id"),
                {"workflow_id": workflow_id},
            )
        ).scalar_one()
        empty_context_snapshot = (
            (await session.execute(text("SELECT * FROM public.fn_lifecycle_invariant_snapshot()")))
            .mappings()
            .one()
        )

    assert int(direct_with_empty_context) == 0
    assert {key: int(value) for key, value in empty_context_snapshot.items()} == after


async def _snapshot() -> dict[str, int]:
    """Read the deliberately aggregate-only scanner result with no tenant context."""
    async with system_session_scope() as session:
        row = (
            (await session.execute(text("SELECT * FROM public.fn_lifecycle_invariant_snapshot()")))
            .mappings()
            .one()
        )
    return {key: int(value) for key, value in row.items()}
