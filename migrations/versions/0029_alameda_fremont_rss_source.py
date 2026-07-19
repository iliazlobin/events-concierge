"""Add the reviewed Alameda County Library Fremont RSS source.

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Fremont's anonymous, handoff-only official library calendar (FR-3.1/FR-10.3)."""
    # The date-bounded feed returns 25 rows per page and no count or next cursor. The reviewed
    # seven-page ceiling covers the observed 141-row window and fails when all seven pages are full.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('alameda-county-library-fremont-events', 'Alameda County Library: Fremont Events',
             'Alameda County Library',
             'https://gateway.bibliocommons.com/v2/libraries/aclibrary/rss/events?locations=FRM',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 1500, 7)
        """
    )


def downgrade() -> None:
    """Remove Fremont-only provenance without affecting other BiblioCommons publishers."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'alameda-county-library-fremont-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'alameda-county-library-fremont-events'
        """
    )
    op.execute(
        "DELETE FROM catalog_sources WHERE source_key = 'alameda-county-library-fremont-events'"
    )
