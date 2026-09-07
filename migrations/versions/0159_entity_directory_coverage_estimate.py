"""Serve the entity directory's coverage denominator without a full sequential scan.

Revision ID: 0159
Revises: 0158
Create Date: 2026-08-26

``fn_get_catalog_entity_directory_v1`` is the entity front door: it runs once on every visit to the
entities view, before anything is on screen.  Measured against the live catalog it cost 22.3 ms
median, and 17.3 ms of that was a single subquery -- ``count(*) FROM canonical_events WHERE
event_status <> 'cancelled'`` -- which plans as a sequential scan over 39,563 rows (6,579 heap
fetches) to produce one number for a coverage line that reads "entity data for 4.0% of events".
Every other aggregate in the function is sub-millisecond: the entity totals are 0.61 ms and the
distinct-event count over mentions is 0.46 ms.

The denominator is a disclosure, not an accounting figure, so it is now estimated from
``pg_class.reltuples`` and corrected by the exact cancelled count.  Measured error of the estimate
against the true count is 0.344% (39,425 vs 39,561) with autoanalyze current, which cannot move a
figure rendered to one decimal place -- 4.042% and 4.028% both render as 4.0%.

Three guards keep the estimate honest rather than merely fast:

* ``nullif(reltuples, -1)`` detects a table that has never been analyzed -- PostgreSQL stores -1
  there rather than 0 -- and ``coalesce`` falls back to the exact count in that case, so a freshly
  restored database reports a true number instead of a negative one.
* The cancelled count stays exact.  It is backed by a partial index that indexes only cancelled
  rows, so it is an index-only scan over an empty relation today (the catalog holds 39,563 rows and
  all of them are ``scheduled``) and stays proportional to the cancelled set rather than the table
  if that ever changes.
* ``GREATEST(..., events_with_entities)`` floors the denominator at the numerator, so a stale or
  understated estimate can never render coverage above 100%.

Nothing else in the function changes; the returned jsonb shape is identical.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0159"
down_revision: str | None = "0158"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIRECTORY = "public.fn_get_catalog_entity_directory_v1(text,text[],text[],integer,integer)"
_CANCELLED_INDEX = "ix_canonical_events_cancelled"


def upgrade() -> None:
    # Indexes only the cancelled rows, so the exact correction term stays an index-only scan
    # proportional to the cancelled set rather than to the 39,563-row table.
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_CANCELLED_INDEX}
        ON public.canonical_events (canonical_event_id)
        WHERE event_status = 'cancelled'
        """
    )
    op.execute(_DIRECTORY_ESTIMATED)
    op.execute(f"REVOKE ALL ON FUNCTION {_DIRECTORY} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_DIRECTORY} TO ec_app")


def downgrade() -> None:
    op.execute(_DIRECTORY_EXACT)
    op.execute(f"REVOKE ALL ON FUNCTION {_DIRECTORY} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_DIRECTORY} TO ec_app")
    op.execute(f"DROP INDEX IF EXISTS public.{_CANCELLED_INDEX}")


