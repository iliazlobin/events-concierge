"""Durably terminalize request-level no-result outcomes.

Revision ID: 0053
Revises: 0052
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add one guarded request terminal ledger plus an atomic no-result outbox function."""
    op.execute(
        """
        CREATE TABLE public.request_terminal_ledger (
            transition_id  text PRIMARY KEY,
            tenant_id      uuid NOT NULL REFERENCES public.tenants (tenant_id) ON DELETE CASCADE,
            request_id     uuid NOT NULL REFERENCES public.event_requests (request_id) ON DELETE CASCADE,
            terminal_state text NOT NULL CHECK (terminal_state = 'failed_no_candidate'),
            created_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, request_id, terminal_state)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_request_terminal_ledger_tenant_request
        ON public.request_terminal_ledger (tenant_id, request_id)
        """
    )
    op.execute("ALTER TABLE public.request_terminal_ledger ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.request_terminal_ledger FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY request_terminal_ledger_tenant_isolation
        ON public.request_terminal_ledger
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    op.execute("REVOKE ALL ON TABLE public.request_terminal_ledger FROM ec_app")
    op.execute(
        """
        CREATE FUNCTION public.fn_terminalize_request_no_candidate(
            p_request_id uuid,
            p_transition_id text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_context_tenant_id uuid;
            v_request_state text;
            v_existing_tenant_id uuid;
            v_existing_request_id uuid;
            v_existing_terminal_state text;
            v_existing_transition_id text;
        BEGIN
            v_context_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_context_tenant_id IS NULL
               OR p_request_id IS NULL
               OR p_transition_id IS NULL
               OR btrim(p_transition_id) = '' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'request terminalization requires an active tenant context and identity';
            END IF;

            SELECT request.state
            INTO v_request_state
            FROM public.event_requests AS request
            WHERE request.request_id = p_request_id
              AND request.tenant_id = v_context_tenant_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'event request is not visible to the tenant context';
            END IF;

            SELECT ledger.tenant_id, ledger.request_id, ledger.terminal_state
            INTO v_existing_tenant_id, v_existing_request_id, v_existing_terminal_state
            FROM public.request_terminal_ledger AS ledger
            WHERE ledger.transition_id = p_transition_id;
            IF FOUND THEN
                IF v_existing_tenant_id IS DISTINCT FROM v_context_tenant_id
                   OR v_existing_request_id IS DISTINCT FROM p_request_id
                   OR v_existing_terminal_state IS DISTINCT FROM 'failed_no_candidate' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'request terminal transition id is already bound to another terminal';
                END IF;
                RETURN false;
            END IF;

            SELECT ledger.transition_id
            INTO v_existing_transition_id
            FROM public.request_terminal_ledger AS ledger
            WHERE ledger.tenant_id = v_context_tenant_id
              AND ledger.request_id = p_request_id
              AND ledger.terminal_state = 'failed_no_candidate'
            FOR UPDATE;
            IF FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'event request already has a different no-result terminal transition';
            END IF;

            IF v_request_state NOT IN ('received', 'started') THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'event request cannot transition to failed_no_candidate from its current state';
            END IF;

            INSERT INTO public.request_terminal_ledger
                (transition_id, tenant_id, request_id, terminal_state)
            VALUES
                (p_transition_id, v_context_tenant_id, p_request_id, 'failed_no_candidate');

            UPDATE public.event_requests
            SET state = 'failed_no_candidate'
            WHERE request_id = p_request_id
              AND tenant_id = v_context_tenant_id;

            INSERT INTO public.outbox (tenant_id, topic, payload)
            VALUES (
                v_context_tenant_id,
                'request.failed_no_candidate',
                jsonb_build_object(
                    'request_id', p_request_id::text,
                    'workflow_id', 'req:' || v_context_tenant_id::text || ':' || p_request_id::text,
                    'transition_id', p_transition_id,
                    'event_summary', 'your request'
                )
            );
            RETURN true;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_terminalize_request_no_candidate(uuid, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_terminalize_request_no_candidate(uuid, text) TO ec_app"
    )


def downgrade() -> None:
    """Remove the request terminal guard; existing EventRequest rows remain historical state."""
    op.execute("DROP FUNCTION IF EXISTS public.fn_terminalize_request_no_candidate(uuid, text)")
    op.execute("DROP TABLE IF EXISTS public.request_terminal_ledger")
