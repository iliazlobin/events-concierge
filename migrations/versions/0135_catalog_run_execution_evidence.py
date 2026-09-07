"""Persist bounded, operator-safe execution evidence for catalog refresh runs.

Revision ID: 0135
Revises: 0134
Create Date: 2026-07-31

The refresh ledger already records durable status, counts, normalized errors, and claim identity.
It intentionally does not contain process resource samples or stage timings.  This migration adds
two run-keyed aggregate tables behind fixed capabilities.  The worker can record only bounded
numeric measurements and closed stage/outcome vocabularies; raw provider data, arbitrary log text,
credentials, stack traces, and lease tokens have no column or function parameter.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0135"
down_revision: str | None = "0134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECORD_EXECUTION = "(text,text,bigint,bigint,bigint,bigint,bigint,bigint,text,text,text,text)"
_RECORD_STAGE = "(text,text,text,bigint,text)"
_RUN_LIST_V4 = "(text,text,text,text,text,integer,boolean,integer,integer)"


def upgrade() -> None:
    """Install bounded telemetry storage, recorders, and the richer run projection."""
    op.execute(
        """
        CREATE TABLE public.catalog_refresh_run_execution_metrics (
            source_key text NOT NULL,
            run_key text NOT NULL,
            execution_count integer NOT NULL DEFAULT 0,
            wall_time_ms bigint NOT NULL DEFAULT 0,
            process_cpu_time_ms bigint,
            rss_first_bytes bigint,
            rss_last_bytes bigint,
            boundary_observed_peak_rss_bytes bigint,
            process_lifetime_peak_rss_bytes bigint,
            measurement_source text NOT NULL,
            measurement_scope text NOT NULL,
            measurement_quality text NOT NULL,
            first_observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            last_observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            last_outcome_code text NOT NULL,
            PRIMARY KEY (source_key, run_key),
            FOREIGN KEY (source_key, run_key)
                REFERENCES public.catalog_refresh_runs(source_key, run_key)
                ON DELETE CASCADE,
            CHECK (execution_count BETWEEN 1 AND 10000),
            CHECK (wall_time_ms BETWEEN 0 AND 2592000000),
            CHECK (
                process_cpu_time_ms IS NULL
                OR process_cpu_time_ms BETWEEN 0 AND 2592000000
            ),
            CHECK (rss_first_bytes IS NULL OR rss_first_bytes BETWEEN 0 AND 1125899906842624),
            CHECK (rss_last_bytes IS NULL OR rss_last_bytes BETWEEN 0 AND 1125899906842624),
            CHECK (
                boundary_observed_peak_rss_bytes IS NULL
                OR boundary_observed_peak_rss_bytes BETWEEN 0 AND 1125899906842624
            ),
            CHECK (
                process_lifetime_peak_rss_bytes IS NULL
                OR process_lifetime_peak_rss_bytes BETWEEN 0 AND 1125899906842624
            ),
            CHECK (
                measurement_source IN (
                    'python_monotonic',
                    'python_monotonic+process_time',
                    'python_monotonic+process_time+linux_procfs',
                    'python_monotonic+process_time+getrusage',
                    'python_monotonic+process_time+linux_procfs+getrusage'
                )
            ),
            CHECK (
                measurement_scope IN (
                    'activity_wall_clock_only',
                    'worker_process_boundary_samples'
                )
            ),
            CHECK (
                measurement_quality IN (
                    'process_metrics_omitted_shared_worker',
                    'best_effort_process_delta_sequential_worker'
                )
            ),
            CHECK (
                last_outcome_code IN (
                    'succeeded', 'queued', 'skipped', 'busy', 'already_succeeded',
                    'deferred', 'progressed', 'failed'
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.catalog_refresh_run_stage_metrics (
            source_key text NOT NULL,
            run_key text NOT NULL,
            stage text NOT NULL,
            observation_count integer NOT NULL DEFAULT 0,
            duration_ms bigint NOT NULL DEFAULT 0,
            last_outcome_code text NOT NULL,
            first_observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            last_observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (source_key, run_key, stage),
            FOREIGN KEY (source_key, run_key)
                REFERENCES public.catalog_refresh_runs(source_key, run_key)
                ON DELETE CASCADE,
            CHECK (
                stage IN (
                    'admission', 'collect', 'extract_enrich',
                    'normalize_dedupe', 'catalog_publish'
                )
            ),
            CHECK (observation_count BETWEEN 1 AND 10000),
            CHECK (duration_ms BETWEEN 0 AND 2592000000),
            CHECK (
                last_outcome_code IN (
                    'succeeded', 'queued', 'skipped', 'busy', 'already_succeeded',
                    'deferred', 'progressed', 'failed'
                )
            )
        )
        """
    )
    for table in (
        "catalog_refresh_run_execution_metrics",
        "catalog_refresh_run_stage_metrics",
    ):
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM ec_app")

    op.execute(
        """
        CREATE FUNCTION public.fn_record_catalog_refresh_run_execution_v1(
            p_source_key text,
            p_run_key text,
            p_wall_time_ms bigint,
            p_process_cpu_time_ms bigint,
            p_rss_before_bytes bigint,
            p_rss_after_bytes bigint,
            p_boundary_observed_peak_rss_bytes bigint,
            p_process_lifetime_peak_rss_bytes bigint,
            p_measurement_source text,
            p_measurement_scope text,
            p_measurement_quality text,
            p_outcome_code text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_wall_time_ms IS NULL OR p_wall_time_ms NOT BETWEEN 0 AND 21600000
               OR (p_process_cpu_time_ms IS NOT NULL
                   AND p_process_cpu_time_ms NOT BETWEEN 0 AND 21600000)
               OR (p_rss_before_bytes IS NOT NULL
                   AND p_rss_before_bytes NOT BETWEEN 0 AND 1125899906842624)
               OR (p_rss_after_bytes IS NOT NULL
                   AND p_rss_after_bytes NOT BETWEEN 0 AND 1125899906842624)
               OR (p_boundary_observed_peak_rss_bytes IS NOT NULL
                   AND p_boundary_observed_peak_rss_bytes NOT BETWEEN 0 AND 1125899906842624)
               OR (p_process_lifetime_peak_rss_bytes IS NOT NULL
                   AND p_process_lifetime_peak_rss_bytes NOT BETWEEN 0 AND 1125899906842624)
               OR p_measurement_source NOT IN (
                    'python_monotonic',
                    'python_monotonic+process_time',
                    'python_monotonic+process_time+linux_procfs',
                    'python_monotonic+process_time+getrusage',
                    'python_monotonic+process_time+linux_procfs+getrusage'
               )
               OR p_measurement_scope NOT IN (
                    'activity_wall_clock_only',
                    'worker_process_boundary_samples'
               )
               OR p_measurement_quality NOT IN (
                    'process_metrics_omitted_shared_worker',
                    'best_effort_process_delta_sequential_worker'
               )
               OR (
                   p_measurement_scope = 'activity_wall_clock_only'
                   AND (
                       p_process_cpu_time_ms IS NOT NULL
                       OR p_rss_before_bytes IS NOT NULL
                       OR p_rss_after_bytes IS NOT NULL
                       OR p_boundary_observed_peak_rss_bytes IS NOT NULL
                       OR p_process_lifetime_peak_rss_bytes IS NOT NULL
                       OR p_measurement_quality <> 'process_metrics_omitted_shared_worker'
                   )
               )
               OR (
                   p_measurement_scope = 'worker_process_boundary_samples'
                   AND (
                       p_process_cpu_time_ms IS NULL
                       OR p_measurement_quality
                           <> 'best_effort_process_delta_sequential_worker'
                   )
               )
               OR p_outcome_code NOT IN (
                    'succeeded', 'queued', 'skipped', 'busy', 'already_succeeded',
                    'deferred', 'progressed', 'failed'
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog refresh execution evidence is invalid';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM public.catalog_refresh_runs AS refresh
                WHERE refresh.source_key = p_source_key
                  AND refresh.run_key = p_run_key
            ) THEN
                RETURN false;
            END IF;

            INSERT INTO public.catalog_refresh_run_execution_metrics AS metric (
                source_key, run_key, execution_count, wall_time_ms, process_cpu_time_ms,
                rss_first_bytes, rss_last_bytes, boundary_observed_peak_rss_bytes,
                process_lifetime_peak_rss_bytes, measurement_source, measurement_scope,
                measurement_quality, last_outcome_code
            ) VALUES (
                p_source_key, p_run_key, 1, p_wall_time_ms, p_process_cpu_time_ms,
                p_rss_before_bytes, p_rss_after_bytes, p_boundary_observed_peak_rss_bytes,
                p_process_lifetime_peak_rss_bytes, p_measurement_source, p_measurement_scope,
                p_measurement_quality, p_outcome_code
            )
            ON CONFLICT (source_key, run_key) DO UPDATE
            SET execution_count = LEAST(metric.execution_count + 1, 10000),
                wall_time_ms = LEAST(metric.wall_time_ms + EXCLUDED.wall_time_ms, 2592000000),
                process_cpu_time_ms = CASE
                    WHEN metric.process_cpu_time_ms IS NULL
                        THEN EXCLUDED.process_cpu_time_ms
                    WHEN EXCLUDED.process_cpu_time_ms IS NULL
                        THEN metric.process_cpu_time_ms
                    ELSE LEAST(
                        metric.process_cpu_time_ms + EXCLUDED.process_cpu_time_ms,
                        2592000000
                    )
                END,
                rss_last_bytes = EXCLUDED.rss_last_bytes,
                boundary_observed_peak_rss_bytes = CASE
                    WHEN metric.boundary_observed_peak_rss_bytes IS NULL
                        THEN EXCLUDED.boundary_observed_peak_rss_bytes
                    WHEN EXCLUDED.boundary_observed_peak_rss_bytes IS NULL
                        THEN metric.boundary_observed_peak_rss_bytes
                    ELSE GREATEST(
                        metric.boundary_observed_peak_rss_bytes,
                        EXCLUDED.boundary_observed_peak_rss_bytes
                    )
                END,
                process_lifetime_peak_rss_bytes = CASE
                    WHEN metric.process_lifetime_peak_rss_bytes IS NULL
                        THEN EXCLUDED.process_lifetime_peak_rss_bytes
                    WHEN EXCLUDED.process_lifetime_peak_rss_bytes IS NULL
                        THEN metric.process_lifetime_peak_rss_bytes
                    ELSE GREATEST(
                        metric.process_lifetime_peak_rss_bytes,
                        EXCLUDED.process_lifetime_peak_rss_bytes
                    )
                END,
                measurement_source = EXCLUDED.measurement_source,
                measurement_scope = EXCLUDED.measurement_scope,
                measurement_quality = EXCLUDED.measurement_quality,
                last_observed_at = clock_timestamp(),
                last_outcome_code = EXCLUDED.last_outcome_code;
            RETURN true;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_record_catalog_refresh_run_stage_v1(
            p_source_key text,
            p_run_key text,
            p_stage text,
            p_duration_ms bigint,
            p_outcome_code text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
               OR p_stage NOT IN (
                    'admission', 'collect', 'extract_enrich',
                    'normalize_dedupe', 'catalog_publish'
               )
               OR p_duration_ms IS NULL OR p_duration_ms NOT BETWEEN 0 AND 21600000
               OR p_outcome_code NOT IN (
                    'succeeded', 'queued', 'skipped', 'busy', 'already_succeeded',
                    'deferred', 'progressed', 'failed'
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog refresh stage evidence is invalid';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM public.catalog_refresh_runs AS refresh
                WHERE refresh.source_key = p_source_key
                  AND refresh.run_key = p_run_key
            ) THEN
                RETURN false;
            END IF;

            INSERT INTO public.catalog_refresh_run_stage_metrics AS metric (
                source_key, run_key, stage, observation_count, duration_ms, last_outcome_code
            ) VALUES (
                p_source_key, p_run_key, p_stage, 1, p_duration_ms, p_outcome_code
            )
            ON CONFLICT (source_key, run_key, stage) DO UPDATE
            SET observation_count = LEAST(metric.observation_count + 1, 10000),
                duration_ms = LEAST(metric.duration_ms + EXCLUDED.duration_ms, 2592000000),
                last_outcome_code = EXCLUDED.last_outcome_code,
                last_observed_at = clock_timestamp();
            RETURN true;
        END;
        $$
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_runs_v4(
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
            is_latest_for_source boolean,
            resolved_by_newer_success boolean,
            mode text,
            reviewed_at timestamptz,
            review_expires_at timestamptz,
            refresh_interval_minutes integer,
            min_interval_ms integer,
            page_limit integer,
            command_id uuid,
            command_action text,
            command_requested_at timestamptz,
            command_started_at timestamptz,
            command_completed_at timestamptz,
            execution_count integer,
            execution_wall_time_ms bigint,
            process_cpu_time_ms bigint,
            rss_before_bytes bigint,
            rss_after_bytes bigint,
            boundary_observed_peak_rss_bytes bigint,
            process_lifetime_peak_rss_bytes bigint,
            measurement_source text,
            measurement_scope text,
            measurement_quality text,
            execution_last_outcome_code text,
            execution_first_observed_at timestamptz,
            execution_last_observed_at timestamptz,
            stage_metrics jsonb,
            total_count bigint
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT listed.source_key,
                   listed.display_name,
                   listed.run_key,
                   listed.status,
                   listed.started_at,
                   listed.completed_at,
                   listed.candidate_count,
                   listed.canonical_count,
                   listed.error,
                   listed.attempt_count,
                   listed.duration_ms,
                   listed.source_revision,
                   listed.release_revision,
                   listed.image_digest,
                   listed.provenance_status,
                   listed.trigger,
                   listed.is_latest_for_source,
                   listed.resolved_by_newer_success,
                   source.mode,
                   source.reviewed_at,
                   source.review_expires_at,
                   source.refresh_interval_minutes,
                   source.min_interval_ms,
                   source.page_limit,
                   command.command_id,
                   command.action,
                   command.requested_at,
                   command.started_at,
                   command.completed_at,
                   metric.execution_count,
                   metric.wall_time_ms,
                   metric.process_cpu_time_ms,
                   metric.rss_first_bytes,
                   metric.rss_last_bytes,
                   metric.boundary_observed_peak_rss_bytes,
                   metric.process_lifetime_peak_rss_bytes,
                   metric.measurement_source,
                   metric.measurement_scope,
                   metric.measurement_quality,
                   metric.last_outcome_code,
                   metric.first_observed_at,
                   metric.last_observed_at,
                   COALESCE(
                       (
                           SELECT jsonb_agg(
                               jsonb_build_object(
                                   'stage', stage.stage,
                                   'observation_count', stage.observation_count,
                                   'duration_ms', stage.duration_ms,
                                   'last_outcome_code', stage.last_outcome_code,
                                   'first_observed_at', stage.first_observed_at,
                                   'last_observed_at', stage.last_observed_at
                               ) ORDER BY CASE stage.stage
                                   WHEN 'admission' THEN 1
                                   WHEN 'collect' THEN 2
                                   WHEN 'extract_enrich' THEN 3
                                   WHEN 'normalize_dedupe' THEN 4
                                   WHEN 'catalog_publish' THEN 5
                                   ELSE 6
                               END
                           )
                           FROM public.catalog_refresh_run_stage_metrics AS stage
                           WHERE stage.source_key = listed.source_key
                             AND stage.run_key = listed.run_key
                       ),
                       '[]'::jsonb
                   ),
                   listed.total_count
            FROM public.fn_list_ingestion_admin_runs_v3(
                p_status, p_source_key, p_mode, p_publisher, p_region,
                p_window_hours, p_include_fixtures, p_limit, p_offset
            ) AS listed
            -- v3 can return fixture-backed rows when explicitly requested.  A fixture does not
            -- have reviewed crawler configuration, but it must not disappear from the richer
            -- projection merely because its execution descriptor is unavailable.
            LEFT JOIN public.catalog_sources AS source
              ON source.source_key = listed.source_key
            LEFT JOIN public.ingestion_admin_commands AS command
              ON command.action = 'refresh_source'
             AND command.source_key = listed.source_key
             AND listed.run_key = 'admin:' || command.command_id::text
            LEFT JOIN public.catalog_refresh_run_execution_metrics AS metric
              ON metric.source_key = listed.source_key
             AND metric.run_key = listed.run_key
            ORDER BY listed.started_at DESC, listed.source_key, listed.run_key DESC
        $$
        """
    )

    for signature in (
        f"public.fn_record_catalog_refresh_run_execution_v1{_RECORD_EXECUTION}",
        f"public.fn_record_catalog_refresh_run_stage_v1{_RECORD_STAGE}",
        f"public.fn_list_ingestion_admin_runs_v4{_RUN_LIST_V4}",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove run execution evidence while retaining the v3 run projection."""
    for signature in (
        f"public.fn_list_ingestion_admin_runs_v4{_RUN_LIST_V4}",
        f"public.fn_record_catalog_refresh_run_stage_v1{_RECORD_STAGE}",
        f"public.fn_record_catalog_refresh_run_execution_v1{_RECORD_EXECUTION}",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP TABLE IF EXISTS public.catalog_refresh_run_stage_metrics")
    op.execute("DROP TABLE IF EXISTS public.catalog_refresh_run_execution_metrics")
