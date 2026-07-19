"""Seed the reviewed Marin County Free Library RSS source.

Revision ID: 0069
Revises: 0068
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCATION_IDS = ("MB", "MC", "MM", "MF", "MI", "MA", "MN", "MP", "MH", "MS")
_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/marinlibrary/rss/events?" + "&".join(
    f"locations={location_id}" for location_id in _LOCATION_IDS
)


def upgrade() -> None:
    """Add Marin's reviewed, handoff-only public RSS seed (FR-3.1/FR-10.3)."""
    op.execute(
        f"""
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('marin-county-free-library-events', 'Marin County Free Library Events',
             'Marin County Free Library', '{_SEED_URL}',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 5000, 25)
        """
    )


def downgrade() -> None:
    """Remove Marin-only refresh provenance and its reviewed source seed."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'marin-county-free-library-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'marin-county-free-library-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'marin-county-free-library-events'")
