"""Add chess and board-game topics to deterministic catalog semantics.

Revision ID: 0144
Revises: 0143
Create Date: 2026-08-03

The taxonomy remains small and provider-neutral. Chess is both a specific topic and a member of
the broader board-game topic. Existing canonical rows are backfilled from their retained public
title and description without replacing provider-derived topics or evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0144"
down_revision: str | None = "0143"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXTRACT_V1 = "public.fn_extract_catalog_event_semantics_v1(text,text)"
_BROWSE_V7 = (
    "public.fn_browse_filtered_current_catalog_events_v7"
    "(text,timestamp with time zone,timestamp with time zone,text,text[],text[],text,integer,"
    "text[],timestamp with time zone,uuid,integer)"
)
_LEGACY_TOPICS = (
    "ai",
    "arts",
    "community",
    "education",
    "family",
    "food-drink",
    "founders",
    "gaming",
    "government",
    "music",
    "networking",
    "outdoors",
    "sports",
    "technology",
    "volleyball",
    "wellness",
    "workshop",
)
_TOPICS = (
    "ai",
    "arts",
    "board-games",
    "chess",
    *_LEGACY_TOPICS[2:],
)
_PATTERNS = {
    "ai": r"(^|[^a-z0-9])(ai|artificial intelligence|generative ai|machine learning|large language models?|llms?|deep learning)([^a-z0-9]|$)",
    "arts": r"(^|[^a-z0-9])(art|arts|gallery|exhibition|museum|theatre|theater|dance|film|cinema|poetry|crafts?)([^a-z0-9]|$)",
    "board-games": r"(^|[^a-z0-9])(board[ -]?games?|tabletop[ -]?games?|chess|bughouse|mah[ -]?jongg?|cribbage|backgammon)([^a-z0-9]|$)",
    "chess": r"(^|[^a-z0-9])(chess|bughouse)([^a-z0-9]|$)",
    "community": r"(^|[^a-z0-9])(community|volunteer|neighborhood|neighbourhood)([^a-z0-9]|$)",
    "education": r"(^|[^a-z0-9])(class|classes|lecture|seminar|storytime|tutoring)([^a-z0-9]|$)",
    "family": r"(^|[^a-z0-9])(family|families|kids?|children|child[ -]?friendly|all ages)([^a-z0-9]|$)",
    "food-drink": r"(^|[^a-z0-9])(food|cooking|dinner|brunch|wine tasting|coffee tasting)([^a-z0-9]|$)",
    "founders": r"(^|[^a-z0-9])(founders?|co[ -]?founders?|entrepreneurs?|startups?)([^a-z0-9]|$)",
    "gaming": r"(^|[^a-z0-9])(gaming|video games?|indie games?|fighting games?|e[ -]?sports?|super smash bros[.]?|smash bros[.]?|smash melee|super smash bros[.]? melee)([^a-z0-9]|$)",
    "government": r"(^|[^a-z0-9])(city council|public meeting|commission meeting|board meeting)([^a-z0-9]|$)",
    "music": r"(^|[^a-z0-9])(music|concert|jazz|orchestra|choir|songwriter|dj set)([^a-z0-9]|$)",
    "networking": r"(^|[^a-z0-9])(networking|networking mixer|meet[ -]?up|social mixer)([^a-z0-9]|$)",
    "outdoors": r"(^|[^a-z0-9])(outdoors?|hiking|nature walk|trail walk|birding|gardening)([^a-z0-9]|$)",
    "sports": r"(^|[^a-z0-9])(sports?|basketball|baseball|softball|soccer|football|tennis|pickleball|golf|running|run club|cycling|hiking|volleyball|martial arts|swimming)([^a-z0-9]|$)",
    "technology": r"(^|[^a-z0-9])(technology|software|hardware|developers?|coding|programming)([^a-z0-9]|$)",
    "volleyball": r"(^|[^a-z0-9])(volleyball|beach volleyball)([^a-z0-9]|$)",
    "wellness": r"(^|[^a-z0-9])(wellness|yoga|meditation|mindfulness|fitness)([^a-z0-9]|$)",
    "workshop": r"(^|[^a-z0-9])(workshops?|hands[ -]?on|bootcamps?|training session)([^a-z0-9]|$)",
}


def upgrade() -> None:
    _replace_topic_constraint(_TOPICS)
    _replace_extractor(_TOPICS)
    _backfill_board_game_topics()
    _replace_browse_v7(_TOPICS)
    op.execute(f"REVOKE ALL ON FUNCTION {_EXTRACT_V1} FROM PUBLIC, ec_app")
    op.execute(f"REVOKE ALL ON FUNCTION {_BROWSE_V7} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_BROWSE_V7} TO ec_app")


def downgrade() -> None:
    op.execute(
        """
        UPDATE public.canonical_events AS event
        SET topics = array_remove(array_remove(event.topics, 'board-games'), 'chess'),
            extraction_evidence = coalesce(
                (
                    SELECT jsonb_agg(item.value ORDER BY item.ordinality)
                    FROM jsonb_array_elements(event.extraction_evidence)
                         WITH ORDINALITY AS item(value, ordinality)
                    WHERE item.value ->> 'value' NOT IN ('board-games', 'chess')
                ),
                '[]'::jsonb
            )
        WHERE event.topics && ARRAY['board-games', 'chess']::text[]
           OR EXISTS (
                SELECT 1
                FROM jsonb_array_elements(event.extraction_evidence) AS evidence(value)
                WHERE evidence.value ->> 'value' IN ('board-games', 'chess')
            )
        """
    )
    _replace_topic_constraint(_LEGACY_TOPICS)
    _replace_extractor(_LEGACY_TOPICS)
    _replace_browse_v7(_LEGACY_TOPICS)
    op.execute(f"REVOKE ALL ON FUNCTION {_EXTRACT_V1} FROM PUBLIC, ec_app")
    op.execute(f"REVOKE ALL ON FUNCTION {_BROWSE_V7} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_BROWSE_V7} TO ec_app")


def _quoted_topics(topics: tuple[str, ...]) -> str:
    return ", ".join(f"'{topic}'" for topic in topics)


def _replace_topic_constraint(topics: tuple[str, ...]) -> None:
    op.execute(
        f"""
        ALTER TABLE public.canonical_events
        DROP CONSTRAINT IF EXISTS ck_canonical_event_topics;
        ALTER TABLE public.canonical_events
        ADD CONSTRAINT ck_canonical_event_topics CHECK (
            cardinality(topics) <= {len(topics)}
            AND array_position(topics, NULL) IS NULL
            AND topics <@ ARRAY[{_quoted_topics(topics)}]::text[]
        )
        """
    )


def _replace_extractor(topics: tuple[str, ...]) -> None:
    values = ",\n              ".join(
        f"('{topic}', '{_PATTERNS[topic]}', {ordinal})"
        for ordinal, topic in enumerate(topics, start=1)
    )
    op.execute(
        rf"""
        CREATE OR REPLACE FUNCTION public.fn_extract_catalog_event_semantics_v1(
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
              {values}
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


def _backfill_board_game_topics() -> None:
    ordered_topics = ", ".join(
        f"('{topic}', {ordinal})" for ordinal, topic in enumerate(_TOPICS, start=1)
    )
    op.execute(
        f"""
        WITH projections AS (
            SELECT source.canonical_event_id,
                   projected.topics,
                   projected.extraction_evidence
            FROM public.canonical_events AS source
            CROSS JOIN LATERAL public.fn_extract_catalog_event_semantics_v1(
                source.title, source.description
            ) AS projected
            WHERE projected.topics && ARRAY['board-games', 'chess']::text[]
        )
        UPDATE public.canonical_events AS event
        SET topics = (
                SELECT coalesce(
                    array_agg(allowed.topic ORDER BY allowed.ordinal),
                    ARRAY[]::text[]
                )
                FROM (VALUES {ordered_topics}) AS allowed(topic, ordinal)
                WHERE allowed.topic = ANY(event.topics)
                   OR allowed.topic = ANY(projected.topics)
            ),
            extraction_evidence = event.extraction_evidence || coalesce(
                (
                    SELECT jsonb_agg(item.value ORDER BY item.ordinality)
                    FROM jsonb_array_elements(projected.extraction_evidence)
                         WITH ORDINALITY AS item(value, ordinality)
                    WHERE item.value ->> 'field' = 'topic'
                      AND item.value ->> 'value' IN ('board-games', 'chess')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM jsonb_array_elements(event.extraction_evidence) AS prior(value)
                          WHERE prior.value ->> 'field' = 'topic'
                            AND prior.value ->> 'value' = item.value ->> 'value'
                      )
                ),
                '[]'::jsonb
            )
        FROM projections AS projected
        WHERE projected.canonical_event_id = event.canonical_event_id
        """
    )


def _replace_browse_v7(topics: tuple[str, ...]) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_browse_filtered_current_catalog_events_v7(
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
            v_returned integer := 0;
            v_current_event_id uuid;
            v_current_selected boolean;
            v_row record;
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 101
               OR cardinality(coalesce(p_topics, ARRAY[]::text[])) > 12
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_topics, ARRAY[]::text[])) AS topic(value)
                   WHERE topic.value IS NULL OR topic.value NOT IN ({_quoted_topics(topics)})
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'catalog topic query is invalid';
            END IF;

            LOOP
                v_batch_events := 0;
                v_current_event_id := NULL;
                v_current_selected := false;

                FOR v_row IN
                    SELECT scan.canonical_event_id, scan.title, scan.start_at, scan.end_at,
                           scan.venue_name, scan.lat, scan.lon, scan.city_norm, scan.description,
                           scan.price_status, scan.price_min_cents, scan.price_max_cents,
                           scan.price_currency, scan.event_status, scan.normalizer_version,
                           scan.merge_version, scan.organizer_name, scan.host_names,
                           scan.speaker_names, scan.partner_names, scan.attendance_count,
                           scan.registration_status, scan.entity_profiles, scan.source_key,
                           scan.source_label, scan.publisher, scan.provider, scan.seed_url,
                           scan.observation_source, scan.source_event_id, scan.registration_url,
                           scan.last_seen_at, scan.refresh_run_key,
                           event.topics AS event_topics,
                           event.extraction_evidence AS event_extraction_evidence
                    FROM public.fn_browse_filtered_current_catalog_events_v6(
                        p_source_key, p_window_start, p_window_end, p_query, p_cities,
                        p_location_scopes, p_price, p_price_max_cents,
                        v_after_start, v_after_id, 101
                    ) AS scan
                    JOIN public.canonical_events AS event
                      ON event.canonical_event_id = scan.canonical_event_id
                    ORDER BY scan.start_at, scan.canonical_event_id, scan.source_key,
                             scan.observation_source, scan.source_event_id
                LOOP
                    IF v_current_event_id IS DISTINCT FROM v_row.canonical_event_id THEN
                        v_current_event_id := v_row.canonical_event_id;
                        v_batch_events := v_batch_events + 1;
                        v_after_start := v_row.start_at;
                        v_after_id := v_row.canonical_event_id;
                        v_current_selected := v_returned < p_limit
                            AND v_row.event_topics @> coalesce(p_topics, ARRAY[]::text[]);
                        IF v_current_selected THEN
                            v_returned := v_returned + 1;
                        END IF;
                    END IF;

                    IF v_current_selected THEN
                        canonical_event_id := v_row.canonical_event_id;
                        title := v_row.title;
                        start_at := v_row.start_at;
                        end_at := v_row.end_at;
                        venue_name := v_row.venue_name;
                        lat := v_row.lat;
                        lon := v_row.lon;
                        city_norm := v_row.city_norm;
                        description := v_row.description;
                        price_status := v_row.price_status;
                        price_min_cents := v_row.price_min_cents;
                        price_max_cents := v_row.price_max_cents;
                        price_currency := v_row.price_currency;
                        event_status := v_row.event_status;
                        normalizer_version := v_row.normalizer_version;
                        merge_version := v_row.merge_version;
                        organizer_name := v_row.organizer_name;
                        host_names := v_row.host_names;
                        speaker_names := v_row.speaker_names;
                        partner_names := v_row.partner_names;
                        attendance_count := v_row.attendance_count;
                        registration_status := v_row.registration_status;
                        entity_profiles := v_row.entity_profiles;
                        source_key := v_row.source_key;
                        source_label := v_row.source_label;
                        publisher := v_row.publisher;
                        provider := v_row.provider;
                        seed_url := v_row.seed_url;
                        observation_source := v_row.observation_source;
                        source_event_id := v_row.source_event_id;
                        registration_url := v_row.registration_url;
                        last_seen_at := v_row.last_seen_at;
                        refresh_run_key := v_row.refresh_run_key;
                        topics := v_row.event_topics;
                        extraction_evidence := v_row.event_extraction_evidence;
                        RETURN NEXT;
                    END IF;
                END LOOP;

                EXIT WHEN v_batch_events = 0
                          OR v_returned >= p_limit
                          OR v_batch_events < 101;
            END LOOP;
        END;
        $$
        """
    )
