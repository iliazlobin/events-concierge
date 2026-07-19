"""Guard reviewed-source configuration and catalog-refresh run transitions.

Revision ID: 0066
Revises: 0065
Create Date: 2026-07-17

The reviewed source registry is the direct authority for a public-catalog fetch's seed URL and
approved origins.  The non-superuser application role may read that public configuration, but it
must not create, redirect, disable, or unreview sources; refresh-run state is advanced only through
fixed, lease-fenced capabilities using the database clock (FR-10.3, NFR-8, ADR-001/004).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace raw app-role source/control-plane mutation with fixed capabilities."""
    _create_refresh_run_capabilities()
    _revoke_tables_and_grant_capabilities()


def downgrade() -> None:
    """Restore the preceding direct app-role grants when this isolated hardening slice rolls back."""
    for signature in (
        "public.fn_get_catalog_refresh_run(text, text)",
        "public.fn_list_due_catalog_refreshes(timestamptz, integer)",
        "public.fn_fail_catalog_refresh(text, text, uuid, text)",
        "public.fn_complete_catalog_refresh(text, text, uuid, integer, integer)",
        "public.fn_claim_catalog_refresh(text, text, integer, uuid)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.catalog_sources TO ec_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.catalog_refresh_runs TO ec_app"
    )


def _create_refresh_run_capabilities() -> None:
    """Create the exact registry-read and refresh-run lease transitions used by the worker."""
    op.execute(
        """
        CREATE FUNCTION public.fn_claim_catalog_refresh(
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
            WHERE public.catalog_refresh_runs.status = 'failed'
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
    op.execute(
        """
        CREATE FUNCTION public.fn_complete_catalog_refresh(
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
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
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
              AND refresh.lease_token = p_lease_token;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_fail_catalog_refresh(
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
                lease_expires_at = NULL,
                error = left(p_error, 2000)
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key
              AND refresh.status = 'running'
              AND refresh.lease_token = p_lease_token;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_due_catalog_refreshes(
            p_now timestamptz,
            p_limit integer
        )
        RETURNS TABLE(
            source_key text,
            display_name text,
            publisher text,
            seed_url text,
            approved_origins text[],
            region text,
            mode text,
            handoff_only boolean,
            enabled boolean,
            reviewed_at timestamptz,
            review_expires_at timestamptz,
            refresh_interval_minutes integer,
            min_interval_ms integer,
            page_limit integer,
            due_at timestamptz,
            last_succeeded_at timestamptz
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_now IS NULL OR p_limit IS NULL OR p_limit < 1 OR p_limit > 500 THEN
                RETURN;
            END IF;

            RETURN QUERY
            WITH latest_success AS (
                SELECT DISTINCT ON (refresh.source_key)
                       refresh.source_key,
                       refresh.completed_at AS last_succeeded_at
                FROM public.catalog_refresh_runs AS refresh
                WHERE refresh.status = 'succeeded'
                  AND refresh.completed_at IS NOT NULL
                ORDER BY refresh.source_key, refresh.completed_at DESC, refresh.run_key DESC
            ), eligible_sources AS (
                SELECT source.source_key AS eligible_source_key,
                       source.display_name AS eligible_display_name,
                       source.publisher AS eligible_publisher,
                       source.seed_url AS eligible_seed_url,
                       source.approved_origins AS eligible_approved_origins,
                       source.region AS eligible_region,
                       source.mode AS eligible_mode,
                       source.handoff_only AS eligible_handoff_only,
                       source.enabled AS eligible_enabled,
                       source.reviewed_at AS eligible_reviewed_at,
                       source.review_expires_at AS eligible_review_expires_at,
                       source.refresh_interval_minutes AS eligible_refresh_interval_minutes,
                       source.min_interval_ms AS eligible_min_interval_ms,
                       source.page_limit AS eligible_page_limit,
                       success.last_succeeded_at AS eligible_last_succeeded_at,
                       COALESCE(
                           success.last_succeeded_at
                               + source.refresh_interval_minutes * INTERVAL '1 minute',
                           source.reviewed_at
                       ) AS eligible_due_at
                FROM public.catalog_sources AS source
                LEFT JOIN latest_success AS success
                  ON success.source_key = source.source_key
                WHERE source.enabled
                  AND source.handoff_only
                  AND source.reviewed_at IS NOT NULL
                  AND source.reviewed_at <= p_now
                  AND (
                      source.review_expires_at IS NULL
                      OR source.review_expires_at > p_now
                  )
            )
            SELECT eligible.eligible_source_key,
                   eligible.eligible_display_name,
                   eligible.eligible_publisher,
                   eligible.eligible_seed_url,
                   eligible.eligible_approved_origins,
                   eligible.eligible_region,
                   eligible.eligible_mode,
                   eligible.eligible_handoff_only,
                   eligible.eligible_enabled,
                   eligible.eligible_reviewed_at,
                   eligible.eligible_review_expires_at,
                   eligible.eligible_refresh_interval_minutes,
                   eligible.eligible_min_interval_ms,
                   eligible.eligible_page_limit,
                   eligible.eligible_due_at,
                   eligible.eligible_last_succeeded_at
            FROM eligible_sources AS eligible
            WHERE eligible.eligible_due_at <= p_now
            ORDER BY eligible.eligible_due_at ASC, eligible.eligible_source_key ASC
            LIMIT p_limit;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_get_catalog_refresh_run(
            p_source_key text,
            p_run_key text
        )
        RETURNS TABLE(
            source_key text,
            run_key text,
            status text,
            started_at timestamptz,
            lease_expires_at timestamptz,
            completed_at timestamptz,
            candidate_count integer,
            canonical_count integer,
            error text,
            attempt_count integer
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
            THEN
                RETURN;
            END IF;

            RETURN QUERY
            SELECT refresh.source_key,
                   refresh.run_key,
                   refresh.status,
                   refresh.started_at,
                   refresh.lease_expires_at,
                   refresh.completed_at,
                   refresh.candidate_count,
                   refresh.canonical_count,
                   refresh.error,
                   refresh.attempt_count
            FROM public.catalog_refresh_runs AS refresh
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key;
        END;
        $$
        """
    )


def _revoke_tables_and_grant_capabilities() -> None:
    """Leave ec_app static registry reads plus exact lease-fenced row-transition operations."""
    for table in ("public.catalog_sources", "public.catalog_refresh_runs"):
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM ec_app")
    op.execute("GRANT SELECT ON TABLE public.catalog_sources TO ec_app")

    for signature in (
        "public.fn_claim_catalog_refresh(text, text, integer, uuid)",
        "public.fn_complete_catalog_refresh(text, text, uuid, integer, integer)",
        "public.fn_fail_catalog_refresh(text, text, uuid, text)",
        "public.fn_list_due_catalog_refreshes(timestamptz, integer)",
        "public.fn_get_catalog_refresh_run(text, text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
