"""Add the reviewed Oakland Museum of California public event-list source.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add OMCA's anonymous, handoff-only official The Events Calendar list API (FR-3.1/FR-10.3)."""
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar',
                'communico_json', 'tribe_events_json', 'localist_json'
            )
        )
        """
    )
    # The source declares finite totals/pages. Five 50-record pages cover 250 public listings;
    # a higher declared count fails the durable refresh rather than retaining a partial calendar.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('omca-events', 'Oakland Museum of California Events',
             'Oakland Museum of California',
             'https://museumca.org/wp-json/tribe/events/v1/events',
             ARRAY['https://museumca.org'], 'bay_area_9_county',
             'tribe_events_json', true, true, now(), NULL, 360, 1500, 5)
        """
    )


def downgrade() -> None:
    """Remove OMCA provenance before restoring the prior catalog mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'tribe_events_json'
        )
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'tribe_events_json'
        )
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE mode = 'tribe_events_json'")
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
