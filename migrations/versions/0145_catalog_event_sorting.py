"""Add direction-aware, stable catalog event paging.

Revision ID: 0145
Revises: 0144
Create Date: 2026-08-03

Sorting is part of the database paging capability rather than a presentation-only reversal.  That
keeps every page globally ordered and lets the API bind cursors to the selected direction.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0145"
down_revision: str | None = "0144"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BROWSE_V8 = (
    "public.fn_browse_filtered_current_catalog_events_v8"
    "(text,timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "text[],text,timestamp with time zone,uuid,integer)"
)


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v8(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
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
                   p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
               OR ((p_after_start IS NULL) <> (p_after_id IS NULL))
               OR ((p_window_start IS NULL) <> (p_window_end IS NULL))
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
                FROM public.fn_list_retained_catalog_browse_observations_v1(
                    p_source_key, p_window_start, p_window_end
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
    op.execute(f"REVOKE ALL ON FUNCTION {_BROWSE_V8} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_BROWSE_V8} TO ec_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_BROWSE_V8}")
