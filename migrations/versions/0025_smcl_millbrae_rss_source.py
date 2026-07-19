"""Add the reviewed San Mateo County Libraries Millbrae BiblioCommons RSS source.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Millbrae's anonymous, handoff-only official library calendar (FR-3.1/FR-10.3)."""
    # BiblioCommons returns 25 RSS rows per page without a count or next cursor. The reviewed
    # 15-page ceiling covers the observed Millbrae 90-day date window and fails rather than
    # silently retaining a partial source if every page is full.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('smcl-millbrae-events', 'San Mateo County Libraries: Millbrae Events',
             'San Mateo County Libraries',
             'https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events?locations=1M',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 1500, 15)
        """
    )


def downgrade() -> None:
    """Remove Millbrae-only provenance without affecting the other BiblioCommons sources."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'smcl-millbrae-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'smcl-millbrae-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'smcl-millbrae-events'")
