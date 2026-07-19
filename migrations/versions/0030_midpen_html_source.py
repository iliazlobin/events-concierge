"""Add the reviewed Midpeninsula Regional Open Space District source.

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Midpen's anonymous, handoff-only Events & Activities list (FR-3.1/FR-10.3)."""
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
                'sf_rec_park_rss', 'midpen_html'
            )
        )
        """
    )
    # The source's nine-page, 15-row calendar is bounded in the closed adapter. A newly exposed
    # tenth page fails the durable refresh rather than silently truncating activities.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('midpen-events', 'Midpeninsula Regional Open Space District Events & Activities',
             'Midpeninsula Regional Open Space District',
             'https://www.openspace.org/get-involved/events-activities?page=0',
             ARRAY['https://www.openspace.org'], 'bay_area_9_county',
             'midpen_html', true, true, now(), NULL, 360, 1500, 9)
        """
    )


def downgrade() -> None:
    """Remove Midpen provenance before restoring the prior source-mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'midpen-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'midpen-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'midpen-events'")
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
