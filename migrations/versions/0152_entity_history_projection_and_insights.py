"""Project entity mentions over retained history and derive catalog-owned entity insights.

Revision ID: 0152
Revises: 0151
Create Date: 2026-08-06

``fn_refresh_catalog_entity_index_v1`` projected roles from
``fn_list_retained_catalog_browse_observations_v1(source, NULL, NULL)``.  That unbounded mode uses
``statement_timestamp()`` as its inclusive lower bound, so an elapsed event was never projected:
0 of 321 past events carrying role names had a mention row, against 83% of future ones.  The
consumer can browse a historical date range (``design/catalog-browse-history.md``), so every host
or organizer chip on an elapsed event resolved to nothing and the UI surfaced a bare 404.

``fn_refresh_catalog_entity_index_v2`` keeps the live call exactly as it was and adds one bounded
history call for the trailing 365 days.  The two windows are mutually exclusive by the retained
capability's own predicates -- the live mode requires an event that has not ended and an
observation from the source's latest successful run, while explicit-window mode admits an
already-ended event -- so the union widens coverage without changing what the live projection
already produced.  365 days keeps the unscoped rebuild inside the capability's 370-day
unscoped-window ceiling.

``fn_list_catalog_entity_events_v2`` returns ``is_past`` and orders upcoming ascending before past
descending, so history cannot crowd upcoming appearances out of the caller's limit.

``fn_get_catalog_entity_insights_v1`` derives insights from the catalog we already own -- cadence,
typical attendance, price mix, topics, venues, cities, and recurring collaborators.  It reads only
admitted mentions and canonical events: no network call, no provider, no name-only enrichment, so
it covers source-scoped entities that the enrichment plane deliberately never researches.
Cadence is measured over a trailing 365-day/leading 90-day activity window rather than the whole
span, because a single far-future outlier (the catalog holds start dates out to 2099) would
otherwise flatten the rate to zero.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0152"
down_revision: str | None = "0151"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFRESH_V2 = "public.fn_refresh_catalog_entity_index_v2(text)"
_EVENTS_V2 = "public.fn_list_catalog_entity_events_v2(uuid,integer)"
_INSIGHTS = "public.fn_get_catalog_entity_insights_v1(uuid)"


def upgrade() -> None:
    _create_refresh_v2()
    _create_events_v2()
    _create_insights()
    for signature in (_REFRESH_V2, _EVENTS_V2, _INSIGHTS):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
    # Rebuild the whole projection once so retained history is covered without waiting for each
    # source's next refresh commit.  This reads stored observations only -- no provider egress.
    # The 0148/0149 quality gate must run after any projection rebuild, exactly as the refresh
    # commit and paged promotion paths sequence it.
    op.execute("SELECT public.fn_refresh_catalog_entity_index_v2(NULL)")
    op.execute("SELECT public.fn_prune_catalog_entity_index_v1(NULL)")


def downgrade() -> None:
    for signature in (_INSIGHTS, _EVENTS_V2, _REFRESH_V2):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    # Restore the live-only projection the v1 capability describes, gated the same way.
    op.execute("SELECT public.fn_refresh_catalog_entity_index_v1(NULL)")
    op.execute("SELECT public.fn_prune_catalog_entity_index_v1(NULL)")


def _create_refresh_v2() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_refresh_catalog_entity_index_v2(p_source_key text)
        RETURNS integer
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_fact record;
            v_enrichment record;
            v_count integer;
        BEGIN
            IF p_source_key IS NOT NULL
               AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'entity source is invalid';
            END IF;

            DROP TABLE IF EXISTS pg_temp.entity_refresh_facts;
            CREATE TEMP TABLE entity_refresh_facts ON COMMIT DROP AS
            WITH browse_observations AS MATERIALIZED (
                -- Live catalog rows: unchanged from the v1 projection.
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v1(
                    p_source_key, NULL, NULL
                )
                UNION ALL
                -- Retained history: only events that already ended enter this branch, so the two
                -- windows cannot return the same observation twice.
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v1(
                    p_source_key,
                    statement_timestamp() - interval '365 days',
                    statement_timestamp()
                )
            ), role_facts AS (
                SELECT observation.source_key,
                       observation.observation_source AS source,
                       observation.source_event_id,
                       observation.refresh_run_key AS source_run_key,
                       observation.canonical_event_id,
                       observation.last_seen_at AS observed_at,
                       role_fact.role,
                       role_fact.name
                FROM browse_observations AS observation
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = observation.canonical_event_id
                CROSS JOIN LATERAL (
                    SELECT 'organizer'::text, event.organizer_name
                    WHERE nullif(btrim(event.organizer_name), '') IS NOT NULL
                    UNION ALL
                    SELECT 'host'::text, name FROM unnest(event.host_names) AS name
                    UNION ALL
                    SELECT 'speaker'::text, name FROM unnest(event.speaker_names) AS name
                    UNION ALL
                    SELECT 'partner'::text, name FROM unnest(event.partner_names) AS name
                ) AS role_fact(role, name)
                WHERE nullif(btrim(role_fact.name), '') IS NOT NULL
            ), profiled AS (
                SELECT role_fact.*,
                       profile.value AS profile
                FROM role_facts AS role_fact
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = role_fact.canonical_event_id
                LEFT JOIN LATERAL (
                    SELECT candidate.value
                    FROM jsonb_array_elements(event.entity_profiles) AS candidate(value)
                    WHERE candidate.value ->> 'role' = role_fact.role
                      AND lower(btrim(candidate.value ->> 'name')) = lower(btrim(role_fact.name))
                    LIMIT 1
                ) AS profile ON true
            )
            SELECT source_key,
                   source,
                   source_event_id,
                   source_run_key,
                   canonical_event_id,
                   observed_at,
                   role,
                   btrim(name) AS observed_name,
                   regexp_replace(lower(btrim(name)), '\s+', ' ', 'g') AS normalized_name,
                   CASE
                       WHEN profile IS NOT NULL THEN profile ->> 'kind'
                       WHEN role IN ('organizer', 'partner') THEN 'organization'
                       WHEN role = 'speaker' THEN 'person'
                       WHEN role = 'host' AND name ~* (
                           '(^|[^[:alnum:]])(association|capital|club|collective|company|events|'
                           'foundation|group|inc|institute|labs?|library|llc|magazine|museum|'
                           'school|society|studio|university|ventures)([^[:alnum:]]|$)'
                       ) THEN 'organization'
                       ELSE 'unknown'
                   END AS kind,
                   CASE WHEN profile IS NOT NULL
                        THEN profile ->> 'profile_url' ELSE NULL END AS profile_url,
                   CASE WHEN profile IS NOT NULL THEN 'profile_verified'
                        ELSE 'source_scoped' END AS identity_status,
                   CASE WHEN profile IS NOT NULL
                        THEN 'profile:' || md5(lower(profile ->> 'profile_url'))
                        ELSE 'source:' || md5(
                            source_key || chr(31) ||
                            CASE
                                WHEN role IN ('organizer', 'partner') THEN 'organization'
                                WHEN role = 'speaker' THEN 'person'
                                WHEN role = 'host' AND name ~* (
                                    '(^|[^[:alnum:]])(association|capital|club|collective|company|'
                                    'events|foundation|group|inc|institute|labs?|library|llc|'
                                    'magazine|museum|school|society|studio|university|ventures)'
                                    '([^[:alnum:]]|$)'
                                ) THEN 'organization'
                                ELSE 'unknown'
                            END || chr(31) ||
                            regexp_replace(lower(btrim(name)), '\s+', ' ', 'g')
                        )
                   END AS identity_key
            FROM profiled;

            IF p_source_key IS NULL THEN
                DELETE FROM public.catalog_entity_event_mentions;
                DELETE FROM public.catalog_entity_source_links;
            ELSE
                DELETE FROM public.catalog_entity_event_mentions
                WHERE source_key = p_source_key;
                DELETE FROM public.catalog_entity_source_links
                WHERE source_key = p_source_key;
            END IF;

            INSERT INTO public.catalog_entities (
                identity_key, identity_status, kind, display_name, normalized_name,
                canonical_profile_url, first_seen_at, last_seen_at
            )
            SELECT identity_key,
                   max(identity_status),
                   (array_agg(kind ORDER BY CASE kind
                       WHEN 'organization' THEN 1 WHEN 'person' THEN 2 ELSE 3 END))[1],
                   (array_agg(observed_name ORDER BY observed_at DESC, observed_name))[1],
                   max(normalized_name),
                   max(profile_url),
                   min(observed_at),
                   max(observed_at)
            FROM entity_refresh_facts
            GROUP BY identity_key
            ON CONFLICT (identity_key) DO UPDATE
            SET identity_status = EXCLUDED.identity_status,
                kind = EXCLUDED.kind,
                display_name = EXCLUDED.display_name,
                normalized_name = EXCLUDED.normalized_name,
                canonical_profile_url = EXCLUDED.canonical_profile_url,
                first_seen_at = least(public.catalog_entities.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at = greatest(public.catalog_entities.last_seen_at, EXCLUDED.last_seen_at),
                updated_at = clock_timestamp();

            INSERT INTO public.catalog_entity_event_mentions (
                entity_id, canonical_event_id, source_key, source, source_event_id,
                source_run_key, role, observed_name, observed_at
            )
            SELECT DISTINCT ON (
                       entity.entity_id, fact.canonical_event_id, fact.source_key, fact.role
                   )
                   entity.entity_id,
                   fact.canonical_event_id,
                   fact.source_key,
                   fact.source,
                   fact.source_event_id,
                   fact.source_run_key,
                   fact.role,
                   fact.observed_name,
                   fact.observed_at
            FROM entity_refresh_facts AS fact
            JOIN public.catalog_entities AS entity
              ON entity.identity_key = fact.identity_key
            ORDER BY entity.entity_id, fact.canonical_event_id, fact.source_key, fact.role,
                     fact.observed_at DESC, fact.source_event_id
            ON CONFLICT (entity_id, canonical_event_id, source_key, role) DO UPDATE
            SET source = EXCLUDED.source,
                source_event_id = EXCLUDED.source_event_id,
                source_run_key = EXCLUDED.source_run_key,
                observed_name = EXCLUDED.observed_name,
                observed_at = EXCLUDED.observed_at;

            -- Only exact, bounded profile URLs enter the enrichment identity plane.  Name-only
            -- entities remain browseable but cannot be researched automatically.
            FOR v_fact IN
                SELECT DISTINCT fact.*, entity.entity_id AS catalog_entity_id
                FROM entity_refresh_facts AS fact
                JOIN public.catalog_entities AS entity
                  ON entity.identity_key = fact.identity_key
                WHERE fact.profile_url IS NOT NULL
                  AND char_length(fact.profile_url) <= 500
                  AND fact.kind IN ('person', 'organization')
            LOOP
                SELECT * INTO v_enrichment
                FROM public.fn_upsert_entity_enrichment_source_fact(
                    v_fact.source_key,
                    v_fact.source,
                    v_fact.source_event_id,
                    v_fact.source_run_key,
                    v_fact.profile_url,
                    'source_profile_url',
                    v_fact.role,
                    v_fact.kind,
                    v_fact.observed_name,
                    v_fact.profile_url,
                    10000,
                    v_fact.observed_at,
                    NULL
                );
                IF v_enrichment.outcome IN ('inserted', 'updated', 'replayed') THEN
                    INSERT INTO public.catalog_entity_source_links (
                        catalog_entity_id, source_entity_id, source_key
                    )
                    VALUES (
                        v_fact.catalog_entity_id, v_enrichment.entity_id, v_fact.source_key
                    )
                    ON CONFLICT (source_entity_id) DO UPDATE
                    SET catalog_entity_id = EXCLUDED.catalog_entity_id,
                        source_key = EXCLUDED.source_key;
                END IF;
            END LOOP;

            DELETE FROM public.catalog_entities AS entity
            WHERE NOT EXISTS (
                SELECT 1 FROM public.catalog_entity_event_mentions AS mention
                WHERE mention.entity_id = entity.entity_id
            );
            SELECT count(*)::integer INTO v_count FROM public.catalog_entities;
            RETURN v_count;
        END;
        $$
        """
    )


