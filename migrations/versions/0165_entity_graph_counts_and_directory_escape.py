"""Repair two capability bodies that the live database and the migration tree disagree about.

Revision ID: 0165
Revises: 0164
Create Date: 2026-08-26

Nothing here changes what the entity explorer is allowed to read.  Both statements below are
``CREATE OR REPLACE`` of a function that already exists, and both restore the body that the
migration tree already claims is installed.

**1. ``fn_get_catalog_entity_graph_v1`` — ``counts.edges`` names the wrong quantity.**

``0156`` installs the function with a bare ``CREATE FUNCTION``, so its body is fixed at the moment
it is stamped and a later edit to that file can never reach a database that has already run it.
The applied body emits::

    'edges', (SELECT count(*) FROM edges)

while the emitted ``edges`` array is the mention edges ``UNION ALL`` one entity->topic edge per
topic node.  Measured on the live catalog for entity ``98744dc7-e452-42b6-a9f6-648b02a6e734``:
``counts.edges = 31`` and ``counts.topics = 5`` against an array of 36 elements.  A legend or a
truncation banner reading ``counts.edges`` therefore understates the drawn graph by exactly the
topic count, for every entity that carries at least one topic -- which is nearly all of them.

Worse, the shape was environment-dependent: a database built fresh from the migration tree got
``mention_edges`` and ``edges = 36``, a database that had already run ``0156`` did not.  This
revision makes both answer the same way: ``edges`` counts the emitted array, and ``mention_edges``
is the capped mention subset that ``edges_total`` and ``truncated.edges`` compare against.

**2. ``fn_get_catalog_entity_directory_v1`` — LIKE metacharacters reach the pattern unescaped.**

``0156`` escapes them.  ``0159`` then ``CREATE OR REPLACE``\ s the whole function to serve the
coverage denominator from ``pg_class.reltuples``, and its copy of the body carries the *unescaped*
branch, which is what is installed today.  Measured on the live catalog: a query of ``%`` returns
``matched = 3036`` -- the entire catalog, presented as search results -- and ``_____`` returns
2958, i.e. "any name at least five characters long".  ``zzzznotathing`` correctly returns 0, so the
filter is otherwise working; the tsquery branch cannot mask the hole because
``plainto_tsquery('simple','%')`` is an empty query.

This revision reinstalls ``0159``'s estimator body with ``0156``'s escaped branch.  The returned
jsonb shape is unchanged, and ``0159``'s three estimator guards are preserved verbatim.

``downgrade()`` puts both bodies back exactly as they are installed today, so the revision is
reversible without asserting that either older body was correct.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0165"
down_revision: str | None = "0164"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GRAPH = "public.fn_get_catalog_entity_graph_v1(uuid,integer,integer,integer)"
_DIRECTORY = "public.fn_get_catalog_entity_directory_v1(text,text[],text[],integer,integer)"


def upgrade() -> None:
    op.execute(_GRAPH_REPAIRED)
    op.execute(_DIRECTORY_REPAIRED)
    _regrant()


def downgrade() -> None:
    op.execute(_GRAPH_AS_INSTALLED_BEFORE_0165)
    op.execute(_DIRECTORY_AS_INSTALLED_BEFORE_0165)
    _regrant()


def _regrant() -> None:
    """``CREATE OR REPLACE`` preserves privileges, but restating them keeps the grant auditable."""
    for signature in (_GRAPH, _DIRECTORY):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


_GRAPH_REPAIRED = r"""
        CREATE OR REPLACE FUNCTION public.fn_get_catalog_entity_graph_v1(
            p_entity_id uuid,
            p_event_limit integer,
            p_peer_limit integer,
            p_topic_limit integer
        )
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_ego record;
            v_result jsonb;
        BEGIN
            -- Validation runs before any scan, in the idiom of fn_list_catalog_entity_events_v2.
            IF p_entity_id IS NULL
               OR p_event_limit IS NULL OR p_event_limit NOT BETWEEN 1 AND 24
               OR p_peer_limit IS NULL OR p_peer_limit NOT BETWEEN 1 AND 48
               OR p_topic_limit IS NULL OR p_topic_limit NOT BETWEEN 0 AND 6
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity graph request is invalid';
            END IF;

            SELECT entity.entity_id, entity.display_name, entity.kind, entity.identity_status,
                   entity.canonical_profile_url, entity.profile_key, entity.normalized_name
              INTO v_ego
              FROM public.catalog_entities AS entity
             WHERE entity.entity_id = p_entity_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;

            WITH ego_events AS MATERIALIZED (
                SELECT event.canonical_event_id,
                       event.title,
                       event.start_at,
                       event.end_at,
                       event.venue_name,
                       event.city_norm,
                       event.topics,
                       event.price_status,
                       coalesce(event.end_at, event.start_at) <= statement_timestamp() AS is_past,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS ego_roles
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.canonical_events AS event
                    ON event.canonical_event_id = mention.canonical_event_id
                 WHERE mention.entity_id = p_entity_id
                   AND event.event_status <> 'cancelled'
                 GROUP BY event.canonical_event_id
            ), event_totals AS (
                SELECT count(*) AS matched FROM ego_events
            ), events AS (
                -- Same order as fn_list_catalog_entity_events_v2: upcoming ascending, then past
                -- descending, so history can never crowd out an upcoming appearance.
                SELECT ego_events.*
                  FROM ego_events
                 ORDER BY is_past,
                          CASE WHEN is_past THEN NULL ELSE start_at END ASC NULLS LAST,
                          CASE WHEN is_past THEN start_at END DESC NULLS LAST,
                          canonical_event_id
                 LIMIT p_event_limit
            ), event_registration AS (
                SELECT mention.canonical_event_id,
                       min(observation.registration_url) AS registration_url
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.catalog_event_observations AS observation
                    ON observation.source_key = mention.source_key
                   AND observation.source = mention.source
                   AND observation.source_event_id = mention.source_event_id
                 WHERE mention.canonical_event_id IN (SELECT canonical_event_id FROM events)
                 GROUP BY mention.canonical_event_id
            ), event_degree AS (
                SELECT mention.canonical_event_id,
                       count(DISTINCT mention.entity_id) AS entity_count
                  FROM public.catalog_entity_event_mentions AS mention
                 WHERE mention.canonical_event_id IN (SELECT canonical_event_id FROM events)
                 GROUP BY mention.canonical_event_id
            ), peer_ranked AS (
                -- Peers at shared_event_count >= 1.  Admissible here in a way it is not in a
                -- co-mention projection, because the event that connects them is itself a drawn
                -- node carrying the role and the source: the tie is evidence, not a weight.
                SELECT other.entity_id,
                       other.display_name,
                       other.kind,
                       other.identity_status,
                       other.canonical_profile_url,
                       other.profile_key,
                       count(DISTINCT mention.canonical_event_id) AS shared_event_count,
                       count(DISTINCT mention.canonical_event_id)
                           FILTER (WHERE NOT events.is_past) AS shared_upcoming_count
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN events
                    ON events.canonical_event_id = mention.canonical_event_id
                  JOIN public.catalog_entities AS other
                    ON other.entity_id = mention.entity_id
                 WHERE mention.entity_id <> p_entity_id
                 GROUP BY other.entity_id
            ), peer_totals AS (
                SELECT count(*) AS matched FROM peer_ranked
            ), peers AS (
                SELECT * FROM peer_ranked
                 ORDER BY shared_event_count DESC, shared_upcoming_count DESC,
                          display_name, entity_id
                 LIMIT p_peer_limit
            ), degrees AS (
                -- One global degree per drawn entity: "17 events" is a property of the organizer,
                -- not of this frame.  Node radius reads off this and nothing else.
                SELECT mention.entity_id,
                       count(DISTINCT mention.canonical_event_id) AS degree
                  FROM public.catalog_entity_event_mentions AS mention
                 WHERE mention.entity_id = p_entity_id
                    OR mention.entity_id IN (SELECT entity_id FROM peers)
                 GROUP BY mention.entity_id
            ), topics AS (
                SELECT topic.value AS topic, count(*) AS event_count
                  FROM events
                  CROSS JOIN LATERAL unnest(events.topics) AS topic(value)
                 GROUP BY topic.value
                 ORDER BY count(*) DESC, topic.value
                 LIMIT p_topic_limit
            ), edges_raw AS (
                -- One edge per (entity, event); roles and sources are aggregated onto it, so a
                -- co-host pair is two 'host' edges into one event node rather than a synthesized
                -- entity-to-entity relation.
                SELECT mention.entity_id,
                       mention.canonical_event_id,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                       array_agg(DISTINCT source.display_name ORDER BY source.display_name)
                           AS source_labels,
                       max(mention.observed_at) AS observed_at
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.catalog_sources AS source
                    ON source.source_key = mention.source_key
                 WHERE mention.canonical_event_id IN (SELECT canonical_event_id FROM events)
                   AND (mention.entity_id = p_entity_id
                        OR mention.entity_id IN (SELECT entity_id FROM peers))
                 GROUP BY mention.entity_id, mention.canonical_event_id
            ), edge_totals AS (
                SELECT count(*) AS matched FROM edges_raw
            ), edges AS (
                SELECT * FROM edges_raw
                 ORDER BY entity_id, canonical_event_id
                 LIMIT 600
            ), same_name AS (
                -- Review candidates.  EXACT normalized_name only: normalized_name is pure
                -- whitespace/case folding (0147).  No token, prefix or fuzzy matching is offered,
                -- because a near-name match would make a name into an identity judgment.
                -- Surfaced, never merged, never joined.
                SELECT other.entity_id, other.display_name, other.kind, other.identity_status,
                       count(DISTINCT mention.canonical_event_id) AS event_count
                  FROM public.catalog_entities AS other
                  JOIN public.catalog_entity_event_mentions AS mention
                    ON mention.entity_id = other.entity_id
                 WHERE other.normalized_name = v_ego.normalized_name
                   AND other.entity_id <> p_entity_id
                 GROUP BY other.entity_id
                 ORDER BY count(DISTINCT mention.canonical_event_id) DESC, other.display_name
                 LIMIT 5
            )
            SELECT jsonb_build_object(
                'focus_id', 'entity:' || p_entity_id::text,
                'generated_at', statement_timestamp(),
                'counts', jsonb_build_object(
                    'events', (SELECT count(*) FROM events),
                    'events_total', (SELECT matched FROM event_totals),
                    'peers', (SELECT count(*) FROM peers),
                    'peers_total', (SELECT matched FROM peer_totals),
                    'topics', (SELECT count(*) FROM topics),
                    -- 'edges' counts the emitted array, which is mention edges UNION ALL one
                    -- entity->topic edge per topic node; a name that does not match its quantity
                    -- is the same silent lie as an unreported truncation.  'mention_edges' is the
                    -- capped subset, and it is the one 'edges_total' and truncated.edges compare
                    -- against, because topic edges are derived from the already-capped event set
                    -- and are never truncated.
                    'edges', (SELECT count(*) FROM edges) + (SELECT count(*) FROM topics),
                    'mention_edges', (SELECT count(*) FROM edges),
                    'edges_total', (SELECT matched FROM edge_totals)
                ),
                'truncated', jsonb_build_object(
                    'events', (SELECT matched FROM event_totals) > (SELECT count(*) FROM events),
                    'peers', (SELECT matched FROM peer_totals) > (SELECT count(*) FROM peers),
                    'edges', (SELECT matched FROM edge_totals) > (SELECT count(*) FROM edges)
                ),
                'nodes',
                (SELECT coalesce(jsonb_agg(node ORDER BY node ->> 'node_id'), '[]'::jsonb) FROM (
                    SELECT jsonb_build_object(
                        'node_id', 'entity:' || v_ego.entity_id::text,
                        'node_kind', 'entity',
                        'ring', 0,
                        'label', v_ego.display_name,
                        'entity_id', v_ego.entity_id,
                        'entity_kind', v_ego.kind,
                        'identity_status', v_ego.identity_status,
                        'profile_url', v_ego.canonical_profile_url,
                        'profile_key', v_ego.profile_key,
                        'degree', (SELECT degree FROM degrees
                                    WHERE degrees.entity_id = v_ego.entity_id),
                        'shared_event_count', NULL,
                        'roles', (SELECT array_agg(DISTINCT mention.role ORDER BY mention.role)
                                    FROM public.catalog_entity_event_mentions AS mention
                                   WHERE mention.entity_id = p_entity_id)
                    ) AS node
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'event:' || events.canonical_event_id::text,
                        'node_kind', 'event',
                        'ring', 1,
                        'label', events.title,
                        'canonical_event_id', events.canonical_event_id,
                        'start_at', events.start_at,
                        'end_at', events.end_at,
                        'is_past', events.is_past,
                        'venue_name', events.venue_name,
                        'city', events.city_norm,
                        'price_status', events.price_status,
                        'topics', to_jsonb(coalesce(events.topics, '{}'::text[])),
                        'ego_roles', to_jsonb(events.ego_roles),
                        'registration_url', event_registration.registration_url,
                        'degree', event_degree.entity_count
                    )
                      FROM events
                      LEFT JOIN event_registration USING (canonical_event_id)
                      LEFT JOIN event_degree USING (canonical_event_id)
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'entity:' || peers.entity_id::text,
                        'node_kind', 'entity',
                        'ring', 2,
                        'label', peers.display_name,
                        'entity_id', peers.entity_id,
                        'entity_kind', peers.kind,
                        'identity_status', peers.identity_status,
                        'profile_url', peers.canonical_profile_url,
                        'profile_key', peers.profile_key,
                        'degree', degrees.degree,
                        'shared_event_count', peers.shared_event_count
                    )
                      FROM peers LEFT JOIN degrees USING (entity_id)
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'topic:' || topics.topic,
                        'node_kind', 'topic',
                        'ring', 3,
                        'label', topics.topic,
                        'degree', topics.event_count
                    )
                      FROM topics
                ) AS assembled),
                'edges',
                (SELECT coalesce(jsonb_agg(edge), '[]'::jsonb) FROM (
                    SELECT jsonb_build_object(
                        'a', 'entity:' || edges.entity_id::text,
                        'b', 'event:' || edges.canonical_event_id::text,
                        'kind', 'mention',
                        'roles', to_jsonb(edges.roles),
                        'source_labels', to_jsonb(edges.source_labels),
                        'observed_at', edges.observed_at
                    ) AS edge
                      FROM edges
                    UNION ALL
                    SELECT jsonb_build_object(
                        'a', 'entity:' || p_entity_id::text,
                        'b', 'topic:' || topics.topic,
                        'kind', 'topic',
                        'roles', '[]'::jsonb,
                        'source_labels', '[]'::jsonb,
                        'observed_at', NULL
                    )
                      FROM topics
                ) AS assembled),
                'same_name_candidates',
                (SELECT coalesce(jsonb_agg(jsonb_build_object(
                            'entity_id', same_name.entity_id,
                            'display_name', same_name.display_name,
                            'kind', same_name.kind,
                            'identity_status', same_name.identity_status,
                            'event_count', same_name.event_count
                        ) ORDER BY same_name.event_count DESC, same_name.display_name), '[]'::jsonb)
                   FROM same_name)
            ) INTO v_result;

            RETURN v_result;
        END;
        $$
        
