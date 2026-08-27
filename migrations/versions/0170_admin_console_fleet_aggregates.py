"""Compute the operator console's headline numbers in SQL instead of in the browser.

Revision ID: 0170
Revises: 0169
Create Date: 2026-08-27

Every chart in the admin console is currently derived by paging the raw run ledger into the browser
100 rows at a time and reducing it in TypeScript.  Each constraint on that path -- page size, the
proxy's 10s timeout, its 2 MiB response cap, the 5,000-row client ceiling -- silently truncates a
number the operator reads as a total, and at a 30-day window the loop simply gives up: the pipeline
view renders five zeros and the sentence "No recorded runs match this pipeline slice" over a window
containing 2,697 real runs.  A stated zero is far worse than a spinner, because it is read as fact.

These three functions move the reduction to where the rows already live.  Measured on the live
fleet: 129 ms, 52 ms and 188 ms respectively -- the whole landing view in under half a second,
against a paging loop that cannot complete at all.

Two contract decisions are load-bearing:

* **The stage summary always returns all five declared stages**, and grades each one.
  ``extract_enrich`` and ``normalize_dedupe`` have never written a row and never will -- they are
  folded into the adapter and commit boundaries respectively -- so they are returned as
  ``not_separately_instrumented`` carrying the note code that names the boundary that owns them.
  A stage the console cannot measure must not be rendered as a stage that measured zero.
* **Every aggregate reads through ``fn_ingestion_admin_run_facts_v2``**, never ``catalog_refresh_runs``
  directly.  That projection owns fixture exclusion, expired-lease handling, error normalization and
  duration derivation; a query that bypasses it silently re-admits 497 fixture sources.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0170"
down_revision: str | None = "0169"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FLEET = "(integer,boolean)"
_STAGE = "(integer,boolean)"
_FRESHNESS = "(timestamp with time zone)"

# The closed stage vocabulary of catalog_refresh_run_stage_metrics' CHECK constraint, in pipeline
# order, with the boundary that absorbs each stage that is not separately timed.
_STAGES = (
    ("admission", 1, None),
    ("collect", 2, None),
    ("extract_enrich", 3, "adapter_boundary_includes_extract_enrich"),
    ("normalize_dedupe", 4, "commit_boundary_includes_normalize_dedupe"),
    ("catalog_publish", 5, None),
)

# Freshness grades an event by the age of its source's last SUCCESSFUL fetch, not by the age of the
# event row: the write path never deletes, so a dead source keeps serving its last known events
# indefinitely and looks identical to a live one from the catalog side.
_FRESHNESS_BUCKETS = (
    ("fresh", 1),
    ("aging", 2),
    ("stale", 3),
    ("dead", 4),
    ("never", 5),
)


def upgrade() -> None:
    """Add the three read-only rollups the console needs to stop aggregating client-side."""
    _create_fleet_summary()
    _create_stage_summary()
    _create_catalog_freshness_summary()


def downgrade() -> None:
    """Drop the rollups; the list/detail projections they read remain untouched."""
    for function, signature in (
        ("fn_get_catalog_freshness_summary_v1", _FRESHNESS),
        ("fn_get_ingestion_admin_stage_summary_v1", _STAGE),
        ("fn_get_ingestion_admin_fleet_summary_v1", _FLEET),
    ):
        qualified = f"public.{function}{signature}"
        op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {qualified}")


def _grant(function: str, signature: str) -> None:
    qualified = f"public.{function}{signature}"
    op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {qualified} TO ec_app")


def _stage_values() -> str:
    rows = []
    for stage, position, note in _STAGES:
        note_sql = "NULL::text" if note is None else f"'{note}'"
        rows.append(f"('{stage}', {position}, {note_sql})")
    return ",\n                       ".join(rows)


def _bucket_values() -> str:
    return ",\n                       ".join(
        f"('{bucket}', {position})" for bucket, position in _FRESHNESS_BUCKETS
    )


def _create_fleet_summary() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_admin_fleet_summary_v1(
            p_window_hours integer,
            p_include_fixtures boolean
        )
        RETURNS TABLE (
            generated_at timestamptz,
            window_start timestamptz,
            window_hours integer,
            runs bigint,
            succeeded bigint,
            failed bigint,
            running bigint,
            paused bigint,
            sources_run bigint,
            sources_failed bigint,
            attempts bigint,
            max_attempts integer,
            retrying_runs bigint,
            candidates bigint,
            canonicals bigint,
            zero_yield_runs bigint,
            wall_ms bigint,
            duration_p50_ms bigint,
            duration_p95_ms bigint,
            duration_p99_ms bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_now timestamptz := statement_timestamp();
            v_start timestamptz;
        BEGIN
            IF p_window_hours IS NULL OR p_window_hours < 1 OR p_window_hours > 2160 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin fleet window is invalid';
            END IF;
            v_start := v_now - make_interval(hours => p_window_hours);

            RETURN QUERY
            WITH facts AS (
                SELECT *
                FROM public.fn_ingestion_admin_run_facts_v2(
                    COALESCE(p_include_fixtures, false), NULL, v_start
                )
            )
            SELECT v_now,
                   v_start,
                   p_window_hours,
                   count(*),
                   count(*) FILTER (WHERE facts.status = 'succeeded'),
                   count(*) FILTER (WHERE facts.status = 'failed'),
                   count(*) FILTER (WHERE facts.status = 'running'),
                   count(*) FILTER (WHERE facts.status = 'paused'),
                   count(DISTINCT facts.source_key),
                   count(DISTINCT facts.source_key) FILTER (WHERE facts.status = 'failed'),
                   COALESCE(sum(facts.attempt_count), 0),
                   COALESCE(max(facts.attempt_count), 0),
                   -- A slot retried more than once is the signal the console has never shown; a
                   -- healthy slot is claimed exactly once.
                   count(*) FILTER (WHERE facts.attempt_count > 1),
                   COALESCE(sum(facts.candidate_count), 0),
                   COALESCE(sum(facts.canonical_count), 0),
                   count(*) FILTER (
                       WHERE facts.status = 'succeeded'
                         AND COALESCE(facts.canonical_count, 0) = 0
                   ),
                   COALESCE(sum(facts.duration_ms), 0)::bigint,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY facts.duration_ms)
                       FILTER (WHERE facts.status = 'succeeded')::bigint,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY facts.duration_ms)
                       FILTER (WHERE facts.status = 'succeeded')::bigint,
                   percentile_cont(0.99) WITHIN GROUP (ORDER BY facts.duration_ms)
                       FILTER (WHERE facts.status = 'succeeded')::bigint
            FROM facts;
        END;
        $$
        """
    )
    _grant("fn_get_ingestion_admin_fleet_summary_v1", _FLEET)


