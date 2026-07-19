"""Add the reviewed DataSF Our415 catalog source.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the anonymous, handoff-only Our415 Socrata source (FR-3.1/FR-10.3)."""
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (mode IN ('public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415'))
        """
    )
    # The adapter owns fixed query fields, a 100-row page size, and an owner-approved 90-day
    # occurrence window.  The 25-page cap permits 2,500 raw rows; it fails rather than persists a
    # partial result if the public dataset grows beyond that reviewed bound.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('datasf-our415-events', 'DataSF Our415 Activities',
             'City and County of San Francisco',
             'https://data.sfgov.org/resource/8i3s-ih2a.json',
             ARRAY['https://data.sfgov.org'], 'bay_area_9_county',
             'datasf_our415', true, true, now(), NULL, 360, 1500, 25)
        """
    )


def downgrade() -> None:
    """Remove DataSF-only registry/provenance state before restoring the prior mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'datasf_our415'
        )
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'datasf_our415'
        )
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE mode = 'datasf_our415'")
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (mode IN ('public_jsonld', 'livewhale_json', 'sf_gov_json'))
        """
    )