"""


_GRAPH_AS_INSTALLED_BEFORE_0165 = r"""
        CREATE OR REPLACE FUNCTION public.fn_get_catalog_entity_graph_v1(
            p_entity_id uuid,
            p_event_limit integer,
            p_peer_limit integer,
            p_topic_limit integer
        )
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_ego record;
            v_result jsonb;
        BEGIN
            -- Validation runs before any scan, in the idiom of fn_list_catalog_entity_events_v2.
            IF p_entity_id IS NULL
               OR p_event_limit IS NULL OR p_event_limit NOT BETWEEN 1 AND 24
               OR p_peer_limit IS NULL OR p_peer_limit NOT BETWEEN 1 AND 48
               OR p_topic_limit IS NULL OR p_topic_limit NOT BETWEEN 0 AND 6
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity graph request is invalid';
            END IF;

            SELECT entity.entity_id, entity.display_name, entity.kind, entity.identity_status,
                   entity.canonical_profile_url, entity.profile_key, entity.normalized_name
              INTO v_ego
              FROM public.catalog_entities AS entity
             WHERE entity.entity_id = p_entity_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;

            WITH ego_events AS MATERIALIZED (
                SELECT event.canonical_event_id,
                       event.title,
                       event.start_at,
                       event.end_at,
                       event.venue_name,
                       event.city_norm,
                       event.topics,
                       event.price_status,
                       coalesce(event.end_at, event.start_at) <= statement_timestamp() AS is_past,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS ego_roles
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.canonical_events AS event
                    ON event.canonical_event_id = mention.canonical_event_id
                 WHERE mention.entity_id = p_entity_id
                   AND event.event_status <> 'cancelled'
                 GROUP BY event.canonical_event_id
            ), event_totals AS (
                SELECT count(*) AS matched FROM ego_events
            ), events AS (
                -- Same order as fn_list_catalog_entity_events_v2: upcoming ascending, then past
                -- descending, so history can never crowd out an upcoming appearance.
                SELECT ego_events.*
                  FROM ego_events
                 ORDER BY is_past,
                          CASE WHEN is_past THEN NULL ELSE start_at END ASC NULLS LAST,
                          CASE WHEN is_past THEN start_at END DESC NULLS LAST,
                          canonical_event_id
                 LIMIT p_event_limit
            ), event_registration AS (
                SELECT mention.canonical_event_id,
                       min(observation.registration_url) AS registration_url
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.catalog_event_observations AS observation
                    ON observation.source_key = mention.source_key
                   AND observation.source = mention.source
                   AND observation.source_event_id = mention.source_event_id
                 WHERE mention.canonical_event_id IN (SELECT canonical_event_id FROM events)
                 GROUP BY mention.canonical_event_id
            ), event_degree AS (
                SELECT mention.canonical_event_id,
                       count(DISTINCT mention.entity_id) AS entity_count
                  FROM public.catalog_entity_event_mentions AS mention
                 WHERE mention.canonical_event_id IN (SELECT canonical_event_id FROM events)
                 GROUP BY mention.canonical_event_id
            ), peer_ranked AS (
                -- Peers at shared_event_count >= 1.  Admissible here in a way it is not in a
                -- co-mention projection, because the event that connects them is itself a drawn
                -- node carrying the role and the source: the tie is evidence, not a weight.
                SELECT other.entity_id,
                       other.display_name,
                       other.kind,
                       other.identity_status,
                       other.canonical_profile_url,
                       other.profile_key,
                       count(DISTINCT mention.canonical_event_id) AS shared_event_count,
                       count(DISTINCT mention.canonical_event_id)
                           FILTER (WHERE NOT events.is_past) AS shared_upcoming_count
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN events
                    ON events.canonical_event_id = mention.canonical_event_id
                  JOIN public.catalog_entities AS other
                    ON other.entity_id = mention.entity_id
                 WHERE mention.entity_id <> p_entity_id
                 GROUP BY other.entity_id
            ), peer_totals AS (
                SELECT count(*) AS matched FROM peer_ranked
            ), peers AS (
                SELECT * FROM peer_ranked
                 ORDER BY shared_event_count DESC, shared_upcoming_count DESC,
                          display_name, entity_id
                 LIMIT p_peer_limit
            ), degrees AS (
                -- One global degree per drawn entity: "17 events" is a property of the organizer,
                -- not of this frame.  Node radius reads off this and nothing else.
                SELECT mention.entity_id,
                       count(DISTINCT mention.canonical_event_id) AS degree
                  FROM public.catalog_entity_event_mentions AS mention
                 WHERE mention.entity_id = p_entity_id
                    OR mention.entity_id IN (SELECT entity_id FROM peers)
                 GROUP BY mention.entity_id
            ), topics AS (
                SELECT topic.value AS topic, count(*) AS event_count
                  FROM events
                  CROSS JOIN LATERAL unnest(events.topics) AS topic(value)
                 GROUP BY topic.value
                 ORDER BY count(*) DESC, topic.value
                 LIMIT p_topic_limit
            ), edges_raw AS (
                -- One edge per (entity, event); roles and sources are aggregated onto it, so a
                -- co-host pair is two 'host' edges into one event node rather than a synthesized
                -- entity-to-entity relation.
                SELECT mention.entity_id,
                       mention.canonical_event_id,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                       array_agg(DISTINCT source.display_name ORDER BY source.display_name)
                           AS source_labels,
                       max(mention.observed_at) AS observed_at
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.catalog_sources AS source
                    ON source.source_key = mention.source_key
                 WHERE mention.canonical_event_id IN (SELECT canonical_event_id FROM events)
                   AND (mention.entity_id = p_entity_id
                        OR mention.entity_id IN (SELECT entity_id FROM peers))
                 GROUP BY mention.entity_id, mention.canonical_event_id
            ), edge_totals AS (
                SELECT count(*) AS matched FROM edges_raw
            ), edges AS (
                SELECT * FROM edges_raw
                 ORDER BY entity_id, canonical_event_id
                 LIMIT 600
            ), same_name AS (
                -- Review candidates.  EXACT normalized_name only: normalized_name is pure
                -- whitespace/case folding (0147).  No token, prefix or fuzzy matching is offered,
                -- because a near-name match would make a name into an identity judgment.
                -- Surfaced, never merged, never joined.
                SELECT other.entity_id, other.display_name, other.kind, other.identity_status,
                       count(DISTINCT mention.canonical_event_id) AS event_count
                  FROM public.catalog_entities AS other
                  JOIN public.catalog_entity_event_mentions AS mention
                    ON mention.entity_id = other.entity_id
                 WHERE other.normalized_name = v_ego.normalized_name
                   AND other.entity_id <> p_entity_id
                 GROUP BY other.entity_id
                 ORDER BY count(DISTINCT mention.canonical_event_id) DESC, other.display_name
                 LIMIT 5
            )
            SELECT jsonb_build_object(
                'focus_id', 'entity:' || p_entity_id::text,
                'generated_at', statement_timestamp(),
                'counts', jsonb_build_object(
                    'events', (SELECT count(*) FROM events),
                    'events_total', (SELECT matched FROM event_totals),
                    'peers', (SELECT count(*) FROM peers),
                    'peers_total', (SELECT matched FROM peer_totals),
                    'topics', (SELECT count(*) FROM topics),
                    'edges', (SELECT count(*) FROM edges),
                    'edges_total', (SELECT matched FROM edge_totals)
                ),
                'truncated', jsonb_build_object(
                    'events', (SELECT matched FROM event_totals) > (SELECT count(*) FROM events),
                    'peers', (SELECT matched FROM peer_totals) > (SELECT count(*) FROM peers),
                    'edges', (SELECT matched FROM edge_totals) > (SELECT count(*) FROM edges)
                ),
                'nodes',
                (SELECT coalesce(jsonb_agg(node ORDER BY node ->> 'node_id'), '[]'::jsonb) FROM (
                    SELECT jsonb_build_object(
                        'node_id', 'entity:' || v_ego.entity_id::text,
                        'node_kind', 'entity',
                        'ring', 0,
                        'label', v_ego.display_name,
                        'entity_id', v_ego.entity_id,
                        'entity_kind', v_ego.kind,
                        'identity_status', v_ego.identity_status,
                        'profile_url', v_ego.canonical_profile_url,
                        'profile_key', v_ego.profile_key,
                        'degree', (SELECT degree FROM degrees
                                    WHERE degrees.entity_id = v_ego.entity_id),
                        'shared_event_count', NULL,
                        'roles', (SELECT array_agg(DISTINCT mention.role ORDER BY mention.role)
                                    FROM public.catalog_entity_event_mentions AS mention
                                   WHERE mention.entity_id = p_entity_id)
                    ) AS node
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'event:' || events.canonical_event_id::text,
                        'node_kind', 'event',
                        'ring', 1,
                        'label', events.title,
                        'canonical_event_id', events.canonical_event_id,
                        'start_at', events.start_at,
                        'end_at', events.end_at,
                        'is_past', events.is_past,
                        'venue_name', events.venue_name,
                        'city', events.city_norm,
                        'price_status', events.price_status,
                        'topics', to_jsonb(coalesce(events.topics, '{}'::text[])),
                        'ego_roles', to_jsonb(events.ego_roles),
                        'registration_url', event_registration.registration_url,
                        'degree', event_degree.entity_count
                    )
                      FROM events
                      LEFT JOIN event_registration USING (canonical_event_id)
                      LEFT JOIN event_degree USING (canonical_event_id)
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'entity:' || peers.entity_id::text,
                        'node_kind', 'entity',
                        'ring', 2,
                        'label', peers.display_name,
                        'entity_id', peers.entity_id,
                        'entity_kind', peers.kind,
                        'identity_status', peers.identity_status,
                        'profile_url', peers.canonical_profile_url,
                        'profile_key', peers.profile_key,
                        'degree', degrees.degree,
                        'shared_event_count', peers.shared_event_count
                    )
                      FROM peers LEFT JOIN degrees USING (entity_id)
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'topic:' || topics.topic,
                        'node_kind', 'topic',
                        'ring', 3,
                        'label', topics.topic,
                        'degree', topics.event_count
                    )
                      FROM topics
                ) AS assembled),
                'edges',
                (SELECT coalesce(jsonb_agg(edge), '[]'::jsonb) FROM (
                    SELECT jsonb_build_object(
                        'a', 'entity:' || edges.entity_id::text,
                        'b', 'event:' || edges.canonical_event_id::text,
                        'kind', 'mention',
                        'roles', to_jsonb(edges.roles),
                        'source_labels', to_jsonb(edges.source_labels),
                        'observed_at', edges.observed_at
                    ) AS edge
                      FROM edges
                    UNION ALL
                    SELECT jsonb_build_object(
                        'a', 'entity:' || p_entity_id::text,
                        'b', 'topic:' || topics.topic,
                        'kind', 'topic',
                        'roles', '[]'::jsonb,
                        'source_labels', '[]'::jsonb,
                        'observed_at', NULL
                    )
                      FROM topics
                ) AS assembled),
                'same_name_candidates',
                (SELECT coalesce(jsonb_agg(jsonb_build_object(
                            'entity_id', same_name.entity_id,
                            'display_name', same_name.display_name,
                            'kind', same_name.kind,
                            'identity_status', same_name.identity_status,
                            'event_count', same_name.event_count
                        ) ORDER BY same_name.event_count DESC, same_name.display_name), '[]'::jsonb)
                   FROM same_name)
            ) INTO v_result;

            RETURN v_result;
        END;
        $$
        
"""


_DIRECTORY_REPAIRED = r"""
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
                        -- LIKE metacharacters are escaped, so the substring branch matches
                        -- what the reader typed.  Unescaped, a query of '%' matches every row and
                        -- the front door presents the whole unfiltered ranking as search results,
                        -- while a run of '_' silently means "any name at least this long".  The
                        -- tsquery branch cannot compensate: plainto_tsquery('simple','%') is an
                        -- empty query, which leaves LIKE as the only matching branch.  '!' is the
                        -- escape character and is escaped first, so it survives as a literal.
                        OR entity.normalized_name LIKE
                           '%' || replace(
                                      replace(
                                          replace(lower(btrim(p_query)), '!', '!!'),
                                          '%', '!%'
                                      ),
                                      '_', '!_'
                                  ) || '%' ESCAPE '!')
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


_DIRECTORY_AS_INSTALLED_BEFORE_0165 = r"""
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
