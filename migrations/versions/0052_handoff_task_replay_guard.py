"""Prevent a stale handoff-task replay from superseding its newer successor.

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Fence exact task-id replays before the 0051 supersession path (NFR-8, ADR-007)."""
    # Keep 0051's full creation implementation intact for an absent task id.  The narrow wrapper
    # first locks/replays an existing task id, so a late retry of task A cannot cancel active task B.
    op.execute(
        "ALTER FUNCTION public.fn_create_handoff_task(jsonb) "
        "RENAME TO fn_create_handoff_task_unchecked"
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_create_handoff_task_unchecked(jsonb) FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.fn_create_handoff_task_unchecked(jsonb) FROM ec_app")
    _create_replay_guard()


def downgrade() -> None:
    """Restore 0051's creation function when rolling back this isolated replay hardening."""
    op.execute(
        "ALTER FUNCTION public.fn_create_handoff_task(jsonb) "
        "RENAME TO fn_create_handoff_task_replay_guard"
    )
    op.execute(
        "ALTER FUNCTION public.fn_create_handoff_task_unchecked(jsonb) "
        "RENAME TO fn_create_handoff_task"
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_create_handoff_task(jsonb) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.fn_create_handoff_task(jsonb) TO ec_app")
    op.execute("DROP FUNCTION public.fn_create_handoff_task_replay_guard(jsonb)")


def _create_replay_guard() -> None:
    """Create the task-id fence while preserving 0051's guarded creator for genuinely new tasks."""
    op.execute(
        """
        CREATE FUNCTION public.fn_create_handoff_task(p_task jsonb)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_context_tenant_id uuid;
            v_task_id text;
            v_input_tenant_id uuid;
            v_workflow_id text;
            v_canonical_event_id uuid;
            v_reason text;
            v_state text;
            v_expiry_transition_id text;
            v_existing_tenant_id uuid;
            v_existing_workflow_id text;
            v_existing_canonical_event_id uuid;
            v_existing_reason text;
            v_existing_expiry_transition_id text;
            v_existing_state text;
        BEGIN
            v_context_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_context_tenant_id IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'handoff task creation requires a tenant context';
            END IF;
            IF p_task IS NULL
               OR jsonb_typeof(p_task) <> 'object'
               OR NULLIF(p_task ->> 'task_id', '') IS NULL
               OR NULLIF(p_task ->> 'tenant_id', '') IS NULL
               OR NULLIF(p_task ->> 'workflow_id', '') IS NULL
               OR NULLIF(p_task ->> 'canonical_event_id', '') IS NULL
               OR NULLIF(p_task ->> 'reason', '') IS NULL
               OR NULLIF(p_task ->> 'event_summary', '') IS NULL
               OR NULLIF(p_task ->> 'ttl_expires_at', '') IS NULL
               OR NULLIF(p_task ->> 'expiry_transition_id', '') IS NULL
               OR jsonb_typeof(COALESCE(p_task -> 'metadata', '{}'::jsonb)) <> 'object'
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff task payload is incomplete or malformed';
            END IF;

            v_task_id := p_task ->> 'task_id';
            v_input_tenant_id := (p_task ->> 'tenant_id')::uuid;
            v_workflow_id := p_task ->> 'workflow_id';
            v_canonical_event_id := (p_task ->> 'canonical_event_id')::uuid;
            v_reason := p_task ->> 'reason';
            v_state := COALESCE(NULLIF(p_task ->> 'state', ''), 'open');
            v_expiry_transition_id := p_task ->> 'expiry_transition_id';
            -- Parse before the replay return too: malformed retries must fail closed rather than
            -- appearing to be valid acknowledgements of an existing task.
            PERFORM (p_task ->> 'ttl_expires_at')::timestamptz;

            IF v_input_tenant_id IS DISTINCT FROM v_context_tenant_id
               OR v_workflow_id IS DISTINCT FROM (
                   v_context_tenant_id::text || chr(58) || v_canonical_event_id::text
               ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff task does not match its deterministic tenant lifecycle';
            END IF;
            IF v_state NOT IN ('open', 'notified', 'completed', 'expired', 'cancelled') THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff task state is invalid';
            END IF;
            IF v_reason NOT IN (
                'browser_fail', 'captcha', 'identity_wall', 'approval_gated', 'dues',
                'deferred_register', 'saturation', 'unexpected_paywall', 'no_autonomous_lane',
                'calendar_write_failed', 'withdrawal_required'
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff task reason is invalid';
            END IF;

            -- ``task_id`` is globally unique while lifecycle locking is scoped to one event.
            -- Serialize the non-existent-row case before looking it up, then return an exact
            -- replay before 0051's creator is allowed to supersede any active sibling task.
            PERFORM pg_advisory_xact_lock(hashtext(v_task_id)::bigint);
            SELECT task.tenant_id, task.workflow_id, task.canonical_event_id, task.reason,
                   task.expiry_transition_id, task.state
            INTO v_existing_tenant_id, v_existing_workflow_id, v_existing_canonical_event_id,
                 v_existing_reason, v_existing_expiry_transition_id, v_existing_state
            FROM public.handoff_tasks AS task
            WHERE task.task_id = v_task_id
            FOR UPDATE;
            IF FOUND THEN
                IF v_existing_tenant_id IS DISTINCT FROM v_input_tenant_id
                   OR v_existing_workflow_id IS DISTINCT FROM v_workflow_id
                   OR v_existing_canonical_event_id IS DISTINCT FROM v_canonical_event_id
                   OR v_existing_reason IS DISTINCT FROM v_reason
                   OR v_existing_expiry_transition_id IS DISTINCT FROM v_expiry_transition_id
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'handoff task id is already bound to another task';
                END IF;

                -- Preserve 0051's queue backstop for a valid active replay, without reviving a
                -- cancelled/expired predecessor or touching a newer task for this lifecycle.
                IF v_existing_state IN ('open', 'notified') THEN
                    INSERT INTO public.handoff_expiry_queue
                        (task_id, tenant_id, workflow_id, canonical_event_id, expiry_transition_id,
                         ttl_expires_at, eligible_at, next_attempt_at)
                    SELECT task.task_id, task.tenant_id, task.workflow_id,
                           task.canonical_event_id, task.expiry_transition_id,
                           task.ttl_expires_at, task.ttl_expires_at + INTERVAL '5 minutes',
                           task.ttl_expires_at + INTERVAL '5 minutes'
                    FROM public.handoff_tasks AS task
                    WHERE task.task_id = v_task_id
                    ON CONFLICT (task_id) DO NOTHING;
                END IF;
                RETURN false;
            END IF;

            RETURN public.fn_create_handoff_task_unchecked(p_task);
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_create_handoff_task(jsonb) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.fn_create_handoff_task(jsonb) TO ec_app")
