"""Add the reviewed University of San Francisco Main Campus calendar source.

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add USFCA's anonymous, handoff-only Main Campus calendar (FR-3.1/FR-10.3)."""
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
                'civic_engage_rss', 'midpen_html', 'usfca_html'
            )
        )
        """
    )
    # The reviewed Main Campus list is currently one non-paginated document. Its closed adapter
    # rejects a pager or page query, so a new list page cannot silently truncate discovery.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('usfca-main-campus-events', 'University of San Francisco Main Campus Events',
             'University of San Francisco',
             'https://www.usfca.edu/life-at-usf/events?field_campus%5B179%5D=179',
             ARRAY['https://www.usfca.edu'], 'bay_area_9_county',
             'usfca_html', true, true, now(), NULL, 360, 1500, 1)
        """
    )


def downgrade() -> None:
    """Remove USFCA provenance before restoring the preceding source-mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'usfca-main-campus-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'usfca-main-campus-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'usfca-main-campus-events'")
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
                'civic_engage_rss', 'midpen_html'
            )
        )
        """
    )
