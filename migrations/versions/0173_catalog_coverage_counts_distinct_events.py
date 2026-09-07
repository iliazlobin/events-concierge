"""Count events, not observation rows, in the source-coverage report.

Revision ID: 0173
Revises: 0172
Create Date: 2026-08-27

``fn_report_catalog_source_coverage_v1`` (0166) counted ``count(*)`` over a join to
``catalog_event_observations``, whose primary key is ``(source_key, source, source_event_id)``.  One
source routinely observes a single canonical event under more than one source identity, so the
report counted observation *rows*: measured on the live catalog, ``san-jose-public-library-events``
reported 3,986 for 3,883 real events and ``contra-costa-county-library-events`` 1,444 for 1,373.
The distortion is small in absolute terms and fatal in kind -- ``depth`` divides two numbers that
were counting different things, which is the one thing this report exists to get right.

Two further corrections, both found by reading the report against the projection it claims to
mirror:

* ``retracted_future_events`` compared ``observation.last_run_key <> newest_success.run_key``.  For
  a source with no succeeded-and-completed run at all, ``newest_success.run_key`` is NULL, so both
  that comparison and its complement evaluate to NULL and *neither* branch counts anything -- a
  source that has never once succeeded reported zero retracted events rather than all of them.  It
  is now ``IS DISTINCT FROM``.
* The window is now the retention rule's own: ``coalesce(end_at, start_at) > now()`` rather than
  ``start_at > now()`` (an event that has begun and not ended is still live), and cancelled events
  are excluded, matching ``fn_list_retained_catalog_browse_observations_v1``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0173"
down_revision: str | None = "0172"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPORT = "public.fn_report_catalog_source_coverage_v1()"

_BODY = """
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
            ), live AS (
                -- One row per (source, canonical event): the observation table can hold several
                -- source identities for one event, and this report is about events.
                SELECT scoped.source_key,
                       observation.canonical_event_id,
                       bool_or(
                           observation.last_run_key IS NOT DISTINCT FROM newest_success.run_key
                       ) AS on_newest_success,
                       max(event.organizer_name) AS organizer_name
                FROM scoped
                JOIN public.catalog_event_observations AS observation
                  ON observation.source_key = scoped.source_key
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = observation.canonical_event_id
                LEFT JOIN newest_success ON newest_success.source_key = scoped.source_key
                WHERE coalesce(event.end_at, event.start_at) > now()
                  AND event.event_status <> 'cancelled'
                GROUP BY scoped.source_key, observation.canonical_event_id
            )
            SELECT scoped.source_key,
                   scoped.mode,
                   latest.status,
                   latest.error,
                   count(live.canonical_event_id) FILTER (
                       WHERE live.on_newest_success
                   ) AS live_future_events,
                   count(live.canonical_event_id) FILTER (
                       WHERE NOT live.on_newest_success
                   ) AS retracted_future_events,
                   count(DISTINCT live.organizer_name) FILTER (
                       WHERE live.on_newest_success
                   ) AS distinct_organizers,
                   extract(
                       epoch FROM now() - newest_success.completed_at
                   ) / 3600.0 AS hours_since_success,
                   scoped.refresh_interval_minutes
            FROM scoped
            LEFT JOIN latest ON latest.source_key = scoped.source_key
            LEFT JOIN newest_success ON newest_success.source_key = scoped.source_key
            LEFT JOIN live ON live.source_key = scoped.source_key
            GROUP BY scoped.source_key, scoped.mode, latest.status, latest.error,
                     newest_success.completed_at, scoped.refresh_interval_minutes
        $$
"""

_PREVIOUS_BODY = """
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


def _install(body: str) -> None:
    op.execute(body)
    op.execute(f"REVOKE ALL ON FUNCTION {_REPORT} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REPORT} TO ec_app")


def upgrade() -> None:
    _install(_BODY)


def downgrade() -> None:
    _install(_PREVIOUS_BODY)
