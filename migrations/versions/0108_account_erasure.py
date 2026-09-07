"""Add fenced, resumable, tenant-wide account erasure.

Revision ID: 0108
Revises: 0107
Create Date: 2026-07-22

The command row is both a durable replay tombstone and a write fence. A shared advisory lock in
every tenant-table mutation trigger closes the race between worker commits/deletes and immutable
erasure inventory capture. A leased system worker resumes external workflow/calendar/vault/object
cleanup before the final SECURITY DEFINER transaction removes identity, lifecycle, behavioral,
notification, watch, consent, and provider-binding rows. Registration-action audit keeps its
explicitly allowed operational fields while its consent reference is irreversibly shredded
(FR-10.5, NFR-10/11).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0108"
down_revision: str | None = "0107"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_BEGIN_SIGNATURE = "(uuid)"
_STAGE_SIGNATURE = "(uuid, text, integer)"
_FINALIZE_SIGNATURE = "(uuid)"
_CLAIM_SIGNATURE = "(integer, integer)"
_RELEASE_SIGNATURE = "(uuid, uuid, uuid, text, integer)"
_RENEW_SIGNATURE = "(uuid, uuid, uuid, integer)"

# Every current relation carrying a tenant_id. The erasure tombstone itself is intentionally not
# fenced: its guarded capabilities must advance after the live tenant identity has been removed.
_FENCED_TABLES = (
    "tenants",
    "calendar_bindings",
    "event_change_calendar_repairs",
    "event_change_deliveries",
    "event_requests",
    "google_calendar_sync_state",
    "handoff_completion_attempts",
    "handoff_expiry_queue",
    "handoff_reminder_ledger",
    "handoff_tasks",
    "lifecycle",
    "lifecycle_organizer_change_ledger",
    "lifecycle_watch_projection_outbox",
    "notification_ledger",
    "outbox",
    "registration_action_audit",
    "request_outcome_links",
    "request_start_outbox",
    "request_terminal_ledger",
    "tenant_policy_control",
    "tenant_ranking_feedback_receipts",
    "tenant_ranking_profiles",
    "tenant_source_consents",
    "tenant_workflow_registry",
    "transition_ledger",
    "watch_subscriptions",
)


def upgrade() -> None:
    """Install one owner-controlled erasure path and fence every tenant write family."""
    _require_bypassrls_definer()
    op.execute(
        """
        ALTER TABLE public.registration_action_audit
        ADD COLUMN pii_shredded_at timestamptz
        """
    )
    _create_request_table()
    _create_target_tables()
    _create_workflow_registry()
    _create_write_fence()
    _create_begin_capability()
    _create_stage_capability()
    _create_finalize_capability()
    _create_worker_capabilities()
    _grant_capabilities()


def downgrade() -> None:
    """Remove the capability without attempting to reconstruct irreversibly erased data."""
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_release_account_erasure_lease{_RELEASE_SIGNATURE}"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_renew_account_erasure_lease{_RENEW_SIGNATURE}"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_claim_account_erasure_batch{_CLAIM_SIGNATURE}"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_finalize_account_erasure{_FINALIZE_SIGNATURE}"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_complete_account_erasure_stage{_STAGE_SIGNATURE}"
    )
    op.execute(f"DROP FUNCTION IF EXISTS public.fn_begin_account_erasure{_BEGIN_SIGNATURE}")
    for table in _FENCED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS tr_account_erasure_write_fence ON public.{table}")
    op.execute("DROP FUNCTION IF EXISTS public.fn_fence_account_erasure_write()")
    op.execute("DROP TABLE IF EXISTS public.account_erasure_calendar_targets")
    op.execute("DROP TABLE IF EXISTS public.account_erasure_workflow_targets")
    op.execute("DROP TABLE IF EXISTS public.tenant_workflow_registry")
    op.execute("DROP TABLE IF EXISTS public.account_erasure_requests")
    op.execute(
        "ALTER TABLE public.registration_action_audit DROP COLUMN IF EXISTS pii_shredded_at"
    )


def _create_request_table() -> None:
    """Persist only opaque command identity, aggregate counts, and stage timestamps."""
    op.execute(
        """
        CREATE TABLE public.account_erasure_requests (
            tenant_id                    uuid PRIMARY KEY,
            request_id                   uuid NOT NULL UNIQUE,
            status                       text NOT NULL DEFAULT 'erasing'
                                         CHECK (status IN ('erasing', 'completed')),
            requested_at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
            workflow_target_count        integer NOT NULL CHECK (workflow_target_count >= 0),
            calendar_target_count        integer NOT NULL CHECK (calendar_target_count >= 0),
            calendar_binding_expected    boolean NOT NULL,
            external_effects_drained_at  timestamptz,
            workflows_cancelled_at       timestamptz,
            calendar_purged_at           timestamptz,
            browser_sessions_revoked_at  timestamptz,
            credential_vault_purged_at   timestamptz,
            object_store_purged_at       timestamptz,
            completed_at                 timestamptz,
            retained_audit_rows          integer NOT NULL DEFAULT 0
                                         CHECK (retained_audit_rows >= 0),
            attempt_count                integer NOT NULL DEFAULT 0
                                         CHECK (attempt_count >= 0),
            next_attempt_at              timestamptz NOT NULL
                                         DEFAULT (clock_timestamp() + INTERVAL '30 seconds'),
            lease_token                  uuid,
            lease_expires_at             timestamptz,
            last_failure_stage           text
                                         CHECK (last_failure_stage IS NULL OR last_failure_stage IN (
                                             'external_effects', 'workflows', 'calendar', 'browser_sessions',
                                             'credential_vault', 'object_store', 'database'
                                         )),
            UNIQUE (tenant_id, request_id),
            CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
            CHECK (
                (
                    status = 'erasing'
                    AND completed_at IS NULL
                )
                OR (
                    status = 'completed'
                    AND external_effects_drained_at IS NOT NULL
                    AND workflows_cancelled_at IS NOT NULL
                    AND calendar_purged_at IS NOT NULL
                    AND browser_sessions_revoked_at IS NOT NULL
                    AND credential_vault_purged_at IS NOT NULL
                    AND object_store_purged_at IS NOT NULL
                    AND completed_at IS NOT NULL
                    AND lease_token IS NULL
                    AND lease_expires_at IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_account_erasure_requests_due
        ON public.account_erasure_requests (next_attempt_at, requested_at)
        WHERE status = 'erasing'
        """
    )
    op.execute("ALTER TABLE public.account_erasure_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.account_erasure_requests FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY account_erasure_requests_tenant_isolation
        ON public.account_erasure_requests
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    op.execute("REVOKE ALL ON TABLE public.account_erasure_requests FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.account_erasure_requests FROM ec_app")
    op.execute("GRANT SELECT ON TABLE public.account_erasure_requests TO ec_app")


def _create_target_tables() -> None:
    """Snapshot tenant/system associations only until external cleanup is acknowledged."""
    op.execute(
        """
        CREATE TABLE public.account_erasure_workflow_targets (
            tenant_id   uuid NOT NULL,
            request_id  uuid NOT NULL,
            workflow_id text NOT NULL,
            PRIMARY KEY (tenant_id, workflow_id),
            FOREIGN KEY (tenant_id, request_id)
                REFERENCES public.account_erasure_requests (tenant_id, request_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.account_erasure_calendar_targets (
            tenant_id          uuid NOT NULL,
            request_id         uuid NOT NULL,
            canonical_event_id uuid NOT NULL,
            PRIMARY KEY (tenant_id, canonical_event_id),
            FOREIGN KEY (tenant_id, request_id)
                REFERENCES public.account_erasure_requests (tenant_id, request_id)
                ON DELETE CASCADE
        )
        """
    )
    for table in (
        "account_erasure_workflow_targets",
        "account_erasure_calendar_targets",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation
            ON public.{table}
            USING ({_TENANT_POLICY})
            WITH CHECK ({_TENANT_POLICY})
            """
        )
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM ec_app")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO ec_app")


def _create_workflow_registry() -> None:
    """Persist child identity before Temporal can start an ABANDON-policy execution."""
    op.execute(
        """
        CREATE TABLE public.tenant_workflow_registry (
            tenant_id  uuid NOT NULL REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            workflow_id text NOT NULL CHECK (btrim(workflow_id) <> ''),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, workflow_id)
        )
        """
    )
    op.execute("ALTER TABLE public.tenant_workflow_registry ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenant_workflow_registry FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_workflow_registry_tenant_isolation
        ON public.tenant_workflow_registry
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    op.execute("REVOKE ALL ON TABLE public.tenant_workflow_registry FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.tenant_workflow_registry FROM ec_app")
    op.execute("GRANT SELECT, INSERT ON TABLE public.tenant_workflow_registry TO ec_app")


def _create_write_fence() -> None:
    """Serialize tenant writes with erasure start and reject every post-fence mutation."""
    op.execute(
        """
        CREATE FUNCTION public.fn_fence_account_erasure_write()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_new_tenant_id uuid;
            v_old_tenant_id uuid;
            v_new_fenced boolean := false;
            v_old_fenced boolean := false;
            v_safe_audit_shred boolean := false;
        BEGIN
            IF TG_OP <> 'DELETE' THEN
                v_new_tenant_id := NULLIF(to_jsonb(NEW) ->> 'tenant_id', '')::uuid;
            END IF;
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                v_old_tenant_id := NULLIF(to_jsonb(OLD) ->> 'tenant_id', '')::uuid;
            END IF;

            IF v_new_tenant_id IS NOT NULL THEN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('account-erasure:' || v_new_tenant_id::text, 0)
                );
                SELECT EXISTS (
                    SELECT 1
                    FROM public.account_erasure_requests AS erasure
                    WHERE erasure.tenant_id = v_new_tenant_id
                ) INTO v_new_fenced;
            END IF;
            IF v_old_tenant_id IS NOT NULL
               AND v_old_tenant_id IS DISTINCT FROM v_new_tenant_id THEN
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('account-erasure:' || v_old_tenant_id::text, 0)
                );
                SELECT EXISTS (
                    SELECT 1
                    FROM public.account_erasure_requests AS erasure
                    WHERE erasure.tenant_id = v_old_tenant_id
                ) INTO v_old_fenced;
            ELSIF v_old_tenant_id IS NOT NULL THEN
                v_old_fenced := v_new_fenced;
            END IF;

            IF NOT v_new_fenced AND NOT v_old_fenced THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;

            -- The immutable target snapshots make post-fence deletes safe: they only remove live
            -- state while the worker continues to address the exact pre-delete external targets.
            -- DELETE still takes the advisory lock, so a pre-fence delete either commits before
            -- inventory capture or waits until the snapshot is durable.
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;

            -- The erasure capability may only clear an audit consent reference and stamp the
            -- shred time. No operational audit identity or decision field can change here.
            IF TG_TABLE_NAME = 'registration_action_audit' AND TG_OP = 'UPDATE' THEN
                v_safe_audit_shred := (
                    (to_jsonb(NEW) - 'consent_ref' - 'pii_shredded_at')
                    = (to_jsonb(OLD) - 'consent_ref' - 'pii_shredded_at')
                    AND to_jsonb(NEW) -> 'consent_ref' = 'null'::jsonb
                    AND to_jsonb(NEW) -> 'pii_shredded_at' <> 'null'::jsonb
                );
                IF v_safe_audit_shred THEN
                    RETURN NEW;
                END IF;
            END IF;

            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'tenant account is fenced for erasure';
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_fence_account_erasure_write() FROM PUBLIC"
    )
    for table in _FENCED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER tr_account_erasure_write_fence
            BEFORE INSERT OR UPDATE OR DELETE ON public.{table}
            FOR EACH ROW
            EXECUTE FUNCTION public.fn_fence_account_erasure_write()
            """
        )


def _create_begin_capability() -> None:
    """Atomically bind the request, engage policy, capture inventory, and shred audit PII."""
    op.execute(
        """
        CREATE FUNCTION public.fn_begin_account_erasure(p_request_id uuid)
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_tenant_id uuid;
            v_existing_request_id uuid;
            v_workflow_count integer;
            v_calendar_count integer;
            v_calendar_binding_expected boolean;
            v_retained_audit_rows integer;
        BEGIN
            v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_tenant_id IS NULL OR p_request_id IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'account erasure requires authenticated tenant context and request id';
            END IF;
            PERFORM pg_advisory_xact_lock(
                hashtextextended('account-erasure:' || v_tenant_id::text, 0)
            );

            SELECT erasure.request_id
            INTO v_existing_request_id
            FROM public.account_erasure_requests AS erasure
            WHERE erasure.tenant_id = v_tenant_id;
            IF FOUND THEN
                IF v_existing_request_id IS DISTINCT FROM p_request_id THEN
                    RETURN 'conflict';
                END IF;
                RETURN 'replayed';
            END IF;

            PERFORM 1
            FROM public.tenants AS tenant
            WHERE tenant.tenant_id = v_tenant_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;

            -- Stop every new policy-gated mutation before exposing the durable fence. The same
            -- transaction commits both facts, so no caller can observe only one of them.
            INSERT INTO public.tenant_policy_control (tenant_id, kill_switch)
            VALUES (v_tenant_id, true)
            ON CONFLICT (tenant_id) DO UPDATE
            SET kill_switch = true,
                updated_at = clock_timestamp();

            SELECT count(*)::integer
            INTO v_retained_audit_rows
            FROM public.registration_action_audit AS audit
            WHERE audit.tenant_id = v_tenant_id;

            SELECT EXISTS (
                SELECT 1
                FROM public.calendar_bindings AS binding
                WHERE binding.tenant_id = v_tenant_id
            ) INTO v_calendar_binding_expected;

            INSERT INTO public.account_erasure_requests (
                tenant_id,
                request_id,
                workflow_target_count,
                calendar_target_count,
                calendar_binding_expected,
                retained_audit_rows
            ) VALUES (
                v_tenant_id,
                p_request_id,
                0,
                0,
                v_calendar_binding_expected,
                v_retained_audit_rows
            );

            -- Persist the cross-system associations themselves, not just counts. They are
            -- derived from accepted identity schemes at the destructive boundary and survive
            -- any later live-row delete until final database convergence.
            INSERT INTO public.account_erasure_workflow_targets (
                tenant_id, request_id, workflow_id
            )
            SELECT v_tenant_id, p_request_id, target.workflow_id
            FROM (
                SELECT lifecycle.tenant_id::text || ':'
                       || lifecycle.canonical_event_id::text AS workflow_id
                FROM public.lifecycle AS lifecycle
                WHERE lifecycle.tenant_id = v_tenant_id
                UNION
                SELECT 'req:' || request.tenant_id::text || ':' || request.request_id::text
                FROM public.event_requests AS request
                WHERE request.tenant_id = v_tenant_id
                UNION
                SELECT registry.workflow_id
                FROM public.tenant_workflow_registry AS registry
                WHERE registry.tenant_id = v_tenant_id
            ) AS target;

            INSERT INTO public.account_erasure_calendar_targets (
                tenant_id, request_id, canonical_event_id
            )
            SELECT v_tenant_id, p_request_id, lifecycle.canonical_event_id
            FROM public.lifecycle AS lifecycle
            WHERE lifecycle.tenant_id = v_tenant_id
            ON CONFLICT DO NOTHING;

            SELECT count(*)::integer INTO v_workflow_count
            FROM public.account_erasure_workflow_targets AS target
            WHERE target.tenant_id = v_tenant_id;
            SELECT count(*)::integer INTO v_calendar_count
            FROM public.account_erasure_calendar_targets AS target
            WHERE target.tenant_id = v_tenant_id;
            UPDATE public.account_erasure_requests
            SET workflow_target_count = v_workflow_count,
                calendar_target_count = v_calendar_count
            WHERE tenant_id = v_tenant_id;

            -- Remove queued/future parent starts after their deterministic IDs are captured. A
            -- relay that leased before this transaction must re-check the now-absent exact row;
            -- even an already-paused external start cannot pass the fenced pre-child registry.
            DELETE FROM public.request_start_outbox
            WHERE tenant_id = v_tenant_id;

            -- ``PostgresTenantEffectAuthority`` holds this same advisory lock across provider I/O.
            -- Consequently every visible notification is fully before this erasure boundary;
            -- all queued/leased notification work is revoked atomically here and no send can
            -- start after the tombstone becomes visible.
            DELETE FROM public.notification_ledger
            WHERE tenant_id = v_tenant_id;
            DELETE FROM public.outbox
            WHERE tenant_id = v_tenant_id;

            UPDATE public.registration_action_audit AS audit
            SET consent_ref = NULL,
                pii_shredded_at = clock_timestamp()
            WHERE audit.tenant_id = v_tenant_id
              AND (audit.consent_ref IS NOT NULL OR audit.pii_shredded_at IS NULL);
            RETURN 'started';
        END;
        $$
        """
    )


def _create_stage_capability() -> None:
    """Record a whole external family only when its replay count matches captured inventory."""
    op.execute(
        """
        CREATE FUNCTION public.fn_complete_account_erasure_stage(
            p_request_id uuid,
            p_stage text,
            p_completed_count integer
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_tenant_id uuid;
            v_request public.account_erasure_requests%ROWTYPE;
            v_expected_count integer;
            v_completed_at timestamptz;
        BEGIN
            v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_tenant_id IS NULL
               OR p_request_id IS NULL
               OR p_stage NOT IN (
                   'external_effects', 'workflows', 'calendar', 'browser_sessions',
                   'credential_vault', 'object_store'
               )
               OR p_completed_count IS NULL
               OR p_completed_count < 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'account erasure stage input is invalid';
            END IF;
            PERFORM pg_advisory_xact_lock(
                hashtextextended('account-erasure:' || v_tenant_id::text, 0)
            );
            SELECT *
            INTO v_request
            FROM public.account_erasure_requests AS erasure
            WHERE erasure.tenant_id = v_tenant_id
            FOR UPDATE;
            IF NOT FOUND OR v_request.request_id IS DISTINCT FROM p_request_id THEN
                RETURN 'conflict';
            END IF;

            v_expected_count := CASE p_stage
                WHEN 'workflows' THEN v_request.workflow_target_count
                WHEN 'calendar' THEN v_request.calendar_target_count
                ELSE 1
            END;
            IF p_completed_count IS DISTINCT FROM v_expected_count THEN
                RETURN 'conflict';
            END IF;
            v_completed_at := CASE p_stage
                WHEN 'external_effects' THEN v_request.external_effects_drained_at
                WHEN 'workflows' THEN v_request.workflows_cancelled_at
                WHEN 'calendar' THEN v_request.calendar_purged_at
                WHEN 'browser_sessions' THEN v_request.browser_sessions_revoked_at
                WHEN 'credential_vault' THEN v_request.credential_vault_purged_at
                WHEN 'object_store' THEN v_request.object_store_purged_at
            END;
            IF v_completed_at IS NOT NULL THEN
                RETURN 'completed';
            END IF;

            UPDATE public.account_erasure_requests AS erasure
            SET external_effects_drained_at = CASE
                    WHEN p_stage = 'external_effects' THEN clock_timestamp()
                    ELSE erasure.external_effects_drained_at
                END,
                workflows_cancelled_at = CASE
                    WHEN p_stage = 'workflows' THEN clock_timestamp()
                    ELSE erasure.workflows_cancelled_at
                END,
                calendar_purged_at = CASE
                    WHEN p_stage = 'calendar' THEN clock_timestamp()
                    ELSE erasure.calendar_purged_at
                END,
                browser_sessions_revoked_at = CASE
                    WHEN p_stage = 'browser_sessions' THEN clock_timestamp()
                    ELSE erasure.browser_sessions_revoked_at
                END,
                credential_vault_purged_at = CASE
                    WHEN p_stage = 'credential_vault' THEN clock_timestamp()
                    ELSE erasure.credential_vault_purged_at
                END,
                object_store_purged_at = CASE
                    WHEN p_stage = 'object_store' THEN clock_timestamp()
                    ELSE erasure.object_store_purged_at
                END
            WHERE erasure.tenant_id = v_tenant_id;
            RETURN 'completed';
        END;
        $$
        """
    )


def _create_finalize_capability() -> None:
    """Delete every database tenant-data family in one transaction after external convergence."""
    op.execute(
        """
        CREATE FUNCTION public.fn_finalize_account_erasure(p_request_id uuid)
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_tenant_id uuid;
            v_request public.account_erasure_requests%ROWTYPE;
            v_audit_count integer;
        BEGIN
            v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_tenant_id IS NULL OR p_request_id IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'account erasure finalization requires tenant context';
            END IF;
            PERFORM pg_advisory_xact_lock(
                hashtextextended('account-erasure:' || v_tenant_id::text, 0)
            );
            SELECT *
            INTO v_request
            FROM public.account_erasure_requests AS erasure
            WHERE erasure.tenant_id = v_tenant_id
            FOR UPDATE;
            IF NOT FOUND OR v_request.request_id IS DISTINCT FROM p_request_id THEN
                RETURN 'conflict';
            END IF;
            IF v_request.status = 'completed' THEN
                RETURN 'completed';
            END IF;
            IF v_request.external_effects_drained_at IS NULL
               OR v_request.workflows_cancelled_at IS NULL
               OR v_request.calendar_purged_at IS NULL
               OR v_request.browser_sessions_revoked_at IS NULL
               OR v_request.credential_vault_purged_at IS NULL
               OR v_request.object_store_purged_at IS NULL THEN
                RETURN 'pending';
            END IF;

            SELECT count(*)::integer
            INTO v_audit_count
            FROM public.registration_action_audit AS audit
            WHERE audit.tenant_id = v_tenant_id
              AND audit.consent_ref IS NULL
              AND audit.pii_shredded_at IS NOT NULL;
            IF v_audit_count IS DISTINCT FROM v_request.retained_audit_rows THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'account erasure audit shred invariant failed';
            END IF;

            -- Relations without tenant FKs are explicit. Remaining tenant-owned relations cascade
            -- from their request/lifecycle/task/tenant roots. Public catalog/change facts survive.
            DELETE FROM public.account_erasure_calendar_targets
            WHERE tenant_id = v_tenant_id;
            DELETE FROM public.account_erasure_workflow_targets
            WHERE tenant_id = v_tenant_id;
            DELETE FROM public.notification_ledger WHERE tenant_id = v_tenant_id;
            DELETE FROM public.outbox WHERE tenant_id = v_tenant_id;
            DELETE FROM public.transition_ledger WHERE tenant_id = v_tenant_id;
            DELETE FROM public.handoff_tasks WHERE tenant_id = v_tenant_id;
            DELETE FROM public.lifecycle WHERE tenant_id = v_tenant_id;
            DELETE FROM public.event_requests WHERE tenant_id = v_tenant_id;
            DELETE FROM public.tenants WHERE tenant_id = v_tenant_id;

            UPDATE public.account_erasure_requests
            SET status = 'completed',
                completed_at = clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_failure_stage = NULL
            WHERE tenant_id = v_tenant_id;
            RETURN 'completed';
        END;
        $$
        """
    )


def _create_worker_capabilities() -> None:
    """Expose a bounded cross-tenant lease queue without granting table mutation rights."""
    op.execute(
        """
        CREATE FUNCTION public.fn_claim_account_erasure_batch(
            p_limit integer,
            p_lease_seconds integer
        )
        RETURNS TABLE (
            tenant_id uuid,
            request_id uuid,
            attempt_count integer,
            lease_token uuid
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_lease_seconds IS NULL
               OR p_lease_seconds < 5 OR p_lease_seconds > 3600 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'account erasure claim input is invalid';
            END IF;

            RETURN QUERY
            WITH candidates AS (
                SELECT erasure.tenant_id
                FROM public.account_erasure_requests AS erasure
                WHERE erasure.status = 'erasing'
                  AND erasure.next_attempt_at <= clock_timestamp()
                  AND (
                      erasure.lease_expires_at IS NULL
                      OR erasure.lease_expires_at <= clock_timestamp()
                  )
                ORDER BY erasure.next_attempt_at, erasure.requested_at, erasure.tenant_id
                FOR UPDATE SKIP LOCKED
                LIMIT p_limit
            )
            UPDATE public.account_erasure_requests AS erasure
            SET attempt_count = erasure.attempt_count + 1,
                lease_token = gen_random_uuid(),
                lease_expires_at = clock_timestamp()
                    + (p_lease_seconds * INTERVAL '1 second'),
                last_failure_stage = NULL
            FROM candidates
            WHERE erasure.tenant_id = candidates.tenant_id
            RETURNING erasure.tenant_id, erasure.request_id, erasure.attempt_count,
                      erasure.lease_token;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_renew_account_erasure_lease(
            p_tenant_id uuid,
            p_request_id uuid,
            p_lease_token uuid,
            p_lease_seconds integer
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_tenant_id IS NULL OR p_request_id IS NULL OR p_lease_token IS NULL
               OR p_lease_seconds IS NULL
               OR p_lease_seconds < 5 OR p_lease_seconds > 3600 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'account erasure lease renewal input is invalid';
            END IF;
            UPDATE public.account_erasure_requests AS erasure
            SET lease_expires_at = clock_timestamp()
                    + (p_lease_seconds * INTERVAL '1 second')
            WHERE erasure.tenant_id = p_tenant_id
              AND erasure.request_id = p_request_id
              AND erasure.status = 'erasing'
              AND erasure.lease_token = p_lease_token
              AND erasure.lease_expires_at > clock_timestamp();
            RETURN FOUND;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_release_account_erasure_lease(
            p_tenant_id uuid,
            p_request_id uuid,
            p_lease_token uuid,
            p_failure_stage text,
            p_retry_after_seconds integer
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_tenant_id IS NULL OR p_request_id IS NULL OR p_lease_token IS NULL
               OR p_failure_stage NOT IN (
                   'external_effects', 'workflows', 'calendar', 'browser_sessions',
                   'credential_vault', 'object_store', 'database'
               )
               OR p_retry_after_seconds IS NULL
               OR p_retry_after_seconds < 1 OR p_retry_after_seconds > 3600 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'account erasure lease release input is invalid';
            END IF;

            UPDATE public.account_erasure_requests AS erasure
            SET next_attempt_at = clock_timestamp()
                    + (p_retry_after_seconds * INTERVAL '1 second'),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_failure_stage = p_failure_stage
            WHERE erasure.tenant_id = p_tenant_id
              AND erasure.request_id = p_request_id
              AND erasure.status = 'erasing'
              AND erasure.lease_token = p_lease_token
              AND erasure.lease_expires_at > clock_timestamp();
            RETURN FOUND;
        END;
        $$
        """
    )


def _grant_capabilities() -> None:
    """Expose only fixed-shape tenant-derived commands to the runtime role."""
    functions = (
        f"public.fn_begin_account_erasure{_BEGIN_SIGNATURE}",
        f"public.fn_complete_account_erasure_stage{_STAGE_SIGNATURE}",
        f"public.fn_finalize_account_erasure{_FINALIZE_SIGNATURE}",
        f"public.fn_claim_account_erasure_batch{_CLAIM_SIGNATURE}",
        f"public.fn_renew_account_erasure_lease{_RENEW_SIGNATURE}",
        f"public.fn_release_account_erasure_lease{_RELEASE_SIGNATURE}",
    )
    for function in functions:
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM ec_app")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO ec_app")


def _require_bypassrls_definer() -> None:
    """Fail migration if its function owner cannot operate across FORCE-RLS purge roots."""
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
                    MESSAGE = '0108 account erasure requires a superuser or BYPASSRLS migration owner';
            END IF;
        END;
        $$
        """
    )
