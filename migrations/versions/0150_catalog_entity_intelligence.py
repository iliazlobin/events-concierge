"""Store source-backed structured intelligence for public event entities.

Revision ID: 0150
Revises: 0149
Create Date: 2026-08-05

The entity catalog already has a conservative identity boundary.  This revision adds a separate,
replace-on-refresh projection for bounded public facts.  Raw provider payloads are never stored;
every displayed value retains its source URL and refresh timestamp.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0150"
down_revision: str | None = "0149"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPLACE = (
    "public.fn_replace_catalog_entity_external_source_v1"
    "(uuid,text,text,text,text,text,timestamptz,timestamptz,text,jsonb)"
)
_SOURCES = "public.fn_list_catalog_entity_external_sources_v1(uuid)"
_FACTS = "public.fn_list_catalog_entity_external_facts_v1(uuid)"
_DUE = "public.fn_list_catalog_entities_due_for_refresh_v1(integer)"
_LIST = "public.fn_list_catalog_entities_v1(text,text[],integer,text,uuid)"


def upgrade() -> None:
    _create_tables()
    _create_capabilities()
    _replace_entity_search()
    for signature in (_REPLACE, _SOURCES, _FACTS, _DUE):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
    for table in ("catalog_entity_external_sources", "catalog_entity_external_facts"):
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, ec_app")


def downgrade() -> None:
    _restore_entity_search()
    for signature in (_DUE, _FACTS, _SOURCES, _REPLACE):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP TABLE IF EXISTS public.catalog_entity_external_facts")
    op.execute("DROP TABLE IF EXISTS public.catalog_entity_external_sources")


def _create_tables() -> None:
    op.execute(
        """
        CREATE TABLE public.catalog_entity_external_sources (
            source_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            entity_id        uuid NOT NULL REFERENCES public.catalog_entities(entity_id)
                             ON DELETE CASCADE,
            provider_key     text NOT NULL,
            external_id      text NOT NULL,
            source_url       text NOT NULL,
            display_name     text NOT NULL,
            status           text NOT NULL,
            fetched_at       timestamptz,
            next_refresh_at  timestamptz NOT NULL,
            error_code       text,
            created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (entity_id, provider_key),
            CHECK (provider_key IN (
                'official_website', 'github_public', 'wikidata_public', 'linkedin_profile'
            )),
            CHECK (char_length(external_id) BETWEEN 1 AND 500),
            CHECK (external_id !~ '[[:cntrl:]]'),
            CHECK (
                char_length(source_url) BETWEEN 9 AND 2048
                AND source_url !~ '[[:cntrl:][:space:]]'
                AND source_url !~ '#'
                AND source_url ~ '^https://[^/?#@[:space:]]+(/|[?]|$)'
            ),
            CHECK (char_length(display_name) BETWEEN 1 AND 160),
            CHECK (display_name !~ '[[:cntrl:]]'),
            CHECK (status IN ('linked', 'fresh', 'failed', 'blocked')),
            CHECK (
                (status = 'fresh' AND fetched_at IS NOT NULL AND error_code IS NULL)
                OR (status = 'linked' AND error_code IS NULL)
                OR (status IN ('failed', 'blocked') AND error_code IS NOT NULL)
            ),
            CHECK (error_code IS NULL OR error_code IN (
                'unavailable', 'rate_limited', 'invalid_response',
                'unsupported_profile', 'network_policy'
            )),
            CHECK (fetched_at IS NULL OR next_refresh_at > fetched_at)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_entity_external_sources_refresh
        ON public.catalog_entity_external_sources (next_refresh_at, entity_id)
        """
    )
    op.execute(
        """
        CREATE TABLE public.catalog_entity_external_facts (
            fact_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id     uuid NOT NULL REFERENCES public.catalog_entity_external_sources(source_id)
                          ON DELETE CASCADE,
            entity_id     uuid NOT NULL REFERENCES public.catalog_entities(entity_id)
                          ON DELETE CASCADE,
            fact_key      text NOT NULL,
            fact_value    text NOT NULL,
            fact_url      text,
            sort_order    integer NOT NULL DEFAULT 0,
            observed_at   timestamptz NOT NULL,
            search_document tsvector GENERATED ALWAYS AS (
                to_tsvector('simple', coalesce(fact_value, ''))
            ) STORED,
            CHECK (fact_key IN (
                'description', 'website', 'location', 'country', 'founded',
                'entity_type', 'focus', 'industry', 'profile', 'job_title',
                'organization', 'known_for', 'public_repositories', 'followers'
            )),
            CHECK (char_length(fact_value) BETWEEN 1 AND 1000),
            CHECK (fact_value !~ '[[:cntrl:]]'),
            CHECK (
                fact_url IS NULL OR (
                    char_length(fact_url) BETWEEN 9 AND 2048
                    AND fact_url !~ '[[:cntrl:][:space:]]'
                    AND fact_url !~ '#'
                    AND fact_url ~ '^https://[^/?#@[:space:]]+(/|[?]|$)'
                )
            ),
            CHECK (sort_order BETWEEN 0 AND 999)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_entity_external_facts_entity
        ON public.catalog_entity_external_facts (entity_id, fact_key, sort_order, fact_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_entity_external_facts_search
        ON public.catalog_entity_external_facts USING gin (search_document)
        """
    )


def _create_capabilities() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_replace_catalog_entity_external_source_v1(
            p_entity_id uuid,
            p_provider_key text,
            p_external_id text,
            p_source_url text,
            p_display_name text,
            p_status text,
            p_fetched_at timestamptz,
            p_next_refresh_at timestamptz,
            p_error_code text,
            p_facts jsonb
        )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_source_id uuid;
        BEGIN
            IF NOT EXISTS (
                    SELECT 1 FROM public.catalog_entities AS entity
                    WHERE entity.entity_id = p_entity_id
                )
               OR p_provider_key NOT IN (
                    'official_website', 'github_public', 'wikidata_public', 'linkedin_profile'
               )
               OR char_length(coalesce(p_external_id, '')) NOT BETWEEN 1 AND 500
               OR p_external_id ~ '[\x00-\x1f\x7f]'
               OR char_length(coalesce(p_source_url, '')) NOT BETWEEN 9 AND 2048
               OR p_source_url !~ '^https://[^/?#@[:space:]]+(/|[?]|$)'
               OR p_source_url ~ '[\x00-\x1f\x7f[:space:]#]'
               OR char_length(coalesce(p_display_name, '')) NOT BETWEEN 1 AND 160
               OR p_display_name ~ '[\x00-\x1f\x7f]'
               OR p_status NOT IN ('linked', 'fresh', 'failed', 'blocked')
               OR p_next_refresh_at IS NULL
               OR (p_fetched_at IS NOT NULL AND p_next_refresh_at <= p_fetched_at)
               OR (p_status = 'fresh' AND (p_fetched_at IS NULL OR p_error_code IS NOT NULL))
               OR (p_status = 'linked' AND p_error_code IS NOT NULL)
               OR (p_status IN ('failed', 'blocked') AND p_error_code NOT IN (
                    'unavailable', 'rate_limited', 'invalid_response',
                    'unsupported_profile', 'network_policy'
               ))
               OR jsonb_typeof(coalesce(p_facts, '[]'::jsonb)) <> 'array'
               OR jsonb_array_length(coalesce(p_facts, '[]'::jsonb)) > 100
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(coalesce(p_facts, '[]'::jsonb)) AS item(value)
                    WHERE jsonb_typeof(item.value) <> 'object'
                       OR item.value->>'key' NOT IN (
                            'description', 'website', 'location', 'country', 'founded',
                            'entity_type', 'focus', 'industry', 'profile', 'job_title',
                            'organization', 'known_for', 'public_repositories', 'followers'
                          )
                       OR char_length(coalesce(item.value->>'value', '')) NOT BETWEEN 1 AND 1000
                       OR item.value->>'value' ~ '[\x00-\x1f\x7f]'
                       OR (
                            nullif(item.value->>'url', '') IS NOT NULL
                            AND (
                                char_length(item.value->>'url') NOT BETWEEN 9 AND 2048
                                OR item.value->>'url' !~ '^https://[^/?#@[:space:]]+(/|[?]|$)'
                                OR item.value->>'url' ~ '[\x00-\x1f\x7f[:space:]#]'
                            )
                       )
                       OR coalesce(item.value->>'order', '0') !~ '^[0-9]{1,3}$'
                       OR CASE
                            WHEN coalesce(item.value->>'order', '0') ~ '^[0-9]{1,3}$'
                            THEN (item.value->>'order')::integer NOT BETWEEN 0 AND 999
                            ELSE false
                          END
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'entity source snapshot is invalid';
            END IF;

            INSERT INTO public.catalog_entity_external_sources (
                entity_id, provider_key, external_id, source_url, display_name,
                status, fetched_at, next_refresh_at, error_code
            ) VALUES (
                p_entity_id, p_provider_key, p_external_id, p_source_url, p_display_name,
                p_status, p_fetched_at, p_next_refresh_at, p_error_code
            )
            ON CONFLICT (entity_id, provider_key) DO UPDATE
            SET external_id = EXCLUDED.external_id,
                source_url = EXCLUDED.source_url,
                display_name = EXCLUDED.display_name,
                status = EXCLUDED.status,
                fetched_at = EXCLUDED.fetched_at,
                next_refresh_at = EXCLUDED.next_refresh_at,
                error_code = EXCLUDED.error_code,
                updated_at = clock_timestamp()
            RETURNING source_id INTO v_source_id;

            DELETE FROM public.catalog_entity_external_facts AS fact
            WHERE fact.source_id = v_source_id;

            INSERT INTO public.catalog_entity_external_facts (
                source_id, entity_id, fact_key, fact_value, fact_url, sort_order, observed_at
            )
            SELECT v_source_id,
                   p_entity_id,
                   item.value->>'key',
                   item.value->>'value',
                   nullif(item.value->>'url', ''),
                   coalesce((item.value->>'order')::integer, 0),
                   coalesce(p_fetched_at, clock_timestamp())
            FROM jsonb_array_elements(coalesce(p_facts, '[]'::jsonb)) AS item(value);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_catalog_entity_external_sources_v1(p_entity_id uuid)
        RETURNS TABLE (
            provider_key text,
            external_id text,
            source_url text,
            display_name text,
            status text,
            fetched_at timestamptz,
            next_refresh_at timestamptz,
            error_code text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT source.provider_key,
                   source.external_id,
                   source.source_url,
                   source.display_name,
                   source.status,
                   source.fetched_at,
                   source.next_refresh_at,
                   source.error_code
            FROM public.catalog_entity_external_sources AS source
            WHERE source.entity_id = p_entity_id
            ORDER BY
                CASE source.status WHEN 'fresh' THEN 0 WHEN 'linked' THEN 1 ELSE 2 END,
                source.display_name,
                source.provider_key
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_catalog_entity_external_facts_v1(p_entity_id uuid)
        RETURNS TABLE (
            provider_key text,
            source_url text,
            fact_key text,
            fact_value text,
            fact_url text,
            sort_order integer,
            observed_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT source.provider_key,
                   source.source_url,
                   fact.fact_key,
                   fact.fact_value,
                   fact.fact_url,
                   fact.sort_order,
                   fact.observed_at
            FROM public.catalog_entity_external_facts AS fact
            JOIN public.catalog_entity_external_sources AS source
              ON source.source_id = fact.source_id
            WHERE fact.entity_id = p_entity_id
              AND source.status IN ('fresh', 'linked')
            ORDER BY fact.fact_key, fact.sort_order, fact.fact_value, fact.fact_id
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_catalog_entities_due_for_refresh_v1(p_limit integer)
        RETURNS TABLE (entity_id uuid)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'entity refresh limit is invalid';
            END IF;
            RETURN QUERY
            SELECT entity.entity_id
            FROM public.catalog_entities AS entity
            LEFT JOIN public.catalog_entity_external_sources AS source
              ON source.entity_id = entity.entity_id
            LEFT JOIN public.catalog_entity_event_mentions AS mention
              ON mention.entity_id = entity.entity_id
            WHERE entity.identity_status = 'profile_verified'
              AND entity.canonical_profile_url IS NOT NULL
            GROUP BY entity.entity_id
            HAVING count(source.source_id) = 0
                OR min(source.next_refresh_at) <= clock_timestamp()
            ORDER BY
                min(source.fetched_at) NULLS FIRST,
                count(DISTINCT mention.canonical_event_id) DESC,
                entity.last_seen_at DESC,
                entity.entity_id
            LIMIT p_limit;
        END;
        $$
        """
    )


def _replace_entity_search() -> None:
    """Include normalized public facts in the indexed entity directory search."""
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.fn_list_catalog_entities_v1(
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
                    OR EXISTS (
                        SELECT 1
                        FROM public.catalog_entity_external_facts AS fact
                        WHERE fact.entity_id = entity.entity_id
                          AND fact.search_document @@ plainto_tsquery('simple', p_query)
                    )
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


def _restore_entity_search() -> None:
    """Restore the pre-intelligence entity directory query during downgrade."""
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.fn_list_catalog_entities_v1(
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
