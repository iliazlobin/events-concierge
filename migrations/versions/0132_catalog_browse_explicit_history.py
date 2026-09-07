"""Honor explicit historical windows in current-catalog browse.

Revision ID: 0132
Revises: 0131
Create Date: 2026-07-31

An omitted window remains a live-catalog request and excludes elapsed events.  Supplying both
bounds makes that bounded interval authoritative, including when it is wholly or partly in the
past.  The additive v5 capability keeps rolling v4 callers available while preserving the same
filter-before-keyset, latest-success, fixture, and least-privilege contracts.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0132"
down_revision: str | None = "0131"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BROWSE_V5 = (
    "public.fn_browse_filtered_current_catalog_events_v5"
    "(text,timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "timestamp with time zone,uuid,integer)"
)
_PROVIDERS_V2 = (
    "public.fn_list_current_catalog_providers_v2"
    "(text,timestamp with time zone,timestamp with time zone)"
)


def upgrade() -> None:
    """Install the additive explicit-history browse capability."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v5(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
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
            price_min_cents integer,
            price_max_cents integer,
            price_currency text,
            event_status text,
            normalizer_version integer,
            merge_version integer,
            organizer_name text,
            host_names text[],
            speaker_names text[],
            partner_names text[],
            attendance_count integer,
            registration_status text,
            entity_profiles jsonb,
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
               OR p_query ~ '[\x00-\x1f\x7f]'
               OR cardinality(coalesce(p_cities, '{}'::text[])) > 20
               OR EXISTS (
                   SELECT 1
                   FROM unnest(coalesce(p_cities, '{}'::text[])) AS selected_city(value)
                   WHERE selected_city.value IS NULL
                      OR length(selected_city.value) > 160
                      OR selected_city.value ~ '[\x00-\x1f\x7f]'
               )
               OR EXISTS (
                   SELECT 1
                   FROM unnest(coalesce(p_location_scopes, '{}'::text[]))
                        AS selected_scope(value)
                   WHERE selected_scope.value IS NULL
                      OR selected_scope.value NOT IN (
                          'bay_area', 'manhattan', 'los_angeles_area'
                      )
               )
               OR (
                   p_price IS NOT NULL
                   AND p_price NOT IN ('free', 'paid', 'unknown')
               )
               OR (
                   p_price_max_cents IS NOT NULL
                   AND (
                       p_price_max_cents NOT BETWEEN 1 AND 100000000
                       OR p_price = 'unknown'
                   )
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog browse query is invalid';
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
                WHERE event.start_at >= coalesce(p_window_start, statement_timestamp())
                  AND (p_window_end IS NULL OR event.start_at < p_window_end)
                  AND event.event_status <> 'cancelled'
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
                                  event.organizer_name,
                                  array_to_string(event.host_names, ' '),
                                  array_to_string(event.speaker_names, ' '),
                                  array_to_string(event.partner_names, ' '),
                                  observation.source_label,
                                  observation.publisher,
                                  observation.provider_key
                              )
                          )
                      ) > 0
                  )
                  AND (
                      (
                          cardinality(coalesce(p_cities, '{}'::text[])) = 0
                          AND cardinality(coalesce(p_location_scopes, '{}'::text[])) = 0
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM unnest(coalesce(p_cities, '{}'::text[]))
                               AS selected_city(value)
                          WHERE event.city_norm = regexp_replace(
                              lower(btrim(selected_city.value)), '[^a-z0-9]+', '', 'g'
                          )
                      )
                      OR (
                          'bay_area' = ANY(coalesce(p_location_scopes, '{}'::text[]))
                          AND event.city_norm = ANY(ARRAY(
                              SELECT regexp_replace(value, '[^a-z0-9]+', '', 'g')
                              FROM unnest(ARRAY[
                                  'alameda', 'albany', 'american canyon', 'antioch', 'atherton',
                                  'belmont', 'belvedere', 'benicia', 'berkeley', 'brentwood',
                                  'brisbane', 'burlingame', 'calistoga', 'campbell', 'clayton',
                                  'colma', 'concord', 'corte madera', 'cotati', 'cupertino',
                                  'daly city', 'danville', 'dixon', 'dublin', 'east palo alto',
                                  'el cerrito', 'emeryville', 'fairfax', 'fairfield', 'foster city',
                                  'fremont', 'gilroy', 'half moon bay', 'hayward', 'healdsburg',
                                  'hercules', 'hillsborough', 'lafayette', 'larkspur', 'livermore',
                                  'los altos', 'los gatos', 'martinez', 'menlo park', 'mill valley',
                                  'millbrae', 'milpitas', 'moraga', 'mountain view', 'napa',
                                  'novato', 'oakland', 'oakley', 'orinda', 'pacifica', 'palo alto',
                                  'petaluma', 'piedmont', 'pinole', 'pittsburg', 'pleasant hill',
                                  'pleasanton', 'redwood city', 'richmond', 'rio vista',
                                  'rohnert park', 'ross', 'san anselmo', 'san bruno', 'san carlos',
                                  'san francisco', 'san jose', 'san leandro', 'san mateo',
                                  'san pablo', 'san rafael', 'san ramon', 'santa clara',
                                  'santa rosa', 'sausalito', 'sebastopol', 'sonoma',
                                  'south san francisco', 'st helena', 'suisun city', 'sunnyvale',
                                  'tiburon', 'union city', 'vacaville', 'vallejo', 'walnut creek',
                                  'windsor', 'woodside'
                              ]::text[]) AS bay_area_city(value)
                          ))
                      )
                      OR (
                          'manhattan' = ANY(coalesce(p_location_scopes, '{}'::text[]))
                          AND (
                              (
                                  event.lat BETWEEN 40.6829 AND 40.8790
                                  AND event.lon BETWEEN -74.0479 AND -73.9067
                              )
                              OR (
                                  (event.lat IS NULL OR event.lon IS NULL)
                                  AND event.city_norm = 'manhattan'
                              )
                          )
                      )
                      OR (
                          'los_angeles_area' = ANY(
                              coalesce(p_location_scopes, '{}'::text[])
                          )
                          AND (
                              (
                                  event.lat BETWEEN 33.7000 AND 34.3400
                                  AND event.lon BETWEEN -118.7500 AND -117.9000
                              )
                              OR event.city_norm = ANY(ARRAY(
                                  SELECT regexp_replace(value, '[^a-z0-9]+', '', 'g')
                                  FROM unnest(ARRAY[
                                      'alhambra', 'arcadia', 'beverly hills', 'burbank',
                                      'culver city', 'el segundo', 'glendale', 'hawthorne',
                                      'inglewood', 'long beach', 'los angeles', 'malibu',
                                      'manhattan beach', 'pasadena', 'redondo beach',
                                      'santa monica', 'torrance', 'west hollywood'
                                  ]::text[]) AS los_angeles_city(value)
                              ))
                          )
                      )
                  )
                  AND (p_price IS NULL OR event.price_status = p_price)
                  AND (
                      p_price_max_cents IS NULL
                      OR (
                          event.price_status = 'free'
                          OR (
                              event.price_status = 'paid'
                              AND event.price_currency = 'USD'
                              AND event.price_max_cents IS NOT NULL
                              AND event.price_max_cents <= p_price_max_cents
                          )
                      )
                  )
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
                   event.price_min_cents,
                   event.price_max_cents,
                   event.price_currency,
                   event.event_status,
                   event.normalizer_version,
                   event.merge_version,
                   event.organizer_name,
                   event.host_names,
                   event.speaker_names,
                   event.partner_names,
                   event.attendance_count,
                   event.registration_status,
                   event.entity_profiles,
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
    _create_providers_v2()
    for signature in (_BROWSE_V5, _PROVIDERS_V2):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove only the additive v5 browse capability."""
    for signature in (_PROVIDERS_V2, _BROWSE_V5):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")


def _create_providers_v2() -> None:
    """Keep provider facets and counts on the same explicit-or-live time contract."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_current_catalog_providers_v2(
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
            event_count bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF (
                   p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
               OR ((p_window_start IS NULL) <> (p_window_end IS NULL))
               OR (
                   p_window_start IS NOT NULL
                   AND (
                       p_window_end <= p_window_start
                       OR p_window_end - p_window_start > interval '370 days'
                   )
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog provider query is invalid';
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
                SELECT observation.source_key,
                       observation.canonical_event_id
                FROM public.catalog_event_observations AS observation
                JOIN latest_success AS success
                  ON success.source_key = observation.source_key
                 AND success.run_key = observation.last_run_key
            )
            SELECT source.source_key,
                   source.display_name,
                   source.publisher,
                   source.provider_key,
                   source.seed_url,
                   count(DISTINCT event.canonical_event_id)
            FROM source_facts AS source
            JOIN current_observations AS observation
              ON observation.source_key = source.source_key
            JOIN public.canonical_events AS event
              ON event.canonical_event_id = observation.canonical_event_id
            WHERE event.start_at >= coalesce(p_window_start, statement_timestamp())
              AND (p_window_end IS NULL OR event.start_at < p_window_end)
              AND event.event_status <> 'cancelled'
            GROUP BY source.source_key,
                     source.display_name,
                     source.publisher,
                     source.provider_key,
                     source.seed_url
            HAVING count(DISTINCT event.canonical_event_id) > 0
            ORDER BY lower(source.display_name), source.display_name, source.source_key;
        END;
        $$
        """
    )
