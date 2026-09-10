"""Reviewed collection horizons and immutable run-window evidence.

Revision ID: 0187
Revises: 0186

Existing sources keep their cadence. New rows default to daily refresh and a 90-day
collection horizon. Older configuration callers preserve the stored horizon; only
new executor code enforces and records frozen collection bounds before egress.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0187"
down_revision: str | None = "0186"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.catalog_sources
          ADD COLUMN collection_horizon_days integer NOT NULL DEFAULT 90
            CHECK (collection_horizon_days BETWEEN 1 AND 90),
          ALTER COLUMN refresh_interval_minutes SET DEFAULT 1440
    """)
    # Adding a request-shaping field must participate in the existing revision fence.
    bind = op.get_bind()
    definition = bind.execute(
        text("SELECT pg_get_functiondef('public.fn_bump_catalog_source_revision()'::regprocedure)")
    ).scalar_one()
    marker = "OR OLD.page_limit IS DISTINCT FROM NEW.page_limit"
    if definition.count(marker) != 1:
        raise RuntimeError("unexpected source revision function contract")
    op.execute(
        definition.replace(
            marker,
            marker
            + "\n               OR OLD.collection_horizon_days IS DISTINCT FROM NEW.collection_horizon_days",
        )
    )
    op.execute(_CONFIGURATION)
    op.execute(_LEGACY_CONFIGURATION)
    op.execute(_DUE)
    op.execute(_WINDOWS)
    op.execute(_PREPARE)
    op.execute(_WINDOW_EVIDENCE)
    op.execute(_DETAIL)
    op.execute(_COVERAGE)
    _update_projections()
    for signature, role in (
        (
            "fn_update_ingestion_admin_source_configuration_v3(text,integer,text,text[],text,boolean,boolean,timestamptz,integer,integer,integer,text,integer)",
            "ec_operator_controller",
        ),
        ("fn_get_ingestion_admin_source_detail_v3(text,boolean)", "ec_operator_viewer"),
        (
            "fn_list_ingestion_admin_due_sources_v4(timestamptz,integer)",
            "ec_ingestion_executor,ec_operator_controller",
        ),
        (
            "fn_prepare_catalog_collection_window_v1(text,text,uuid,integer)",
            "ec_ingestion_executor",
        ),
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC,ec_app,ec_operator_viewer,ec_operator_controller,ec_ingestion_executor"
        )
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {role}")
    for signature in (
        "fn_catalog_collection_evidence_v1(text,text,integer)",
        "fn_catalog_observation_is_current_v1(text,text,timestamptz)",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC,ec_app,ec_operator_viewer,ec_operator_controller,ec_ingestion_executor"
        )


def downgrade() -> None:
    # Immutable evidence and source settings are retained when old code is restored.
    # Old configuration signatures remain callable and preserve the added horizon.
    raise RuntimeError(
        "0187 retains immutable collection evidence; restore compatible code without downgrading this schema"
    )


_LEGACY_CONFIGURATION = """
CREATE OR REPLACE FUNCTION public.fn_update_ingestion_admin_source_configuration(
 p_source_key text,p_expected_revision integer,p_seed_url text,p_approved_origins text[],
 p_mode text,p_enabled boolean,p_handoff_only boolean,p_review_expires_at timestamptz,
 p_refresh_interval_minutes integer,p_min_interval_ms integer,p_page_limit integer,p_requested_by text
) RETURNS TABLE(outcome text,source_revision integer,reviewed_at timestamptz,updated_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT * FROM public.fn_update_ingestion_admin_source_configuration_v3(
  p_source_key,p_expected_revision,p_seed_url,p_approved_origins,p_mode,p_enabled,p_handoff_only,
  p_review_expires_at,p_refresh_interval_minutes,p_min_interval_ms,p_page_limit,p_requested_by,NULL
 )
$$
"""

_DUE = """
CREATE FUNCTION public.fn_list_ingestion_admin_due_sources_v4(p_now timestamptz,p_limit integer)
RETURNS TABLE(source_key text,display_name text,publisher text,seed_url text,approved_origins text[],
 region text,mode text,handoff_only boolean,enabled boolean,reviewed_at timestamptz,
 review_expires_at timestamptz,refresh_interval_minutes integer,min_interval_ms integer,
 page_limit integer,source_revision integer,due_at timestamptz,last_succeeded_at timestamptz,
 collection_horizon_days integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT d.*,s.collection_horizon_days
 FROM public.fn_list_ingestion_admin_due_sources_v3(p_now,p_limit) d
 JOIN public.catalog_sources s USING(source_key)
 ORDER BY d.due_at,d.source_key
$$
"""

