"""Add the reviewed City of Los Altos CivicEngage RSS source.

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Los Altos's anonymous, handoff-only rolling calendar (FR-3.1/FR-10.3)."""
    # The official feed has no cursor, count, or next link. The closed profile accepts one document
    # and fails at its reviewed 50-item ceiling rather than claiming a partial rolling window.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('los-altos-events', 'City of Los Altos Events', 'City of Los Altos',
             'https://www.losaltosca.gov/RSSFeed.aspx?CID=All-calendar.xml&ModID=58',
             ARRAY['https://www.losaltosca.gov'], 'bay_area_9_county',
             'civic_engage_rss', true, true, now(), NULL, 360, 1500, 1)
        """
    )


def downgrade() -> None:
    """Remove Los Altos source provenance before removing its registry record."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'los-altos-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'los-altos-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'los-altos-events'")
