"""Guard tenant-bearing ADR-008 global control-plane mutations.

Revision ID: 0063
Revises: 0062
Create Date: 2026-07-18

The central detector intentionally spans tenants by distinct public event, but the ordinary app
role must not therefore receive arbitrary table mutation/read authority over opaque tenant/workflow
queues. Fixed-shape capabilities preserve record/lease/recovery behavior while preventing direct
fabrication, suppression, redirection, and foreign-key existence probing (NFR-7/NFR-8, ADR-007/008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace app-role table DML/read access with opaque guarded queue capabilities."""
    _create_event_change_capabilities()
    _create_calendar_repair_capabilities()
    _create_watch_projection_capabilities()
    _revoke_tables_and_grant_capabilities()


def downgrade() -> None:
    """Restore the prior direct app-role grants if this isolated hardening slice is rolled back."""
    for signature in (
        "public.fn_watch_projection_reschedule(bigint, text, text)",
        "public.fn_mark_watch_projection_delivered(bigint, text)",
        "public.fn_claim_watch_projections(integer, integer, text)",
        "public.fn_list_active_change_watches()",
        "public.fn_calendar_repair_change_applied(bigint, text)",
        "public.fn_reschedule_calendar_repair(bigint, text, text)",
        "public.fn_mark_calendar_repair(bigint, text)",
        "public.fn_claim_calendar_repairs(integer, integer, text)",
        "public.fn_enqueue_closed_workflow_calendar_repair(text, uuid, text, text, text)",
        "public.fn_release_event_change_delivery(text, uuid, text, text, text)",
        "public.fn_mark_event_change_delivery(text, uuid, text, text)",
        "public.fn_claim_event_change_deliveries(integer, integer, text)",
        "public.fn_record_event_change(text, uuid, text, text, timestamptz, timestamptz, text, text, text)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.event_changes TO ec_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.event_change_deliveries TO ec_app"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.event_change_calendar_repairs TO ec_app"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE public.event_change_calendar_repairs_repair_id_seq TO ec_app"
    )
    op.execute("GRANT SELECT ON TABLE public.watch_subscriptions TO ec_app")
    op.execute("GRANT SELECT ON TABLE public.lifecycle_organizer_change_ledger TO ec_app")
    op.execute("GRANT SELECT, UPDATE ON TABLE public.lifecycle_watch_projection_outbox TO ec_app")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE public.lifecycle_watch_projection_outbox_projection_id_seq TO ec_app"
    )


