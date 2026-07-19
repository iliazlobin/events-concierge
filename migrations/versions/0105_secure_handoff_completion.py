"""Add one-time handoff completion capabilities and guarded completion receipts.

Revision ID: 0105
Revises: 0104
Create Date: 2026-07-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0105"
down_revision: str | None = "0104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Install a digest-only capability lookup and atomic verified/review outcomes.

    The unauthenticated mark-done endpoint is itself capability authenticated. Its lookup therefore
    cannot establish a tenant GUC before resolving the token digest. As with the lifecycle
    invariant scanner, the narrowly-scoped resolver must be owned by a migration role that can
    bypass FORCE RLS; the runtime role receives EXECUTE only.
    """
    _require_bypassrls_definer()
    op.execute("ALTER TABLE public.handoff_tasks ADD COLUMN completion_token_hash text")
    op.execute(
        """
        ALTER TABLE public.handoff_tasks
        ADD CONSTRAINT ck_handoff_tasks_completion_token_hash
        CHECK (
            completion_token_hash IS NULL
            OR completion_token_hash ~ '^[0-9a-f]{64}$'
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_handoff_tasks_completion_token_hash
        ON public.handoff_tasks (completion_token_hash)
        WHERE completion_token_hash IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TABLE public.handoff_completion_attempts (
            task_id               text PRIMARY KEY
                                  REFERENCES public.handoff_tasks(task_id) ON DELETE CASCADE,
            tenant_id             uuid NOT NULL,
            workflow_id           text NOT NULL,
            canonical_event_id    uuid NOT NULL,
            completion_id         text NOT NULL UNIQUE,
            evidence              text NOT NULL CHECK (evidence = 'user_mark_done'),
            outcome               text NOT NULL
                                  CHECK (outcome IN ('verified', 'review_required')),
            detail                text NOT NULL DEFAULT '',
            registration_source   text,
            conflict_warning      boolean,
            created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            CHECK (NULLIF(btrim(completion_id), '') IS NOT NULL),
            CHECK (char_length(detail) <= 1000),
            CHECK (
                (
                    outcome = 'verified'
                    AND NULLIF(btrim(registration_source), '') IS NOT NULL
                    AND conflict_warning IS NOT NULL
                )
                OR (
                    outcome = 'review_required'
                    AND registration_source IS NULL
                    AND conflict_warning IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_handoff_completion_attempts_tenant_created
        ON public.handoff_completion_attempts (tenant_id, created_at)
        """
    )
    op.execute("ALTER TABLE public.handoff_completion_attempts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.handoff_completion_attempts FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY handoff_completion_attempts_tenant_isolation
        ON public.handoff_completion_attempts
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    op.execute("REVOKE ALL ON TABLE public.handoff_completion_attempts FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.handoff_completion_attempts FROM ec_app")
    op.execute("GRANT SELECT ON TABLE public.handoff_completion_attempts TO ec_app")
    _create_attach_capability()
    _create_resolve_capability()
    _create_review_capability()
    _create_verified_completion_capability()


def downgrade() -> None:
    """Remove the completion capabilities without changing existing lifecycle/task truth."""
    op.execute(
        "DROP FUNCTION IF EXISTS public.fn_complete_verified_handoff("
        "text, uuid, text, text, text, boolean, jsonb)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.fn_record_handoff_completion_review(text, text, text)"
    )
    op.execute("DROP FUNCTION IF EXISTS public.fn_resolve_handoff_completion_token(text)")
    op.execute("DROP FUNCTION IF EXISTS public.fn_attach_handoff_completion_token(text, text)")
    op.execute("DROP TABLE IF EXISTS public.handoff_completion_attempts")
    op.execute("DROP INDEX IF EXISTS public.ux_handoff_tasks_completion_token_hash")
    op.execute(
        "ALTER TABLE public.handoff_tasks "
        "DROP CONSTRAINT IF EXISTS ck_handoff_tasks_completion_token_hash"
    )
    op.execute(
        "ALTER TABLE public.handoff_tasks DROP COLUMN IF EXISTS completion_token_hash"
    )


def _require_bypassrls_definer() -> None:
    """Reject a resolver owner that would silently see zero FORCE-RLS task rows."""
    op.execute(
        """
        DO $$
        DECLARE
            v_can_bypass boolean;
        BEGIN
            SELECT role.rolsuper OR role.rolbypassrls
            INTO v_can_bypass
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = current_user;
            IF COALESCE(v_can_bypass, false) IS NOT TRUE THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = '0105 handoff capability resolver requires a superuser or BYPASSRLS migration owner';
            END IF;
        END;
        $$
        """
    )


def _create_attach_capability() -> None:
    """Attach a digest once inside the task-creation transaction; never replace it on replay."""
    op.execute(
        """
        CREATE FUNCTION public.fn_attach_handoff_completion_token(
            p_task_id text,
            p_token_hash text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_tenant_id uuid;
            v_existing_hash text;
        BEGIN
            v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_tenant_id IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'handoff completion capability requires a tenant context';
            END IF;
            IF NULLIF(btrim(p_task_id), '') IS NULL
               OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff completion capability input is invalid';
            END IF;

            UPDATE public.handoff_tasks AS task
            SET completion_token_hash = p_token_hash
            WHERE task.task_id = p_task_id
              AND task.tenant_id = v_tenant_id
              AND task.state IN ('open', 'notified')
              AND task.completion_token_hash IS NULL
            RETURNING task.completion_token_hash INTO v_existing_hash;
            IF FOUND THEN
                RETURN true;
            END IF;

            SELECT task.completion_token_hash
            INTO v_existing_hash
            FROM public.handoff_tasks AS task
            WHERE task.task_id = p_task_id
              AND task.tenant_id = v_tenant_id;
            IF FOUND AND v_existing_hash IS NOT NULL THEN
                -- A lost task-creation acknowledgement can regenerate a different plaintext
                -- capability in the retrying activity. The first digest and its already-committed
                -- notification remain authoritative; never replace it and never fail the replay.
                RETURN false;
            END IF;
            RAISE EXCEPTION USING
                ERRCODE = '23505',
                MESSAGE = 'handoff task capability is missing, inactive, or already bound';
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_attach_handoff_completion_token(text, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_attach_handoff_completion_token(text, text) TO ec_app"
    )


def _create_resolve_capability() -> None:
    """Resolve exactly one token digest without exposing a cross-tenant list/read surface."""
    op.execute(
        """
        CREATE FUNCTION public.fn_resolve_handoff_completion_token(p_token_hash text)
        RETURNS TABLE(
            task_id text,
            tenant_id uuid,
            workflow_id text,
            canonical_event_id uuid,
            capability_status text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT task.task_id,
                   task.tenant_id,
                   task.workflow_id,
                   task.canonical_event_id,
                   CASE
                       WHEN attempt.task_id IS NOT NULL OR task.state = 'completed' THEN 'used'
                       WHEN task.ttl_expires_at <= clock_timestamp()
                            OR task.state = 'expired'
                            OR lifecycle.state = 'expired' THEN 'expired'
                       WHEN task.state IN ('open', 'notified')
                            AND lifecycle.state = 'handoff'
                            AND task.reason NOT IN (
                                'calendar_write_failed', 'withdrawal_required'
                            ) THEN 'active'
                       ELSE 'inactive'
                   END
            FROM public.handoff_tasks AS task
            JOIN public.lifecycle AS lifecycle
              ON lifecycle.tenant_id = task.tenant_id
             AND lifecycle.workflow_id = task.workflow_id
             AND lifecycle.canonical_event_id = task.canonical_event_id
            LEFT JOIN public.handoff_completion_attempts AS attempt
              ON attempt.task_id = task.task_id
            WHERE p_token_hash ~ '^[0-9a-f]{64}$'
              AND task.completion_token_hash = p_token_hash
            LIMIT 1
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_resolve_handoff_completion_token(text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_resolve_handoff_completion_token(text) TO ec_app"
    )


def _create_review_capability() -> None:
    """Consume an unverified mark-done and emit one durable review-required notification."""
    op.execute(
        """
        CREATE FUNCTION public.fn_record_handoff_completion_review(
            p_task_id text,
            p_completion_id text,
            p_detail text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_tenant_id uuid;
            v_task public.handoff_tasks%ROWTYPE;
            v_existing_completion_id text;
            v_existing_outcome text;
            v_inserted_task_id text;
        BEGIN
            v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_tenant_id IS NULL
               OR NULLIF(btrim(p_completion_id), '') IS NULL
               OR char_length(COALESCE(p_detail, '')) > 1000 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff review input is invalid';
            END IF;

            SELECT task.*
            INTO v_task
            FROM public.handoff_tasks AS task
            JOIN public.lifecycle AS lifecycle
              ON lifecycle.tenant_id = task.tenant_id
             AND lifecycle.workflow_id = task.workflow_id
             AND lifecycle.canonical_event_id = task.canonical_event_id
             AND lifecycle.state = 'handoff'
            WHERE task.task_id = p_task_id
              AND task.tenant_id = v_tenant_id
              AND task.state IN ('open', 'notified')
              AND task.completion_token_hash IS NOT NULL
            FOR UPDATE OF task;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff review task is not active';
            END IF;

            INSERT INTO public.handoff_completion_attempts
                (task_id, tenant_id, workflow_id, canonical_event_id, completion_id,
                 evidence, outcome, detail)
            VALUES
                (v_task.task_id, v_task.tenant_id, v_task.workflow_id,
                 v_task.canonical_event_id, p_completion_id, 'user_mark_done',
                 'review_required', COALESCE(p_detail, ''))
            ON CONFLICT (task_id) DO NOTHING
            RETURNING task_id INTO v_inserted_task_id;
            IF v_inserted_task_id IS NULL THEN
                SELECT attempt.completion_id, attempt.outcome
                INTO v_existing_completion_id, v_existing_outcome
                FROM public.handoff_completion_attempts AS attempt
                WHERE attempt.task_id = p_task_id
                  AND attempt.tenant_id = v_tenant_id;
                IF v_existing_completion_id = p_completion_id
                   AND v_existing_outcome = 'review_required' THEN
                    RETURN false;
                END IF;
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'handoff completion capability was already consumed';
            END IF;

            INSERT INTO public.outbox (tenant_id, topic, payload)
            VALUES (
                v_task.tenant_id,
                'handoff_completion_review_required',
                jsonb_build_object(
                    'task_id', v_task.task_id,
                    'workflow_id', v_task.workflow_id,
                    'canonical_event_id', v_task.canonical_event_id,
                    'completion_id', p_completion_id,
                    'event_summary', v_task.event_summary,
                    'deep_link', v_task.deep_link,
                    'evidence', 'user_mark_done',
                    'detail', COALESCE(p_detail, '')
                )
            );
            RETURN true;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_record_handoff_completion_review(text, text, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_record_handoff_completion_review(text, text, text) TO ec_app"
    )


def _create_verified_completion_capability() -> None:
    """Atomically consume the capability, complete the task, and advance lifecycle truth."""
    op.execute(
        """
        CREATE FUNCTION public.fn_complete_verified_handoff(
            p_task_id text,
            p_lifecycle_id uuid,
            p_transition_id text,
            p_completion_id text,
            p_registration_source text,
            p_conflict_warning boolean,
            p_payload jsonb
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_tenant_id uuid;
            v_task public.handoff_tasks%ROWTYPE;
            v_lifecycle public.lifecycle%ROWTYPE;
            v_existing_completion_id text;
            v_existing_outcome text;
            v_applied boolean;
        BEGIN
            v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_tenant_id IS NULL
               OR NULLIF(btrim(p_transition_id), '') IS NULL
               OR NULLIF(btrim(p_completion_id), '') IS NULL
               OR p_payload IS NULL
               OR jsonb_typeof(p_payload) <> 'object' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'verified handoff completion input is invalid';
            END IF;

            SELECT attempt.completion_id, attempt.outcome
            INTO v_existing_completion_id, v_existing_outcome
            FROM public.handoff_completion_attempts AS attempt
            WHERE attempt.task_id = p_task_id
              AND attempt.tenant_id = v_tenant_id;
            IF FOUND THEN
                IF v_existing_completion_id = p_completion_id
                   AND v_existing_outcome = 'verified' THEN
                    RETURN false;
                END IF;
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'handoff completion capability was already consumed';
            END IF;

            SELECT task.*
            INTO v_task
            FROM public.handoff_tasks AS task
            WHERE task.task_id = p_task_id
              AND task.tenant_id = v_tenant_id
              AND task.state IN ('open', 'notified')
              AND task.completion_token_hash IS NOT NULL
              AND task.reason NOT IN ('calendar_write_failed', 'withdrawal_required')
            FOR UPDATE;
            IF NOT FOUND THEN
                -- A concurrent replay can have waited on the task row while the first claimant
                -- completed it and inserted the receipt. Re-read after that lock wait before
                -- classifying the now-inactive task (NFR-8).
                SELECT attempt.completion_id, attempt.outcome
                INTO v_existing_completion_id, v_existing_outcome
                FROM public.handoff_completion_attempts AS attempt
                WHERE attempt.task_id = p_task_id
                  AND attempt.tenant_id = v_tenant_id;
                IF FOUND THEN
                    IF v_existing_completion_id = p_completion_id
                       AND v_existing_outcome = 'verified' THEN
                        RETURN false;
                    END IF;
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'handoff completion capability was already consumed';
                END IF;
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'verified handoff task is not active';
            END IF;

            SELECT lifecycle.*
            INTO v_lifecycle
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.lifecycle_id = p_lifecycle_id
              AND lifecycle.tenant_id = v_tenant_id
              AND lifecycle.workflow_id = v_task.workflow_id
              AND lifecycle.canonical_event_id = v_task.canonical_event_id
              AND lifecycle.state = 'handoff'
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'verified handoff lifecycle is not active';
            END IF;

            SELECT public.fn_transition(
                p_lifecycle_id,
                'handoff',
                'registered',
                p_transition_id,
                'handoff',
                p_conflict_warning,
                p_payload || jsonb_build_object(
                    'task_id', p_task_id,
                    'completion_id', p_completion_id,
                    'evidence', 'user_mark_done',
                    'registration_source', p_registration_source
                ),
                NULL
            )
            INTO v_applied;

            UPDATE public.handoff_tasks
            SET state = 'completed'
            WHERE task_id = p_task_id
              AND tenant_id = v_tenant_id
              AND state IN ('open', 'notified');
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'P0001',
                    MESSAGE = 'verified handoff task lost completion authority';
            END IF;

            UPDATE public.handoff_expiry_queue
            SET resolved_at = clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error = 'resolved by verified handoff completion'
            WHERE task_id = p_task_id
              AND tenant_id = v_tenant_id
              AND resolved_at IS NULL;

            INSERT INTO public.handoff_completion_attempts
                (task_id, tenant_id, workflow_id, canonical_event_id, completion_id,
                 evidence, outcome, detail, registration_source, conflict_warning)
            VALUES
                (v_task.task_id, v_task.tenant_id, v_task.workflow_id,
                 v_task.canonical_event_id, p_completion_id, 'user_mark_done',
                 'verified', '', p_registration_source, p_conflict_warning);
            RETURN v_applied;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_complete_verified_handoff("
        "text, uuid, text, text, text, boolean, jsonb) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_complete_verified_handoff("
        "text, uuid, text, text, text, boolean, jsonb) TO ec_app"
    )
