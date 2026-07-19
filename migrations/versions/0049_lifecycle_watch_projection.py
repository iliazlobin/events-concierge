"""Project guarded lifecycle entry/exit to the ADR-008 watch registry.

Revision ID: 0049
Revises: 0048
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "(uuid, text, text, text, text, boolean, jsonb, jsonb)"


def upgrade() -> None:
    """Atomically enqueue one watch register/unregister after each relevant lifecycle transition."""
    op.execute(
        """
        ALTER TABLE public.lifecycle
        ADD COLUMN registration_source text
        CHECK (
            registration_source IS NULL OR registration_source IN (
                'public_jsonld', 'meetup', 'ticketmaster', 'luma', 'eventbrite', 'partiful',
                'serpapi'
            )
        )
        """
    )
    op.execute(
        """
        UPDATE public.lifecycle
        SET registration_source = CASE lane
            WHEN 'autonomous_sla' THEN 'meetup'
            WHEN 'browser_best_effort' THEN 'luma'
            ELSE NULL
        END
        WHERE registration_source IS NULL
          AND lane IN ('autonomous_sla', 'browser_best_effort')
        """
    )
    _replace_register_watch_function(require_registration_source=True)
    _replace_unregister_watch_function(require_registration_source=True)
    op.execute(
        """
        CREATE TABLE lifecycle_watch_projection_outbox (
            projection_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            lifecycle_id        uuid NOT NULL REFERENCES lifecycle(lifecycle_id) ON DELETE CASCADE,
            tenant_id           uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            workflow_id         text NOT NULL,
            canonical_event_id  uuid NOT NULL REFERENCES canonical_events(canonical_event_id)
                                      ON DELETE CASCADE,
            source              text NOT NULL,
            action              text NOT NULL CHECK (action IN ('register', 'unregister')),
            active_since        timestamptz NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            delivered_at        timestamptz,
            attempt_count       integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at     timestamptz NOT NULL DEFAULT now(),
            lease_token         text,
            lease_expires_at    timestamptz,
            last_error          text,
            UNIQUE (lifecycle_id, action),
            CHECK (
                (lease_token IS NULL AND lease_expires_at IS NULL)
                OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_lifecycle_watch_projection_outbox_ready
        ON lifecycle_watch_projection_outbox (next_attempt_at, projection_id)
        WHERE delivered_at IS NULL
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_enqueue_lifecycle_watch_projection()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_source text;
            v_action text;
        BEGIN
            v_source := NEW.registration_source;
            IF v_source IS NULL THEN
                RETURN NEW;
            END IF;

            IF NEW.state = 'registered' AND OLD.state IS DISTINCT FROM 'registered' THEN
                v_action := 'register';
            ELSIF OLD.state IN ('registered', 'scheduled', 'reconciled')
              AND NEW.state NOT IN ('registered', 'scheduled', 'reconciled') THEN
                v_action := 'unregister';
            ELSE
                RETURN NEW;
            END IF;

            -- A registered source has a matching link by the saga's read-before-mutate
            -- contract.  Retaining this database check prevents a malformed lane row from
            -- creating a permanently failing projection instruction.
            IF v_action = 'register' AND NOT EXISTS (
                SELECT 1
                FROM public.event_source_links
                WHERE canonical_event_id = NEW.canonical_event_id
                  AND source = v_source
            ) THEN
                RETURN NEW;
            END IF;

            INSERT INTO public.lifecycle_watch_projection_outbox
                (lifecycle_id, tenant_id, workflow_id, canonical_event_id, source, action, active_since)
            VALUES
                (NEW.lifecycle_id, NEW.tenant_id, NEW.workflow_id, NEW.canonical_event_id,
                 v_source, v_action, NEW.updated_at)
            ON CONFLICT (lifecycle_id, action) DO NOTHING;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_enqueue_lifecycle_watch_projection() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER tr_lifecycle_watch_projection
        AFTER UPDATE OF state ON public.lifecycle
        FOR EACH ROW
        EXECUTE FUNCTION public.fn_enqueue_lifecycle_watch_projection()
        """
    )
    _replace_transition_function(allow_registered_reconcile=True, include_registration_source=True)
    # ``ec_app`` may create and read its own lifecycle rows under RLS, but all
    # state mutation and ledger writes must remain behind ``fn_transition``.
    # The SECURITY DEFINER guard supplies the tenant check, legal-edge check,
    # idempotency ledger, outbox write, and projection trigger atomically
    # (ADR-007/ADR-008).
    op.execute("REVOKE DELETE ON TABLE public.lifecycle FROM ec_app")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON TABLE public.transition_ledger FROM ec_app")
    op.execute("REVOKE ALL ON TABLE public.lifecycle_watch_projection_outbox FROM ec_app")
    op.execute("GRANT SELECT, UPDATE ON TABLE public.lifecycle_watch_projection_outbox TO ec_app")
    op.execute(
        """
        INSERT INTO public.lifecycle_watch_projection_outbox
            (lifecycle_id, tenant_id, workflow_id, canonical_event_id, source, action, active_since)
        SELECT lifecycle.lifecycle_id, lifecycle.tenant_id, lifecycle.workflow_id,
               lifecycle.canonical_event_id,
               lifecycle.registration_source,
               'register',
               COALESCE(
                   (
                       SELECT ledger.created_at
                       FROM public.transition_ledger AS ledger
                       WHERE ledger.lifecycle_id = lifecycle.lifecycle_id
                         AND ledger.to_state = 'registered'
                       ORDER BY ledger.created_at DESC
                       LIMIT 1
                   ),
                   lifecycle.created_at
               )
        FROM public.lifecycle AS lifecycle
        WHERE lifecycle.state IN ('registered', 'scheduled', 'reconciled')
          AND lifecycle.registration_source IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM public.event_source_links AS link
              WHERE link.canonical_event_id = lifecycle.canonical_event_id
                AND link.source = lifecycle.registration_source
          )
        ON CONFLICT (lifecycle_id, action) DO NOTHING
        """
    )


def downgrade() -> None:
    """Remove the independent projection queue and restore the pre-P3 registered transition set."""
    op.execute("DROP TRIGGER IF EXISTS tr_lifecycle_watch_projection ON public.lifecycle")
    op.execute("DROP FUNCTION IF EXISTS public.fn_enqueue_lifecycle_watch_projection()")
    op.execute("DROP TABLE IF EXISTS public.lifecycle_watch_projection_outbox")
    _replace_transition_function(
        allow_registered_reconcile=False, include_registration_source=False
    )
    _replace_register_watch_function(require_registration_source=False)
    _replace_unregister_watch_function(require_registration_source=False)
    op.execute("ALTER TABLE public.lifecycle DROP COLUMN IF EXISTS registration_source")


def _replace_register_watch_function(*, require_registration_source: bool) -> None:
    """Bind an ADR-008 watch to the source that actually registered the lifecycle.

    Revision 0048 predates ``lifecycle.registration_source``.  Replacing the guarded function
    only after this revision adds/backfills the column preserves a fresh migration chain while
    making malformed or stale projections a safe no-op rather than a cross-source subscription.
    """
    source_predicate = (
        "AND lifecycle.registration_source = p_source" if require_registration_source else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_register_change_watch(
            p_tenant_id uuid,
            p_workflow_id text,
            p_canonical_event_id uuid,
            p_source text,
            p_active_since timestamptz
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_context_tenant_id uuid;
            v_lifecycle_id uuid;
            v_subscription_workflow_id text;
        BEGIN
            v_context_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_context_tenant_id IS NULL
               OR p_tenant_id IS DISTINCT FROM v_context_tenant_id
               OR p_active_since IS NULL THEN
                RETURN false;
            END IF;

            SELECT lifecycle.lifecycle_id
            INTO v_lifecycle_id
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.tenant_id = p_tenant_id
              AND lifecycle.workflow_id = p_workflow_id
              AND lifecycle.canonical_event_id = p_canonical_event_id
              AND lifecycle.workflow_id = (
                  lifecycle.tenant_id::text || ':' || lifecycle.canonical_event_id::text
              )
              AND lifecycle.state IN ('registered', 'scheduled', 'reconciled')
              {source_predicate}
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN false;
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM public.event_source_links
                WHERE canonical_event_id = p_canonical_event_id
                  AND source = p_source
            ) THEN
                RETURN false;
            END IF;

            -- Shared with the detector INSERT and registered transition.  Either the detector
            -- observes this subscription, or this catch-up sees its post-activation record.
            PERFORM pg_advisory_xact_lock(hashtext(p_canonical_event_id::text)::bigint);

            INSERT INTO public.watch_registry (canonical_event_id, source)
            VALUES (p_canonical_event_id, p_source)
            ON CONFLICT (canonical_event_id, source) DO UPDATE
            SET updated_at = now();

            INSERT INTO public.watch_subscriptions
                (canonical_event_id, source, tenant_id, workflow_id)
            VALUES (p_canonical_event_id, p_source, p_tenant_id, p_workflow_id)
            ON CONFLICT (canonical_event_id, source, tenant_id, workflow_id) DO NOTHING
            RETURNING workflow_id INTO v_subscription_workflow_id;

            INSERT INTO public.event_change_deliveries (fingerprint, tenant_id, workflow_id)
            SELECT change.fingerprint, p_tenant_id, p_workflow_id
            FROM public.event_changes AS change
            WHERE change.canonical_event_id = p_canonical_event_id
              AND change.detected_at >= p_active_since
            ON CONFLICT (fingerprint, tenant_id, workflow_id) DO NOTHING;
            RETURN v_subscription_workflow_id IS NOT NULL;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_register_change_watch(uuid, text, uuid, text, timestamptz) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_register_change_watch(uuid, text, uuid, text, timestamptz) TO ec_app"
    )


def _replace_unregister_watch_function(*, require_registration_source: bool) -> None:
    """Allow a watch exit only after its guarded lifecycle has actually become inactive."""
    source_predicate = (
        "AND lifecycle.registration_source = p_source" if require_registration_source else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_unregister_change_watch(
            p_tenant_id uuid,
            p_workflow_id text,
            p_canonical_event_id uuid,
            p_source text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_context_tenant_id uuid;
            v_lifecycle_id uuid;
            v_subscription_workflow_id text;
        BEGIN
            v_context_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_context_tenant_id IS NULL OR p_tenant_id IS DISTINCT FROM v_context_tenant_id THEN
                RETURN false;
            END IF;

            SELECT lifecycle.lifecycle_id
            INTO v_lifecycle_id
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.tenant_id = p_tenant_id
              AND lifecycle.workflow_id = p_workflow_id
              AND lifecycle.canonical_event_id = p_canonical_event_id
              AND lifecycle.workflow_id = (
                  lifecycle.tenant_id::text || ':' || lifecycle.canonical_event_id::text
              )
              AND lifecycle.state NOT IN ('registered', 'scheduled', 'reconciled')
              {source_predicate}
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN false;
            END IF;

            -- Shared with record/register: a change that wins this lock either creates a
            -- delivery which this function retires, or observes that its subscription is gone.
            PERFORM pg_advisory_xact_lock(hashtext(p_canonical_event_id::text)::bigint);

            DELETE FROM public.watch_subscriptions
            WHERE canonical_event_id = p_canonical_event_id
              AND source = p_source
              AND tenant_id = p_tenant_id
              AND workflow_id = p_workflow_id
            RETURNING workflow_id INTO v_subscription_workflow_id;
            IF v_subscription_workflow_id IS NULL THEN
                RETURN false;
            END IF;

            -- A delivery may have been leased just as a cancellation/un-RSVP completed.  It must
            -- not retry a signal to the now-closed workflow forever; the transition itself has
            -- already journaled the authoritative lifecycle/calendar effect (ADR-007/008).
            UPDATE public.event_change_deliveries AS delivery
            SET delivered_at = clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error = 'retired after lifecycle watch exit'
            FROM public.event_changes AS change
            WHERE delivery.fingerprint = change.fingerprint
              AND change.canonical_event_id = p_canonical_event_id
              AND delivery.tenant_id = p_tenant_id
              AND delivery.workflow_id = p_workflow_id
              AND delivery.delivered_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.watch_subscriptions AS remaining
                  WHERE remaining.canonical_event_id = p_canonical_event_id
                    AND remaining.tenant_id = p_tenant_id
                    AND remaining.workflow_id = p_workflow_id
              );

            DELETE FROM public.watch_registry AS registry
            WHERE registry.canonical_event_id = p_canonical_event_id
              AND registry.source = p_source
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.watch_subscriptions AS subscription
                  WHERE subscription.canonical_event_id = registry.canonical_event_id
                    AND subscription.source = registry.source
              );
            RETURN true;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_unregister_change_watch(uuid, text, uuid, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_unregister_change_watch(uuid, text, uuid, text) TO ec_app"
    )


def _replace_transition_function(
    *, allow_registered_reconcile: bool, include_registration_source: bool
) -> None:
    """Retain ADR-007's complete guard while adding the FR-8.7 registered reschedule edge."""
    registered_targets = "'scheduled', 'reconciled', 'withdrawing', 'cancelled'"
    if not allow_registered_reconcile:
        registered_targets = "'scheduled', 'withdrawing', 'cancelled'"
    registration_validation = ""
    registration_assignment = ""
    if include_registration_source:
        registration_validation = """
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
        """
        registration_assignment = """
                registration_source = CASE
                    WHEN p_to_state = 'registered'
                        THEN NULLIF(p_payload ->> 'registration_source', '')
                    ELSE registration_source
                END,
        """
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
                    ({registered_targets}))
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
                v_tenant_id::text || ':' || v_canonical_event_id::text
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'lifecycle workflow id must equal the deterministic tenant:event identity';
            END IF;

            IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'lifecycle transition payload must be a JSON object';
            END IF;
            {registration_validation}

            -- Serialize activation with the central detector's insertion lock.  PostgreSQL
            -- ``now()`` is transaction-start time, so it cannot safely order a detector that
            -- waited on this lock against an active-lifecycle cutoff.  The protected
            -- ``clock_timestamp()`` below is projected as ``active_since`` (ADR-008).
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
                {registration_assignment}
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
                   OR jsonb_typeof(COALESCE(p_handoff_task -> 'metadata', '{{}}'::jsonb)) <> 'object'
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
                        COALESCE(p_handoff_task -> 'metadata', '{{}}'::jsonb)
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
