"""Add deterministic event topics, extraction evidence, and topic browse facets.

Revision ID: 0136
Revises: 0135
Create Date: 2026-07-31

Topics are a small product taxonomy derived from explicit adapter metadata when it is available
at ingest, otherwise from conservative title/description rules.  This migration backfills the
same public taxonomy from retained canonical text and adds paging-safe server-side topic filters.
It never infers sensitive traits.  A textual free-price fallback recognizes only unambiguous
phrases and applies only where structured provider pricing is unknown.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0136"
down_revision: str | None = "0135"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXTRACT_V1 = "public.fn_extract_catalog_event_semantics_v1(text,text)"
_BROWSE_V7 = (
    "public.fn_browse_filtered_current_catalog_events_v7"
    "(text,timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "text[],timestamp with time zone,uuid,integer)"
)
_TOPICS_V1 = (
    "public.fn_list_catalog_topic_facets_v1"
    "(text,timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer)"
)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.canonical_events
        ADD COLUMN topics text[] NOT NULL DEFAULT ARRAY[]::text[],
        ADD COLUMN extraction_evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
        ADD CONSTRAINT ck_canonical_event_topics CHECK (
            cardinality(topics) <= 17
            AND array_position(topics, NULL) IS NULL
            AND topics <@ ARRAY[
                'ai', 'arts', 'community', 'education', 'family', 'food-drink', 'founders', 'gaming',
                'government', 'music', 'networking', 'outdoors', 'sports', 'technology',
                'volleyball', 'wellness', 'workshop'
            ]::text[]
        ),
        ADD CONSTRAINT ck_canonical_event_extraction_evidence CHECK (
            jsonb_typeof(extraction_evidence) = 'array'
            AND jsonb_array_length(extraction_evidence) <= 64
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_canonical_event_topics ON public.canonical_events USING gin (topics)"
    )
    _create_extractor()
    _backfill_semantics()
    _create_browse_v7()
    _create_topic_facets()

    op.execute(f"REVOKE ALL ON FUNCTION {_EXTRACT_V1} FROM PUBLIC, ec_app")
    for signature in (_BROWSE_V7, _TOPICS_V1):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    for signature in (_TOPICS_V1, _BROWSE_V7):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(f"DROP FUNCTION IF EXISTS {_EXTRACT_V1}")
    op.execute("DROP INDEX IF EXISTS public.ix_canonical_event_topics")
    op.execute(
        """
        ALTER TABLE public.canonical_events
        DROP CONSTRAINT IF EXISTS ck_canonical_event_extraction_evidence,
        DROP CONSTRAINT IF EXISTS ck_canonical_event_topics,
        DROP COLUMN IF EXISTS extraction_evidence,
        DROP COLUMN IF EXISTS topics
        """
    )


def _create_extractor() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_extract_catalog_event_semantics_v1(
            p_title text,
            p_description text
        )
        RETURNS TABLE (
            topics text[],
            extraction_evidence jsonb,
            inferred_price_status text
        )
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $$
        WITH input AS (
            SELECT lower(coalesce(p_title, '')) AS title,
                   lower(coalesce(p_description, '')) AS description
        ), rules(topic, pattern, ordinal) AS (
            VALUES
              ('ai', '(^|[^a-z0-9])(ai|artificial intelligence|generative ai|machine learning|large language models?|llms?|deep learning)([^a-z0-9]|$)', 1),
              ('arts', '(^|[^a-z0-9])(art|arts|gallery|exhibition|museum|theatre|theater|dance|film|cinema|poetry|crafts?)([^a-z0-9]|$)', 2),
              ('community', '(^|[^a-z0-9])(community|volunteer|neighborhood|neighbourhood)([^a-z0-9]|$)', 3),
              ('education', '(^|[^a-z0-9])(class|classes|lecture|seminar|storytime|tutoring)([^a-z0-9]|$)', 4),
              ('family', '(^|[^a-z0-9])(family|families|kids?|children|child[ -]?friendly|all ages)([^a-z0-9]|$)', 5),
              ('food-drink', '(^|[^a-z0-9])(food|cooking|dinner|brunch|wine tasting|coffee tasting)([^a-z0-9]|$)', 6),
              ('founders', '(^|[^a-z0-9])(founders?|co[ -]?founders?|entrepreneurs?|startups?)([^a-z0-9]|$)', 7),
              ('gaming', '(^|[^a-z0-9])(gaming|video games?|indie games?|fighting games?|e[ -]?sports?|super smash bros[.]?|smash bros[.]?|smash melee|super smash bros[.]? melee)([^a-z0-9]|$)', 17),
              ('government', '(^|[^a-z0-9])(city council|public meeting|commission meeting|board meeting)([^a-z0-9]|$)', 8),
              ('music', '(^|[^a-z0-9])(music|concert|jazz|orchestra|choir|songwriter|dj set)([^a-z0-9]|$)', 9),
              ('networking', '(^|[^a-z0-9])(networking|networking mixer|meet[ -]?up|social mixer)([^a-z0-9]|$)', 10),
              ('outdoors', '(^|[^a-z0-9])(outdoors?|hiking|nature walk|trail walk|birding|gardening)([^a-z0-9]|$)', 11),
              ('sports', '(^|[^a-z0-9])(sports?|basketball|baseball|softball|soccer|football|tennis|pickleball|golf|running|run club|cycling|hiking|volleyball|martial arts|swimming)([^a-z0-9]|$)', 12),
              ('technology', '(^|[^a-z0-9])(technology|software|hardware|developers?|coding|programming)([^a-z0-9]|$)', 13),
              ('volleyball', '(^|[^a-z0-9])(volleyball|beach volleyball)([^a-z0-9]|$)', 14),
              ('wellness', '(^|[^a-z0-9])(wellness|yoga|meditation|mindfulness|fitness)([^a-z0-9]|$)', 15),
              ('workshop', '(^|[^a-z0-9])(workshops?|hands[ -]?on|bootcamps?|training session)([^a-z0-9]|$)', 16)
        ), matches AS (
            SELECT rule.topic,
                   rule.ordinal,
                   CASE WHEN input.title ~ rule.pattern THEN 'title' ELSE 'description' END AS source
            FROM input
            JOIN rules AS rule
              ON input.title ~ rule.pattern OR input.description ~ rule.pattern
        ), price AS (
            SELECT CASE
                     WHEN input.title ~ '(^|[^a-z0-9])(free admission|admission is free|no cost|free of charge)([^a-z0-9]|$)'
                       OR input.title ~ '(^|[^a-z0-9])cost\s*:\s*free([!.]|\s|$)'
                       THEN 'title'
                     WHEN input.description ~ '(^|[^a-z0-9])(free admission|admission is free|no cost|free of charge)([^a-z0-9]|$)'
                       OR input.description ~ '(^|[^a-z0-9])cost\s*:\s*free([!.]|\s|$)'
                       THEN 'description'
                     ELSE NULL
                   END AS source
            FROM input
        ), evidence AS (
            SELECT jsonb_build_object(
                       'field', 'topic', 'value', match.topic, 'source', match.source,
                       'rule', 'keyword:' || match.topic
                   ) AS value,
                   match.ordinal
            FROM matches AS match
            UNION ALL
            SELECT jsonb_build_object(
                       'field', 'price_status', 'value', 'free', 'source', price.source,
                       'rule', 'unambiguous-free-text'
                   ),
                   100
            FROM price
            WHERE price.source IS NOT NULL
        )
        SELECT coalesce(
                   array_agg(match.topic ORDER BY match.ordinal)
                       FILTER (WHERE match.topic IS NOT NULL),
                   ARRAY[]::text[]
               ),
               coalesce(
                   (SELECT jsonb_agg(evidence.value ORDER BY evidence.ordinal) FROM evidence),
                   '[]'::jsonb
               ),
               CASE WHEN price.source IS NOT NULL THEN 'free' END
        FROM price
        LEFT JOIN matches AS match ON true
        GROUP BY price.source;
        $$
        """
    )


