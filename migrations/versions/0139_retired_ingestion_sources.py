"""Make superseded ingestion-source retirement durable and fail closed.

Revision ID: 0139
Revises: 0138
Create Date: 2026-08-01

Disabled is an operator-controlled, reversible state.  Retired is an immutable registry fact used
when one reviewed source has been replaced by another.  Retired sources stay visible as evidence,
but cannot be admitted by cadence or re-enabled through either configuration capability.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0139"
down_revision: str | None = "0138"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_LIST = "(text,text,text,text,text,text,boolean,text,text,integer,integer)"
_SOURCE_DETAIL = "(text,boolean)"
_DUE = "(timestamp with time zone,integer)"
_UPDATE = (
    "(text,integer,text,text[],text,boolean,boolean,timestamp with time zone,"
    "integer,integer,integer,text)"
)
_BULK = "(jsonb,boolean,text)"

_SUPERSEDED_SOURCES = (
    (
        "alameda-county-library-fremont-events",
        "alameda-county-library-all-physical-branches-events",
    ),
    ("sccld-milpitas-events", "sccld-all-physical-branches-events"),
    ("sccld-saratoga-events", "sccld-all-physical-branches-events"),
    ("smcl-millbrae-events", "smcl-all-physical-branches-events"),
)


def upgrade() -> None:
    """Persist retirement and expose lifecycle-aware admin capabilities."""
    op.execute(
        """
        ALTER TABLE public.catalog_sources
        ADD COLUMN retired_at timestamptz,
        ADD COLUMN retired_reason text,
        ADD COLUMN superseded_by_source_key text,
        ADD CONSTRAINT fk_catalog_sources_superseded_by_source_key
            FOREIGN KEY (superseded_by_source_key)
            REFERENCES public.catalog_sources(source_key) ON DELETE RESTRICT,
        ADD CONSTRAINT ck_catalog_sources_retirement_shape
            CHECK (
                (
                    retired_at IS NULL
                    AND retired_reason IS NULL
                    AND superseded_by_source_key IS NULL
                )
                OR (
                    retired_at IS NOT NULL
                    AND retired_reason IS NOT NULL
                    AND char_length(retired_reason) BETWEEN 1 AND 160
                    AND retired_reason !~ '[[:cntrl:]]'
                    AND (
                        superseded_by_source_key IS NULL
                        OR superseded_by_source_key <> source_key
                    )
                )
            ),
        ADD CONSTRAINT ck_catalog_sources_retired_disabled
            CHECK (retired_at IS NULL OR NOT enabled)
        """
    )
    values = ",\n".join(
        f"('{source_key}', '{replacement_key}')"
        for source_key, replacement_key in _SUPERSEDED_SOURCES
    )
    op.execute(
        f"""
        UPDATE public.catalog_sources AS source
        SET enabled = false,
            retired_at = clock_timestamp(),
            retired_reason = 'superseded_by_aggregate_source',
            superseded_by_source_key = replacement.replacement_key
        FROM (VALUES {values}) AS replacement(source_key, replacement_key)
        WHERE source.source_key = replacement.source_key
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_guard_catalog_source_retirement()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF OLD.retired_at IS NOT NULL
               AND (
                   NEW.retired_at IS DISTINCT FROM OLD.retired_at
                   OR NEW.retired_reason IS DISTINCT FROM OLD.retired_reason
                   OR NEW.superseded_by_source_key
                        IS DISTINCT FROM OLD.superseded_by_source_key
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'catalog source retirement is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        REVOKE ALL ON FUNCTION public.fn_guard_catalog_source_retirement() FROM PUBLIC
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_source_retirement_immutable
        BEFORE UPDATE ON public.catalog_sources
        FOR EACH ROW
        EXECUTE FUNCTION public.fn_guard_catalog_source_retirement()
        """
    )
    _create_source_list()
    _create_source_detail()
    _create_due_list()
    _create_update_guard()
    _create_bulk_guard()


def downgrade() -> None:
    """Remove the additive capabilities and metadata; seeded sources remain disabled."""
    for function, signature in (
        ("fn_set_ingestion_admin_sources_enabled_v2", _BULK),
        ("fn_update_ingestion_admin_source_configuration_v2", _UPDATE),
        ("fn_list_ingestion_admin_due_sources_v2", _DUE),
        ("fn_get_ingestion_admin_source_detail_v2", _SOURCE_DETAIL),
        ("fn_list_ingestion_admin_sources_v6", _SOURCE_LIST),
    ):
        qualified = f"public.{function}{signature}"
        op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {qualified}")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_catalog_source_retirement_immutable "
        "ON public.catalog_sources"
    )
    op.execute("DROP FUNCTION IF EXISTS public.fn_guard_catalog_source_retirement()")
    op.execute(
        """
        ALTER TABLE public.catalog_sources
        DROP CONSTRAINT IF EXISTS ck_catalog_sources_retired_disabled,
        DROP CONSTRAINT IF EXISTS ck_catalog_sources_retirement_shape,
        DROP CONSTRAINT IF EXISTS fk_catalog_sources_superseded_by_source_key,
        DROP COLUMN IF EXISTS superseded_by_source_key,
        DROP COLUMN IF EXISTS retired_reason,
        DROP COLUMN IF EXISTS retired_at
        """
    )


def _grant(function: str, signature: str) -> None:
    qualified = f"public.{function}{signature}"
    op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {qualified} TO ec_app")


def _create_source_list() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_sources_v6(
            p_query text,
            p_state text,
            p_mode text,
            p_publisher text,
            p_region text,
            p_source_key text,
            p_include_fixtures boolean,
            p_sort_by text,
            p_sort_direction text,
            p_limit integer,
            p_offset integer
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            publisher text,
            mode text,
            region text,
            seed_url text,
            enabled boolean,
            source_revision integer,
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
            total_count bigint,
            retired_at timestamptz,
            retired_reason text,
            superseded_by_source_key text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT listed.source_key,
                   listed.display_name,
                   listed.publisher,
                   listed.mode,
                   listed.region,
                   listed.seed_url,
                   CASE WHEN registry.retired_at IS NULL THEN listed.enabled ELSE false END,
                   listed.source_revision,
                   listed.review_status,
                   CASE
                       WHEN registry.retired_at IS NOT NULL THEN 'retired'
                       ELSE listed.effective_status
                   END,
                   listed.policy_blocked,
                   CASE WHEN registry.retired_at IS NULL THEN listed.due ELSE false END,
                   listed.last_succeeded_at,
                   CASE
                       WHEN registry.retired_at IS NULL THEN listed.next_due_at
                       ELSE NULL
                   END,
                   listed.event_count,
                   listed.latest_run_key,
                   listed.latest_run_status,
                   listed.latest_run_started_at,
                   listed.latest_run_completed_at,
                   listed.latest_run_candidate_count,
                   listed.latest_run_canonical_count,
                   listed.latest_run_error,
                   listed.latest_run_attempt_count,
                   listed.latest_run_duration_ms,
                   listed.latest_run_source_revision,
                   listed.latest_run_release_revision,
                   listed.latest_run_image_digest,
                   listed.latest_run_provenance_status,
                   listed.latest_run_trigger,
                   listed.total_count,
                   registry.retired_at,
                   registry.retired_reason,
                   registry.superseded_by_source_key
            FROM public.fn_list_ingestion_admin_sources_v5(
                p_query, p_state, p_mode, p_publisher, p_region, p_source_key,
                p_include_fixtures, p_sort_by, p_sort_direction, p_limit, p_offset
            ) WITH ORDINALITY AS listed
            JOIN public.catalog_sources AS registry
              ON registry.source_key = listed.source_key
            ORDER BY listed.ordinality
        $$
        """
    )
    _grant("fn_list_ingestion_admin_sources_v6", _SOURCE_LIST)


