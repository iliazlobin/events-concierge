"""Fence P15 paged-refresh preparation at its final live lease boundary.

Revision ID: 0099
Revises: 0098
Create Date: 2026-07-18

Preparation runs before Pacer admission and source egress, but it can wait first on the refresh
run and then on an existing durable cursor.  A worker whose lease expires at either wait must not
create, reset, or return a cursor that could authorize a later GET (FR-10.3/10.4, NFR-8,
ADR-001/003/005).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0099"
down_revision: str | None = "0098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LATE_PREPARE_LEASE_SQLSTATE = "EC003"
_PREPARE_SIGNATURE = "public.fn_prepare_paged_catalog_refresh(text, text, uuid, integer)"


def upgrade() -> None:
    """Require a live lease after every P15 prepare wait before returning a cursor."""
    _replace_prepare_capability(require_final_live_lease=True)


def downgrade() -> None:
    """Restore the exact P28-era prepare capability during an isolated rollback."""
    _replace_prepare_capability(require_final_live_lease=False)


def _reviewed_profiles() -> str:
    """Return P15e's four exact reviewed city request contracts, unchanged by this fence."""
    return """
                    (source.source_key = 'san-jose-legistar-meetings'
                     AND source.mode = 'san_jose_legistar'
                     AND source.seed_url = 'https://webapi.legistar.com/v1/SanJose/Events'
                     AND source.approved_origins = ARRAY['https://webapi.legistar.com'])
                    OR (source.source_key = 'sunnyvale-legistar-meetings'
                        AND source.mode = 'sunnyvale_legistar'
                        AND source.seed_url = 'https://webapi.legistar.com/v1/SunnyvaleCA/Events'
                        AND source.approved_origins = ARRAY['https://webapi.legistar.com'])
                    OR (source.source_key = 'alameda-legistar-meetings'
                        AND source.mode = 'alameda_legistar'
                        AND source.seed_url = 'https://webapi.legistar.com/v1/Alameda/Events'
                        AND source.approved_origins = ARRAY['https://webapi.legistar.com'])
                    OR (source.source_key = 'oakland-legistar-meetings'
                        AND source.mode = 'oakland_legistar'
                        AND source.seed_url = 'https://webapi.legistar.com/v1/Oakland/Events'
                        AND source.approved_origins = ARRAY['https://webapi.legistar.com'])
    """