_WINDOWS = """
CREATE TABLE public.catalog_refresh_collection_windows (
 source_key text NOT NULL,run_key text NOT NULL,
 source_revision integer NOT NULL CHECK(source_revision>0),
 horizon_days integer NOT NULL CHECK(horizon_days BETWEEN 1 AND 90),
 start_at timestamptz NOT NULL,end_at timestamptz NOT NULL,
 captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(source_key,run_key), CHECK(end_at=start_at+horizon_days*interval '24 hours'),
 FOREIGN KEY(source_key,run_key) REFERENCES public.catalog_refresh_runs(source_key,run_key) ON DELETE CASCADE
);
CREATE TABLE public.catalog_refresh_execution_configurations (
 source_key text NOT NULL,run_key text NOT NULL,
 attempt_count integer NOT NULL CHECK(attempt_count>0),
 source_revision integer NOT NULL CHECK(source_revision>0),
 configuration jsonb NOT NULL CHECK(jsonb_typeof(configuration)='object' AND octet_length(configuration::text)<=16384),
 captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(source_key,run_key,attempt_count),
 FOREIGN KEY(source_key,run_key) REFERENCES public.catalog_refresh_runs(source_key,run_key) ON DELETE CASCADE
);
REVOKE ALL ON public.catalog_refresh_collection_windows,public.catalog_refresh_execution_configurations
 FROM PUBLIC,ec_app,ec_operator_viewer,ec_operator_controller,ec_ingestion_executor;
CREATE INDEX ix_catalog_refresh_runs_success_completed
 ON public.catalog_refresh_runs(source_key,completed_at DESC,run_key DESC)
 WHERE status='succeeded' AND completed_at IS NOT NULL;
"""

_PREPARE = """
CREATE FUNCTION public.fn_prepare_catalog_collection_window_v1(
 p_source_key text,p_run_key text,p_lease_token uuid,p_expected_revision integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE s public.catalog_sources%ROWTYPE; r public.catalog_refresh_runs%ROWTYPE;
 w public.catalog_refresh_collection_windows%ROWTYPE; v_now timestamptz; v_config jsonb; v_revision integer;
BEGIN
 IF p_source_key IS NULL OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
   OR p_run_key IS NULL OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
   OR p_lease_token IS NULL OR p_expected_revision IS NULL OR p_expected_revision<1 THEN RETURN NULL; END IF;
 SELECT * INTO s FROM public.catalog_sources WHERE source_key=p_source_key FOR SHARE;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT * INTO r FROM public.catalog_refresh_runs
 WHERE source_key=p_source_key AND run_key=p_run_key FOR UPDATE;
 v_now:=clock_timestamp();
 IF NOT FOUND OR r.status<>'running' OR r.lease_token IS DISTINCT FROM p_lease_token
   OR r.lease_expires_at IS NULL OR r.lease_expires_at<=v_now
   OR s.source_revision<>p_expected_revision OR NOT s.enabled OR NOT s.handoff_only
   OR s.retired_at IS NOT NULL OR s.reviewed_at IS NULL
   OR (s.review_expires_at IS NOT NULL AND s.review_expires_at<=v_now) THEN RETURN NULL; END IF;
 SELECT * INTO w FROM public.catalog_refresh_collection_windows
 WHERE source_key=p_source_key AND run_key=p_run_key;
 IF NOT FOUND THEN
   -- Existing nonempty pages have no proven new-window contract. Keep their evidence
   -- intact and deny egress/promotion until an operator starts a fresh run identity.
   IF EXISTS(SELECT 1 FROM public.catalog_refresh_progress p
     WHERE p.source_key=p_source_key AND p.run_key=p_run_key
       AND (p.next_page>0 OR p.staged_raw_count>0 OR p.terminal_page IS NOT NULL)) THEN
     RETURN jsonb_build_object('outcome','legacy_stage_requires_new_run');
   END IF;
   INSERT INTO public.catalog_refresh_collection_windows(source_key,run_key,source_revision,horizon_days,start_at,end_at)
   VALUES(p_source_key,p_run_key,s.source_revision,s.collection_horizon_days,v_now,
     v_now+s.collection_horizon_days*interval '24 hours') RETURNING * INTO w;
 END IF;
 IF w.end_at<=v_now THEN RETURN jsonb_build_object('outcome','collection_window_expired'); END IF;
 v_config:=jsonb_build_object('source_revision',s.source_revision,'mode',s.mode,
   'seed_url',s.seed_url,'approved_origins',s.approved_origins,'handoff_only',s.handoff_only,
   'reviewed_at',s.reviewed_at,'review_expires_at',s.review_expires_at,
   'refresh_interval_minutes',s.refresh_interval_minutes,'min_interval_ms',s.min_interval_ms,
   'page_limit',s.page_limit,'collection_horizon_days',s.collection_horizon_days);
 INSERT INTO public.catalog_refresh_execution_configurations(source_key,run_key,attempt_count,source_revision,configuration)
 VALUES(p_source_key,p_run_key,r.attempt_count,s.source_revision,v_config)
 ON CONFLICT(source_key,run_key,attempt_count) DO NOTHING;
 SELECT source_revision INTO v_revision FROM public.catalog_refresh_execution_configurations
 WHERE source_key=p_source_key AND run_key=p_run_key AND attempt_count=r.attempt_count;
 IF v_revision<>s.source_revision THEN RETURN NULL; END IF;
 RETURN jsonb_build_object('source_revision',w.source_revision,'horizon_days',w.horizon_days,
   'start_at',w.start_at,'end_at',w.end_at,'attempt_count',r.attempt_count);
END;
$$
"""