def _create_source_detail() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_admin_source_detail_v2(
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
    )
    _grant("fn_get_ingestion_admin_source_detail_v2", _SOURCE_DETAIL)


def _create_due_list() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_due_sources_v2(
            p_now timestamptz,
            p_limit integer
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            publisher text,
            seed_url text,
            approved_origins text[],
            region text,
            mode text,
            handoff_only boolean,
            enabled boolean,
            reviewed_at timestamptz,
            review_expires_at timestamptz,
            refresh_interval_minutes integer,
            min_interval_ms integer,
            page_limit integer,
            source_revision integer,
            due_at timestamptz,
            last_succeeded_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT due_source.*
            FROM public.fn_list_ingestion_admin_due_sources(p_now, p_limit) AS due_source
            JOIN public.catalog_sources AS registry
              ON registry.source_key = due_source.source_key
            WHERE registry.retired_at IS NULL
            ORDER BY due_source.due_at, due_source.source_key
        $$
        """
    )
    _grant("fn_list_ingestion_admin_due_sources_v2", _DUE)


def _create_update_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_update_ingestion_admin_source_configuration_v2(
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
            p_requested_by text
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
            v_source public.catalog_sources%ROWTYPE;
        BEGIN
            IF p_enabled IS TRUE THEN
                SELECT source.*
                INTO v_source
                FROM public.catalog_sources AS source
                WHERE source.source_key = p_source_key;
                IF FOUND AND v_source.retired_at IS NOT NULL THEN
                    RETURN QUERY SELECT 'unavailable', v_source.source_revision,
                                        v_source.reviewed_at, v_source.updated_at;
                    RETURN;
                END IF;
            END IF;
            RETURN QUERY
            SELECT updated.outcome,
                   updated.source_revision,
                   updated.reviewed_at,
                   updated.updated_at
            FROM public.fn_update_ingestion_admin_source_configuration(
                p_source_key, p_expected_revision, p_seed_url, p_approved_origins,
                p_mode, p_enabled, p_handoff_only, p_review_expires_at,
                p_refresh_interval_minutes, p_min_interval_ms, p_page_limit,
                p_requested_by
            ) AS updated;
        END;
        $$
        """
    )
    _grant("fn_update_ingestion_admin_source_configuration_v2", _UPDATE)


