"""Freeze command source plans and retain fenced attempts and structured progress.

Revision ID: 0183
Revises: 0182
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0183"
down_revision: str | None = "0182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_TRIGGER)
    op.execute(_EXECUTE)
    op.execute(_READ)
    op.execute(_RUN_EVIDENCE)
    op.execute(_CHILD_PROJECTION)
    # Retain all existing source-retirement and lease guards while making a yielded fleet
    # continuation take its new position behind commands that have already been waiting.
    definition = (
        op.get_bind()
        .execute(
            text(
                "SELECT pg_get_functiondef('public.fn_claim_ingestion_admin_commands_v2(integer,integer,text,text)'::regprocedure)"
            )
        )
        .scalar_one()
    )
    old_order = "ORDER BY command.requested_at, command.command_id"
    if definition.count(old_order) != 1:
        raise RuntimeError("command claim ordering changed; review fairness migration")
    op.execute(
        definition.replace(
            old_order, "ORDER BY command.available_at, command.requested_at, command.command_id"
        )
    )
    for signature in (
        "fn_command_attempt_observed_v1()",
        "fn_command_execution_v1(uuid,integer,uuid,text,jsonb)",
        "fn_get_command_investigation_v1(uuid,bigint,integer)",
        "fn_get_command_run_evidence_v1(uuid,text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC, ec_app")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_command_execution_v1(uuid,integer,uuid,text,jsonb) TO ec_ingestion_executor"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_get_command_investigation_v1(uuid,bigint,integer) TO ec_operator_viewer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.fn_get_command_run_evidence_v1(uuid,text) TO ec_operator_viewer"
    )


def downgrade() -> None:
    raise RuntimeError(
        "0183 retains operational evidence; downgrade requires an explicit preservation plan"
    )


_TABLES = """
CREATE TABLE public.ingestion_command_plans (
 command_id uuid PRIMARY KEY REFERENCES public.ingestion_admin_commands(command_id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 adopted boolean NOT NULL DEFAULT false
);
CREATE TABLE public.ingestion_command_tasks (
 command_id uuid NOT NULL REFERENCES public.ingestion_command_plans(command_id),
 position integer NOT NULL CHECK(position BETWEEN 0 AND 499),
 source_key text NOT NULL REFERENCES public.catalog_sources(source_key),
 run_key text NOT NULL CHECK(run_key ~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN
   ('pending','running','succeeded','already_succeeded','queued','skipped','failed','deferred','busy')),
 attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
 parent_attempt integer,
 available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 started_at timestamptz, completed_at timestamptz, last_progress_at timestamptz,
 last_outcome_code text, candidate_count integer NOT NULL DEFAULT 0 CHECK(candidate_count>=0),
 canonical_count integer NOT NULL DEFAULT 0 CHECK(canonical_count>=0 AND canonical_count<=candidate_count),
 PRIMARY KEY(command_id,source_key,run_key), UNIQUE(command_id,position), UNIQUE(command_id,source_key)
);
CREATE TABLE public.ingestion_command_attempts (
 command_id uuid NOT NULL REFERENCES public.ingestion_admin_commands(command_id),
 attempt_count integer NOT NULL,
 kind text NOT NULL CHECK(kind IN ('initial','continuation','retry','reclaim','adopted')),
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 claimed_at timestamptz, last_heartbeat_at timestamptz, last_progress_at timestamptz,
 ended_at timestamptz, outcome_code text, worker_id text, release_revision text, image_digest text,
 PRIMARY KEY(command_id,attempt_count)
);
CREATE TABLE public.ingestion_command_events (
 event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 deduplication_id uuid UNIQUE,
 command_id uuid NOT NULL REFERENCES public.ingestion_admin_commands(command_id),
 command_attempt integer NOT NULL,
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_code text NOT NULL,
 source_key text, run_key text, task_attempt integer,
 stage text, outcome_code text, duration_ms bigint, candidate_count integer, canonical_count integer,
 request_count integer, page_count integer,
 error_type text, worker_id text, release_revision text, image_digest text
);
CREATE INDEX ingestion_command_events_cursor ON public.ingestion_command_events(command_id,event_id);
REVOKE ALL ON TABLE public.ingestion_command_plans,public.ingestion_command_tasks,
 public.ingestion_command_attempts,public.ingestion_command_events FROM PUBLIC,ec_app,
 ec_operator_viewer,ec_operator_controller,ec_ingestion_executor;
REVOKE ALL ON SEQUENCE public.ingestion_command_events_event_id_seq FROM PUBLIC,ec_app,
 ec_operator_viewer,ec_operator_controller,ec_ingestion_executor;
"""

_TRIGGER = """
CREATE FUNCTION public.fn_command_attempt_observed_v1() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE v_kind text; v_now timestamptz := clock_timestamp();
BEGIN
 IF NEW.attempt_count > OLD.attempt_count THEN
   v_kind := CASE WHEN OLD.attempt_count=0 THEN 'initial'
     WHEN OLD.status='running' THEN 'reclaim'
     WHEN (SELECT t.status FROM public.ingestion_command_tasks t WHERE t.command_id=NEW.command_id
       AND t.status IN ('pending','running','deferred','busy') AND t.available_at<=v_now
       ORDER BY t.position LIMIT 1) IN ('deferred','busy') THEN 'retry'
     ELSE 'continuation' END;
   IF OLD.status='running' THEN
     UPDATE public.ingestion_command_attempts SET ended_at=COALESCE(ended_at,v_now),outcome_code='lease_expired'
       WHERE command_id=NEW.command_id AND attempt_count=OLD.attempt_count;
   END IF;
   INSERT INTO public.ingestion_command_attempts(command_id,attempt_count,kind,claimed_at,
      last_heartbeat_at,release_revision,image_digest)
     VALUES(NEW.command_id,NEW.attempt_count,v_kind,v_now,v_now,NEW.executor_release_revision,NEW.executor_image_digest);
   INSERT INTO public.ingestion_command_events(command_id,command_attempt,event_code,outcome_code)
     VALUES(NEW.command_id,NEW.attempt_count,'command_claimed',v_kind);
 ELSIF NEW.attempt_count>0 THEN
   IF NEW.status='running' AND NEW.lease_expires_at IS DISTINCT FROM OLD.lease_expires_at THEN
     UPDATE public.ingestion_command_attempts SET last_heartbeat_at=v_now
       WHERE command_id=NEW.command_id AND attempt_count=NEW.attempt_count;
   END IF;
   IF OLD.status='running' AND NEW.status<>'running' THEN
     UPDATE public.ingestion_command_attempts SET ended_at=v_now,outcome_code=
       CASE WHEN NEW.status='queued' THEN 'yielded' ELSE NEW.status END
       WHERE command_id=NEW.command_id AND attempt_count=NEW.attempt_count;
     INSERT INTO public.ingestion_command_events(command_id,command_attempt,event_code,outcome_code)
       VALUES(NEW.command_id,NEW.attempt_count,'command_released',
         CASE WHEN NEW.status='queued' THEN 'yielded' ELSE NEW.status END);
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER command_attempt_observed AFTER UPDATE ON public.ingestion_admin_commands
 FOR EACH ROW EXECUTE FUNCTION public.fn_command_attempt_observed_v1();
"""

_EXECUTE = """
CREATE FUNCTION public.fn_command_execution_v1(p_id uuid,p_attempt integer,p_token uuid,p_operation text,p jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE c public.ingestion_admin_commands; t public.ingestion_command_tasks;
 v_now timestamptz:=clock_timestamp(); v_targets jsonb; v_adopted boolean:=false; v_count integer;
 v_outcome text; v_event text; v_tasks jsonb; v_source text; v_run text; v_task_attempt integer;
BEGIN
 IF p_id IS NULL OR p_attempt IS NULL OR p_attempt<1 OR p_token IS NULL OR p_operation IS NULL
   OR p_operation NOT IN ('get_plan','ensure_plan','start_task','finish_task','record_event')
   OR p IS NULL OR jsonb_typeof(p)<>'object' OR octet_length(p::text)>131072 THEN
   RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid command execution operation';
 END IF;
 SELECT * INTO c FROM public.ingestion_admin_commands WHERE command_id=p_id FOR UPDATE;
 v_now:=clock_timestamp();
 IF NOT FOUND OR c.status<>'running' OR c.attempt_count<>p_attempt OR c.lease_token IS DISTINCT FROM p_token
   OR c.lease_expires_at IS NULL
   OR (c.lease_expires_at<=v_now AND NOT(p_operation='record_event' AND COALESCE(p->>'event_code','') IN
     ('lease_lost','lease_renewal_error'))) THEN RETURN jsonb_build_object('lease_lost',true); END IF;
 INSERT INTO public.ingestion_command_attempts(command_id,attempt_count,kind,release_revision,image_digest)
 VALUES(p_id,p_attempt,'adopted',c.executor_release_revision,c.executor_image_digest) ON CONFLICT DO NOTHING;

 IF p_operation IN ('get_plan','ensure_plan') THEN
   IF NOT EXISTS(SELECT 1 FROM public.ingestion_command_plans WHERE command_id=p_id) THEN
     SELECT jsonb_agg(jsonb_build_object('position',l.position,'source_key',l.source_key,'run_key',l.run_key)
          ORDER BY l.position) INTO v_targets FROM public.ingestion_admin_command_runs l
       WHERE l.command_id=p_id AND l.command_attempt=(SELECT max(command_attempt)
         FROM public.ingestion_admin_command_runs WHERE command_id=p_id);
     v_adopted:=v_targets IS NOT NULL;
     IF v_targets IS NULL AND p_operation='ensure_plan' THEN v_targets:=p->'targets'; END IF;
     IF v_targets IS NOT NULL THEN
       IF jsonb_typeof(v_targets)<>'array' OR jsonb_array_length(v_targets)>500 THEN
         RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid command plan'; END IF;
       IF EXISTS(SELECT 1 FROM jsonb_array_elements(v_targets) x WHERE
          jsonb_typeof(x)<>'object' OR NOT(x ?& ARRAY['position','source_key','run_key']) OR
          x->>'position' IS NULL OR x->>'source_key' IS NULL OR x->>'run_key' IS NULL OR
          (x->>'source_key') !~ '^[a-z0-9][a-z0-9-]{1,79}$' OR
          (x->>'run_key') !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$') THEN
         RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid command plan target'; END IF;
       IF c.action='refresh_source' AND (jsonb_array_length(v_targets)<>1 OR
          v_targets->0->>'source_key' IS DISTINCT FROM c.source_key OR
          v_targets->0->>'run_key' IS DISTINCT FROM 'admin:'||p_id::text OR
          (v_targets->0->>'position')::integer<>0) THEN
         RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='source command plan does not match intent'; END IF;
       IF c.action='refresh_due' AND EXISTS(SELECT 1 FROM jsonb_array_elements(v_targets) x
          WHERE left(x->>'run_key',length('cadence:'||(x->>'source_key')||':'))<>'cadence:'||(x->>'source_key')||':') THEN
         RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='fleet plan requires source cadence identity'; END IF;
       IF EXISTS(SELECT 1 FROM jsonb_array_elements(v_targets) x
          WHERE (x->>'position')::integer NOT BETWEEN 0 AND jsonb_array_length(v_targets)-1) THEN
         RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='command plan positions must be contiguous'; END IF;
       INSERT INTO public.ingestion_command_plans(command_id,adopted) VALUES(p_id,v_adopted);
       INSERT INTO public.ingestion_command_tasks(command_id,position,source_key,run_key,status,last_outcome_code,
           candidate_count,canonical_count,completed_at)
         SELECT p_id,(x->>'position')::integer,x->>'source_key',x->>'run_key',
           CASE WHEN v_adopted AND r.status='succeeded' THEN 'already_succeeded' ELSE 'pending' END,
           CASE WHEN v_adopted AND r.status='succeeded' THEN 'already_succeeded' END,
           CASE WHEN v_adopted THEN COALESCE(r.candidate_count,0) ELSE 0 END,
           CASE WHEN v_adopted THEN COALESCE(r.canonical_count,0) ELSE 0 END,
           CASE WHEN v_adopted AND r.status='succeeded' THEN r.completed_at END
         FROM jsonb_array_elements(v_targets) x LEFT JOIN public.catalog_refresh_runs r
           ON r.source_key=x->>'source_key' AND r.run_key=x->>'run_key';
       INSERT INTO public.ingestion_command_events(command_id,command_attempt,event_code)
         VALUES(p_id,p_attempt,CASE WHEN v_adopted THEN 'plan_adopted' ELSE 'plan_created' END);
     ELSIF p_operation='ensure_plan' THEN
       RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='command plan targets are required';
     END IF;
   END IF;
   v_now:=clock_timestamp();
   SELECT COALESCE(jsonb_agg((to_jsonb(x)-'command_id'-'parent_attempt')||jsonb_build_object(
       'retry_after_seconds',GREATEST(0,ceil(extract(epoch FROM(x.available_at-v_now))))::integer)
       ORDER BY x.position),'[]') INTO v_tasks
     FROM public.ingestion_command_tasks x WHERE x.command_id=p_id;
   RETURN jsonb_build_object('planned',EXISTS(SELECT 1 FROM public.ingestion_command_plans WHERE command_id=p_id),'tasks',v_tasks);
 END IF;

 v_source:=p->>'source_key'; v_run:=p->>'run_key'; v_task_attempt:=(p->>'task_attempt')::integer;
 IF p_operation IN ('start_task','finish_task') THEN
   SELECT * INTO t FROM public.ingestion_command_tasks WHERE command_id=p_id AND source_key=v_source AND run_key=v_run FOR UPDATE;
   IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='command task not in fixed plan'; END IF;
 END IF;
 IF p_operation='start_task' THEN
   IF t.status IN ('succeeded','already_succeeded','queued','skipped','failed') OR t.available_at>v_now
      OR (t.status='running' AND t.parent_attempt=p_attempt) THEN RETURN jsonb_build_object('task',NULL); END IF;
   IF p->>'worker_id' IS NULL OR (p->>'worker_id') !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,159}$'
      OR p->>'release_revision' IS NULL OR (p->>'release_revision') !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
      OR (p->>'image_digest' IS NOT NULL AND p->>'image_digest' !~ '^sha256:[0-9a-f]{64}$') THEN
      RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid execution identity'; END IF;
   UPDATE public.ingestion_command_tasks SET status='running',attempt_count=attempt_count+1,parent_attempt=p_attempt,
      started_at=v_now,completed_at=NULL,last_outcome_code=NULL,last_progress_at=v_now
      WHERE command_id=p_id AND source_key=v_source AND run_key=v_run RETURNING * INTO t;
   UPDATE public.ingestion_command_attempts SET worker_id=p->>'worker_id',release_revision=p->>'release_revision',
      image_digest=p->>'image_digest',last_progress_at=v_now WHERE command_id=p_id AND attempt_count=p_attempt;
   INSERT INTO public.ingestion_command_events(command_id,command_attempt,event_code,source_key,run_key,task_attempt,
      worker_id,release_revision,image_digest) VALUES(p_id,p_attempt,'task_started',v_source,v_run,t.attempt_count,
      p->>'worker_id',p->>'release_revision',p->>'image_digest');
   RETURN jsonb_build_object('task',(to_jsonb(t)-'command_id'-'parent_attempt')||jsonb_build_object(
     'retry_after_seconds',GREATEST(0,ceil(extract(epoch FROM(t.available_at-v_now))))::integer));
 END IF;
 IF p_operation='finish_task' THEN
   IF t.status<>'running' OR t.parent_attempt IS DISTINCT FROM p_attempt OR t.attempt_count IS DISTINCT FROM v_task_attempt THEN RETURN jsonb_build_object('updated',false); END IF;
   v_outcome:=p->>'outcome_code';
   IF v_outcome IS NULL OR v_outcome NOT IN ('succeeded','already_succeeded','queued','skipped','failed','deferred','busy','progressed','retry_exhausted')
      OR COALESCE((p->>'candidate_count')::integer,0)<0 OR COALESCE((p->>'canonical_count')::integer,0)<0
      OR COALESCE((p->>'canonical_count')::integer,0)>COALESCE((p->>'candidate_count')::integer,0)
      OR COALESCE((p->>'retry_after_seconds')::double precision,1) NOT BETWEEN 0 AND 86400 THEN
      RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid command task result'; END IF;
   UPDATE public.ingestion_command_tasks SET status=CASE WHEN v_outcome='progressed' THEN 'deferred' WHEN v_outcome='retry_exhausted' THEN 'failed' ELSE v_outcome END,
      last_outcome_code=v_outcome,candidate_count=COALESCE((p->>'candidate_count')::integer,0),
      canonical_count=COALESCE((p->>'canonical_count')::integer,0),last_progress_at=v_now,
      available_at=v_now+make_interval(secs=>GREATEST(1,COALESCE((p->>'retry_after_seconds')::double precision,1))),
      completed_at=CASE WHEN v_outcome NOT IN ('deferred','busy','progressed') THEN v_now END
      WHERE command_id=p_id AND source_key=v_source AND run_key=v_run;
   UPDATE public.ingestion_command_attempts SET last_progress_at=v_now WHERE command_id=p_id AND attempt_count=p_attempt;
   INSERT INTO public.ingestion_command_events(command_id,command_attempt,event_code,source_key,run_key,task_attempt,
      outcome_code,candidate_count,canonical_count) VALUES(p_id,p_attempt,'task_finished',v_source,v_run,t.attempt_count,
      v_outcome,COALESCE((p->>'candidate_count')::integer,0),COALESCE((p->>'canonical_count')::integer,0));
   RETURN jsonb_build_object('updated',true);
 END IF;

 v_event:=p->>'event_code';
 IF v_event IS NULL OR v_event NOT IN ('stage_started','stage_completed','progress','error','lease_lost','lease_renewal_error',
     'command_started','lease_renewed','continuation_scheduled','retry_scheduled','execution_error')
   OR EXISTS(SELECT 1 FROM jsonb_object_keys(p) k WHERE k NOT IN ('event_id','event_code','source_key','run_key',
     'task_attempt','stage','outcome_code','duration_ms','candidate_count','canonical_count','request_count','page_count','error_type','worker_id','release_revision','image_digest'))
   OR (p->>'stage' IS NOT NULL AND p->>'stage' NOT IN ('admission','collect','extract_enrich','normalize_dedupe','catalog_publish'))
   OR (p->>'outcome_code' IS NOT NULL AND p->>'outcome_code' NOT IN
      ('started','succeeded','failed','deferred','skipped','busy','already_succeeded','progressed','queued','retry_exhausted'))
   OR (p->>'error_type' IS NOT NULL AND p->>'error_type' NOT IN ('timeout','network','rate_limited','access_denied','validation','internal'))
   OR (p->>'worker_id' IS NOT NULL AND p->>'worker_id' !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,159}$')
   OR (p->>'release_revision' IS NOT NULL AND p->>'release_revision' !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
   OR (p->>'image_digest' IS NOT NULL AND p->>'image_digest' !~ '^sha256:[0-9a-f]{64}$')
   OR COALESCE((p->>'duration_ms')::bigint,0)<0 OR COALESCE((p->>'candidate_count')::integer,0)<0
   OR COALESCE((p->>'canonical_count')::integer,0)<0 OR COALESCE((p->>'request_count')::integer,0)<0
   OR COALESCE((p->>'page_count')::integer,0)<0 THEN
   RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid structured command event'; END IF;
 IF v_source IS NOT NULL OR v_run IS NOT NULL THEN
   SELECT * INTO t FROM public.ingestion_command_tasks WHERE command_id=p_id AND source_key=v_source AND run_key=v_run;
   IF NOT FOUND OR t.status<>'running' OR t.parent_attempt IS DISTINCT FROM p_attempt
      OR t.attempt_count IS DISTINCT FROM v_task_attempt THEN RETURN jsonb_build_object('updated',false); END IF;
 ELSIF v_event IN ('stage_started','stage_completed','progress') THEN
   RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='source progress requires a running task';
 END IF;
 INSERT INTO public.ingestion_command_events(deduplication_id,command_id,command_attempt,event_code,source_key,run_key,
   task_attempt,stage,outcome_code,duration_ms,candidate_count,canonical_count,request_count,page_count,error_type,worker_id,release_revision,image_digest)
 VALUES((p->>'event_id')::uuid,p_id,p_attempt,v_event,v_source,v_run,v_task_attempt,p->>'stage',p->>'outcome_code',
   (p->>'duration_ms')::bigint,(p->>'candidate_count')::integer,(p->>'canonical_count')::integer,
   (p->>'request_count')::integer,(p->>'page_count')::integer,p->>'error_type',
   p->>'worker_id',p->>'release_revision',p->>'image_digest') ON CONFLICT(deduplication_id) DO NOTHING;
 GET DIAGNOSTICS v_count=ROW_COUNT;
 IF v_count>0 AND v_event IN ('stage_started','stage_completed','progress') THEN
   UPDATE public.ingestion_command_attempts SET last_progress_at=v_now WHERE command_id=p_id AND attempt_count=p_attempt;
   UPDATE public.ingestion_command_tasks SET last_progress_at=v_now WHERE command_id=p_id AND source_key=v_source AND run_key=v_run;
 END IF;
 RETURN jsonb_build_object('updated',true);
END $$;
"""

_READ = """
CREATE FUNCTION public.fn_get_command_investigation_v1(p_id uuid,p_after bigint DEFAULT 0,p_limit integer DEFAULT 100)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE c public.ingestion_admin_commands; v_events jsonb; v_next bigint; v_more boolean;
BEGIN
 IF p_after IS NULL OR p_after<0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 THEN
   RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid investigation cursor'; END IF;
 SELECT * INTO c FROM public.ingestion_admin_commands WHERE command_id=p_id;
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF p_after=0 THEN
   -- Open at the live tail; operators need not drain hours of history before seeing progress.
   SELECT COALESCE(jsonb_agg(to_jsonb(e)-'deduplication_id'-'command_id' ORDER BY e.event_id),'[]'),max(e.event_id)
     INTO v_events,v_next FROM (SELECT * FROM public.ingestion_command_events WHERE command_id=p_id
       ORDER BY event_id DESC LIMIT p_limit) e;
 ELSE
   SELECT COALESCE(jsonb_agg(to_jsonb(e)-'deduplication_id'-'command_id' ORDER BY e.event_id),'[]'),max(e.event_id)
     INTO v_events,v_next FROM (SELECT * FROM public.ingestion_command_events WHERE command_id=p_id AND event_id>p_after
       ORDER BY event_id LIMIT p_limit) e;
 END IF;
 SELECT EXISTS(SELECT 1 FROM public.ingestion_command_events WHERE command_id=p_id AND event_id>COALESCE(v_next,p_after)) INTO v_more;
 RETURN jsonb_build_object('generated_at',statement_timestamp(),'command_id',p_id,
   'plan',jsonb_build_object('status',COALESCE((SELECT CASE WHEN adopted THEN 'adopted' ELSE 'fixed' END
     FROM public.ingestion_command_plans WHERE command_id=p_id),'unplanned'),
     'created_at',(SELECT created_at FROM public.ingestion_command_plans WHERE command_id=p_id),
     'tasks',COALESCE((SELECT jsonb_agg((to_jsonb(t)-'command_id'-'parent_attempt')||jsonb_build_object('lease_state',
       CASE WHEN t.status<>'running' THEN 'not_running' WHEN t.parent_attempt=c.attempt_count AND c.status='running'
         AND c.lease_expires_at>statement_timestamp() THEN 'live' ELSE 'expired' END) ORDER BY t.position)
       FROM public.ingestion_command_tasks t WHERE command_id=p_id),'[]')),
   'attempts',COALESCE((SELECT jsonb_agg(to_jsonb(a)-'command_id' ORDER BY a.attempt_count)
      FROM (SELECT * FROM public.ingestion_command_attempts WHERE command_id=p_id ORDER BY attempt_count DESC LIMIT 100) a),'[]'),
   'attempts_total',(SELECT count(*) FROM public.ingestion_command_attempts WHERE command_id=p_id),
   'attempts_truncated',(SELECT count(*)>100 FROM public.ingestion_command_attempts WHERE command_id=p_id),
   'events',v_events,'next_event_id',v_next::text,'has_more',v_more,
   'evidence',jsonb_build_object('events_since',(SELECT min(observed_at) FROM public.ingestion_command_events WHERE command_id=p_id),
      'history_complete',false,'logs','structured_events_only'));
END $$;
"""

_RUN_EVIDENCE = """
CREATE FUNCTION public.fn_get_command_run_evidence_v1(p_id uuid,p_source text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT to_jsonb(r)||jsonb_build_object(
   'trigger',CASE WHEN c.action='refresh_source' THEN 'admin_source' ELSE 'cadence_or_manual' END,
   'duration_ms',GREATEST(0,floor(extract(epoch FROM(COALESCE(r.completed_at,statement_timestamp())-r.started_at))*1000)::bigint),
   'mode',s.mode,'reviewed_at',s.reviewed_at,'review_expires_at',s.review_expires_at,
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
   'stage_metrics',COALESCE((SELECT jsonb_agg(jsonb_build_object('stage',stage.stage,
     'observation_count',stage.observation_count,'duration_ms',stage.duration_ms,
     'last_outcome_code',stage.last_outcome_code,'first_observed_at',stage.first_observed_at,
     'last_observed_at',stage.last_observed_at)) FROM public.catalog_refresh_run_stage_metrics stage
     WHERE stage.source_key=t.source_key AND stage.run_key=t.run_key),'[]'::jsonb))
 FROM public.ingestion_command_tasks t JOIN public.ingestion_admin_commands c ON c.command_id=t.command_id
 JOIN public.catalog_sources s ON s.source_key=t.source_key
 CROSS JOIN LATERAL public.fn_get_ingestion_admin_run(t.source_key,t.run_key) r
 LEFT JOIN public.catalog_refresh_run_execution_metrics m ON m.source_key=t.source_key AND m.run_key=t.run_key
 WHERE t.command_id=p_id AND t.source_key=p_source
$$;
"""

_CHILD_PROJECTION = """
CREATE OR REPLACE FUNCTION public.fn_list_ingestion_admin_command_runs_v1(p_command_id uuid)
RETURNS TABLE(run_position integer,source_key text,display_name text,run_key text,status text,phase text,
 linked_at timestamptz,started_at timestamptz,completed_at timestamptz,candidate_count integer,
 canonical_count integer,error_code text,attempt_count integer,duration_ms bigint,updated_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 WITH links AS (
   SELECT t.position,t.source_key,t.run_key,p.created_at AS linked_at
     FROM public.ingestion_command_tasks t JOIN public.ingestion_command_plans p USING(command_id)
     WHERE t.command_id=p_command_id
   UNION ALL
   SELECT l.position,l.source_key,l.run_key,l.linked_at
     FROM public.ingestion_admin_command_runs l JOIN public.ingestion_admin_commands c USING(command_id)
     WHERE l.command_id=p_command_id AND l.command_attempt=c.attempt_count
       AND NOT EXISTS(SELECT 1 FROM public.ingestion_command_plans WHERE command_id=p_command_id)
 )
 SELECT l.position,l.source_key,s.display_name,l.run_key,COALESCE(r.status,'pending'),
   CASE COALESCE(r.status,'pending') WHEN 'running' THEN 'collecting' WHEN 'paused' THEN 'deferred'
     WHEN 'succeeded' THEN 'completed' WHEN 'pending' THEN 'awaiting_dispatch' ELSE 'failed' END,
   l.linked_at,r.started_at,r.completed_at,r.candidate_count,r.canonical_count,r.error,r.attempt_count,
   CASE WHEN r.started_at IS NOT NULL THEN GREATEST(0,floor(extract(epoch FROM
     (COALESCE(r.completed_at,statement_timestamp())-r.started_at))*1000)::bigint) END,
   COALESCE(r.completed_at,r.started_at,l.linked_at)
 FROM links l JOIN public.catalog_sources s ON s.source_key=l.source_key
 LEFT JOIN LATERAL public.fn_get_ingestion_admin_run(l.source_key,l.run_key) r ON true
 ORDER BY l.position LIMIT 500
$$;
"""