_WINDOW_EVIDENCE = """
CREATE FUNCTION public.fn_catalog_collection_evidence_v1(p_source text,p_run text,p_attempt integer)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT jsonb_build_object('collection_window',CASE WHEN w.source_key IS NULL THEN NULL ELSE
   jsonb_build_object('window_source_revision',w.source_revision,'horizon_days',w.horizon_days,
     'start_at',w.start_at,'end_at',w.end_at) END,
   'execution_configuration',c.configuration)
 FROM (SELECT 1) anchor
 LEFT JOIN public.catalog_refresh_collection_windows w ON w.source_key=p_source AND w.run_key=p_run
 LEFT JOIN public.catalog_refresh_execution_configurations c
   ON c.source_key=p_source AND c.run_key=p_run AND c.attempt_count=p_attempt
$$
"""

_COVERAGE = """
CREATE FUNCTION public.fn_catalog_observation_is_current_v1(p_source text,p_observed_run text,p_start timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT COALESCE((SELECT r.run_key=p_observed_run FROM public.catalog_refresh_runs r
   LEFT JOIN public.catalog_refresh_collection_windows w USING(source_key,run_key)
   WHERE r.source_key=p_source AND r.status='succeeded' AND r.completed_at IS NOT NULL
     AND NOT public.fn_ingestion_admin_run_is_fixture(r.run_key,r.error)
     AND (w.source_key IS NULL OR (p_start>=w.start_at AND p_start<w.end_at))
   ORDER BY r.completed_at DESC,r.run_key DESC LIMIT 1),false)
$$
"""


