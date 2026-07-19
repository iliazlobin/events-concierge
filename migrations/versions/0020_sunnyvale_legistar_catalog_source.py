"""Add the reviewed Sunnyvale Legistar public-meetings source.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the anonymous, handoff-only City Clerk civic calendar source (FR-3.1/FR-10.3)."""
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar', 'localist_json'
            )
        )
        """
    )
    # The adapter owns a fixed local 90-day OData filter and 100-record pages. Five pages cover
    # 500 public meetings while retaining a <=7.5-second paced request ceiling; a full fifth page
    # fails the durable run instead of claiming a partial city calendar.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('sunnyvale-legistar-meetings', 'Sunnyvale Public Meetings',
             'City of Sunnyvale City Clerk',
             'https://webapi.legistar.com/v1/SunnyvaleCA/Events',
             ARRAY['https://webapi.legistar.com'], 'bay_area_9_county',
             'sunnyvale_legistar', true, true, now(), NULL, 360, 1500, 5)
        """
    )


def downgrade() -> None:
    """Remove Sunnyvale provenance before restoring the prior catalog mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'sunnyvale_legistar'
        )
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key IN (
            SELECT source_key
            FROM catalog_sources
            WHERE mode = 'sunnyvale_legistar'
        )
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE mode = 'sunnyvale_legistar'")
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'localist_json'
            )
        )
        """
    )
