"""Expose a current watch-projection lease before registry mutation.

Revision ID: 0104
Revises: 0103
Create Date: 2026-07-18

P21 fences watch-projection terminal writes, yet a worker can lose its projection lease after a
claim and before it reaches the tenant-scoped registry register/unregister capability. This
fixed-shape, database-clock projection locks only the exact opaque outbox row so the worker can
skip an already stale/reclaimed projection before that effect boundary (NFR-8, ADR-008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0104"
down_revision: str | None = "0103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_has_live_watch_projection_lease(bigint, text)"


def upgrade() -> None:
    """Grant the app a narrow exact-projection lease projection, not outbox-row access."""
    op.execute(
        """
        CREATE FUNCTION public.fn_has_live_watch_projection_lease(
            p_projection_id bigint,
            p_lease_token text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_projection public.lifecycle_watch_projection_outbox%ROWTYPE;
        BEGIN
            IF p_projection_id IS NULL
               OR p_projection_id < 1
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
            THEN
                RETURN false;
            END IF;

            SELECT projection.*
            INTO v_projection
            FROM public.lifecycle_watch_projection_outbox AS projection
            WHERE projection.projection_id = p_projection_id
            FOR UPDATE;
            IF NOT FOUND
               OR v_projection.delivered_at IS NOT NULL
               OR v_projection.lease_token IS DISTINCT FROM p_lease_token
               OR v_projection.lease_expires_at IS NULL
               OR v_projection.lease_expires_at <= pg_catalog.clock_timestamp()
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
    """Remove only the pre-registry projection lease projection."""
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
