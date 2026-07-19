"""Admit Oakland's exact cursor profile and pin all paged source shapes in PostgreSQL.

Revision ID: 0091
Revises: 0090
Create Date: 2026-07-18

P15e adds only the separately reviewed Oakland civic Events profile to the raw-free Legistar
cursor.  It also makes the capability's existing city profiles require their exact reviewed API
seed and sole approved origin, in addition to the revision and owner-state guards at page staging
and terminal promotion (FR-10.3/10.4, NFR-1/NFR-8, ADR-001/003/005).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0091"
down_revision: str | None = "0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Admit just Oakland's exact reviewed request profile to the durable cursor."""
    _replace_prepare_capability(include_oakland=True)
    _replace_current_contract_guard(include_oakland=True)
    _bump_oakland_contract_revision()


def downgrade() -> None:
    """Restore P15d's three-profile capability without lowering a source revision.

    Oakland's elevated revision remains after rollback.  An old Oakland stage therefore cannot be
    combined with a previously authorized request contract (NFR-8).
    """
    _replace_current_contract_guard(include_oakland=False)
    _replace_prepare_capability(include_oakland=False)


def _reviewed_profiles(*, include_oakland: bool) -> str:
    """Return the exact static profile predicate shared by prepare and write-time guards."""
    oakland = (
        """
                    OR (source.source_key = 'oakland-legistar-meetings'
                        AND source.mode = 'oakland_legistar'
                        AND source.seed_url = 'https://webapi.legistar.com/v1/Oakland/Events'
                        AND source.approved_origins = ARRAY['https://webapi.legistar.com'])
        """
        if include_oakland
        else ""
    )
    return f"""
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
                    {oakland}
    """


def _replace_prepare_capability(*, include_oakland: bool) -> None:
    """Keep cursor creation pinned to exact reviewed city endpoint/origin pairs."""
    profiles = _reviewed_profiles(include_oakland=include_oakland)
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


def _replace_current_contract_guard(*, include_oakland: bool) -> None:
    """Make the existing stage/promotion triggers require the exact current city contract."""
    profiles = _reviewed_profiles(include_oakland=include_oakland)
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


def _bump_oakland_contract_revision() -> None:
    """Record Oakland's admitted exact execution contract in its monotonic source revision."""
    op.execute(
        """
        DO $$
        DECLARE
            v_updated integer;
            v_now timestamptz;
        BEGIN
            v_now := pg_catalog.clock_timestamp();
            UPDATE public.catalog_sources
            SET source_revision = source_revision + 1
            WHERE source_key = 'oakland-legistar-meetings'
              AND mode = 'oakland_legistar'
              AND seed_url = 'https://webapi.legistar.com/v1/Oakland/Events'
              AND approved_origins = ARRAY['https://webapi.legistar.com']
              AND handoff_only
              AND enabled
              AND reviewed_at IS NOT NULL
              AND reviewed_at <= v_now
              AND (review_expires_at IS NULL OR review_expires_at > v_now)
              AND page_limit = 5;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            IF v_updated <> 1 THEN
                RAISE EXCEPTION 'Oakland paged catalog contract did not match one reviewed source';
            END IF;
        END;
        $$
        """
    )
