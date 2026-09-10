"""Aggregate registration dates of retained sources without exposing registry rows.

Revision ID: 0192
Revises: 0191

This is database registration history of the current retained registry, not historical enabled
state or a complete record of physically deleted sources. Paused and retired rows are included.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0192"
down_revision: str | None = "0191"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_get_operator_source_registration_history_v1(integer,boolean)"
_DEFINER = "ec_operator_aggregate_definer"


def upgrade() -> None:
    op.execute(_PROJECTION)
    op.execute(
        f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC, ec_app, ec_operator_viewer, ec_operator_controller, ec_ingestion_executor"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO ec_operator_viewer")
    # Reuse the existing restricted definer. Callers receive only fixed aggregates.
    op.execute(f"GRANT SELECT ON public.catalog_sources TO {_DEFINER}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.fn_ingestion_admin_source_is_fixture(text,text,text) TO {_DEFINER}"
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
    # Supporting privileges belong to the shared definer and other projections.


_PROJECTION = """
CREATE FUNCTION public.fn_get_operator_source_registration_history_v1(
    p_window_days integer DEFAULT 90, p_include_fixtures boolean DEFAULT false
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE
    v_now timestamptz := statement_timestamp();
    v_start timestamptz;
    v_result jsonb;
BEGIN
    IF p_window_days IS NULL OR p_window_days NOT IN (7,30,90)
       OR p_include_fixtures IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid source registration history query';
    END IF;
    v_start := v_now - p_window_days * INTERVAL '24 hours';
    IF EXISTS (
        SELECT 1 FROM public.catalog_sources source
        WHERE (p_include_fixtures OR NOT public.fn_ingestion_admin_source_is_fixture(
                source.source_key,source.publisher,source.seed_url))
          AND (source.created_at IS NULL OR NOT isfinite(source.created_at) OR source.created_at>v_now)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='22000', MESSAGE='source registration history unavailable';
    END IF;
    WITH registry AS MATERIALIZED (
        SELECT source.created_at FROM public.catalog_sources source
        WHERE p_include_fixtures OR NOT public.fn_ingestion_admin_source_is_fixture(
                source.source_key,source.publisher,source.seed_url)
    ), buckets AS (
        SELECT number,
               v_start+(number-1)*INTERVAL '24 hours' AS bucket_start,
               v_start+number*INTERVAL '24 hours' AS bucket_end
        FROM generate_series(1,p_window_days) number
    ), history AS (
        SELECT bucket_start,bucket_end,
               count(registry.created_at) FILTER (
                   WHERE registry.created_at<bucket_end
                      OR (number=p_window_days AND registry.created_at<=bucket_end)
               ) AS registered_sources,
               count(registry.created_at) FILTER (
                   WHERE registry.created_at>=bucket_start
                     AND (registry.created_at<bucket_end
                          OR (number=p_window_days AND registry.created_at<=bucket_end))
               ) AS added_sources
        FROM buckets LEFT JOIN registry ON true
        GROUP BY number,bucket_start,bucket_end
    )
    SELECT jsonb_build_object(
        'generated_at',v_now,'window_start',v_start,'window_days',p_window_days,'bucket_hours',24,
        'include_fixtures',p_include_fixtures,'history_scope','retained_registry',
        'baseline_sources',(SELECT count(*) FROM registry WHERE created_at<v_start),
        'total_sources',(SELECT count(*) FROM registry),
        'added_sources',(SELECT count(*) FROM registry WHERE created_at>=v_start),
        'items',(SELECT jsonb_agg(to_jsonb(history) ORDER BY bucket_start) FROM history)
    ) INTO v_result;
    RETURN v_result;
END $$;
"""