def _create_bulk_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION public.fn_set_ingestion_admin_sources_enabled_v2(
            p_targets jsonb,
            p_enabled boolean,
            p_requested_by text
        )
        RETURNS TABLE (
            outcome text,
            requested_count integer,
            updated_count integer,
            unchanged_count integer,
            result_source_key text,
            source_revision integer,
            reviewed_at timestamptz,
            updated_at timestamptz
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_requested integer;
        BEGIN
            IF p_enabled IS TRUE
               AND jsonb_typeof(p_targets) = 'array'
               AND jsonb_array_length(p_targets) BETWEEN 1 AND 100
               AND EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(p_targets) AS target(value)
                   JOIN public.catalog_sources AS source
                     ON source.source_key = target.value ->> 'source_key'
                   WHERE source.retired_at IS NOT NULL
               )
            THEN
                v_requested := jsonb_array_length(p_targets);
                RETURN QUERY
                SELECT 'unavailable', v_requested, 0, 0, NULL::text, NULL::integer,
                       NULL::timestamptz, NULL::timestamptz;
                RETURN;
            END IF;
            RETURN QUERY
            SELECT updated.outcome,
                   updated.requested_count,
                   updated.updated_count,
                   updated.unchanged_count,
                   updated.result_source_key,
                   updated.source_revision,
                   updated.reviewed_at,
                   updated.updated_at
            FROM public.fn_set_ingestion_admin_sources_enabled(
                p_targets, p_enabled, p_requested_by
            ) AS updated;
        END;
        $$
        """
    )
    _grant("fn_set_ingestion_admin_sources_enabled_v2", _BULK)
