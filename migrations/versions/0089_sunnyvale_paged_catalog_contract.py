"""Admit Sunnyvale's closed page cursor and fence staged contract races.

Revision ID: 0089
Revises: 0088
Create Date: 2026-07-18

P15c extends the existing raw-free Legistar cursor only to Sunnyvale's separately reviewed
profile.  It also makes staging and terminal promotion verify the current owner-reviewed source
row while holding a shared row lock, so a contract edit that wins a race cannot publish an older
page/stage (FR-10.3/10.4, NFR-1/NFR-8, ADR-001/003/005).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0089"
down_revision: str | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Admit only Sunnyvale's reviewed profile and lock every staged/PROMOTE effect."""
    _replace_prepare_capability(include_sunnyvale=True)
    _create_current_contract_guards()
    _bump_sunnyvale_contract_revision()


def downgrade() -> None:
    """Restore the prior San Jose-only prepare fence without lowering source revisions.

    A source revision is monotonic by design. Leaving Sunnyvale's higher revision in place is
    fail-safe: a rollback cannot combine a staged page with a prior request contract.
    """
    op.execute(
        "DROP TRIGGER IF EXISTS trg_catalog_refresh_stage_contract ON public.catalog_refresh_stage_pages"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_catalog_refresh_promotion_contract ON public.catalog_refresh_runs"
    )
    op.execute("DROP FUNCTION IF EXISTS public.fn_guard_paged_catalog_promotion_contract()")
    op.execute("DROP FUNCTION IF EXISTS public.fn_guard_paged_catalog_stage_contract()")
    op.execute("DROP FUNCTION IF EXISTS public.fn_paged_catalog_contract_is_current(text, text)")
    _replace_prepare_capability(include_sunnyvale=False)


def _replace_prepare_capability(*, include_sunnyvale: bool) -> None:
    """Keep the only cursor-creation capability pinned to an exact reviewed profile set."""
    profiles = (
        """
                    (source.source_key = 'san-jose-legistar-meetings'
                     AND source.mode = 'san_jose_legistar')
                    OR (source.source_key = 'sunnyvale-legistar-meetings'
                        AND source.mode = 'sunnyvale_legistar')
    """
        if include_sunnyvale
        else """
                    source.source_key = 'san-jose-legistar-meetings'
                    AND source.mode = 'san_jose_legistar'
    """
    )
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


def _create_current_contract_guards() -> None:
    """Lock and verify the profile at the two writes that can publish a page or full run."""
    op.execute(
        """
        CREATE FUNCTION public.fn_paged_catalog_contract_is_current(
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
              AND (
                    (source.source_key = 'san-jose-legistar-meetings'
                     AND source.mode = 'san_jose_legistar')
                    OR (source.source_key = 'sunnyvale-legistar-meetings'
                        AND source.mode = 'sunnyvale_legistar')
              )
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
        """
        CREATE FUNCTION public.fn_guard_paged_catalog_stage_contract()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NOT public.fn_paged_catalog_contract_is_current(NEW.source_key, NEW.run_key) THEN
                RAISE EXCEPTION 'paged catalog source contract is no longer current'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_guard_paged_catalog_promotion_contract()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.status = 'succeeded'
               AND OLD.status = 'running'
               AND EXISTS (
                    SELECT 1
                    FROM public.catalog_refresh_progress AS progress
                    WHERE progress.source_key = NEW.source_key
                      AND progress.run_key = NEW.run_key
               )
               AND NOT public.fn_paged_catalog_contract_is_current(NEW.source_key, NEW.run_key)
            THEN
                RAISE EXCEPTION 'paged catalog source contract is no longer current'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for signature in (
        "public.fn_paged_catalog_contract_is_current(text, text)",
        "public.fn_guard_paged_catalog_stage_contract()",
        "public.fn_guard_paged_catalog_promotion_contract()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_catalog_refresh_stage_contract ON public.catalog_refresh_stage_pages"
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_refresh_stage_contract
        BEFORE INSERT ON public.catalog_refresh_stage_pages
        FOR EACH ROW EXECUTE FUNCTION public.fn_guard_paged_catalog_stage_contract()
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_catalog_refresh_promotion_contract ON public.catalog_refresh_runs"
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_refresh_promotion_contract
        BEFORE UPDATE OF status ON public.catalog_refresh_runs
        FOR EACH ROW EXECUTE FUNCTION public.fn_guard_paged_catalog_promotion_contract()
        """
    )


def _bump_sunnyvale_contract_revision() -> None:
    """Record this reviewed execution-contract change in Sunnyvale's monotonic source revision."""
    op.execute(
        """
        DO $$
        DECLARE
            v_updated integer;
        BEGIN
            UPDATE public.catalog_sources
            SET source_revision = source_revision + 1
            WHERE source_key = 'sunnyvale-legistar-meetings'
              AND mode = 'sunnyvale_legistar'
              AND seed_url = 'https://webapi.legistar.com/v1/SunnyvaleCA/Events'
              AND approved_origins = ARRAY['https://webapi.legistar.com']
              AND handoff_only
              AND enabled
              AND page_limit = 5;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            IF v_updated <> 1 THEN
                RAISE EXCEPTION 'Sunnyvale paged catalog contract did not match one reviewed source';
            END IF;
        END;
        $$
        """
    )
