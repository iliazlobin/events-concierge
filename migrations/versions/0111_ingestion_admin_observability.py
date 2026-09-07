"""Add bounded source analysis and build provenance to the ingestion admin.

Revision ID: 0111
Revises: 0110
Create Date: 2026-07-23

Only fixed SECURITY DEFINER projections cross the runtime-role boundary. The projections expose
reviewed public-source configuration, aggregate run health, normalized errors, and bounded release
identity. Raw provider payloads/errors, command actors, leases, and tenant data remain private.
Legacy runs deliberately retain NULL provenance.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0111"
down_revision: str | None = "0110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_LIST = "(text,text,text,text,text,text,boolean,integer,integer)"
_SOURCE_COUNT = "(text,text,text,text,text,text,boolean)"
_FILTERS = "(boolean)"
_RUN_FACTS = "(boolean,text,timestamptz)"
_RUN_LIST = "(text,text,text,text,text,integer,boolean,integer,integer)"
_RUN_COUNT = "(text,text,text,text,text,integer,boolean)"
_SOURCE_DETAIL = "(text,boolean)"
_SOURCE_SUMMARY = "(text,integer,boolean)"
_SOURCE_HISTORY = "(text,integer,integer,boolean)"
_COMMAND_LIST = "(integer)"
_COMMAND_GET = "(uuid)"
_ENQUEUE = "(uuid,text,text,text,text,text)"
_CLAIM = "(integer,integer,text,text)"
_LEGACY_ENQUEUE = "(uuid,text,text,text)"
_LEGACY_CLAIM = "(integer,integer)"


def upgrade() -> None:
    """Install fixture-aware facets, source analytics, and future-run provenance."""
    _add_command_provenance()
    _create_source_filter_capabilities()
    _create_run_fact_capability()
    _create_source_analysis_capabilities()
    _create_command_provenance_capabilities()
    _grant_capabilities()


def downgrade() -> None:
    """Remove observability v2 while preserving the 0109/0110 admin plane."""
    op.execute(
        f"REVOKE ALL ON FUNCTION public.fn_enqueue_ingestion_admin_command_v2{_ENQUEUE} FROM ec_app"
    )
    for name, signature in (
        ("fn_claim_ingestion_admin_commands_v2", _CLAIM),
        ("fn_enqueue_ingestion_admin_command_v2", _ENQUEUE),
        ("fn_get_ingestion_admin_command_v2", _COMMAND_GET),
        ("fn_list_ingestion_admin_commands_v2", _COMMAND_LIST),
        ("fn_list_ingestion_admin_source_history", _SOURCE_HISTORY),
        ("fn_get_ingestion_admin_source_summary", _SOURCE_SUMMARY),
        ("fn_get_ingestion_admin_source_detail", _SOURCE_DETAIL),
        ("fn_count_ingestion_admin_runs_v2", _RUN_COUNT),
        ("fn_list_ingestion_admin_runs_v2", _RUN_LIST),
        ("fn_ingestion_admin_run_facts_v2", _RUN_FACTS),
        ("fn_list_ingestion_admin_filter_values", _FILTERS),
        ("fn_count_ingestion_admin_sources_v2", _SOURCE_COUNT),
        ("fn_list_ingestion_admin_sources_v2", _SOURCE_LIST),
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{name}{signature}")
    op.execute("DROP INDEX IF EXISTS public.ix_catalog_refresh_runs_admin_source_started")
    op.execute("DROP INDEX IF EXISTS public.ix_catalog_refresh_runs_admin_started")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.fn_enqueue_ingestion_admin_command"
        f"{_LEGACY_ENQUEUE} TO ec_app"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.fn_claim_ingestion_admin_commands"
        f"{_LEGACY_CLAIM} TO ec_app"
    )
    op.execute(
        """
        ALTER TABLE public.ingestion_admin_commands
        DROP CONSTRAINT IF EXISTS ck_ingestion_admin_commands_image_digest,
        DROP CONSTRAINT IF EXISTS ck_ingestion_admin_commands_release_revision,
        DROP CONSTRAINT IF EXISTS ck_ingestion_admin_commands_source_revision,
        DROP CONSTRAINT IF EXISTS ck_ingestion_admin_commands_executor_source_revision,
        DROP CONSTRAINT IF EXISTS ck_ingestion_admin_commands_executor_image_digest,
        DROP CONSTRAINT IF EXISTS ck_ingestion_admin_commands_executor_release_revision,
        DROP COLUMN IF EXISTS executor_image_digest,
        DROP COLUMN IF EXISTS executor_release_revision,
        DROP COLUMN IF EXISTS executor_source_revision,
        DROP COLUMN IF EXISTS image_digest,
        DROP COLUMN IF EXISTS release_revision,
        DROP COLUMN IF EXISTS source_revision
        """
    )


def _add_command_provenance() -> None:
    op.execute(
        """
        ALTER TABLE public.ingestion_admin_commands
        ADD COLUMN source_revision integer,
        ADD COLUMN release_revision text,
        ADD COLUMN image_digest text,
        ADD COLUMN executor_source_revision integer,
        ADD COLUMN executor_release_revision text,
        ADD COLUMN executor_image_digest text,
        ADD CONSTRAINT ck_ingestion_admin_commands_source_revision
            CHECK (source_revision IS NULL OR source_revision > 0),
        ADD CONSTRAINT ck_ingestion_admin_commands_release_revision
            CHECK (
                release_revision IS NULL
                OR release_revision ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            ),
        ADD CONSTRAINT ck_ingestion_admin_commands_image_digest
            CHECK (
                image_digest IS NULL
                OR image_digest ~ '^sha256:[0-9a-f]{64}$'
            ),
        ADD CONSTRAINT ck_ingestion_admin_commands_executor_source_revision
            CHECK (executor_source_revision IS NULL OR executor_source_revision > 0),
        ADD CONSTRAINT ck_ingestion_admin_commands_executor_release_revision
            CHECK (
                executor_release_revision IS NULL
                OR executor_release_revision ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            ),
        ADD CONSTRAINT ck_ingestion_admin_commands_executor_image_digest
            CHECK (
                executor_image_digest IS NULL
                OR executor_image_digest ~ '^sha256:[0-9a-f]{64}$'
            )
        """
    )


def _create_source_filter_capabilities() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_sources_v2(
            p_query text,
            p_state text,
            p_mode text,
            p_publisher text,
            p_region text,
            p_source_key text,
            p_include_fixtures boolean,
            p_limit integer,
            p_offset integer
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            publisher text,
            mode text,
            region text,
            seed_url text,
            enabled boolean,
            review_status text,
            effective_status text,
            policy_blocked boolean,
            due boolean,
            last_succeeded_at timestamptz,
            next_due_at timestamptz,
            event_count bigint,
            latest_run_key text,
            latest_run_status text,
            latest_run_started_at timestamptz,
            latest_run_completed_at timestamptz,
            latest_run_candidate_count integer,
            latest_run_canonical_count integer,
            latest_run_error text,
            latest_run_attempt_count integer,
            total_count bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_include_fixtures IS NULL
               OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_offset IS NULL OR p_offset < 0 OR p_offset > 100000
               OR (p_query IS NOT NULL AND char_length(p_query) > 200)
               OR p_state IS NULL
               OR p_state NOT IN ('all', 'active', 'due', 'blocked', 'failed')
               OR (p_mode IS NOT NULL AND (
                    char_length(p_mode) NOT BETWEEN 1 AND 80 OR p_mode ~ '[[:cntrl:]]'
               ))
               OR (p_publisher IS NOT NULL AND (
                    char_length(p_publisher) NOT BETWEEN 1 AND 300
                    OR p_publisher ~ '[[:cntrl:]]'
               ))
               OR (p_region IS NOT NULL AND (
                    char_length(p_region) NOT BETWEEN 1 AND 120 OR p_region ~ '[[:cntrl:]]'
               ))
               OR (
                   p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin source query is invalid';
            END IF;

            RETURN QUERY
            WITH filtered AS (
                SELECT source.*
                FROM public.fn_ingestion_admin_sources_base(p_include_fixtures) AS source
                WHERE (
                        p_query IS NULL
                        OR strpos(
                            lower(
                                source.source_key || ' ' || source.display_name || ' '
                                || source.publisher || ' ' || source.region || ' '
                                || source.mode || ' ' || source.seed_url
                            ),
                            lower(p_query)
                        ) > 0
                    )
                  AND (p_source_key IS NULL OR source.source_key = p_source_key)
                  AND (p_mode IS NULL OR source.mode = p_mode)
                  AND (p_publisher IS NULL OR source.publisher = p_publisher)
                  AND (p_region IS NULL OR source.region = p_region)
                  AND (
                      p_state = 'all'
                      OR (
                          p_state = 'active'
                          AND source.effective_status IN ('active', 'due', 'running')
                      )
                      OR (p_state = 'due' AND source.due)
                      OR (
                          p_state = 'blocked'
                          AND source.effective_status IN (
                              'disabled', 'unreviewed', 'review_expired', 'policy_blocked'
                          )
                      )
                      OR (p_state = 'failed' AND source.latest_run_status = 'failed')
                  )
            )
            SELECT filtered.source_key,
                   filtered.display_name,
                   filtered.publisher,
                   filtered.mode,
                   filtered.region,
                   filtered.seed_url,
                   filtered.enabled,
                   filtered.review_status,
                   filtered.effective_status,
                   filtered.policy_blocked,
                   filtered.due,
                   filtered.last_succeeded_at,
                   filtered.next_due_at,
                   filtered.event_count,
                   filtered.latest_run_key,
                   filtered.latest_run_status,
                   filtered.latest_run_started_at,
                   filtered.latest_run_completed_at,
                   filtered.latest_run_candidate_count,
                   filtered.latest_run_canonical_count,
                   filtered.latest_run_error,
                   filtered.latest_run_attempt_count,
                   count(*) OVER () AS total_count
            FROM filtered
            ORDER BY filtered.source_key
            LIMIT p_limit
            OFFSET p_offset;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_count_ingestion_admin_sources_v2(
            p_query text,
            p_state text,
            p_mode text,
            p_publisher text,
            p_region text,
            p_source_key text,
            p_include_fixtures boolean
        )
        RETURNS bigint
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            result bigint;
        BEGIN
            IF p_include_fixtures IS NULL
               OR (p_query IS NOT NULL AND char_length(p_query) > 200)
               OR p_state IS NULL
               OR p_state NOT IN ('all', 'active', 'due', 'blocked', 'failed')
               OR (p_mode IS NOT NULL AND (
                    char_length(p_mode) NOT BETWEEN 1 AND 80 OR p_mode ~ '[[:cntrl:]]'
               ))
               OR (p_publisher IS NOT NULL AND (
                    char_length(p_publisher) NOT BETWEEN 1 AND 300
                    OR p_publisher ~ '[[:cntrl:]]'
               ))
               OR (p_region IS NOT NULL AND (
                    char_length(p_region) NOT BETWEEN 1 AND 120 OR p_region ~ '[[:cntrl:]]'
               ))
               OR (p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin source count query is invalid';
            END IF;
            SELECT count(*)
            INTO result
            FROM public.fn_ingestion_admin_sources_base(p_include_fixtures) AS source
            WHERE (
                    p_query IS NULL
                    OR strpos(
                        lower(
                            source.source_key || ' ' || source.display_name || ' '
                            || source.publisher || ' ' || source.region || ' '
                            || source.mode || ' ' || source.seed_url
                        ),
                        lower(p_query)
                    ) > 0
                )
              AND (p_source_key IS NULL OR source.source_key = p_source_key)
              AND (p_mode IS NULL OR source.mode = p_mode)
              AND (p_publisher IS NULL OR source.publisher = p_publisher)
              AND (p_region IS NULL OR source.region = p_region)
              AND (
                  p_state = 'all'
                  OR (
                      p_state = 'active'
                      AND source.effective_status IN ('active', 'due', 'running')
                  )
                  OR (p_state = 'due' AND source.due)
                  OR (
                      p_state = 'blocked'
                      AND source.effective_status IN (
                          'disabled', 'unreviewed', 'review_expired', 'policy_blocked'
                      )
                  )
                  OR (p_state = 'failed' AND source.latest_run_status = 'failed')
              );
            RETURN result;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_filter_values(
            p_include_fixtures boolean
        )
        RETURNS TABLE (
            dimension text,
            value text,
            source_count bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_include_fixtures IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin filter query is invalid';
            END IF;
            RETURN QUERY
            WITH sources AS MATERIALIZED (
                SELECT source.*
                FROM public.catalog_sources AS source
                WHERE p_include_fixtures
                   OR NOT public.fn_ingestion_admin_source_is_fixture(
                       source.source_key, source.publisher, source.seed_url
                   )
            ), values AS (
                SELECT 'mode'::text AS dimension, source.mode AS value, count(*) AS source_count
                FROM sources AS source
                GROUP BY source.mode
                UNION ALL
                SELECT 'publisher', source.publisher, count(*)
                FROM sources AS source
                GROUP BY source.publisher
                UNION ALL
                SELECT 'region', source.region, count(*)
                FROM sources AS source
                GROUP BY source.region
            )
            SELECT values.dimension, values.value, values.source_count
            FROM values
            WHERE values.value IS NOT NULL AND btrim(values.value) <> ''
            ORDER BY values.dimension, lower(values.value), values.value;
        END;
        $$
        """
    )


