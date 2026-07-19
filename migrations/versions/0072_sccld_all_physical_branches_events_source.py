"""Seed the reviewed Santa Clara County Library District physical-branch RSS source.

Revision ID: 0072
Revises: 0071
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCATION_IDS = ("CA", "CU", "GI", "LA", "MI", "MH", "SA", "WO")
_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?" + "&".join(
    f"locations={location_id}" for location_id in _LOCATION_IDS
)


def upgrade() -> None:
    """Add SCCLD's reviewed, physical-only public RSS seed (FR-3.1/FR-10.3)."""
    op.execute(
        f"""
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('sccld-all-physical-branches-events',
             'Santa Clara County Library District: All Physical Branch Events',
             'Santa Clara County Library District', '{_SEED_URL}',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 5000, 50)
        """
    )


def downgrade() -> None:
    """Remove SCCLD physical-branch refresh provenance and its reviewed source seed."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'sccld-all-physical-branches-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'sccld-all-physical-branches-events'
        """
    )
    op.execute(
        "DELETE FROM catalog_sources WHERE source_key = 'sccld-all-physical-branches-events'"
    )
