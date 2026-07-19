"""Prevent the legacy completion capability from bypassing a P15b stage promotion.

Revision ID: 0088
Revises: 0087
Create Date: 2026-07-18

The older one-shot completion function remains valid for legacy and P15a sources. A run that owns a
P15b progress row must instead reach ``fn_promote_paged_catalog_refresh``, which verifies its
terminal stage/provenance in the same transaction (NFR-8, ADR-001/003).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Fence generic success completion away from any durable P15b cursor/stage."""
    _create_complete_capability(block_paged_progress=True)


def downgrade() -> None:
    """Restore 0087's legacy completion shape during an isolated rollback."""
    _create_complete_capability(block_paged_progress=False)


def _create_complete_capability(*, block_paged_progress: bool) -> None:
    """Replace the exact existing signature while retaining its one-shot caller contract."""
    progress_guard = (
        """
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.catalog_refresh_progress AS progress
                  WHERE progress.source_key = refresh.source_key
                    AND progress.run_key = refresh.run_key
              )
        """
        if block_paged_progress
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
              {progress_guard};
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_complete_catalog_refresh(text, text, uuid, integer, integer) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_complete_catalog_refresh(text, text, uuid, integer, integer) TO ec_app"
    )