def _update_projections() -> None:
    bind = op.get_bind()
    for signature in (
        "public.fn_list_retained_catalog_browse_observations_v1(text,timestamptz,timestamptz)",
        "public.fn_list_retained_catalog_browse_observations_v2(text[],timestamptz,timestamptz)",
    ):
        definition = bind.execute(
            text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": signature},
        ).scalar_one()
        marker = "success.run_key = observation.last_run_key"
        if definition.count(marker) != 1:
            raise RuntimeError("unexpected retained catalog membership contract")
        replacement = """(success.run_key = observation.last_run_key OR (
          EXISTS(SELECT 1 FROM public.catalog_refresh_collection_windows latest_window
            WHERE latest_window.source_key=observation.source_key AND latest_window.run_key=success.run_key)
          AND public.fn_catalog_observation_is_current_v1(observation.source_key, observation.last_run_key, event.start_at)
        ))"""
        op.execute(definition.replace(marker, replacement))
    definition = bind.execute(
        text(
            "SELECT pg_get_functiondef('public.fn_report_catalog_source_coverage_v1()'::regprocedure)"
        )
    ).scalar_one()
    marker = "observation.last_run_key IS NOT DISTINCT FROM newest_success.run_key"
    if definition.count(marker) != 1:
        raise RuntimeError("unexpected catalog coverage contract")
    definition = (
        definition.replace(
            "WHERE run.status IN ('succeeded', 'failed')",
            "WHERE run.status IN ('succeeded', 'failed') AND NOT public.fn_ingestion_admin_run_is_fixture(run.run_key,run.error)",
        )
        .replace(
            "WHERE run.status = 'succeeded' AND run.completed_at IS NOT NULL",
            "WHERE run.status = 'succeeded' AND run.completed_at IS NOT NULL AND NOT public.fn_ingestion_admin_run_is_fixture(run.run_key,run.error)",
        )
        .replace(
            "ORDER BY run.source_key, run.started_at DESC",
            "ORDER BY run.source_key, run.started_at DESC,run.run_key DESC",
        )
        .replace(
            "ORDER BY run.source_key, run.completed_at DESC",
            "ORDER BY run.source_key, run.completed_at DESC,run.run_key DESC",
        )
    )
    replacement = """(observation.last_run_key IS NOT DISTINCT FROM newest_success.run_key OR (
       EXISTS(SELECT 1 FROM public.catalog_refresh_collection_windows latest_window
         WHERE latest_window.source_key=observation.source_key AND latest_window.run_key=newest_success.run_key)
       AND public.fn_catalog_observation_is_current_v1(observation.source_key, observation.last_run_key, event.start_at)
    ))"""
    op.execute(definition.replace(marker, replacement))
    definition = bind.execute(
        text(
            "SELECT pg_get_functiondef('public.fn_operator_run_evidence_v1(jsonb,boolean)'::regprocedure)"
        )
    ).scalar_one()
    marker = "SELECT (f-'publisher'-'region'-'stage_duration'-'ordinal')||jsonb_build_object("
    replacement = "SELECT (f-'publisher'-'region'-'stage_duration'-'ordinal')||public.fn_catalog_collection_evidence_v1(f->>'source_key',f->>'run_key',(f->>'attempt_count')::integer)||jsonb_build_object("
    if definition.count(marker) != 1:
        raise RuntimeError("unexpected run evidence projection contract")
    definition = definition.replace(marker, replacement)
    definition = definition.replace(
        "'refresh_interval_minutes',s.refresh_interval_minutes",
        "'collection_horizon_days',s.collection_horizon_days,'refresh_interval_minutes',s.refresh_interval_minutes",
    )
    op.execute(definition)


