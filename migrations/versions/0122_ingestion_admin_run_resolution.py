"""Annotate ingestion run history with authoritative current and resolution state.

Revision ID: 0122
Revises: 0121
Create Date: 2026-07-28

The bounded run ledger is useful for forensic history, but a status-filtered or offset-paged
client cannot determine whether a returned run is still the source's latest durable result.
This additive v3 capability computes source-relative annotations over the complete normalized
run facts before applying status, time-window, or pagination filters.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0122"
down_revision: str | None = "0121"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_LIST_V3 = "(text,text,text,text,text,integer,boolean,integer,integer)"


def upgrade() -> None:
    """Install the annotated, bounded run-list capability."""
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_runs_v3(
            p_status text,
            p_source_key text,
            p_mode text,
            p_publisher text,
            p_region text,
            p_window_hours integer,
            p_include_fixtures boolean,
            p_limit integer,
            p_offset integer
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            run_key text,
            status text,
            started_at timestamptz,
            completed_at timestamptz,
            candidate_count integer,
            canonical_count integer,
            error text,
            attempt_count integer,
            duration_ms bigint,
            source_revision integer,
            release_revision text,
            image_digest text,
            provenance_status text,
            trigger text,
            is_latest_for_source boolean,
            resolved_by_newer_success boolean,
            total_count bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_include_fixtures IS NULL
               OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_offset IS NULL OR p_offset < 0 OR p_offset > 100000
               OR (p_status IS NOT NULL
                   AND p_status NOT IN ('running', 'paused', 'succeeded', 'failed'))
               OR (p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$')
               OR (p_mode IS NOT NULL
                   AND char_length(p_mode) NOT BETWEEN 1 AND 80)
               OR (p_publisher IS NOT NULL
                   AND char_length(p_publisher) NOT BETWEEN 1 AND 300)
               OR (p_region IS NOT NULL
                   AND char_length(p_region) NOT BETWEEN 1 AND 120)
               OR (p_window_hours IS NOT NULL
                   AND (p_window_hours < 1 OR p_window_hours > 2160))
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin run query is invalid';
            END IF;

            RETURN QUERY
            WITH observed AS (
                SELECT pg_catalog.statement_timestamp() AS observed_at
            ), scoped AS MATERIALIZED (
                SELECT facts.*
                FROM public.fn_ingestion_admin_run_facts_v2(
                    p_include_fixtures,
                    p_source_key,
                    NULL
                ) AS facts
                WHERE (p_source_key IS NULL OR facts.source_key = p_source_key)
                  AND (p_mode IS NULL OR facts.mode = p_mode)
                  AND (p_publisher IS NULL OR facts.publisher = p_publisher)
                  AND (p_region IS NULL OR facts.region = p_region)
            ), annotated AS MATERIALIZED (
                SELECT scoped.*,
                       (
                           row_number() OVER (
                               PARTITION BY scoped.source_key
                               ORDER BY scoped.started_at DESC, scoped.run_key DESC
                           ) = 1
                       ) AS is_latest_for_source,
                       CASE
                           WHEN scoped.status = 'failed'
                               THEN COALESCE(
                                   bool_or(scoped.status = 'succeeded') OVER (
                                       PARTITION BY scoped.source_key
                                       ORDER BY scoped.started_at DESC, scoped.run_key DESC
                                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                                   ),
                                   false
                               )
                           ELSE false
                       END AS resolved_by_newer_success
                FROM scoped
            ), filtered AS (
                SELECT annotated.*
                FROM annotated
                CROSS JOIN observed
                WHERE (p_status IS NULL OR annotated.status = p_status)
                  AND (
                      p_window_hours IS NULL
                      OR annotated.started_at >= observed.observed_at
                          - p_window_hours * INTERVAL '1 hour'
                  )
            )
            SELECT filtered.source_key,
                   filtered.display_name,
                   filtered.run_key,
                   filtered.status,
                   filtered.started_at,
                   filtered.completed_at,
                   filtered.candidate_count,
                   filtered.canonical_count,
                   filtered.error,
                   filtered.attempt_count,
                   filtered.duration_ms,
                   filtered.source_revision,
                   filtered.release_revision,
                   filtered.image_digest,
                   filtered.provenance_status,
                   filtered.trigger,
                   filtered.is_latest_for_source,
                   filtered.resolved_by_newer_success,
                   count(*) OVER () AS total_count
            FROM filtered
            ORDER BY filtered.started_at DESC, filtered.source_key, filtered.run_key DESC
            LIMIT p_limit
            OFFSET p_offset;
        END;
        $$
        """
    )
    signature = f"public.fn_list_ingestion_admin_runs_v3{_RUN_LIST_V3}"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove v3 while retaining the unchanged v2 run-list capability."""
    signature = f"public.fn_list_ingestion_admin_runs_v3{_RUN_LIST_V3}"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {signature}")