def _backfill_semantics() -> None:
    op.execute(
        """
        UPDATE public.canonical_events AS event
        SET topics = (
                SELECT projected.topics
                FROM public.fn_extract_catalog_event_semantics_v1(
                    event.title, event.description
                ) AS projected
            ),
            extraction_evidence = (
                SELECT projected.extraction_evidence
                FROM public.fn_extract_catalog_event_semantics_v1(
                    event.title, event.description
                ) AS projected
            )
        """
    )
    op.execute(
        """
        UPDATE public.event_source_links AS link
        SET price_status = 'free',
            price_min_cents = NULL,
            price_max_cents = NULL,
            price_currency = NULL
        FROM public.canonical_events AS event
        CROSS JOIN LATERAL public.fn_extract_catalog_event_semantics_v1(
            event.title, event.description
        ) AS projected
        WHERE event.canonical_event_id = link.canonical_event_id
          AND link.price_status = 'unknown'
          AND projected.inferred_price_status = 'free'
          AND 1 = (
              SELECT count(*)
              FROM public.event_source_links AS sibling
              WHERE sibling.canonical_event_id = event.canonical_event_id
          )
        """
    )
    op.execute(
        """
        UPDATE public.canonical_events AS event
        SET price_status = 'free',
            price_min_cents = NULL,
            price_max_cents = NULL,
            price_currency = NULL
        WHERE event.price_status = 'unknown'
          AND EXISTS (
              SELECT 1 FROM public.event_source_links AS link
              WHERE link.canonical_event_id = event.canonical_event_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.event_source_links AS link
              WHERE link.canonical_event_id = event.canonical_event_id
                AND link.price_status <> 'free'
          )
        """
    )


