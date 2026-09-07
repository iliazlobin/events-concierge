"""Open the entities view on the hubs and the events that actually join them.

Revision ID: 0174
Revises: 0173
Create Date: 2026-08-27

The entities front door has been a ranked list with a graph hidden one click behind it, which asks
the reader to already know which name to click before the catalog will show them anything.  The
question the view exists to answer -- *who are the hubs and how are they connected* -- is a shape,
and a list cannot draw a shape.  So the landing surface becomes a graph, and this is the single
capability that produces it.

**Why one representative event per pair.**  Measured against the live catalog (3,190 entities,
6,011 mentions, 41,347 canonical events) at the moment this was written:

* the top 40 entities by event count are bridged by **268 distinct canonical events** -- drawing
  all of them is 308 nodes and an unreadable mat;
* but those 268 events express only **18 connected pairs**, because the same two organizers
  co-appear again and again.  One pair alone shares 83 events;
* so the graph draws **one event per connected pair -- the most recent shared event**.  Measured
  output of this function at its defaults: 40 entity nodes, 13 bridge nodes, 30 edges, 8.3 ms warm
  (the ranked directory it replaces costs 22.3 ms).  Thirteen rather than eighteen because three
  hubs meeting at one event produce three pairs that all elect the *same* representative; the
  bridge set is deduplicated, so the node stands for every pair it joins.  Legible, and nothing is
  invented: every node and every edge is still a literal ``catalog_entity_event_mentions`` row,
  and the spine stays bipartite (entity -> event -> entity), never a synthesized
  entity-to-entity relation.

The obvious alternative -- keep the events that join the *most* entities and drop the rest -- was
measured and rejected: it collapses the 40 connected entities to **10**, because the few
many-entity events cluster on a handful of organizers and every other hub loses its only bridge.
Ranking by pair and keeping one event each is what preserves the whole hub set.

An entity in no pair is still emitted.  It is a top hub by measured event count; that it shares no
event with another top hub is a finding about the catalog, not a reason to hide it.  Measured, that
is not a corner case: of the top 40, **24 are connected and 16 are isolated**.  Dropping them would
delete 40% of the landing graph and quietly redefine "top hub" as "top hub that happens to
co-appear", which is the honest picture -- four fifths of catalogued entities appear at a single
event, and the landing graph should not pretend otherwise.

The representative choice is a *summary*, so it is disclosed rather than implied: the event node
carries ``shared_event_count``, the pair's true shared total, and the UI renders it beside the
node the way the coverage line renders the 4% caveat.

``p_identity_statuses`` is new here and has no counterpart on
``fn_get_catalog_entity_directory_v1``.  The directory's identity chips filter the already-fetched
page in the browser, which is correct for a list of 48 rows and wrong for a graph: filtering after
the top-N cut would silently shrink the graph to whatever survived, and the pairs would then be
computed over a set the reader never chose.  Identity is therefore a server-side predicate, applied
before the ranking, exactly like kind.

Query, kind and cancelled-event semantics are otherwise copied from the directory verbatim,
including its LIKE-metacharacter escaping -- an unescaped ``%`` matches every row, and the tsquery
branch cannot compensate because ``plainto_tsquery('simple','%')`` is an empty query.

The returned envelope is byte-compatible with ``fn_get_catalog_entity_graph_v1`` so the domain
dataclasses, the parser and the TypeScript types are reused unchanged.  ``focus_id`` is the literal
``'overview'`` because there is no ego.  ``counts`` is re-purposed rather than extended:
``peers``/``peers_total`` are entities drawn against entities matching, and ``events``/
``events_total`` are the bridge event nodes drawn against the total number of connected pairs.

Those last two are deliberately *not* the same quantity, and on the live catalog they differ every
time: 13 bridge nodes against 18 pairs.  ``counts.events`` always names the array beside it -- a
count that does not match its array is the same silent lie as an unreported truncation -- while
``events_total`` is what the pair cap actually drops.  Consequently ``truncated.events`` is read
off the pair cap directly (``pairs_total > pairs_drawn``) and never off a comparison of those two
fields, which would report truncation on every single call.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0174"
down_revision: str | None = "0173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OVERVIEW = "public.fn_get_catalog_entity_overview_graph_v1(text,text[],text[],integer,integer)"


def upgrade() -> None:
    op.execute(_OVERVIEW_GRAPH)
    # ec_app holds no table privileges, so the capability is the only reachable path to these rows.
    op.execute(f"REVOKE ALL ON FUNCTION {_OVERVIEW} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_OVERVIEW} TO ec_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_OVERVIEW}")


# Raw string: the control-character guard below contains \x escapes that a cooked Python literal
# would decode, silently shipping literal control bytes into the SQL instead of the regex class.
_OVERVIEW_GRAPH = r"""
CREATE OR REPLACE FUNCTION public.fn_get_catalog_entity_overview_graph_v1(
    p_query text,
    p_kinds text[],
    p_identity_statuses text[],
    p_entity_limit integer,
    p_pair_limit integer
)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $$
        DECLARE
            v_result jsonb;
        BEGIN
            -- Validation runs before any scan, in the idiom of the ego graph and the directory.
            -- No statement_timeout exists on this deployment, so every cap this function honours
            -- is enforced here rather than borrowed from the session.
            IF p_entity_limit IS NULL OR p_entity_limit NOT BETWEEN 1 AND 60
               OR p_pair_limit IS NULL OR p_pair_limit NOT BETWEEN 1 AND 60
               OR length(coalesce(p_query, '')) > 160
               OR p_query ~ '[\x00-\x1f\x7f]'
               OR cardinality(coalesce(p_kinds, '{}'::text[])) > 3
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_kinds, '{}'::text[])) AS kind(value)
                   WHERE kind.value NOT IN ('person', 'organization', 'unknown')
               )
               OR cardinality(coalesce(p_identity_statuses, '{}'::text[])) > 2
               OR EXISTS (
                   SELECT 1
                     FROM unnest(coalesce(p_identity_statuses, '{}'::text[])) AS status(value)
                    WHERE status.value NOT IN ('profile_verified', 'source_scoped')
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity overview graph request is invalid';
            END IF;

            WITH scoped AS MATERIALIZED (
                -- Query/kind/cancelled semantics are the directory's, unchanged, so the chips at
                -- the top of the view select the same population in both surfaces.  Every filter
                -- here is an entity predicate, which is what makes event_count below a
                -- whole-catalog degree rather than a property of this frame.
                SELECT entity.entity_id,
                       entity.display_name,
                       entity.kind,
                       entity.identity_status,
                       entity.canonical_profile_url,
                       entity.profile_key,
                       count(DISTINCT mention.canonical_event_id) AS event_count,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles
                  FROM public.catalog_entities AS entity
                  JOIN public.catalog_entity_event_mentions AS mention
                    ON mention.entity_id = entity.entity_id
                  JOIN public.canonical_events AS event
                    ON event.canonical_event_id = mention.canonical_event_id
                 WHERE event.event_status <> 'cancelled'
                   AND (cardinality(coalesce(p_kinds, '{}'::text[])) = 0
                        OR entity.kind = ANY(p_kinds))
                   AND (cardinality(coalesce(p_identity_statuses, '{}'::text[])) = 0
                        OR entity.identity_status = ANY(p_identity_statuses))
                   AND (nullif(btrim(p_query), '') IS NULL
                        OR entity.search_document @@ plainto_tsquery('simple', p_query)
                        -- LIKE metacharacters are escaped so the substring branch matches what the
                        -- reader typed.  Unescaped, a query of '%' matches every row and the graph
                        -- silently becomes the unfiltered top 40; a run of '_' silently means "any
                        -- name at least this long".  '!' is the escape character and is escaped
                        -- first, so it survives as a literal.
                        OR entity.normalized_name LIKE
                           '%' || replace(
                                      replace(
                                          replace(lower(btrim(p_query)), '!', '!!'),
                                          '%', '!%'
                                      ),
                                      '_', '!_'
                                  ) || '%' ESCAPE '!')
                 GROUP BY entity.entity_id
            ), ranked AS MATERIALIZED (
                -- Ranked by measured event count and nothing else.  entity_id breaks the tie so
                -- two requests a second apart cannot hand back different top-N sets.
                SELECT * FROM scoped
                 ORDER BY event_count DESC, entity_id
                 LIMIT p_entity_limit
            ), pair_agg AS MATERIALIZED (
                -- Every unordered pair of ranked entities that shares at least one live event.
                -- Bounded by construction: the self-join is restricted to the <= 60 ranked ids on
                -- both sides before it aggregates, so this is never a whole-graph self-join.
                -- The representative is the most recent shared event; canonical_event_id breaks a
                -- start_at tie, so the choice is stable across calls.
                SELECT mine.entity_id AS entity_a,
                       theirs.entity_id AS entity_b,
                       count(DISTINCT mine.canonical_event_id) AS shared_event_count,
                       (array_agg(mine.canonical_event_id
                            ORDER BY event.start_at DESC NULLS LAST, mine.canonical_event_id))[1]
                           AS representative_event_id
                  FROM public.catalog_entity_event_mentions AS mine
                  JOIN public.catalog_entity_event_mentions AS theirs
                    ON theirs.canonical_event_id = mine.canonical_event_id
                   AND theirs.entity_id > mine.entity_id
                  JOIN public.canonical_events AS event
                    ON event.canonical_event_id = mine.canonical_event_id
                 WHERE mine.entity_id IN (SELECT entity_id FROM ranked)
                   AND theirs.entity_id IN (SELECT entity_id FROM ranked)
                   AND event.event_status <> 'cancelled'
                 GROUP BY mine.entity_id, theirs.entity_id
            ), pairs AS MATERIALIZED (
                SELECT * FROM pair_agg
                 ORDER BY shared_event_count DESC, entity_a, entity_b
                 LIMIT p_pair_limit
            ), bridges AS (
                -- One node per surviving pair, deduplicated: two pairs may elect the same
                -- representative when three hubs meet at one event.  The node then carries the
                -- largest true shared count it stands for, because that is the number a reader
                -- would otherwise most badly under-read.
                SELECT pairs.representative_event_id AS canonical_event_id,
                       max(pairs.shared_event_count) AS shared_event_count
                  FROM pairs
                 GROUP BY pairs.representative_event_id
            ), bridge_events AS (
                SELECT event.canonical_event_id,
                       event.title,
                       event.start_at,
                       event.end_at,
                       event.venue_name,
                       event.city_norm,
                       event.topics,
                       event.price_status,
                       coalesce(event.end_at, event.start_at) <= statement_timestamp() AS is_past,
                       bridges.shared_event_count
                  FROM bridges
                  JOIN public.canonical_events AS event USING (canonical_event_id)
            ), bridge_registration AS (
                SELECT mention.canonical_event_id,
                       min(observation.registration_url) AS registration_url
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.catalog_event_observations AS observation
                    ON observation.source_key = mention.source_key
                   AND observation.source = mention.source
                   AND observation.source_event_id = mention.source_event_id
                 WHERE mention.canonical_event_id IN
                       (SELECT canonical_event_id FROM bridges)
                 GROUP BY mention.canonical_event_id
            ), bridge_degree AS (
                -- Whole-catalog entity count for the event, matching the ego graph: "6 entities"
                -- is a property of the event, not of this frame.
                SELECT mention.canonical_event_id,
                       count(DISTINCT mention.entity_id) AS entity_count
                  FROM public.catalog_entity_event_mentions AS mention
                 WHERE mention.canonical_event_id IN
                       (SELECT canonical_event_id FROM bridges)
                 GROUP BY mention.canonical_event_id
            ), edge_endpoints AS (
                -- UNION, not UNION ALL: an entity in several pairs that elect the same
                -- representative gets one edge into that node, not one per pair.
                SELECT pairs.entity_a AS entity_id,
                       pairs.representative_event_id AS canonical_event_id
                  FROM pairs
                 UNION
                SELECT pairs.entity_b, pairs.representative_event_id
                  FROM pairs
            ), edges AS (
                -- One edge per (entity, event), carrying the real roles and source labels of the
                -- mentions it stands for.  No weight: a mention is a fact, not a score.
                SELECT edge_endpoints.entity_id,
                       edge_endpoints.canonical_event_id,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                       array_agg(DISTINCT source.display_name ORDER BY source.display_name)
                           AS source_labels,
                       max(mention.observed_at) AS observed_at
                  FROM edge_endpoints
                  JOIN public.catalog_entity_event_mentions AS mention
                    ON mention.entity_id = edge_endpoints.entity_id
                   AND mention.canonical_event_id = edge_endpoints.canonical_event_id
                  JOIN public.catalog_sources AS source
                    ON source.source_key = mention.source_key
                 GROUP BY edge_endpoints.entity_id, edge_endpoints.canonical_event_id
            ), bridge_roles AS (
                -- The ego graph's 'ego_roles' is "what the focused entity did at this event".
                -- There is no ego here, so the analogue is what the hubs this bridge joins did.
                SELECT edges.canonical_event_id,
                       array_agg(DISTINCT role.value ORDER BY role.value) AS roles
                  FROM edges
                  CROSS JOIN LATERAL unnest(edges.roles) AS role(value)
                 GROUP BY edges.canonical_event_id
            )
            SELECT jsonb_build_object(
                -- There is no ego, and 'overview' cannot collide with a namespaced node id, so a
                -- client that keys off focus_id fails loudly rather than centring on a stranger.
                'focus_id', 'overview',
                'generated_at', statement_timestamp(),
                'counts', jsonb_build_object(
                    -- 'events' names the bridge node array beside it; 'events_total' is the number
                    -- of connected pairs, which is what the pair cap actually drops.
                    'events', (SELECT count(*) FROM bridge_events),
                    'events_total', (SELECT count(*) FROM pair_agg),
                    'peers', (SELECT count(*) FROM ranked),
                    'peers_total', (SELECT count(*) FROM scoped),
                    'topics', 0,
                    'edges', (SELECT count(*) FROM edges),
                    'mention_edges', (SELECT count(*) FROM edges),
                    'edges_total', (SELECT count(*) FROM edges)
                ),
                'truncated', jsonb_build_object(
                    -- Read off the pair cap itself rather than off counts.events, so the
                    -- disclosure stays correct when two pairs elect the same representative.
                    'events', (SELECT count(*) FROM pair_agg) > (SELECT count(*) FROM pairs),
                    'peers', (SELECT count(*) FROM scoped) > (SELECT count(*) FROM ranked),
                    'edges', false
                ),
                'nodes',
                (SELECT coalesce(jsonb_agg(node ORDER BY node ->> 'node_id'), '[]'::jsonb) FROM (
                    -- Ring 0 is every ranked hub, including the ones in no pair.  An isolated hub
                    -- is a finding about the catalog, not a row to hide.
                    SELECT jsonb_build_object(
                        'node_id', 'entity:' || ranked.entity_id::text,
                        'node_kind', 'entity',
                        'ring', 0,
                        'label', ranked.display_name,
                        'entity_id', ranked.entity_id,
                        'entity_kind', ranked.kind,
                        'identity_status', ranked.identity_status,
                        'profile_url', ranked.canonical_profile_url,
                        'profile_key', ranked.profile_key,
                        'degree', ranked.event_count,
                        'shared_event_count', NULL,
                        'roles', to_jsonb(coalesce(ranked.roles, '{}'::text[]))
                    ) AS node
                      FROM ranked
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'event:' || bridge_events.canonical_event_id::text,
                        'node_kind', 'event',
                        'ring', 1,
                        'label', bridge_events.title,
                        'canonical_event_id', bridge_events.canonical_event_id,
                        'start_at', bridge_events.start_at,
                        'end_at', bridge_events.end_at,
                        'is_past', bridge_events.is_past,
                        'venue_name', bridge_events.venue_name,
                        'city', bridge_events.city_norm,
                        'price_status', bridge_events.price_status,
                        'topics', to_jsonb(coalesce(bridge_events.topics, '{}'::text[])),
                        'ego_roles', to_jsonb(coalesce(bridge_roles.roles, '{}'::text[])),
                        'registration_url', bridge_registration.registration_url,
                        'degree', bridge_degree.entity_count,
                        -- The pair's TRUE shared total, not the one event drawn.  This is the
                        -- number the UI must disclose beside the node, because the node is a
                        -- representative and saying so is the whole contract.
                        'shared_event_count', bridge_events.shared_event_count
                    )
                      FROM bridge_events
                      LEFT JOIN bridge_registration USING (canonical_event_id)
                      LEFT JOIN bridge_degree USING (canonical_event_id)
                      LEFT JOIN bridge_roles USING (canonical_event_id)
                ) AS assembled),
                'edges',
                (SELECT coalesce(jsonb_agg(jsonb_build_object(
                            'a', 'entity:' || edges.entity_id::text,
                            'b', 'event:' || edges.canonical_event_id::text,
                            'kind', 'mention',
                            'roles', to_jsonb(edges.roles),
                            'source_labels', to_jsonb(edges.source_labels),
                            'observed_at', edges.observed_at
                        ) ORDER BY edges.entity_id, edges.canonical_event_id), '[]'::jsonb)
                   FROM edges),
                -- The overview has no ego, so there is no name to review against.  The exact-name
                -- review path stays on the ego graph where a reader has chosen a subject.
                'same_name_candidates', '[]'::jsonb
            ) INTO v_result;

            RETURN v_result;
        END;
        $$
"""
