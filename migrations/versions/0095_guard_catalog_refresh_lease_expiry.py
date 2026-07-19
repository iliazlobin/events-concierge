"""Fence catalog-refresh terminal capabilities at lease expiry.

Revision ID: 0095
Revises: 0094
Create Date: 2026-07-18

An exact catalog-refresh token authorizes success, failure, or a paged-stage abort only while its
database lease remains live.  An expired original worker must leave the run and any normalized P15
cursor/stage reclaimable rather than advancing cadence or discarding recoverable work before a new
claimant rotates the token (NFR-8, ADR-001/003/005).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Require a current live lease for catalog-refresh terminal writes."""
    _replace_catalog_refresh_terminal_capabilities(require_live_lease=True)


def downgrade() -> None:
    """Restore the prior exact-token-only catalog terminal capability bodies."""
    _replace_catalog_refresh_terminal_capabilities(require_live_lease=False)


def _replace_catalog_refresh_terminal_capabilities(*, require_live_lease: bool) -> None:
    """Replace only the three catalog terminal capabilities without changing their API."""
    live_lease_guard = (
        "AND refresh.lease_expires_at > pg_catalog.clock_timestamp()"
        if require_live_lease
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_complete_catalog_refresh(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid,
            p_candidate_count integer,
            p_canonical_count integer
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_updated integer;
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{{1,79}}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{{0,255}}$'
               OR p_lease_token IS NULL
               OR p_candidate_count IS NULL
               OR p_candidate_count < 0
               OR p_canonical_count IS NULL
               OR p_canonical_count < 0
               OR p_canonical_count > p_candidate_count
            THEN
                RETURN false;
            END IF;

            UPDATE public.catalog_refresh_runs AS refresh
            SET status = 'succeeded',
                completed_at = pg_catalog.clock_timestamp(),
                lease_expires_at = NULL,
                candidate_count = p_candidate_count,
                canonical_count = p_canonical_count,
                error = NULL
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key
              AND refresh.status = 'running'
              AND refresh.lease_token = p_lease_token
              {live_lease_guard}
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.catalog_refresh_progress AS progress
                  WHERE progress.source_key = refresh.source_key
                    AND progress.run_key = refresh.run_key
              );
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_fail_catalog_refresh(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid,
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
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{{1,79}}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{{0,255}}$'
               OR p_lease_token IS NULL
               OR p_error IS NULL
            THEN
                RETURN false;
            END IF;

            UPDATE public.catalog_refresh_runs AS refresh
            SET status = 'failed',
                completed_at = pg_catalog.clock_timestamp(),
                lease_expires_at = NULL,
                error = left(p_error, 2000)
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key
              AND refresh.status = 'running'
              AND refresh.lease_token = p_lease_token
              {live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_abort_paged_catalog_refresh(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid,
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
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{{1,79}}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{{0,255}}$'
               OR p_lease_token IS NULL
               OR p_error IS NULL
            THEN
                RETURN false;
            END IF;
            UPDATE public.catalog_refresh_runs AS refresh
            SET status = 'failed',
                completed_at = pg_catalog.clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                error = left(p_error, 2000)
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key
              AND refresh.status = 'running'
              AND refresh.lease_token = p_lease_token
              {live_lease_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            IF v_updated <> 1 THEN
                RETURN false;
            END IF;
            DELETE FROM public.catalog_refresh_progress AS progress
            WHERE progress.source_key = p_source_key AND progress.run_key = p_run_key;
            RETURN true;
        END;
        $$
        """
    )
    for signature in (
        "public.fn_complete_catalog_refresh(text, text, uuid, integer, integer)",
        "public.fn_fail_catalog_refresh(text, text, uuid, text)",
        "public.fn_abort_paged_catalog_refresh(text, text, uuid, text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
