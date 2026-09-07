"""Expose a bounded catalog-wide city inventory for consumer filtering.

Revision ID: 0146
Revises: 0145
Create Date: 2026-08-03

City autocomplete must not depend on whichever event cards happen to be in the first browse page.
The facet follows the same admitted-source/latest-success projection as the public catalog and
returns only cities with at least one upcoming event.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0146"
down_revision: str | None = "0145"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CITY_FACETS_V1 = "public.fn_list_current_catalog_city_facets_v1()"


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_list_current_catalog_city_facets_v1()
        RETURNS TABLE (city text, event_count bigint)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH browse_observations AS MATERIALIZED (
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v1(NULL, NULL, NULL)
            )
            SELECT event.city_norm AS city,
                   count(DISTINCT event.canonical_event_id) AS event_count
            FROM public.canonical_events AS event
            JOIN browse_observations AS observation
              ON observation.canonical_event_id = event.canonical_event_id
            WHERE nullif(btrim(event.city_norm), '') IS NOT NULL
            GROUP BY event.city_norm
            ORDER BY event_count DESC, lower(event.city_norm), event.city_norm
            LIMIT 500
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_CITY_FACETS_V1} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_CITY_FACETS_V1} TO ec_app")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION {_CITY_FACETS_V1} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {_CITY_FACETS_V1}")
