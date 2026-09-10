"""Count and sort source catalog inventory with the operator Catalog predicates.

Revision ID: 0193
Revises: 0192

The legacy event_count retains its existing discovery-count semantics. New all/upcoming
counts include ongoing and cancelled records and always exclude fixture provenance,
matching the operator Catalog. Aggregation and sorting precede source pagination.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0193"
down_revision: str | None = "0192"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "public.fn_list_ingestion_admin_sources_v7(text,text,text,text,text,text,boolean,text,text,integer,integer)"
_DEFINER = "ec_operator_aggregate_definer"


def upgrade() -> None:
    op.execute(_PROJECTION)
    op.execute(
        f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC, ec_app, ec_operator_viewer, ec_operator_controller, ec_ingestion_executor"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO ec_operator_viewer")
    for table in (
        "catalog_sources",
        "catalog_event_observations",
        "canonical_events",
        "catalog_refresh_runs",
        "catalog_refresh_progress",
        "ingestion_admin_commands",
    ):
        op.execute(f"GRANT SELECT ON public.{table} TO {_DEFINER}")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_ingestion_admin_sources_base(boolean), "
        "public.fn_ingestion_admin_source_is_fixture(text,text,text), "
        f"public.fn_ingestion_admin_run_is_fixture(text,text) TO {_DEFINER}"
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
    # The definer is shared with other operator projections; retain supporting grants.


_ORDER = """
    CASE WHEN p_sort_by='source' AND p_sort_direction='asc' THEN lower(ranked.display_name) COLLATE "C" END ASC NULLS LAST,
    CASE WHEN p_sort_by='source' AND p_sort_direction='desc' THEN lower(ranked.display_name) COLLATE "C" END DESC NULLS LAST,
    CASE WHEN p_sort_by='health' AND p_sort_direction='asc' THEN ranked.health_rank END ASC NULLS LAST,
    CASE WHEN p_sort_by='health' AND p_sort_direction='desc' THEN ranked.health_rank END DESC NULLS LAST,
    CASE WHEN p_sort_by='catalog' AND p_sort_direction='asc' THEN ranked.upcoming_event_count END ASC NULLS LAST,
    CASE WHEN p_sort_by='catalog' AND p_sort_direction='desc' THEN ranked.upcoming_event_count END DESC NULLS LAST,
    CASE WHEN p_sort_by='catalog_total' AND p_sort_direction='asc' THEN ranked.total_event_count END ASC NULLS LAST,
    CASE WHEN p_sort_by='catalog_total' AND p_sort_direction='desc' THEN ranked.total_event_count END DESC NULLS LAST,
    CASE WHEN p_sort_by='last_success' AND p_sort_direction='asc' THEN ranked.last_succeeded_at END ASC NULLS LAST,
    CASE WHEN p_sort_by='last_success' AND p_sort_direction='desc' THEN ranked.last_succeeded_at END DESC NULLS LAST,
    CASE WHEN p_sort_by='latest_run' AND p_sort_direction='asc' THEN ranked.latest_run_rank END ASC NULLS LAST,
    CASE WHEN p_sort_by='latest_run' AND p_sort_direction='desc' THEN ranked.latest_run_rank END DESC NULLS LAST,
    CASE WHEN p_sort_by='latest_run' AND p_sort_direction='asc' THEN ranked.latest_run_started_at END ASC NULLS LAST,
    CASE WHEN p_sort_by='latest_run' AND p_sort_direction='desc' THEN ranked.latest_run_started_at END DESC NULLS LAST,
    CASE WHEN p_sort_by='output' AND p_sort_direction='asc' THEN ranked.latest_run_canonical_count END ASC NULLS LAST,
    CASE WHEN p_sort_by='output' AND p_sort_direction='desc' THEN ranked.latest_run_canonical_count END DESC NULLS LAST,
    CASE WHEN p_sort_by='output' AND p_sort_direction='asc' THEN ranked.latest_run_candidate_count END ASC NULLS LAST,
    CASE WHEN p_sort_by='output' AND p_sort_direction='desc' THEN ranked.latest_run_candidate_count END DESC NULLS LAST,
    ranked.source_key ASC
