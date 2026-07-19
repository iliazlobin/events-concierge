"""Seed the reviewed San Mateo County Libraries physical-branch RSS source.

Revision ID: 0070
Revises: 0069
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCATION_IDS = ("1A", "1B", "1R", "1E", "1F", "1H", "1M", "1N", "1Z", "1P", "1V", "1S", "1W")
_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events?" + "&".join(
    f"locations={location_id}" for location_id in _LOCATION_IDS
)


def upgrade() -> None:
    """Add SMCL's reviewed, physical-only public RSS seed (FR-3.1/FR-10.3)."""
    op.execute(
        f"""
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('smcl-all-physical-branches-events',
             'San Mateo County Libraries: All Physical Branch Events',
             'San Mateo County Libraries', '{_SEED_URL}',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 5000, 150)
        """
    )


def downgrade() -> None:
    """Remove SMCL physical-branch refresh provenance and its reviewed source seed."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'smcl-all-physical-branches-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'smcl-all-physical-branches-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'smcl-all-physical-branches-events'")
