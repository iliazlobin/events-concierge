"""Fence P15 paged-refresh terminal work at its final live lease boundary.

Revision ID: 0098
Revises: 0097
Create Date: 2026-07-18

A P15 worker can wait after its entry lease check: a full page waits in its source-contract
trigger before releasing its cursor, and a promotion waits in the final contract trigger after
catalog/provenance writes begin.  A late worker must leave the page/cursor or terminal stage
recoverable instead of advancing a stale run (FR-10.3/10.4, NFR-8, ADR-001/003/005).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0098"
down_revision: str | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LATE_STAGE_LEASE_SQLSTATE = "EC002"
_STAGE_SIGNATURE = (
    "public.fn_stage_paged_catalog_refresh_page(text, text, uuid, integer, integer, integer, jsonb, jsonb)"
)
_PROMOTION_SIGNATURE = "public.fn_promote_paged_catalog_refresh(text, text, uuid, integer, integer, integer)"


def upgrade() -> None:
    """Require a final live lease for P15's nonterminal page and terminal promotion writes."""
    _replace_paged_refresh_capabilities(require_final_live_lease=True)
    _create_promotion_post_contract_lease_guard()


def downgrade() -> None:
    """Restore the exact pre-P28 P15 capability behavior during an isolated rollback."""
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_refresh_promotion_lease ON public.catalog_refresh_runs")
    op.execute("DROP FUNCTION IF EXISTS public.fn_guard_paged_catalog_promotion_lease()")
    _replace_paged_refresh_capabilities(require_final_live_lease=False)


