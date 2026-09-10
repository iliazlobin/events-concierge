"""Browse retained catalog records through a bounded operator projection.

Revision ID: 0191
Revises: 0190

This inventory differs from consumer discovery eligibility and historical event versions.
Source/run, date, price and search filters precede deduplication and keyset pagination.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0191"
down_revision: str | None = "0190"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = (
    "public.fn_get_operator_catalog_records_v1(text,text,text,text,text,timestamptz,uuid,integer)"
)
_DEFINER = "ec_operator_aggregate_definer"
_TABLES = (
    "catalog_sources",
    "catalog_refresh_runs",
    "catalog_event_observations",
    "canonical_events",
)


def upgrade() -> None:
    op.execute(_PROJECTION)
    op.execute(
        f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC, ec_app, ec_operator_viewer, ec_operator_controller, ec_ingestion_executor"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO ec_operator_viewer")
    # The restricted NOLOGIN definer may read tenant-neutral catalog rows. Operators
    # receive only the fixed projection, with no raw tables, payloads or tenant grants.
    for table in _TABLES:
        op.execute(f"GRANT SELECT ON public.{table} TO {_DEFINER}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.fn_ingestion_admin_source_is_fixture(text,text,text), public.fn_ingestion_admin_run_is_fixture(text,text) TO {_DEFINER}"
    )
    bind = op.get_bind()
    owner = bind.dialect.identifier_preparer.quote(
        bind.execute(text("SELECT current_user")).scalar_one()
    )
    op.execute(f"GRANT {_DEFINER} TO {owner}")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {_DEFINER}")
    op.execute(f"ALTER FUNCTION {_SIGNATURE} OWNER TO {_DEFINER}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {_DEFINER}")
    op.execute(f"REVOKE {_DEFINER} FROM {owner}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
    # The definer is shared by other operator projections. Do not revoke its
    # supporting privileges when removing this single public capability.


_PROJECTION = """
CREATE FUNCTION public.fn_get_operator_catalog_records_v1(
    p_source text DEFAULT NULL, p_run text DEFAULT NULL, p_query text DEFAULT NULL,
    p_date_scope text DEFAULT 'all', p_price_status text DEFAULT 'all',
    p_after timestamptz DEFAULT NULL, p_after_id uuid DEFAULT NULL,
    p_limit integer DEFAULT 20
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE v_result jsonb;
BEGIN
    IF (p_source IS NOT NULL AND p_source !~ '^[a-z0-9][a-z0-9-]{1,79}$')
       OR (p_run IS NOT NULL AND (p_source IS NULL OR length(p_run) NOT BETWEEN 1 AND 256 OR p_run ~ '[[:cntrl:]]'))
       OR p_date_scope IS NULL OR p_date_scope NOT IN ('all','upcoming','past')
       OR p_price_status IS NULL OR p_price_status NOT IN ('all','free','paid','unknown')
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
       OR (p_query IS NOT NULL AND (length(p_query)>160 OR p_query ~ '[[:cntrl:]]'))
       OR ((p_after IS NULL)<>(p_after_id IS NULL))
       OR (p_after IS NOT NULL AND NOT isfinite(p_after)) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid operator catalog query';
    END IF;
    WITH matching AS MATERIALIZED (
        SELECT
            event.canonical_event_id, event.title, event.start_at, event.end_at,
            observation.source_key, left(source.display_name,500) AS source_display_name,
            event.venue_name, event.city_norm AS city, event.lat AS latitude, event.lon AS longitude,
            left(event.description,2000) AS description, length(event.description) AS description_length,
            event.price_status, event.price_min_cents, event.price_max_cents, event.price_currency,
            event.event_status, event.normalizer_version, event.merge_version,
            observation.source AS observation_source,
            observation.source_event_id, nullif(btrim(observation.registration_url),'') AS registration_url,
            observation.last_seen_at, observation.last_run_key AS refresh_run_key,
            array_remove(ARRAY[
                CASE WHEN btrim(event.description)='' THEN 'missing_description' END,
                CASE WHEN event.end_at IS NULL THEN 'missing_end_time' END,
                CASE WHEN nullif(btrim(event.venue_name),'') IS NULL THEN 'missing_venue' END,
                CASE WHEN nullif(btrim(event.city_norm),'') IS NULL THEN 'missing_city' END,
                CASE WHEN event.lat IS NULL OR event.lon IS NULL THEN 'missing_geo' END,
                CASE WHEN nullif(btrim(observation.registration_url),'') IS NULL THEN 'missing_registration_url' END
            ],NULL) AS quality_issues,
            event.organizer_name, event.host_names, event.speaker_names, event.partner_names,
            event.entity_profiles, event.attendance_count, event.registration_status
        FROM public.catalog_event_observations observation
        JOIN public.canonical_events event USING (canonical_event_id)
        JOIN public.catalog_sources source ON source.source_key=observation.source_key
        JOIN public.catalog_refresh_runs refresh
          ON refresh.source_key=observation.source_key AND refresh.run_key=observation.last_run_key
        WHERE (p_source IS NULL OR observation.source_key=p_source)
          AND (p_run IS NULL OR observation.last_run_key=p_run)
          AND (p_date_scope='all'
               OR (p_date_scope='upcoming' AND coalesce(event.end_at,event.start_at)>statement_timestamp())
               OR (p_date_scope='past' AND coalesce(event.end_at,event.start_at)<=statement_timestamp()))
          AND (p_price_status='all' OR event.price_status=p_price_status)
          AND NOT public.fn_ingestion_admin_source_is_fixture(source.source_key,source.publisher,source.seed_url)
          AND NOT public.fn_ingestion_admin_run_is_fixture(refresh.run_key,refresh.error)
          AND (p_query IS NULL OR strpos(lower(concat_ws(' ',event.title,event.venue_name,event.city_norm,
              event.description,event.organizer_name,array_to_string(event.host_names,' '),
              array_to_string(event.speaker_names,' '),array_to_string(event.partner_names,' '))),lower(p_query))>0)
    ), eligible AS MATERIALIZED (
        SELECT DISTINCT ON (canonical_event_id) * FROM matching
        ORDER BY canonical_event_id, last_seen_at DESC,
                 source_key, observation_source, source_event_id
    ), contributions AS MATERIALIZED (
        SELECT source_key, source_display_name, count(DISTINCT canonical_event_id) AS events
        FROM matching GROUP BY source_key,source_display_name
    ), page AS MATERIALIZED (
        SELECT * FROM eligible
        WHERE p_after IS NULL OR (start_at,canonical_event_id)>(p_after,p_after_id)
        ORDER BY start_at,canonical_event_id LIMIT p_limit+1
    ), shown AS MATERIALIZED (
        SELECT * FROM page ORDER BY start_at,canonical_event_id LIMIT p_limit
    )
    SELECT jsonb_build_object(
        'items',COALESCE((SELECT jsonb_agg(to_jsonb(shown)-'observation_source' ORDER BY start_at,canonical_event_id) FROM shown),'[]'::jsonb),
        'generated_at',statement_timestamp(),
        'total',(SELECT count(*) FROM eligible), 'limit',p_limit,
        'upcoming_total',(SELECT count(*) FROM eligible WHERE coalesce(end_at,start_at)>statement_timestamp()),
        'source_count',(SELECT count(*) FROM contributions),
        'source_counts_truncated',(SELECT count(*)>500 FROM contributions),
        'source_counts',COALESCE((SELECT jsonb_agg(to_jsonb(distribution) ORDER BY events DESC,source_key)
            FROM (SELECT * FROM contributions ORDER BY events DESC,source_key LIMIT 500) distribution),'[]'::jsonb),
        'has_more',(SELECT count(*)>p_limit FROM page),
        'next_start_at',CASE WHEN (SELECT count(*)>p_limit FROM page) THEN
            (SELECT start_at FROM shown ORDER BY start_at DESC,canonical_event_id DESC LIMIT 1) END,
        'next_canonical_event_id',CASE WHEN (SELECT count(*)>p_limit FROM page) THEN
            (SELECT canonical_event_id FROM shown ORDER BY start_at DESC,canonical_event_id DESC LIMIT 1) END,
        'query',p_query,'source_key',p_source,'run_key',p_run,
        'date_scope',p_date_scope,'price_status',p_price_status
    ) INTO v_result;
    RETURN v_result;
END $$;
"""
