"""Atomically fence orphan handoff expiry against its live queue lease.

Revision ID: 0097
Revises: 0096
Create Date: 2026-07-18

Temporal remains the authoritative ordinary TTL owner.  Its post-grace orphan-repair queue may
advance a lifecycle only if the exact leased instruction is still live at the transaction's final
queue write.  A lost lease rolls the guarded lifecycle, ledger, task, and outbox work back as one
unit (FR-6.6, FR-8.9, NFR-8, ADR-007).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0097"
down_revision: str | None = "0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRANSITION_SIGNATURE = "(uuid, text, text, text, text, boolean, jsonb, jsonb)"
_LEASE_LOST_SQLSTATE = "EC001"


def upgrade() -> None:
    """Install the narrow orphan-repair capability and its guarded-transition cleanup skip."""
    _replace_transition_function(allow_orphan_queue_skip=True)
    _create_orphan_expiry_capability()


def downgrade() -> None:
    """Remove only the orphan queue authority path and restore 0051 transition behavior."""
    op.execute("DROP FUNCTION IF EXISTS public.fn_expire_handoff_from_queue(text, text)")
    _replace_transition_function(allow_orphan_queue_skip=False)


def _replace_transition_function(*, allow_orphan_queue_skip: bool) -> None:
    """Preserve 0051's guard while optionally deferring one queue row to the outer lease fence."""
    skip_declaration = "v_orphan_queue_skip_task_id text;" if allow_orphan_queue_skip else ""
    skip_validation = (
        """
            v_orphan_queue_skip_task_id := NULLIF(
                current_setting('app.handoff_expiry_queue_skip_task_id', true), ''
            );
            IF v_orphan_queue_skip_task_id IS NOT NULL AND (
                p_to_state <> 'expired'
                OR v_orphan_queue_skip_task_id IS DISTINCT FROM NULLIF(p_payload ->> 'task_id', '')
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'orphan expiry queue cleanup skip is invalid';
            END IF;
        """
        if allow_orphan_queue_skip
        else ""
    )
    skip_filter = (
        """
                  AND (
                      v_orphan_queue_skip_task_id IS NULL
                      OR expiry.task_id <> v_orphan_queue_skip_task_id
                  )
        """
        if allow_orphan_queue_skip
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_transition(
            p_lifecycle_id uuid,
            p_expected_state text,
            p_to_state text,
            p_transition_id text,
            p_lane text,
            p_conflict_warning boolean,
            p_payload jsonb,
            p_handoff_task jsonb DEFAULT NULL
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_tenant_id uuid;
            v_workflow_id text;
            v_canonical_event_id uuid;
            v_existing_tenant_id uuid;
            v_existing_lifecycle_id uuid;
            v_existing_to_state text;
            v_payload jsonb;
            v_expiry_task_id text;
            v_expiry_transition_id text;
            v_expiry_task_ttl timestamptz;
            v_expiry_task_state text;
            v_created_handoff_task boolean;
            {skip_declaration}
        BEGIN
            v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_tenant_id IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'lifecycle transition requires a tenant context';
            END IF;

            IF NOT (
                (p_expected_state = 'found' AND p_to_state IN
                    ('awaiting_confirmation', 'registered', 'handoff', 'failed_no_candidate'))
                OR (p_expected_state = 'handoff' AND p_to_state IN
                    ('registered', 'expired', 'cancelled'))
                OR (p_expected_state = 'awaiting_confirmation' AND p_to_state IN
                    ('registered', 'handoff', 'cancelled'))
                OR (p_expected_state = 'registered' AND p_to_state IN
                    ('scheduled', 'reconciled', 'withdrawing', 'cancelled', 'expired'))
                OR (p_expected_state = 'scheduled' AND p_to_state IN
                    ('reconciled', 'withdrawing', 'cancelled', 'completed'))
                OR (p_expected_state = 'reconciled' AND p_to_state IN
                    ('scheduled', 'reconciled', 'withdrawing', 'cancelled', 'completed'))
                OR (p_expected_state = 'withdrawing' AND p_to_state IN ('cancelled', 'expired'))
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'P0001',
                    MESSAGE = format(
                        'illegal lifecycle transition %s -> %s', p_expected_state, p_to_state
                    );
            END IF;

            SELECT lifecycle.tenant_id, lifecycle.workflow_id, lifecycle.canonical_event_id
            INTO v_tenant_id, v_workflow_id, v_canonical_event_id
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.lifecycle_id = p_lifecycle_id
              AND lifecycle.tenant_id = v_tenant_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'lifecycle is not visible to the tenant context';
            END IF;

            IF v_workflow_id IS DISTINCT FROM (
                v_tenant_id::text || chr(58) || v_canonical_event_id::text
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'lifecycle workflow id must equal the deterministic tenant-event identity';
            END IF;

            IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'lifecycle transition payload must be a JSON object';
            END IF;
            {skip_validation}
            IF p_to_state = 'registered' AND (
                NULLIF(p_payload ->> 'registration_source', '') IS NULL
                OR NOT EXISTS (
                    SELECT 1
                    FROM public.event_source_links
                    WHERE canonical_event_id = v_canonical_event_id
                      AND source = p_payload ->> 'registration_source'
                )
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'registered lifecycle requires a linked registration_source';
            END IF;
            IF p_to_state = 'expired' AND (
                NULLIF(p_payload ->> 'task_id', '') IS NULL
                OR NULLIF(p_payload ->> 'expiry_transition_id', '') IS NULL
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'expired lifecycle requires its handoff task identity';
            END IF;

            -- Serialize activation with the central detector's insertion lock. PostgreSQL
            -- ``now()`` is transaction-start time, so it cannot safely order a detector that
            -- waited on this lock against an active-lifecycle cutoff (ADR-008).
            IF p_to_state = 'registered' THEN
                PERFORM pg_advisory_xact_lock(hashtext(v_canonical_event_id::text)::bigint);
            END IF;

            INSERT INTO public.transition_ledger
                (transition_id, tenant_id, lifecycle_id, to_state)
            VALUES
                (p_transition_id, v_tenant_id, p_lifecycle_id, p_to_state)
            ON CONFLICT (transition_id) DO NOTHING
            RETURNING tenant_id, lifecycle_id, to_state
            INTO v_existing_tenant_id, v_existing_lifecycle_id, v_existing_to_state;

            IF NOT FOUND THEN
                SELECT ledger.tenant_id, ledger.lifecycle_id, ledger.to_state
                INTO v_existing_tenant_id, v_existing_lifecycle_id, v_existing_to_state
                FROM public.transition_ledger AS ledger
                WHERE ledger.transition_id = p_transition_id;
                IF FOUND
                   AND v_existing_tenant_id = v_tenant_id
                   AND v_existing_lifecycle_id = p_lifecycle_id
                   AND v_existing_to_state = p_to_state THEN
                    RETURN false;
                END IF;
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'transition id is already bound to another lifecycle transition';
            END IF;

            IF p_to_state = 'expired' THEN
                v_expiry_task_id := p_payload ->> 'task_id';
                SELECT task.expiry_transition_id, task.ttl_expires_at, task.state
                INTO v_expiry_transition_id, v_expiry_task_ttl, v_expiry_task_state
                FROM public.handoff_tasks AS task
                WHERE task.task_id = v_expiry_task_id
                  AND task.tenant_id = v_tenant_id
                  AND task.workflow_id = v_workflow_id
                  AND task.canonical_event_id = v_canonical_event_id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'expired lifecycle task does not match its active lifecycle';
                END IF;
                IF v_expiry_transition_id IS DISTINCT FROM p_transition_id
                   OR v_expiry_transition_id IS DISTINCT FROM p_payload ->> 'expiry_transition_id'
                   OR v_expiry_task_state NOT IN ('open', 'notified') THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'expired lifecycle task identity is invalid or already resolved';
                END IF;
                -- The workflow's own durable timer is allowed at the exact TTL. Only the
                -- separate orphan-recovery queue waits the additional five-minute grace.
                IF v_expiry_task_ttl > clock_timestamp() THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'handoff task expiry is not due yet';
                END IF;
            END IF;

            UPDATE public.lifecycle
            SET state = p_to_state,
                lane = p_lane,
                registration_source = CASE
                    WHEN p_to_state = 'registered'
                        THEN NULLIF(p_payload ->> 'registration_source', '')
                    ELSE registration_source
                END,
                conflict_warning = p_conflict_warning,
                updated_at = clock_timestamp()
            WHERE lifecycle_id = p_lifecycle_id
              AND tenant_id = v_tenant_id
              AND state = p_expected_state;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'P0001',
                    MESSAGE = format(
                        'lifecycle %s is not in expected state %s', p_lifecycle_id, p_expected_state
                    );
            END IF;

            IF p_handoff_task IS NOT NULL THEN
                SELECT public.fn_create_handoff_task(p_handoff_task)
                INTO v_created_handoff_task;
            END IF;

            IF p_to_state IN ('completed', 'cancelled', 'expired', 'failed_no_candidate') THEN
                UPDATE public.handoff_tasks
                SET state = CASE p_to_state
                    WHEN 'expired' THEN 'expired'
                    ELSE 'cancelled'
                END
                WHERE tenant_id = v_tenant_id
                  AND workflow_id = v_workflow_id
                  AND canonical_event_id = v_canonical_event_id
                  AND state IN ('open', 'notified');

                UPDATE public.handoff_expiry_queue AS expiry
                SET resolved_at = clock_timestamp(),
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = 'resolved by terminal lifecycle transition'
                WHERE tenant_id = v_tenant_id
                  AND workflow_id = v_workflow_id
                  AND canonical_event_id = v_canonical_event_id
                  AND resolved_at IS NULL
                  {skip_filter};
            END IF;

            v_payload := p_payload || jsonb_build_object(
                'workflow_id', v_workflow_id,
                'transition_id', p_transition_id,
                'lifecycle_state', p_to_state
            );
            INSERT INTO public.outbox (tenant_id, topic, payload)
            VALUES (v_tenant_id, 'lifecycle.' || p_to_state, v_payload);
            RETURN true;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION public.fn_transition{_TRANSITION_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.fn_transition{_TRANSITION_SIGNATURE} TO ec_app")


def _create_orphan_expiry_capability() -> None:
    """Create the sole post-grace path that ties a repair lease to guarded terminality."""
    op.execute(
        f"""
        CREATE FUNCTION public.fn_expire_handoff_from_queue(
            p_task_id text,
            p_lease_token text
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_expiry public.handoff_expiry_queue%ROWTYPE;
            v_lifecycle public.lifecycle%ROWTYPE;
            v_event_summary text;
            v_applied boolean;
            v_terminal_state text;
        BEGIN
            IF NULLIF(btrim(p_task_id), '') IS NULL
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
            THEN
                RETURN 'ignored';
            END IF;

            IF NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'orphan handoff expiry requires an empty caller tenant context';
            END IF;

            SELECT expiry.* INTO v_expiry
            FROM public.handoff_expiry_queue AS expiry
            WHERE expiry.task_id = p_task_id
              AND expiry.lease_token = p_lease_token
              AND expiry.resolved_at IS NULL;
            IF NOT FOUND
               OR v_expiry.eligible_at > clock_timestamp()
               OR v_expiry.next_attempt_at > clock_timestamp()
               OR v_expiry.lease_expires_at IS NULL
               OR v_expiry.lease_expires_at <= clock_timestamp()
            THEN
                RETURN 'lease_lost';
            END IF;

            PERFORM set_config('app.tenant_id', v_expiry.tenant_id::text, true);

            SELECT task.event_summary INTO v_event_summary
            FROM public.handoff_tasks AS task
            WHERE task.task_id = v_expiry.task_id
              AND task.tenant_id = v_expiry.tenant_id
              AND task.workflow_id = v_expiry.workflow_id
              AND task.canonical_event_id = v_expiry.canonical_event_id
              AND task.expiry_transition_id = v_expiry.expiry_transition_id;
            IF NOT FOUND THEN
                RETURN 'ignored';
            END IF;

            SELECT lifecycle.* INTO v_lifecycle
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.tenant_id = v_expiry.tenant_id
              AND lifecycle.workflow_id = v_expiry.workflow_id
              AND lifecycle.canonical_event_id = v_expiry.canonical_event_id;
            IF NOT FOUND THEN
                RETURN 'ignored';
            END IF;

            IF v_lifecycle.state IN ('expired', 'completed', 'cancelled', 'failed_no_candidate') THEN
                -- Acquire the row first, then read ``clock_timestamp()``. A volatile predicate
                -- embedded in an UPDATE can be evaluated before waiting on another row locker.
                SELECT expiry.* INTO v_expiry
                FROM public.handoff_expiry_queue AS expiry
                WHERE expiry.task_id = v_expiry.task_id
                  AND expiry.tenant_id = v_expiry.tenant_id
                  AND expiry.workflow_id = v_expiry.workflow_id
                  AND expiry.canonical_event_id = v_expiry.canonical_event_id
                  AND expiry.expiry_transition_id = v_expiry.expiry_transition_id
                  AND expiry.lease_token = p_lease_token
                  AND expiry.resolved_at IS NULL
                FOR UPDATE;
                IF NOT FOUND
                   OR v_expiry.eligible_at > clock_timestamp()
                   OR v_expiry.next_attempt_at > clock_timestamp()
                   OR v_expiry.lease_expires_at IS NULL
                   OR v_expiry.lease_expires_at <= clock_timestamp()
                THEN
                    RETURN 'lease_lost';
                END IF;
                UPDATE public.handoff_expiry_queue AS expiry
                SET resolved_at = clock_timestamp(),
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL
                WHERE expiry.task_id = v_expiry.task_id
                  AND expiry.tenant_id = v_expiry.tenant_id
                  AND expiry.workflow_id = v_expiry.workflow_id
                  AND expiry.canonical_event_id = v_expiry.canonical_event_id
                  AND expiry.expiry_transition_id = v_expiry.expiry_transition_id
                  AND expiry.lease_token = p_lease_token
                  AND expiry.resolved_at IS NULL
                ;
                IF NOT FOUND THEN
                    RETURN 'lease_lost';
                END IF;
                IF v_lifecycle.state = 'expired' THEN
                    RETURN 'expired';
                END IF;
                RETURN 'terminal';
            END IF;

            IF v_lifecycle.state NOT IN ('handoff', 'registered', 'withdrawing') THEN
                RETURN 'ignored';
            END IF;

            PERFORM set_config('app.handoff_expiry_queue_skip_task_id', v_expiry.task_id, true);
            BEGIN
                SELECT public.fn_transition(
                    v_lifecycle.lifecycle_id,
                    v_lifecycle.state,
                    'expired',
                    v_expiry.expiry_transition_id,
                    v_lifecycle.lane,
                    v_lifecycle.conflict_warning,
                    jsonb_build_object(
                        'canonical_event_id', v_expiry.canonical_event_id::text,
                        'workflow_id', v_expiry.workflow_id,
                        'event_summary', v_event_summary,
                        'task_id', v_expiry.task_id,
                        'expiry_transition_id', v_expiry.expiry_transition_id,
                        'expiry_reason', 'handoff_ttl'
                    ),
                    NULL::jsonb
                ) INTO v_applied;

                IF NOT v_applied THEN
                    SELECT lifecycle.state INTO v_terminal_state
                    FROM public.lifecycle AS lifecycle
                    WHERE lifecycle.lifecycle_id = v_lifecycle.lifecycle_id
                      AND lifecycle.tenant_id = v_expiry.tenant_id;
                    IF v_terminal_state IS NULL OR v_terminal_state NOT IN (
                        'expired', 'completed', 'cancelled', 'failed_no_candidate'
                    ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = 'P0001',
                            MESSAGE = 'handoff expiry ledger replay did not terminalize its lifecycle';
                    END IF;
                    SELECT expiry.* INTO v_expiry
                    FROM public.handoff_expiry_queue AS expiry
                    WHERE expiry.task_id = v_expiry.task_id
                      AND expiry.tenant_id = v_expiry.tenant_id
                      AND expiry.workflow_id = v_expiry.workflow_id
                      AND expiry.canonical_event_id = v_expiry.canonical_event_id
                      AND expiry.expiry_transition_id = v_expiry.expiry_transition_id
                      AND expiry.lease_token = p_lease_token
                      AND expiry.resolved_at IS NULL
                    FOR UPDATE;
                    IF NOT FOUND
                       OR v_expiry.eligible_at > clock_timestamp()
                       OR v_expiry.next_attempt_at > clock_timestamp()
                       OR v_expiry.lease_expires_at IS NULL
                       OR v_expiry.lease_expires_at <= clock_timestamp()
                    THEN
                        RETURN 'lease_lost';
                    END IF;
                    UPDATE public.handoff_expiry_queue AS expiry
                    SET resolved_at = clock_timestamp(),
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error = NULL
                    WHERE expiry.task_id = v_expiry.task_id
                      AND expiry.tenant_id = v_expiry.tenant_id
                      AND expiry.workflow_id = v_expiry.workflow_id
                      AND expiry.canonical_event_id = v_expiry.canonical_event_id
                      AND expiry.expiry_transition_id = v_expiry.expiry_transition_id
                      AND expiry.lease_token = p_lease_token
                      AND expiry.resolved_at IS NULL
                    ;
                    IF NOT FOUND THEN
                        RETURN 'lease_lost';
                    END IF;
                    IF v_terminal_state = 'expired' THEN
                        RETURN 'expired';
                    END IF;
                    RETURN 'terminal';
                END IF;

                SELECT expiry.* INTO v_expiry
                FROM public.handoff_expiry_queue AS expiry
                WHERE expiry.task_id = v_expiry.task_id
                  AND expiry.tenant_id = v_expiry.tenant_id
                  AND expiry.workflow_id = v_expiry.workflow_id
                  AND expiry.canonical_event_id = v_expiry.canonical_event_id
                  AND expiry.expiry_transition_id = v_expiry.expiry_transition_id
                  AND expiry.lease_token = p_lease_token
                  AND expiry.resolved_at IS NULL
                FOR UPDATE;
                IF NOT FOUND
                   OR v_expiry.eligible_at > clock_timestamp()
                   OR v_expiry.next_attempt_at > clock_timestamp()
                   OR v_expiry.lease_expires_at IS NULL
                   OR v_expiry.lease_expires_at <= clock_timestamp()
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '{_LEASE_LOST_SQLSTATE}',
                        MESSAGE = 'orphan handoff expiry lease is no longer current';
                END IF;
                UPDATE public.handoff_expiry_queue AS expiry
                SET resolved_at = clock_timestamp(),
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL
                WHERE expiry.task_id = v_expiry.task_id
                  AND expiry.tenant_id = v_expiry.tenant_id
                  AND expiry.workflow_id = v_expiry.workflow_id
                  AND expiry.canonical_event_id = v_expiry.canonical_event_id
                  AND expiry.expiry_transition_id = v_expiry.expiry_transition_id
                  AND expiry.lease_token = p_lease_token
                  AND expiry.resolved_at IS NULL
                ;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '{_LEASE_LOST_SQLSTATE}',
                        MESSAGE = 'orphan handoff expiry lease is no longer current';
                END IF;
            EXCEPTION
                WHEN SQLSTATE '{_LEASE_LOST_SQLSTATE}' THEN
                    RETURN 'lease_lost';
            END;
            RETURN 'expired';
        END;
        $$
        """
    )
    signature = "public.fn_expire_handoff_from_queue(text, text)"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
