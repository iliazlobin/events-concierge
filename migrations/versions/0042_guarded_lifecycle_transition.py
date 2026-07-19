"""Move lifecycle state changes behind the ADR-007 guarded database transition.

Revision ID: 0042
Revises: 0041
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "(uuid, text, text, text, text, boolean, jsonb, jsonb)"


def upgrade() -> None:
    """Install the one transition choke point for state, ledger, outbox, and handoff creation."""
    op.execute(
        """
        CREATE FUNCTION public.fn_transition(
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
            v_task_id text;
            v_payload jsonb;
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
                    ('scheduled', 'withdrawing', 'cancelled'))
                OR (p_expected_state = 'scheduled' AND p_to_state IN
                    ('reconciled', 'withdrawing', 'cancelled', 'completed'))
                OR (p_expected_state = 'reconciled' AND p_to_state IN
                    ('scheduled', 'withdrawing', 'completed'))
                OR (p_expected_state = 'withdrawing' AND p_to_state = 'cancelled')
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'P0001',
                    MESSAGE = format(
                        'illegal lifecycle transition %s -> %s', p_expected_state, p_to_state
                    );
            END IF;

            SELECT l.tenant_id, l.workflow_id, l.canonical_event_id
            INTO v_tenant_id, v_workflow_id, v_canonical_event_id
            FROM public.lifecycle AS l
            WHERE l.lifecycle_id = p_lifecycle_id
              AND l.tenant_id = v_tenant_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'lifecycle is not visible to the tenant context';
            END IF;

            IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'lifecycle transition payload must be a JSON object';
            END IF;

            -- The unique reservation serializes duplicate activity retries. Any later failure rolls
            -- this insert back with the rest of the function transaction.
            INSERT INTO public.transition_ledger
                (transition_id, tenant_id, lifecycle_id, to_state)
            VALUES
                (p_transition_id, v_tenant_id, p_lifecycle_id, p_to_state)
            ON CONFLICT (transition_id) DO NOTHING
            RETURNING tenant_id, lifecycle_id, to_state
            INTO v_existing_tenant_id, v_existing_lifecycle_id, v_existing_to_state;

            IF NOT FOUND THEN
                SELECT l.tenant_id, l.lifecycle_id, l.to_state
                INTO v_existing_tenant_id, v_existing_lifecycle_id, v_existing_to_state
                FROM public.transition_ledger AS l
                WHERE l.transition_id = p_transition_id;
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

            UPDATE public.lifecycle
            SET state = p_to_state,
                lane = p_lane,
                conflict_warning = p_conflict_warning,
                updated_at = now()
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
                IF jsonb_typeof(p_handoff_task) <> 'object'
                   OR NULLIF(p_handoff_task ->> 'task_id', '') IS NULL
                   OR NULLIF(p_handoff_task ->> 'reason', '') IS NULL
                   OR NULLIF(p_handoff_task ->> 'event_summary', '') IS NULL
                   OR NULLIF(p_handoff_task ->> 'ttl_expires_at', '') IS NULL
                   OR (p_handoff_task ->> 'tenant_id')::uuid IS DISTINCT FROM v_tenant_id
                   OR p_handoff_task ->> 'workflow_id' IS DISTINCT FROM v_workflow_id
                   OR (p_handoff_task ->> 'canonical_event_id')::uuid
                      IS DISTINCT FROM v_canonical_event_id
                   OR jsonb_typeof(COALESCE(p_handoff_task -> 'metadata', '{}'::jsonb)) <> 'object'
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'handoff task does not match its lifecycle transition';
                END IF;

                INSERT INTO public.handoff_tasks
                    (task_id, tenant_id, workflow_id, canonical_event_id, reason, deep_link,
                     event_summary, ttl_expires_at, state, metadata)
                VALUES
                    (
                        p_handoff_task ->> 'task_id',
                        v_tenant_id,
                        v_workflow_id,
                        v_canonical_event_id,
                        p_handoff_task ->> 'reason',
                        COALESCE(p_handoff_task ->> 'deep_link', ''),
                        p_handoff_task ->> 'event_summary',
                        (p_handoff_task ->> 'ttl_expires_at')::timestamptz,
                        COALESCE(p_handoff_task ->> 'state', 'open'),
                        COALESCE(p_handoff_task -> 'metadata', '{}'::jsonb)
                    )
                ON CONFLICT (task_id) DO NOTHING
                RETURNING task_id INTO v_task_id;

                IF v_task_id IS NULL AND NOT EXISTS (
                    SELECT 1
                    FROM public.handoff_tasks
                    WHERE task_id = (p_handoff_task ->> 'task_id')
                      AND tenant_id = v_tenant_id
                      AND workflow_id = v_workflow_id
                      AND canonical_event_id = v_canonical_event_id
                      AND reason = (p_handoff_task ->> 'reason')
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'handoff task id is already bound to another task';
                END IF;
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
    op.execute(f"REVOKE ALL ON FUNCTION public.fn_transition{_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.fn_transition{_SIGNATURE} TO ec_app")
    # State updates and transition-ledger reservations must flow through the SECURITY DEFINER
    # function, which explicitly re-applies the caller's tenant context before touching data.
    op.execute("REVOKE UPDATE ON TABLE public.lifecycle FROM ec_app")
    op.execute("REVOKE INSERT ON TABLE public.transition_ledger FROM ec_app")


def downgrade() -> None:
    """Restore direct transition DML only when removing the guarded function."""
    op.execute("GRANT UPDATE ON TABLE public.lifecycle TO ec_app")
    op.execute("GRANT INSERT ON TABLE public.transition_ledger TO ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS public.fn_transition{_SIGNATURE}")