def _replace_paged_refresh_capabilities(*, require_final_live_lease: bool) -> None:
    """Replace only the two P15 capabilities while retaining their signatures and contracts."""
    stage_clock_before_run_lock = "" if require_final_live_lease else "v_now := pg_catalog.clock_timestamp();"
    stage_clock_after_run_lock = "v_now := pg_catalog.clock_timestamp();" if require_final_live_lease else ""
    stage_block_open = "BEGIN" if require_final_live_lease else ""
    stage_final_lease_guard = (
        "AND refresh.lease_expires_at > pg_catalog.clock_timestamp()"
        if require_final_live_lease
        else ""
    )
    stage_late_lease_failure = (
        f"""
                    RAISE EXCEPTION USING
                        ERRCODE = '{_LATE_STAGE_LEASE_SQLSTATE}',
                        MESSAGE = 'paged catalog refresh lease is no longer current';
        """
        if require_final_live_lease
        else "RETURN 'lease_lost';"
    )
    stage_block_close = (
        f"""
            EXCEPTION
                WHEN SQLSTATE '{_LATE_STAGE_LEASE_SQLSTATE}' THEN
                    RETURN 'lease_lost';
            END;
        """
        if require_final_live_lease
        else ""
    )
    promotion_final_recheck = (
        """
            -- Count/provenance checks can take time. Reacquire the exact run row before its
            -- terminal write so ``clock_timestamp()`` is read after any competing row lock.
            SELECT refresh.* INTO v_run
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
        """
        if require_final_live_lease
        else ""
    )
    promotion_final_lease_guard = (
        "AND refresh.lease_expires_at > pg_catalog.clock_timestamp()"
        if require_final_live_lease
        else ""
    )

    stage_sql = """
        CREATE OR REPLACE FUNCTION public.fn_stage_paged_catalog_refresh_page(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid,
            p_source_revision integer,
            p_page_number integer,
            p_raw_count integer,
            p_candidates jsonb,
            p_source_event_ids jsonb
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_now timestamptz;
            v_run public.catalog_refresh_runs%ROWTYPE;
            v_progress public.catalog_refresh_progress%ROWTYPE;
            v_candidate jsonb;
            v_event_id jsonb;
            v_candidate_id text;
            v_remote_id text;
            v_start_at timestamptz;
            v_end_at timestamptz;
            v_candidate_ids text[] := ARRAY[]::text[];
            v_remote_ids text[] := ARRAY[]::text[];
            v_candidate_count integer;
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_lease_token IS NULL
               OR p_source_revision IS NULL OR p_source_revision < 1
               OR p_page_number IS NULL OR p_page_number < 0 OR p_page_number > 4
               OR p_raw_count IS NULL OR p_raw_count < 0 OR p_raw_count > 100
               OR pg_catalog.jsonb_typeof(p_candidates) <> 'array'
               OR pg_catalog.jsonb_typeof(p_source_event_ids) <> 'array'
            THEN
                RETURN 'invalid';
            END IF;
            v_candidate_count := pg_catalog.jsonb_array_length(p_candidates);
            IF v_candidate_count > p_raw_count
               OR pg_catalog.jsonb_array_length(p_source_event_ids) > p_raw_count
            THEN
                RETURN 'invalid';
            END IF;

            -- Validate the full JSON input before inserting anything. This keeps an invalid
            -- app-role invocation from committing a partial normalized stage.
            FOR v_candidate IN SELECT value FROM pg_catalog.jsonb_array_elements(p_candidates)
            LOOP
                IF pg_catalog.jsonb_typeof(v_candidate) <> 'object'
                   OR NOT (v_candidate ?& ARRAY[
                       'source', 'source_event_id', 'title', 'start_at', 'end_at',
                       'registration_url', 'venue_name', 'city', 'description',
                       'price_status', 'content_hash'
                   ])
                   OR EXISTS (
                       SELECT 1
                       FROM pg_catalog.jsonb_object_keys(v_candidate) AS key_name
                       WHERE key_name NOT IN (
                           'source', 'source_event_id', 'title', 'start_at', 'end_at',
                           'registration_url', 'venue_name', 'city', 'description',
                           'price_status', 'content_hash'
                       )
                   )
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'source') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'source_event_id') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'title') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'start_at') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'registration_url') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'description') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'price_status') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'content_hash') <> 'string'
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'end_at') NOT IN ('string', 'null')
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'venue_name') NOT IN ('string', 'null')
                   OR pg_catalog.jsonb_typeof(v_candidate -> 'city') NOT IN ('string', 'null')
                   OR v_candidate ->> 'source' <> 'public_jsonld'
                   OR char_length(v_candidate ->> 'source_event_id') NOT BETWEEN 1 AND 512
                   OR char_length(v_candidate ->> 'title') NOT BETWEEN 1 AND 5000
                   OR char_length(v_candidate ->> 'description') > 20000
                   OR char_length(v_candidate ->> 'registration_url') NOT BETWEEN 9 AND 4096
                   OR v_candidate ->> 'registration_url' !~ '^https://[^[:space:]]+$'
                   OR v_candidate ->> 'price_status' NOT IN ('free', 'paid', 'unknown')
                   OR v_candidate ->> 'content_hash' !~ '^[0-9a-f]{64}$'
                THEN
                    RETURN 'invalid';
                END IF;
                BEGIN
                    v_start_at := (v_candidate ->> 'start_at')::timestamptz;
                    IF v_candidate ->> 'end_at' IS NULL THEN
                        v_end_at := NULL;
                    ELSE
                        v_end_at := (v_candidate ->> 'end_at')::timestamptz;
                    END IF;
                EXCEPTION WHEN others THEN
                    RETURN 'invalid';
                END;
                IF v_start_at IS NULL OR (v_end_at IS NOT NULL AND v_end_at < v_start_at) THEN
                    RETURN 'invalid';
                END IF;
                v_candidate_id := v_candidate ->> 'source_event_id';
                IF v_candidate_id = ANY(v_candidate_ids) THEN
                    RETURN 'conflict';
                END IF;
                v_candidate_ids := array_append(v_candidate_ids, v_candidate_id);
            END LOOP;
            FOR v_event_id IN SELECT value FROM pg_catalog.jsonb_array_elements(p_source_event_ids)
            LOOP
                IF pg_catalog.jsonb_typeof(v_event_id) <> 'string' THEN
                    RETURN 'invalid';
                END IF;
                v_remote_id := v_event_id #>> '{}';
                IF v_remote_id !~ '^[1-9][0-9]{0,18}$' THEN
                    RETURN 'invalid';
                END IF;
                IF v_remote_id = ANY(v_remote_ids) THEN
                    RETURN 'conflict';
                END IF;
                v_remote_ids := array_append(v_remote_ids, v_remote_id);
            END LOOP;

            __STAGE_CLOCK_BEFORE_RUN_LOCK__
            SELECT refresh.* INTO v_run
            FROM public.catalog_refresh_runs AS refresh
            WHERE refresh.source_key = p_source_key AND refresh.run_key = p_run_key
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'lease_lost';
            END IF;
            __STAGE_CLOCK_AFTER_RUN_LOCK__
            IF v_run.status <> 'running'
               OR v_run.lease_token IS DISTINCT FROM p_lease_token
               OR v_run.lease_expires_at IS NULL
               OR v_run.lease_expires_at <= v_now
            THEN
                RETURN 'lease_lost';
            END IF;
            SELECT progress.* INTO v_progress
            FROM public.catalog_refresh_progress AS progress
            WHERE progress.source_key = p_source_key AND progress.run_key = p_run_key
            FOR UPDATE;
            IF NOT FOUND
               OR v_progress.source_revision <> p_source_revision
               OR v_progress.terminal_page IS NOT NULL
               OR v_progress.next_page <> p_page_number
            THEN
                RETURN 'stale_cursor';
            END IF;
            IF p_raw_count = 100 AND p_page_number + 1 >= v_progress.page_limit THEN
                RETURN 'cap_exceeded';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.catalog_refresh_stage_candidates AS candidate
                WHERE candidate.source_key = p_source_key
                  AND candidate.run_key = p_run_key
                  AND candidate.source_event_id = ANY(v_candidate_ids)
            ) OR EXISTS (
                SELECT 1
                FROM public.catalog_refresh_stage_event_ids AS remote
                WHERE remote.source_key = p_source_key
                  AND remote.run_key = p_run_key
                  AND remote.source_event_id = ANY(v_remote_ids)
            ) THEN
                RETURN 'conflict';
            END IF;

            -- The full-page stage and its pause transition are one recoverable effect. An exact
            -- late-lease exception rolls this nested subtransaction back before returning.
            __STAGE_EFFECT_OPEN__
                INSERT INTO public.catalog_refresh_stage_pages
                    (source_key, run_key, page_number, raw_count, candidate_count)
                VALUES (p_source_key, p_run_key, p_page_number, p_raw_count, v_candidate_count);
                FOR v_candidate IN SELECT value FROM pg_catalog.jsonb_array_elements(p_candidates)
                LOOP
                    INSERT INTO public.catalog_refresh_stage_candidates
                        (source_key, run_key, page_number, source, source_event_id, title, start_at,
                         end_at, registration_url, venue_name, city, description, price_status,
                         content_hash)
                    VALUES
                        (p_source_key, p_run_key, p_page_number, v_candidate ->> 'source',
                         v_candidate ->> 'source_event_id', v_candidate ->> 'title',
                         (v_candidate ->> 'start_at')::timestamptz,
                         (v_candidate ->> 'end_at')::timestamptz,
                         v_candidate ->> 'registration_url', v_candidate ->> 'venue_name',
                         v_candidate ->> 'city', v_candidate ->> 'description',
                         v_candidate ->> 'price_status', v_candidate ->> 'content_hash');
                END LOOP;
                FOR v_event_id IN SELECT value FROM pg_catalog.jsonb_array_elements(p_source_event_ids)
                LOOP
                    INSERT INTO public.catalog_refresh_stage_event_ids
                        (source_key, run_key, page_number, source_event_id)
                    VALUES (p_source_key, p_run_key, p_page_number, v_event_id #>> '{}');
                END LOOP;

                UPDATE public.catalog_refresh_progress AS progress
                SET next_page = p_page_number + 1,
                    terminal_page = CASE WHEN p_raw_count < 100 THEN p_page_number ELSE NULL END,
                    staged_raw_count = progress.staged_raw_count + p_raw_count,
                    staged_candidate_count = progress.staged_candidate_count + v_candidate_count,
                    updated_at = v_now
                WHERE progress.source_key = p_source_key AND progress.run_key = p_run_key;
                IF p_raw_count < 100 THEN
                    RETURN 'terminal';
                END IF;

                UPDATE public.catalog_refresh_runs AS refresh
                SET status = 'paused',
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    error = NULL
                WHERE refresh.source_key = p_source_key
                  AND refresh.run_key = p_run_key
                  AND refresh.status = 'running'
                  AND refresh.lease_token = p_lease_token
                  __STAGE_FINAL_LEASE_GUARD__;
                IF NOT FOUND THEN
                    __STAGE_LATE_LEASE_FAILURE__
                END IF;
            __STAGE_EFFECT_CLOSE__
            RETURN 'more';
        END;
        $$
        """
    op.execute(
        stage_sql.replace("__STAGE_CLOCK_BEFORE_RUN_LOCK__", stage_clock_before_run_lock)
        .replace("__STAGE_CLOCK_AFTER_RUN_LOCK__", stage_clock_after_run_lock)
        .replace("__STAGE_EFFECT_OPEN__", stage_block_open)
        .replace("__STAGE_FINAL_LEASE_GUARD__", stage_final_lease_guard)
        .replace("__STAGE_LATE_LEASE_FAILURE__", stage_late_lease_failure)
        .replace("__STAGE_EFFECT_CLOSE__", stage_block_close)
    )

    promotion_sql = """
        CREATE OR REPLACE FUNCTION public.fn_promote_paged_catalog_refresh(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid,
            p_source_revision integer,
            p_candidate_count integer,
            p_canonical_count integer
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_staged_count integer;
            v_observed_count integer;
            v_updated integer;
            v_run public.catalog_refresh_runs%ROWTYPE;
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_lease_token IS NULL
               OR p_source_revision IS NULL OR p_source_revision < 1
               OR p_candidate_count IS NULL OR p_candidate_count < 0
               OR p_canonical_count IS NULL OR p_canonical_count < 0
               OR p_canonical_count > p_candidate_count
            THEN
                RETURN false;
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM public.catalog_refresh_runs AS refresh
                JOIN public.catalog_refresh_progress AS progress
                  ON progress.source_key = refresh.source_key AND progress.run_key = refresh.run_key
                WHERE refresh.source_key = p_source_key
                  AND refresh.run_key = p_run_key
                  AND refresh.status = 'running'
                  AND refresh.lease_token = p_lease_token
                  AND refresh.lease_expires_at > pg_catalog.clock_timestamp()
                  AND progress.source_revision = p_source_revision
                  AND progress.terminal_page IS NOT NULL
            ) THEN
                RETURN false;
            END IF;
            SELECT count(*) INTO v_staged_count
            FROM public.catalog_refresh_stage_candidates AS candidate
            WHERE candidate.source_key = p_source_key AND candidate.run_key = p_run_key;
            IF v_staged_count <> p_candidate_count THEN
                RETURN false;
            END IF;
            SELECT count(*) INTO v_observed_count
            FROM public.catalog_refresh_stage_candidates AS candidate
            JOIN public.catalog_event_observations AS observation
              ON observation.source_key = p_source_key
             AND observation.source = candidate.source
             AND observation.source_event_id = candidate.source_event_id
             AND observation.last_run_key = p_run_key
            WHERE candidate.source_key = p_source_key AND candidate.run_key = p_run_key;
            IF v_observed_count <> v_staged_count THEN
                RETURN false;
            END IF;
            __PROMOTION_FINAL_RECHECK__
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
              __PROMOTION_FINAL_LEASE_GUARD__;
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
    op.execute(
        promotion_sql.replace("__PROMOTION_FINAL_RECHECK__", promotion_final_recheck).replace(
            "__PROMOTION_FINAL_LEASE_GUARD__", promotion_final_lease_guard
        )
    )
    for signature in (_STAGE_SIGNATURE, _PROMOTION_SIGNATURE):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def _create_promotion_post_contract_lease_guard() -> None:
    """Check wall-clock lease authority after the existing source-contract trigger can wait."""
    op.execute(
        """
        CREATE FUNCTION public.fn_guard_paged_catalog_promotion_lease()
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
               AND (
                    OLD.lease_token IS NULL
                    OR OLD.lease_expires_at IS NULL
                    OR OLD.lease_expires_at <= pg_catalog.clock_timestamp()
               )
            THEN
                -- Returning NULL from a BEFORE ROW trigger suppresses this stale terminal update.
                RETURN NULL;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_guard_paged_catalog_promotion_lease() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.fn_guard_paged_catalog_promotion_lease() FROM ec_app")
    # PostgreSQL invokes same-kind triggers alphabetically. ``contract`` therefore waits/rechecks
    # the owner profile before this final wall-clock lease check can suppress a stale success.
    op.execute("DROP TRIGGER IF EXISTS trg_catalog_refresh_promotion_lease ON public.catalog_refresh_runs")
    op.execute(
        """
        CREATE TRIGGER trg_catalog_refresh_promotion_lease
        BEFORE UPDATE OF status ON public.catalog_refresh_runs
        FOR EACH ROW EXECUTE FUNCTION public.fn_guard_paged_catalog_promotion_lease()
        """
    )
