"""Add the reviewed Cal Performances public collection source.

Revision ID: 0039
Revises: 0038
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Cal Performances' bounded anonymous, handoff-only collection (FR-3.1/FR-10.3)."""
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
    # The closed adapter constructs every X-WP-TotalPages page itself and fails when the source
    # declares more than four, so the registry cap cannot silently truncate this collection.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('calperformances-events', 'Cal Performances Events', 'Cal Performances',
             'https://calperformances.org/wp-json/wp/v2/cp_event?per_page=100&page=1',
             ARRAY['https://calperformances.org'], 'bay_area_9_county',
             'calperformances_json', true, true, now(), NULL, 360, 1500, 4)
        """
    )


def downgrade() -> None:
    """Remove Cal Performances provenance before restoring the preceding source-mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'calperformances-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'calperformances-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'calperformances-events'")
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
