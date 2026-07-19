"""Persist guarded handoff TTL expiry and its post-grace repair queue.

Revision ID: 0051
Revises: 0050
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "(uuid, text, text, text, text, boolean, jsonb, jsonb)"


def upgrade() -> None:
    """Make handoff TTL expiry recoverable without moving lifecycle truth out of PostgreSQL.

    Temporal owns the normal timer.  The opaque queue becomes eligible only after five minutes,
    providing ADR-007's sweeper repair path if that durable owner is unavailable while preserving
    the database transition guard as the sole state author (FR-6.6, FR-8.9, ADR-007).
    """
    op.execute("ALTER TABLE public.handoff_tasks ADD COLUMN expiry_transition_id text")
    # Old tasks predate a workflow-persisted expiry id.  Their task ids are already stable, so a
    # deterministic legacy identity is safe to replay and cannot collide across rows.
    op.execute(
        """
        UPDATE public.handoff_tasks
        SET expiry_transition_id = workflow_id || chr(58) || 'handoff-expiry' || chr(58) || task_id
        WHERE expiry_transition_id IS NULL
        """
    )
    op.execute("ALTER TABLE public.handoff_tasks ALTER COLUMN expiry_transition_id SET NOT NULL")
    op.execute(
        """
        ALTER TABLE public.handoff_tasks
        ADD CONSTRAINT ck_handoff_tasks_state
        CHECK (state IN ('open', 'notified', 'completed', 'expired', 'cancelled'))
        """
    )
    op.execute(
        """
        ALTER TABLE public.handoff_tasks
        ADD CONSTRAINT ck_handoff_tasks_reason
        CHECK (reason IN (
            'browser_fail', 'captcha', 'identity_wall', 'approval_gated', 'dues',
            'deferred_register', 'saturation', 'unexpected_paywall', 'no_autonomous_lane',
            'calendar_write_failed', 'withdrawal_required'
        ))
        """
    )
    op.execute(
        """
        ALTER TABLE public.handoff_tasks
        ADD CONSTRAINT ck_handoff_tasks_expiry_transition_id
        CHECK (NULLIF(btrim(expiry_transition_id), '') IS NOT NULL)
        """
    )
    # The pre-0051 schema permits two open recovery tasks for one lifecycle.  Preserve the most
    # recently created task, terminalize older duplicates before adding the active-row invariant,
    # and never enqueue those superseded legacy rows.
    op.execute(
        """
        WITH ranked AS (
            SELECT task_id,
                   row_number() OVER (
                       PARTITION BY tenant_id, workflow_id, canonical_event_id
                       ORDER BY created_at DESC, task_id DESC
                   ) AS precedence
            FROM public.handoff_tasks
            WHERE state IN ('open', 'notified')
        )
        UPDATE public.handoff_tasks AS task
        SET state = 'cancelled'
        FROM ranked
        WHERE task.task_id = ranked.task_id
          AND ranked.precedence > 1
        """
    )
    # Earlier handoff rows were not required to have a live lifecycle, and test/erasure cleanup may
    # have left an open orphan behind.  It has no workflow that can resolve it, so terminalize only
    # that orphan; preserve active legacy rows even when their catalog/tenant FK history was loose.
    op.execute(
        """
        UPDATE public.handoff_tasks AS task
        SET state = 'cancelled'
        WHERE task.state IN ('open', 'notified')
          AND NOT EXISTS (
              SELECT 1
              FROM public.lifecycle AS lifecycle
              WHERE lifecycle.tenant_id = task.tenant_id
                AND lifecycle.workflow_id = task.workflow_id
                AND lifecycle.canonical_event_id = task.canonical_event_id
                AND lifecycle.state NOT IN (
                    'completed', 'cancelled', 'expired', 'failed_no_candidate'
                )
          )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_handoff_tasks_one_active_lifecycle
        ON public.handoff_tasks (tenant_id, workflow_id, canonical_event_id)
        WHERE state IN ('open', 'notified')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_handoff_tasks_expiry_transition_id
        ON public.handoff_tasks (expiry_transition_id)
        """
    )
    op.execute(
        """
        -- Like the start and organizer-repair queues, this is a global opaque control plane.  It
        -- deliberately cannot require catalog/tenant FKs: older valid lifecycle rows predate
        -- those relationships and the queue must not make direct handoff creation fail for them.
        CREATE TABLE public.handoff_expiry_queue (
            task_id               text PRIMARY KEY REFERENCES public.handoff_tasks(task_id)
                                           ON DELETE CASCADE,
            tenant_id             uuid NOT NULL,
            workflow_id           text NOT NULL,
            canonical_event_id    uuid NOT NULL,
            expiry_transition_id  text NOT NULL UNIQUE,
            ttl_expires_at        timestamptz NOT NULL,
            eligible_at           timestamptz NOT NULL,
            next_attempt_at       timestamptz NOT NULL,
            created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            resolved_at           timestamptz,
            attempt_count         integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            lease_token           text,
            lease_expires_at      timestamptz,
            last_error            text,
            CHECK (eligible_at = ttl_expires_at + INTERVAL '5 minutes'),
            CHECK (next_attempt_at >= eligible_at),
            CHECK (
                (lease_token IS NULL AND lease_expires_at IS NULL)
                OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_handoff_expiry_queue_ready
        ON public.handoff_expiry_queue (next_attempt_at, task_id)
        WHERE resolved_at IS NULL
        """
    )
    _create_enqueue_handoff_expiry_function()
    op.execute(
        """
        CREATE TRIGGER tr_handoff_tasks_enqueue_expiry
        AFTER INSERT ON public.handoff_tasks
        FOR EACH ROW
        EXECUTE FUNCTION public.fn_enqueue_handoff_expiry()
        """
    )
    _create_handoff_task_function()
    _replace_transition_function()
    # Bootstrap retained open legacy tasks after the queue and trigger exist.  Already-terminal
    # rows intentionally receive no delayed expiry instruction.
    op.execute(
        """
        INSERT INTO public.handoff_expiry_queue
            (task_id, tenant_id, workflow_id, canonical_event_id, expiry_transition_id,
             ttl_expires_at, eligible_at, next_attempt_at)
        SELECT task.task_id, task.tenant_id, task.workflow_id, task.canonical_event_id,
               task.expiry_transition_id, task.ttl_expires_at,
               task.ttl_expires_at + INTERVAL '5 minutes',
               task.ttl_expires_at + INTERVAL '5 minutes'
        FROM public.handoff_tasks AS task
        WHERE task.state IN ('open', 'notified')
        ON CONFLICT (task_id) DO NOTHING
        """
    )
    # The queue is an opaque global control plane like the organizer-change repair queue.  App
    # code must enter task creation and terminalization through the SECURITY DEFINER guards, so it
    # cannot directly create, mutate, or delete task rows around the guarded transition.
    op.execute("REVOKE INSERT ON TABLE public.handoff_tasks FROM ec_app")
    op.execute("REVOKE UPDATE ON TABLE public.handoff_tasks FROM ec_app")
    op.execute("REVOKE DELETE ON TABLE public.handoff_tasks FROM ec_app")
    op.execute("REVOKE ALL ON TABLE public.handoff_expiry_queue FROM ec_app")
    op.execute("GRANT SELECT, UPDATE ON TABLE public.handoff_expiry_queue TO ec_app")


def downgrade() -> None:
    """Restore the 0049 transition guard and direct task-update grant for a full rollback."""
    _replace_transition_function_0049()
    op.execute("DROP TRIGGER IF EXISTS tr_handoff_tasks_enqueue_expiry ON public.handoff_tasks")
    op.execute("DROP FUNCTION IF EXISTS public.fn_create_handoff_task(jsonb)")
    op.execute("DROP FUNCTION IF EXISTS public.fn_enqueue_handoff_expiry()")
    op.execute("DROP TABLE IF EXISTS public.handoff_expiry_queue")
    op.execute("DROP INDEX IF EXISTS public.ux_handoff_tasks_one_active_lifecycle")
    op.execute("DROP INDEX IF EXISTS public.ux_handoff_tasks_expiry_transition_id")
    op.execute(
        "ALTER TABLE public.handoff_tasks DROP CONSTRAINT IF EXISTS ck_handoff_tasks_expiry_transition_id"
    )
    op.execute("ALTER TABLE public.handoff_tasks DROP CONSTRAINT IF EXISTS ck_handoff_tasks_reason")
    op.execute("ALTER TABLE public.handoff_tasks DROP CONSTRAINT IF EXISTS ck_handoff_tasks_state")
    op.execute("ALTER TABLE public.handoff_tasks DROP COLUMN IF EXISTS expiry_transition_id")
    op.execute("GRANT INSERT, UPDATE, DELETE ON TABLE public.handoff_tasks TO ec_app")


def _create_enqueue_handoff_expiry_function() -> None:
    """Validate an active deterministic task row and enqueue its fixed post-TTL grace window."""
    op.execute(
        """
        CREATE FUNCTION public.fn_enqueue_handoff_expiry()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_context_tenant_id uuid;
            v_lifecycle_id uuid;
        BEGIN
            v_context_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_context_tenant_id IS NULL
               OR NEW.tenant_id IS DISTINCT FROM v_context_tenant_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'handoff task creation requires its tenant context';
            END IF;
            IF NEW.workflow_id IS DISTINCT FROM (
                NEW.tenant_id::text || chr(58) || NEW.canonical_event_id::text
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff task workflow id must equal the deterministic tenant-event identity';
            END IF;

            SELECT lifecycle.lifecycle_id
            INTO v_lifecycle_id
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.tenant_id = NEW.tenant_id
              AND lifecycle.workflow_id = NEW.workflow_id
              AND lifecycle.canonical_event_id = NEW.canonical_event_id
              AND lifecycle.state NOT IN ('completed', 'cancelled', 'expired', 'failed_no_candidate')
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff task must attach to an active matching lifecycle';
            END IF;

            IF NEW.state IN ('open', 'notified') THEN
                INSERT INTO public.handoff_expiry_queue
                    (task_id, tenant_id, workflow_id, canonical_event_id, expiry_transition_id,
                     ttl_expires_at, eligible_at, next_attempt_at)
                VALUES
                    (NEW.task_id, NEW.tenant_id, NEW.workflow_id, NEW.canonical_event_id,
                     NEW.expiry_transition_id, NEW.ttl_expires_at,
                     NEW.ttl_expires_at + INTERVAL '5 minutes',
                     NEW.ttl_expires_at + INTERVAL '5 minutes')
                ON CONFLICT (task_id) DO NOTHING;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_enqueue_handoff_expiry() FROM PUBLIC")


def _create_handoff_task_function() -> None:
    """Create or replay one task while atomically superseding a prior open task for its lifecycle."""
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
            v_workflow_id text;
            v_canonical_event_id uuid;
            v_reason text;
            v_deep_link text;
            v_event_summary text;
            v_ttl_expires_at timestamptz;
            v_state text;
            v_metadata jsonb;
            v_expiry_transition_id text;
            v_lifecycle_id uuid;
            v_inserted_task_id text;
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
            v_workflow_id := p_task ->> 'workflow_id';
            v_canonical_event_id := (p_task ->> 'canonical_event_id')::uuid;
            v_reason := p_task ->> 'reason';
            v_deep_link := COALESCE(p_task ->> 'deep_link', '');
            v_event_summary := p_task ->> 'event_summary';
            v_ttl_expires_at := (p_task ->> 'ttl_expires_at')::timestamptz;
            v_state := COALESCE(NULLIF(p_task ->> 'state', ''), 'open');
            v_metadata := COALESCE(p_task -> 'metadata', '{}'::jsonb);
            v_expiry_transition_id := p_task ->> 'expiry_transition_id';

            IF (p_task ->> 'tenant_id')::uuid IS DISTINCT FROM v_context_tenant_id
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

            SELECT lifecycle.lifecycle_id
            INTO v_lifecycle_id
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.tenant_id = v_context_tenant_id
              AND lifecycle.workflow_id = v_workflow_id
              AND lifecycle.canonical_event_id = v_canonical_event_id
              AND lifecycle.state NOT IN ('completed', 'cancelled', 'expired', 'failed_no_candidate')
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'handoff task must attach to an active matching lifecycle';
            END IF;

            -- A later manual path (notably calendar recovery followed by un-RSVP) replaces its
            -- stale task before it creates the new one.  The paired queue rows are resolved in
            -- this same transaction, so an old timer cannot terminalize the newer obligation.
            WITH superseded AS (
                UPDATE public.handoff_tasks AS task
                SET state = 'cancelled'
                WHERE task.tenant_id = v_context_tenant_id
                  AND task.workflow_id = v_workflow_id
                  AND task.canonical_event_id = v_canonical_event_id
                  AND task.task_id <> v_task_id
                  AND task.state IN ('open', 'notified')
                RETURNING task.task_id
            )
            UPDATE public.handoff_expiry_queue AS expiry
            SET resolved_at = clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error = 'superseded by a newer handoff task'
            FROM superseded
            WHERE expiry.task_id = superseded.task_id
              AND expiry.resolved_at IS NULL;

            INSERT INTO public.handoff_tasks
                (task_id, tenant_id, workflow_id, canonical_event_id, reason, deep_link,
                 event_summary, ttl_expires_at, state, metadata, expiry_transition_id)
            VALUES
                (v_task_id, v_context_tenant_id, v_workflow_id, v_canonical_event_id, v_reason,
                 v_deep_link, v_event_summary, v_ttl_expires_at, v_state, v_metadata,
                 v_expiry_transition_id)
            ON CONFLICT (task_id) DO NOTHING
            RETURNING task_id INTO v_inserted_task_id;

            -- A task inserted by pre-0051 code cannot normally exist after this migration, but
            -- this backstop keeps a replay from leaving an exact active task without its queue.
            INSERT INTO public.handoff_expiry_queue
                (task_id, tenant_id, workflow_id, canonical_event_id, expiry_transition_id,
                 ttl_expires_at, eligible_at, next_attempt_at)
            SELECT task.task_id, task.tenant_id, task.workflow_id, task.canonical_event_id,
                   task.expiry_transition_id, task.ttl_expires_at,
                   task.ttl_expires_at + INTERVAL '5 minutes',
                   task.ttl_expires_at + INTERVAL '5 minutes'
            FROM public.handoff_tasks AS task
            WHERE task.task_id = v_task_id
              AND task.state IN ('open', 'notified')
            ON CONFLICT (task_id) DO NOTHING;

            IF v_inserted_task_id IS NOT NULL THEN
                RETURN true;
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.handoff_tasks AS task
                WHERE task.task_id = v_task_id
                  AND task.tenant_id = v_context_tenant_id
                  AND task.workflow_id = v_workflow_id
                  AND task.canonical_event_id = v_canonical_event_id
                  AND task.reason = v_reason
                  AND task.expiry_transition_id = v_expiry_transition_id
            ) THEN
                RETURN false;
            END IF;
            RAISE EXCEPTION USING
                ERRCODE = '23505',
                MESSAGE = 'handoff task id is already bound to another task';
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_create_handoff_task(jsonb) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.fn_create_handoff_task(jsonb) TO ec_app")


def _replace_transition_function() -> None:
    """Extend 0049's guard with task-bound expiry validation and terminal resource cleanup."""
    op.execute(
        """
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

            -- Serialize activation with the central detector's insertion lock.  PostgreSQL
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
                -- The workflow's own durable timer is allowed at the exact TTL.  Only the
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

                UPDATE public.handoff_expiry_queue
                SET resolved_at = clock_timestamp(),
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = 'resolved by terminal lifecycle transition'
                WHERE tenant_id = v_tenant_id
                  AND workflow_id = v_workflow_id
                  AND canonical_event_id = v_canonical_event_id
                  AND resolved_at IS NULL;
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


def _replace_transition_function_0049() -> None:
    """Restore the complete 0049 guarded transition on downgrade, not a partial approximation."""
    op.execute(
        """
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
                    ('scheduled', 'reconciled', 'withdrawing', 'cancelled'))
                OR (p_expected_state = 'scheduled' AND p_to_state IN
                    ('reconciled', 'withdrawing', 'cancelled', 'completed'))
                OR (p_expected_state = 'reconciled' AND p_to_state IN
                    ('scheduled', 'reconciled', 'withdrawing', 'cancelled', 'completed'))
                OR (p_expected_state = 'withdrawing' AND p_to_state = 'cancelled')
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
                    FROM public.handoff_tasks AS task
                    WHERE task.task_id = (p_handoff_task ->> 'task_id')
                      AND task.tenant_id = v_tenant_id
                      AND task.workflow_id = v_workflow_id
                      AND task.canonical_event_id = v_canonical_event_id
                      AND task.reason = (p_handoff_task ->> 'reason')
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'handoff task id is already bound to another task';
                END IF;
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
