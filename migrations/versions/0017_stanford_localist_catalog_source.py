"""Promote Stanford's reviewed homepage seed to its official Localist API.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Use Stanford's documented anonymous occurrence API without changing publisher provenance (FR-3.1)."""
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
    # The source key continues to identify Stanford University, while the reviewed feed changes
    # from one homepage JSON-LD document to its documented, occurrence-expanded Localist API.
    # The adapter owns a fixed local 90-day query and trusts only the API's declared page count;
    # the 20-page cap fails rather than claiming a partial catalog if Stanford grows beyond it.
    op.execute(
        """
        UPDATE catalog_sources
        SET display_name = 'Stanford Events Localist Calendar',
            seed_url = 'https://events.stanford.edu/api/2/events',
            mode = 'localist_json',
            page_limit = 20,
            reviewed_at = now()
        WHERE source_key = 'stanford-events'
        """
    )


def downgrade() -> None:
    """Restore the prior reviewed homepage source before dropping the Localist-only mode."""
    op.execute(
        """
        UPDATE catalog_sources
        SET display_name = 'Stanford Events',
            seed_url = 'https://events.stanford.edu/',
            mode = 'public_jsonld',
            page_limit = 1
        WHERE source_key = 'stanford-events'
        """
    )
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar'
            )
        )
        """
    )
