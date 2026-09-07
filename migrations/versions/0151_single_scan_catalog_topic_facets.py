"""Count catalog topic facets in one scan instead of paging the whole catalog.

Revision ID: 0151
Revises: 0150
Create Date: 2026-08-05

``fn_list_catalog_topic_facets_v1`` reused the bounded v6 browse capability, so it advanced a
101-event keyset over every eligible event and re-ran the whole observation/eligibility pipeline
once per batch.  That is linear in catalog size on a request path the consumer hits for every
uncached browse: at ~8.5k eligible events the facet call alone measured ~13.6s of a ~14s
``GET /v1/catalog/events``, while the page query beside it took ~0.2s.

Topic facets never needed the page.  ``fn_list_catalog_topic_facets_v2`` runs the identical
observation and eligibility pipeline once, unbounded, and aggregates topics from it directly.
The keyset predicate and the paging-only limit bound are the only filter differences, and both
exist solely to cut a scan into pages.  Counts, taxonomy, window rules, and the
before-topic-selection contract are unchanged; the repository still sums the returned rows, which
now arrive as one row per topic.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0151"
down_revision: str | None = "0150"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOPICS_V2 = (
    "public.fn_list_catalog_topic_facets_v2"
    "(text,timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer)"
)


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_catalog_topic_facets_v2(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer
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
                   p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
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
    op.execute(f"REVOKE ALL ON FUNCTION {_TOPICS_V2} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_TOPICS_V2} TO ec_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_TOPICS_V2}")
