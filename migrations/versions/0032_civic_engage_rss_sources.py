"""Generalize the CivicEngage RSS source type and add Campbell.

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Use the closed shared RSS type and add Campbell's rolling calendar (FR-3.1/FR-10.3)."""
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        UPDATE catalog_sources
        SET mode = 'civic_engage_rss'
        WHERE source_key = 'sf-rec-park-events'
        """
    )
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar',
                'communico_json', 'tribe_events_json', 'localist_json', 'libcal_ics',
                'civic_engage_rss', 'midpen_html'
            )
        )
        """
    )
    # This official rolling feed has no cursor, count, or next link. The closed publisher profile
    # accepts only one document and fails at its reviewed 50-item ceiling rather than claiming a
    # partial near-term calendar.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('campbell-events', 'Campbell Recreation & Community Services Events',
             'City of Campbell Recreation & Community Services',
             'https://www.campbellca.gov/RSSFeed.aspx?CID=Recreation-Community-Services-29&ModID=58',
             ARRAY['https://www.campbellca.gov'], 'bay_area_9_county',
             'civic_engage_rss', true, true, now(), NULL, 360, 1500, 1)
        """
    )


def downgrade() -> None:
    """Remove Campbell then restore the original SF-specific registry type."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'campbell-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'campbell-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'campbell-events'")
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        UPDATE catalog_sources
        SET mode = 'sf_rec_park_rss'
        WHERE source_key = 'sf-rec-park-events'
        """
    )
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar',
                'communico_json', 'tribe_events_json', 'localist_json', 'libcal_ics',
                'sf_rec_park_rss', 'midpen_html'
            )
        )
        """
    )
