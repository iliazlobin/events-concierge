"""Bounded global run drilldowns and exact run lookup for operator viewers.

Revision ID: 0184
Revises: 0183
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0184"
down_revision: str | None = "0183"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_EVIDENCE)
    op.execute(_QUERY)
    op.execute(_LOOKUP)
    for signature in (
        "fn_operator_run_evidence_v1(jsonb,boolean)",
        "fn_query_ingestion_admin_runs_v1(jsonb)",
        "fn_lookup_ingestion_admin_run_v1(text,text,boolean)",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC,ec_app,ec_operator_viewer,ec_operator_controller,ec_ingestion_executor"
        )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_query_ingestion_admin_runs_v1(jsonb),public.fn_lookup_ingestion_admin_run_v1(text,text,boolean) TO ec_operator_viewer"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION public.fn_query_ingestion_admin_runs_v1(jsonb)")
    op.execute("DROP FUNCTION public.fn_lookup_ingestion_admin_run_v1(text,text,boolean)")
    op.execute("DROP FUNCTION public.fn_operator_run_evidence_v1(jsonb,boolean)")


# This private helper accepts only already-normalized facts from the two fixed public reads.
# It enriches the selected page, never pages the old projection and sorts that partial result.
_EVIDENCE = """
CREATE FUNCTION public.fn_operator_run_evidence_v1(f jsonb,p_fixtures boolean)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT (f-'publisher'-'region'-'stage_duration'-'ordinal')||jsonb_build_object(
   'is_latest_for_source',NOT EXISTS(SELECT 1 FROM public.fn_ingestion_admin_run_facts_v2(p_fixtures,f->>'source_key',NULL) n
     WHERE (n.started_at,n.run_key)>((f->>'started_at')::timestamptz,f->>'run_key')),
   'resolved_by_newer_success',(f->>'status'='failed' AND EXISTS(
     SELECT 1 FROM public.fn_ingestion_admin_run_facts_v2(p_fixtures,f->>'source_key',NULL) n
     WHERE n.status='succeeded' AND (n.started_at,n.run_key)>((f->>'started_at')::timestamptz,f->>'run_key'))),
   'reviewed_at',s.reviewed_at,'review_expires_at',s.review_expires_at,
   'refresh_interval_minutes',s.refresh_interval_minutes,'min_interval_ms',s.min_interval_ms,'page_limit',s.page_limit,
   'command_id',c.command_id,'command_action',c.action,'command_requested_at',c.requested_at,
   'command_started_at',c.started_at,'command_completed_at',c.completed_at,
   'execution_count',m.execution_count,'execution_wall_time_ms',m.wall_time_ms,'process_cpu_time_ms',m.process_cpu_time_ms,
   'rss_before_bytes',m.rss_first_bytes,'rss_after_bytes',m.rss_last_bytes,
   'boundary_observed_peak_rss_bytes',m.boundary_observed_peak_rss_bytes,
   'process_lifetime_peak_rss_bytes',m.process_lifetime_peak_rss_bytes,'measurement_source',m.measurement_source,
   'measurement_scope',m.measurement_scope,'measurement_quality',m.measurement_quality,
   'execution_last_outcome_code',m.last_outcome_code,'execution_first_observed_at',m.first_observed_at,
   'execution_last_observed_at',m.last_observed_at,
   'stage_metrics',COALESCE((SELECT jsonb_agg(jsonb_build_object('stage',sm.stage,'observation_count',sm.observation_count,
     'duration_ms',sm.duration_ms,'last_outcome_code',sm.last_outcome_code,'first_observed_at',sm.first_observed_at,
     'last_observed_at',sm.last_observed_at) ORDER BY sm.stage)
     FROM public.catalog_refresh_run_stage_metrics sm WHERE sm.source_key=f->>'source_key' AND sm.run_key=f->>'run_key'),'[]'))
 FROM (SELECT 1) anchor
 LEFT JOIN public.catalog_sources s ON s.source_key=f->>'source_key'
 LEFT JOIN LATERAL (
   SELECT command.* FROM public.ingestion_admin_commands command
   WHERE (command.action='refresh_source' AND command.source_key=f->>'source_key' AND f->>'run_key'='admin:'||command.command_id::text)
     OR EXISTS(SELECT 1 FROM public.ingestion_command_tasks task WHERE task.command_id=command.command_id
       AND task.source_key=f->>'source_key' AND task.run_key=f->>'run_key')
   ORDER BY command.requested_at DESC,command.command_id DESC LIMIT 1
 ) c ON true
 LEFT JOIN public.catalog_refresh_run_execution_metrics m ON m.source_key=f->>'source_key' AND m.run_key=f->>'run_key'
