"""Add the reviewed SCCLD Saratoga BiblioCommons RSS source.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Saratoga's anonymous, handoff-only official library calendar (FR-3.1/FR-10.3)."""
    # BiblioCommons returns 25 RSS items per page with no count or next link. The reviewed 15-page
    # ceiling covers the observed Saratoga feed and fails rather than silently retaining a partial
    # source if every page is full.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('sccld-saratoga-events', 'SCCLD Saratoga Library Events',
             'Santa Clara County Library District',
             'https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=SA',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 1500, 15)
        """
    )


def downgrade() -> None:
    """Remove Saratoga-only provenance without affecting the pre-existing Milpitas source."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'sccld-saratoga-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'sccld-saratoga-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'sccld-saratoga-events'")
