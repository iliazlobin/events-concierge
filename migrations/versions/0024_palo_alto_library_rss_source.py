"""Add the reviewed Palo Alto City Library BiblioCommons RSS source.

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Palo Alto's anonymous, handoff-only official library calendar (FR-3.1/FR-10.3)."""
    # The reviewed date-bounded publisher feed exposes 25 RSS items per page without a count or
    # next link. Its 15-page ceiling covers the observed 90-day window and fails rather than
    # retaining a partial source when every reviewed page is full.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('palo-alto-library-events', 'Palo Alto City Library Events',
             'Palo Alto City Library',
             'https://gateway.bibliocommons.com/v2/libraries/paloalto/rss/events',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 1500, 15)
        """
    )


def downgrade() -> None:
    """Remove Palo Alto-only provenance without affecting the other BiblioCommons sources."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'palo-alto-library-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'palo-alto-library-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'palo-alto-library-events'")