def _create_event_change_capabilities() -> None:
    """Install record and delivery lease functions with no arbitrary tenant-row mutation path."""
    op.execute(
        """
        CREATE FUNCTION public.fn_list_active_change_watches()
        RETURNS TABLE(
            canonical_event_id uuid,
            source text,
            created_at timestamptz,
            subscriber_count integer
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT registry.canonical_event_id,
                   registry.source,
                   registry.created_at,
                   count(subscription.workflow_id)::integer AS subscriber_count
            FROM public.watch_registry AS registry
            LEFT JOIN public.watch_subscriptions AS subscription
              ON subscription.canonical_event_id = registry.canonical_event_id
             AND subscription.source = registry.source
            GROUP BY registry.canonical_event_id, registry.source, registry.created_at
            HAVING count(subscription.workflow_id) > 0
            ORDER BY registry.canonical_event_id, registry.source
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_record_event_change(
            p_fingerprint text,
            p_canonical_event_id uuid,
            p_source text,
            p_event_status text,
            p_start_at timestamptz,
            p_end_at timestamptz,
            p_time_zone text,
            p_title text,
            p_venue_name text
        )
        RETURNS TABLE(inserted boolean, queued_deliveries integer)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_inserted text;
            v_queued integer := 0;
        BEGIN
            IF NULLIF(btrim(p_fingerprint), '') IS NULL
               OR p_canonical_event_id IS NULL
               OR p_source IS NULL
               OR p_source NOT IN (
                   'public_jsonld', 'meetup', 'ticketmaster', 'luma', 'eventbrite', 'partiful', 'serpapi'
               )
               OR p_event_status IS NULL
               OR p_event_status NOT IN ('cancelled', 'rescheduled')
               OR (p_event_status = 'rescheduled' AND p_start_at IS NULL)
               OR (p_start_at IS NOT NULL AND p_end_at IS NOT NULL AND p_end_at <= p_start_at)
               OR (
                   p_time_zone IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = p_time_zone
                   )
               )
               OR NOT EXISTS (
                   SELECT 1
                   FROM public.canonical_events AS event
                   WHERE event.canonical_event_id = p_canonical_event_id
               )
               OR NOT EXISTS (
                   SELECT 1
                   FROM public.event_source_links AS link
                   WHERE link.canonical_event_id = p_canonical_event_id
                     AND link.source = p_source
               )
            THEN
                RETURN QUERY SELECT false, 0;
                RETURN;
            END IF;

            -- Shared with lifecycle watch registration, so registration either observes this
            -- public change or backfills it under the same transaction-scoped identity lock.
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtext(p_canonical_event_id::text)::bigint
            );

            INSERT INTO public.event_changes
                (fingerprint, canonical_event_id, source, event_status, start_at, end_at,
                 time_zone, title, venue_name, detected_at)
            VALUES
                (p_fingerprint, p_canonical_event_id, p_source, p_event_status, p_start_at,
                 p_end_at, p_time_zone, p_title, p_venue_name, pg_catalog.clock_timestamp())
            ON CONFLICT (fingerprint) DO NOTHING
            RETURNING fingerprint INTO v_inserted;
            IF v_inserted IS NULL THEN
                RETURN QUERY SELECT false, 0;
                RETURN;
            END IF;

            INSERT INTO public.event_change_deliveries
                (fingerprint, tenant_id, workflow_id)
            SELECT p_fingerprint, subscription.tenant_id, subscription.workflow_id
            FROM public.watch_subscriptions AS subscription
            WHERE subscription.canonical_event_id = p_canonical_event_id
            ON CONFLICT (fingerprint, tenant_id, workflow_id) DO NOTHING;
            GET DIAGNOSTICS v_queued = ROW_COUNT;
            RETURN QUERY SELECT true, v_queued;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_claim_event_change_deliveries(
            p_limit integer,
            p_lease_seconds integer,
            p_lease_token text
        )
        RETURNS TABLE(
            fingerprint text,
            canonical_event_id uuid,
            source text,
            event_status text,
            start_at timestamptz,
            end_at timestamptz,
            time_zone text,
            title text,
            venue_name text,
            tenant_id uuid,
            workflow_id text,
            attempt_count integer,
            lease_token text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000
               OR p_lease_seconds IS NULL OR p_lease_seconds < 1 OR p_lease_seconds > 3600
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR length(p_lease_token) > 128
            THEN
                RETURN;
            END IF;

            RETURN QUERY
            WITH candidates AS (
                SELECT delivery.fingerprint, delivery.tenant_id, delivery.workflow_id
                FROM public.event_change_deliveries AS delivery
                WHERE delivery.delivered_at IS NULL
                  AND delivery.next_attempt_at <= pg_catalog.clock_timestamp()
                  AND (
                      delivery.lease_expires_at IS NULL
                      OR delivery.lease_expires_at <= pg_catalog.clock_timestamp()
                  )
                ORDER BY delivery.created_at, delivery.fingerprint, delivery.tenant_id,
                         delivery.workflow_id
                FOR UPDATE SKIP LOCKED
                LIMIT p_limit
            ), claimed AS (
                UPDATE public.event_change_deliveries AS delivery
                SET lease_token = p_lease_token,
                    lease_expires_at = pg_catalog.clock_timestamp()
                        + (p_lease_seconds * INTERVAL '1 second'),
                    attempt_count = delivery.attempt_count + 1
                FROM candidates AS candidate
                WHERE delivery.fingerprint = candidate.fingerprint
                  AND delivery.tenant_id = candidate.tenant_id
                  AND delivery.workflow_id = candidate.workflow_id
                RETURNING delivery.fingerprint, delivery.tenant_id, delivery.workflow_id,
                          delivery.attempt_count, delivery.lease_token
            )
            SELECT change.fingerprint, change.canonical_event_id, change.source,
                   change.event_status, change.start_at, change.end_at, change.time_zone,
                   change.title, change.venue_name, claimed.tenant_id, claimed.workflow_id,
                   claimed.attempt_count, claimed.lease_token
            FROM claimed
            JOIN public.event_changes AS change
              ON change.fingerprint = claimed.fingerprint;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_mark_event_change_delivery(
            p_fingerprint text,
            p_tenant_id uuid,
            p_workflow_id text,
            p_lease_token text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_updated integer;
        BEGIN
            IF NULLIF(btrim(p_fingerprint), '') IS NULL
               OR p_tenant_id IS NULL
               OR NULLIF(btrim(p_workflow_id), '') IS NULL
               OR NULLIF(btrim(p_lease_token), '') IS NULL THEN
                RETURN false;
            END IF;
            UPDATE public.event_change_deliveries AS delivery
            SET delivered_at = pg_catalog.clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error = NULL
            WHERE delivery.fingerprint = p_fingerprint
              AND delivery.tenant_id = p_tenant_id
              AND delivery.workflow_id = p_workflow_id
              AND delivery.lease_token = p_lease_token
              AND delivery.delivered_at IS NULL;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_release_event_change_delivery(
            p_fingerprint text,
            p_tenant_id uuid,
            p_workflow_id text,
            p_lease_token text,
            p_error text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_updated integer;
        BEGIN
            IF NULLIF(btrim(p_fingerprint), '') IS NULL
               OR p_tenant_id IS NULL
               OR NULLIF(btrim(p_workflow_id), '') IS NULL
               OR NULLIF(btrim(p_lease_token), '') IS NULL THEN
                RETURN false;
            END IF;
            UPDATE public.event_change_deliveries AS delivery
            SET lease_token = NULL,
                lease_expires_at = NULL,
                next_attempt_at = pg_catalog.clock_timestamp()
                    + (
                        LEAST(
                            300,
                            1::integer << LEAST(GREATEST(delivery.attempt_count - 1, 0), 9)
                        ) * INTERVAL '1 second'
                    ),
                last_error = left(p_error, 1000)
            WHERE delivery.fingerprint = p_fingerprint
              AND delivery.tenant_id = p_tenant_id
              AND delivery.workflow_id = p_workflow_id
              AND delivery.lease_token = p_lease_token
              AND delivery.delivered_at IS NULL;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )


def _create_calendar_repair_capabilities() -> None:
    """Install closed-workflow repair functions fenced by the exact queue lease."""
    op.execute(
        """
        CREATE FUNCTION public.fn_enqueue_closed_workflow_calendar_repair(
            p_fingerprint text,
            p_tenant_id uuid,
            p_workflow_id text,
            p_lease_token text,
            p_error text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_fingerprint text;
            v_tenant_id uuid;
            v_workflow_id text;
        BEGIN
            IF NULLIF(btrim(p_fingerprint), '') IS NULL
               OR p_tenant_id IS NULL
               OR NULLIF(btrim(p_workflow_id), '') IS NULL
               OR NULLIF(btrim(p_lease_token), '') IS NULL THEN
                RETURN false;
            END IF;
            UPDATE public.event_change_deliveries AS delivery
            SET delivered_at = pg_catalog.clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error = left(p_error, 1000)
            WHERE delivery.fingerprint = p_fingerprint
              AND delivery.tenant_id = p_tenant_id
              AND delivery.workflow_id = p_workflow_id
              AND delivery.lease_token = p_lease_token
              AND delivery.delivered_at IS NULL
            RETURNING delivery.fingerprint, delivery.tenant_id, delivery.workflow_id
            INTO v_fingerprint, v_tenant_id, v_workflow_id;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            INSERT INTO public.event_change_calendar_repairs
                (fingerprint, tenant_id, workflow_id, last_error)
            VALUES (v_fingerprint, v_tenant_id, v_workflow_id, left(p_error, 1000))
            ON CONFLICT (fingerprint, tenant_id, workflow_id) DO NOTHING;
            RETURN true;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_claim_calendar_repairs(
            p_limit integer,
            p_lease_seconds integer,
            p_lease_token text
        )
        RETURNS TABLE(
            repair_id bigint,
            fingerprint text,
            canonical_event_id uuid,
            source text,
            event_status text,
            start_at timestamptz,
            end_at timestamptz,
            time_zone text,
            title text,
            venue_name text,
            tenant_id uuid,
            workflow_id text,
            attempt_count integer,
            lease_token text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000
               OR p_lease_seconds IS NULL OR p_lease_seconds < 1 OR p_lease_seconds > 3600
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR length(p_lease_token) > 128 THEN
                RETURN;
            END IF;
            RETURN QUERY
            WITH candidates AS (
                SELECT repair.repair_id
                FROM public.event_change_calendar_repairs AS repair
                WHERE repair.repaired_at IS NULL
                  AND repair.next_attempt_at <= pg_catalog.clock_timestamp()
                  AND (
                      repair.lease_expires_at IS NULL
                      OR repair.lease_expires_at <= pg_catalog.clock_timestamp()
                  )
                ORDER BY repair.next_attempt_at, repair.repair_id
                FOR UPDATE SKIP LOCKED
                LIMIT p_limit
            ), claimed AS (
                UPDATE public.event_change_calendar_repairs AS repair
                SET lease_token = p_lease_token,
                    lease_expires_at = pg_catalog.clock_timestamp()
                        + (p_lease_seconds * INTERVAL '1 second'),
                    attempt_count = repair.attempt_count + 1
                FROM candidates
                WHERE repair.repair_id = candidates.repair_id
                RETURNING repair.repair_id, repair.fingerprint, repair.tenant_id,
                          repair.workflow_id, repair.attempt_count, repair.lease_token
            )
            SELECT claimed.repair_id, change.fingerprint, change.canonical_event_id,
                   change.source, change.event_status, change.start_at, change.end_at,
                   change.time_zone, change.title, change.venue_name, claimed.tenant_id,
                   claimed.workflow_id, claimed.attempt_count, claimed.lease_token
            FROM claimed
            JOIN public.event_changes AS change
              ON change.fingerprint = claimed.fingerprint;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_mark_calendar_repair(p_repair_id bigint, p_lease_token text)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_updated integer;
        BEGIN
            IF p_repair_id < 1 OR NULLIF(btrim(p_lease_token), '') IS NULL THEN
                RETURN false;
            END IF;
            UPDATE public.event_change_calendar_repairs AS repair
            SET repaired_at = pg_catalog.clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error = NULL
            WHERE repair.repair_id = p_repair_id
              AND repair.lease_token = p_lease_token
              AND repair.repaired_at IS NULL;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_reschedule_calendar_repair(
            p_repair_id bigint,
            p_lease_token text,
            p_error text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_updated integer;
        BEGIN
            IF p_repair_id < 1
               OR NULLIF(btrim(p_lease_token), '') IS NULL THEN
                RETURN false;
            END IF;
            UPDATE public.event_change_calendar_repairs AS repair
            SET lease_token = NULL,
                lease_expires_at = NULL,
                next_attempt_at = pg_catalog.clock_timestamp()
                    + (
                        LEAST(
                            300,
                            1::integer << LEAST(GREATEST(repair.attempt_count - 1, 0), 9)
                        ) * INTERVAL '1 second'
                    ),
                last_error = left(p_error, 1000)
            WHERE repair.repair_id = p_repair_id
              AND repair.lease_token = p_lease_token
              AND repair.repaired_at IS NULL;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_calendar_repair_change_applied(
            p_repair_id bigint,
            p_lease_token text
        )
        RETURNS boolean
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT EXISTS (
                SELECT 1
                FROM public.event_change_calendar_repairs AS repair
                JOIN public.lifecycle_organizer_change_ledger AS ledger
                  ON ledger.tenant_id = repair.tenant_id
                 AND ledger.workflow_id = repair.workflow_id
                 AND ledger.fingerprint = repair.fingerprint
                WHERE repair.repair_id = p_repair_id
                  AND repair.lease_token = p_lease_token
                  AND repair.repaired_at IS NULL
            )
        $$
        """
    )


def _create_watch_projection_capabilities() -> None:
    """Install the same lease-only boundary for tenant-bearing lifecycle watch projections."""
    op.execute(
        """
        CREATE FUNCTION public.fn_claim_watch_projections(
            p_limit integer,
            p_lease_seconds integer,
            p_lease_token text
        )
        RETURNS TABLE(
            projection_id bigint,
            tenant_id uuid,
            workflow_id text,
            canonical_event_id uuid,
            source text,
            action text,
            active_since timestamptz,
            attempt_count integer,
            lease_token text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000
               OR p_lease_seconds IS NULL OR p_lease_seconds < 1 OR p_lease_seconds > 3600
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR length(p_lease_token) > 128 THEN
                RETURN;
            END IF;
            RETURN QUERY
            WITH candidates AS (
                SELECT projection.projection_id
                FROM public.lifecycle_watch_projection_outbox AS projection
                WHERE projection.delivered_at IS NULL
                  AND projection.next_attempt_at <= pg_catalog.clock_timestamp()
                  AND (
                      projection.lease_expires_at IS NULL
                      OR projection.lease_expires_at <= pg_catalog.clock_timestamp()
                  )
                ORDER BY projection.created_at, projection.projection_id
                FOR UPDATE SKIP LOCKED
                LIMIT p_limit
            )
            UPDATE public.lifecycle_watch_projection_outbox AS projection
            SET lease_token = p_lease_token,
                lease_expires_at = pg_catalog.clock_timestamp()
                    + (p_lease_seconds * INTERVAL '1 second'),
                attempt_count = projection.attempt_count + 1
            FROM candidates
            WHERE projection.projection_id = candidates.projection_id
            RETURNING projection.projection_id, projection.tenant_id, projection.workflow_id,
                      projection.canonical_event_id, projection.source, projection.action,
                      projection.active_since, projection.attempt_count, projection.lease_token;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_mark_watch_projection_delivered(
            p_projection_id bigint,
            p_lease_token text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_updated integer;
        BEGIN
            IF p_projection_id < 1 OR NULLIF(btrim(p_lease_token), '') IS NULL THEN
                RETURN false;
            END IF;
            UPDATE public.lifecycle_watch_projection_outbox AS projection
            SET delivered_at = pg_catalog.clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error = NULL
            WHERE projection.projection_id = p_projection_id
              AND projection.lease_token = p_lease_token
              AND projection.delivered_at IS NULL;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_watch_projection_reschedule(
            p_projection_id bigint,
            p_lease_token text,
            p_error text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_updated integer;
        BEGIN
            IF p_projection_id < 1 OR NULLIF(btrim(p_lease_token), '') IS NULL THEN
                RETURN false;
            END IF;
            UPDATE public.lifecycle_watch_projection_outbox AS projection
            SET lease_token = NULL,
                lease_expires_at = NULL,
                next_attempt_at = pg_catalog.clock_timestamp()
                    + (
                        LEAST(300, 2 * (1 << LEAST(projection.attempt_count, 7)))
                        * INTERVAL '1 second'
                    ),
                last_error = left(p_error, 1000)
            WHERE projection.projection_id = p_projection_id
              AND projection.lease_token = p_lease_token
              AND projection.delivered_at IS NULL;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )


def _revoke_tables_and_grant_capabilities() -> None:
    """Leave ec_app only the exact executable capabilities and no tenant-bearing raw reads."""
    for table in (
        "public.event_changes",
        "public.event_change_deliveries",
        "public.event_change_calendar_repairs",
        "public.lifecycle_organizer_change_ledger",
        "public.lifecycle_watch_projection_outbox",
        "public.watch_subscriptions",
    ):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM ec_app")

    for sequence in (
        "public.event_change_calendar_repairs_repair_id_seq",
        "public.lifecycle_watch_projection_outbox_projection_id_seq",
    ):
        op.execute(f"REVOKE ALL PRIVILEGES ON SEQUENCE {sequence} FROM PUBLIC")
        op.execute(f"REVOKE ALL PRIVILEGES ON SEQUENCE {sequence} FROM ec_app")

    for signature in (
        "public.fn_record_event_change(text, uuid, text, text, timestamptz, timestamptz, text, text, text)",
        "public.fn_list_active_change_watches()",
        "public.fn_claim_event_change_deliveries(integer, integer, text)",
        "public.fn_mark_event_change_delivery(text, uuid, text, text)",
        "public.fn_release_event_change_delivery(text, uuid, text, text, text)",
        "public.fn_enqueue_closed_workflow_calendar_repair(text, uuid, text, text, text)",
        "public.fn_claim_calendar_repairs(integer, integer, text)",
        "public.fn_mark_calendar_repair(bigint, text)",
        "public.fn_reschedule_calendar_repair(bigint, text, text)",
        "public.fn_calendar_repair_change_applied(bigint, text)",
        "public.fn_claim_watch_projections(integer, integer, text)",
        "public.fn_mark_watch_projection_delivered(bigint, text)",
        "public.fn_watch_projection_reschedule(bigint, text, text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