def _replace_prepare_capability(*, require_final_live_lease: bool) -> None:
    """Replace only the P15 cursor-prepare capability without widening its authority surface."""
    clock_after_run_lock = (
        "v_now := pg_catalog.clock_timestamp();" if require_final_live_lease else ""
    )
    cursor_effect_open = "BEGIN" if require_final_live_lease else ""
    cursor_effect_before_write_guard = (
        f"""
                IF v_run.status <> 'running'
                   OR v_run.lease_token IS DISTINCT FROM p_lease_token
                   OR v_run.lease_expires_at IS NULL
                   OR v_run.lease_expires_at <= pg_catalog.clock_timestamp()
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '{_LATE_PREPARE_LEASE_SQLSTATE}',
                        MESSAGE = 'paged catalog refresh prepare lease is no longer current';
                END IF;
        """
        if require_final_live_lease
        else ""
    )
    cursor_effect_after_write_guard = cursor_effect_before_write_guard
    cursor_effect_close = (
        f"""
            EXCEPTION
                WHEN SQLSTATE '{_LATE_PREPARE_LEASE_SQLSTATE}' THEN
                    RETURN QUERY SELECT 'lease_lost', NULL::integer, NULL::date, NULL::integer,
                                        NULL::integer, NULL::integer, NULL::integer, NULL::integer;
                    RETURN;
            END;
        """
        if require_final_live_lease
        else ""
    )
    profiles = _reviewed_profiles()
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_prepare_paged_catalog_refresh(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid,
            p_source_revision integer
        )
        RETURNS TABLE(
            outcome text,
            progress_source_revision integer,
            window_start_day date,
            next_page integer,
            page_limit integer,
            terminal_page integer,
            staged_raw_count integer,
            staged_candidate_count integer
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_now timestamptz;
            v_current_revision integer;
            v_page_limit integer;
            v_progress public.catalog_refresh_progress%ROWTYPE;
            v_run public.catalog_refresh_runs%ROWTYPE;
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{{1,79}}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{{0,255}}$'
               OR p_lease_token IS NULL
               OR p_source_revision IS NULL
               OR p_source_revision < 1
            THEN
                RETURN QUERY SELECT 'invalid', NULL::integer, NULL::date, NULL::integer,
                                    NULL::integer, NULL::integer, NULL::integer, NULL::integer;
                RETURN;
            END IF;

            v_now := pg_catalog.clock_timestamp();
            SELECT source.source_revision, source.page_limit
            INTO v_current_revision, v_page_limit
            FROM public.catalog_sources AS source
            WHERE source.source_key = p_source_key
              AND ({profiles})
              AND source.enabled
              AND source.handoff_only
              AND source.reviewed_at IS NOT NULL
              AND source.reviewed_at <= v_now
              AND (source.review_expires_at IS NULL OR source.review_expires_at > v_now)
              AND source.page_limit BETWEEN 1 AND 5;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'invalid', NULL::integer, NULL::date, NULL::integer,
                                    NULL::integer, NULL::integer, NULL::integer, NULL::integer;
                RETURN;
            END IF;
            IF v_current_revision <> p_source_revision THEN
                RETURN QUERY SELECT 'source_changed', NULL::integer, NULL::date, NULL::integer,
                                    NULL::integer, NULL::integer, NULL::integer, NULL::integer;
                RETURN;
            END IF;

            SELECT refresh.* INTO v_run
            FROM public.catalog_refresh_runs AS refresh
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key
            FOR UPDATE;
            {clock_after_run_lock}
            IF NOT FOUND
               OR v_run.status <> 'running'
               OR v_run.lease_token IS DISTINCT FROM p_lease_token
               OR v_run.lease_expires_at IS NULL
               OR v_run.lease_expires_at <= v_now
            THEN
                RETURN QUERY SELECT 'lease_lost', NULL::integer, NULL::date, NULL::integer,
                                    NULL::integer, NULL::integer, NULL::integer, NULL::integer;
                RETURN;
            END IF;

            -- A stale prepare has no provider fact to preserve.  The nested effect rolls any
            -- cursor reset/creation back if the lease expires while the cursor row is waited on
            -- or during its final database writes.
            {cursor_effect_open}
                SELECT progress.* INTO v_progress
                FROM public.catalog_refresh_progress AS progress
                WHERE progress.source_key = p_source_key
                  AND progress.run_key = p_run_key
                FOR UPDATE;
                {cursor_effect_before_write_guard}
                IF NOT FOUND OR v_progress.source_revision <> p_source_revision THEN
                    IF FOUND THEN
                        DELETE FROM public.catalog_refresh_progress AS progress
                        WHERE progress.source_key = p_source_key AND progress.run_key = p_run_key;
                    END IF;
                    INSERT INTO public.catalog_refresh_progress
                        (source_key, run_key, source_revision, window_start_day, page_limit)
                    VALUES
                        (p_source_key, p_run_key, p_source_revision,
                         (v_now AT TIME ZONE 'America/Los_Angeles')::date, v_page_limit)
                    RETURNING * INTO v_progress;
                END IF;
                {cursor_effect_after_write_guard}
            {cursor_effect_close}

            RETURN QUERY SELECT 'ready', v_progress.source_revision, v_progress.window_start_day,
                                v_progress.next_page, v_progress.page_limit,
                                v_progress.terminal_page, v_progress.staged_raw_count,
                                v_progress.staged_candidate_count;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_PREPARE_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PREPARE_SIGNATURE} TO ec_app")
