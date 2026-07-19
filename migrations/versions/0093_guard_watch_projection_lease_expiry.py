"""Fence watch-projection acknowledgements at their exact lease expiry.

Revision ID: 0093
Revises: 0092
Create Date: 2026-07-18

An opaque watch-projection token authorizes acknowledgement or retry only while its database lease
is live.  Once that lease expires, the row must remain reclaimable by another worker rather than
allowing a late original worker to suppress or reschedule it before the token rotates (NFR-8,
ADR-008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0093"
down_revision: str | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Require the current watch-projection lease to remain live for terminal mutations."""
    _replace_watch_projection_terminal_capabilities(require_live_lease=True)


def downgrade() -> None:
    """Restore 0063's prior exact-token capability shape during an isolated rollback."""
    _replace_watch_projection_terminal_capabilities(require_live_lease=False)


def _replace_watch_projection_terminal_capabilities(*, require_live_lease: bool) -> None:
    """Replace only the two exact-lease terminal capabilities without changing their signatures."""
    live_lease_guard = (
        "AND projection.lease_expires_at > pg_catalog.clock_timestamp()"
        if require_live_lease
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_mark_watch_projection_delivered(
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
              AND projection.delivered_at IS NULL
              {live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_mark_watch_projection_delivered(bigint, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_mark_watch_projection_delivered(bigint, text) TO ec_app"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_watch_projection_reschedule(
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
              AND projection.delivered_at IS NULL
              {live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_watch_projection_reschedule(bigint, text, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_watch_projection_reschedule(bigint, text, text) TO ec_app"
    )
