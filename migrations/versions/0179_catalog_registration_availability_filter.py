"""Filter the consumer catalog by confirmed registration availability.

Revision ID: 0179
Revises: 0178
Create Date: 2026-08-28

The catalog already carries a four-state source observation -- open, waitlist, sold out, or
unknown -- but browsing could only display it.  This revision adds the two product choices the
consumer needs: ``available`` means confirmed ``open`` and ``sold_out`` means confirmed
``sold_out``.  Waitlists and unknown inventory intentionally satisfy neither choice.

The v9 browse capability owns a long, security-reviewed eligibility predicate.  Rather than copy
that predicate into three new functions and let the page, topic facets, and calendar drift, the
migration clones it as a private unbounded primitive.  The public v10 page applies availability
before its own bounded keyset page; the topic and day aggregates use the same primitive once and
aggregate all eligible events.  The existing v9 function remains bounded and unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0179"
down_revision: str | None = "0178"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BROWSE_V9 = (
    "public.fn_browse_filtered_current_catalog_events_v9"
    "(text[],timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "integer,text[],text,timestamp with time zone,uuid,integer)"
)
_BROWSE_UNBOUNDED_V1 = (
    "public.fn_browse_filtered_current_catalog_events_unbounded_v1"
    "(text[],timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "integer,text[],text,timestamp with time zone,uuid,integer)"
)
_BROWSE_V10 = (
    "public.fn_browse_filtered_current_catalog_events_v10"
    "(text[],timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "integer,text[],text,text,timestamp with time zone,uuid,integer)"
)
_TOPICS_V4 = (
    "public.fn_list_catalog_topic_facets_v4"
    "(text[],timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "integer,text)"
)
_DAYS_V3 = (
    "public.fn_list_catalog_day_facets_v3"
    "(text[],timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "integer,text[],text,text)"
)


def upgrade() -> None:
    # Clone the reviewed v9 predicate and relax only its page bound.  The clone is never granted
    # to the application role; it is an implementation primitive under the public, bounded page
    # and aggregate functions below.  Guards make an unexpected v9 definition fail the migration
    # rather than silently produce a differently governed capability.
    op.execute(
        rf"""
        DO $availability_filter$
        DECLARE
            v_definition text;
            v_original text;
        BEGIN
            SELECT pg_catalog.pg_get_functiondef('{_BROWSE_V9}'::pg_catalog.regprocedure)
            INTO v_definition;
            v_original := v_definition;
            v_definition := replace(
                v_definition,
                'fn_browse_filtered_current_catalog_events_v9',
                'fn_browse_filtered_current_catalog_events_unbounded_v1'
            );
            v_definition := replace(
                v_definition,
                'IF p_limit IS NULL OR p_limit < 1 OR p_limit > 101',
                'IF p_limit IS NOT NULL AND (p_limit < 1 OR p_limit > 101)'
            );
            IF v_definition = v_original
               OR position(
                   'fn_browse_filtered_current_catalog_events_unbounded_v1' IN v_definition
               ) = 0
               OR position(
                   'IF p_limit IS NOT NULL AND (p_limit < 1 OR p_limit > 101)' IN v_definition
               ) = 0
            THEN
                RAISE EXCEPTION 'unexpected catalog browse v9 definition';
            END IF;
            EXECUTE v_definition;
        END;
        $availability_filter$;
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v10(
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
            p_availability text,
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
               OR (
                   p_availability IS NOT NULL
                   AND p_availability NOT IN ('available', 'sold_out')
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog browse query is invalid';
            END IF;

            RETURN QUERY
            WITH eligible_rows AS MATERIALIZED (
                SELECT candidate.*
                FROM public.fn_browse_filtered_current_catalog_events_unbounded_v1(
                    p_source_keys, p_window_start, p_window_end, p_query, p_cities,
                    p_location_scopes, p_price, p_price_max_cents, p_price_min_cents,
                    p_topics, p_sort, p_after_start, p_after_id, NULL
                ) AS candidate
                WHERE p_availability IS NULL
                   OR (
                       p_availability = 'available'
                       AND candidate.registration_status = 'open'
                   )
                   OR (
                       p_availability = 'sold_out'
                       AND candidate.registration_status = 'sold_out'
                   )
            ), page AS MATERIALIZED (
                SELECT eligible.canonical_event_id,
                       eligible.start_at
                FROM eligible_rows AS eligible
                GROUP BY eligible.canonical_event_id, eligible.start_at
                ORDER BY
                    CASE WHEN p_sort = 'soonest' THEN eligible.start_at END ASC,
                    CASE WHEN p_sort = 'soonest' THEN eligible.canonical_event_id END ASC,
                    CASE WHEN p_sort = 'latest' THEN eligible.start_at END DESC,
                    CASE WHEN p_sort = 'latest' THEN eligible.canonical_event_id END DESC
                LIMIT p_limit
            )
            SELECT eligible.*
            FROM page
            JOIN eligible_rows AS eligible
              ON eligible.canonical_event_id = page.canonical_event_id
            ORDER BY
                CASE WHEN p_sort = 'soonest' THEN page.start_at END ASC,
                CASE WHEN p_sort = 'soonest' THEN page.canonical_event_id END ASC,
                CASE WHEN p_sort = 'latest' THEN page.start_at END DESC,
                CASE WHEN p_sort = 'latest' THEN page.canonical_event_id END DESC,
                eligible.source_key,
                eligible.observation_source,
                eligible.source_event_id;
        END;
        $$
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_catalog_topic_facets_v4(
            p_source_keys text[],
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
            p_price_min_cents integer,
            p_availability text
        )
        RETURNS TABLE (topic text, event_count bigint)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_availability IS NOT NULL
               AND p_availability NOT IN ('available', 'sold_out')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'catalog browse query is invalid';
            END IF;

            RETURN QUERY
            WITH eligible_events AS MATERIALIZED (
                SELECT DISTINCT candidate.canonical_event_id,
                                candidate.topics
                FROM public.fn_browse_filtered_current_catalog_events_unbounded_v1(
                    p_source_keys, p_window_start, p_window_end, p_query, p_cities,
                    p_location_scopes, p_price, p_price_max_cents, p_price_min_cents,
                    '{}'::text[], 'soonest', NULL, NULL, NULL
                ) AS candidate
                WHERE p_availability IS NULL
                   OR (
                       p_availability = 'available'
                       AND candidate.registration_status = 'open'
                   )
                   OR (
                       p_availability = 'sold_out'
                       AND candidate.registration_status = 'sold_out'
                   )
            )
            SELECT selected.topic,
                   count(DISTINCT eligible.canonical_event_id)
            FROM eligible_events AS eligible
            CROSS JOIN LATERAL unnest(eligible.topics) AS selected(topic)
            GROUP BY selected.topic;
        END;
        $$
        """
    )

    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_catalog_day_facets_v3(
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
            p_availability text,
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
            IF (
                   p_availability IS NOT NULL
                   AND p_availability NOT IN ('available', 'sold_out')
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
            WITH eligible_events AS MATERIALIZED (
                SELECT DISTINCT candidate.canonical_event_id,
                                candidate.start_at,
                                candidate.topics
                FROM public.fn_browse_filtered_current_catalog_events_unbounded_v1(
                    p_source_keys, p_window_start, p_window_end, p_query, p_cities,
                    p_location_scopes, p_price, p_price_max_cents, p_price_min_cents,
                    p_topics, 'soonest', NULL, NULL, NULL
                ) AS candidate
                WHERE p_availability IS NULL
                   OR (
                       p_availability = 'available'
                       AND candidate.registration_status = 'open'
                   )
                   OR (
                       p_availability = 'sold_out'
                       AND candidate.registration_status = 'sold_out'
                   )
            ), day_events AS MATERIALIZED (
                SELECT eligible.canonical_event_id,
                       (eligible.start_at AT TIME ZONE p_time_zone)::date AS start_day,
                       eligible.topics
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
            CROSS JOIN LATERAL unnest(
                CASE
                    WHEN coalesce(cardinality(day_event.topics), 0) = 0
                    THEN ARRAY['other']::text[]
                    ELSE day_event.topics
                END
            ) AS selected(topic)
            GROUP BY day_total.start_day, day_total.day_event_count, selected.topic;
        END;
        $$
        """
    )

    op.execute(f"REVOKE ALL ON FUNCTION {_BROWSE_UNBOUNDED_V1} FROM PUBLIC")
    for signature in (_BROWSE_V10, _TOPICS_V4, _DAYS_V3):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    for signature in (_DAYS_V3, _TOPICS_V4, _BROWSE_V10, _BROWSE_UNBOUNDED_V1):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