def _create_events_v2() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_catalog_entity_events_v2(
            p_entity_id uuid,
            p_limit integer
        )
        RETURNS TABLE (
            canonical_event_id uuid,
            title text,
            start_at timestamptz,
            end_at timestamptz,
            venue_name text,
            city text,
            description text,
            roles text[],
            source_labels text[],
            registration_url text,
            is_past boolean
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'entity events limit is invalid';
            END IF;
            RETURN QUERY
            WITH mentioned AS (
                SELECT event.canonical_event_id,
                       event.title,
                       event.start_at,
                       event.end_at,
                       event.venue_name,
                       event.city_norm AS city,
                       left(event.description, 800) AS description,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                       array_agg(
                           DISTINCT source.display_name ORDER BY source.display_name
                       ) AS source_labels,
                       min(observation.registration_url) AS registration_url,
                       coalesce(event.end_at, event.start_at)
                           <= statement_timestamp() AS is_past
                FROM public.catalog_entity_event_mentions AS mention
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = mention.canonical_event_id
                JOIN public.catalog_sources AS source
                  ON source.source_key = mention.source_key
                JOIN public.catalog_event_observations AS observation
                  ON observation.source_key = mention.source_key
                 AND observation.source = mention.source
                 AND observation.source_event_id = mention.source_event_id
                WHERE mention.entity_id = p_entity_id
                  AND event.event_status <> 'cancelled'
                GROUP BY event.canonical_event_id
            )
            SELECT mentioned.canonical_event_id,
                   mentioned.title,
                   mentioned.start_at,
                   mentioned.end_at,
                   mentioned.venue_name,
                   mentioned.city,
                   mentioned.description,
                   mentioned.roles,
                   mentioned.source_labels,
                   mentioned.registration_url,
                   mentioned.is_past
            FROM mentioned
            ORDER BY mentioned.is_past,
                     CASE WHEN mentioned.is_past THEN NULL ELSE mentioned.start_at END
                         ASC NULLS LAST,
                     CASE WHEN mentioned.is_past THEN mentioned.start_at END
                         DESC NULLS LAST,
                     mentioned.canonical_event_id
            LIMIT p_limit;
        END;
        $$
        """
    )


def _create_insights() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_get_catalog_entity_insights_v1(p_entity_id uuid)
        RETURNS TABLE (
            event_count bigint,
            upcoming_count bigint,
            past_count bigint,
            first_event_at timestamptz,
            last_event_at timestamptz,
            recent_event_count bigint,
            active_months integer,
            events_per_month numeric,
            typical_attendance integer,
            free_count bigint,
            paid_count bigint,
            top_topics text[],
            top_venues text[],
            top_cities text[],
            source_labels text[],
            collaborators jsonb
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_window_start timestamptz := statement_timestamp() - interval '365 days';
            v_window_end timestamptz := statement_timestamp() + interval '90 days';
        BEGIN
            RETURN QUERY
            WITH mentioned AS MATERIALIZED (
                SELECT DISTINCT event.canonical_event_id,
                       event.start_at,
                       event.end_at,
                       event.venue_name,
                       event.city_norm,
                       event.topics,
                       event.price_status,
                       event.attendance_count
                FROM public.catalog_entity_event_mentions AS mention
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = mention.canonical_event_id
                WHERE mention.entity_id = p_entity_id
                  AND event.event_status <> 'cancelled'
            ), totals AS (
                SELECT count(*) AS event_count,
                       count(*) FILTER (
                           WHERE coalesce(end_at, start_at) > statement_timestamp()
                       ) AS upcoming_count,
                       count(*) FILTER (
                           WHERE coalesce(end_at, start_at) <= statement_timestamp()
                       ) AS past_count,
                       min(start_at) AS first_event_at,
                       max(start_at) AS last_event_at,
                       count(*) FILTER (WHERE price_status = 'free') AS free_count,
                       count(*) FILTER (WHERE price_status = 'paid') AS paid_count
                FROM mentioned
            ), cadence AS (
                SELECT count(*) AS recent_event_count,
                       greatest(
                           1,
                           ceil(
                               extract(epoch FROM (max(start_at) - min(start_at)))
                               / 2629746.0
                           )::integer
                       ) AS active_months
                FROM mentioned
                WHERE start_at >= v_window_start
                  AND start_at < v_window_end
            ), attendance AS (
                SELECT round(
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY attendance_count)
                       )::integer AS typical_attendance
                FROM mentioned
                WHERE attendance_count IS NOT NULL
                  AND attendance_count > 0
            ), topics AS (
                SELECT array_agg(topic ORDER BY topic_count DESC, topic) AS values
                FROM (
                    SELECT selected.topic, count(*) AS topic_count
                    FROM mentioned
                    CROSS JOIN LATERAL unnest(mentioned.topics) AS selected(topic)
                    GROUP BY selected.topic
                    ORDER BY count(*) DESC, selected.topic
                    LIMIT 6
                ) AS ranked
            ), venues AS (
                SELECT array_agg(venue_name ORDER BY venue_count DESC, venue_name) AS values
                FROM (
                    SELECT mentioned.venue_name, count(*) AS venue_count
                    FROM mentioned
                    WHERE nullif(btrim(mentioned.venue_name), '') IS NOT NULL
                    GROUP BY mentioned.venue_name
                    ORDER BY count(*) DESC, mentioned.venue_name
                    LIMIT 3
                ) AS ranked
            ), cities AS (
                SELECT array_agg(city_norm ORDER BY city_count DESC, city_norm) AS values
                FROM (
                    SELECT mentioned.city_norm, count(*) AS city_count
                    FROM mentioned
                    WHERE nullif(btrim(mentioned.city_norm), '') IS NOT NULL
                    GROUP BY mentioned.city_norm
                    ORDER BY count(*) DESC, mentioned.city_norm
                    LIMIT 3
                ) AS ranked
            ), sources AS (
                SELECT array_agg(
                           DISTINCT source.display_name ORDER BY source.display_name
                       ) AS values
                FROM public.catalog_entity_event_mentions AS mention
                JOIN public.catalog_sources AS source
                  ON source.source_key = mention.source_key
                WHERE mention.entity_id = p_entity_id
            ), peers AS (
                SELECT jsonb_agg(
                           jsonb_build_object(
                               'entity_id', peer_entity_id,
                               'display_name', display_name,
                               'kind', kind,
                               'shared_event_count', shared_event_count
                           )
                           ORDER BY shared_event_count DESC, display_name
                       ) AS values
                FROM (
                    SELECT other.entity_id AS peer_entity_id,
                           other.display_name,
                           other.kind,
                           count(DISTINCT peer.canonical_event_id) AS shared_event_count
                    FROM public.catalog_entity_event_mentions AS peer
                    JOIN public.catalog_entities AS other
                      ON other.entity_id = peer.entity_id
                    WHERE peer.entity_id <> p_entity_id
                      AND peer.canonical_event_id IN (
                          SELECT canonical_event_id FROM mentioned
                      )
                    GROUP BY other.entity_id, other.display_name, other.kind
                    HAVING count(DISTINCT peer.canonical_event_id) >= 2
                    ORDER BY count(DISTINCT peer.canonical_event_id) DESC, other.display_name
                    LIMIT 8
                ) AS ranked
            )
            SELECT totals.event_count,
                   totals.upcoming_count,
                   totals.past_count,
                   totals.first_event_at,
                   totals.last_event_at,
                   coalesce(cadence.recent_event_count, 0),
                   coalesce(cadence.active_months, 1),
                   CASE
                       WHEN coalesce(cadence.recent_event_count, 0) = 0 THEN NULL
                       ELSE round(
                           cadence.recent_event_count::numeric
                           / greatest(cadence.active_months, 1),
                           1
                       )
                   END,
                   attendance.typical_attendance,
                   totals.free_count,
                   totals.paid_count,
                   coalesce(topics.values, '{}'::text[]),
                   coalesce(venues.values, '{}'::text[]),
                   coalesce(cities.values, '{}'::text[]),
                   coalesce(sources.values, '{}'::text[]),
                   coalesce(peers.values, '[]'::jsonb)
            FROM totals
            CROSS JOIN cadence
            CROSS JOIN attendance
            CROSS JOIN topics
            CROSS JOIN venues
            CROSS JOIN cities
            CROSS JOIN sources
            CROSS JOIN peers;
        END;
        $$
        """
    )
