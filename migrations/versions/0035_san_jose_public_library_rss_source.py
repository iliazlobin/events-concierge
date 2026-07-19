"""Add the reviewed San José Public Library RSS source.

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCATION_IDS = (
    "00",
    "01",
    "02",
    "03",
    "04",
    "05",
    "06",
    "07",
    "08",
    "09",
    "10",
    "11",
    "12",
    "14",
    "15",
    "16",
    "17",
    "18",
    "19",
    "21",
    "22",
    "23",
    "24",
    "25",
    "26",
)
_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/sjpl/rss/events?" + "&".join(
    f"locations={location_id}" for location_id in _LOCATION_IDS
)


def upgrade() -> None:
    """Add San José's anonymous, handoff-only official library calendar (FR-3.1/FR-10.3)."""
    # The closed OR-filtered physical feed exposes 25 rows per page with no count or next cursor. The initial
    # 60-page ceiling remains fail-closed while the first normal paced refresh verifies the 90-day source volume.
    op.execute(
        f"""
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('san-jose-public-library-events', 'San José Public Library Events', 'San José Public Library',
             '{_SEED_URL}', ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 1500, 60)
        """
    )


def downgrade() -> None:
    """Remove San José-only provenance without affecting the other BiblioCommons sources."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'san-jose-public-library-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'san-jose-public-library-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'san-jose-public-library-events'")
