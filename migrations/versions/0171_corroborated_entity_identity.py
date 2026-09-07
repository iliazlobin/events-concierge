"""Let two sources corroborate one source-scoped identity instead of splitting it.

Revision ID: 0171
Revises: 0170
Create Date: 2026-08-26

Migration 0147 set the rule that still holds here: "Verified direct profile URLs are safe
cross-source identities.  Display names are not."  An entity without a profile URL is therefore
keyed to the source that asserted it, and two sources naming "Shu" have no reason to be the same
person.

That rule has a case it does not cover.  When two sources assert **the same role, for the same
name, on the same canonical event**, the shared event has done the identifying -- not the name.
Two feeds describing one event and naming one host are not guessing at a coincidence; they are
corroborating each other.  Keying them apart splits one host's evidence across two rows.

Until host calendars existed this was rare, because the two Discover shelves cover different cities
and rarely overlap.  Registering each host's own Luma calendar alongside the city shelf made it the
common case overnight: the same event now arrives through both, and the catalog split **75 names
across 164 source-scoped rows**.  Visibly, ``The SF Commons`` became two entities -- one holding
the 2 events the shelf sees, one holding the 86 the calendar sees.

The representative is the minimum source key of the whole **connected component**, not of a
source's direct neighbours.  A direct-neighbour minimum is not symmetric: a shelf that shares
events with thirty calendars would pick the smallest of all thirty, while each calendar would pick
between itself and the shelf, so hub and leaves would choose different representatives and never
converge on one row.  The recursive closure below makes every member of a component derive the same
key.

Components are computed over the union of this refresh's facts and every mention already persisted
for *other* sources, so a per-source refresh derives the same components a full rebuild does.  The
rows for the refreshing source are deleted immediately above the new block, which is why the
persisted branch cannot read its own stale assertions.

Profile-verified identities are untouched: they already merge across sources through
``profile_key``, and nothing here widens what counts as a profile.

Measured after this ran against the live catalog: the entity the coverage work had split back into
one row carrying every event, and the 164 duplicated rows collapsed.  ``fn_prune_catalog_entity_
index_v1`` clears whatever is left without mentions on the next pass.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0171"
down_revision: str | None = "0170"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFRESH = "public.fn_refresh_catalog_entity_index_v3(text)"

_CORROBORATED = r"""
CREATE OR REPLACE FUNCTION public.fn_refresh_catalog_entity_index_v3(p_source_key text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
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
                      -- A placeholder slug is not an identity.  Dropping it here means the row
                      -- falls back to source-scoped keying AND never reaches the enrichment loop
                      -- below, so a garbage URL is never sent to a provider.
                      AND NOT public.fn_catalog_profile_url_placeholder_v1(
                              candidate.value ->> 'profile_url'
                          )
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
                        THEN 'profile:' || md5(
                            public.fn_normalize_profile_url_v1(profile ->> 'profile_url')
                        )
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
                   END AS identity_key,
                   -- Written from the same input, in the same expression list, as the key above.
                   -- profile_key collision <=> identity_key collision; the GROUP BY below
                   -- therefore collapses a collision before the INSERT can observe it.
                   CASE WHEN profile IS NOT NULL
                        THEN public.fn_normalize_profile_url_v1(profile ->> 'profile_url')
                        ELSE NULL
                   END AS profile_key
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

            -- Corroborated source-scoped identity (see migration 0170).
            --
            -- A name is not a cross-source identity, so an entity without a verified profile URL
            -- is keyed to the source that asserted it.  That is right until two sources assert
            -- *the same role, for the same name, on the same canonical event* -- at that point the
            -- shared event has done the identifying, not the name, and keeping them apart splits
            -- one host's evidence across two rows.  Registering a host's own Luma calendar
            -- alongside the city Discover shelf made that the common case rather than a corner
            -- one: 75 names, 164 rows.
            --
            -- The representative is the minimum source key of the whole connected component, not
            -- of a source's direct neighbours, because a direct-neighbour minimum is not
            -- symmetric: a hub shelf and its leaf calendars would each choose a different
            -- representative and never converge.  Components are computed over the union of this
            -- refresh's facts and every mention already persisted for other sources, so the same
            -- component is derived no matter which source is refreshing.
            DROP TABLE IF EXISTS pg_temp.entity_identity_components;
            CREATE TEMP TABLE entity_identity_components ON COMMIT DROP AS
            WITH RECURSIVE assertions AS (
                SELECT fact.source_key,
                       fact.canonical_event_id,
                       fact.role,
                       fact.normalized_name,
                       fact.kind
                FROM entity_refresh_facts AS fact
                WHERE fact.identity_status = 'source_scoped'
                UNION
                -- Rows for p_source_key were deleted immediately above, so this branch carries
                -- only the other sources' standing assertions and can never see stale ones.
                SELECT mention.source_key,
                       mention.canonical_event_id,
                       mention.role,
                       regexp_replace(lower(btrim(mention.observed_name)), '\s+', ' ', 'g'),
                       entity.kind
                FROM public.catalog_entity_event_mentions AS mention
                JOIN public.catalog_entities AS entity
                  ON entity.entity_id = mention.entity_id
                WHERE entity.identity_status = 'source_scoped'
            ), edges AS (
                SELECT DISTINCT
                       lhs.kind,
                       lhs.normalized_name,
                       lhs.source_key AS lhs_source_key,
                       rhs.source_key AS rhs_source_key
                FROM assertions AS lhs
                JOIN assertions AS rhs
                  ON rhs.kind = lhs.kind
                 AND rhs.normalized_name = lhs.normalized_name
                 AND rhs.canonical_event_id = lhs.canonical_event_id
                 AND rhs.role = lhs.role
                 AND rhs.source_key <> lhs.source_key
            ), reach(kind, normalized_name, root, node) AS (
                SELECT kind, normalized_name, lhs_source_key, lhs_source_key FROM edges
                UNION
                SELECT walked.kind,
                       walked.normalized_name,
                       walked.root,
                       edge.rhs_source_key
                FROM reach AS walked
                JOIN edges AS edge
                  ON edge.kind = walked.kind
                 AND edge.normalized_name = walked.normalized_name
                 AND edge.lhs_source_key = walked.node
            )
            SELECT kind,
                   normalized_name,
                   root AS source_key,
                   min(node) AS representative
            FROM reach
            GROUP BY kind, normalized_name, root;

            UPDATE entity_refresh_facts AS fact
            SET identity_key = 'source:' || md5(
                    component.representative || chr(31) || fact.kind || chr(31) ||
                    fact.normalized_name
                )
            FROM entity_identity_components AS component
            WHERE fact.identity_status = 'source_scoped'
              AND component.kind = fact.kind
              AND component.normalized_name = fact.normalized_name
              AND component.source_key = fact.source_key;

            INSERT INTO public.catalog_entities (
                identity_key, identity_status, kind, display_name, normalized_name,
                canonical_profile_url, profile_key, first_seen_at, last_seen_at
            )
            SELECT identity_key,
                   max(identity_status),
                   (array_agg(kind ORDER BY CASE kind
                       WHEN 'organization' THEN 1 WHEN 'person' THEN 2 ELSE 3 END))[1],
                   (array_agg(observed_name ORDER BY observed_at DESC, observed_name))[1],
                   max(normalized_name),
                   max(profile_url),
                   max(profile_key),
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
                profile_key = EXCLUDED.profile_key,
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

            -- Relabelling only this refresh's facts is not enough.  A component is discovered the
            -- moment its *second* source arrives, and by then the first source's mentions are
            -- already persisted under its own key.  If that first source is not the component's
            -- representative, its rows would be stranded on an entity nobody keys to any more --
            -- which is exactly what happened in testing when the second source published was the
            -- lexicographic minimum, and the merge came out dependent on refresh order.
            --
            -- So every member that is not the representative has its standing mentions moved onto
            -- the representative's entity.  Duplicates are dropped rather than overwritten: the
            -- representative's own row for a given (event, source, role) is already correct.  The
            -- emptied rows are removed by the prune at the end of this function, and
            -- catalog_entity_source_links follows them by ON DELETE CASCADE.
            WITH moved AS (
                SELECT stale.entity_id AS stale_entity_id,
                       target.entity_id AS target_entity_id
                FROM entity_identity_components AS component
                JOIN public.catalog_entities AS stale
                  ON stale.identity_key = 'source:' || md5(
                         component.source_key || chr(31) || component.kind || chr(31) ||
                         component.normalized_name
                     )
                JOIN public.catalog_entities AS target
                  ON target.identity_key = 'source:' || md5(
                         component.representative || chr(31) || component.kind || chr(31) ||
                         component.normalized_name
                     )
                WHERE component.source_key <> component.representative
            ), carried AS (
                INSERT INTO public.catalog_entity_event_mentions (
                    entity_id, canonical_event_id, source_key, source, source_event_id,
                    source_run_key, role, observed_name, observed_at
                )
                SELECT moved.target_entity_id,
                       mention.canonical_event_id,
                       mention.source_key,
                       mention.source,
                       mention.source_event_id,
                       mention.source_run_key,
                       mention.role,
                       mention.observed_name,
                       mention.observed_at
                FROM public.catalog_entity_event_mentions AS mention
                JOIN moved ON moved.stale_entity_id = mention.entity_id
                ON CONFLICT (entity_id, canonical_event_id, source_key, role) DO NOTHING
                RETURNING 1
            )
            DELETE FROM public.catalog_entity_event_mentions AS mention
            USING moved
            WHERE mention.entity_id = moved.stale_entity_id;

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
        $function$
"""

_SOURCE_SCOPED = r"""
CREATE OR REPLACE FUNCTION public.fn_refresh_catalog_entity_index_v3(p_source_key text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
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
                      -- A placeholder slug is not an identity.  Dropping it here means the row
                      -- falls back to source-scoped keying AND never reaches the enrichment loop
                      -- below, so a garbage URL is never sent to a provider.
                      AND NOT public.fn_catalog_profile_url_placeholder_v1(
                              candidate.value ->> 'profile_url'
                          )
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
                        THEN 'profile:' || md5(
                            public.fn_normalize_profile_url_v1(profile ->> 'profile_url')
                        )
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
                   END AS identity_key,
                   -- Written from the same input, in the same expression list, as the key above.
                   -- profile_key collision <=> identity_key collision; the GROUP BY below
                   -- therefore collapses a collision before the INSERT can observe it.
                   CASE WHEN profile IS NOT NULL
                        THEN public.fn_normalize_profile_url_v1(profile ->> 'profile_url')
                        ELSE NULL
                   END AS profile_key
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
                canonical_profile_url, profile_key, first_seen_at, last_seen_at
            )
            SELECT identity_key,
                   max(identity_status),
                   (array_agg(kind ORDER BY CASE kind
                       WHEN 'organization' THEN 1 WHEN 'person' THEN 2 ELSE 3 END))[1],
                   (array_agg(observed_name ORDER BY observed_at DESC, observed_name))[1],
                   max(normalized_name),
                   max(profile_url),
                   max(profile_key),
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
                profile_key = EXCLUDED.profile_key,
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
        $function$
"""


def _install(definition: str) -> None:
    op.execute(definition)
    op.execute(f"REVOKE ALL ON FUNCTION {_REFRESH} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REFRESH} TO ec_app")


def upgrade() -> None:
    """Corroborated identity, then one full rebuild so standing rows adopt it immediately."""
    _install(_CORROBORATED)
    op.execute("SELECT public.fn_refresh_catalog_entity_index_v3(NULL)")


def downgrade() -> None:
    """Return to strictly source-scoped identity and rebuild the split rows."""
    _install(_SOURCE_SCOPED)
    op.execute("SELECT public.fn_refresh_catalog_entity_index_v3(NULL)")