_DIRECTORY_ESTIMATED = r"""
CREATE OR REPLACE FUNCTION public.fn_get_catalog_entity_directory_v1(p_query text, p_kinds text[], p_city_norms text[], p_limit integer, p_min_events integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $$
        DECLARE
            v_result jsonb;
        BEGIN
            IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 60
               OR p_min_events IS NULL OR p_min_events NOT BETWEEN 1 AND 10
               OR length(coalesce(p_query, '')) > 160
               OR p_query ~ '[\x00-\x1f\x7f]'
               OR cardinality(coalesce(p_kinds, '{}'::text[])) > 3
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_kinds, '{}'::text[])) AS kind(value)
                   WHERE kind.value NOT IN ('person', 'organization', 'unknown')
               )
               OR cardinality(coalesce(p_city_norms, '{}'::text[])) > 8
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_city_norms, '{}'::text[])) AS city(value)
                   WHERE length(city.value) > 64 OR city.value ~ '[\x00-\x1f\x7f]'
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity directory request is invalid';
            END IF;

            WITH scoped AS MATERIALIZED (
                SELECT entity.entity_id,
                       entity.display_name,
                       entity.kind,
                       entity.identity_status,
                       entity.canonical_profile_url,
                       entity.profile_key,
                       entity.normalized_name,
                       count(DISTINCT mention.canonical_event_id) AS event_count,
                       count(DISTINCT mention.canonical_event_id)
                           FILTER (WHERE coalesce(event.end_at, event.start_at)
                                         > statement_timestamp()) AS upcoming_count,
                       count(DISTINCT mention.source_key) AS source_count,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                       max(event.start_at) AS last_event_at,
                       (array_agg(event.city_norm
                           ORDER BY event.start_at DESC NULLS LAST))[1] AS top_city
                  FROM public.catalog_entities AS entity
                  JOIN public.catalog_entity_event_mentions AS mention
                    ON mention.entity_id = entity.entity_id
                  JOIN public.canonical_events AS event
                    ON event.canonical_event_id = mention.canonical_event_id
                 WHERE event.event_status <> 'cancelled'
                   AND (cardinality(coalesce(p_kinds, '{}'::text[])) = 0
                        OR entity.kind = ANY(p_kinds))
                   AND (cardinality(coalesce(p_city_norms, '{}'::text[])) = 0
                        OR event.city_norm = ANY(p_city_norms))
                   AND (nullif(btrim(p_query), '') IS NULL
                        OR entity.search_document @@ plainto_tsquery('simple', p_query)
                        OR entity.normalized_name LIKE '%' || lower(btrim(p_query)) || '%')
                 GROUP BY entity.entity_id
            ), ranked AS (
                SELECT * FROM scoped
                 WHERE event_count >= p_min_events
                 ORDER BY event_count DESC, upcoming_count DESC, display_name, entity_id
                 LIMIT p_limit
            ), peer_counts AS (
                -- Bounded to the <= 60 ranked rows: one small lookup each, never a whole-graph
                -- self-join.
                SELECT ranked.entity_id,
                       (SELECT count(DISTINCT other.entity_id)
                          FROM public.catalog_entity_event_mentions AS mine
                          JOIN public.catalog_entity_event_mentions AS other
                            ON other.canonical_event_id = mine.canonical_event_id
                           AND other.entity_id <> mine.entity_id
                         WHERE mine.entity_id = ranked.entity_id) AS peer_count
                  FROM ranked
            ), totals AS (
                SELECT count(*) AS entity_count,
                       count(*) FILTER (WHERE kind = 'person') AS person_count,
                       count(*) FILTER (WHERE kind = 'organization') AS organization_count,
                       count(*) FILTER (WHERE kind = 'unknown') AS unknown_count,
                       count(*) FILTER (
                           WHERE identity_status = 'profile_verified'
                       ) AS verified_count,
                       count(*) FILTER (
                           WHERE identity_status = 'source_scoped'
                       ) AS scoped_count
                  FROM public.catalog_entities
            ), coverage AS (
                SELECT (SELECT count(DISTINCT canonical_event_id)
                          FROM public.catalog_entity_event_mentions) AS events_with_entities,
                       GREATEST(
                           coalesce(
                               nullif(
                                   (SELECT class.reltuples::bigint
                                      FROM pg_catalog.pg_class AS class
                                     WHERE class.oid
                                           = 'public.canonical_events'::regclass),
                                   -1
                               ),
                               (SELECT count(*) FROM public.canonical_events)
                           )
                           - (SELECT count(*) FROM public.canonical_events
                               WHERE event_status = 'cancelled'),
                           (SELECT count(DISTINCT canonical_event_id)
                              FROM public.catalog_entity_event_mentions)
                       ) AS events_total,
                       (SELECT count(*)
                          FROM public.catalog_entity_event_mentions) AS mention_count
            )
            SELECT jsonb_build_object(
                'generated_at', statement_timestamp(),
                'totals', (SELECT to_jsonb(totals) FROM totals),
                'coverage', (SELECT to_jsonb(coverage) FROM coverage),
                'matched', (SELECT count(*) FROM scoped WHERE event_count >= p_min_events),
                'hubs', (SELECT coalesce(jsonb_agg(jsonb_build_object(
                            'entity_id', ranked.entity_id,
                            'display_name', ranked.display_name,
                            'kind', ranked.kind,
                            'identity_status', ranked.identity_status,
                            'profile_url', ranked.canonical_profile_url,
                            'profile_key', ranked.profile_key,
                            'event_count', ranked.event_count,
                            'upcoming_count', ranked.upcoming_count,
                            'peer_count', peer_counts.peer_count,
                            'source_count', ranked.source_count,
                            'roles', to_jsonb(ranked.roles),
                            'top_city', ranked.top_city,
                            'last_event_at', ranked.last_event_at
                        ) ORDER BY ranked.event_count DESC, ranked.upcoming_count DESC,
                                   ranked.display_name, ranked.entity_id), '[]'::jsonb)
                   FROM ranked LEFT JOIN peer_counts USING (entity_id))
            ) INTO v_result;

            RETURN v_result;
        END;
        $$
"""


