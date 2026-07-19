"""Expose a live-lease capability for generic catalog fetch entry.

Revision ID: 0100
Revises: 0099
Create Date: 2026-07-18

P31 rolls stale generic catalog publication back, but cannot undo a source GET issued after an
expired/reclaimed run lease. This read-only capability locks the exact run and evaluates the
database clock after that wait so a worker can fence its final generic fetch entry (FR-10.3,
NFR-8, ADR-001/003/005).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0100"
down_revision: str | None = "0099"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_has_live_catalog_refresh_lease(text, text, uuid)"


def upgrade() -> None:
    """Grant the app a read-only, database-clock exact-lease projection."""
    op.execute(
        """
        CREATE FUNCTION public.fn_has_live_catalog_refresh_lease(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_run public.catalog_refresh_runs%ROWTYPE;
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_lease_token IS NULL
            THEN
                RETURN false;
            END IF;

            SELECT refresh.*
            INTO v_run
            FROM public.catalog_refresh_runs AS refresh
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key
            FOR UPDATE;
            IF NOT FOUND
               OR v_run.status <> 'running'
               OR v_run.lease_token IS DISTINCT FROM p_lease_token
               OR v_run.lease_expires_at IS NULL
               OR v_run.lease_expires_at <= pg_catalog.clock_timestamp()
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
    """Remove only the final generic-fetch authority projection."""
    op.execute(f"DROP FUNCTION IF EXISTS {_SIGNATURE}")
