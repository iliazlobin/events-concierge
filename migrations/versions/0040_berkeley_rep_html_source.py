"""Add the reviewed Berkeley Repertory Theatre public show-list source.

Revision ID: 0040
Revises: 0039
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Berkeley Rep's bounded anonymous, handoff-only show list (FR-3.1/FR-10.3)."""
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
                'berkeley_rep_html'
            )
        )
        """
    )
    # Robots specifies Crawl-delay: 5. The closed adapter reads only this one SSR list and fails
    # if its nine-card list capacity or an unreviewed pager would make discovery incomplete.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('berkeley-rep-shows', 'Berkeley Repertory Theatre Shows',
             'Berkeley Repertory Theatre', 'https://www.berkeleyrep.org/shows',
             ARRAY['https://www.berkeleyrep.org', 'https://tickets.berkeleyrep.org'],
             'bay_area_9_county', 'berkeley_rep_html', true, true, now(), NULL, 360, 5000, 1)
        """
    )


def downgrade() -> None:
    """Remove Berkeley Rep provenance before restoring the preceding source-mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'berkeley-rep-shows'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'berkeley-rep-shows'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'berkeley-rep-shows'")
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
                'civic_engage_rss', 'midpen_html', 'usfca_html', 'calperformances_json'
            )
        )
        """
    )