$$;
"""

_ORDER = """
 CASE WHEN v_sort='source' AND v_direction='asc' THEN COALESCE(display_name,source_key) END ASC NULLS LAST,
 CASE WHEN v_sort='source' AND v_direction='desc' THEN COALESCE(display_name,source_key) END DESC NULLS LAST,
 CASE WHEN v_sort='status' AND v_direction='asc' THEN status END ASC NULLS LAST,
 CASE WHEN v_sort='status' AND v_direction='desc' THEN status END DESC NULLS LAST,
 CASE WHEN v_sort='started' AND v_direction='asc' THEN started_at END ASC NULLS LAST,
 CASE WHEN v_sort='started' AND v_direction='desc' THEN started_at END DESC NULLS LAST,
 CASE WHEN v_direction='asc' THEN CASE v_sort WHEN 'duration' THEN duration_ms WHEN 'attempts' THEN attempt_count
   WHEN 'output' THEN canonical_count WHEN 'stage_duration' THEN stage_duration END END ASC NULLS LAST,
 CASE WHEN v_direction='desc' THEN CASE v_sort WHEN 'duration' THEN duration_ms WHEN 'attempts' THEN attempt_count
   WHEN 'output' THEN canonical_count WHEN 'stage_duration' THEN stage_duration END END DESC NULLS LAST,
 started_at DESC,source_key,run_key DESC
"""

_QUERY = """
CREATE FUNCTION public.fn_query_ingestion_admin_runs_v1(p jsonb)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE v_query text:=NULLIF(btrim(p->>'query'),''); v_sort text:=COALESCE(p->>'sort_by','started');
 v_direction text:=COALESCE(p->>'sort_direction','desc'); v_source text:=p->>'source_key';
 v_stage text:=p->>'stage'; v_outcome text:=p->>'stage_outcome'; v_status text:=p->>'status';
 v_after timestamptz; v_before timestamptz; v_window integer:=(p->>'window_hours')::integer;
 v_limit integer:=COALESCE((p->>'limit')::integer,50); v_offset integer:=COALESCE((p->>'offset')::integer,0);
 v_fixtures boolean:=COALESCE((p->>'include_fixtures')::boolean,false); v_result jsonb;