"""

_PROJECTION = f"""
CREATE FUNCTION public.fn_list_ingestion_admin_sources_v7(
    p_query text, p_state text, p_mode text, p_publisher text, p_region text,
    p_source_key text, p_include_fixtures boolean, p_sort_by text,
    p_sort_direction text, p_limit integer, p_offset integer
) RETURNS TABLE (
    source_key text, display_name text, publisher text, mode text, region text,
    seed_url text, enabled boolean, source_revision integer, review_status text,
    effective_status text, policy_blocked boolean, due boolean,
    last_succeeded_at timestamptz, next_due_at timestamptz, event_count bigint,
    latest_run_key text, latest_run_status text, latest_run_started_at timestamptz,
    latest_run_completed_at timestamptz, latest_run_candidate_count integer,
    latest_run_canonical_count integer, latest_run_error text, latest_run_attempt_count integer,
    latest_run_duration_ms bigint, latest_run_source_revision integer,
    latest_run_release_revision text, latest_run_image_digest text,
    latest_run_provenance_status text, latest_run_trigger text, total_count bigint,
    retired_at timestamptz, retired_reason text, superseded_by_source_key text,
    total_event_count bigint, upcoming_event_count bigint
) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF p_include_fixtures IS NULL
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
       OR p_offset IS NULL OR p_offset NOT BETWEEN 0 AND 100000
       OR (p_query IS NOT NULL AND char_length(p_query)>200)
       OR p_state IS NULL OR p_state NOT IN ('all','active','due','blocked','failed')
       OR p_sort_by IS NULL OR p_sort_by NOT IN ('source','health','catalog','catalog_total','last_success','latest_run','output')
       OR p_sort_direction IS NULL OR p_sort_direction NOT IN ('asc','desc')
       OR (p_mode IS NOT NULL AND (char_length(p_mode) NOT BETWEEN 1 AND 80 OR p_mode ~ '[[:cntrl:]]'))
       OR (p_publisher IS NOT NULL AND (char_length(p_publisher) NOT BETWEEN 1 AND 300 OR p_publisher ~ '[[:cntrl:]]'))
       OR (p_region IS NOT NULL AND (char_length(p_region) NOT BETWEEN 1 AND 120 OR p_region ~ '[[:cntrl:]]'))
       OR (p_source_key IS NOT NULL AND p_source_key !~ '^[a-z0-9][a-z0-9-]{{1,79}}$') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='ingestion admin source query is invalid';
    END IF;
    RETURN QUERY
    WITH filtered AS MATERIALIZED (
        SELECT source.*, registry.source_revision, registry.retired_at,
               registry.retired_reason, registry.superseded_by_source_key
        FROM public.fn_ingestion_admin_sources_base(p_include_fixtures) source
        JOIN public.catalog_sources registry ON registry.source_key=source.source_key
        WHERE (p_query IS NULL OR strpos(lower(source.source_key||' '||source.display_name||' '||source.publisher||' '||source.region||' '||source.mode||' '||source.seed_url),lower(p_query))>0)
          AND (p_source_key IS NULL OR source.source_key=p_source_key)
          AND (p_mode IS NULL OR source.mode=p_mode)
          AND (p_publisher IS NULL OR source.publisher=p_publisher)
          AND (p_region IS NULL OR source.region=p_region)
          AND (p_state='all'
               OR (p_state='active' AND source.effective_status IN ('active','due','running'))
               OR (p_state='due' AND source.due)
               OR (p_state='blocked' AND source.effective_status IN ('disabled','unreviewed','review_expired','policy_blocked'))
               OR (p_state='failed' AND source.latest_run_status='failed'))
    ), catalog_counts AS MATERIALIZED (
        -- Same inventory predicates as fn_get_operator_catalog_records_v1 with
        -- date_scope=all: source/run attribution first, distinct canonical IDs,
        -- unconditional fixture exclusion, including ongoing/cancelled records.
        SELECT observation.source_key,
               count(DISTINCT event.canonical_event_id) AS total_event_count,
               count(DISTINCT event.canonical_event_id) FILTER (
                   WHERE coalesce(event.end_at,event.start_at)>statement_timestamp()
               ) AS upcoming_event_count
        FROM filtered source
        JOIN public.catalog_event_observations observation ON observation.source_key=source.source_key
        JOIN public.canonical_events event USING (canonical_event_id)
        JOIN public.catalog_refresh_runs refresh
          ON refresh.source_key=observation.source_key AND refresh.run_key=observation.last_run_key
        WHERE NOT public.fn_ingestion_admin_source_is_fixture(source.source_key,source.publisher,source.seed_url)
          AND NOT public.fn_ingestion_admin_run_is_fixture(refresh.run_key,refresh.error)
        GROUP BY observation.source_key
    ), ranked AS (
        SELECT filtered.*, count(*) OVER () AS total_count,
               coalesce(counts.total_event_count,0)::bigint AS total_event_count,
               coalesce(counts.upcoming_event_count,0)::bigint AS upcoming_event_count,
               CASE filtered.effective_status
                   WHEN 'policy_blocked' THEN 0 WHEN 'review_expired' THEN 1
                   WHEN 'unreviewed' THEN 2 WHEN 'due' THEN 3 WHEN 'running' THEN 4
                   WHEN 'active' THEN 5 WHEN 'disabled' THEN 6 END AS health_rank,
               CASE filtered.latest_run_status
                   WHEN 'failed' THEN 0 WHEN 'paused' THEN 1 WHEN 'running' THEN 2
                   WHEN 'succeeded' THEN 3 ELSE NULL END AS latest_run_rank
        FROM filtered LEFT JOIN catalog_counts counts USING (source_key)
    ), page AS MATERIALIZED (
        SELECT ranked.* FROM ranked ORDER BY {_ORDER} LIMIT p_limit OFFSET p_offset
    ), enriched AS (
        SELECT page.*, command.command_id AS admin_command_id,
               coalesce(progress.source_revision,command.executor_source_revision,command.source_revision) AS effective_source_revision,
               command.executor_release_revision AS claim_release_revision,
               command.executor_image_digest AS claim_image_digest
        FROM page
        LEFT JOIN public.ingestion_admin_commands command
          ON command.action='refresh_source' AND command.source_key=page.source_key
         AND page.latest_run_key='admin:'||command.command_id::text
        LEFT JOIN public.catalog_refresh_progress progress
          ON progress.source_key=page.source_key AND progress.run_key=page.latest_run_key
    )
    SELECT enriched.source_key, enriched.display_name, enriched.publisher, enriched.mode,
           enriched.region, enriched.seed_url,
           CASE WHEN enriched.retired_at IS NULL THEN enriched.enabled ELSE false END,
           enriched.source_revision, enriched.review_status,
           CASE WHEN enriched.retired_at IS NULL THEN enriched.effective_status ELSE 'retired' END,
           enriched.policy_blocked,
           CASE WHEN enriched.retired_at IS NULL THEN enriched.due ELSE false END,
           enriched.last_succeeded_at,
           CASE WHEN enriched.retired_at IS NULL THEN enriched.next_due_at ELSE NULL END,
           enriched.event_count, enriched.latest_run_key, enriched.latest_run_status,
           enriched.latest_run_started_at, enriched.latest_run_completed_at,
           enriched.latest_run_candidate_count, enriched.latest_run_canonical_count,
           enriched.latest_run_error, enriched.latest_run_attempt_count,
           CASE WHEN enriched.latest_run_completed_at IS NULL THEN NULL ELSE
               greatest(0,floor(extract(epoch FROM enriched.latest_run_completed_at-enriched.latest_run_started_at)*1000)::bigint) END,
           enriched.effective_source_revision, enriched.claim_release_revision, enriched.claim_image_digest,
           CASE WHEN enriched.admin_command_id IS NOT NULL AND enriched.effective_source_revision IS NOT NULL AND enriched.claim_release_revision IS NOT NULL THEN 'claim_recorded'
                WHEN enriched.effective_source_revision IS NOT NULL THEN 'source_revision_only' ELSE 'legacy_unavailable' END,
           CASE WHEN enriched.admin_command_id IS NOT NULL THEN 'admin_source' ELSE 'cadence_or_manual' END,
           enriched.total_count, enriched.retired_at, enriched.retired_reason,
           enriched.superseded_by_source_key, enriched.total_event_count, enriched.upcoming_event_count
    FROM enriched ORDER BY {_ORDER.replace("ranked.", "enriched.")};
END $$;
"""
