"""Fence public watch-poll terminal writes at lease expiry.

Revision ID: 0096
Revises: 0095
Create Date: 2026-07-18

An exact public watch-poll token authorizes either the successful cursor advance or failed-poll
release only while its database lease remains live.  A late worker leaves the public cursor
reclaimable instead of suppressing a due poll or altering its staleness evidence (FR-8.7a,
NFR-8/NFR-17, ADR-008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Require a current live lease for both watch-poll terminal capabilities."""
    _replace_watch_poll_result_capabilities(require_live_lease=True)


def downgrade() -> None:
    """Restore 0065's exact-token-only result capability bodies during rollback."""
    _replace_watch_poll_result_capabilities(require_live_lease=False)


def _replace_watch_poll_result_capabilities(*, require_live_lease: bool) -> None:
    """Replace only ADR-008's two public poll-result capabilities without changing their API."""
    live_lease_guard = (
        "AND state.lease_expires_at > pg_catalog.clock_timestamp()"
        if require_live_lease
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_mark_watch_poll_succeeded(
            p_canonical_event_id uuid,
            p_source text,
            p_lease_token text,
            p_completed_at timestamptz,
            p_next_due_at timestamptz
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_updated integer;
        BEGIN
            IF p_canonical_event_id IS NULL
               OR p_source IS NULL
               OR p_source NOT IN (
                   'public_jsonld', 'meetup', 'ticketmaster', 'luma', 'eventbrite', 'partiful', 'serpapi'
               )
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
               OR p_completed_at IS NULL
               OR p_next_due_at IS NULL
               OR p_next_due_at < p_completed_at
            THEN
                RETURN false;
            END IF;

            UPDATE public.watch_poll_state AS state
            SET last_success_at = p_completed_at,
                consecutive_failures = 0,
                next_due_at = p_next_due_at,
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error_type = NULL
            WHERE state.canonical_event_id = p_canonical_event_id
              AND state.source = p_source
              AND state.lease_token = p_lease_token
              {live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_release_watch_poll(
            p_canonical_event_id uuid,
            p_source text,
            p_lease_token text,
            p_completed_at timestamptz,
            p_next_due_at timestamptz,
            p_error_type text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_error_type text;
            v_updated integer;
        BEGIN
            v_error_type := btrim(p_error_type);
            IF p_canonical_event_id IS NULL
               OR p_source IS NULL
               OR p_source NOT IN (
                   'public_jsonld', 'meetup', 'ticketmaster', 'luma', 'eventbrite', 'partiful', 'serpapi'
               )
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
               OR p_completed_at IS NULL
               OR p_next_due_at IS NULL
               OR p_next_due_at < p_completed_at
               OR v_error_type IS NULL
               OR v_error_type !~ '^[A-Za-z][A-Za-z0-9_.]{{0,127}}$'
            THEN
                RETURN false;
            END IF;

            UPDATE public.watch_poll_state AS state
            SET last_failure_at = p_completed_at,
                consecutive_failures = state.consecutive_failures + 1,
                next_due_at = p_next_due_at,
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error_type = v_error_type
            WHERE state.canonical_event_id = p_canonical_event_id
              AND state.source = p_source
              AND state.lease_token = p_lease_token
              {live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    for signature in (
        "public.fn_mark_watch_poll_succeeded(uuid, text, text, timestamptz, timestamptz)",
        "public.fn_release_watch_poll(uuid, text, text, timestamptz, timestamptz, text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
