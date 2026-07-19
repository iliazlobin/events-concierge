"""Persist the guarded San Jose Legistar page cursor and normalized stage.

Revision ID: 0087
Revises: 0086
Create Date: 2026-07-18

P15b makes the multi-request civic feed resumable without persisting a provider response document.
The application role receives only fixed ``SECURITY DEFINER`` transitions: it cannot create a
cursor, write a staged row, inspect a lease, or mark a partial crawl successful (FR-10.3/10.4,
NFR-1/NFR-8, ADR-001/003/005).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0087"
down_revision: str | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add capability-only P15b state, source revisions, and lease-fenced transitions."""
    _add_source_revision()
    _add_paged_refresh_tables()
    _allow_paused_refresh_runs()
    _replace_claim_capability()
    _create_paged_refresh_capabilities()
    _revoke_tables_and_grant_capabilities()


def downgrade() -> None:
    """Remove P15b state without re-enabling any source or widening app-role access."""
    for signature in (
        "public.fn_promote_paged_catalog_refresh(text, text, uuid, integer, integer, integer)",
        "public.fn_read_paged_catalog_refresh_stage(text, text, uuid, integer)",
        "public.fn_abort_paged_catalog_refresh(text, text, uuid, text)",
        "public.fn_pause_paged_catalog_refresh(text, text, uuid, text)",
        "public.fn_stage_paged_catalog_refresh_page(text, text, uuid, integer, integer, integer, jsonb, jsonb)",
        "public.fn_prepare_paged_catalog_refresh(text, text, uuid, integer)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    # A paused run has no completed effect.  Convert it to the historic retryable state before the
    # old check is restored; this does not reactivate a source or make an HTTP request.
    op.execute(
        """
        UPDATE public.catalog_refresh_runs
        SET status = 'failed',
            completed_at = COALESCE(completed_at, pg_catalog.clock_timestamp()),
            lease_token = NULL,
            lease_expires_at = NULL,
            error = COALESCE(error, 'P15b paused refresh discarded during downgrade')
        WHERE status = 'paused'
        """
    )
    op.execute("DROP TABLE IF EXISTS public.catalog_refresh_stage_event_ids")
    op.execute("DROP TABLE IF EXISTS public.catalog_refresh_stage_candidates")
    op.execute("DROP TABLE IF EXISTS public.catalog_refresh_stage_pages")
    op.execute("DROP TABLE IF EXISTS public.catalog_refresh_progress")
    op.execute(
        "ALTER TABLE public.catalog_refresh_runs DROP CONSTRAINT IF EXISTS ck_catalog_refresh_runs_status"
    )
    op.execute(
        """
        ALTER TABLE public.catalog_refresh_runs
        ADD CONSTRAINT ck_catalog_refresh_runs_status
        CHECK (status IN ('running', 'succeeded', 'failed'))
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_catalog_sources_source_revision ON public.catalog_sources"
    )
    op.execute("DROP FUNCTION IF EXISTS public.fn_bump_catalog_source_revision()")
    op.execute("ALTER TABLE public.catalog_sources DROP COLUMN IF EXISTS source_revision")


def _add_source_revision() -> None:
    """Version request-shaping registry edits so a staged page can never cross contracts."""
    op.execute(
        """
        ALTER TABLE public.catalog_sources
        ADD COLUMN source_revision integer NOT NULL DEFAULT 1
        CHECK (source_revision > 0)
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_bump_catalog_source_revision()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF OLD.seed_url IS DISTINCT FROM NEW.seed_url
               OR OLD.approved_origins IS DISTINCT FROM NEW.approved_origins
               OR OLD.mode IS DISTINCT FROM NEW.mode
               OR OLD.handoff_only IS DISTINCT FROM NEW.handoff_only
               OR OLD.min_interval_ms IS DISTINCT FROM NEW.min_interval_ms
               OR OLD.page_limit IS DISTINCT FROM NEW.page_limit
            THEN
                NEW.source_revision := GREATEST(NEW.source_revision, OLD.source_revision + 1);
            ELSIF NEW.source_revision < OLD.source_revision THEN
                RAISE EXCEPTION 'catalog source revision cannot decrease';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_sources_source_revision
        BEFORE UPDATE ON public.catalog_sources
        FOR EACH ROW EXECUTE FUNCTION public.fn_bump_catalog_source_revision()
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.fn_bump_catalog_source_revision() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION public.fn_bump_catalog_source_revision() FROM ec_app")


def _add_paged_refresh_tables() -> None:
    """Store only normalized page facts; raw HTTP documents never enter PostgreSQL (NFR-1)."""
    op.execute(
        """
        CREATE TABLE public.catalog_refresh_progress (
            source_key              text NOT NULL,
            run_key                 text NOT NULL,
            source_revision         integer NOT NULL CHECK (source_revision > 0),
            window_start_day        date NOT NULL,
            page_limit              integer NOT NULL CHECK (page_limit BETWEEN 1 AND 5),
            next_page               integer NOT NULL DEFAULT 0 CHECK (next_page BETWEEN 0 AND 5),
            terminal_page           integer,
            staged_raw_count        integer NOT NULL DEFAULT 0 CHECK (staged_raw_count >= 0),
            staged_candidate_count  integer NOT NULL DEFAULT 0 CHECK (staged_candidate_count >= 0),
            created_at              timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            updated_at              timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (source_key, run_key),
            FOREIGN KEY (source_key, run_key)
                REFERENCES public.catalog_refresh_runs(source_key, run_key) ON DELETE CASCADE,
            CHECK (next_page <= page_limit),
            CHECK (terminal_page IS NULL OR terminal_page BETWEEN 0 AND page_limit - 1),
            CHECK (staged_candidate_count <= staged_raw_count)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.catalog_refresh_stage_pages (
            source_key       text NOT NULL,
            run_key          text NOT NULL,
            page_number      integer NOT NULL CHECK (page_number BETWEEN 0 AND 4),
            raw_count        integer NOT NULL CHECK (raw_count BETWEEN 0 AND 100),
            candidate_count  integer NOT NULL CHECK (candidate_count BETWEEN 0 AND 100),
            created_at       timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (source_key, run_key, page_number),
            FOREIGN KEY (source_key, run_key)
                REFERENCES public.catalog_refresh_progress(source_key, run_key) ON DELETE CASCADE,
            CHECK (candidate_count <= raw_count)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.catalog_refresh_stage_candidates (
            source_key         text NOT NULL,
            run_key            text NOT NULL,
            page_number        integer NOT NULL CHECK (page_number BETWEEN 0 AND 4),
            source             text NOT NULL CHECK (source = 'public_jsonld'),
            source_event_id    text NOT NULL CHECK (char_length(source_event_id) BETWEEN 1 AND 512),
            title              text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 5000),
            start_at           timestamptz NOT NULL,
            end_at             timestamptz,
            registration_url   text NOT NULL CHECK (char_length(registration_url) BETWEEN 9 AND 4096),
            venue_name         text,
            city               text,
            description        text NOT NULL CHECK (char_length(description) <= 20000),
            price_status       text NOT NULL CHECK (price_status IN ('free', 'paid', 'unknown')),
            content_hash       text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at         timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (source_key, run_key, source_event_id),
            FOREIGN KEY (source_key, run_key, page_number)
                REFERENCES public.catalog_refresh_stage_pages(source_key, run_key, page_number)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.catalog_refresh_stage_event_ids (
            source_key       text NOT NULL,
            run_key          text NOT NULL,
            page_number      integer NOT NULL CHECK (page_number BETWEEN 0 AND 4),
            source_event_id  text NOT NULL CHECK (source_event_id ~ '^[1-9][0-9]{0,18}$'),
            PRIMARY KEY (source_key, run_key, source_event_id),
            FOREIGN KEY (source_key, run_key, page_number)
                REFERENCES public.catalog_refresh_stage_pages(source_key, run_key, page_number)
                ON DELETE CASCADE
        )
        """
    )


def _allow_paused_refresh_runs() -> None:
    """Make a staged nonterminal page reclaimable without discarding its durable cursor."""
    op.execute(
        "ALTER TABLE public.catalog_refresh_runs DROP CONSTRAINT IF EXISTS catalog_refresh_runs_status_check"
    )
    op.execute(
        "ALTER TABLE public.catalog_refresh_runs DROP CONSTRAINT IF EXISTS ck_catalog_refresh_runs_status"
    )
    op.execute(
        """
        ALTER TABLE public.catalog_refresh_runs
        ADD CONSTRAINT ck_catalog_refresh_runs_status
        CHECK (status IN ('running', 'paused', 'succeeded', 'failed'))
        """
    )


def _replace_claim_capability() -> None:
    """Allow an exact P15b paused cursor to be reclaimed under a fresh database lease."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.fn_claim_catalog_refresh(
            p_source_key text,
            p_run_key text,
            p_lease_seconds integer,
            p_lease_token uuid
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_now timestamptz;
            v_claimed boolean := false;
            v_status text;
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_lease_seconds IS NULL
               OR p_lease_seconds < 1
               OR p_lease_seconds > 3600
               OR p_lease_token IS NULL
            THEN
                RETURN 'invalid';
            END IF;

            v_now := pg_catalog.clock_timestamp();
            IF NOT EXISTS (
                SELECT 1
                FROM public.catalog_sources AS source
                WHERE source.source_key = p_source_key
                  AND source.enabled
                  AND source.handoff_only
                  AND source.reviewed_at IS NOT NULL
                  AND source.reviewed_at <= v_now
                  AND (
                      source.review_expires_at IS NULL
                      OR source.review_expires_at > v_now
                  )
            ) THEN
                RETURN 'unavailable';
            END IF;

            INSERT INTO public.catalog_refresh_runs
                (source_key, run_key, status, lease_token, started_at, lease_expires_at,
                 attempt_count)
            VALUES
                (p_source_key, p_run_key, 'running', p_lease_token, v_now,
                 v_now + (p_lease_seconds * INTERVAL '1 second'), 1)
            ON CONFLICT (source_key, run_key) DO UPDATE
            SET status = 'running',
                lease_token = EXCLUDED.lease_token,
                started_at = EXCLUDED.started_at,
                lease_expires_at = EXCLUDED.lease_expires_at,
                completed_at = NULL,
                candidate_count = NULL,
                canonical_count = NULL,
                error = NULL,
                attempt_count = public.catalog_refresh_runs.attempt_count + 1
            WHERE public.catalog_refresh_runs.status IN ('failed', 'paused')
               OR (
                   public.catalog_refresh_runs.status = 'running'
                   AND public.catalog_refresh_runs.lease_expires_at <= v_now
               )
            RETURNING true INTO v_claimed;
            IF v_claimed THEN
                RETURN 'acquired';
            END IF;

            SELECT refresh.status
            INTO v_status
            FROM public.catalog_refresh_runs AS refresh
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key;
            IF v_status = 'succeeded' THEN
                RETURN 'succeeded';
            END IF;
            RETURN 'busy';
        END;
        $$
        """
    )


def _create_paged_refresh_capabilities() -> None:
    """Create the six fixed P15b transitions; every one validates source, cursor, and lease."""
    op.execute(
        """
        CREATE FUNCTION public.fn_prepare_paged_catalog_refresh(
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
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
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
              AND source.source_key = 'san-jose-legistar-meetings'
              AND source.mode = 'san_jose_legistar'
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
        """
        CREATE FUNCTION public.fn_stage_paged_catalog_refresh_page(
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

            -- Validate the full JSON input before inserting anything.  This keeps an invalid
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

            v_now := pg_catalog.clock_timestamp();
            SELECT refresh.* INTO v_run
            FROM public.catalog_refresh_runs AS refresh
            WHERE refresh.source_key = p_source_key AND refresh.run_key = p_run_key
            FOR UPDATE;
            IF NOT FOUND
               OR v_run.status <> 'running'
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
              AND refresh.lease_token = p_lease_token;
            IF NOT FOUND THEN
                RETURN 'lease_lost';
            END IF;
            RETURN 'more';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_pause_paged_catalog_refresh(
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
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_lease_token IS NULL
               OR p_error IS NULL
            THEN
                RETURN false;
            END IF;
            UPDATE public.catalog_refresh_runs AS refresh
            SET status = 'paused',
                lease_token = NULL,
                lease_expires_at = NULL,
                error = left(p_error, 2000)
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key
              AND refresh.status = 'running'
              AND refresh.lease_token = p_lease_token
              AND refresh.lease_expires_at > pg_catalog.clock_timestamp();
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_abort_paged_catalog_refresh(
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
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
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
              AND refresh.lease_token = p_lease_token;
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
    op.execute(
        """
        CREATE FUNCTION public.fn_read_paged_catalog_refresh_stage(
            p_source_key text,
            p_run_key text,
            p_lease_token uuid,
            p_source_revision integer
        )
        RETURNS TABLE(
            source text,
            source_event_id text,
            title text,
            start_at timestamptz,
            end_at timestamptz,
            registration_url text,
            venue_name text,
            city text,
            description text,
            price_status text,
            content_hash text
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_lease_token IS NULL
               OR p_source_revision IS NULL OR p_source_revision < 1
            THEN
                RETURN;
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
                RETURN;
            END IF;
            RETURN QUERY
            SELECT candidate.source,
                   candidate.source_event_id,
                   candidate.title,
                   candidate.start_at,
                   candidate.end_at,
                   candidate.registration_url,
                   candidate.venue_name,
                   candidate.city,
                   candidate.description,
                   candidate.price_status,
                   candidate.content_hash
            FROM public.catalog_refresh_stage_candidates AS candidate
            WHERE candidate.source_key = p_source_key AND candidate.run_key = p_run_key
            ORDER BY candidate.page_number, candidate.source_event_id;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_promote_paged_catalog_refresh(
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
              AND refresh.lease_token = p_lease_token;
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


def _revoke_tables_and_grant_capabilities() -> None:
    """Keep all P15b state private to owner functions and grant only exact runtime calls."""
    for table in (
        "public.catalog_refresh_progress",
        "public.catalog_refresh_stage_pages",
        "public.catalog_refresh_stage_candidates",
        "public.catalog_refresh_stage_event_ids",
    ):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM ec_app")
    for signature in (
        "public.fn_claim_catalog_refresh(text, text, integer, uuid)",
        "public.fn_prepare_paged_catalog_refresh(text, text, uuid, integer)",
        "public.fn_stage_paged_catalog_refresh_page(text, text, uuid, integer, integer, integer, jsonb, jsonb)",
        "public.fn_pause_paged_catalog_refresh(text, text, uuid, text)",
        "public.fn_abort_paged_catalog_refresh(text, text, uuid, text)",
        "public.fn_read_paged_catalog_refresh_stage(text, text, uuid, integer)",
        "public.fn_promote_paged_catalog_refresh(text, text, uuid, integer, integer, integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
