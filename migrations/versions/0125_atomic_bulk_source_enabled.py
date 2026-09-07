"""Add atomic reviewed bulk source-state changes and list configuration revisions.

Revision ID: 0125
Revises: 0124
Create Date: 2026-07-29

Bulk source state changes are exact optimistic registry revisions, not refresh commands.  The
application role receives one bounded capability that locks every requested source in a stable
order, validates the complete set before mutating anything, and preserves the existing per-source
configuration audit contract.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0125"
down_revision: str | None = "0124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_LIST_V4 = "(text,text,text,text,text,text,boolean,integer,integer)"
_BULK_ENABLED = "(jsonb,boolean,text)"


def upgrade() -> None:
    """Expose config revisions and install one all-or-nothing enabled-state capability."""
    _create_source_list_v4()
    _create_bulk_enabled_capability()


def downgrade() -> None:
    """Remove only the additive list and bulk-state capabilities."""
    bulk_signature = f"public.fn_set_ingestion_admin_sources_enabled{_BULK_ENABLED}"
    source_signature = f"public.fn_list_ingestion_admin_sources_v4{_SOURCE_LIST_V4}"
    op.execute(f"REVOKE ALL ON FUNCTION {bulk_signature} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {bulk_signature}")
    op.execute(f"REVOKE ALL ON FUNCTION {source_signature} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {source_signature}")


def _create_source_list_v4() -> None:
    """Add the current registry revision without conflating it with run provenance."""
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_sources_v4(
            p_query text,
            p_state text,
            p_mode text,
            p_publisher text,
            p_region text,
            p_source_key text,
            p_include_fixtures boolean,
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
            total_count bigint
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
                   listed.enabled,
                   source.source_revision,
                   listed.review_status,
                   listed.effective_status,
                   listed.policy_blocked,
                   listed.due,
                   listed.last_succeeded_at,
                   listed.next_due_at,
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
                   listed.total_count
            FROM public.fn_list_ingestion_admin_sources_v3(
                p_query, p_state, p_mode, p_publisher, p_region, p_source_key,
                p_include_fixtures, p_limit, p_offset
            ) AS listed
            JOIN public.catalog_sources AS source
              ON source.source_key = listed.source_key
            ORDER BY listed.source_key;
        $$
        """
    )
    signature = f"public.fn_list_ingestion_admin_sources_v4{_SOURCE_LIST_V4}"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def _create_bulk_enabled_capability() -> None:
    """Install one bounded, deterministic, per-source-audited registry transition."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_set_ingestion_admin_sources_enabled(
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
            v_now timestamptz := clock_timestamp();
            v_requested integer := 0;
            v_updated_count integer := 0;
            v_target record;
            v_source public.catalog_sources%ROWTYPE;
            v_updated public.catalog_sources%ROWTYPE;
            v_before jsonb;
            v_after jsonb;
        BEGIN
            IF p_targets IS NULL
               OR jsonb_typeof(p_targets) IS DISTINCT FROM 'array'
               OR p_enabled IS NULL
               OR p_requested_by IS NULL
               OR char_length(p_requested_by) NOT BETWEEN 1 AND 256
               OR p_requested_by ~ '[[:cntrl:]]'
            THEN
                RETURN QUERY
                SELECT 'invalid', 0, 0, 0, NULL::text, NULL::integer,
                       NULL::timestamptz, NULL::timestamptz;
                RETURN;
            END IF;

            v_requested := jsonb_array_length(p_targets);
            IF v_requested NOT BETWEEN 1 AND 100
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(p_targets) AS target(value)
                    WHERE jsonb_typeof(target.value) IS DISTINCT FROM 'object'
               )
            THEN
                RETURN QUERY
                SELECT 'invalid', 0, 0, 0, NULL::text, NULL::integer,
                       NULL::timestamptz, NULL::timestamptz;
                RETURN;
            END IF;

            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(p_targets) AS target(value)
                WHERE (
                        SELECT count(*)
                        FROM jsonb_object_keys(target.value)
                      ) <> 2
                   OR NOT target.value ? 'source_key'
                   OR NOT target.value ? 'expected_revision'
                   OR jsonb_typeof(target.value -> 'source_key') IS DISTINCT FROM 'string'
                   OR jsonb_typeof(target.value -> 'expected_revision') IS DISTINCT FROM 'number'
                   OR target.value ->> 'source_key'
                        !~ '^[a-z0-9][a-z0-9-]{1,79}$'
                   OR target.value ->> 'expected_revision' !~ '^[1-9][0-9]{0,9}$'
                   OR char_length(target.value ->> 'expected_revision') > 10
                   OR (
                        char_length(target.value ->> 'expected_revision') = 10
                        AND target.value ->> 'expected_revision' > '2147483647'
                   )
            )
            OR (
                SELECT count(DISTINCT target.value ->> 'source_key')
                FROM jsonb_array_elements(p_targets) AS target(value)
            ) <> v_requested
            THEN
                RETURN QUERY
                SELECT 'invalid', 0, 0, 0, NULL::text, NULL::integer,
                       NULL::timestamptz, NULL::timestamptz;
                RETURN;
            END IF;

            -- Lock and validate the full target set before the first mutation. Stable ordering
            -- prevents concurrent overlapping bulk requests from choosing opposing lock orders.
            FOR v_target IN
                SELECT parsed.source_key, parsed.expected_revision
                FROM jsonb_to_recordset(p_targets)
                    AS parsed(source_key text, expected_revision integer)
                ORDER BY parsed.source_key
            LOOP
                SELECT source.*
                INTO v_source
                FROM public.catalog_sources AS source
                WHERE source.source_key = v_target.source_key
                FOR UPDATE;

                IF NOT FOUND THEN
                    RETURN QUERY
                    SELECT 'not_found', v_requested, 0, 0, NULL::text, NULL::integer,
                           NULL::timestamptz, NULL::timestamptz;
                    RETURN;
                END IF;
                IF v_source.source_revision <> v_target.expected_revision THEN
                    RETURN QUERY
                    SELECT 'conflict', v_requested, 0, 0, NULL::text, NULL::integer,
                           NULL::timestamptz, NULL::timestamptz;
                    RETURN;
                END IF;
                IF p_enabled
                   AND (
                       NOT v_source.handoff_only
                       OR v_source.reviewed_at IS NULL
                       OR v_source.reviewed_at > v_now
                       OR (
                           v_source.review_expires_at IS NOT NULL
                           AND v_source.review_expires_at <= v_now
                       )
                   )
                THEN
                    RETURN QUERY
                    SELECT 'unavailable', v_requested, 0, 0, NULL::text, NULL::integer,
                           NULL::timestamptz, NULL::timestamptz;
                    RETURN;
                END IF;
                IF v_source.enabled IS DISTINCT FROM p_enabled THEN
                    v_updated_count := v_updated_count + 1;
                END IF;
            END LOOP;

            FOR v_target IN
                SELECT parsed.source_key, parsed.expected_revision
                FROM jsonb_to_recordset(p_targets)
                    AS parsed(source_key text, expected_revision integer)
                ORDER BY parsed.source_key
            LOOP
                SELECT source.*
                INTO STRICT v_source
                FROM public.catalog_sources AS source
                WHERE source.source_key = v_target.source_key;

                IF v_source.enabled IS NOT DISTINCT FROM p_enabled THEN
                    CONTINUE;
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
                    'source_revision', v_source.source_revision
                );

                UPDATE public.catalog_sources AS source
                SET enabled = p_enabled,
                    reviewed_at = v_now,
                    source_revision = source.source_revision + 1,
                    updated_at = v_now
                WHERE source.source_key = v_target.source_key
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
                    'source_revision', v_updated.source_revision
                );

                INSERT INTO public.catalog_source_configuration_audit (
                    source_key, prior_revision, new_revision, requested_by,
                    changed_at, before_config, after_config
                )
                VALUES (
                    v_target.source_key, v_source.source_revision, v_updated.source_revision,
                    p_requested_by, v_now, v_before, v_after
                );

                outcome := 'updated';
                requested_count := v_requested;
                updated_count := v_updated_count;
                unchanged_count := v_requested - v_updated_count;
                result_source_key := v_updated.source_key;
                source_revision := v_updated.source_revision;
                reviewed_at := v_updated.reviewed_at;
                updated_at := v_updated.updated_at;
                RETURN NEXT;
            END LOOP;

            IF v_updated_count = 0 THEN
                RETURN QUERY
                SELECT 'updated', v_requested, 0, v_requested, NULL::text, NULL::integer,
                       NULL::timestamptz, NULL::timestamptz;
            END IF;
        END;
        $$
        """
    )
    signature = f"public.fn_set_ingestion_admin_sources_enabled{_BULK_ENABLED}"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
