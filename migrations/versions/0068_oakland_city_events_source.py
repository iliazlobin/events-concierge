"""Add the reviewed City of Oakland public event-calendar source.

Revision ID: 0068
Revises: 0067
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Oakland's sitemap-bounded, anonymous, handoff-only catalog (FR-3.1/FR-10.3)."""
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
                'civic_engage_rss', 'midpen_html', 'usfca_html', 'calperformances_json',
                'berkeley_rep_html', 'ybca_html', 'oakland_html'
            )
        )
        """
    )
    # The closed adapter enumerates same-origin Event-Calendar detail URLs from the public sitemap,
    # never automates the WebForms list pager, and fails closed at this capacity rather than silently
    # returning a partial catalog.  It fetches factual metadata only and hands users back to Oakland.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('oakland-city-events', 'City of Oakland Event Calendar', 'City of Oakland',
             'https://www.oaklandca.gov/sitemap.xml', ARRAY['https://www.oaklandca.gov'],
             'bay_area_9_county', 'oakland_html', true, true, now(), NULL, 1440, 5000, 160)
        """
    )


def downgrade() -> None:
    """Remove Oakland provenance before restoring the preceding source-mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'oakland-city-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'oakland-city-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'oakland-city-events'")
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
                'civic_engage_rss', 'midpen_html', 'usfca_html', 'calperformances_json',
                'berkeley_rep_html', 'ybca_html'
            )
        )
        """
    )