_DIRECTORY_EXACT = r"""
CREATE OR REPLACE FUNCTION public.fn_get_catalog_entity_directory_v1(p_query text, p_kinds text[], p_city_norms text[], p_limit integer, p_min_events integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $$
        DECLARE
            v_result jsonb;
        BEGIN
            IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 60
               OR p_min_events IS NULL OR p_min_events NOT BETWEEN 1 AND 10
               OR length(coalesce(p_query, '')) > 160
               OR p_query ~ '[\x00-\x1f\x7f]'
               OR cardinality(coalesce(p_kinds, '{}'::text[])) > 3
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_kinds, '{}'::text[])) AS kind(value)
                   WHERE kind.value NOT IN ('person', 'organization', 'unknown')
               )
               OR cardinality(coalesce(p_city_norms, '{}'::text[])) > 8
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_city_norms, '{}'::text[])) AS city(value)
                   WHERE length(city.value) > 64 OR city.value ~ '[\x00-\x1f\x7f]'
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity directory request is invalid';
            END IF;

            WITH scoped AS MATERIALIZED (
                SELECT entity.entity_id,
                       entity.display_name,
                       entity.kind,
                       entity.identity_status,
                       entity.canonical_profile_url,
                       entity.profile_key,
                       entity.normalized_name,
                       count(DISTINCT mention.canonical_event_id) AS event_count,
                       count(DISTINCT mention.canonical_event_id)
                           FILTER (WHERE coalesce(event.end_at, event.start_at)
                                         > statement_timestamp()) AS upcoming_count,
                       count(DISTINCT mention.source_key) AS source_count,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                       max(event.start_at) AS last_event_at,
                       (array_agg(event.city_norm
                           ORDER BY event.start_at DESC NULLS LAST))[1] AS top_city
                  FROM public.catalog_entities AS entity
                  JOIN public.catalog_entity_event_mentions AS mention
                    ON mention.entity_id = entity.entity_id
                  JOIN public.canonical_events AS event
                    ON event.canonical_event_id = mention.canonical_event_id
                 WHERE event.event_status <> 'cancelled'
                   AND (cardinality(coalesce(p_kinds, '{}'::text[])) = 0
                        OR entity.kind = ANY(p_kinds))
                   AND (cardinality(coalesce(p_city_norms, '{}'::text[])) = 0
                        OR event.city_norm = ANY(p_city_norms))
                   AND (nullif(btrim(p_query), '') IS NULL
                        OR entity.search_document @@ plainto_tsquery('simple', p_query)
                        OR entity.normalized_name LIKE '%' || lower(btrim(p_query)) || '%')
                 GROUP BY entity.entity_id
            ), ranked AS (
                SELECT * FROM scoped
                 WHERE event_count >= p_min_events
                 ORDER BY event_count DESC, upcoming_count DESC, display_name, entity_id
                 LIMIT p_limit
            ), peer_counts AS (
                -- Bounded to the <= 60 ranked rows: one small lookup each, never a whole-graph
                -- self-join.
                SELECT ranked.entity_id,
                       (SELECT count(DISTINCT other.entity_id)
                          FROM public.catalog_entity_event_mentions AS mine
                          JOIN public.catalog_entity_event_mentions AS other
                            ON other.canonical_event_id = mine.canonical_event_id
                           AND other.entity_id <> mine.entity_id
                         WHERE mine.entity_id = ranked.entity_id) AS peer_count
                  FROM ranked
            ), totals AS (
                SELECT count(*) AS entity_count,
                       count(*) FILTER (WHERE kind = 'person') AS person_count,
                       count(*) FILTER (WHERE kind = 'organization') AS organization_count,
                       count(*) FILTER (WHERE kind = 'unknown') AS unknown_count,
                       count(*) FILTER (
                           WHERE identity_status = 'profile_verified'
                       ) AS verified_count,
                       count(*) FILTER (
                           WHERE identity_status = 'source_scoped'
                       ) AS scoped_count
                  FROM public.catalog_entities
            ), coverage AS (
                SELECT (SELECT count(DISTINCT canonical_event_id)
                          FROM public.catalog_entity_event_mentions) AS events_with_entities,
                       (SELECT count(*) FROM public.canonical_events
                         WHERE event_status <> 'cancelled') AS events_total,
                       (SELECT count(*)
                          FROM public.catalog_entity_event_mentions) AS mention_count
            )
            SELECT jsonb_build_object(
                'generated_at', statement_timestamp(),
                'totals', (SELECT to_jsonb(totals) FROM totals),
                'coverage', (SELECT to_jsonb(coverage) FROM coverage),
                'matched', (SELECT count(*) FROM scoped WHERE event_count >= p_min_events),
                'hubs', (SELECT coalesce(jsonb_agg(jsonb_build_object(
                            'entity_id', ranked.entity_id,
                            'display_name', ranked.display_name,
                            'kind', ranked.kind,
                            'identity_status', ranked.identity_status,
                            'profile_url', ranked.canonical_profile_url,
                            'profile_key', ranked.profile_key,
                            'event_count', ranked.event_count,
                            'upcoming_count', ranked.upcoming_count,
                            'peer_count', peer_counts.peer_count,
                            'source_count', ranked.source_count,
                            'roles', to_jsonb(ranked.roles),
                            'top_city', ranked.top_city,
                            'last_event_at', ranked.last_event_at
                        ) ORDER BY ranked.event_count DESC, ranked.upcoming_count DESC,
                                   ranked.display_name, ranked.entity_id), '[]'::jsonb)
                   FROM ranked LEFT JOIN peer_counts USING (entity_id))
            ) INTO v_result;

            RETURN v_result;
        END;
        $$
"""