_CONFIGURATION = r"""
CREATE FUNCTION public.fn_update_ingestion_admin_source_configuration_v3(
    p_source_key text,
    p_expected_revision integer,
    p_seed_url text,
    p_approved_origins text[],
    p_mode text,
    p_enabled boolean,
    p_handoff_only boolean,
    p_review_expires_at timestamptz,
    p_refresh_interval_minutes integer,
    p_min_interval_ms integer,
    p_page_limit integer,
    p_requested_by text,
    p_collection_horizon_days integer
)
RETURNS TABLE (
    outcome text,
    source_revision integer,
    reviewed_at timestamptz,
    updated_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_source public.catalog_sources%ROWTYPE;
    v_updated public.catalog_sources%ROWTYPE;
    v_before jsonb;
    v_after jsonb;
BEGIN
    IF p_source_key IS NULL
       OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
       OR p_expected_revision IS NULL
       OR p_expected_revision < 1
       OR p_seed_url IS NULL
       OR char_length(p_seed_url) NOT BETWEEN 1 AND 2048
       OR p_seed_url ~ '[[:cntrl:]]'
       OR p_seed_url !~ '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/|[?]|$)'
       OR p_approved_origins IS NULL
       OR cardinality(p_approved_origins) NOT BETWEEN 1 AND 20
       OR EXISTS (
            SELECT 1
            FROM unnest(p_approved_origins) AS approved(origin)
            WHERE approved.origin IS NULL
               OR char_length(approved.origin) NOT BETWEEN 9 AND 512
               OR approved.origin ~ '[[:cntrl:]]'
               OR approved.origin
                  !~ '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$'
       )
       OR (
            SELECT count(DISTINCT approved.origin)
            FROM unnest(p_approved_origins) AS approved(origin)
       ) <> cardinality(p_approved_origins)
       OR NOT EXISTS (
            SELECT 1
            FROM unnest(p_approved_origins) AS approved(origin)
            WHERE p_seed_url = approved.origin
               OR p_seed_url LIKE approved.origin || '/%'
               OR p_seed_url LIKE approved.origin || '?%'
       )
       OR p_enabled IS NULL
       OR p_handoff_only IS DISTINCT FROM true
       OR (
            p_review_expires_at IS NOT NULL
            AND p_review_expires_at <= v_now
       )
       OR p_refresh_interval_minutes IS NULL
       OR p_min_interval_ms IS NULL
       OR p_page_limit IS NULL
       OR p_refresh_interval_minutes NOT BETWEEN 5 AND 1440
       OR (p_collection_horizon_days IS NOT NULL AND p_collection_horizon_days NOT BETWEEN 1 AND 90)
       OR p_min_interval_ms NOT BETWEEN 250 AND 60000
       OR p_page_limit NOT BETWEEN 1 AND 500
       OR p_requested_by IS NULL
       OR char_length(p_requested_by) NOT BETWEEN 1 AND 256
       OR p_requested_by ~ '[[:cntrl:]]'
    THEN
        RETURN QUERY SELECT 'invalid', NULL::integer, NULL::timestamptz,
                            NULL::timestamptz;
        RETURN;
    END IF;

    SELECT source.*
    INTO v_source
    FROM public.catalog_sources AS source
    WHERE source.source_key = p_source_key
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'not_found', NULL::integer, NULL::timestamptz,
                            NULL::timestamptz;
        RETURN;
    END IF;
    v_now := clock_timestamp();
    IF p_review_expires_at IS NOT NULL AND p_review_expires_at <= v_now THEN
        RETURN QUERY SELECT 'invalid', NULL::integer, NULL::timestamptz, NULL::timestamptz;
        RETURN;
    END IF;
    IF v_source.source_revision <> p_expected_revision THEN
        RETURN QUERY SELECT 'conflict', v_source.source_revision,
                            v_source.reviewed_at, v_source.updated_at;
        RETURN;
    END IF;

    -- Mode is source identity, never an operator-editable configuration field.
    -- NULL supports newer callers omitting this legacy parameter; older callers must
    -- echo the stored mode. No duplicated allowlist can exclude a reviewed adapter.
    IF p_mode IS NOT NULL AND p_mode IS DISTINCT FROM v_source.mode THEN
        RETURN QUERY SELECT 'invalid', v_source.source_revision,
                            v_source.reviewed_at, v_source.updated_at;
        RETURN;
    END IF;
    IF p_enabled AND v_source.retired_at IS NOT NULL THEN
        RETURN QUERY SELECT 'unavailable', v_source.source_revision,
                            v_source.reviewed_at, v_source.updated_at;
        RETURN;
    END IF;

    v_before := jsonb_build_object(
        'seed_url', v_source.seed_url,
        'approved_origins', to_jsonb(v_source.approved_origins),
        'mode', v_source.mode,
        'enabled', v_source.enabled,
        'handoff_only', v_source.handoff_only,
        'reviewed_at', v_source.reviewed_at,
        'review_expires_at', v_source.review_expires_at,
        'refresh_interval_minutes', v_source.refresh_interval_minutes,
        'min_interval_ms', v_source.min_interval_ms,
        'page_limit', v_source.page_limit,
        'collection_horizon_days', v_source.collection_horizon_days,
        'source_revision', v_source.source_revision
    );

    UPDATE public.catalog_sources AS source
    SET seed_url = p_seed_url,
        approved_origins = p_approved_origins,
        mode = v_source.mode,
        enabled = p_enabled,
        handoff_only = p_handoff_only,
        reviewed_at = v_now,
        review_expires_at = p_review_expires_at,
        refresh_interval_minutes = p_refresh_interval_minutes,
        min_interval_ms = p_min_interval_ms,
        page_limit = p_page_limit,
        collection_horizon_days = COALESCE(p_collection_horizon_days, source.collection_horizon_days),
        source_revision = source.source_revision + 1,
        updated_at = v_now
    WHERE source.source_key = p_source_key
    RETURNING source.* INTO v_updated;

    v_after := jsonb_build_object(
        'seed_url', v_updated.seed_url,
        'approved_origins', to_jsonb(v_updated.approved_origins),
        'mode', v_updated.mode,
        'enabled', v_updated.enabled,
        'handoff_only', v_updated.handoff_only,
        'reviewed_at', v_updated.reviewed_at,
        'review_expires_at', v_updated.review_expires_at,
        'refresh_interval_minutes', v_updated.refresh_interval_minutes,
        'min_interval_ms', v_updated.min_interval_ms,
        'page_limit', v_updated.page_limit,
        'collection_horizon_days', v_updated.collection_horizon_days,
        'source_revision', v_updated.source_revision
    );

    INSERT INTO public.catalog_source_configuration_audit (
        source_key, prior_revision, new_revision, requested_by,
        changed_at, before_config, after_config
    )
    VALUES (
        p_source_key, v_source.source_revision, v_updated.source_revision,
        p_requested_by, v_now, v_before, v_after
    );

    RETURN QUERY SELECT 'updated', v_updated.source_revision,
                        v_updated.reviewed_at, v_updated.updated_at;
END;
$$
"""

