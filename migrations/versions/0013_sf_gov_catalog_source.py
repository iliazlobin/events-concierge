"""Add the reviewed SF.gov related-events source.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the anonymous, publisher-owned SF.gov API under the existing review boundary (FR-3.1)."""
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (mode IN ('public_jsonld', 'livewhale_json', 'sf_gov_json'))
        """
    )
    # The endpoint currently returns ten flattened events per page.  A 25-page cap covers its
    # reported 220 upcoming events with a bounded 250-record ceiling; the adapter fails rather
    # than silently claiming coverage if the publisher grows beyond the reviewed cap.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('sf-gov-related-events', 'SF.gov Citywide Events', 'City and County of San Francisco',
             'https://api.sf.gov/api/related-events/?list=upcoming&locale=en&page=1&groupby=date',
             ARRAY['https://api.sf.gov'], 'bay_area_9_county',
             'sf_gov_json', true, true, now(), NULL, 360, 1500, 25)
        """
    )


def downgrade() -> None:
    """Remove SF.gov-only registry/provenance state before restoring the prior mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'sf_gov_json'
        )
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'sf_gov_json'
        )
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE mode = 'sf_gov_json'")
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (mode IN ('public_jsonld', 'livewhale_json'))
        """
    )
