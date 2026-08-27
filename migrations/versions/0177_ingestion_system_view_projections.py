"""Project the shape of the ingestion system, not only its failures.

Revision ID: 0177
Revises: 0176
Create Date: 2026-08-27

Every projection the admin console has is an exception report: which sources are late, which runs
failed, which slots are burning retries.  Nothing describes the system that works -- how many
sources there are and of what kind, how much moves through the pipe each day, where the catalog
actually comes from.  An operator opening the console on a normal day learns nothing about how
their platform is running.

Three read-only rollups, all measured against the live fleet:

* ``fn_get_ingestion_fleet_shape_v1`` (22 ms) -- sources and served events per adapter mode.  The
  first thing it shows is that the two distributions are inverted: twelve ``bibliocommons_rss``
  feeds are 11% of the fleet and 70% of the catalog, while sixty-four ``luma_calendar_json`` host
  calendars are 61% of the fleet and under 4%.  A hundredfold difference in events per source,
  and both cost the same slot time to poll.
* ``fn_get_ingestion_throughput_v1`` (60 ms) -- gap-filled buckets of runs, records collected and
  records published.  Gap-filling matters: a day with no runs is a fact about the fleet, and it
  must render as an empty bucket rather than vanish from the series.
* ``fn_get_catalog_concentration_v1`` (20 ms) -- per-source share of served events with a running
  cumulative.  Ten sources carry 78% of what people can see.

The cumulative uses an explicit ``ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`` frame.  The
default frame is ``RANGE``, which lumps peer rows together and produces a curve that jumps past
100% on ties -- a real defect in the first draft of this query.

All three read through ``fn_ingestion_admin_source_is_fixture`` or
``fn_ingestion_admin_run_facts_v2`` so the 497 fixture rows in the shared dev database stay out.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0177"
down_revision: str | None = "0176"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHAPE = "()"
_THROUGHPUT = "(integer,integer)"
_CONCENTRATION = "(integer)"

# A window wider than this is not a fleet view, it is a report.
_MAX_WINDOW_HOURS = 2_160
_MAX_CONCENTRATION_ROWS = 200


def upgrade() -> None:
    """Add the three system-shape projections."""
    _create_fleet_shape()
    _create_throughput()
    _create_concentration()


def downgrade() -> None:
    """Drop them; nothing else reads these."""
    for function, signature in (
        ("fn_get_catalog_concentration_v1", _CONCENTRATION),
        ("fn_get_ingestion_throughput_v1", _THROUGHPUT),
        ("fn_get_ingestion_fleet_shape_v1", _SHAPE),
    ):
        qualified = f"public.{function}{signature}"
        op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {qualified}")


def _grant(function: str, signature: str) -> None:
    qualified = f"public.{function}{signature}"
    op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {qualified} TO ec_app")


def _create_fleet_shape() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_fleet_shape_v1()
        RETURNS TABLE (
            mode text,
            sources bigint,
            scheduled bigint,
            paused bigint,
            retired bigint,
            upcoming_events bigint,
            events_per_source numeric,
            pct_of_sources numeric,
            pct_of_events numeric
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_now timestamptz := statement_timestamp();
        BEGIN
            RETURN QUERY
            WITH src AS (
                SELECT source.*
                FROM public.catalog_sources AS source
                WHERE NOT public.fn_ingestion_admin_source_is_fixture(
                    source.source_key, source.publisher, source.seed_url
                )
            ), served AS (
                SELECT observation.source_key AS served_key, count(*) AS upcoming
                FROM public.catalog_event_observations AS observation
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = observation.canonical_event_id
                JOIN src ON src.source_key = observation.source_key
                WHERE event.start_at > v_now
                GROUP BY observation.source_key
            ), rolled AS (
                SELECT src.mode AS adapter_mode,
                       count(*)::bigint AS sources,
                       count(*) FILTER (
                           WHERE src.enabled AND src.retired_at IS NULL
                       )::bigint AS scheduled,
                       count(*) FILTER (
                           WHERE NOT src.enabled AND src.retired_at IS NULL
                       )::bigint AS paused,
                       count(*) FILTER (WHERE src.retired_at IS NOT NULL)::bigint AS retired,
                       COALESCE(sum(served.upcoming), 0)::bigint AS upcoming_events
                FROM src
                LEFT JOIN served ON served.served_key = src.source_key
                GROUP BY src.mode
            )
            SELECT rolled.adapter_mode,
                   rolled.sources,
                   rolled.scheduled,
                   rolled.paused,
                   rolled.retired,
                   rolled.upcoming_events,
                   round(
                       rolled.upcoming_events::numeric / NULLIF(rolled.sources, 0), 1
                   ),
                   round(100.0 * rolled.sources / NULLIF(sum(rolled.sources) OVER (), 0), 1),
                   round(
                       100.0 * rolled.upcoming_events
                       / NULLIF(sum(rolled.upcoming_events) OVER (), 0), 1
                   )
            FROM rolled
            ORDER BY rolled.upcoming_events DESC, rolled.adapter_mode;
        END;
        $$
        """
    )
    _grant("fn_get_ingestion_fleet_shape_v1", _SHAPE)


