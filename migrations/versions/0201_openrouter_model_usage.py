"""Durable OpenRouter accounting and application budgets; amounts start unset.

Revision ID: 0201
Revises: 0199
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0201"
down_revision: str | None = "0199"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFINER = "ec_model_usage_definer"
_FUNCTIONS = {
    "fn_begin_model_call_v1(uuid,text)": "ec_app",
    "fn_finish_model_call_v1(uuid,jsonb)": "ec_app",
    "fn_model_budget_v1()": "ec_operator_viewer",
    "fn_model_usage_report_v1(timestamptz,timestamptz,integer,text)": "ec_operator_viewer",
    "fn_update_model_budget_v1(jsonb,text)": "ec_operator_controller",
}


def upgrade() -> None:
    op.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{_DEFINER}') THEN
            CREATE ROLE {_DEFINER} NOLOGIN NOSUPERUSER NOBYPASSRLS;
        END IF;
    END $$""")
    op.execute(_TABLES)
    op.execute(_FUNCTION_SQL)
    owner = op.get_bind().dialect.identifier_preparer.quote(
        op.get_bind().execute(text("SELECT current_user")).scalar_one()
    )
    op.execute(f"GRANT SELECT,INSERT,UPDATE ON public.model_usage_calls TO {_DEFINER}")
    op.execute(f"GRANT SELECT,UPDATE ON public.model_usage_budget TO {_DEFINER}")
    op.execute(f"GRANT INSERT ON public.model_usage_budget_audit TO {_DEFINER}")
    op.execute(f"GRANT {_DEFINER} TO {owner}")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {_DEFINER}")
    for signature, role in _FUNCTIONS.items():
        op.execute(f"ALTER FUNCTION public.{signature} OWNER TO {_DEFINER}")
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC,ec_app,ec_operator_viewer,ec_operator_controller,ec_ingestion_executor"
        )
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {role}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {_DEFINER}")
    op.execute(f"REVOKE {_DEFINER} FROM {owner}")


