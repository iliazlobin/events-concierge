"""Add the reviewed SCCLD Milpitas BiblioCommons RSS source.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add an anonymous, handoff-only official Milpitas Library RSS catalog (FR-3.1/FR-10.3)."""
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss'
            )
        )
        """
    )
    # BiblioCommons returns 25 RSS items per page with no count or next link.  The reviewed
    # 15-page ceiling covers the observed feed and the adapter fails rather than silently retaining
    # a partial feed if every page is full.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('sccld-milpitas-events', 'SCCLD Milpitas Library Events',
             'Santa Clara County Library District',
             'https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=MI',
             ARRAY['https://gateway.bibliocommons.com'], 'bay_area_9_county',
             'bibliocommons_rss', true, true, now(), NULL, 360, 1500, 15)
        """
    )


def downgrade() -> None:
    """Remove BiblioCommons-only registry/provenance state before restoring the prior mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'bibliocommons_rss'
        )
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'bibliocommons_rss'
        )
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE mode = 'bibliocommons_rss'")
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (mode IN ('public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415'))
        """
    )
