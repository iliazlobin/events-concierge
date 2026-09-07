"""Make ingestion-admin source facet counts follow the active source filters.

Revision ID: 0120
Revises: 0119
Create Date: 2026-07-28

The original facet projection counted the complete non-fixture registry. That made a region
option continue to say, for example, ``39`` while the source table was scoped to only active,
due, failed, searched, or otherwise-filtered records. This additive capability applies the same
query and state semantics as the source list and uses standard disjunctive faceting: every
dimension honors the other selected dimensions while keeping all of its own alternatives visible.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0120"
down_revision: str | None = "0119"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FILTERS_V2 = "(text,text,text,text,text,boolean)"


def upgrade() -> None:
    """Install the contextual, fixture-aware source facet capability."""
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_filter_values_v2(
            p_query text,
            p_state text,
            p_mode text,
            p_publisher text,
            p_region text,
            p_include_fixtures boolean
        )
        RETURNS TABLE (
            dimension text,
            value text,
            source_count bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_include_fixtures IS NULL
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
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin filter query is invalid';
            END IF;

            RETURN QUERY
            WITH scoped AS MATERIALIZED (
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
            ), facet_values AS (
                SELECT 'mode'::text AS dimension,
                       source.mode AS value,
                       count(*) AS source_count
                FROM scoped AS source
                WHERE (p_publisher IS NULL OR source.publisher = p_publisher)
                  AND (p_region IS NULL OR source.region = p_region)
                GROUP BY source.mode

                UNION ALL

                SELECT 'publisher',
                       source.publisher,
                       count(*)
                FROM scoped AS source
                WHERE (p_mode IS NULL OR source.mode = p_mode)
                  AND (p_region IS NULL OR source.region = p_region)
                GROUP BY source.publisher

                UNION ALL

                SELECT 'region',
                       source.region,
                       count(*)
                FROM scoped AS source
                WHERE (p_mode IS NULL OR source.mode = p_mode)
                  AND (p_publisher IS NULL OR source.publisher = p_publisher)
                GROUP BY source.region
            )
            SELECT facet_values.dimension,
                   facet_values.value,
                   facet_values.source_count
            FROM facet_values
            WHERE facet_values.value IS NOT NULL
              AND btrim(facet_values.value) <> ''
            ORDER BY facet_values.dimension,
                     lower(facet_values.value),
                     facet_values.value;
        END;
        $$
        """
    )
    signature = f"public.fn_list_ingestion_admin_filter_values_v2{_FILTERS_V2}"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    """Remove the contextual facet capability and retain the 0111 global projection."""
    signature = f"public.fn_list_ingestion_admin_filter_values_v2{_FILTERS_V2}"
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {signature}")
