"""Filter current catalog events before chronological pagination.

Revision ID: 0116
Revises: 0115
Create Date: 2026-07-27

The consumer UI previously loaded one bounded chronological page and filtered it in the browser.
That made valid week and month results disappear whenever earlier catalog rows filled the page.
This capability keeps the latest-observation and fixture boundaries from 0115, while applying the
user's date, text, city, and price predicates before the keyset page is selected.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0116"
down_revision: str | None = "0115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FILTERED_BROWSE_SIGNATURE = (
    "(text,timestamp with time zone,timestamp with time zone,text,text,text,"
    "timestamp with time zone,uuid,integer)"
)


def upgrade() -> None:
    """Install a bounded filtered chronological browse capability."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_browse_filtered_current_catalog_events(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_city text,
            p_price text,
            p_after_start timestamptz,
            p_after_id uuid,
            p_limit integer
        )
        RETURNS TABLE (
            canonical_event_id uuid,
            title text,
            start_at timestamptz,
            end_at timestamptz,
            venue_name text,
            lat double precision,
            lon double precision,
            city_norm text,
            description text,
            price_status text,
            event_status text,
            normalizer_version integer,
            merge_version integer,
            source_key text,
            source_label text,
            publisher text,
            provider text,
            seed_url text,
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
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 101
               OR (
                   p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
               OR ((p_after_start IS NULL) <> (p_after_id IS NULL))
               OR ((p_window_start IS NULL) <> (p_window_end IS NULL))
               OR (
                   p_window_start IS NOT NULL
                   AND (
                       p_window_end <= p_window_start
                       OR p_window_end - p_window_start > interval '370 days'
                   )
               )
               OR length(coalesce(p_query, '')) > 160
               OR length(coalesce(p_city, '')) > 160
               OR p_query ~ '[\x00-\x1f\x7f]'
               OR p_city ~ '[\x00-\x1f\x7f]'
               OR (
                   p_price IS NOT NULL
                   AND p_price NOT IN ('free', 'paid', 'unknown')
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'filtered catalog browse query is invalid';
            END IF;

            RETURN QUERY
            WITH source_facts AS MATERIALIZED (
                SELECT source.*,
                       CASE
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?luma\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'luma'
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?meetup\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'meetup'
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?eventbrite\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'eventbrite'
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?ticketmaster\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'ticketmaster'
                           WHEN lower(source.seed_url)
                                ~ '^https://([^/]+\.)?partiful\.com(?::[0-9]+)?(?:/|$)'
                               THEN 'partiful'
                           ELSE lower(
                               substring(source.seed_url from '^https://([^/:]+)')
                           )
                       END AS provider_key
                FROM public.catalog_sources AS source
                WHERE source.enabled
                  AND source.handoff_only
                  AND source.reviewed_at IS NOT NULL
                  AND (
                      source.review_expires_at IS NULL
                      OR source.review_expires_at > statement_timestamp()
                  )
                  AND NOT public.fn_ingestion_admin_source_is_fixture(
                      source.source_key, source.publisher, source.seed_url
                  )
                  AND (p_source_key IS NULL OR source.source_key = p_source_key)
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
            ), current_observations AS MATERIALIZED (
                SELECT observation.*,
                       source.display_name AS source_label,
                       source.publisher,
                       source.provider_key,
                       source.seed_url
                FROM public.catalog_event_observations AS observation
                JOIN latest_success AS success
                  ON success.source_key = observation.source_key
                 AND success.run_key = observation.last_run_key
                JOIN source_facts AS source
                  ON source.source_key = observation.source_key
            ), eligible_events AS MATERIALIZED (
                SELECT event.canonical_event_id,
                       event.start_at
                FROM public.canonical_events AS event
                JOIN current_observations AS observation
                  ON observation.canonical_event_id = event.canonical_event_id
                WHERE event.start_at >= statement_timestamp()
                  AND event.event_status <> 'cancelled'
                  AND (
                      p_window_start IS NULL
                      OR (
                          event.start_at >= p_window_start
                          AND event.start_at < p_window_end
                      )
                  )
                  AND (
                      nullif(btrim(p_query), '') IS NULL
                      OR position(
                          lower(btrim(p_query))
                          IN lower(
                              concat_ws(
                                  ' ',
                                  event.title,
                                  event.description,
                                  event.venue_name,
                                  event.city_norm,
                                  observation.source_label,
                                  observation.publisher,
                                  observation.provider_key
                              )
                          )
                      ) > 0
                  )
                  AND (
                      nullif(btrim(p_city), '') IS NULL
                      OR event.city_norm = regexp_replace(
                          lower(btrim(p_city)), '[^a-z0-9]+', '', 'g'
                      )
                  )
                  AND (p_price IS NULL OR event.price_status = p_price)
                  AND (
                      p_after_start IS NULL
                      OR event.start_at > p_after_start
                      OR (
                          event.start_at = p_after_start
                          AND event.canonical_event_id > p_after_id
                      )
                  )
                GROUP BY event.canonical_event_id, event.start_at
            ), page AS MATERIALIZED (
                SELECT eligible.canonical_event_id,
                       eligible.start_at
                FROM eligible_events AS eligible
                ORDER BY eligible.start_at, eligible.canonical_event_id
                LIMIT p_limit
            )
            SELECT event.canonical_event_id,
                   event.title,
                   event.start_at,
                   event.end_at,
                   event.venue_name,
                   event.lat,
                   event.lon,
                   event.city_norm,
                   event.description,
                   event.price_status,
                   event.event_status,
                   event.normalizer_version,
                   event.merge_version,
                   observation.source_key,
                   observation.source_label,
                   observation.publisher,
                   observation.provider_key,
                   observation.seed_url,
                   observation.source,
                   observation.source_event_id,
                   observation.registration_url,
                   observation.last_seen_at,
                   observation.last_run_key
            FROM page
            JOIN public.canonical_events AS event
              ON event.canonical_event_id = page.canonical_event_id
            JOIN current_observations AS observation
              ON observation.canonical_event_id = event.canonical_event_id
            ORDER BY page.start_at,
                     page.canonical_event_id,
                     observation.source_key,
                     observation.source,
                     observation.source_event_id;
        END;
        $$
        """
    )
    signature = f"public.fn_browse_filtered_current_catalog_events{_FILTERED_BROWSE_SIGNATURE}"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove only the filtered browse capability."""
    op.execute(
        "DROP FUNCTION IF EXISTS public.fn_browse_filtered_current_catalog_events"
        f"{_FILTERED_BROWSE_SIGNATURE}"
    )
