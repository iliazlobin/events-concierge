"""Grade every source's health server-side, including the ones that cannot currently raise an alarm.

Revision ID: 0172
Revises: 0171
Create Date: 2026-08-27

The console's attention count is computed in the browser by ``sourceAttentionScore``, whose first
statement is ``if (!source.enabled) return 0``.  A paused or retired source therefore scores zero
no matter how stale the catalog rows it is still serving -- and the write path never deletes, so a
disabled source keeps serving its last known events indefinitely.  Measured when this was written:
twelve paused or retired sources back 1,547 upcoming events that no tile in the console can count.

This function grades the whole registry instead, and returns it unpaginated.  Health is the most
elevated of four independently inspectable components rather than a composite number, so a chip
can always be explained by naming the component that produced it:

* ``run_state``  -- what the newest non-fixture run did.
* ``freshness``  -- age of the last success against this source's own cadence (1.5x warn, 3x late,
  7 days down).  This is clock arithmetic, deliberately independent of what the last run reported:
  aggressive retry is exactly what makes ``last_attempt_at`` look healthy while nothing publishes.
* ``retry_burn`` -- attempts on the newest run.  A healthy slot is claimed once; the two runaway
  slots reached 5,168 and 2,253 while reporting a 98% success rate.
* ``yield``      -- a run that succeeded and published nothing.

Three timestamps are returned rather than one, because collapsing them is what hid a thirteen-day
outage behind minutes-old activity: ``last_attempt_at``, ``last_success_at`` and
``last_catalog_change_at``.

The served-events rollup is a full aggregate over ``catalog_event_observations`` joined to
``canonical_events`` -- measured 199 ms for 35,768 rows, and no index removes that because every
row participates.  The whole projection measures roughly 500 ms.  The index added here is
therefore *not* an optimisation of this function: it supports the per-source freshness lookups the
catalog view will make, and is added now because the table has no index on ``last_seen_at`` at all.
If this projection becomes a landing-view blocker, the fix is a materialised rollup refreshed on
commit, not another index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0172"
down_revision: str | None = "0171"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEALTH = "(boolean)"
_OBSERVATION_INDEX = "ix_catalog_event_observations_source_last_seen"

# Ordered most severe first. `retired` and `paused` are lifecycle states, not failures, so they
# rank below the failure grades but are still returned -- an omitted row is an invisible one.
_HEALTH_TOKENS = (
    "down",
    "never_succeeded",
    "late",
    "warn",
    "paused",
    "retired",
    "healthy",
)

# A slot claimed this many times without succeeding is burning capacity, not retrying.
_RETRY_SEVERE_ATTEMPTS = 20


def upgrade() -> None:
    """Add the unpaginated roster projection and the index its served-events rollup needs."""
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_OBSERVATION_INDEX}
        ON public.catalog_event_observations (source_key, last_seen_at DESC)
        """
    )
    _create_source_health()


def downgrade() -> None:
    """Drop the projection and its index; no other projection reads either."""
    qualified = f"public.fn_list_ingestion_admin_source_health_v1{_HEALTH}"
    op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {qualified}")
    op.execute(f"DROP INDEX IF EXISTS public.{_OBSERVATION_INDEX}")


def _grant(function: str, signature: str) -> None:
    qualified = f"public.{function}{signature}"
    op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {qualified} TO ec_app")


def _create_source_health() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.fn_list_ingestion_admin_source_health_v1(
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
                       -- Clock arithmetic against this source's own cadence, never the last
                       -- run's self-report.
                       CASE
                           WHEN succeeded.ok_at IS NULL THEN 'never'
                           WHEN v_now - succeeded.ok_at > INTERVAL '7 days' THEN 'down'
                           WHEN extract(epoch FROM (v_now - succeeded.ok_at)) / 60.0
                                > 3 * src.refresh_interval_minutes THEN 'late'
                           WHEN extract(epoch FROM (v_now - succeeded.ok_at)) / 60.0
                                > 1.5 * src.refresh_interval_minutes THEN 'warn'
                           ELSE 'ok'
                       END AS freshness_state,
                       CASE
                           WHEN COALESCE(latest.attempt_count, 0)
                                >= {_RETRY_SEVERE_ATTEMPTS} THEN 'severe'
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
                   -- Most elevated component wins. Lifecycle states are reported as themselves
                   -- so a paused source stays visible instead of scoring zero and disappearing.
                   CASE
                       WHEN graded.retired_at IS NOT NULL THEN 'retired'
                       WHEN NOT graded.enabled THEN 'paused'
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
    _grant("fn_list_ingestion_admin_source_health_v1", _HEALTH)
