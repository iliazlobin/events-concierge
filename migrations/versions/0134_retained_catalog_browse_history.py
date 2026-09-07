"""Browse retained past observations without weakening the live projection.

Revision ID: 0134
Revises: 0133
Create Date: 2026-07-31

The observation table retains one last-known row per source event identity; it is not version
history.  For an explicit window, v6 may therefore surface a retained successful observation when
the canonical event is in the past even if that identity rolled off the latest crawl.  Current and
future events still require the source's latest successful non-fixture run.  Canonical fields are
the latest known state, never an as-of reconstruction.

Source-scoped archive reads may span 7,305 days.  Unscoped reads retain the 370-day bound.  The
provider v3 projection also retains admitted sources with zero events in the requested range.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0134"
down_revision: str | None = "0133"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCES_V1 = "public.fn_list_admitted_catalog_browse_sources_v1(text)"
_OBSERVATIONS_V1 = (
    "public.fn_list_retained_catalog_browse_observations_v1"
    "(text,timestamp with time zone,timestamp with time zone)"
)
_BROWSE_V6 = (
    "public.fn_browse_filtered_current_catalog_events_v6"
    "(text,timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "timestamp with time zone,uuid,integer)"
)
_PROVIDERS_V3 = (
    "public.fn_list_current_catalog_providers_v3"
    "(text,timestamp with time zone,timestamp with time zone)"
)


def upgrade() -> None:
    """Install additive retained-history browse and zero-count provider capabilities."""
    _create_admitted_sources()
    _create_retained_observations()
    _create_browse_v6()
    _create_providers_v3()

    for signature in (_SOURCES_V1, _OBSERVATIONS_V1):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, ec_app")
    for signature in (_BROWSE_V6, _PROVIDERS_V3):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove only the additive 0134 capabilities."""
    for signature in (_PROVIDERS_V3, _BROWSE_V6):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for signature in (_OBSERVATIONS_V1, _SOURCES_V1):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")


def _create_admitted_sources() -> None:
    """Centralize current admission and fixture rules for both public projections."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_admitted_catalog_browse_sources_v1(
            p_source_key text
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
            IF p_source_key IS NOT NULL
               AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
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
              AND (p_source_key IS NULL OR source.source_key = p_source_key)
            ORDER BY lower(source.display_name), source.display_name, source.source_key;
        END;
        $$
        """
    )


def _create_retained_observations() -> None:
    """Select the correct observation lane on either side of statement time."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_retained_catalog_browse_observations_v1(
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
              AND event.start_at >= coalesce(p_window_start, statement_timestamp())
              AND (p_window_end IS NULL OR event.start_at < p_window_end)
              AND event.event_status <> 'cancelled'
              AND (
                  (
                      p_window_start IS NOT NULL
                      AND event.start_at < statement_timestamp()
                  )
                  OR success.run_key = observation.last_run_key
              );
        END;
        $$
        """
    )


def _create_browse_v6() -> None:
    """Filter retained-or-current observations before the stable keyset page."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v6(
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
                   observation.provider,
                   observation.seed_url,
                   observation.observation_source,
                   observation.source_event_id,
                   observation.registration_url,
                   observation.last_seen_at,
                   observation.refresh_run_key
            FROM page
            JOIN public.canonical_events AS event
              ON event.canonical_event_id = page.canonical_event_id
            JOIN browse_observations AS observation
              ON observation.canonical_event_id = event.canonical_event_id
            ORDER BY page.start_at,
                     page.canonical_event_id,
                     observation.source_key,
                     observation.observation_source,
                     observation.source_event_id;
        END;
        $$
        """
    )


def _create_providers_v3() -> None:
    """Return all admitted facets, including sources with zero events in range."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_current_catalog_providers_v3(
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
                    MESSAGE = 'catalog provider query is invalid';
            END IF;

            RETURN QUERY
            WITH source_facts AS MATERIALIZED (
                SELECT *
                FROM public.fn_list_admitted_catalog_browse_sources_v1(p_source_key)
            ), eligible_observations AS MATERIALIZED (
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v1(
                    p_source_key, p_window_start, p_window_end
                )
            )
            SELECT source.source_key,
                   source.source_label,
                   source.publisher,
                   source.provider,
                   source.seed_url,
                   count(DISTINCT observation.canonical_event_id)
            FROM source_facts AS source
            LEFT JOIN eligible_observations AS observation
              ON observation.source_key = source.source_key
            GROUP BY source.source_key,
                     source.source_label,
                     source.publisher,
                     source.provider,
                     source.seed_url
            ORDER BY lower(source.source_label), source.source_label, source.source_key;
        END;
        $$
        """
    )
