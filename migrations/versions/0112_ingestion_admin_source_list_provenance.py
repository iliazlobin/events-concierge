"""Enrich source-list latest runs with the same safe provenance as run detail.

Revision ID: 0112
Revises: 0111
Create Date: 2026-07-23

The 0111 source-list capability intentionally retained its legacy shape. That made its nested
latest-run projection look like legacy provenance even when the exact run/detail capabilities
could associate an admin command with a source revision and worker claim. This additive v3
capability enriches only the already-bounded source page in one set-based query.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0112"
down_revision: str | None = "0111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_LIST_V3 = "(text,text,text,text,text,text,boolean,integer,integer)"


def upgrade() -> None:
    """Install the provenance-enriched, bounded source-list capability."""
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_sources_v3(
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
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_include_fixtures IS NULL
               OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_offset IS NULL OR p_offset < 0 OR p_offset > 100000
               OR (p_query IS NOT NULL AND char_length(p_query) > 200)
               OR p_state IS NULL
               OR p_state NOT IN ('all', 'active', 'due', 'blocked', 'failed')
               OR (p_mode IS NOT NULL AND (
                    char_length(p_mode) NOT BETWEEN 1 AND 80 OR p_mode ~ '[[:cntrl:]]'
               ))
               OR (p_publisher IS NOT NULL AND (
                    char_length(p_publisher) NOT BETWEEN 1 AND 300
                    OR p_publisher ~ '[[:cntrl:]]'
               ))
               OR (p_region IS NOT NULL AND (
                    char_length(p_region) NOT BETWEEN 1 AND 120 OR p_region ~ '[[:cntrl:]]'
               ))
               OR (
                   p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin source query is invalid';
            END IF;

            RETURN QUERY
            WITH filtered AS (
                SELECT source.*
                FROM public.fn_ingestion_admin_sources_base(p_include_fixtures) AS source
                WHERE (
                        p_query IS NULL
                        OR strpos(
                            lower(
                                source.source_key || ' ' || source.display_name || ' '
                                || source.publisher || ' ' || source.region || ' '
                                || source.mode || ' ' || source.seed_url
                            ),
                            lower(p_query)
                        ) > 0
                    )
                  AND (p_source_key IS NULL OR source.source_key = p_source_key)
                  AND (p_mode IS NULL OR source.mode = p_mode)
                  AND (p_publisher IS NULL OR source.publisher = p_publisher)
                  AND (p_region IS NULL OR source.region = p_region)
                  AND (
                      p_state = 'all'
                      OR (
                          p_state = 'active'
                          AND source.effective_status IN ('active', 'due', 'running')
                      )
                      OR (p_state = 'due' AND source.due)
                      OR (
                          p_state = 'blocked'
                          AND source.effective_status IN (
                              'disabled', 'unreviewed', 'review_expired', 'policy_blocked'
                          )
                      )
                      OR (p_state = 'failed' AND source.latest_run_status = 'failed')
                  )
            ), counted AS (
                SELECT filtered.*,
                       count(*) OVER () AS total_count
                FROM filtered
            ), page AS MATERIALIZED (
                SELECT counted.*
                FROM counted
                ORDER BY counted.source_key
                LIMIT p_limit
                OFFSET p_offset
            ), enriched AS (
                SELECT page.*,
                       command.command_id AS admin_command_id,
                       COALESCE(
                           progress.source_revision,
                           command.executor_source_revision,
                           command.source_revision
                       ) AS effective_source_revision,
                       command.executor_release_revision AS claim_release_revision,
                       command.executor_image_digest AS claim_image_digest
                FROM page
                LEFT JOIN public.ingestion_admin_commands AS command
                  ON command.action = 'refresh_source'
                 AND command.source_key = page.source_key
                 AND page.latest_run_key = 'admin:' || command.command_id::text
                LEFT JOIN public.catalog_refresh_progress AS progress
                  ON progress.source_key = page.source_key
                 AND progress.run_key = page.latest_run_key
            )
            SELECT enriched.source_key,
                   enriched.display_name,
                   enriched.publisher,
                   enriched.mode,
                   enriched.region,
                   enriched.seed_url,
                   enriched.enabled,
                   enriched.review_status,
                   enriched.effective_status,
                   enriched.policy_blocked,
                   enriched.due,
                   enriched.last_succeeded_at,
                   enriched.next_due_at,
                   enriched.event_count,
                   enriched.latest_run_key,
                   enriched.latest_run_status,
                   enriched.latest_run_started_at,
                   enriched.latest_run_completed_at,
                   enriched.latest_run_candidate_count,
                   enriched.latest_run_canonical_count,
                   enriched.latest_run_error,
                   enriched.latest_run_attempt_count,
                   CASE
                       WHEN enriched.latest_run_completed_at IS NULL THEN NULL
                       ELSE GREATEST(
                           0,
                           floor(
                               extract(
                                   epoch FROM enriched.latest_run_completed_at
                                       - enriched.latest_run_started_at
                               ) * 1000
                           )::bigint
                       )
                   END,
                   enriched.effective_source_revision,
                   enriched.claim_release_revision,
                   enriched.claim_image_digest,
                   CASE
                       WHEN enriched.admin_command_id IS NOT NULL
                        AND enriched.effective_source_revision IS NOT NULL
                        AND enriched.claim_release_revision IS NOT NULL
                           THEN 'claim_recorded'
                       WHEN enriched.effective_source_revision IS NOT NULL
                           THEN 'source_revision_only'
                       ELSE 'legacy_unavailable'
                   END,
                   CASE
                       WHEN enriched.admin_command_id IS NOT NULL THEN 'admin_source'
                       ELSE 'cadence_or_manual'
                   END,
                   enriched.total_count
            FROM enriched
            ORDER BY enriched.source_key;
        END;
        $$
        """
    )
    signature = f"public.fn_list_ingestion_admin_sources_v3{_SOURCE_LIST_V3}"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove v3 so the unchanged 0111 source-list capability becomes authoritative again."""
    signature = f"public.fn_list_ingestion_admin_sources_v3{_SOURCE_LIST_V3}"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {signature}")
