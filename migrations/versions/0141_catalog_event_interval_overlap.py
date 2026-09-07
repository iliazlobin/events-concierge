"""Browse events whose intervals overlap the requested catalog window.

Revision ID: 0141
Revises: 0140
Create Date: 2026-08-02

Catalog windows previously compared only ``start_at`` with the lower bound.  That made an event
disappear as soon as it started, even while its recorded ``end_at`` was still in the future.  The
observation boundary now uses half-open interval overlap: an event is eligible when its effective
end is after the window start and its start is before the window end.  Events without an end remain
point events by falling back to ``start_at``.

The retained-history lane follows the same effective end.  An ongoing event must still be present
in the source's latest successful run; only events that have fully ended may use a retained older
observation.  Browse ordering and cursors remain keyed by ``(start_at, canonical_event_id)`` in the
unchanged v6/v7 paging functions.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0141"
down_revision: str | None = "0140"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSERVATIONS_V1 = (
    "public.fn_list_retained_catalog_browse_observations_v1"
    "(text,timestamp with time zone,timestamp with time zone)"
)


def upgrade() -> None:
    """Use event-interval overlap while retaining the latest-success live boundary."""
    _replace_retained_observations(interval_overlap=True)
    op.execute(f"REVOKE ALL ON FUNCTION {_OBSERVATIONS_V1} FROM PUBLIC, ec_app")


def downgrade() -> None:
    """Restore the 0134 start-time-only browse contract."""
    _replace_retained_observations(interval_overlap=False)
    op.execute(f"REVOKE ALL ON FUNCTION {_OBSERVATIONS_V1} FROM PUBLIC, ec_app")


def _replace_retained_observations(*, interval_overlap: bool) -> None:
    """Replace only observation eligibility; downstream keyset paging is unchanged."""
    if interval_overlap:
        window_predicate = """
              AND coalesce(event.end_at, event.start_at)
                    > coalesce(p_window_start, statement_timestamp())
              AND (p_window_end IS NULL OR event.start_at < p_window_end)"""
        retained_predicate = """
                      p_window_start IS NOT NULL
                      AND coalesce(event.end_at, event.start_at) <= statement_timestamp()"""
    else:
        window_predicate = """
              AND event.start_at >= coalesce(p_window_start, statement_timestamp())
              AND (p_window_end IS NULL OR event.start_at < p_window_end)"""
        retained_predicate = """
                      p_window_start IS NOT NULL
                      AND event.start_at < statement_timestamp()"""

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_list_retained_catalog_browse_observations_v1(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz
        )
        RETURNS TABLE (
            source_key text,
            source_label text,
            publisher text,
            provider text,
            seed_url text,
            canonical_event_id uuid,
            observation_source text,
            source_event_id text,
            registration_url text,
            last_seen_at timestamptz,
            refresh_run_key text
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF ((p_window_start IS NULL) <> (p_window_end IS NULL))
               OR (
                   p_window_start IS NOT NULL
                   AND (
                       p_window_end <= p_window_start
                       OR (
                           p_source_key IS NULL
                           AND p_window_end - p_window_start > interval '370 days'
                       )
                       OR (
                           p_source_key IS NOT NULL
                           AND p_window_end - p_window_start > interval '7305 days'
                       )
                   )
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog observation window is invalid';
            END IF;

            RETURN QUERY
            WITH source_facts AS MATERIALIZED (
                SELECT *
                FROM public.fn_list_admitted_catalog_browse_sources_v1(p_source_key)
            ), latest_success AS MATERIALIZED (
                SELECT DISTINCT ON (refresh.source_key)
                       refresh.source_key,
                       refresh.run_key
                FROM public.catalog_refresh_runs AS refresh
                JOIN source_facts AS source
                  ON source.source_key = refresh.source_key
                WHERE refresh.status = 'succeeded'
                  AND refresh.completed_at IS NOT NULL
                  AND NOT public.fn_ingestion_admin_run_is_fixture(
                      refresh.run_key, refresh.error
                  )
                ORDER BY refresh.source_key, refresh.completed_at DESC, refresh.run_key DESC
            )
            SELECT source.source_key,
                   source.source_label,
                   source.publisher,
                   source.provider,
                   source.seed_url,
                   observation.canonical_event_id,
                   observation.source,
                   observation.source_event_id,
                   observation.registration_url,
                   observation.last_seen_at,
                   observation.last_run_key
            FROM public.catalog_event_observations AS observation
            JOIN source_facts AS source
              ON source.source_key = observation.source_key
            JOIN public.catalog_refresh_runs AS observed_run
              ON observed_run.source_key = observation.source_key
             AND observed_run.run_key = observation.last_run_key
            JOIN public.canonical_events AS event
              ON event.canonical_event_id = observation.canonical_event_id
            LEFT JOIN latest_success AS success
              ON success.source_key = observation.source_key
            WHERE observed_run.status = 'succeeded'
              AND observed_run.completed_at IS NOT NULL
              AND NOT public.fn_ingestion_admin_run_is_fixture(
                  observed_run.run_key, observed_run.error
              )
{window_predicate}
              AND event.event_status <> 'cancelled'
              AND (
                  (
{retained_predicate}
                  )
                  OR success.run_key = observation.last_run_key
              );
        END;
        $$
        """
    )
