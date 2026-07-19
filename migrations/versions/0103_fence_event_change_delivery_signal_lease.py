"""Expose a current event-change delivery lease before Temporal signaling.

Revision ID: 0103
Revises: 0102
Create Date: 2026-07-18

P22 fences the fanout queue's terminal writes, yet a worker can lose its delivery lease after a
claim and before it asks Temporal to signal the target workflow.  This fixed-shape,
database-clock projection locks only the exact opaque delivery row so the worker can skip an
already stale/reclaimed delivery before that effect boundary (NFR-8, ADR-008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0103"
down_revision: str | None = "0102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_has_live_event_change_delivery_lease(text, uuid, text, text)"


def upgrade() -> None:
    """Grant the app a narrow exact-delivery lease projection, not queue-row access."""
    op.execute(
        """
        CREATE FUNCTION public.fn_has_live_event_change_delivery_lease(
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
            v_delivery public.event_change_deliveries%ROWTYPE;
        BEGIN
            IF NULLIF(btrim(p_fingerprint), '') IS NULL
               OR p_tenant_id IS NULL
               OR NULLIF(btrim(p_workflow_id), '') IS NULL
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
            THEN
                RETURN false;
            END IF;

            SELECT delivery.*
            INTO v_delivery
            FROM public.event_change_deliveries AS delivery
            WHERE delivery.fingerprint = p_fingerprint
              AND delivery.tenant_id = p_tenant_id
              AND delivery.workflow_id = p_workflow_id
            FOR UPDATE;
            IF NOT FOUND
               OR v_delivery.delivered_at IS NOT NULL
               OR v_delivery.lease_token IS DISTINCT FROM p_lease_token
               OR v_delivery.lease_expires_at IS NULL
               OR v_delivery.lease_expires_at <= pg_catalog.clock_timestamp()
            THEN
                RETURN false;
            END IF;
            RETURN true;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO ec_app")


def downgrade() -> None:
    """Remove only the pre-signal delivery lease projection."""
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
