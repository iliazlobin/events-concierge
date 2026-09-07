"""Expose per-source admitted-catalog shape so coverage loss is measurable, not anecdotal.

Revision ID: 0166
Revises: 0165
Create Date: 2026-08-26

Every completeness guard the ingestion path has is a *truncation* detector: the page-cap raise, the
cursor checks, the lease fence.  None of them can see the failure that actually loses events --
walking the wrong seed to completion.  A curated city Discover feed returning one event per host is
a perfectly complete walk and raises nothing, while the host's own calendar carries eighty-six
more.  That defect ran undetected until it was noticed by hand on an entity page.

This function measures shape instead.  Its headline column is depth -- live future events per
distinct organizer named across them.  A discovery shelf scores about 1.0 by construction because
it is a ranked sample across many hosts; a source carrying a host's real programme scores its
programme.  Measured when this was written, ``luma-sf`` scored 1.08 and ``luma-thecommons`` 17.4.

``live_future_events`` deliberately counts only observations whose ``last_run_key`` matches the
newest successful run, because that is exactly the rule
``fn_list_retained_catalog_browse_observations_v1`` applies: a future observation absent from the
newest successful fetch is not live.  ``retracted_future_events`` counts the remainder, which is
otherwise invisible -- the write path never deletes, so nothing else reports it.

Fixture sources are excluded on the same predicate the cadence projections use, so the report
describes the reviewed fleet rather than the test suite's leftovers.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0166"
down_revision: str | None = "0165"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPORT = "public.fn_report_catalog_source_coverage_v1()"


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.fn_report_catalog_source_coverage_v1()
        RETURNS TABLE (
            source_key text,
            mode text,
            latest_run_status text,
            latest_run_error text,
            live_future_events bigint,
            retracted_future_events bigint,
            distinct_organizers bigint,
            hours_since_success double precision,
            refresh_interval_minutes integer
        )
        LANGUAGE sql
        STABLE SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'public'
        AS $$
            WITH scoped AS (
                SELECT source.source_key, source.mode, source.refresh_interval_minutes
                FROM public.catalog_sources AS source
                WHERE source.enabled
                  AND source.retired_at IS NULL
                  AND NOT public.fn_ingestion_admin_source_is_fixture(
                          source.source_key, source.publisher, source.seed_url
                      )
            ), latest AS (
                SELECT DISTINCT ON (run.source_key)
                       run.source_key, run.status, run.error
                FROM public.catalog_refresh_runs AS run
                WHERE run.status IN ('succeeded', 'failed')
                ORDER BY run.source_key, run.started_at DESC
            ), newest_success AS (
                SELECT DISTINCT ON (run.source_key)
                       run.source_key, run.run_key, run.completed_at
                FROM public.catalog_refresh_runs AS run
                WHERE run.status = 'succeeded' AND run.completed_at IS NOT NULL
                ORDER BY run.source_key, run.completed_at DESC
            )
            SELECT scoped.source_key,
                   scoped.mode,
                   latest.status,
                   latest.error,
                   count(*) FILTER (
                       WHERE event.start_at > now()
                         AND observation.last_run_key = newest_success.run_key
                   ) AS live_future_events,
                   count(*) FILTER (
                       WHERE event.start_at > now()
                         AND observation.last_run_key <> newest_success.run_key
                   ) AS retracted_future_events,
                   count(DISTINCT event.organizer_name) FILTER (
                       WHERE event.start_at > now()
                         AND observation.last_run_key = newest_success.run_key
                   ) AS distinct_organizers,
                   extract(
                       epoch FROM now() - newest_success.completed_at
                   ) / 3600.0 AS hours_since_success,
                   scoped.refresh_interval_minutes
            FROM scoped
            LEFT JOIN latest ON latest.source_key = scoped.source_key
            LEFT JOIN newest_success ON newest_success.source_key = scoped.source_key
            LEFT JOIN public.catalog_event_observations AS observation
                   ON observation.source_key = scoped.source_key
            LEFT JOIN public.canonical_events AS event
                   ON event.canonical_event_id = observation.canonical_event_id
            GROUP BY scoped.source_key, scoped.mode, latest.status, latest.error,
                     newest_success.completed_at, scoped.refresh_interval_minutes
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_REPORT} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REPORT} TO ec_app")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION {_REPORT} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {_REPORT}")
