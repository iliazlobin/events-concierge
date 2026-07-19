"""Add the reviewed San Francisco Recreation & Parks RSS source.

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add SF Recreation & Parks' anonymous, handoff-only rolling Main Calendar (FR-3.1/FR-10.3)."""
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar',
                'communico_json', 'tribe_events_json', 'localist_json', 'libcal_ics',
                'sf_rec_park_rss'
            )
        )
        """
    )
    # The expressly published feed has no cursor, total, or next link. The closed adapter accepts
    # one document at a time and fails at its reviewed 200-item ceiling instead of claiming a
    # partial rolling window.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('sf-rec-park-events', 'San Francisco Recreation & Parks Events',
             'San Francisco Recreation & Park Department',
             'https://sfrecpark.org/RSSFeed.aspx?CID=Main-Calendar-14&ModID=58',
             ARRAY['https://sfrecpark.org'], 'bay_area_9_county',
             'sf_rec_park_rss', true, true, now(), NULL, 360, 1500, 1)
        """
    )


def downgrade() -> None:
    """Remove SF Recreation & Parks provenance before restoring the prior source-mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'sf-rec-park-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'sf-rec-park-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'sf-rec-park-events'")
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar',
                'communico_json', 'tribe_events_json', 'localist_json', 'libcal_ics'
            )
        )
        """
    )
