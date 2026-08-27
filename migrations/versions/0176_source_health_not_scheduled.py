"""Stop grading freshness for sources that are not on a cadence.

Revision ID: 0176
Revises: 0175
Create Date: 2026-08-27

``freshness_state`` is clock arithmetic against ``refresh_interval_minutes``: how far past its own
cadence a source is since its last success.  That question is only meaningful for a source the
scheduler is actually dispatching.  A paused or retired source is not on a cadence at all, so
measuring it against one produces a number that is arithmetically correct and operationally
meaningless -- "163x behind" for a source deliberately switched off six weeks ago.

The console made the consequence obvious.  Of sixteen sources flagged for attention, twelve
rendered solid red, and *every* red freshness badge belonged to a paused or retired source.  Not
one active source had a freshness problem.  The screen was a wall of red reporting that switched-
off things were not running.

``freshness_state`` now returns ``not_scheduled`` for those sources, and the health rollup no
longer promotes them to ``down`` on that basis.  This does not hide them: a stopped source keeps
serving whatever it last published, because the write path never deletes, and those twelve are
still serving 1,538 upcoming events.  That is a real condition -- it is simply a different one,
belonging to catalog coverage rather than to cadence failure, and it must not borrow the colour
that means "a running source is broken".

A stopped source that is *also* genuinely failing keeps its other components: ``run_state``,
``retry_state`` and ``yield_state`` are unchanged, so the retired source still grinding an
orphaned retry loop at 287 executions continues to grade as severe.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0176"
down_revision: str | None = "0175"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEALTH = "(boolean)"

# A source the cadence scheduler will never pick up has no schedule to be late against.
_UNSCHEDULED_PREDICATE = "src.retired_at IS NOT NULL OR NOT src.enabled"


def upgrade() -> None:
    """Replace the health projection so freshness is only graded where a cadence exists."""
    _create_source_health()


def downgrade() -> None:
    """Restore the prior definition, which graded every source against a cadence."""
    _create_source_health(grade_unscheduled=True)


def _create_source_health(grade_unscheduled: bool = False) -> None:
    # When restoring the old behaviour the unscheduled branch simply never fires.
    unscheduled_branch = (
        ""
        if grade_unscheduled
        else f"""
                           WHEN {_UNSCHEDULED_PREDICATE} THEN 'not_scheduled'"""
    )
    health_unscheduled = (
        ""
        if grade_unscheduled
        else """
                       -- Lifecycle is reported as itself. It never borrows the colour that
                       -- means "a source that should be running is broken".
                       WHEN graded.freshness_state = 'not_scheduled'
                            AND graded.run_state <> 'failed'
                            AND graded.retry_state = 'ok'
                            AND graded.yield_state <> 'zero_yield'
                            THEN CASE
                                WHEN graded.retired_at IS NOT NULL THEN 'retired'
                                ELSE 'paused'
                            END"""
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_list_ingestion_admin_source_health_v1(
            p_include_fixtures boolean
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            publisher text,
            mode text,
            region text,
            enabled boolean,
            retired_at timestamptz,
            refresh_interval_minutes integer,
            page_limit integer,
            health text,
            run_state text,
            freshness_state text,
            retry_state text,
            yield_state text,
            last_attempt_at timestamptz,
            last_success_at timestamptz,
            last_catalog_change_at timestamptz,
            latest_run_status text,
            latest_run_error text,
            latest_attempt_count integer,
            upcoming_events bigint,
            hours_since_success double precision
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
                WHERE COALESCE(p_include_fixtures, false)
                   OR NOT public.fn_ingestion_admin_source_is_fixture(
                          source.source_key, source.publisher, source.seed_url
                      )
            ), latest AS (
                SELECT DISTINCT ON (refresh.source_key)
                       refresh.source_key AS attempt_source_key,
                       refresh.status AS attempt_status,
                       refresh.started_at AS attempt_started_at,
                       refresh.attempt_count AS attempt_count,
                       refresh.canonical_count AS attempt_canonical_count,
                       public.fn_normalize_catalog_refresh_error(
                           refresh.error
                       ) AS attempt_error_code
                FROM public.catalog_refresh_runs AS refresh
                JOIN src ON src.source_key = refresh.source_key
                WHERE NOT public.fn_ingestion_admin_run_is_fixture(
                    refresh.run_key, refresh.error
                )
                ORDER BY refresh.source_key,
                         refresh.started_at DESC,
                         refresh.run_key DESC
            ), succeeded AS (
                SELECT refresh.source_key AS ok_source_key,
                       max(refresh.completed_at) AS ok_at
                FROM public.catalog_refresh_runs AS refresh
                JOIN src ON src.source_key = refresh.source_key
                WHERE refresh.status = 'succeeded'
                  AND refresh.completed_at IS NOT NULL
                GROUP BY refresh.source_key
            ), served AS (
                SELECT observation.source_key AS served_source_key,
                       count(*) FILTER (WHERE event.start_at > v_now) AS upcoming,
                       max(observation.last_seen_at) AS changed_at
                FROM public.catalog_event_observations AS observation
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = observation.canonical_event_id
                JOIN src ON src.source_key = observation.source_key
                GROUP BY observation.source_key
            ), graded AS (
                SELECT src.*,
                       latest.attempt_status,
                       latest.attempt_started_at,
                       latest.attempt_count,
                       latest.attempt_error_code,
                       latest.attempt_canonical_count,
                       succeeded.ok_at,
                       COALESCE(served.upcoming, 0) AS upcoming,
                       served.changed_at,
                       CASE
                           WHEN latest.attempt_source_key IS NULL THEN 'never_run'
                           WHEN latest.attempt_status = 'failed' THEN 'failed'
                           WHEN latest.attempt_status = 'running' THEN 'running'
                           WHEN latest.attempt_status = 'paused' THEN 'deferred'
                           ELSE 'ok'
                       END AS run_state,
                       CASE{unscheduled_branch}
                           WHEN succeeded.ok_at IS NULL THEN 'never'
                           WHEN v_now - succeeded.ok_at > INTERVAL '7 days' THEN 'down'
                           WHEN extract(epoch FROM (v_now - succeeded.ok_at)) / 60.0
                                > 3 * src.refresh_interval_minutes THEN 'late'
                           WHEN extract(epoch FROM (v_now - succeeded.ok_at)) / 60.0
                                > 1.5 * src.refresh_interval_minutes THEN 'warn'
                           ELSE 'ok'
                       END AS freshness_state,
                       CASE
                           WHEN COALESCE(latest.attempt_count, 0) >= 20 THEN 'severe'
                           WHEN COALESCE(latest.attempt_count, 0) > 1 THEN 'elevated'
                           ELSE 'ok'
                       END AS retry_state,
                       CASE
                           WHEN latest.attempt_status IS NULL THEN 'unknown'
                           WHEN latest.attempt_status = 'succeeded'
                                AND COALESCE(latest.attempt_canonical_count, 0) = 0
                                THEN 'zero_yield'
                           WHEN latest.attempt_status = 'succeeded' THEN 'ok'
                           ELSE 'unknown'
                       END AS yield_state
                FROM src
                LEFT JOIN latest ON latest.attempt_source_key = src.source_key
                LEFT JOIN succeeded ON succeeded.ok_source_key = src.source_key
                LEFT JOIN served ON served.served_source_key = src.source_key
            )
            SELECT graded.source_key,
                   graded.display_name,
                   graded.publisher,
                   graded.mode,
                   graded.region,
                   graded.enabled,
                   graded.retired_at,
                   graded.refresh_interval_minutes,
                   graded.page_limit,
                   CASE
                       WHEN graded.retired_at IS NOT NULL THEN 'retired'
                       WHEN NOT graded.enabled THEN 'paused'{health_unscheduled}
                       WHEN graded.freshness_state = 'never' THEN 'never_succeeded'
                       WHEN graded.freshness_state = 'down'
                            OR graded.retry_state = 'severe' THEN 'down'
                       WHEN graded.freshness_state = 'late' THEN 'late'
                       WHEN graded.freshness_state = 'warn'
                            OR graded.retry_state = 'elevated'
                            OR graded.run_state = 'failed'
                            OR graded.yield_state = 'zero_yield' THEN 'warn'
                       ELSE 'healthy'
                   END,
                   graded.run_state,
                   graded.freshness_state,
                   graded.retry_state,
                   graded.yield_state,
                   graded.attempt_started_at,
                   graded.ok_at,
                   graded.changed_at,
                   graded.attempt_status,
                   graded.attempt_error_code,
                   graded.attempt_count,
                   graded.upcoming,
                   CASE
                       WHEN graded.ok_at IS NULL THEN NULL
                       ELSE (extract(epoch FROM (v_now - graded.ok_at)) / 3600.0)
                            ::double precision
                   END
            FROM graded
            ORDER BY graded.upcoming DESC, graded.source_key;
        END;
        $$
        """
    )