_DETAIL = r"""
CREATE FUNCTION public.fn_get_ingestion_admin_source_detail_v3(
    p_source_key text,
    p_include_fixtures boolean
)
RETURNS TABLE (
    source_key text,
    display_name text,
    publisher text,
    mode text,
    region text,
    seed_url text,
    enabled boolean,
    handoff_only boolean,
    approved_origins text[],
    reviewed_at timestamptz,
    review_expires_at timestamptz,
    refresh_interval_minutes integer,
    min_interval_ms integer,
    page_limit integer,
    source_revision integer,
    collection_horizon_days integer,
    latest_run_collection_window jsonb,
    latest_run_execution_configuration jsonb,
    review_status text,
    effective_status text,
    policy_blocked boolean,
    due boolean,
    last_succeeded_at timestamptz,
    next_due_at timestamptz,
    event_count bigint,
    latest_run_key text,
    latest_run_status text,
    latest_run_started_at timestamptz,
    latest_run_completed_at timestamptz,
    latest_run_candidate_count integer,
    latest_run_canonical_count integer,
    latest_run_error text,
    latest_run_attempt_count integer,
    latest_run_duration_ms bigint,
    latest_run_source_revision integer,
    latest_run_release_revision text,
    latest_run_image_digest text,
    latest_run_provenance_status text,
    latest_run_trigger text,
    retired_at timestamptz,
    retired_reason text,
    superseded_by_source_key text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT detail.source_key,
           detail.display_name,
           detail.publisher,
           detail.mode,
           detail.region,
           detail.seed_url,
           CASE WHEN registry.retired_at IS NULL THEN detail.enabled ELSE false END,
           detail.handoff_only,
           detail.approved_origins,
           detail.reviewed_at,
           detail.review_expires_at,
           detail.refresh_interval_minutes,
           detail.min_interval_ms,
           detail.page_limit,
           detail.source_revision,
           registry.collection_horizon_days,
           public.fn_catalog_collection_evidence_v1(detail.source_key,detail.latest_run_key,detail.latest_run_attempt_count)->'collection_window',
           public.fn_catalog_collection_evidence_v1(detail.source_key,detail.latest_run_key,detail.latest_run_attempt_count)->'execution_configuration',
           detail.review_status,
           CASE
               WHEN registry.retired_at IS NOT NULL THEN 'retired'
               ELSE detail.effective_status
           END,
           detail.policy_blocked,
           CASE WHEN registry.retired_at IS NULL THEN detail.due ELSE false END,
           detail.last_succeeded_at,
           CASE
               WHEN registry.retired_at IS NULL THEN detail.next_due_at
               ELSE NULL
           END,
           detail.event_count,
           detail.latest_run_key,
           detail.latest_run_status,
           detail.latest_run_started_at,
           detail.latest_run_completed_at,
           detail.latest_run_candidate_count,
           detail.latest_run_canonical_count,
           detail.latest_run_error,
           detail.latest_run_attempt_count,
           detail.latest_run_duration_ms,
           detail.latest_run_source_revision,
           detail.latest_run_release_revision,
           detail.latest_run_image_digest,
           detail.latest_run_provenance_status,
           detail.latest_run_trigger,
           registry.retired_at,
           registry.retired_reason,
           registry.superseded_by_source_key
    FROM public.fn_get_ingestion_admin_source_detail(
        p_source_key, p_include_fixtures
    ) AS detail
    JOIN public.catalog_sources AS registry
      ON registry.source_key = detail.source_key
$$
"""