def _create_run_fact_capability() -> None:
    op.execute(
        """
        CREATE INDEX ix_catalog_refresh_runs_admin_source_started
        ON public.catalog_refresh_runs (source_key, started_at DESC, run_key DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_refresh_runs_admin_started
        ON public.catalog_refresh_runs (started_at DESC, source_key, run_key DESC)
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_ingestion_admin_run_facts_v2(
            p_include_fixtures boolean,
            p_source_key text,
            p_started_after timestamptz
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            publisher text,
            mode text,
            region text,
            run_key text,
            status text,
            started_at timestamptz,
            completed_at timestamptz,
            candidate_count integer,
            canonical_count integer,
            error text,
            attempt_count integer,
            duration_ms bigint,
            source_revision integer,
            release_revision text,
            image_digest text,
            provenance_status text,
            trigger text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH observed AS (
                SELECT pg_catalog.statement_timestamp() AS observed_at
            ), facts AS (
                SELECT refresh.source_key,
                       source.display_name,
                       source.publisher,
                       source.mode,
                       source.region,
                       refresh.run_key,
                       CASE
                           WHEN refresh.status = 'running'
                            AND (
                                refresh.lease_expires_at IS NULL
                                OR refresh.lease_expires_at <= observed.observed_at
                            )
                               THEN 'failed'
                           ELSE refresh.status
                       END AS projected_status,
                       refresh.started_at,
                       CASE
                           WHEN refresh.status = 'running'
                            AND (
                                refresh.lease_expires_at IS NULL
                                OR refresh.lease_expires_at <= observed.observed_at
                            )
                               THEN COALESCE(refresh.lease_expires_at, refresh.started_at)
                           ELSE refresh.completed_at
                       END AS projected_completed_at,
                       refresh.candidate_count,
                       refresh.canonical_count,
                       public.fn_normalize_catalog_refresh_error(
                           CASE
                               WHEN refresh.status = 'running'
                                AND (
                                    refresh.lease_expires_at IS NULL
                                    OR refresh.lease_expires_at <= observed.observed_at
                                )
                                   THEN 'lease expired'
                               ELSE refresh.error
                           END
                       ) AS safe_error,
                       refresh.attempt_count,
                       command.command_id AS admin_command_id,
                       command.source_revision AS command_source_revision,
                       command.executor_source_revision,
                       command.executor_release_revision AS execution_release_revision,
                       command.executor_image_digest AS execution_image_digest,
                       progress.source_revision AS progress_source_revision
                FROM public.catalog_refresh_runs AS refresh
                JOIN public.catalog_sources AS source
                  ON source.source_key = refresh.source_key
                CROSS JOIN observed
                LEFT JOIN public.ingestion_admin_commands AS command
                  ON command.action = 'refresh_source'
                 AND command.source_key = refresh.source_key
                 AND refresh.run_key = 'admin:' || command.command_id::text
                LEFT JOIN public.catalog_refresh_progress AS progress
                  ON progress.source_key = refresh.source_key
                 AND progress.run_key = refresh.run_key
                WHERE (
                        p_include_fixtures
                        OR NOT public.fn_ingestion_admin_source_is_fixture(
                            source.source_key, source.publisher, source.seed_url
                        )
                    )
                  AND (
                      p_include_fixtures
                      OR NOT public.fn_ingestion_admin_run_is_fixture(
                          refresh.run_key, refresh.error
                      )
                  )
                  AND (p_source_key IS NULL OR refresh.source_key = p_source_key)
                  AND (p_started_after IS NULL OR refresh.started_at >= p_started_after)
            )
            SELECT facts.source_key,
                   facts.display_name,
                   facts.publisher,
                   facts.mode,
                   facts.region,
                   facts.run_key,
                   facts.projected_status,
                   facts.started_at,
                   facts.projected_completed_at,
                   facts.candidate_count,
                   facts.canonical_count,
                   facts.safe_error,
                   facts.attempt_count,
                   CASE
                       WHEN facts.projected_completed_at IS NULL THEN NULL
                       ELSE GREATEST(
                           0,
                           floor(
                               extract(
                                   epoch FROM facts.projected_completed_at - facts.started_at
                               ) * 1000
                           )::bigint
                       )
                   END,
                   COALESCE(
                       facts.progress_source_revision,
                       facts.executor_source_revision,
                       facts.command_source_revision
                   ),
                   facts.execution_release_revision,
                   facts.execution_image_digest,
                   CASE
                       WHEN facts.admin_command_id IS NOT NULL
                        AND COALESCE(
                            facts.progress_source_revision,
                            facts.executor_source_revision,
                            facts.command_source_revision
                        ) IS NOT NULL
                        AND facts.execution_release_revision IS NOT NULL
                           THEN 'claim_recorded'
                       WHEN COALESCE(
                           facts.progress_source_revision,
                           facts.executor_source_revision,
                           facts.command_source_revision
                       ) IS NOT NULL
                           THEN 'source_revision_only'
                       ELSE 'legacy_unavailable'
                   END,
                   CASE
                       WHEN facts.admin_command_id IS NOT NULL THEN 'admin_source'
                       ELSE 'cadence_or_manual'
                   END
            FROM facts
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_runs_v2(
            p_status text,
            p_source_key text,
            p_mode text,
            p_publisher text,
            p_region text,
            p_window_hours integer,
            p_include_fixtures boolean,
            p_limit integer,
            p_offset integer
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            run_key text,
            status text,
            started_at timestamptz,
            completed_at timestamptz,
            candidate_count integer,
            canonical_count integer,
            error text,
            attempt_count integer,
            duration_ms bigint,
            source_revision integer,
            release_revision text,
            image_digest text,
            provenance_status text,
            trigger text,
            total_count bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_include_fixtures IS NULL
               OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_offset IS NULL OR p_offset < 0 OR p_offset > 100000
               OR (p_status IS NOT NULL
                   AND p_status NOT IN ('running', 'paused', 'succeeded', 'failed'))
               OR (p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$')
               OR (p_mode IS NOT NULL
                   AND char_length(p_mode) NOT BETWEEN 1 AND 80)
               OR (p_publisher IS NOT NULL
                   AND char_length(p_publisher) NOT BETWEEN 1 AND 300)
               OR (p_region IS NOT NULL
                   AND char_length(p_region) NOT BETWEEN 1 AND 120)
               OR (p_window_hours IS NOT NULL
                   AND (p_window_hours < 1 OR p_window_hours > 2160))
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin run query is invalid';
            END IF;

            RETURN QUERY
            WITH filtered AS (
                SELECT facts.*
                FROM public.fn_ingestion_admin_run_facts_v2(
                    p_include_fixtures,
                    p_source_key,
                    CASE
                        WHEN p_window_hours IS NULL THEN NULL
                        ELSE pg_catalog.statement_timestamp()
                            - p_window_hours * INTERVAL '1 hour'
                    END
                ) AS facts
                WHERE (p_status IS NULL OR facts.status = p_status)
                  AND (p_source_key IS NULL OR facts.source_key = p_source_key)
                  AND (p_mode IS NULL OR facts.mode = p_mode)
                  AND (p_publisher IS NULL OR facts.publisher = p_publisher)
                  AND (p_region IS NULL OR facts.region = p_region)
                  AND (
                      p_window_hours IS NULL
                      OR facts.started_at >= pg_catalog.statement_timestamp()
                          - p_window_hours * INTERVAL '1 hour'
                  )
            )
            SELECT filtered.source_key,
                   filtered.display_name,
                   filtered.run_key,
                   filtered.status,
                   filtered.started_at,
                   filtered.completed_at,
                   filtered.candidate_count,
                   filtered.canonical_count,
                   filtered.error,
                   filtered.attempt_count,
                   filtered.duration_ms,
                   filtered.source_revision,
                   filtered.release_revision,
                   filtered.image_digest,
                   filtered.provenance_status,
                   filtered.trigger,
                   count(*) OVER () AS total_count
            FROM filtered
            ORDER BY filtered.started_at DESC, filtered.source_key, filtered.run_key
            LIMIT p_limit
            OFFSET p_offset;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_count_ingestion_admin_runs_v2(
            p_status text,
            p_source_key text,
            p_mode text,
            p_publisher text,
            p_region text,
            p_window_hours integer,
            p_include_fixtures boolean
        )
        RETURNS bigint
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            result bigint;
        BEGIN
            IF p_include_fixtures IS NULL
               OR (p_status IS NOT NULL
                   AND p_status NOT IN ('running', 'paused', 'succeeded', 'failed'))
               OR (p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$')
               OR (p_mode IS NOT NULL
                   AND char_length(p_mode) NOT BETWEEN 1 AND 80)
               OR (p_publisher IS NOT NULL
                   AND char_length(p_publisher) NOT BETWEEN 1 AND 300)
               OR (p_region IS NOT NULL
                   AND char_length(p_region) NOT BETWEEN 1 AND 120)
               OR (p_window_hours IS NOT NULL
                   AND (p_window_hours < 1 OR p_window_hours > 2160))
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin run count query is invalid';
            END IF;
            SELECT count(*)
            INTO result
            FROM public.fn_ingestion_admin_run_facts_v2(
                p_include_fixtures,
                p_source_key,
                CASE
                    WHEN p_window_hours IS NULL THEN NULL
                    ELSE pg_catalog.statement_timestamp()
                        - p_window_hours * INTERVAL '1 hour'
                END
            ) AS facts
            WHERE (p_status IS NULL OR facts.status = p_status)
              AND (p_source_key IS NULL OR facts.source_key = p_source_key)
              AND (p_mode IS NULL OR facts.mode = p_mode)
              AND (p_publisher IS NULL OR facts.publisher = p_publisher)
              AND (p_region IS NULL OR facts.region = p_region)
              AND (
                  p_window_hours IS NULL
                  OR facts.started_at >= pg_catalog.statement_timestamp()
                      - p_window_hours * INTERVAL '1 hour'
              );
            RETURN result;
        END;
        $$
        """
    )


