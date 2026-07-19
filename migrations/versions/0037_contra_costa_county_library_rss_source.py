"""Add the reviewed Contra Costa County Library RSS source.

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCATION_IDS = (
    "43",
    "7",
    "25",
    "19",
    "14",
    "60",
    "4",
    "21",
    "26",
    "24",
    "11",
    "16",
    "6",
    "55",
    "9",
    "94",
    "1",
    "17",
    "12",
    "15",
    "27",
    "23",
    "8",
    "2",
    "13",
)
_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/ccclib/rss/events?" + "&".join(
    f"locations={location_id}" for location_id in _LOCATION_IDS
)


def upgrade() -> None:
    """Add Contra Costa's anonymous, handoff-only official library calendar (FR-3.1/FR-10.3)."""
    # The publisher's public index reports 2,606 rows across the same closed current physical location facets.
    # BiblioCommons returns 25 RSS rows per page, so the 105-page cap covers that broader set; the narrower
    # 90-day request still stops at its first short page and fails rather than preserving a partial source.
    op.execute(
        f"""
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('contra-costa-county-library-events', 'Contra Costa County Library Events',
             'Contra Costa County Library', '{_SEED_URL}',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 1500, 105)
        """
    )


def downgrade() -> None:
    """Remove Contra Costa-only provenance without affecting the other BiblioCommons sources."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'contra-costa-county-library-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'contra-costa-county-library-events'
        """
    )
    op.execute(
        "DELETE FROM catalog_sources WHERE source_key = 'contra-costa-county-library-events'"
    )