def _create_stage_summary() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.fn_get_ingestion_admin_stage_summary_v1(
            p_window_hours integer,
            p_include_fixtures boolean
        )
        RETURNS TABLE (
            stage text,
            stage_position integer,
            evidence_status text,
            folded_into text,
            runs_with_evidence bigint,
            observations bigint,
            total_ms bigint,
            avg_ms bigint,
            p95_ms bigint,
            failed_count bigint,
            pct_of_wall numeric
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_start timestamptz;
        BEGIN
            IF p_window_hours IS NULL OR p_window_hours < 1 OR p_window_hours > 2160 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin stage window is invalid';
            END IF;
            v_start := statement_timestamp() - make_interval(hours => p_window_hours);

            RETURN QUERY
            WITH declared(stage_name, position, folded_note) AS (
                VALUES {_stage_values()}
            ), facts AS (
                SELECT run_facts.source_key, run_facts.run_key
                FROM public.fn_ingestion_admin_run_facts_v2(
                    COALESCE(p_include_fixtures, false), NULL, v_start
                ) AS run_facts
            ), measured AS (
                SELECT metric.stage AS stage_name,
                       count(*) AS runs_with_evidence,
                       sum(metric.observation_count) AS observations,
                       sum(metric.duration_ms) AS total_ms,
                       avg(metric.duration_ms) AS avg_ms,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY metric.duration_ms) AS p95_ms,
                       count(*) FILTER (WHERE metric.last_outcome_code = 'failed') AS failed_count
                FROM public.catalog_refresh_run_stage_metrics AS metric
                JOIN facts
                  ON facts.source_key = metric.source_key
                 AND facts.run_key = metric.run_key
                GROUP BY metric.stage
            )
            SELECT declared.stage_name,
                   declared.position,
                   -- A stage folded into another boundary is not a stage that measured zero.
                   CASE
                       WHEN declared.folded_note IS NOT NULL THEN 'not_separately_instrumented'
                       WHEN measured.stage_name IS NULL THEN 'not_observed'
                       ELSE 'measured'
                   END,
                   declared.folded_note,
                   COALESCE(measured.runs_with_evidence, 0),
                   COALESCE(measured.observations, 0)::bigint,
                   COALESCE(measured.total_ms, 0)::bigint,
                   round(measured.avg_ms)::bigint,
                   round(measured.p95_ms)::bigint,
                   COALESCE(measured.failed_count, 0),
                   round(
                       100.0 * COALESCE(measured.total_ms, 0)
                       / NULLIF(sum(COALESCE(measured.total_ms, 0)) OVER (), 0),
                       1
                   )
            FROM declared
            LEFT JOIN measured ON measured.stage_name = declared.stage_name
            ORDER BY declared.position;
        END;
        $$
        """
    )
    _grant("fn_get_ingestion_admin_stage_summary_v1", _STAGE)


def _create_catalog_freshness_summary() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.fn_get_catalog_freshness_summary_v1(
            p_now timestamptz
        )
        RETURNS TABLE (
            bucket text,
            bucket_position integer,
            sources bigint,
            events bigint,
            pct numeric
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_now timestamptz := COALESCE(p_now, statement_timestamp());
        BEGIN
            RETURN QUERY
            WITH buckets(bucket_name, position) AS (
                VALUES {_bucket_values()}
            ), last_success AS (
                SELECT refresh.source_key,
                       max(refresh.completed_at) AS succeeded_at
                FROM public.catalog_refresh_runs AS refresh
                WHERE refresh.status = 'succeeded'
                  AND refresh.completed_at IS NOT NULL
                GROUP BY refresh.source_key
            ), served AS (
                -- What is actually being served: upcoming events, from non-fixture sources.
                SELECT observation.source_key,
                       count(*) AS event_count
                FROM public.catalog_event_observations AS observation
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = observation.canonical_event_id
                JOIN public.catalog_sources AS source
                  ON source.source_key = observation.source_key
                WHERE event.start_at > v_now
                  AND NOT public.fn_ingestion_admin_source_is_fixture(
                      source.source_key, source.publisher, source.seed_url
                  )
                GROUP BY observation.source_key
            ), graded AS (
                SELECT served.source_key,
                       served.event_count,
                       CASE
                           WHEN last_success.succeeded_at IS NULL THEN 'never'
                           WHEN v_now - last_success.succeeded_at < INTERVAL '24 hours' THEN 'fresh'
                           WHEN v_now - last_success.succeeded_at < INTERVAL '3 days' THEN 'aging'
                           WHEN v_now - last_success.succeeded_at < INTERVAL '7 days' THEN 'stale'
                           ELSE 'dead'
                       END AS bucket_name
                FROM served
                LEFT JOIN last_success ON last_success.source_key = served.source_key
            )
            SELECT buckets.bucket_name,
                   buckets.position,
                   count(graded.source_key),
                   COALESCE(sum(graded.event_count), 0)::bigint,
                   round(
                       100.0 * COALESCE(sum(graded.event_count), 0)
                       / NULLIF(sum(COALESCE(sum(graded.event_count), 0)) OVER (), 0),
                       1
                   )
            FROM buckets
            LEFT JOIN graded ON graded.bucket_name = buckets.bucket_name
            GROUP BY buckets.bucket_name, buckets.position
            ORDER BY buckets.position;
        END;
        $$
        """
    )
    _grant("fn_get_catalog_freshness_summary_v1", _FRESHNESS)
