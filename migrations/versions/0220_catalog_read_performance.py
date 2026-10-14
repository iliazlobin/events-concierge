"""Avoid repeated catalog admission scans and fence short-lived public read caches.

Revision ID: 0220
Revises: 0219
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0220"
down_revision: str | None = "0219"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDERS = (
    "public.fn_list_filtered_catalog_providers_v1"
    "(text,timestamptz,timestamptz,text[],text[],text,integer,integer,text[],text)"
)
_TOKEN = "public.fn_catalog_public_read_token_v1()"
_REGISTRY_SCAN = """public.fn_list_current_catalog_providers_v3(
                p_source_key, p_window_start, p_window_end
            )"""
_REGISTRY = """public.fn_list_admitted_catalog_browse_sources_v1(p_source_key)"""


def _definition(signature: str) -> str:
    return str(
        op.get_bind()
        .execute(
            text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": signature},
        )
        .scalar_one()
    )


def upgrade() -> None:
    # Exact indexed observation lookup, with the same source, successful-run,
    # retained-past and collection-window admission as the existing browse reader.
    # The old helper re-ran the entire source projection for every returned card.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.fn_catalog_observation_card_v1(
            p_source_key text, p_source text, p_source_event_id text, p_event_id uuid
        ) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $function$
        SELECT jsonb_build_object('visibility', observed.visibility,
                                 'location_withheld', observed.location_withheld)
        FROM public.catalog_event_observations AS observed
        JOIN public.canonical_events AS event USING (canonical_event_id)
        JOIN public.catalog_refresh_runs AS observed_run
          ON observed_run.source_key=observed.source_key
         AND observed_run.run_key=observed.last_run_key
        JOIN public.fn_list_admitted_catalog_browse_sources_v2(ARRAY[p_source_key]) AS source
          ON source.source_key=observed.source_key
        LEFT JOIN LATERAL (
            SELECT refresh.run_key FROM public.catalog_refresh_runs AS refresh
            WHERE refresh.source_key=observed.source_key AND refresh.status='succeeded'
              AND refresh.completed_at IS NOT NULL
              AND NOT public.fn_ingestion_admin_run_is_fixture(refresh.run_key,refresh.error)
            ORDER BY refresh.completed_at DESC,refresh.run_key DESC LIMIT 1
        ) AS latest ON true
        WHERE observed.source_key=p_source_key AND observed.source::text=p_source
          AND observed.source_event_id=p_source_event_id
          AND observed.canonical_event_id=p_event_id
          AND observed_run.status='succeeded' AND observed_run.completed_at IS NOT NULL
          AND NOT public.fn_ingestion_admin_run_is_fixture(observed_run.run_key,observed_run.error)
          AND event.event_status<>'cancelled'
          AND (
            coalesce(event.end_at,event.start_at)<=statement_timestamp()
            OR latest.run_key=observed.last_run_key
            OR (
                EXISTS(SELECT 1 FROM public.catalog_refresh_collection_windows AS collection
                       WHERE collection.source_key=observed.source_key AND collection.run_key=latest.run_key)
                AND public.fn_catalog_observation_is_current_v1(
                    observed.source_key,observed.last_run_key,event.start_at
                )
            )
          )
        $function$
    """)
    providers = _definition(_PROVIDERS)
    if providers.count(_REGISTRY_SCAN) != 1:
        raise RuntimeError("unexpected filtered provider registry contract")
    # Counts already contain the filtered admission scan. Listing registry labels
    # must not scan the same event catalog a second time.
    op.execute(providers.replace(_REGISTRY_SCAN, _REGISTRY))
    op.execute("""
        CREATE FUNCTION public.fn_catalog_public_read_token_v1() RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
          WITH admitted AS MATERIALIZED (
            SELECT source.*, registry.source_revision
            FROM public.fn_list_admitted_catalog_browse_sources_v2('{}'::text[]) AS source
            JOIN public.catalog_sources AS registry USING(source_key)
          ), publications AS (
            SELECT refresh.source_key,
                   md5(string_agg(jsonb_build_array(refresh.run_key,refresh.completed_at,
                       collection.start_at,collection.end_at)::text,',' ORDER BY refresh.run_key)) AS token
            FROM public.catalog_refresh_runs AS refresh
            JOIN admitted USING(source_key)
            LEFT JOIN public.catalog_refresh_collection_windows AS collection
              USING(source_key,run_key)
            WHERE refresh.status='succeeded' AND refresh.completed_at IS NOT NULL
              AND NOT public.fn_ingestion_admin_run_is_fixture(refresh.run_key,refresh.error)
            GROUP BY refresh.source_key
          )
          SELECT md5(coalesce(string_agg(
              jsonb_build_array(admitted.source_key,admitted.source_label,admitted.publisher,
                  admitted.provider,admitted.seed_url,admitted.source_revision,publications.token)::text,
              ';' ORDER BY admitted.source_key),'empty'))
          FROM admitted LEFT JOIN publications USING(source_key)
        $$
    """)
    op.execute(f"REVOKE ALL ON FUNCTION {_TOKEN} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_TOKEN} TO ec_app")


def downgrade() -> None:
    # Restore the reviewed 0218 helper; visibility/history data is untouched.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.fn_catalog_observation_card_v1(
            p_source_key text,p_source text,p_source_event_id text,p_event_id uuid
        ) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
          SELECT jsonb_build_object('visibility',observed.visibility,
                                   'location_withheld',observed.location_withheld)
          FROM public.canonical_events AS event
          CROSS JOIN LATERAL public.fn_list_retained_catalog_browse_observations_v2(
            ARRAY[p_source_key],event.start_at-interval '1 microsecond',
            event.start_at+interval '1 microsecond') AS admitted
          JOIN public.catalog_event_observations AS observed
            ON observed.source_key=admitted.source_key
           AND observed.source::text=admitted.observation_source
           AND observed.source_event_id=admitted.source_event_id
           AND observed.canonical_event_id=admitted.canonical_event_id
           AND observed.last_run_key=admitted.refresh_run_key
          WHERE event.canonical_event_id=p_event_id AND admitted.canonical_event_id=p_event_id
            AND admitted.source_key=p_source_key AND admitted.observation_source=p_source
            AND admitted.source_event_id=p_source_event_id
        $$
    """)
    providers = _definition(_PROVIDERS)
    if providers.count(_REGISTRY) != 1:
        raise RuntimeError("unexpected optimized provider registry contract")
    op.execute(providers.replace(_REGISTRY, _REGISTRY_SCAN))
    op.execute(f"DROP FUNCTION {_TOKEN}")
