"""Add reviewed, bounded LiveWhale public-event sources.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add bounded, owner-reviewed public LiveWhale catalogs (FR-3.1/FR-10.3)."""
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD COLUMN page_limit integer NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_page_limit
        CHECK (page_limit > 0)
        """
    )
    # 0008 created this unnamed CHECK; PostgreSQL assigned catalog_sources_mode_check.
    op.execute(
        """
        ALTER TABLE catalog_sources
        DROP CONSTRAINT IF EXISTS catalog_sources_mode_check
        """
    )
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (mode IN ('public_jsonld', 'livewhale_json'))
        """
    )
    # The three reviewed publishers expose anonymous, documented LiveWhale JSON.  A cap of three
    # 100-record pages is a deliberately bounded quality pass, not a claim of full-catalog crawl.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('berkeley-events', 'UC Berkeley Events', 'University of California, Berkeley',
             'https://events.berkeley.edu/live/json/events/',
             ARRAY['https://events.berkeley.edu'], 'bay_area_9_county',
             'livewhale_json', true, true, now(), NULL, 720, 1500, 3),
            ('scu-events', 'Santa Clara University Events', 'Santa Clara University',
             'https://events.scu.edu/live/json/events/',
             ARRAY['https://events.scu.edu'], 'bay_area_9_county',
             'livewhale_json', true, true, now(), NULL, 720, 1500, 3),
            ('smccd-events', 'SMCCD Events',
             'San Mateo County Community College District',
             'https://events.smccd.edu/live/json/events/',
             ARRAY['https://events.smccd.edu'], 'bay_area_9_county',
             'livewhale_json', true, true, now(), NULL, 720, 1500, 3)
        """
    )


def downgrade() -> None:
    """Remove LiveWhale-only control-plane/provenance rows before restoring the old mode set."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'livewhale_json'
        )
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'livewhale_json'
        )
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE mode = 'livewhale_json'")
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT catalog_sources_mode_check
        CHECK (mode IN ('public_jsonld'))
        """
    )
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_page_limit")
    op.execute("ALTER TABLE catalog_sources DROP COLUMN page_limit")
