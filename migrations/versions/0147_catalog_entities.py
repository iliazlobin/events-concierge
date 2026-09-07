"""Materialize a searchable event-entity catalog and connect it to enrichment facts.

Revision ID: 0147
Revises: 0146
Create Date: 2026-08-04

Verified direct profile URLs are safe cross-source identities.  Display names are not: entities
without a stable profile remain scoped to the source that asserted the event role.  The projection
is refreshed in the same transaction that publishes a source refresh, and verified identities are
also written into the existing review-gated enrichment control plane.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0147"
down_revision: str | None = "0146"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFRESH = "public.fn_refresh_catalog_entity_index_v1(text)"
_LIST = "public.fn_list_catalog_entities_v1(text,text[],integer,text,uuid)"
_GET = "public.fn_get_catalog_entity_v1(uuid)"
_EVENTS = "public.fn_list_catalog_entity_events_v1(uuid,integer)"
_RESOLVE = "public.fn_resolve_catalog_event_entity_v1(uuid,text,text)"


def upgrade() -> None:
    _create_tables()
    _register_research_adapters()
    _create_refresh()
    _create_reads()
    for signature in (_REFRESH, _LIST, _GET, _EVENTS, _RESOLVE):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
    for table in (
        "catalog_entities",
        "catalog_entity_event_mentions",
        "catalog_entity_source_links",
    ):
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, ec_app")
    op.execute("SELECT public.fn_refresh_catalog_entity_index_v1(NULL)")


def downgrade() -> None:
    for signature in (_RESOLVE, _EVENTS, _GET, _LIST, _REFRESH):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for table in (
        "catalog_entity_source_links",
        "catalog_entity_event_mentions",
        "catalog_entities",
    ):
        op.execute(f"DROP TABLE IF EXISTS public.{table}")
    op.execute(
        """
        DELETE FROM public.entity_enrichment_providers
        WHERE provider_key IN (
            'official_website', 'wikidata_public', 'github_public', 'pdl_company'
        )
        """
    )


def _create_tables() -> None:
    op.execute(
        """
        CREATE TABLE public.catalog_entities (
            entity_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            identity_key          text NOT NULL UNIQUE,
            identity_status       text NOT NULL,
            kind                  text NOT NULL,
            display_name          text NOT NULL,
            normalized_name       text NOT NULL,
            canonical_profile_url text,
            summary               text,
            website_url           text,
            logo_url              text,
            city                  text,
            country               text,
            first_seen_at         timestamptz NOT NULL,
            last_seen_at          timestamptz NOT NULL,
            created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            search_document       tsvector GENERATED ALWAYS AS (
                to_tsvector(
                    'simple',
                    coalesce(display_name, '') || ' ' || coalesce(summary, '') || ' ' ||
                    coalesce(city, '') || ' ' || coalesce(country, '')
                )
            ) STORED,
            CHECK (identity_status IN ('profile_verified', 'source_scoped')),
            CHECK (kind IN ('person', 'organization', 'unknown')),
            CHECK (char_length(display_name) BETWEEN 1 AND 160),
            CHECK (display_name !~ '[[:cntrl:]]'),
            CHECK (char_length(normalized_name) BETWEEN 1 AND 160),
            CHECK (last_seen_at >= first_seen_at),
            CHECK (
                (identity_status = 'profile_verified' AND canonical_profile_url IS NOT NULL)
                OR (identity_status = 'source_scoped' AND canonical_profile_url IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_entities_search
        ON public.catalog_entities USING gin (search_document)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_entities_name
        ON public.catalog_entities (normalized_name, entity_id)
        """
    )
    op.execute(
        """
        CREATE TABLE public.catalog_entity_event_mentions (
            entity_id          uuid NOT NULL REFERENCES public.catalog_entities(entity_id)
                               ON DELETE CASCADE,
            canonical_event_id uuid NOT NULL REFERENCES public.canonical_events(canonical_event_id)
                               ON DELETE CASCADE,
            source_key         text NOT NULL REFERENCES public.catalog_sources(source_key)
                               ON DELETE RESTRICT,
            source             text NOT NULL,
            source_event_id    text NOT NULL,
            source_run_key     text NOT NULL,
            role               text NOT NULL,
            observed_name      text NOT NULL,
            observed_at        timestamptz NOT NULL,
            created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (entity_id, canonical_event_id, source_key, role),
            FOREIGN KEY (source_key, source, source_event_id)
                REFERENCES public.catalog_event_observations(source_key, source, source_event_id)
                ON DELETE CASCADE,
            FOREIGN KEY (source_key, source_run_key)
                REFERENCES public.catalog_refresh_runs(source_key, run_key)
                ON DELETE RESTRICT,
            CHECK (role IN ('organizer', 'host', 'speaker', 'partner')),
            CHECK (char_length(observed_name) BETWEEN 1 AND 160),
            CHECK (observed_name !~ '[[:cntrl:]]')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_entity_mentions_event
        ON public.catalog_entity_event_mentions (canonical_event_id, role, lower(observed_name))
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_entity_mentions_entity_time
        ON public.catalog_entity_event_mentions (entity_id, observed_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE public.catalog_entity_source_links (
            catalog_entity_id uuid NOT NULL REFERENCES public.catalog_entities(entity_id)
                              ON DELETE CASCADE,
            source_entity_id  uuid NOT NULL REFERENCES public.entity_enrichment_source_entities(entity_id)
                              ON DELETE RESTRICT,
            source_key        text NOT NULL REFERENCES public.catalog_sources(source_key)
                              ON DELETE RESTRICT,
            created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (catalog_entity_id, source_entity_id),
            UNIQUE (source_entity_id),
            FOREIGN KEY (source_entity_id, source_key)
                REFERENCES public.entity_enrichment_source_entities(entity_id, source_key)
                ON DELETE RESTRICT
        )
        """
    )


def _register_research_adapters() -> None:
    """Declare bounded adapters; provider/operator approval remains an explicit later action."""
    op.execute(
        """
        INSERT INTO public.entity_enrichment_providers (
            provider_key, display_name, allowed_entity_kinds, enabled,
            credentials_configured, terms_approved_at, operator_approved_at
        )
        VALUES
            ('official_website', 'Official website structured metadata',
             ARRAY['organization']::text[], false, false, NULL, NULL),
            ('wikidata_public', 'Wikidata public entity crosswalk',
             ARRAY['person', 'organization']::text[], false, false, NULL, NULL),
            ('github_public', 'GitHub public organization metadata',
             ARRAY['organization']::text[], false, false, NULL, NULL),
            ('pdl_company', 'People Data Labs company enrichment',
             ARRAY['organization']::text[], false, false, NULL, NULL)
        ON CONFLICT (provider_key) DO NOTHING
        """
    )


def _create_refresh() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_refresh_catalog_entity_index_v1(p_source_key text)
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
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v1(
                    p_source_key, NULL, NULL
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


def _create_reads() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_catalog_entities_v1(
            p_query text,
            p_kinds text[],
            p_limit integer,
            p_after_name text,
            p_after_id uuid
        )
        RETURNS TABLE (
            entity_id uuid,
            display_name text,
            kind text,
            identity_status text,
            canonical_profile_url text,
            summary text,
            website_url text,
            logo_url text,
            city text,
            country text,
            event_count bigint,
            roles text[],
            source_count bigint,
            research_status text
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 101
               OR length(coalesce(p_query, '')) > 160
               OR p_query ~ '[\x00-\x1f\x7f]'
               OR cardinality(coalesce(p_kinds, '{}'::text[])) > 3
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_kinds, '{}'::text[])) AS kind(value)
                   WHERE kind.value NOT IN ('person', 'organization', 'unknown')
               )
               OR ((p_after_name IS NULL) <> (p_after_id IS NULL))
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'entity browse is invalid';
            END IF;
            RETURN QUERY
            SELECT entity.entity_id,
                   entity.display_name,
                   entity.kind,
                   entity.identity_status,
                   entity.canonical_profile_url,
                   entity.summary,
                   entity.website_url,
                   entity.logo_url,
                   entity.city,
                   entity.country,
                   count(DISTINCT mention.canonical_event_id) AS event_count,
                   array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                   count(DISTINCT mention.source_key) AS source_count,
                   CASE
                       WHEN bool_or(job.status = 'running') THEN 'researching'
                       WHEN bool_or(job.status = 'queued') THEN 'queued'
                       WHEN bool_or(job.status = 'succeeded') THEN 'review_pending'
                       WHEN count(link.source_entity_id) > 0 THEN 'researchable'
                       ELSE 'identity_required'
                   END AS research_status
            FROM public.catalog_entities AS entity
            JOIN public.catalog_entity_event_mentions AS mention
              ON mention.entity_id = entity.entity_id
            LEFT JOIN public.catalog_entity_source_links AS link
              ON link.catalog_entity_id = entity.entity_id
            LEFT JOIN public.entity_enrichment_jobs AS job
              ON job.entity_id = link.source_entity_id
             AND job.status IN ('queued', 'running', 'succeeded')
            WHERE (
                    nullif(btrim(p_query), '') IS NULL
                    OR entity.search_document @@ plainto_tsquery('simple', p_query)
                    OR entity.normalized_name LIKE '%' || lower(btrim(p_query)) || '%'
                  )
              AND (
                    cardinality(coalesce(p_kinds, '{}'::text[])) = 0
                    OR entity.kind = ANY(p_kinds)
                  )
              AND (
                    p_after_name IS NULL
                    OR (entity.normalized_name, entity.entity_id) > (p_after_name, p_after_id)
                  )
            GROUP BY entity.entity_id
            ORDER BY entity.normalized_name, entity.entity_id
            LIMIT p_limit;
        END;
        $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION public.fn_get_catalog_entity_v1(p_entity_id uuid)
        RETURNS TABLE (
            entity_id uuid,
            display_name text,
            kind text,
            identity_status text,
            canonical_profile_url text,
            summary text,
            website_url text,
            logo_url text,
            city text,
            country text,
            event_count bigint,
            roles text[],
            source_count bigint,
            research_status text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT entity.entity_id,
                   entity.display_name,
                   entity.kind,
                   entity.identity_status,
                   entity.canonical_profile_url,
                   entity.summary,
                   entity.website_url,
                   entity.logo_url,
                   entity.city,
                   entity.country,
                   count(DISTINCT mention.canonical_event_id) AS event_count,
                   array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                   count(DISTINCT mention.source_key) AS source_count,
                   CASE
                       WHEN bool_or(job.status = 'running') THEN 'researching'
                       WHEN bool_or(job.status = 'queued') THEN 'queued'
                       WHEN bool_or(job.status = 'succeeded') THEN 'review_pending'
                       WHEN count(link.source_entity_id) > 0 THEN 'researchable'
                       ELSE 'identity_required'
                   END AS research_status
            FROM public.catalog_entities AS entity
            JOIN public.catalog_entity_event_mentions AS mention
              ON mention.entity_id = entity.entity_id
            LEFT JOIN public.catalog_entity_source_links AS link
              ON link.catalog_entity_id = entity.entity_id
            LEFT JOIN public.entity_enrichment_jobs AS job
              ON job.entity_id = link.source_entity_id
             AND job.status IN ('queued', 'running', 'succeeded')
            WHERE entity.entity_id = p_entity_id
            GROUP BY entity.entity_id
        $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_catalog_entity_events_v1(
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
            registration_url text
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
            SELECT event.canonical_event_id,
                   event.title,
                   event.start_at,
                   event.end_at,
                   event.venue_name,
                   event.city_norm,
                   left(event.description, 800),
                   array_agg(DISTINCT mention.role ORDER BY mention.role),
                   array_agg(DISTINCT source.display_name ORDER BY source.display_name),
                   min(observation.registration_url)
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
            ORDER BY event.start_at, event.canonical_event_id
            LIMIT p_limit;
        END;
        $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION public.fn_resolve_catalog_event_entity_v1(
            p_event_id uuid,
            p_role text,
            p_name text
        )
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT mention.entity_id
            FROM public.catalog_entity_event_mentions AS mention
            WHERE mention.canonical_event_id = p_event_id
              AND mention.role = p_role
              AND lower(btrim(mention.observed_name)) = lower(btrim(p_name))
            ORDER BY mention.observed_at DESC, mention.entity_id
            LIMIT 1
        $$
        """
    )
