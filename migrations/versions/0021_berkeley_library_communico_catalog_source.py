"""Add the reviewed Berkeley Public Library Communico calendar source.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the anonymous, handoff-only official Berkeley Public Library event list (FR-3.1/FR-10.3)."""
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar',
                'communico_json', 'localist_json'
            )
        )
        """
    )
    # The reviewed Communico endpoint accepts a fixed local start date and inclusive-looking
    # 91-day request. The adapter clips locally to a 90-day half-open horizon, validates a
    # maximum 599 rows / 2 MB response, and fails rather than claiming an unbounded result.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('berkeley-public-library-events', 'Berkeley Public Library Events',
             'Berkeley Public Library',
             'https://berkeleypubliclibrary.libnet.info/eeventcaldata',
             ARRAY['https://berkeleypubliclibrary.libnet.info'], 'bay_area_9_county',
             'communico_json', true, true, now(), NULL, 360, 1500, 1)
        """
    )


def downgrade() -> None:
    """Remove Berkeley Public Library provenance before restoring the prior mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'communico_json'
        )
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'communico_json'
        )
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE mode = 'communico_json'")
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar', 'localist_json'
            )
        )
        """
    )