def _create_source_analysis_capabilities() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_admin_source_detail(
            p_source_key text,
            p_include_fixtures boolean
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            publisher text,
            mode text,
            region text,
            seed_url text,
            enabled boolean,
            handoff_only boolean,
            approved_origins text[],
            reviewed_at timestamptz,
            review_expires_at timestamptz,
            refresh_interval_minutes integer,
            min_interval_ms integer,
            page_limit integer,
            source_revision integer,
            review_status text,
            effective_status text,
            policy_blocked boolean,
            due boolean,
            last_succeeded_at timestamptz,
            next_due_at timestamptz,
            event_count bigint,
            latest_run_key text,
            latest_run_status text,
            latest_run_started_at timestamptz,
            latest_run_completed_at timestamptz,
            latest_run_candidate_count integer,
            latest_run_canonical_count integer,
            latest_run_error text,
            latest_run_attempt_count integer,
            latest_run_duration_ms bigint,
            latest_run_source_revision integer,
            latest_run_release_revision text,
            latest_run_image_digest text,
            latest_run_provenance_status text,
            latest_run_trigger text
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_include_fixtures IS NULL
            THEN
                RETURN;
            END IF;
            RETURN QUERY
            WITH observed AS (
                SELECT pg_catalog.statement_timestamp() AS observed_at
            ), policy AS (
                SELECT policy_row.source IS NOT NULL AS present,
                       COALESCE(policy_row.quarantined, false) AS quarantined,
                       COALESCE(
                           (policy_row.automation_allowed ->> 'browser')::boolean,
                           false
                       ) AS browser_allowed
                FROM (SELECT 1) AS singleton
                LEFT JOIN public.source_policy AS policy_row
                  ON policy_row.source = 'public_jsonld'
            ), selected_source AS MATERIALIZED (
                SELECT source.*
                FROM public.catalog_sources AS source
                WHERE source.source_key = p_source_key
                  AND (
                      p_include_fixtures
                      OR NOT public.fn_ingestion_admin_source_is_fixture(
                          source.source_key, source.publisher, source.seed_url
                      )
                  )
            ), runs AS MATERIALIZED (
                SELECT run.*
                FROM public.fn_ingestion_admin_run_facts_v2(
                    p_include_fixtures, p_source_key, NULL
                ) AS run
            ), latest_run AS (
                SELECT run.*
                FROM runs AS run
                ORDER BY run.started_at DESC, run.run_key DESC
                LIMIT 1
            ), latest_success AS (
                SELECT run.completed_at
                FROM runs AS run
                WHERE run.status = 'succeeded' AND run.completed_at IS NOT NULL
                ORDER BY run.completed_at DESC, run.run_key DESC
                LIMIT 1
            ), observations AS (
                SELECT count(
                           DISTINCT observation.canonical_event_id
                       ) AS event_count
                FROM public.catalog_event_observations AS observation
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = observation.canonical_event_id
                JOIN public.catalog_refresh_runs AS observation_run
                  ON observation_run.source_key = observation.source_key
                 AND observation_run.run_key = observation.last_run_key
                CROSS JOIN observed
                WHERE observation.source_key = p_source_key
                  AND event.start_at >= observed.observed_at
                  AND event.event_status <> 'cancelled'
                  AND (
                      p_include_fixtures
                      OR NOT public.fn_ingestion_admin_run_is_fixture(
                          observation_run.run_key, observation_run.error
                      )
                  )
            ), facts AS (
                SELECT source.*,
                       success.completed_at AS succeeded_at,
                       COALESCE(
                           success.completed_at
                               + source.refresh_interval_minutes * INTERVAL '1 minute',
                           source.reviewed_at
                       ) AS due_at,
                       run.run_key,
                       run.status AS run_status,
                       run.started_at AS run_started_at,
                       run.completed_at AS run_completed_at,
                       run.candidate_count AS run_candidate_count,
                       run.canonical_count AS run_canonical_count,
                       run.error AS run_error,
                       run.attempt_count AS run_attempt_count,
                       run.duration_ms AS run_duration_ms,
                       run.source_revision AS run_source_revision,
                       run.release_revision AS run_release_revision,
                       run.image_digest AS run_image_digest,
                       run.provenance_status AS run_provenance_status,
                       run.trigger AS run_trigger,
                       policy.present,
                       policy.quarantined,
                       policy.browser_allowed,
                       COALESCE(observations.event_count, 0) AS source_event_count,
                       observed.observed_at
                FROM selected_source AS source
                CROSS JOIN policy
                CROSS JOIN observed
                CROSS JOIN observations
                LEFT JOIN latest_success AS success ON true
                LEFT JOIN latest_run AS run ON true
            )
            SELECT facts.source_key,
                   facts.display_name,
                   facts.publisher,
                   facts.mode,
                   facts.region,
                   facts.seed_url,
                   facts.enabled,
                   facts.handoff_only,
                   facts.approved_origins,
                   facts.reviewed_at,
                   facts.review_expires_at,
                   facts.refresh_interval_minutes,
                   facts.min_interval_ms,
                   facts.page_limit,
                   facts.source_revision,
                   CASE
                       WHEN facts.reviewed_at IS NULL THEN 'unreviewed'
                       WHEN facts.review_expires_at IS NOT NULL
                        AND facts.review_expires_at <= facts.observed_at THEN 'expired'
                       ELSE 'reviewed'
                   END,
                   CASE
                       WHEN NOT facts.enabled THEN 'disabled'
                       WHEN facts.reviewed_at IS NULL THEN 'unreviewed'
                       WHEN facts.review_expires_at IS NOT NULL
                        AND facts.review_expires_at <= facts.observed_at THEN 'review_expired'
                       WHEN NOT facts.present OR facts.quarantined OR NOT facts.browser_allowed
                           THEN 'policy_blocked'
                       WHEN facts.run_status = 'running' THEN 'running'
                       WHEN facts.due_at IS NOT NULL AND facts.due_at <= facts.observed_at
                           THEN 'due'
                       ELSE 'active'
                   END,
                   NOT facts.present OR facts.quarantined OR NOT facts.browser_allowed,
                   (
                       facts.enabled
                       AND facts.handoff_only
                       AND facts.reviewed_at IS NOT NULL
                       AND (
                           facts.review_expires_at IS NULL
                           OR facts.review_expires_at > facts.observed_at
                       )
                       AND facts.due_at IS NOT NULL
                       AND facts.due_at <= facts.observed_at
                   ),
                   facts.succeeded_at,
                   facts.due_at,
                   facts.source_event_count,
                   facts.run_key,
                   facts.run_status,
                   facts.run_started_at,
                   facts.run_completed_at,
                   facts.run_candidate_count,
                   facts.run_canonical_count,
                   facts.run_error,
                   facts.run_attempt_count,
                   facts.run_duration_ms,
                   facts.run_source_revision,
                   facts.run_release_revision,
                   facts.run_image_digest,
                   COALESCE(facts.run_provenance_status, 'legacy_unavailable'),
                   COALESCE(facts.run_trigger, 'cadence_or_manual')
            FROM facts;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_admin_source_summary(
            p_source_key text,
            p_window_hours integer,
            p_include_fixtures boolean
        )
        RETURNS TABLE (
            generated_at timestamptz,
            window_starts_at timestamptz,
            total_runs bigint,
            succeeded_runs bigint,
            failed_runs bigint,
            running_runs bigint,
            success_rate double precision,
            candidate_count bigint,
            canonical_count bigint,
            yield_rate double precision,
            average_duration_ms bigint,
            p95_duration_ms bigint,
            latest_success_at timestamptz,
            latest_failure_at timestamptz
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_window_hours IS NULL
               OR p_window_hours < 1 OR p_window_hours > 2160
               OR p_include_fixtures IS NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin source summary query is invalid';
            END IF;
            RETURN QUERY
            WITH observed AS (
                SELECT pg_catalog.statement_timestamp() AS generated_at
            ), facts AS MATERIALIZED (
                SELECT run.*
                FROM public.fn_ingestion_admin_run_facts_v2(
                    p_include_fixtures,
                    p_source_key,
                    pg_catalog.statement_timestamp()
                        - p_window_hours * INTERVAL '1 hour'
                ) AS run
                CROSS JOIN observed
                WHERE run.source_key = p_source_key
                  AND run.started_at >= observed.generated_at
                      - p_window_hours * INTERVAL '1 hour'
            ), aggregate AS (
                SELECT count(*) AS total_runs,
                       count(*) FILTER (WHERE facts.status = 'succeeded') AS succeeded_runs,
                       count(*) FILTER (WHERE facts.status = 'failed') AS failed_runs,
                       count(*) FILTER (
                           WHERE facts.status IN ('running', 'paused')
                       ) AS running_runs,
                       COALESCE(sum(facts.candidate_count), 0) AS candidate_count,
                       COALESCE(sum(facts.canonical_count), 0) AS canonical_count,
                       round(avg(facts.duration_ms))::bigint AS average_duration_ms,
                       round(
                           percentile_cont(0.95) WITHIN GROUP (
                               ORDER BY facts.duration_ms
                           )
                       )::bigint AS p95_duration_ms,
                       max(facts.completed_at) FILTER (
                           WHERE facts.status = 'succeeded'
                       ) AS latest_success_at,
                       max(facts.completed_at) FILTER (
                           WHERE facts.status = 'failed'
                       ) AS latest_failure_at
                FROM facts
            )
            SELECT observed.generated_at,
                   observed.generated_at - p_window_hours * INTERVAL '1 hour',
                   aggregate.total_runs,
                   aggregate.succeeded_runs,
                   aggregate.failed_runs,
                   aggregate.running_runs,
                   CASE
                       WHEN aggregate.succeeded_runs + aggregate.failed_runs = 0 THEN NULL
                       ELSE aggregate.succeeded_runs::double precision
                           / (aggregate.succeeded_runs + aggregate.failed_runs)
                   END,
                   aggregate.candidate_count,
                   aggregate.canonical_count,
                   CASE
                       WHEN aggregate.candidate_count = 0 THEN NULL
                       ELSE aggregate.canonical_count::double precision
                           / aggregate.candidate_count
                   END,
                   aggregate.average_duration_ms,
                   aggregate.p95_duration_ms,
                   aggregate.latest_success_at,
                   aggregate.latest_failure_at
            FROM observed
            CROSS JOIN aggregate;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_source_history(
            p_source_key text,
            p_window_hours integer,
            p_bucket_hours integer,
            p_include_fixtures boolean
        )
        RETURNS TABLE (
            bucket_start timestamptz,
            total_runs bigint,
            succeeded_runs bigint,
            failed_runs bigint,
            candidate_count bigint,
            canonical_count bigint,
            average_duration_ms bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_window_hours IS NULL OR p_window_hours < 1 OR p_window_hours > 2160
               OR p_bucket_hours IS NULL OR p_bucket_hours < 1 OR p_bucket_hours > 168
               OR p_window_hours % p_bucket_hours <> 0
               OR p_window_hours / p_bucket_hours > 120
               OR p_include_fixtures IS NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin source history query is invalid';
            END IF;
            RETURN QUERY
            WITH observed AS (
                SELECT pg_catalog.statement_timestamp() AS generated_at,
                       pg_catalog.statement_timestamp()
                           - p_window_hours * INTERVAL '1 hour' AS window_starts_at,
                       p_bucket_hours * INTERVAL '1 hour' AS bucket_interval
            ), buckets AS (
                SELECT generate_series(
                    observed.window_starts_at,
                    observed.generated_at - observed.bucket_interval,
                    observed.bucket_interval
                ) AS bucket_start
                FROM observed
            ), aggregate AS (
                SELECT date_bin(
                           observed.bucket_interval,
                           run.started_at,
                           observed.window_starts_at
                       ) AS bucket_start,
                       count(*) AS total_runs,
                       count(*) FILTER (WHERE run.status = 'succeeded') AS succeeded_runs,
                       count(*) FILTER (WHERE run.status = 'failed') AS failed_runs,
                       COALESCE(sum(run.candidate_count), 0) AS candidate_count,
                       COALESCE(sum(run.canonical_count), 0) AS canonical_count,
                       round(avg(run.duration_ms))::bigint AS average_duration_ms
                FROM public.fn_ingestion_admin_run_facts_v2(
                    p_include_fixtures,
                    p_source_key,
                    pg_catalog.statement_timestamp()
                        - p_window_hours * INTERVAL '1 hour'
                ) AS run
                CROSS JOIN observed
                WHERE run.source_key = p_source_key
                  AND run.started_at >= observed.window_starts_at
                  AND run.started_at <= observed.generated_at
                GROUP BY 1
            )
            SELECT buckets.bucket_start,
                   COALESCE(aggregate.total_runs, 0),
                   COALESCE(aggregate.succeeded_runs, 0),
                   COALESCE(aggregate.failed_runs, 0),
                   COALESCE(aggregate.candidate_count, 0),
                   COALESCE(aggregate.canonical_count, 0),
                   aggregate.average_duration_ms
            FROM buckets
            LEFT JOIN aggregate USING (bucket_start)
            ORDER BY buckets.bucket_start;
        END;
        $$
        """
    )


def _create_command_provenance_capabilities() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_commands_v2(p_limit integer)
        RETURNS TABLE (
            command_id uuid,
            action text,
            source_key text,
            status text,
            requested_at timestamptz,
            started_at timestamptz,
            completed_at timestamptz,
            result jsonb,
            error_code text,
            source_revision integer,
            release_revision text,
            image_digest text,
            executor_source_revision integer,
            executor_release_revision text,
            executor_image_digest text
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin command limit is invalid';
            END IF;
            RETURN QUERY
            SELECT command.command_id,
                   command.action,
                   command.source_key,
                   command.status,
                   command.requested_at,
                   command.started_at,
                   command.completed_at,
                   command.result,
                   command.error_code,
                   command.source_revision,
                   command.release_revision,
                   command.image_digest,
                   command.executor_source_revision,
                   command.executor_release_revision,
                   command.executor_image_digest
            FROM public.ingestion_admin_commands AS command
            ORDER BY command.requested_at DESC, command.command_id
            LIMIT p_limit;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_admin_command_v2(p_command_id uuid)
        RETURNS TABLE (
            command_id uuid,
            action text,
            source_key text,
            status text,
            requested_at timestamptz,
            started_at timestamptz,
            completed_at timestamptz,
            result jsonb,
            error_code text,
            source_revision integer,
            release_revision text,
            image_digest text,
            executor_source_revision integer,
            executor_release_revision text,
            executor_image_digest text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT command.command_id,
                   command.action,
                   command.source_key,
                   command.status,
                   command.requested_at,
                   command.started_at,
                   command.completed_at,
                   command.result,
                   command.error_code,
                   command.source_revision,
                   command.release_revision,
                   command.image_digest,
                   command.executor_source_revision,
                   command.executor_release_revision,
                   command.executor_image_digest
            FROM public.ingestion_admin_commands AS command
            WHERE p_command_id IS NOT NULL
              AND command.command_id = p_command_id
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_enqueue_ingestion_admin_command_v2(
            p_command_id uuid,
            p_action text,
            p_source_key text,
            p_requested_by text,
            p_release_revision text,
            p_image_digest text
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            outcome text;
        BEGIN
            IF p_release_revision IS NULL
               OR p_release_revision !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
               OR (
                   p_image_digest IS NOT NULL
                   AND p_image_digest !~ '^sha256:[0-9a-f]{64}$'
               )
            THEN
                RETURN 'invalid';
            END IF;

            outcome := public.fn_enqueue_ingestion_admin_command(
                p_command_id, p_action, p_source_key, p_requested_by
            );
            IF outcome = 'enqueued' THEN
                UPDATE public.ingestion_admin_commands AS command
                SET source_revision = CASE
                        WHEN p_action = 'refresh_source' THEN source.source_revision
                        ELSE NULL
                    END,
                    release_revision = p_release_revision,
                    image_digest = p_image_digest
                FROM (SELECT 1) AS singleton
                LEFT JOIN public.catalog_sources AS source
                  ON source.source_key = p_source_key
                WHERE command.command_id = p_command_id;
            END IF;
            RETURN outcome;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_claim_ingestion_admin_commands_v2(
            p_limit integer,
            p_lease_seconds integer,
            p_executor_release_revision text,
            p_executor_image_digest text
        )
        RETURNS TABLE (
            command_id uuid,
            action text,
            source_key text,
            attempt_count integer,
            lease_token uuid
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_lease_seconds IS NULL
               OR p_lease_seconds < 300 OR p_lease_seconds > 21600
               OR p_executor_release_revision IS NULL
               OR p_executor_release_revision
                    !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
               OR (
                   p_executor_image_digest IS NOT NULL
                   AND p_executor_image_digest !~ '^sha256:[0-9a-f]{64}$'
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin command claim input is invalid';
            END IF;

            RETURN QUERY
            WITH candidates AS (
                SELECT command.command_id
                FROM public.ingestion_admin_commands AS command
                WHERE (
                        command.status = 'queued'
                        AND command.available_at <= clock_timestamp()
                    )
                   OR (
                       command.status = 'running'
                       AND command.lease_expires_at <= clock_timestamp()
                   )
                ORDER BY command.requested_at, command.command_id
                FOR UPDATE SKIP LOCKED
                LIMIT p_limit
            )
            UPDATE public.ingestion_admin_commands AS command
            SET status = 'running',
                started_at = COALESCE(command.started_at, clock_timestamp()),
                attempt_count = command.attempt_count + 1,
                lease_token = gen_random_uuid(),
                lease_expires_at =
                    clock_timestamp() + p_lease_seconds * INTERVAL '1 second',
                result = NULL,
                error_code = NULL,
                executor_source_revision = CASE
                    WHEN command.action = 'refresh_source' THEN (
                        SELECT source.source_revision
                        FROM public.catalog_sources AS source
                        WHERE source.source_key = command.source_key
                    )
                    ELSE NULL
                END,
                executor_release_revision = p_executor_release_revision,
                executor_image_digest = p_executor_image_digest
            FROM candidates
            WHERE command.command_id = candidates.command_id
            RETURNING command.command_id,
                      command.action,
                      command.source_key,
                      command.attempt_count,
                      command.lease_token;
        END;
        $$
        """
    )


def _grant_capabilities() -> None:
    private_functions = (f"public.fn_ingestion_admin_run_facts_v2{_RUN_FACTS}",)
    granted_functions = (
        f"public.fn_list_ingestion_admin_sources_v2{_SOURCE_LIST}",
        f"public.fn_count_ingestion_admin_sources_v2{_SOURCE_COUNT}",
        f"public.fn_list_ingestion_admin_filter_values{_FILTERS}",
        f"public.fn_list_ingestion_admin_runs_v2{_RUN_LIST}",
        f"public.fn_count_ingestion_admin_runs_v2{_RUN_COUNT}",
        f"public.fn_get_ingestion_admin_source_detail{_SOURCE_DETAIL}",
        f"public.fn_get_ingestion_admin_source_summary{_SOURCE_SUMMARY}",
        f"public.fn_list_ingestion_admin_source_history{_SOURCE_HISTORY}",
        f"public.fn_list_ingestion_admin_commands_v2{_COMMAND_LIST}",
        f"public.fn_get_ingestion_admin_command_v2{_COMMAND_GET}",
        f"public.fn_enqueue_ingestion_admin_command_v2{_ENQUEUE}",
        f"public.fn_claim_ingestion_admin_commands_v2{_CLAIM}",
    )
    for signature in private_functions:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
    for signature in granted_functions:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
    op.execute(
        f"REVOKE ALL ON FUNCTION public.fn_enqueue_ingestion_admin_command"
        f"{_LEGACY_ENQUEUE} FROM ec_app"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION public.fn_claim_ingestion_admin_commands"
        f"{_LEGACY_CLAIM} FROM ec_app"
    )
