"""Add the reviewed Oakland Public Library RSS source.

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCATION_IDS = (
    "81A",
    "AAA",
    "ASA",
    "BRA",
    "CCA",
    "DMA",
    "EAA",
    "ELA",
    "GGA",
    "KGA",
    "LVA",
    "MEA",
    "MOA",
    "OHR",
    "PMA",
    "RRA",
    "TMA",
    "WAS",
    "XXA",
    "XXJ",
    "XXY",
    "676de3ef74596c36004dc6bd",
    "6a0c99a4e8af4a2f00739408",
    "6a517185e9de6536001ad4d7",
)
_SEED_URL = "https://gateway.bibliocommons.com/v2/libraries/oaklandlibrary/rss/events?" + "&".join(
    f"locations={location_id}" for location_id in _LOCATION_IDS
)


def upgrade() -> None:
    """Add Oakland's anonymous, handoff-only official library calendar (FR-3.1/FR-10.3)."""
    # The closed OR-filtered physical feed returns 25 rows per page without a count or next cursor. Its
    # reviewed 60-page ceiling covers the observed 1,165-row 90-day window and fails rather than preserving
    # a partial source if all reviewed pages are full.
    op.execute(
        f"""
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('oakland-public-library-events', 'Oakland Public Library Events', 'Oakland Public Library',
             '{_SEED_URL}', ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 1500, 60)
        """
    )


def downgrade() -> None:
    """Remove Oakland-only provenance without affecting the other BiblioCommons sources."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'oakland-public-library-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'oakland-public-library-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'oakland-public-library-events'")
