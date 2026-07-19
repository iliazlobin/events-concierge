"""Seed the reviewed Alameda County Library physical-branch RSS source.

Revision ID: 0074
Revises: 0073
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCATION_IDS = ("ALB", "CSV", "CTV", "CHY", "DUB", "FRM", "NWK", "NLS", "SLZ", "UCY")
_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/aclibrary/rss/events?" + "&".join(
    f"locations={location_id}" for location_id in _LOCATION_IDS
)


def upgrade() -> None:
    """Add Alameda County Library's reviewed physical-only public RSS seed (FR-3.1/FR-10.3)."""
    op.execute(
        f"""
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('alameda-county-library-all-physical-branches-events',
             'Alameda County Library: All Physical Branch Events',
             'Alameda County Library', '{_SEED_URL}',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 5000, 40)
        """
    )


def downgrade() -> None:
    """Remove Alameda County physical-branch refresh provenance and its reviewed source seed."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'alameda-county-library-all-physical-branches-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'alameda-county-library-all-physical-branches-events'
        """
    )
    op.execute(
        "DELETE FROM catalog_sources WHERE source_key = 'alameda-county-library-all-physical-branches-events'"
    )