BEGIN
 IF p IS NULL OR jsonb_typeof(p)<>'object' OR octet_length(p::text)>4096
   OR EXISTS(SELECT 1 FROM jsonb_object_keys(p) key WHERE key NOT IN ('query','sort_by','sort_direction','source_key',
      'stage','stage_outcome','status','started_after','started_before','window_hours','limit','offset','include_fixtures','mode','publisher','region'))
   OR v_sort NOT IN ('source','status','started','duration','attempts','output','stage_duration')
   OR v_direction NOT IN ('asc','desc') OR v_limit NOT BETWEEN 1 AND 100 OR v_offset NOT BETWEEN 0 AND 100000
   OR (p->>'query' IS NOT NULL AND (length(p->>'query')>160 OR p->>'query' ~ '[[:cntrl:]]'))
   OR (v_source IS NOT NULL AND v_source !~ '^[a-z0-9][a-z0-9-]{1,79}$')
   OR (v_status IS NOT NULL AND v_status NOT IN ('running','paused','succeeded','failed'))
   OR (v_window IS NOT NULL AND v_window NOT BETWEEN 1 AND 2160)
   OR (v_stage IS NOT NULL AND v_stage NOT IN ('admission','collect','extract_enrich','normalize_dedupe','catalog_publish'))
   OR (v_outcome IS NOT NULL AND v_outcome<>'failed') OR ((v_outcome IS NOT NULL OR v_sort='stage_duration') AND v_stage IS NULL)
   OR EXISTS(SELECT 1 FROM jsonb_each_text(p) e WHERE e.key IN ('mode','publisher','region') AND e.value IS NOT NULL
       AND (length(e.value) NOT BETWEEN 1 AND CASE e.key WHEN 'mode' THEN 80 WHEN 'region' THEN 120 ELSE 300 END OR e.value ~ '[[:cntrl:]]'))
   OR ((p->>'started_after' IS NULL)<>(p->>'started_before' IS NULL)) THEN
   RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid operator run query'; END IF;
 IF p->>'started_after' IS NOT NULL THEN
   IF p->>'started_after' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' OR p->>'started_before' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
     RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='run interval requires timezone'; END IF;
   v_after:=(p->>'started_after')::timestamptz; v_before:=(p->>'started_before')::timestamptz;
   IF NOT isfinite(v_after) OR NOT isfinite(v_before) OR v_before<=v_after OR v_before-v_after>interval '90 days' THEN
     RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid operator run interval'; END IF;
 ELSE v_after:=statement_timestamp()-make_interval(hours=>v_window); END IF;
 WITH filtered AS MATERIALIZED (
   SELECT f.*,sm.duration_ms AS stage_duration
   FROM public.fn_ingestion_admin_run_facts_v2(v_fixtures,v_source,v_after) f
   LEFT JOIN public.catalog_refresh_run_stage_metrics sm ON sm.source_key=f.source_key AND sm.run_key=f.run_key AND sm.stage=v_stage
   WHERE (v_status IS NULL OR f.status=v_status) AND (v_before IS NULL OR f.started_at<v_before)
     AND (p->>'mode' IS NULL OR f.mode=p->>'mode') AND (p->>'publisher' IS NULL OR f.publisher=p->>'publisher')
     AND (p->>'region' IS NULL OR f.region=p->>'region')
     AND (v_query IS NULL OR strpos(lower(concat_ws(' ',f.source_key,f.display_name,f.run_key,f.error)),lower(v_query))>0)
     AND (v_stage IS NULL OR sm.stage IS NOT NULL) AND (v_outcome IS NULL OR sm.last_outcome_code=v_outcome)
 ), ranked AS (
   SELECT filtered.*,row_number() OVER (ORDER BY __ORDER__) AS ordinal FROM filtered
 ), page AS (SELECT * FROM ranked ORDER BY ordinal LIMIT v_limit OFFSET v_offset)
 SELECT jsonb_build_object('items',COALESCE((SELECT jsonb_agg(public.fn_operator_run_evidence_v1(to_jsonb(page),v_fixtures)
   ORDER BY ordinal) FROM page),'[]'),'total',(SELECT count(*) FROM filtered),'limit',v_limit,'offset',v_offset) INTO v_result;
 RETURN v_result;
END $$;
""".replace("__ORDER__", _ORDER)

_LOOKUP = """
CREATE FUNCTION public.fn_lookup_ingestion_admin_run_v1(p_source text,p_run text,p_fixtures boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE v_fact jsonb;
BEGIN
 IF p_source IS NULL OR p_source !~ '^[a-z0-9][a-z0-9-]{1,79}$' OR p_run IS NULL
   OR length(p_run) NOT BETWEEN 1 AND 256 OR p_run ~ '[[:cntrl:]]' OR p_fixtures IS NULL THEN
   RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid operator run identity'; END IF;
 SELECT to_jsonb(f) INTO v_fact FROM public.fn_ingestion_admin_run_facts_v2(p_fixtures,p_source,NULL) f WHERE f.run_key=p_run;
 IF v_fact IS NULL THEN RETURN NULL; END IF;
 RETURN public.fn_operator_run_evidence_v1(v_fact,p_fixtures);
END $$;
"""
