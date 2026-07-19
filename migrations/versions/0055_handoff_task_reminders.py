"""Atomically ledger due handoff reminders and their notification outbox rows.

Revision ID: 0055
Revises: 0054
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "(text, text, uuid, text, text, text)"


def upgrade() -> None:
    """Add the task-bound reminder ledger and its sole guarded write path.

    The retained Temporal child owns durable timers, but PostgreSQL remains the source of truth
    for whether a task is still active and due.  The function deliberately writes the cadence
    ledger and user-visible outbox instruction in one transaction, so an activity ACK loss can
    replay its once-minted reminder identity without a second notification (FR-6.6, FR-8.3/8.9,
    ADR-003/007/009).
    """
    op.execute(
        """
        CREATE TABLE public.handoff_reminder_ledger (
            reminder_id           text PRIMARY KEY,
            tenant_id             uuid NOT NULL,
            task_id               text NOT NULL REFERENCES public.handoff_tasks(task_id) ON DELETE CASCADE,
            workflow_id           text NOT NULL,
            canonical_event_id    uuid NOT NULL,
            expiry_transition_id  text NOT NULL,
            reminder_kind         text NOT NULL CHECK (reminder_kind IN ('t24h', 't5d')),
            created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (task_id, reminder_kind)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_handoff_reminder_ledger_tenant_task
        ON public.handoff_reminder_ledger (tenant_id, task_id)
        """
    )
    op.execute("ALTER TABLE public.handoff_reminder_ledger ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.handoff_reminder_ledger FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY handoff_reminder_ledger_tenant_isolation
        ON public.handoff_reminder_ledger
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    # The ledger is an implementation detail of the SECURITY DEFINER guard.  The app role must
    # not be able to manufacture a visible reminder without the task/lifecycle due checks.
    op.execute("REVOKE ALL ON TABLE public.handoff_reminder_ledger FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.handoff_reminder_ledger FROM ec_app")
    _create_enqueue_reminder_function()
    op.execute(f"REVOKE ALL ON FUNCTION public.fn_enqueue_handoff_reminder{_SIGNATURE} FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.fn_enqueue_handoff_reminder{_SIGNATURE} TO ec_app"
    )


def downgrade() -> None:
    """Remove the private reminder guard and ledger without altering existing handoff tasks."""
    op.execute(f"DROP FUNCTION IF EXISTS public.fn_enqueue_handoff_reminder{_SIGNATURE}")
    op.execute("DROP TABLE IF EXISTS public.handoff_reminder_ledger")


def _create_enqueue_reminder_function() -> None:
    """Install the only path that can turn a due open task into a reminder outbox instruction."""
    op.execute(
        """
        CREATE FUNCTION public.fn_enqueue_handoff_reminder(
            p_task_id text,
            p_workflow_id text,
            p_canonical_event_id uuid,
            p_expiry_transition_id text,
            p_reminder_kind text,
            p_reminder_id text
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_context_tenant_id uuid;
            v_task_tenant_id uuid;
            v_task_workflow_id text;
            v_task_canonical_event_id uuid;
            v_task_expiry_transition_id text;
            v_task_state text;
            v_task_created_at timestamptz;
            v_task_ttl_expires_at timestamptz;
            v_task_event_summary text;
            v_task_deep_link text;
            v_lifecycle_state text;
            v_due_at timestamptz;
            v_now timestamptz;
            v_existing_reminder_id text;
            v_existing_tenant_id uuid;
            v_existing_task_id text;
            v_existing_workflow_id text;
            v_existing_canonical_event_id uuid;
            v_existing_expiry_transition_id text;
            v_existing_kind text;
            v_inserted_reminder_id text;
        BEGIN
            v_context_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_context_tenant_id IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'handoff reminder enqueue requires a tenant context';
            END IF;
            IF p_task_id IS NULL
               OR NULLIF(btrim(p_task_id), '') IS NULL
               OR p_workflow_id IS NULL
               OR NULLIF(btrim(p_workflow_id), '') IS NULL
               OR p_canonical_event_id IS NULL
               OR p_expiry_transition_id IS NULL
               OR NULLIF(btrim(p_expiry_transition_id), '') IS NULL
               OR p_reminder_kind NOT IN ('t24h', 't5d')
               OR p_reminder_id IS NULL
               OR NULLIF(btrim(p_reminder_id), '') IS NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff reminder identity is incomplete or malformed';
            END IF;
            IF p_workflow_id IS DISTINCT FROM (
                v_context_tenant_id::text || chr(58) || p_canonical_event_id::text
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff reminder workflow id must equal the deterministic tenant-event identity';
            END IF;

            -- Confirm an opaque replay identity against its task before looking at the ledger.
            -- This makes a task id reuse or a mismatched expiry id fail closed rather than return
            -- another task's acknowledgement.
            SELECT task.tenant_id, task.workflow_id, task.canonical_event_id,
                   task.expiry_transition_id, task.state, task.created_at, task.ttl_expires_at,
                   task.event_summary, task.deep_link
            INTO v_task_tenant_id, v_task_workflow_id, v_task_canonical_event_id,
                 v_task_expiry_transition_id, v_task_state, v_task_created_at,
                 v_task_ttl_expires_at, v_task_event_summary, v_task_deep_link
            FROM public.handoff_tasks AS task
            WHERE task.task_id = p_task_id
              AND task.tenant_id = v_context_tenant_id;
            IF NOT FOUND THEN
                RETURN 'inactive';
            END IF;
            IF v_task_workflow_id IS DISTINCT FROM p_workflow_id
               OR v_task_canonical_event_id IS DISTINCT FROM p_canonical_event_id
               OR v_task_expiry_transition_id IS DISTINCT FROM p_expiry_transition_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff reminder does not match its task identity';
            END IF;

            -- A successful prior call remains a replay even if a later terminal transition has
            -- resolved the task. The immutable ledger also rejects one reminder id rebound to a
            -- different task/cadence before any visible side effect is attempted.
            SELECT ledger.reminder_id, ledger.tenant_id, ledger.task_id, ledger.workflow_id,
                   ledger.canonical_event_id, ledger.expiry_transition_id, ledger.reminder_kind
            INTO v_existing_reminder_id, v_existing_tenant_id, v_existing_task_id,
                 v_existing_workflow_id, v_existing_canonical_event_id,
                 v_existing_expiry_transition_id, v_existing_kind
            FROM public.handoff_reminder_ledger AS ledger
            WHERE ledger.reminder_id = p_reminder_id;
            IF FOUND THEN
                IF v_existing_tenant_id IS DISTINCT FROM v_context_tenant_id
                   OR v_existing_task_id IS DISTINCT FROM p_task_id
                   OR v_existing_workflow_id IS DISTINCT FROM p_workflow_id
                   OR v_existing_canonical_event_id IS DISTINCT FROM p_canonical_event_id
                   OR v_existing_expiry_transition_id IS DISTINCT FROM p_expiry_transition_id
                   OR v_existing_kind IS DISTINCT FROM p_reminder_kind THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'handoff reminder id is already bound to another reminder';
                END IF;
                RETURN 'already_enqueued';
            END IF;

            SELECT ledger.reminder_id
            INTO v_existing_reminder_id
            FROM public.handoff_reminder_ledger AS ledger
            WHERE ledger.task_id = p_task_id
              AND ledger.reminder_kind = p_reminder_kind;
            IF FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'handoff task reminder cadence is already bound to another reminder';
            END IF;

            -- This is the same lifecycle-first ordering used by the guarded handoff creation
            -- path. A terminal transition and a timer race therefore serialize at durable state,
            -- while a deadlock victim can safely retry with the same reminder id.
            SELECT lifecycle.state
            INTO v_lifecycle_state
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.tenant_id = v_context_tenant_id
              AND lifecycle.workflow_id = p_workflow_id
              AND lifecycle.canonical_event_id = p_canonical_event_id
            FOR UPDATE;
            IF NOT FOUND
               OR v_lifecycle_state IN ('completed', 'cancelled', 'expired', 'failed_no_candidate') THEN
                RETURN 'inactive';
            END IF;

            -- Re-read under lock after the lifecycle lock because a newer handoff task may have
            -- superseded this one while the timer activity was waiting for a retry.
            SELECT task.tenant_id, task.workflow_id, task.canonical_event_id,
                   task.expiry_transition_id, task.state, task.created_at, task.ttl_expires_at,
                   task.event_summary, task.deep_link
            INTO v_task_tenant_id, v_task_workflow_id, v_task_canonical_event_id,
                 v_task_expiry_transition_id, v_task_state, v_task_created_at,
                 v_task_ttl_expires_at, v_task_event_summary, v_task_deep_link
            FROM public.handoff_tasks AS task
            WHERE task.task_id = p_task_id
              AND task.tenant_id = v_context_tenant_id
            FOR UPDATE;
            IF NOT FOUND OR v_task_state NOT IN ('open', 'notified') THEN
                RETURN 'inactive';
            END IF;
            IF v_task_workflow_id IS DISTINCT FROM p_workflow_id
               OR v_task_canonical_event_id IS DISTINCT FROM p_canonical_event_id
               OR v_task_expiry_transition_id IS DISTINCT FROM p_expiry_transition_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff reminder does not match its task identity';
            END IF;

            v_now := clock_timestamp();
            v_due_at := CASE p_reminder_kind
                WHEN 't24h' THEN v_task_created_at + INTERVAL '24 hours'
                WHEN 't5d' THEN v_task_created_at + INTERVAL '5 days'
                ELSE NULL
            END;
            IF v_due_at IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff reminder cadence is invalid';
            END IF;
            -- A reminder never competes with expiry: it must become due strictly before the
            -- persisted TTL and be enqueued before that deadline.
            IF v_due_at >= v_task_ttl_expires_at OR v_now >= v_task_ttl_expires_at THEN
                RETURN 'inactive';
            END IF;
            IF v_now < v_due_at THEN
                RETURN 'not_due';
            END IF;

            INSERT INTO public.handoff_reminder_ledger
                (reminder_id, tenant_id, task_id, workflow_id, canonical_event_id,
                 expiry_transition_id, reminder_kind)
            VALUES
                (p_reminder_id, v_context_tenant_id, p_task_id, p_workflow_id,
                 p_canonical_event_id, p_expiry_transition_id, p_reminder_kind)
            ON CONFLICT DO NOTHING
            RETURNING reminder_id INTO v_inserted_reminder_id;
            IF v_inserted_reminder_id IS NULL THEN
                SELECT ledger.reminder_id, ledger.tenant_id, ledger.task_id, ledger.workflow_id,
                       ledger.canonical_event_id, ledger.expiry_transition_id, ledger.reminder_kind
                INTO v_existing_reminder_id, v_existing_tenant_id, v_existing_task_id,
                     v_existing_workflow_id, v_existing_canonical_event_id,
                     v_existing_expiry_transition_id, v_existing_kind
                FROM public.handoff_reminder_ledger AS ledger
                WHERE ledger.reminder_id = p_reminder_id;
                IF FOUND
                   AND v_existing_tenant_id = v_context_tenant_id
                   AND v_existing_task_id = p_task_id
                   AND v_existing_workflow_id = p_workflow_id
                   AND v_existing_canonical_event_id = p_canonical_event_id
                   AND v_existing_expiry_transition_id = p_expiry_transition_id
                   AND v_existing_kind = p_reminder_kind THEN
                    RETURN 'already_enqueued';
                END IF;

                SELECT ledger.reminder_id
                INTO v_existing_reminder_id
                FROM public.handoff_reminder_ledger AS ledger
                WHERE ledger.task_id = p_task_id
                  AND ledger.reminder_kind = p_reminder_kind;
                IF FOUND THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'handoff task reminder cadence is already bound to another reminder';
                END IF;
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'handoff reminder id is already bound to another reminder';
            END IF;

            INSERT INTO public.outbox (tenant_id, topic, payload)
            VALUES (
                v_context_tenant_id,
                'handoff.reminder',
                jsonb_build_object(
                    'workflow_id', v_task_workflow_id,
                    'task_id', p_task_id,
                    'reminder_id', p_reminder_id,
                    'reminder_kind', p_reminder_kind,
                    'expiry_transition_id', v_task_expiry_transition_id,
                    'event_summary', v_task_event_summary,
                    'deep_link', v_task_deep_link
                )
            );
            RETURN 'enqueued';
        END;
        $$
        """
    )
