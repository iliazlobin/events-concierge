"""Fence event-change and calendar-repair terminal writes at lease expiry.

Revision ID: 0094
Revises: 0093
Create Date: 2026-07-18

An exact opaque token authorizes a delivery acknowledgement, release, repair enqueue, or repair
terminal write only while its database lease remains live.  A late worker must leave the durable
row reclaimable rather than suppressing, rescheduling, or creating a repair before a fresh worker
can take ownership (NFR-8, ADR-008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0094"
down_revision: str | None = "0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Require a live current lease for event-change and repair terminal mutations."""
    _replace_event_change_terminal_capabilities(require_live_lease=True)


def downgrade() -> None:
    """Restore 0063's exact-token-only terminal capability bodies during rollback."""
    _replace_event_change_terminal_capabilities(require_live_lease=False)


def _replace_event_change_terminal_capabilities(*, require_live_lease: bool) -> None:
    """Replace only ADR-008's five terminal capability functions without changing their API."""
    delivery_live_lease_guard = (
        "AND delivery.lease_expires_at > pg_catalog.clock_timestamp()" if require_live_lease else ""
    )
    repair_live_lease_guard = (
        "AND repair.lease_expires_at > pg_catalog.clock_timestamp()" if require_live_lease else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_mark_event_change_delivery(
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
              AND delivery.delivered_at IS NULL
              {delivery_live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_release_event_change_delivery(
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
              AND delivery.delivered_at IS NULL
              {delivery_live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_enqueue_closed_workflow_calendar_repair(
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
              {delivery_live_lease_guard}
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
        f"""
        CREATE OR REPLACE FUNCTION public.fn_mark_calendar_repair(p_repair_id bigint, p_lease_token text)
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
              AND repair.repaired_at IS NULL
              {repair_live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_reschedule_calendar_repair(
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
              AND repair.repaired_at IS NULL
              {repair_live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    for signature in (
        "public.fn_mark_event_change_delivery(text, uuid, text, text)",
        "public.fn_release_event_change_delivery(text, uuid, text, text, text)",
        "public.fn_enqueue_closed_workflow_calendar_repair(text, uuid, text, text, text)",
        "public.fn_mark_calendar_repair(bigint, text)",
        "public.fn_reschedule_calendar_repair(bigint, text, text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