def _create_browse_v7() -> None:
    """Scan v6 in bounded pages so topic filtering remains keyset-correct."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_browse_filtered_current_catalog_events_v7(
            p_source_key text,
            p_window_start timestamptz,
            p_window_end timestamptz,
            p_query text,
            p_cities text[],
            p_location_scopes text[],
            p_price text,
            p_price_max_cents integer,
            p_topics text[],
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
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_after_start timestamptz := p_after_start;
            v_after_id uuid := p_after_id;
            v_batch_events integer;
            v_selected_events integer;
            v_returned integer := 0;
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 101
               OR cardinality(coalesce(p_topics, ARRAY[]::text[])) > 12
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_topics, ARRAY[]::text[])) AS topic(value)
                   WHERE topic.value IS NULL OR topic.value NOT IN (
                       'ai', 'arts', 'community', 'education', 'family', 'food-drink',
                       'founders', 'gaming', 'government', 'music', 'networking', 'outdoors', 'sports',
                       'technology', 'volleyball', 'wellness', 'workshop'
                   )
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog topic query is invalid';
            END IF;

            LOOP
                DROP TABLE IF EXISTS pg_temp.catalog_topic_scan;
                CREATE TEMP TABLE catalog_topic_scan ON COMMIT DROP AS
                SELECT *
                FROM public.fn_browse_filtered_current_catalog_events_v6(
                    p_source_key, p_window_start, p_window_end, p_query, p_cities,
                    p_location_scopes, p_price, p_price_max_cents,
                    v_after_start, v_after_id, 101
                );

                SELECT count(DISTINCT scan.canonical_event_id)
                INTO v_batch_events
                FROM catalog_topic_scan AS scan;
                EXIT WHEN v_batch_events = 0;

                DROP TABLE IF EXISTS pg_temp.catalog_topic_selected;
                CREATE TEMP TABLE catalog_topic_selected ON COMMIT DROP AS
                SELECT DISTINCT scan.canonical_event_id, scan.start_at
                FROM catalog_topic_scan AS scan
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = scan.canonical_event_id
                WHERE event.topics @> coalesce(p_topics, ARRAY[]::text[])
                ORDER BY scan.start_at, scan.canonical_event_id
                LIMIT (p_limit - v_returned);

                SELECT count(*) INTO v_selected_events FROM catalog_topic_selected;
                RETURN QUERY
                SELECT scan.canonical_event_id, scan.title, scan.start_at, scan.end_at,
                       scan.venue_name, scan.lat, scan.lon, scan.city_norm, scan.description,
                       scan.price_status, scan.price_min_cents, scan.price_max_cents,
                       scan.price_currency, scan.event_status, scan.normalizer_version,
                       scan.merge_version, scan.organizer_name, scan.host_names,
                       scan.speaker_names, scan.partner_names, scan.attendance_count,
                       scan.registration_status, scan.entity_profiles, scan.source_key,
                       scan.source_label, scan.publisher, scan.provider, scan.seed_url,
                       scan.observation_source, scan.source_event_id, scan.registration_url,
                       scan.last_seen_at, scan.refresh_run_key, event.topics,
                       event.extraction_evidence
                FROM catalog_topic_scan AS scan
                JOIN catalog_topic_selected AS selected
                  ON selected.canonical_event_id = scan.canonical_event_id
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = scan.canonical_event_id
                ORDER BY scan.start_at, scan.canonical_event_id, scan.source_key,
                         scan.observation_source, scan.source_event_id;

                v_returned := v_returned + v_selected_events;
                EXIT WHEN v_returned >= p_limit OR v_batch_events < 101;
                SELECT scan.start_at, scan.canonical_event_id
                INTO v_after_start, v_after_id
                FROM catalog_topic_scan AS scan
                GROUP BY scan.start_at, scan.canonical_event_id
                ORDER BY scan.start_at DESC, scan.canonical_event_id DESC
                LIMIT 1;
            END LOOP;
        END;
        $$
        """
    )


def _create_topic_facets() -> None:
    """Return per-scan topic counts; the repository safely sums identical topic rows."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_catalog_topic_facets_v1(
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
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_after_start timestamptz := NULL;
            v_after_id uuid := NULL;
            v_batch_events integer;
        BEGIN
            LOOP
                DROP TABLE IF EXISTS pg_temp.catalog_topic_facet_scan;
                CREATE TEMP TABLE catalog_topic_facet_scan ON COMMIT DROP AS
                SELECT *
                FROM public.fn_browse_filtered_current_catalog_events_v6(
                    p_source_key, p_window_start, p_window_end, p_query, p_cities,
                    p_location_scopes, p_price, p_price_max_cents,
                    v_after_start, v_after_id, 101
                );
                SELECT count(DISTINCT scan.canonical_event_id)
                INTO v_batch_events
                FROM catalog_topic_facet_scan AS scan;
                EXIT WHEN v_batch_events = 0;

                RETURN QUERY
                SELECT selected.topic, count(DISTINCT scan.canonical_event_id)
                FROM catalog_topic_facet_scan AS scan
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = scan.canonical_event_id
                CROSS JOIN LATERAL unnest(event.topics) AS selected(topic)
                GROUP BY selected.topic;

                EXIT WHEN v_batch_events < 101;
                SELECT scan.start_at, scan.canonical_event_id
                INTO v_after_start, v_after_id
                FROM catalog_topic_facet_scan AS scan
                GROUP BY scan.start_at, scan.canonical_event_id
                ORDER BY scan.start_at DESC, scan.canonical_event_id DESC
                LIMIT 1;
            END LOOP;
        END;
        $$
        """
    )
