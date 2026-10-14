"""Retain source visibility for anonymous linked event cards without exposing private tables.

Revision ID: 0218
Revises: 0217
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from events_concierge.deployment.catalog_source_seeds import remove_unused_seed

revision: str = "0218"
down_revision: str | None = "0217"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODE_CONSTRAINT = """CHECK (mode IN (
 'public_jsonld','livewhale_json','sf_gov_json','datasf_our415','bibliocommons_rss',
 'san_jose_legistar','sunnyvale_legistar','alameda_legistar','oakland_legistar',
 'communico_json','tribe_events_json','localist_json','libcal_ics','civic_engage_rss',
 'midpen_html','usfca_html','calperformances_json','berkeley_rep_html','ybca_html',
 'oakland_html','luma_calendar_json','luma_discover_json','meetup_city_jsonld',
 'meetup_group_ics','tech_week_mcp','langchain_html','luma_event_html'
))"""


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.catalog_event_observations ADD COLUMN visibility text
            CHECK (visibility IN ('public','unlisted','private')),
            ADD COLUMN location_withheld boolean NOT NULL DEFAULT false
    """)
    op.execute("""
        ALTER TABLE public.catalog_sources DROP CONSTRAINT ck_catalog_sources_mode
    """)
    op.execute(
        "ALTER TABLE public.catalog_sources ADD CONSTRAINT ck_catalog_sources_mode "
        + _MODE_CONSTRAINT
    )
    op.execute("""
        INSERT INTO public.catalog_sources(
          source_key,display_name,publisher,seed_url,approved_origins,region,mode,
          handoff_only,enabled,reviewed_at,review_expires_at,refresh_interval_minutes,
          min_interval_ms,page_limit,collection_horizon_days
        ) VALUES (
          'luma-silicon-after-dark-2026','SemiAnalysis: Silicon After Dark','SemiAnalysis',
          'https://luma.com/siliconafterdark',ARRAY['https://luma.com'],
          'bayarea','luma_event_html',true,false,NULL,NULL,360,1500,1,90
        )
    """)
    # Exact observation metadata only after the existing capability proves source admission,
    # fixture exclusion, current publication or retained past eligibility. No table grant.
    op.execute("""
        CREATE FUNCTION public.fn_catalog_observation_card_v1(
            p_source_key text, p_source text, p_source_event_id text, p_event_id uuid
        ) RETURNS jsonb
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT jsonb_build_object('visibility', observed.visibility,
                                     'location_withheld', observed.location_withheld)
            FROM public.canonical_events AS event
            CROSS JOIN LATERAL public.fn_list_retained_catalog_browse_observations_v2(
                ARRAY[p_source_key], event.start_at - interval '1 microsecond',
                event.start_at + interval '1 microsecond'
            ) AS admitted
            JOIN public.catalog_event_observations AS observed
              ON observed.source_key = admitted.source_key
             AND observed.source::text = admitted.observation_source
             AND observed.source_event_id = admitted.source_event_id
             AND observed.canonical_event_id = admitted.canonical_event_id
             AND observed.last_run_key = admitted.refresh_run_key
            WHERE event.canonical_event_id = p_event_id
              AND admitted.canonical_event_id = p_event_id
              AND admitted.source_key = p_source_key
              AND admitted.observation_source = p_source
              AND admitted.source_event_id = p_source_event_id
        $function$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.fn_catalog_observation_card_v1(text,text,text,uuid) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.fn_catalog_observation_card_v1(text,text,text,uuid) TO ec_app"
    )
    op.execute("""
        CREATE FUNCTION public.fn_list_filtered_catalog_providers_v1(
            p_source_key text, p_window_start timestamptz, p_window_end timestamptz,
            p_cities text[], p_location_scopes text[], p_price text,
            p_price_max_cents integer, p_price_min_cents integer,
            p_topics text[], p_availability text
        ) RETURNS TABLE(source_key text, source_label text, publisher text,
                        provider text, seed_url text, event_count bigint)
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
        BEGIN
            IF p_availability IS NOT NULL AND p_availability NOT IN ('available','sold_out') THEN
                RAISE EXCEPTION 'invalid catalog provider availability';
            END IF;
            RETURN QUERY
            WITH matching AS MATERIALIZED (
                SELECT candidate.source_key, candidate.canonical_event_id
                FROM public.fn_catalog_name_candidates_v2(
                    CASE WHEN p_source_key IS NULL THEN '{}'::text[] ELSE ARRAY[p_source_key] END,
                    p_window_start, p_window_end, NULL, p_cities, p_location_scopes,
                    p_price, p_price_max_cents, p_price_min_cents, p_topics,
                    'soonest', NULL, NULL, NULL
                ) AS candidate
                WHERE p_availability IS NULL
                   OR (p_availability='available' AND candidate.registration_status='open')
                   OR (p_availability='sold_out' AND candidate.registration_status='sold_out')
            ), counts AS (
                SELECT matching.source_key, count(DISTINCT matching.canonical_event_id) AS count
                FROM matching GROUP BY matching.source_key
            )
            SELECT registry.source_key, registry.source_label, registry.publisher,
                   registry.provider, registry.seed_url, coalesce(counts.count, 0)
            FROM public.fn_list_current_catalog_providers_v3(
                p_source_key, p_window_start, p_window_end
            ) AS registry
            LEFT JOIN counts ON counts.source_key=registry.source_key;
        END;
        $function$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.fn_list_filtered_catalog_providers_v1"
        "(text,timestamptz,timestamptz,text[],text[],text,integer,integer,text[],text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_list_filtered_catalog_providers_v1"
        "(text,timestamptz,timestamptz,text[],text[],text,integer,integer,text[],text) TO ec_app"
    )


def downgrade() -> None:
    retained = (
        op.get_bind()
        .execute(
            text("""
        SELECT EXISTS (SELECT 1 FROM public.catalog_event_observations
                       WHERE visibility IS NOT NULL OR location_withheld)
    """)
        )
        .scalar_one()
    )
    if retained:
        raise RuntimeError("retain linked event visibility and source history; use code rollback")
    remove_unused_seed(
        "luma-silicon-after-dark-2026",
        {
            "display_name": "SemiAnalysis: Silicon After Dark",
            "publisher": "SemiAnalysis",
            "seed_url": "https://luma.com/siliconafterdark",
            "region": "bayarea",
            "mode": "luma_event_html",
            "refresh_interval_minutes": 360,
            "min_interval_ms": 1500,
            "page_limit": 1,
            "collection_horizon_days": 90,
            "approved_origins": ["https://luma.com"],
        },
    )
    if (
        op.get_bind()
        .execute(
            text("SELECT EXISTS(SELECT 1 FROM public.catalog_sources WHERE mode='luma_event_html')")
        )
        .scalar_one()
    ):
        raise RuntimeError("retain additional linked event sources; use code rollback")
    op.execute(
        "DROP FUNCTION public.fn_list_filtered_catalog_providers_v1"
        "(text,timestamptz,timestamptz,text[],text[],text,integer,integer,text[],text)"
    )
    op.execute("DROP FUNCTION public.fn_catalog_observation_card_v1(text,text,text,uuid)")
    op.execute(
        "ALTER TABLE public.catalog_event_observations DROP COLUMN visibility, DROP COLUMN location_withheld"
    )
    op.execute("ALTER TABLE public.catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        "ALTER TABLE public.catalog_sources ADD CONSTRAINT ck_catalog_sources_mode "
        + _MODE_CONSTRAINT.replace(",'luma_event_html'", "")
    )