def downgrade() -> None:
    count = (
        op.get_bind()
        .execute(
            text(
                "SELECT (SELECT count(*) FROM public.model_usage_calls) + "
                "(SELECT count(*) FROM public.model_usage_budget_audit)"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "retain model usage and budget audit; roll back compatible code without dropping history"
        )
    for signature in _FUNCTIONS:
        op.execute(f"DROP FUNCTION public.{signature}")
    op.execute(
        "DROP TABLE public.model_usage_calls, public.model_usage_budget_audit, public.model_usage_budget"
    )


_TABLES = """
CREATE TABLE public.model_usage_calls (
    call_id uuid PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    requested_model text NOT NULL CHECK (requested_model ~ '^[!-~]{1,200}$'),
    actual_model text CHECK (actual_model ~ '^[!-~]{1,200}$'),
    generation_id text UNIQUE CHECK (generation_id ~ '^[!-~]{1,200}$'),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','ok','failed','interrupted')),
    input_tokens bigint CHECK (input_tokens BETWEEN 0 AND 1000000000),
    output_tokens bigint CHECK (output_tokens BETWEEN 0 AND 1000000000),
    cached_tokens bigint CHECK (cached_tokens BETWEEN 0 AND 1000000000),
    reasoning_tokens bigint CHECK (reasoning_tokens BETWEEN 0 AND 1000000000),
    cost_usd numeric(20,10) CHECK (cost_usd BETWEEN 0 AND 1000000),
    error_code text CHECK (error_code ~ '^[a-z0-9_]{1,80}$')
);
CREATE INDEX ix_model_usage_started ON public.model_usage_calls(started_at, actual_model);
CREATE TABLE public.model_usage_budget (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision integer NOT NULL DEFAULT 1,
    mode text NOT NULL DEFAULT 'enforce' CHECK (mode IN ('warn','enforce')),
    daily_limit_usd numeric(18,8) CHECK (daily_limit_usd BETWEEN 0 AND 100000),
    monthly_limit_usd numeric(18,8) CHECK (monthly_limit_usd BETWEEN 0 AND 100000),
    alert_percent integer NOT NULL DEFAULT 80 CHECK (alert_percent BETWEEN 1 AND 100),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO public.model_usage_budget(singleton) VALUES (true);
CREATE TABLE public.model_usage_budget_audit (
    revision integer PRIMARY KEY,
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    previous_settings jsonb NOT NULL,
    settings jsonb NOT NULL
);
REVOKE ALL ON public.model_usage_calls, public.model_usage_budget, public.model_usage_budget_audit
FROM PUBLIC,ec_app,ec_operator_viewer,ec_operator_controller,ec_ingestion_executor;
"""

_FUNCTION_SQL = """
CREATE FUNCTION public.fn_begin_model_call_v1(p_id uuid, p_model text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_day timestamptz := date_trunc('day',v_now AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    v_month timestamptz := date_trunc('month',v_now AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    b public.model_usage_budget%ROWTYPE;
    v_day_used numeric; v_month_used numeric; v_unknown bigint;
BEGIN
    IF p_id IS NULL OR p_model IS NULL OR p_model !~ '^[!-~]{1,200}$' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid model call';
    END IF;
    SELECT * INTO STRICT b FROM public.model_usage_budget WHERE singleton FOR UPDATE;
    IF EXISTS (SELECT 1 FROM public.model_usage_calls WHERE call_id=p_id) THEN
        RETURN 'already_started';
    END IF;
    SELECT coalesce(sum(cost_usd) FILTER (WHERE started_at>=v_day),0),
           coalesce(sum(cost_usd),0),
           count(*) FILTER (WHERE cost_usd IS NULL AND
               (status<>'pending' OR started_at<v_now-interval '2 minutes')
               AND (b.monthly_limit_usd IS NOT NULL OR started_at>=v_day))
    INTO v_day_used,v_month_used,v_unknown
    FROM public.model_usage_calls WHERE started_at>=v_month;
    IF b.mode='enforce' AND (b.daily_limit_usd IS NOT NULL OR b.monthly_limit_usd IS NOT NULL) THEN
        IF (b.daily_limit_usd IS NOT NULL AND v_day_used>=b.daily_limit_usd)
           OR (b.monthly_limit_usd IS NOT NULL AND v_month_used>=b.monthly_limit_usd) THEN
            RETURN 'budget_exhausted';
        END IF;
        IF v_unknown>0 THEN RETURN 'cost_unknown'; END IF;
    END IF;
    INSERT INTO public.model_usage_calls(call_id,started_at,requested_model) VALUES(p_id,v_now,p_model);
    RETURN 'allowed';
END $$;

CREATE FUNCTION public.fn_finish_model_call_v1(p_id uuid, p_usage jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    IF p_usage IS NULL OR jsonb_typeof(p_usage)<>'object'
       OR (p_usage->>'status') IS NULL OR (p_usage->>'status') NOT IN ('ok','failed','interrupted') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid model usage';
    END IF;
    UPDATE public.model_usage_calls SET
        completed_at=clock_timestamp(), status=p_usage->>'status',
        actual_model=p_usage->>'actual_model', generation_id=p_usage->>'generation_id',
        input_tokens=(p_usage->>'input_tokens')::bigint, output_tokens=(p_usage->>'output_tokens')::bigint,
        cached_tokens=(p_usage->>'cached_tokens')::bigint, reasoning_tokens=(p_usage->>'reasoning_tokens')::bigint,
        cost_usd=(p_usage->>'cost_usd')::numeric, error_code=p_usage->>'error_code'
    WHERE call_id=p_id AND status='pending';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='model call is not pending';
    END IF;
    RETURN true;
END $$;

CREATE FUNCTION public.fn_model_budget_v1()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
WITH bounds AS (
    SELECT date_trunc('day',statement_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS day,
           date_trunc('month',statement_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS month
), usage AS (
    SELECT coalesce(sum(c.cost_usd) FILTER (WHERE c.started_at>=bounds.day),0) AS daily_used,
           coalesce(sum(c.cost_usd),0) AS monthly_used,
           count(*) FILTER (WHERE c.cost_usd IS NULL AND
               (c.status<>'pending' OR c.started_at<statement_timestamp()-interval '2 minutes')
               AND (b.monthly_limit_usd IS NOT NULL OR c.started_at>=bounds.day)) AS unknown_calls,
           count(*) FILTER (WHERE c.status='pending') AS pending_calls
    FROM bounds CROSS JOIN public.model_usage_budget b
    LEFT JOIN public.model_usage_calls c ON c.started_at>=bounds.month
)
SELECT to_jsonb(b)-'singleton' || jsonb_build_object(
    'daily_limit_usd',b.daily_limit_usd::text, 'monthly_limit_usd',b.monthly_limit_usd::text,
    'daily_used_usd',u.daily_used::text, 'monthly_used_usd',u.monthly_used::text,
    'unknown_calls',u.unknown_calls,'pending_calls',u.pending_calls,
    'day_start',bounds.day,'month_start',bounds.month,'generated_at',statement_timestamp()
) FROM public.model_usage_budget b CROSS JOIN usage u CROSS JOIN bounds WHERE b.singleton
$$;

CREATE FUNCTION public.fn_update_model_budget_v1(p_settings jsonb, p_actor text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE b public.model_usage_budget%ROWTYPE; v_previous jsonb;
BEGIN
    IF p_actor IS NULL OR length(p_actor) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid budget actor';
    END IF;
    SELECT * INTO STRICT b FROM public.model_usage_budget WHERE singleton FOR UPDATE;
    IF (p_settings->>'expected_revision')::integer IS DISTINCT FROM b.revision THEN RETURN NULL; END IF;
    v_previous:=to_jsonb(b)-'singleton';
    UPDATE public.model_usage_budget SET revision=revision+1, updated_at=clock_timestamp(),
        mode=p_settings->>'mode', daily_limit_usd=(p_settings->>'daily_limit_usd')::numeric,
        monthly_limit_usd=(p_settings->>'monthly_limit_usd')::numeric,
        alert_percent=(p_settings->>'alert_percent')::integer WHERE singleton;
    INSERT INTO public.model_usage_budget_audit(revision,actor,previous_settings,settings)
    SELECT revision,p_actor,v_previous,to_jsonb(new)-'singleton' FROM public.model_usage_budget new WHERE singleton;
    RETURN public.fn_model_budget_v1();
END $$;

CREATE FUNCTION public.fn_model_usage_report_v1(p_start timestamptz,p_end timestamptz,p_hours integer,p_model text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE v_report jsonb;
BEGIN
    IF p_start IS NULL OR p_end IS NULL OR NOT isfinite(p_start) OR NOT isfinite(p_end)
       OR p_start>=p_end OR p_end-p_start>interval '90 days' OR p_end>statement_timestamp()+interval '1 day'
       OR p_hours IS NULL OR p_hours NOT IN (1,24)
       OR ceil(extract(epoch FROM p_end-p_start)/(p_hours*3600))>120
       OR (p_model IS NOT NULL AND p_model !~ '^[!-~]{1,200}$') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid model usage window';
    END IF;
    WITH window_calls AS MATERIALIZED (
        SELECT *, coalesce(actual_model,'unreported') AS model,
               extract(epoch FROM completed_at-started_at)*1000 AS latency_ms
        FROM public.model_usage_calls WHERE started_at>=p_start AND started_at<p_end
    ), scoped AS MATERIALIZED (
        SELECT * FROM window_calls WHERE p_model IS NULL OR model=p_model
    ), series AS (
        SELECT i, p_start+i*p_hours*interval '1 hour' AS at,
               least(p_start+(i+1)*p_hours*interval '1 hour',p_end) AS until
        FROM generate_series(0,ceil(extract(epoch FROM p_end-p_start)/(p_hours*3600))::integer-1) i
    ), buckets AS (
        SELECT s.i,s.at,s.until,count(c.call_id) AS calls,
               count(c.call_id) FILTER (WHERE c.status IN ('failed','interrupted')) AS failed,
               coalesce(sum(c.cost_usd),0)::text AS cost_usd,
               count(c.call_id) FILTER (WHERE c.cost_usd IS NULL) AS unknown_cost_calls,
               coalesce(sum(c.input_tokens),0) AS input_tokens,coalesce(sum(c.output_tokens),0) AS output_tokens,
               avg(c.latency_ms) AS latency_ms
        FROM series s LEFT JOIN scoped c ON c.started_at>=s.at AND c.started_at<s.until GROUP BY s.i,s.at,s.until
    ), models AS (
        SELECT model,count(*) AS calls,count(*) FILTER (WHERE status IN ('failed','interrupted')) AS failed,
               coalesce(sum(cost_usd),0)::text AS cost_usd,count(*) FILTER (WHERE cost_usd IS NULL) AS unknown_cost_calls,
               coalesce(sum(input_tokens),0) AS input_tokens,coalesce(sum(output_tokens),0) AS output_tokens,
               coalesce(sum(cached_tokens),0) AS cached_tokens,coalesce(sum(reasoning_tokens),0) AS reasoning_tokens,
               avg(latency_ms) AS latency_ms
        FROM scoped GROUP BY model ORDER BY sum(cost_usd) DESC NULLS LAST, model LIMIT 100
    ), recent AS (
        SELECT call_id,started_at,completed_at,requested_model,actual_model,status,
               input_tokens,output_tokens,cost_usd::text,latency_ms,error_code
        FROM scoped ORDER BY started_at DESC,call_id DESC LIMIT 50
    ) SELECT jsonb_build_object(
        'generated_at',statement_timestamp(),'start_at',p_start,'end_at',p_end,'bucket_hours',p_hours,'model',p_model,
        'tracked_since',(SELECT min(started_at) FROM public.model_usage_calls),
        'model_options',coalesce((SELECT jsonb_agg(model ORDER BY model) FROM (SELECT DISTINCT model FROM window_calls LIMIT 100) available),'[]'),
        'totals',(SELECT jsonb_build_object(
            'calls',count(*),'failed',count(*) FILTER (WHERE status IN ('failed','interrupted')),
            'pending',count(*) FILTER (WHERE status='pending'),'cost_usd',coalesce(sum(cost_usd),0)::text,
            'unknown_cost_calls',count(*) FILTER (WHERE cost_usd IS NULL),
            'unknown_token_calls',count(*) FILTER (WHERE input_tokens IS NULL OR output_tokens IS NULL),
            'input_tokens',coalesce(sum(input_tokens),0),'output_tokens',coalesce(sum(output_tokens),0),
            'cached_tokens',coalesce(sum(cached_tokens),0),'reasoning_tokens',coalesce(sum(reasoning_tokens),0),
            'latency_ms',avg(latency_ms)) FROM scoped),
        'series',(SELECT jsonb_agg(to_jsonb(b)-'i' ORDER BY b.i) FROM buckets b),
        'models',coalesce((SELECT jsonb_agg(to_jsonb(m)) FROM models m),'[]'),
        'recent',coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM recent r),'[]')
    ) INTO v_report;
    RETURN v_report;
END $$;
"""
