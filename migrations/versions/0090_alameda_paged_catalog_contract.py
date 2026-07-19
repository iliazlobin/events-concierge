"""Admit Alameda's closed page cursor without widening the Legistar capability.

Revision ID: 0090
Revises: 0089
Create Date: 2026-07-18

P15d adds only the separately reviewed Alameda civic Events profile to P15b/P15c's
raw-free cursor capability.  It retains the source-revision and owner-state guards at
page staging and terminal promotion (FR-10.3/10.4, NFR-1/NFR-8, ADR-001/003/005).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0090"
down_revision: str | None = "0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Admit just Alameda's reviewed profile to the existing durable cursor."""
    _replace_prepare_capability(include_alameda=True)
    _replace_current_contract_guard(include_alameda=True)
    _bump_alameda_contract_revision()


def downgrade() -> None:
    """Restore P15c's two-profile capability without lowering a source revision.

    Alameda's monotonic revision deliberately remains elevated.  A rollback therefore cannot
    combine an old staged page with a previously authorized request shape (NFR-8).
    """
    _replace_current_contract_guard(include_alameda=False)
    _replace_prepare_capability(include_alameda=False)


def _reviewed_profiles(*, include_alameda: bool) -> str:
    """Return the closed SQL predicate shared by prepare and write-time guards."""
    alameda = (
        """
                    OR (source.source_key = 'alameda-legistar-meetings'
                        AND source.mode = 'alameda_legistar')
        """
        if include_alameda
        else ""
    )
    return f"""
                    (source.source_key = 'san-jose-legistar-meetings'
                     AND source.mode = 'san_jose_legistar')
                    OR (source.source_key = 'sunnyvale-legistar-meetings'
                        AND source.mode = 'sunnyvale_legistar')
                    {alameda}
    """


def _replace_prepare_capability(*, include_alameda: bool) -> None:
    """Keep cursor creation pinned to the exact reviewed civic profile set."""
    profiles = _reviewed_profiles(include_alameda=include_alameda)
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

            SELECT progress.* INTO v_progress
            FROM public.catalog_refresh_progress AS progress
            WHERE progress.source_key = p_source_key
              AND progress.run_key = p_run_key
            FOR UPDATE;
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

            RETURN QUERY SELECT 'ready', v_progress.source_revision, v_progress.window_start_day,
                                v_progress.next_page, v_progress.page_limit,
                                v_progress.terminal_page, v_progress.staged_raw_count,
                                v_progress.staged_candidate_count;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_prepare_paged_catalog_refresh(text, text, uuid, integer) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_prepare_paged_catalog_refresh(text, text, uuid, integer) TO ec_app"
    )


def _replace_current_contract_guard(*, include_alameda: bool) -> None:
    """Make the existing stage/promotion triggers recognize only current reviewed profiles."""
    profiles = _reviewed_profiles(include_alameda=include_alameda)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_paged_catalog_contract_is_current(
            p_source_key text,
            p_run_key text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_progress_revision integer;
            v_source_revision integer;
            v_now timestamptz;
        BEGIN
            IF p_source_key IS NULL OR p_run_key IS NULL THEN
                RETURN false;
            END IF;
            SELECT progress.source_revision
            INTO v_progress_revision
            FROM public.catalog_refresh_progress AS progress
            WHERE progress.source_key = p_source_key
              AND progress.run_key = p_run_key
            FOR SHARE;
            IF NOT FOUND THEN
                RETURN false;
            END IF;

            v_now := pg_catalog.clock_timestamp();
            SELECT source.source_revision
            INTO v_source_revision
            FROM public.catalog_sources AS source
            WHERE source.source_key = p_source_key
              AND ({profiles})
              AND source.enabled
              AND source.handoff_only
              AND source.reviewed_at IS NOT NULL
              AND source.reviewed_at <= v_now
              AND (source.review_expires_at IS NULL OR source.review_expires_at > v_now)
              AND source.page_limit BETWEEN 1 AND 5
            FOR SHARE;
            RETURN FOUND AND v_source_revision = v_progress_revision;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_paged_catalog_contract_is_current(text, text) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_paged_catalog_contract_is_current(text, text) FROM ec_app"
    )


def _bump_alameda_contract_revision() -> None:
    """Record this review-approved execution-contract change in Alameda's monotonic revision."""
    op.execute(
        """
        DO $$
        DECLARE
            v_updated integer;
        BEGIN
            UPDATE public.catalog_sources
            SET source_revision = source_revision + 1
            WHERE source_key = 'alameda-legistar-meetings'
              AND mode = 'alameda_legistar'
              AND seed_url = 'https://webapi.legistar.com/v1/Alameda/Events'
              AND approved_origins = ARRAY['https://webapi.legistar.com']
              AND handoff_only
              AND enabled
              AND page_limit = 5;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            IF v_updated <> 1 THEN
                RAISE EXCEPTION 'Alameda paged catalog contract did not match one reviewed source';
            END IF;
        END;
        $$
        """
    )
