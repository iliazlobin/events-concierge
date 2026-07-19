"""Create the ADR-008 public change ledger and opaque workflow-fanout control plane.

Revision ID: 0048
Revises: 0047
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist one public event/source watch and fingerprinted change before tenant-workflow fanout.

    ``watch_registry`` and ``event_changes`` hold no tenant data.  ``watch_subscriptions`` and
    ``event_change_deliveries`` are global opaque control records, analogous to the start/outbox
    queues: they contain only tenant UUIDs and deterministic workflow ids, never user contact data,
    credentials, raw request text, or tenant event metadata.  They intentionally have no RLS so one
    central detector can fan a public event change out across tenants (FR-8.7a, ADR-008).
    """
    op.execute(
        """
        CREATE TABLE watch_registry (
            canonical_event_id  uuid NOT NULL REFERENCES canonical_events(canonical_event_id)
                                      ON DELETE CASCADE,
            source              text NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (canonical_event_id, source)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE watch_subscriptions (
            canonical_event_id  uuid NOT NULL,
            source              text NOT NULL,
            tenant_id           uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            workflow_id         text NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (canonical_event_id, source, tenant_id, workflow_id),
            FOREIGN KEY (canonical_event_id, source)
                REFERENCES watch_registry(canonical_event_id, source) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_watch_subscriptions_event_source
        ON watch_subscriptions (canonical_event_id, source)
        """
    )
    op.execute(
        """
        CREATE TABLE event_changes (
            fingerprint          text PRIMARY KEY,
            canonical_event_id   uuid NOT NULL REFERENCES canonical_events(canonical_event_id)
                                       ON DELETE CASCADE,
            source               text NOT NULL,
            event_status         text NOT NULL CHECK (event_status IN ('cancelled', 'rescheduled')),
            start_at              timestamptz,
            end_at                timestamptz,
            time_zone             text,
            title                 text,
            venue_name            text,
            detected_at           timestamptz NOT NULL DEFAULT now(),
            CHECK (event_status = 'cancelled' OR start_at IS NOT NULL),
            CHECK (end_at IS NULL OR start_at IS NULL OR end_at > start_at)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_event_changes_canonical_source_detected
        ON event_changes (canonical_event_id, source, detected_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE event_change_deliveries (
            fingerprint          text NOT NULL REFERENCES event_changes(fingerprint) ON DELETE CASCADE,
            tenant_id            uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            workflow_id          text NOT NULL,
            created_at           timestamptz NOT NULL DEFAULT now(),
            delivered_at         timestamptz,
            attempt_count        integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at      timestamptz NOT NULL DEFAULT now(),
            lease_token          text,
            lease_expires_at     timestamptz,
            last_error           text,
            PRIMARY KEY (fingerprint, tenant_id, workflow_id),
            CHECK (
                (lease_token IS NULL AND lease_expires_at IS NULL)
                OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_event_change_deliveries_ready
        ON event_change_deliveries (next_attempt_at, fingerprint, tenant_id, workflow_id)
        WHERE delivered_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_event_change_deliveries_tenant_workflow
        ON event_change_deliveries (tenant_id, workflow_id)
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_register_change_watch(
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

            SELECT lifecycle_id
            INTO v_lifecycle_id
            FROM public.lifecycle
            WHERE tenant_id = p_tenant_id
              AND workflow_id = p_workflow_id
              AND canonical_event_id = p_canonical_event_id
              AND state IN ('registered', 'scheduled', 'reconciled')
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

            -- ``record`` takes this same transaction-scoped lock.  It closes the interval where
            -- a lifecycle reaches an active state, a detector records its change, and the async
            -- lifecycle->watch projection has not yet created its subscription (ADR-008).
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

            -- Source identifies the feeder surface, not a fanout partition.  Backfill all
            -- already-detected changes for the canonical event so registering after a record
            -- cannot permanently lose the workflow delivery.  The shared advisory lock above
            -- guarantees concurrent ``record`` either observes this subscription or is observed
            -- here; the primary key provides idempotency for repeated registrations.
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
        """
        CREATE FUNCTION public.fn_unregister_change_watch(
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
            v_subscription_workflow_id text;
        BEGIN
            v_context_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_context_tenant_id IS NULL OR p_tenant_id IS DISTINCT FROM v_context_tenant_id THEN
                RETURN false;
            END IF;

            -- ``record`` and registration take this same lock.  Taking it before deletion and
            -- delivery retirement prevents a concurrent record from materializing a delivery
            -- after this terminal lifecycle has left the watch registry (ADR-008).
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

            -- A delivery is shared by any source watches for the same deterministic workflow.
            -- Retire it only after the last source subscription leaves, so a terminal workflow
            -- cannot hot-retry an already-queued/lost-ack organizer signal indefinitely.
            UPDATE public.event_change_deliveries AS delivery
            SET delivered_at = now(),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error = 'retired after lifecycle watch removal'
            WHERE delivery.tenant_id = p_tenant_id
              AND delivery.workflow_id = p_workflow_id
              AND delivery.delivered_at IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM public.event_changes AS change
                  WHERE change.fingerprint = delivery.fingerprint
                    AND change.canonical_event_id = p_canonical_event_id
              )
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
    # Lifecycle UPDATE is revoked from ec_app, so the functions hold its row lock as the migration
    # owner after explicitly checking the caller's RLS tenant context.  Direct subscription DML is
    # withheld for the same reason; the detector retains SELECT for global opaque fanout reads.
    # 0002 grants DML through schema default privileges.  The app role may inspect opaque watch
    # rows for the central detector, but lifecycle entry/exit must go through the guarded
    # SECURITY DEFINER functions above rather than direct mutable control-plane access.
    op.execute("REVOKE ALL PRIVILEGES ON watch_registry FROM ec_app")
    op.execute("REVOKE ALL PRIVILEGES ON watch_subscriptions FROM ec_app")
    op.execute("GRANT SELECT ON watch_registry TO ec_app")
    op.execute("GRANT SELECT ON watch_subscriptions TO ec_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON event_changes TO ec_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON event_change_deliveries TO ec_app")
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_register_change_watch(uuid, text, uuid, text, timestamptz) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_unregister_change_watch(uuid, text, uuid, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_register_change_watch(uuid, text, uuid, text, timestamptz) TO ec_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_unregister_change_watch(uuid, text, uuid, text) TO ec_app"
    )


def downgrade() -> None:
    """Remove the central detector control plane without altering lifecycle or catalog records."""
    op.execute("DROP FUNCTION IF EXISTS public.fn_unregister_change_watch(uuid, text, uuid, text)")
    op.execute(
        "DROP FUNCTION IF EXISTS public.fn_register_change_watch(uuid, text, uuid, text, timestamptz)"
    )
    # This four-argument cleanup supports a local migration rebuild after the in-progress 0048
    # revision changed its registration contract before release.
    op.execute("DROP FUNCTION IF EXISTS public.fn_register_change_watch(uuid, text, uuid, text)")
    op.execute("DROP TABLE IF EXISTS event_change_deliveries")
    op.execute("DROP TABLE IF EXISTS event_changes")
    op.execute("DROP TABLE IF EXISTS watch_subscriptions")
    op.execute("DROP TABLE IF EXISTS watch_registry")
