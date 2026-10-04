"""Separate imported social links from fenced, refreshable API snapshots.

Revision ID: 0200
Revises: 0199
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0200"
down_revision: str | None = "0199"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIM = "public.fn_claim_catalog_social_refresh_v1(text[],integer)"
_FINISH = "public.fn_finish_catalog_social_refresh_v1(uuid,text,uuid,text,jsonb,text,integer)"
_FACTS = "public.fn_list_catalog_entity_external_facts_v2(uuid)"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.catalog_entity_external_sources DROP CONSTRAINT ck_catalog_entity_external_sources_provider_key_v2"
    )
    op.execute("""
        ALTER TABLE public.catalog_entity_external_sources
        ADD CONSTRAINT ck_catalog_entity_external_sources_provider_key_v3 CHECK (provider_key IN (
            'official_website','github_public','wikidata_public','linkedin_profile',
            'x_profile','instagram_profile','tiktok_profile','youtube_profile',
            'x_public_api','instagram_public_api'
        ))
    """)
    # Keep v1's writer vocabulary unchanged: API rows are writable only through the fenced path.
    op.execute(
        "ALTER TABLE public.catalog_entity_external_sources DROP CONSTRAINT catalog_entity_external_sources_error_code_check"
    )
    op.execute("""ALTER TABLE public.catalog_entity_external_sources ADD CONSTRAINT
        catalog_entity_external_sources_error_code_check CHECK(error_code IS NULL OR error_code IN (
        'unavailable','rate_limited','invalid_response','unsupported_profile','network_policy',
        'credentials_rejected','identity_changed'))""")
    op.execute(
        "ALTER TABLE public.catalog_entity_external_facts DROP CONSTRAINT catalog_entity_external_facts_fact_key_check"
    )
    op.execute("""
        ALTER TABLE public.catalog_entity_external_facts
        ADD CONSTRAINT catalog_entity_external_facts_fact_key_check CHECK (fact_key IN (
            'description','website','location','country','founded','entity_type','focus','industry',
            'profile','job_title','organization','known_for','public_repositories','followers','avatar'
        ))
    """)
    op.execute("""
        CREATE TABLE public.catalog_social_profile_refreshes (
            entity_id uuid NOT NULL REFERENCES public.catalog_entities ON DELETE CASCADE,
            provider_key text NOT NULL CHECK (provider_key IN ('x_public_api','instagram_public_api')),
            source_url text NOT NULL,
            next_refresh_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            lease_token uuid,
            lease_expires_at timestamptz,
            PRIMARY KEY(entity_id, provider_key)
        );
        CREATE INDEX ix_catalog_social_refresh_due
            ON public.catalog_social_profile_refreshes(next_refresh_at, entity_id);
        CREATE TABLE public.catalog_social_profile_daily_budget (
            provider_key text NOT NULL CHECK (provider_key IN ('x_public_api','instagram_public_api')),
            budget_date date NOT NULL,
            reserved_calls integer NOT NULL DEFAULT 0 CHECK(reserved_calls >= 0),
            PRIMARY KEY(provider_key, budget_date)
        );
        REVOKE ALL ON public.catalog_social_profile_refreshes,
            public.catalog_social_profile_daily_budget FROM PUBLIC, ec_app;
    """)
    _claim_function()
    _finish_function()
    op.execute("""
        CREATE FUNCTION public.fn_list_catalog_entity_external_facts_v2(p_entity_id uuid)
        RETURNS TABLE(provider_key text, source_url text, fact_key text, fact_value text,
                      fact_url text, sort_order integer, observed_at timestamptz)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT s.provider_key, s.source_url, f.fact_key, f.fact_value,
                   f.fact_url, f.sort_order, f.observed_at
            FROM public.catalog_entity_external_facts f
            JOIN public.catalog_entity_external_sources s ON s.source_id=f.source_id
            WHERE f.entity_id=p_entity_id AND (
                s.status IN ('fresh','linked') OR (
                    s.provider_key IN ('x_public_api','instagram_public_api')
                    AND s.fetched_at IS NOT NULL
                )
            )
            ORDER BY f.fact_key, f.sort_order, f.fact_value, f.fact_id
        $$
    """)
    for signature in (_CLAIM, _FINISH, _FACTS):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def _claim_function() -> None:
    op.execute("""
        CREATE FUNCTION public.fn_claim_catalog_social_refresh_v1(p_providers text[], p_daily_limit integer)
        RETURNS TABLE(entity_id uuid, provider_key text, source_url text, previous_id text, lease_token uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            v_job public.catalog_social_profile_refreshes%ROWTYPE;
            v_day date := (clock_timestamp() AT TIME ZONE 'UTC')::date;
            v_calls integer;
        BEGIN
            IF p_providers IS NULL OR cardinality(p_providers) NOT BETWEEN 1 AND 2
               OR array_position(p_providers,NULL) IS NOT NULL
               OR NOT p_providers <@ ARRAY['x_public_api','instagram_public_api']::text[]
               OR p_daily_limit IS NULL OR p_daily_limit NOT BETWEEN 1 AND 10000 THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='social refresh configuration invalid';
            END IF;
            INSERT INTO public.catalog_social_profile_refreshes(entity_id,provider_key,source_url)
            SELECT e.entity_id, p.key, s.source_url
            FROM public.catalog_entities e
            CROSS JOIN unnest(p_providers) p(key)
            JOIN public.catalog_entity_external_sources s ON s.entity_id=e.entity_id
                AND s.provider_key=CASE p.key WHEN 'x_public_api' THEN 'x_profile' ELSE 'instagram_profile' END
            WHERE e.identity_status='profile_verified' AND e.canonical_profile_url IS NOT NULL
            ON CONFLICT ON CONSTRAINT catalog_social_profile_refreshes_pkey DO NOTHING;

            FOR v_job IN
                SELECT j.* FROM public.catalog_social_profile_refreshes j
                JOIN public.catalog_entities e ON e.entity_id=j.entity_id
                JOIN public.catalog_entity_external_sources s ON s.entity_id=j.entity_id
                  AND s.provider_key=CASE j.provider_key WHEN 'x_public_api' THEN 'x_profile' ELSE 'instagram_profile' END
                WHERE j.provider_key=ANY(p_providers) AND j.next_refresh_at <= clock_timestamp()
                  AND (j.lease_expires_at IS NULL OR j.lease_expires_at <= clock_timestamp())
                  AND e.identity_status='profile_verified' AND e.canonical_profile_url IS NOT NULL
                  AND public.fn_normalize_social_profile_url_v1(s.source_url)=s.source_url
                  AND NOT EXISTS (SELECT 1 FROM public.catalog_social_profile_daily_budget b
                      WHERE b.provider_key=j.provider_key AND b.budget_date=v_day
                          AND b.reserved_calls>=p_daily_limit)
                ORDER BY j.next_refresh_at, j.entity_id, j.provider_key
                LIMIT 1
                FOR UPDATE OF j SKIP LOCKED
            LOOP
                INSERT INTO public.catalog_social_profile_daily_budget(provider_key,budget_date)
                    VALUES(v_job.provider_key,v_day) ON CONFLICT DO NOTHING;
                UPDATE public.catalog_social_profile_daily_budget b
                    SET reserved_calls=b.reserved_calls+1
                    WHERE b.provider_key=v_job.provider_key AND b.budget_date=v_day
                        AND b.reserved_calls<p_daily_limit
                    RETURNING b.reserved_calls INTO v_calls;
                IF NOT FOUND THEN RETURN; END IF;
                UPDATE public.catalog_social_profile_refreshes j
                    SET lease_token=gen_random_uuid(), lease_expires_at=clock_timestamp()+interval '60 seconds',
                        source_url=s.source_url
                    FROM public.catalog_entity_external_sources s
                    WHERE j.entity_id=v_job.entity_id AND j.provider_key=v_job.provider_key
                      AND s.entity_id=j.entity_id AND s.provider_key=CASE j.provider_key
                        WHEN 'x_public_api' THEN 'x_profile' ELSE 'instagram_profile' END
                    RETURNING j.* INTO v_job;
                RETURN QUERY SELECT v_job.entity_id,v_job.provider_key,v_job.source_url,
                    (SELECT s.external_id FROM public.catalog_entity_external_sources s
                     WHERE s.entity_id=v_job.entity_id AND s.provider_key=v_job.provider_key
                         AND s.fetched_at IS NOT NULL), v_job.lease_token;
                RETURN;
            END LOOP;
        END;
        $$
    """)


def _finish_function() -> None:
    op.execute(r"""
        CREATE FUNCTION public.fn_finish_catalog_social_refresh_v1(
            p_entity_id uuid,p_provider_key text,p_token uuid,p_external_id text,
            p_facts jsonb,p_error_code text,p_refresh_seconds integer
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        DECLARE
            v_job public.catalog_social_profile_refreshes%ROWTYPE;
            v_source_id uuid;
            v_now timestamptz := clock_timestamp();
            v_previous_id text;
        BEGIN
            SELECT * INTO v_job FROM public.catalog_social_profile_refreshes j
                WHERE j.entity_id=p_entity_id AND j.provider_key=p_provider_key
                FOR UPDATE;
            IF NOT FOUND OR p_token IS NULL OR v_job.lease_token IS DISTINCT FROM p_token
               OR v_job.lease_expires_at IS NULL
               OR v_job.lease_expires_at <= clock_timestamp() THEN RETURN false; END IF;
            -- Imported links can change while the request is in flight; reject that candidate.
            PERFORM 1 FROM public.catalog_entity_external_sources s
                JOIN public.catalog_entities e ON e.entity_id=s.entity_id
                WHERE s.entity_id=p_entity_id AND s.provider_key=CASE p_provider_key
                    WHEN 'x_public_api' THEN 'x_profile' ELSE 'instagram_profile' END
                  AND s.source_url=v_job.source_url AND e.identity_status='profile_verified'
                  AND e.canonical_profile_url IS NOT NULL FOR SHARE OF s,e;
            IF NOT FOUND THEN
                UPDATE public.catalog_social_profile_refreshes j SET lease_token=NULL,lease_expires_at=NULL,
                    next_refresh_at=v_now+interval '1 day'
                    WHERE j.entity_id=p_entity_id AND j.provider_key=p_provider_key;
                RETURN false;
            END IF;
            IF p_refresh_seconds IS NULL OR p_refresh_seconds NOT BETWEEN 3600 AND 604800
               OR (p_error_code IS NOT NULL AND p_error_code NOT IN (
                    'unavailable','rate_limited','invalid_response','unsupported_profile',
                    'credentials_rejected','identity_changed'
               ))
               OR jsonb_typeof(p_facts) IS DISTINCT FROM 'array'
               OR jsonb_array_length(p_facts)>3
               OR (p_error_code IS NULL AND (p_external_id IS NULL OR p_external_id !~ '^[0-9]{1,32}$'))
               OR (p_error_code IS NOT NULL AND jsonb_array_length(p_facts)<>0)
               OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements(p_facts) i(v)
                    WHERE jsonb_typeof(i.v)<>'object'
                      OR coalesce(i.v->>'key','') NOT IN ('description','followers','avatar')
                      OR char_length(coalesce(i.v->>'value','')) NOT BETWEEN 1 AND 1000
                      OR i.v->>'value' ~ '[[:cntrl:]]'
                      OR (i.v->>'key'='followers' AND i.v->>'value' !~ '^[0-9]{1,13}$')
                      OR (i.v->>'key'='avatar' AND (
                          coalesce(i.v->>'url','') !~ '^https://[^/?#@[:space:]]+/'
                          OR char_length(i.v->>'url')>2048 OR i.v->>'url' ~ '[[:cntrl:][:space:]#]'
                          OR (p_provider_key='x_public_api' AND i.v->>'url' !~ '^https://pbs[.]twimg[.]com/')
                          OR p_provider_key<>'x_public_api'
                      )) OR (i.v->>'key'<>'avatar' AND nullif(i.v->>'url','') IS NOT NULL)
               ) OR (SELECT count(*) FROM jsonb_array_elements(p_facts)) <>
                    (SELECT count(DISTINCT i.v->>'key') FROM jsonb_array_elements(p_facts) i(v))
            THEN RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='social snapshot invalid'; END IF;

            SELECT s.external_id INTO v_previous_id FROM public.catalog_entity_external_sources s
                WHERE s.entity_id=p_entity_id AND s.provider_key=p_provider_key AND s.fetched_at IS NOT NULL;
            IF p_error_code IS NULL AND v_previous_id IS NOT NULL AND v_previous_id<>p_external_id THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='social account identity changed';
            END IF;
            IF p_error_code IS NULL THEN
                INSERT INTO public.catalog_entity_external_sources(
                    entity_id,provider_key,external_id,source_url,display_name,status,fetched_at,next_refresh_at,error_code
                ) VALUES(p_entity_id,p_provider_key,p_external_id,v_job.source_url,
                    CASE p_provider_key WHEN 'x_public_api' THEN 'X API' ELSE 'Instagram API' END,
                    'fresh',v_now,v_now+make_interval(secs=>p_refresh_seconds),NULL)
                ON CONFLICT(entity_id,provider_key) DO UPDATE SET external_id=EXCLUDED.external_id,
                    source_url=EXCLUDED.source_url,status='fresh',fetched_at=v_now,
                    next_refresh_at=EXCLUDED.next_refresh_at,error_code=NULL,updated_at=v_now
                RETURNING source_id INTO v_source_id;
                DELETE FROM public.catalog_entity_external_facts f WHERE f.source_id=v_source_id;
                INSERT INTO public.catalog_entity_external_facts(
                    source_id,entity_id,fact_key,fact_value,fact_url,sort_order,observed_at
                ) SELECT v_source_id,p_entity_id,i.v->>'key',i.v->>'value',nullif(i.v->>'url',''),0,v_now
                  FROM jsonb_array_elements(p_facts) i(v);
            ELSE
                INSERT INTO public.catalog_entity_external_sources(
                    entity_id,provider_key,external_id,source_url,display_name,status,next_refresh_at,error_code
                ) VALUES(p_entity_id,p_provider_key,'pending',v_job.source_url,
                    CASE p_provider_key WHEN 'x_public_api' THEN 'X API' ELSE 'Instagram API' END,
                    'failed',v_now+make_interval(secs=>p_refresh_seconds),p_error_code)
                ON CONFLICT(entity_id,provider_key) DO UPDATE SET status='failed',error_code=p_error_code,
                    next_refresh_at=EXCLUDED.next_refresh_at,updated_at=v_now;
                -- Keep last successful facts, stable ID, source URL and fetched_at unchanged.
            END IF;
            UPDATE public.catalog_social_profile_refreshes j SET lease_token=NULL,lease_expires_at=NULL,
                next_refresh_at=v_now+make_interval(secs=>p_refresh_seconds)
                WHERE j.entity_id=p_entity_id AND j.provider_key=p_provider_key;
            RETURN true;
        END;
        $$
    """)


def downgrade() -> None:
    # Roll application code back while keeping retained snapshots/schema. Never delete fetched data.
    if (
        op.get_bind()
        .execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM public.catalog_entity_external_sources WHERE provider_key IN ('x_public_api','instagram_public_api'))"
            )
        )
        .scalar_one()
    ):
        raise RuntimeError("retain social snapshot schema while rolling application code back")
    for signature in (_CLAIM, _FINISH, _FACTS):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute(
        "DROP TABLE public.catalog_social_profile_refreshes, public.catalog_social_profile_daily_budget"
    )
    op.execute(
        "ALTER TABLE public.catalog_entity_external_sources DROP CONSTRAINT ck_catalog_entity_external_sources_provider_key_v3"
    )
    op.execute("""ALTER TABLE public.catalog_entity_external_sources ADD CONSTRAINT
        ck_catalog_entity_external_sources_provider_key_v2 CHECK(provider_key IN (
        'official_website','github_public','wikidata_public','linkedin_profile',
        'x_profile','instagram_profile','tiktok_profile','youtube_profile'))""")
    op.execute(
        "ALTER TABLE public.catalog_entity_external_sources DROP CONSTRAINT catalog_entity_external_sources_error_code_check"
    )
    op.execute("""ALTER TABLE public.catalog_entity_external_sources ADD CONSTRAINT
        catalog_entity_external_sources_error_code_check CHECK(error_code IS NULL OR error_code IN (
        'unavailable','rate_limited','invalid_response','unsupported_profile','network_policy'))""")
    op.execute(
        "ALTER TABLE public.catalog_entity_external_facts DROP CONSTRAINT catalog_entity_external_facts_fact_key_check"
    )
    op.execute("""ALTER TABLE public.catalog_entity_external_facts ADD CONSTRAINT
        catalog_entity_external_facts_fact_key_check CHECK(fact_key IN (
        'description','website','location','country','founded','entity_type','focus','industry',
        'profile','job_title','organization','known_for','public_repositories','followers'))""")
