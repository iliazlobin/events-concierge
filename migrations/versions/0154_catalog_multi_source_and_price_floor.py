"""Let catalog browsing filter by several sources and by a price floor.

Revision ID: 0154
Revises: 0153
Create Date: 2026-08-26

Source selection becomes a set, so a reader can ask for "Luma or Meetup" in one request, and price
gains a lower bound, so "at least", "exactly", and "between" are expressible rather than only a
ceiling. These are new function versions rather than replacements: the entity projection still reads
observations through the single-source v1.

The floor deliberately does not admit free events the way the ceiling does. A ceiling asks "nothing
above this", which every free event satisfies; a floor asks "nothing below this", which no free
event satisfies.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0154"
down_revision: str | None = "0153"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCES_V2 = "public.fn_list_admitted_catalog_browse_sources_v2(text[])"
_OBSERVATIONS_V2 = (
    "public.fn_list_retained_catalog_browse_observations_v2"
    "(text[],timestamp with time zone,timestamp with time zone)"
)
_BROWSE_V9 = (
    "public.fn_browse_filtered_current_catalog_events_v9"
    "(text[],timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "integer,text[],text,timestamp with time zone,uuid,integer)"
)
_DAYS_V2 = (
    "public.fn_list_catalog_day_facets_v2"
    "(text[],timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "integer,text[],text)"
)
_TOPICS_V3 = (
    "public.fn_list_catalog_topic_facets_v3"
    "(text[],timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "integer)"
)


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_admitted_catalog_browse_sources_v2(
            p_source_keys text[]
        )
        RETURNS TABLE (
            source_key text,
            source_label text,
            publisher text,
            provider text,
            seed_url text
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF cardinality(coalesce(p_source_keys, '{}'::text[])) > 40
               OR EXISTS (
                   SELECT 1
                   FROM unnest(coalesce(p_source_keys, '{}'::text[])) AS selected_source(value)
                   WHERE selected_source.value IS NULL
                      OR selected_source.value !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog source query is invalid';
            END IF;

            RETURN QUERY
            SELECT source.source_key,
                   source.display_name,
                   source.publisher,
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
                       ELSE lower(substring(source.seed_url from '^https://([^/:]+)'))
                   END,
                   source.seed_url
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
              AND (
                  cardinality(coalesce(p_source_keys, '{}'::text[])) = 0
                  OR source.source_key = ANY(p_source_keys)
              )
            ORDER BY lower(source.display_name), source.display_name, source.source_key;
        END;
        $$
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_retained_catalog_browse_observations_v2(
            p_source_keys text[],
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
                           cardinality(coalesce(p_source_keys, '{}'::text[])) = 0
                           AND p_window_end - p_window_start > interval '370 days'
                       )
                       OR (
                           cardinality(coalesce(p_source_keys, '{}'::text[])) > 0
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
                FROM public.fn_list_admitted_catalog_browse_sources_v2(p_source_keys)
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

              AND coalesce(event.end_at, event.start_at)
                    > coalesce(p_window_start, statement_timestamp())
              AND (p_window_end IS NULL OR event.start_at < p_window_end)
              AND event.event_status <> 'cancelled'
              AND (
                  (

                      p_window_start IS NOT NULL
                      AND coalesce(event.end_at, event.start_at) <= statement_timestamp()
                  )
                  OR success.run_key = observation.last_run_key
              );
        END;
        $$
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v9(
            p_source_keys text[],
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
            p_price_min_cents integer,
            p_topics text[],
            p_sort text,
            p_after_start timestamptz,
            p_after_id uuid,
            p_limit integer
        )
        RETURNS TABLE (
            canonical_event_id uuid, title text, start_at timestamptz, end_at timestamptz,
            venue_name text, lat double precision, lon double precision, city_norm text,
            description text, price_status text, price_min_cents integer,
            price_max_cents integer, price_currency text, event_status text,
            normalizer_version integer, merge_version integer, organizer_name text,
            host_names text[], speaker_names text[], partner_names text[],
            attendance_count integer, registration_status text, entity_profiles jsonb,
            source_key text, source_label text, publisher text, provider text, seed_url text,
            observation_source text, source_event_id text, registration_url text,
            last_seen_at timestamptz, refresh_run_key text, topics text[],
            extraction_evidence jsonb
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 101
               OR p_sort IS NULL OR p_sort NOT IN ('soonest', 'latest')
               OR (
                   cardinality(coalesce(p_source_keys, '{}'::text[])) > 40
                   OR EXISTS (
                       SELECT 1
                       FROM unnest(coalesce(p_source_keys, '{}'::text[])) AS selected_source(value)
                       WHERE selected_source.value IS NULL
                          OR selected_source.value !~ '^[a-z0-9][a-z0-9-]{1,79}$'
                   )
               )
               OR ((p_after_start IS NULL) <> (p_after_id IS NULL))
               OR ((p_window_start IS NULL) <> (p_window_end IS NULL))
               OR (
                   p_window_start IS NOT NULL
                   AND (
                       p_window_end <= p_window_start
                       OR (
                           cardinality(coalesce(p_source_keys, '{}'::text[])) = 0
                           AND p_window_end - p_window_start > interval '370 days'
                       )
                       OR (
                           cardinality(coalesce(p_source_keys, '{}'::text[])) > 0
                           AND p_window_end - p_window_start > interval '7305 days'
                       )
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
               OR (p_price IS NOT NULL AND p_price NOT IN ('free', 'paid', 'unknown'))
               OR (
                   p_price_max_cents IS NOT NULL
                   AND (
                       p_price_max_cents NOT BETWEEN 1 AND 100000000
                       OR p_price = 'unknown'
                   )
               )
               OR (
                   p_price_min_cents IS NOT NULL
                   AND (
                       p_price_min_cents NOT BETWEEN 1 AND 100000000
                       OR p_price IN ('free', 'unknown')
                       OR (
                           p_price_max_cents IS NOT NULL
                           AND p_price_min_cents > p_price_max_cents
                       )
                   )
               )
               OR cardinality(coalesce(p_topics, '{}'::text[])) > 12
               OR EXISTS (
                   SELECT 1
                   FROM unnest(coalesce(p_topics, '{}'::text[])) AS selected_topic(value)
                   WHERE selected_topic.value IS NULL
                      OR selected_topic.value NOT IN (
                          'ai', 'arts', 'board-games', 'chess', 'community', 'education',
                          'family', 'food-drink', 'founders', 'gaming', 'government', 'music',
                          'networking', 'outdoors', 'sports', 'technology', 'volleyball',
                          'wellness', 'workshop'
                      )
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog browse query is invalid';
            END IF;

            RETURN QUERY
            WITH browse_observations AS MATERIALIZED (
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v2(
                    p_source_keys, p_window_start, p_window_end
                )
            ), eligible_events AS MATERIALIZED (
                SELECT event.canonical_event_id,
                       event.start_at
                FROM public.canonical_events AS event
                JOIN browse_observations AS observation
                  ON observation.canonical_event_id = event.canonical_event_id
                WHERE (
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
                                      observation.provider
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
                      OR event.price_status = 'free'
                      OR (
                          event.price_status = 'paid'
                          AND event.price_currency = 'USD'
                          AND event.price_max_cents IS NOT NULL
                          AND event.price_max_cents <= p_price_max_cents
                      )
                  )
                  AND (
                      p_price_min_cents IS NULL
                      OR (
                          event.price_status = 'paid'
                          AND event.price_currency = 'USD'
                          AND event.price_min_cents IS NOT NULL
                          AND event.price_min_cents >= p_price_min_cents
                      )
                  )
                  AND event.topics @> coalesce(p_topics, '{}'::text[])
                  AND (
                      p_after_start IS NULL
                      OR (
                          p_sort = 'soonest'
                          AND (
                              event.start_at > p_after_start
                              OR (
                                  event.start_at = p_after_start
                                  AND event.canonical_event_id > p_after_id
                              )
                          )
                      )
                      OR (
                          p_sort = 'latest'
                          AND (
                              event.start_at < p_after_start
                              OR (
                                  event.start_at = p_after_start
                                  AND event.canonical_event_id < p_after_id
                              )
                          )
                      )
                  )
                GROUP BY event.canonical_event_id, event.start_at
            ), page AS MATERIALIZED (
                SELECT eligible.canonical_event_id,
                       eligible.start_at
                FROM eligible_events AS eligible
                ORDER BY
                    CASE WHEN p_sort = 'soonest' THEN eligible.start_at END ASC,
                    CASE WHEN p_sort = 'soonest' THEN eligible.canonical_event_id END ASC,
                    CASE WHEN p_sort = 'latest' THEN eligible.start_at END DESC,
                    CASE WHEN p_sort = 'latest' THEN eligible.canonical_event_id END DESC
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
                   observation.provider,
                   observation.seed_url,
                   observation.observation_source,
                   observation.source_event_id,
                   observation.registration_url,
                   observation.last_seen_at,
                   observation.refresh_run_key,
                   event.topics,
                   event.extraction_evidence
            FROM page
            JOIN public.canonical_events AS event
              ON event.canonical_event_id = page.canonical_event_id
            JOIN browse_observations AS observation
              ON observation.canonical_event_id = event.canonical_event_id
            ORDER BY
                CASE WHEN p_sort = 'soonest' THEN page.start_at END ASC,
                CASE WHEN p_sort = 'soonest' THEN page.canonical_event_id END ASC,
                CASE WHEN p_sort = 'latest' THEN page.start_at END DESC,
                CASE WHEN p_sort = 'latest' THEN page.canonical_event_id END DESC,
                observation.source_key,
                observation.observation_source,
                observation.source_event_id;
        END;
        $$
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_catalog_topic_facets_v3(
            p_source_keys text[],
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
            p_price_min_cents integer
        )
        RETURNS TABLE (topic text, event_count bigint)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF
               (
                   cardinality(coalesce(p_source_keys, '{}'::text[])) > 40
                   OR EXISTS (
                       SELECT 1
                       FROM unnest(coalesce(p_source_keys, '{}'::text[])) AS selected_source(value)
                       WHERE selected_source.value IS NULL
                          OR selected_source.value !~ '^[a-z0-9][a-z0-9-]{1,79}$'
                   )
               )
               OR ((p_window_start IS NULL) <> (p_window_end IS NULL))
               OR (
                   p_window_start IS NOT NULL
                   AND (
                       p_window_end <= p_window_start
                       OR (
                           cardinality(coalesce(p_source_keys, '{}'::text[])) = 0
                           AND p_window_end - p_window_start > interval '370 days'
                       )
                       OR (
                           cardinality(coalesce(p_source_keys, '{}'::text[])) > 0
                           AND p_window_end - p_window_start > interval '7305 days'
                       )
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
               OR (p_price IS NOT NULL AND p_price NOT IN ('free', 'paid', 'unknown'))
               OR (
                   p_price_max_cents IS NOT NULL
                   AND (
                       p_price_max_cents NOT BETWEEN 1 AND 100000000
                       OR p_price = 'unknown'
                   )
               )
               OR (
                   p_price_min_cents IS NOT NULL
                   AND (
                       p_price_min_cents NOT BETWEEN 1 AND 100000000
                       OR p_price IN ('free', 'unknown')
                       OR (
                           p_price_max_cents IS NOT NULL
                           AND p_price_min_cents > p_price_max_cents
                       )
                   )
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog browse query is invalid';
            END IF;

            RETURN QUERY
            WITH browse_observations AS MATERIALIZED (
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v2(
                    p_source_keys, p_window_start, p_window_end
                )
            ), eligible_events AS MATERIALIZED (
                SELECT event.canonical_event_id,
                       event.start_at
                FROM public.canonical_events AS event
                JOIN browse_observations AS observation
                  ON observation.canonical_event_id = event.canonical_event_id
                WHERE (
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
                                      observation.provider
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
                      OR event.price_status = 'free'
                      OR (
                          event.price_status = 'paid'
                          AND event.price_currency = 'USD'
                          AND event.price_max_cents IS NOT NULL
                          AND event.price_max_cents <= p_price_max_cents
                      )
                  )
                  AND (
                      p_price_min_cents IS NULL
                      OR (
                          event.price_status = 'paid'
                          AND event.price_currency = 'USD'
                          AND event.price_min_cents IS NOT NULL
                          AND event.price_min_cents >= p_price_min_cents
                      )
                  )
                GROUP BY event.canonical_event_id, event.start_at
            )
            SELECT selected.topic, count(DISTINCT eligible.canonical_event_id)
            FROM eligible_events AS eligible
            JOIN public.canonical_events AS event
              ON event.canonical_event_id = eligible.canonical_event_id
            CROSS JOIN LATERAL unnest(event.topics) AS selected(topic)
            GROUP BY selected.topic;
        END;
        $$
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_catalog_day_facets_v2(
            p_source_keys text[],
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
            p_price_min_cents integer,
            p_topics text[],
            p_time_zone text
        )
        RETURNS TABLE (
            start_day date,
            day_event_count bigint,
            topic text,
            topic_event_count bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF
               (
                   cardinality(coalesce(p_source_keys, '{}'::text[])) > 40
                   OR EXISTS (
                       SELECT 1
                       FROM unnest(coalesce(p_source_keys, '{}'::text[])) AS selected_source(value)
                       WHERE selected_source.value IS NULL
                          OR selected_source.value !~ '^[a-z0-9][a-z0-9-]{1,79}$'
                   )
               )
               OR ((p_window_start IS NULL) <> (p_window_end IS NULL))
               OR (
                   p_window_start IS NOT NULL
                   AND (
                       p_window_end <= p_window_start
                       OR (
                           cardinality(coalesce(p_source_keys, '{}'::text[])) = 0
                           AND p_window_end - p_window_start > interval '370 days'
                       )
                       OR (
                           cardinality(coalesce(p_source_keys, '{}'::text[])) > 0
                           AND p_window_end - p_window_start > interval '7305 days'
                       )
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
               OR (p_price IS NOT NULL AND p_price NOT IN ('free', 'paid', 'unknown'))
               OR (
                   p_price_max_cents IS NOT NULL
                   AND (
                       p_price_max_cents NOT BETWEEN 1 AND 100000000
                       OR p_price = 'unknown'
                   )
               )
               OR (
                   p_price_min_cents IS NOT NULL
                   AND (
                       p_price_min_cents NOT BETWEEN 1 AND 100000000
                       OR p_price IN ('free', 'unknown')
                       OR (
                           p_price_max_cents IS NOT NULL
                           AND p_price_min_cents > p_price_max_cents
                       )
                   )
               )
               OR cardinality(coalesce(p_topics, '{}'::text[])) > 12
               OR EXISTS (
                   SELECT 1
                   FROM unnest(coalesce(p_topics, '{}'::text[])) AS selected_topic(value)
                   WHERE selected_topic.value IS NULL
                      OR selected_topic.value NOT IN (
                          'ai', 'arts', 'board-games', 'chess', 'community', 'education',
                          'family', 'food-drink', 'founders', 'gaming', 'government', 'music',
                          'networking', 'outdoors', 'sports', 'technology', 'volleyball',
                          'wellness', 'workshop'
                      )
               )
               OR p_time_zone IS NULL
               OR p_time_zone !~ '^[A-Za-z0-9+_/-]{1,64}$'
               OR NOT EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_timezone_names AS zone
                   WHERE zone.name = p_time_zone
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog browse query is invalid';
            END IF;

            RETURN QUERY
            WITH browse_observations AS MATERIALIZED (
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v2(
                    p_source_keys, p_window_start, p_window_end
                )
            ), eligible_events AS MATERIALIZED (
                SELECT event.canonical_event_id,
                       event.start_at
                FROM public.canonical_events AS event
                JOIN browse_observations AS observation
                  ON observation.canonical_event_id = event.canonical_event_id
                WHERE (
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
                                      observation.provider
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
                      OR event.price_status = 'free'
                      OR (
                          event.price_status = 'paid'
                          AND event.price_currency = 'USD'
                          AND event.price_max_cents IS NOT NULL
                          AND event.price_max_cents <= p_price_max_cents
                      )
                  )
                  AND (
                      p_price_min_cents IS NULL
                      OR (
                          event.price_status = 'paid'
                          AND event.price_currency = 'USD'
                          AND event.price_min_cents IS NOT NULL
                          AND event.price_min_cents >= p_price_min_cents
                      )
                  )
                  AND event.topics @> coalesce(p_topics, '{}'::text[])
                GROUP BY event.canonical_event_id, event.start_at
            ), day_events AS MATERIALIZED (
                SELECT eligible.canonical_event_id,
                       (eligible.start_at AT TIME ZONE p_time_zone)::date AS start_day
                FROM eligible_events AS eligible
            ), day_totals AS (
                SELECT day_event.start_day,
                       count(DISTINCT day_event.canonical_event_id) AS day_event_count
                FROM day_events AS day_event
                GROUP BY day_event.start_day
            )
            SELECT day_total.start_day,
                   day_total.day_event_count,
                   selected.topic,
                   count(DISTINCT day_event.canonical_event_id)
            FROM day_events AS day_event
            JOIN day_totals AS day_total
              ON day_total.start_day = day_event.start_day
            JOIN public.canonical_events AS event
              ON event.canonical_event_id = day_event.canonical_event_id
            CROSS JOIN LATERAL unnest(
                CASE
                    WHEN coalesce(cardinality(event.topics), 0) = 0
                    THEN ARRAY['other']::text[]
                    ELSE event.topics
                END
            ) AS selected(topic)
            GROUP BY day_total.start_day, day_total.day_event_count, selected.topic;
        END;
        $$
        """
    )

    for signature in (_SOURCES_V2, _OBSERVATIONS_V2, _BROWSE_V9, _TOPICS_V3, _DAYS_V2):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    for signature in (_DAYS_V2, _TOPICS_V3, _BROWSE_V9, _OBSERVATIONS_V2, _SOURCES_V2):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
