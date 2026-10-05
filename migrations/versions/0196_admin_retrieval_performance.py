"""Keep operator navigation independent of catalog aggregates and page enrichment.

Revision ID: 0196
Revises: 0194

0195 is reserved by the separate Google identity work. No catalog or tenant data is
changed. Existing projection signatures, owners and grants remain unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from runpy import run_path

from alembic import op
from sqlalchemy import text

revision: str = "0196"
down_revision: str | None = "0194"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_PROJECTION = run_path(
    str(Path(__file__).with_name("0193_operator_source_catalog_counts.py"))
)["_PROJECTION"]
_CATALOG_PROJECTION = run_path(str(Path(__file__).with_name("0191_operator_catalog_records.py")))[
    "_PROJECTION"
]
_STATE = "public.fn_ingestion_admin_source_states_v1(boolean)"
_STATUS = "public.fn_list_ingestion_admin_source_status_v1(boolean)"
_POLICY = "public.fn_get_ingestion_admin_policy_v1()"
_DEFINER = "ec_operator_aggregate_definer"
_INVENTORY_INDEX = "ix_canonical_events_operator_inventory"
_SOURCE_NAVIGATION = (
    "public.fn_count_ingestion_admin_sources_v2(text,text,text,text,text,text,boolean)",
    "public.fn_list_ingestion_admin_filter_values_v2(text,text,text,text,text,boolean)",
)


def _replace_once(body: str, before: str, after: str) -> str:
    if body.count(before) != 1:
        raise RuntimeError("operator retrieval projection differs from its migration contract")
    return body.replace(before, after, 1)


def _source_states(body: str) -> str:
    """Reuse the installed lifecycle/lease logic without its discovery inventory scan."""
    body = _replace_once(
        body, "fn_ingestion_admin_sources_base(", "fn_ingestion_admin_source_states_v1("
    )
    start = body.index("            ), observation_counts AS (")
    end = body.index("            ), facts AS (", start)
    body = body[:start] + body[end:]
    body = _replace_once(
        body,
        "COALESCE(observations.event_count, 0) AS source_event_count",
        "0::bigint AS source_event_count",
    )
    body = _replace_once(
        body,
        "                LEFT JOIN observation_counts AS observations\n                  ON observations.source_key = source.source_key",
        "",
    )
    start = body.index("            ), latest_success AS (")
    end = body.index("            ), facts AS (", start)
    body = (
        body[:start]
        + """
            ), latest_success AS (
                SELECT source.source_key,refresh.completed_at
                FROM selected_sources source
                CROSS JOIN LATERAL (
                    SELECT refresh.completed_at FROM public.catalog_refresh_runs refresh
                    WHERE refresh.source_key=source.source_key
                      AND refresh.status='succeeded' AND refresh.completed_at IS NOT NULL
                      AND (p_include_fixtures OR NOT public.fn_ingestion_admin_run_is_fixture(refresh.run_key,refresh.error))
                    ORDER BY refresh.completed_at DESC,refresh.run_key DESC LIMIT 1
                ) refresh
            ), latest_run AS (
                SELECT source.source_key,refresh.*
                FROM selected_sources source
                CROSS JOIN LATERAL (
                    SELECT refresh.run_key,refresh.status,refresh.started_at,refresh.lease_expires_at,
                           refresh.completed_at,refresh.candidate_count,refresh.canonical_count,
                           refresh.error,refresh.attempt_count
                    FROM public.catalog_refresh_runs refresh
                    WHERE refresh.source_key=source.source_key
                      AND (p_include_fixtures OR NOT public.fn_ingestion_admin_run_is_fixture(refresh.run_key,refresh.error))
                    ORDER BY refresh.started_at DESC,refresh.run_key DESC LIMIT 1
                ) refresh
"""
        + body[end:]
    )
    return body


@contextmanager
def _aggregate_owner() -> Iterator[None]:
    """Replace restricted-definer projections without requiring migration superuser."""
    bind = op.get_bind()
    owner = bind.dialect.identifier_preparer.quote(
        bind.execute(text("SELECT current_user")).scalar_one()
    )
    already_inherits = bind.execute(
        text("SELECT pg_has_role(current_user,:role,'USAGE')"), {"role": _DEFINER}
    ).scalar_one()
    own_membership = bind.execute(
        text("""
        SELECT membership.inherit_option
        FROM pg_auth_members membership
        JOIN pg_roles target ON target.oid=membership.roleid
        WHERE target.rolname=:role
          AND membership.member=current_user::regrole
          AND membership.grantor=current_user::regrole
    """),
        {"role": _DEFINER},
    ).first()
    if not already_inherits:
        # PG16 uses per-membership INHERIT. The role's NOINHERIT default and a
        # previous own grant must not suppress this temporary owner authority.
        op.execute(f"GRANT {_DEFINER} TO {owner} WITH INHERIT TRUE GRANTED BY {owner}")
    try:
        yield
    finally:
        if not already_inherits:
            if own_membership is None:
                op.execute(f"REVOKE {_DEFINER} FROM {owner} GRANTED BY {owner}")
            else:
                option = "TRUE" if own_membership.inherit_option else "FALSE"
                op.execute(f"GRANT {_DEFINER} TO {owner} WITH INHERIT {option} GRANTED BY {owner}")


def _route_source_navigation(*, inventory: bool) -> None:
    """Source-only counts/facets never need canonical-event inventory."""
    before, after = "fn_ingestion_admin_sources_base(", "fn_ingestion_admin_source_states_v1("
    if inventory:
        before, after = after, before
    for signature in _SOURCE_NAVIGATION:
        body = (
            op.get_bind()
            .execute(
                text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
                {"signature": signature},
            )
            .scalar_one()
        )
        op.execute(_replace_once(body, before, after))


def upgrade() -> None:
    bind = op.get_bind()
    base = bind.execute(
        text(
            "SELECT pg_get_functiondef('public.fn_ingestion_admin_sources_base(boolean)'::regprocedure)"
        )
    ).scalar_one()
    op.execute(_source_states(base))
    op.execute(
        f"REVOKE ALL ON FUNCTION {_STATE} FROM PUBLIC,ec_app,ec_operator_viewer,ec_operator_controller,ec_ingestion_executor"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {_STATE} TO ec_operator_aggregate_definer")
    _route_source_navigation(inventory=False)
    op.execute(_POLICY_SQL)
    op.execute(_STATUS_SQL)
    for signature in (_POLICY, _STATUS):
        op.execute(
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC,ec_app,ec_operator_viewer,ec_operator_controller,ec_ingestion_executor"
        )
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_operator_viewer")
    with _aggregate_owner():
        op.execute(_SOURCE_SQL)
        op.execute(_CATALOG_SQL)
    _route_run_evidence(optimized=True)
    # The fixed inventory reads need only these small fields before paging. Keep
    # their scans off the much wider event rows and fetch full payloads on page.
    op.execute(
        f"CREATE INDEX {_INVENTORY_INDEX} ON public.canonical_events(canonical_event_id) INCLUDE(start_at,end_at,event_status,price_status)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX public.{_INVENTORY_INDEX}")
    _route_source_navigation(inventory=True)
    with _aggregate_owner():
        op.execute(_CATALOG_PROJECTION.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
        op.execute(_SOURCE_PROJECTION.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    _route_run_evidence(optimized=False)
    for signature in (_STATUS, _POLICY, _STATE):
        op.execute(f"DROP FUNCTION {signature}")


_POLICY_SQL = """
CREATE FUNCTION public.fn_get_ingestion_admin_policy_v1()
RETURNS TABLE(allowed boolean, reason text, code text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT CASE WHEN policy.source IS NULL OR policy.quarantined THEN false
             ELSE coalesce((policy.automation_allowed->>'browser')::boolean,false) END,
        CASE WHEN policy.source IS NULL THEN 'public catalog policy is missing'
             WHEN policy.quarantined THEN 'public catalog ingestion is quarantined'
             WHEN NOT coalesce((policy.automation_allowed->>'browser')::boolean,false)
                  THEN 'public catalog browser discovery is disabled'
             ELSE 'public catalog browser discovery is allowed' END,
        CASE WHEN policy.source IS NULL THEN 'unknown_source'
             WHEN policy.quarantined THEN 'source_quarantined'
             WHEN NOT coalesce((policy.automation_allowed->>'browser')::boolean,false)
                  THEN 'modality_disabled' ELSE 'allowed' END
 FROM (SELECT 1) anchor LEFT JOIN public.source_policy policy ON policy.source='public_jsonld'
$$;
"""

_STATUS_SQL = """
CREATE FUNCTION public.fn_list_ingestion_admin_source_status_v1(p_include_fixtures boolean)
RETURNS TABLE(source_key text,display_name text,publisher text,mode text,region text,
 enabled boolean,retired_at timestamptz,review_status text,effective_status text,due boolean,
 last_succeeded_at timestamptz,next_due_at timestamptz,latest_run_status text,
 refresh_interval_minutes integer)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF p_include_fixtures IS NULL THEN
   RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid operator source status query';
 END IF;
 RETURN QUERY SELECT source.source_key,source.display_name,source.publisher,source.mode,source.region,
   source.enabled AND registry.retired_at IS NULL,registry.retired_at,source.review_status,
   CASE WHEN registry.retired_at IS NULL THEN source.effective_status ELSE 'retired' END,
   source.due AND registry.retired_at IS NULL,source.last_succeeded_at,
   CASE WHEN registry.retired_at IS NULL THEN source.next_due_at ELSE NULL END,
   source.latest_run_status,registry.refresh_interval_minutes
 FROM public.fn_ingestion_admin_source_states_v1(p_include_fixtures) source
 JOIN public.catalog_sources registry ON registry.source_key=source.source_key
 ORDER BY lower(source.display_name) COLLATE "C",source.source_key;
END $$;
"""

_SOURCE_SQL = _SOURCE_PROJECTION.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
# Broad and narrowly filtered pages need different join plans. PostgreSQL's generic
# plan can turn the 105-source second page into repeated full inventory scans.
_SOURCE_SQL = _replace_once(
    _SOURCE_SQL,
    "SET search_path = pg_catalog, public AS $$",
    "SET search_path = pg_catalog, public SET plan_cache_mode = force_custom_plan AS $$",
)
_SOURCE_SQL = _replace_once(
    _SOURCE_SQL,
    "public.fn_ingestion_admin_sources_base(p_include_fixtures)",
    "public.fn_ingestion_admin_source_states_v1(p_include_fixtures)",
)
_COUNT_START = _SOURCE_SQL.index("    ), catalog_counts AS MATERIALIZED (")
_COUNT_END = _SOURCE_SQL.index("    ), ranked AS (", _COUNT_START)
_SOURCE_SQL = (
    _SOURCE_SQL[:_COUNT_START]
    + """
    ), attributed_runs AS NOT MATERIALIZED (
        -- Retain the refresh primary-key uniqueness for join estimates. An opaque
        -- CTE misestimates the match and causes one event lookup per observation.
        SELECT refresh.source_key,refresh.run_key,
               public.fn_ingestion_admin_run_is_fixture(refresh.run_key,refresh.error) AS is_fixture
        FROM public.catalog_refresh_runs refresh
    ), catalog_counts AS MATERIALIZED (
        SELECT observation.source_key,
               count(DISTINCT event.canonical_event_id) FILTER (
                   WHERE NOT source.is_fixture AND (NOT p_include_fixtures OR NOT refresh.is_fixture)
               ) AS total_event_count,
               count(DISTINCT event.canonical_event_id) FILTER (
                   WHERE NOT source.is_fixture AND (NOT p_include_fixtures OR NOT refresh.is_fixture)
                     AND coalesce(event.end_at,event.start_at)>statement_timestamp()
               ) AS upcoming_event_count,
               count(DISTINCT event.canonical_event_id) FILTER (
                   WHERE event.start_at>=statement_timestamp() AND event.event_status<>'cancelled'
               ) AS discovery_event_count
        FROM filtered source
        JOIN public.catalog_event_observations observation ON observation.source_key=source.source_key
        JOIN public.canonical_events event USING(canonical_event_id)
        JOIN attributed_runs refresh ON refresh.source_key=observation.source_key AND refresh.run_key=observation.last_run_key
        WHERE p_include_fixtures OR NOT refresh.is_fixture
        GROUP BY observation.source_key
"""
    + _SOURCE_SQL[_COUNT_END:]
)
_SOURCE_SQL = _replace_once(
    _SOURCE_SQL,
    "SELECT filtered.*, count(*) OVER () AS total_count,",
    "SELECT filtered.*, count(*) OVER () AS total_count,\n               coalesce(counts.discovery_event_count,0)::bigint AS discovery_event_count,",
)
_SOURCE_SQL = _replace_once(
    _SOURCE_SQL,
    "enriched.event_count, enriched.latest_run_key",
    "enriched.discovery_event_count, enriched.latest_run_key",
)

# The input is already a visibility-filtered, normalized fact. Latest/success evidence
# needs only indexed run identities and raw success status, not the full fact projection
# (command joins, normalized errors and provenance) twice for every displayed row.
_RUN_REPLACEMENTS = (
    (
        "FROM public.fn_ingestion_admin_run_facts_v2(p_fixtures,f->>'source_key',NULL) n\n     WHERE (n.started_at,n.run_key)",
        "FROM public.catalog_refresh_runs n\n     WHERE n.source_key=f->>'source_key'\n       AND (p_fixtures OR NOT public.fn_ingestion_admin_run_is_fixture(n.run_key,n.error))\n       AND (n.started_at,n.run_key)",
    ),
    (
        "FROM public.fn_ingestion_admin_run_facts_v2(p_fixtures,f->>'source_key',NULL) n\n     WHERE n.status='succeeded'",
        "FROM public.catalog_refresh_runs n\n     WHERE n.source_key=f->>'source_key'\n       AND (p_fixtures OR NOT public.fn_ingestion_admin_run_is_fixture(n.run_key,n.error))\n       AND n.status='succeeded'",
    ),
)


def _route_run_evidence(*, optimized: bool) -> None:
    # Patch only the two existence checks in the installed definition. In
    # particular, retain 0187's captured collection window/configuration evidence
    # on both upgrade and downgrade.
    body = (
        op.get_bind()
        .execute(
            text(
                "SELECT pg_get_functiondef('public.fn_operator_run_evidence_v1(jsonb,boolean)'::regprocedure)"
            )
        )
        .scalar_one()
    )
    for before, after in _RUN_REPLACEMENTS:
        body = (
            _replace_once(body, before, after) if optimized else _replace_once(body, after, before)
        )
    op.execute(body)


def _catalog_sql() -> str:
    """Count/sort compact identities; enrich the exact winning observation only on page."""
    body = _CATALOG_PROJECTION.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    body = _replace_once(
        body,
        "SET search_path = pg_catalog, public AS $$",
        "SET search_path = pg_catalog, public SET plan_cache_mode = force_custom_plan AS $$",
    )
    select_start = body.index("        SELECT\n            event.canonical_event_id")
    from_start = body.index(
        "        FROM public.catalog_event_observations observation", select_start
    )
    payload = body[select_start:from_start]
    skinny = """        SELECT event.canonical_event_id,event.start_at,event.end_at,
            observation.source_key,left(source.display_name,500) AS source_display_name,
            observation.source AS observation_source,observation.source_event_id,observation.last_seen_at
"""
    body = body[:select_start] + skinny + body[from_start:]
    body = _replace_once(
        body,
        "    WITH matching AS MATERIALIZED (",
        """    WITH sources AS MATERIALIZED (
        SELECT source_key,display_name FROM public.catalog_sources source
        WHERE (p_source IS NULL OR source.source_key=p_source)
          AND NOT public.fn_ingestion_admin_source_is_fixture(source.source_key,source.publisher,source.seed_url)
    ), matching AS MATERIALIZED (""",
    )
    body = _replace_once(
        body,
        "JOIN public.catalog_sources source ON source.source_key=observation.source_key",
        "JOIN sources source ON source.source_key=observation.source_key",
    )
    body = _replace_once(
        body,
        "          AND NOT public.fn_ingestion_admin_source_is_fixture(source.source_key,source.publisher,source.seed_url)\n          AND NOT public.fn_ingestion_admin_run_is_fixture(refresh.run_key,refresh.error)\n",
        "          AND NOT public.fn_ingestion_admin_run_is_fixture(refresh.run_key,refresh.error)\n",
    )
    body = _replace_once(
        body,
        "    ), shown AS MATERIALIZED (\n        SELECT * FROM page ORDER BY start_at,canonical_event_id LIMIT p_limit\n    )",
        """    ), shown_ids AS MATERIALIZED (
        SELECT * FROM page ORDER BY start_at,canonical_event_id LIMIT p_limit
    ), shown AS MATERIALIZED (
"""
        + payload
        + """        FROM shown_ids chosen
        JOIN public.canonical_events event ON event.canonical_event_id=chosen.canonical_event_id
        JOIN public.catalog_event_observations observation
          ON observation.source=chosen.observation_source AND observation.source_event_id=chosen.source_event_id
         AND observation.source_key=chosen.source_key AND observation.canonical_event_id=chosen.canonical_event_id
        JOIN sources source ON source.source_key=chosen.source_key
    )""",
    )
    return body


_CATALOG_SQL = _catalog_sql()
