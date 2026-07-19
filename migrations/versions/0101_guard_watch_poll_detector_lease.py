"""Expose a live-lease capability before fixture watch-poll detector entry.

Revision ID: 0101
Revises: 0100
Create Date: 2026-07-18

P26 fences terminal watch-poll writes, but a worker can still lose/reclaim its lease after claim
and before its detector boundary. This read-only projection locks the exact public cursor and
reads the database clock after any wait so the fixture scheduler can skip a poll that is already
stale at its final entry check (FR-8.7a, NFR-8/NFR-17, ADR-008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0101"
down_revision: str | None = "0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_has_live_watch_poll_lease(uuid, text, text)"


def upgrade() -> None:
    """Grant the app a database-clock exact-watch-lease projection only."""
    op.execute(
        """
        CREATE FUNCTION public.fn_has_live_watch_poll_lease(
            p_canonical_event_id uuid,
            p_source text,
            p_lease_token text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_state public.watch_poll_state%ROWTYPE;
        BEGIN
            IF p_canonical_event_id IS NULL
               OR p_source IS NULL
               OR p_source NOT IN (
                   'public_jsonld', 'meetup', 'ticketmaster', 'luma', 'eventbrite', 'partiful', 'serpapi'
               )
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
            THEN
                RETURN false;
            END IF;

            SELECT state.*
            INTO v_state
            FROM public.watch_poll_state AS state
            WHERE state.canonical_event_id = p_canonical_event_id
              AND state.source = p_source
            FOR UPDATE;
            IF NOT FOUND
               OR v_state.lease_token IS DISTINCT FROM p_lease_token
               OR v_state.lease_expires_at IS NULL
               OR v_state.lease_expires_at <= pg_catalog.clock_timestamp()
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
    """Remove only the detector-entry authority projection."""
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