def _create_throughput() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.fn_get_ingestion_throughput_v1(
            p_window_hours integer,
            p_bucket_hours integer
        )
        RETURNS TABLE (
            bucket_start timestamptz,
            runs bigint,
            succeeded bigint,
            failed bigint,
            deferred bigint,
            collected bigint,
            published bigint,
            yield_pct numeric,
            median_duration_ms bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_now timestamptz := statement_timestamp();
            v_start timestamptz;
            v_step interval;
        BEGIN
            IF p_window_hours IS NULL OR p_window_hours < 1
               OR p_window_hours > {_MAX_WINDOW_HOURS} THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion throughput window is invalid';
            END IF;
            IF p_bucket_hours IS NULL OR p_bucket_hours < 1
               OR p_bucket_hours > p_window_hours THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion throughput bucket is invalid';
            END IF;

            v_step := make_interval(hours => p_bucket_hours);
            -- Align to the bucket grid so a series is stable between refreshes.
            v_start := to_timestamp(
                floor(extract(epoch FROM (v_now - make_interval(hours => p_window_hours)))
                      / extract(epoch FROM v_step)) * extract(epoch FROM v_step)
            );

            RETURN QUERY
            WITH grid AS (
                SELECT generate_series(v_start, v_now, v_step) AS bucket
            ), facts AS (
                SELECT to_timestamp(
                           floor(extract(epoch FROM run_facts.started_at)
                                 / extract(epoch FROM v_step)) * extract(epoch FROM v_step)
                       ) AS bucket,
                       run_facts.status,
                       run_facts.candidate_count,
                       run_facts.canonical_count,
                       run_facts.duration_ms
                FROM public.fn_ingestion_admin_run_facts_v2(false, NULL, v_start) AS run_facts
            )
            -- LEFT JOIN against the grid: a bucket with no runs is a fact about the fleet and
            -- must render as an empty bucket, never be absent from the series.
            SELECT grid.bucket,
                   count(facts.*)::bigint,
                   count(facts.*) FILTER (WHERE facts.status = 'succeeded')::bigint,
                   count(facts.*) FILTER (WHERE facts.status = 'failed')::bigint,
                   count(facts.*) FILTER (WHERE facts.status = 'paused')::bigint,
                   COALESCE(sum(facts.candidate_count), 0)::bigint,
                   COALESCE(sum(facts.canonical_count), 0)::bigint,
                   round(
                       100.0 * COALESCE(sum(facts.canonical_count), 0)
                       / NULLIF(sum(facts.candidate_count), 0), 1
                   ),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY facts.duration_ms)
                       FILTER (WHERE facts.status = 'succeeded')::bigint
            FROM grid
            LEFT JOIN facts ON facts.bucket = grid.bucket
            GROUP BY grid.bucket
            ORDER BY grid.bucket;
        END;
        $$
        """
    )
    _grant("fn_get_ingestion_throughput_v1", _THROUGHPUT)


def _create_concentration() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.fn_get_catalog_concentration_v1(
            p_limit integer
        )
        RETURNS TABLE (
            rank integer,
            source_key text,
            display_name text,
            mode text,
            publisher text,
            upcoming_events bigint,
            pct numeric,
            cumulative_pct numeric,
            total_events bigint,
            total_sources bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_now timestamptz := statement_timestamp();
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > {_MAX_CONCENTRATION_ROWS} THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog concentration limit is invalid';
            END IF;

            RETURN QUERY
            WITH served AS (
                SELECT observation.source_key AS served_key,
                       source.display_name AS name,
                       source.mode AS adapter_mode,
                       source.publisher AS source_publisher,
                       count(*)::bigint AS upcoming
                FROM public.catalog_event_observations AS observation
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = observation.canonical_event_id
                JOIN public.catalog_sources AS source
                  ON source.source_key = observation.source_key
                WHERE event.start_at > v_now
                  AND NOT public.fn_ingestion_admin_source_is_fixture(
                      source.source_key, source.publisher, source.seed_url
                  )
                GROUP BY 1, 2, 3, 4
            ), totals AS (
                SELECT COALESCE(sum(upcoming), 0)::bigint AS all_events,
                       count(*)::bigint AS all_sources
                FROM served
            )
            SELECT (row_number() OVER (ORDER BY served.upcoming DESC, served.served_key))::int,
                   served.served_key,
                   served.name,
                   served.adapter_mode,
                   served.source_publisher,
                   served.upcoming,
                   round(
                       100.0 * served.upcoming
                       / NULLIF((SELECT all_events FROM totals), 0), 1
                   ),
                   -- An explicit ROWS frame. The default RANGE frame lumps peer rows together
                   -- and produces a cumulative curve that overshoots on ties.
                   round(
                       100.0 * sum(served.upcoming) OVER (
                           ORDER BY served.upcoming DESC, served.served_key
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) / NULLIF((SELECT all_events FROM totals), 0), 1
                   ),
                   (SELECT all_events FROM totals),
                   (SELECT all_sources FROM totals)
            FROM served
            ORDER BY served.upcoming DESC, served.served_key
            LIMIT p_limit;
        END;
        $$
        """
    )
    _grant("fn_get_catalog_concentration_v1", _CONCENTRATION)
