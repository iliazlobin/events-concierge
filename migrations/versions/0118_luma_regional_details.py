"""Add New York and approve structured detail enrichment for Luma regions.

Revision ID: 0118
Revises: 0117
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0118"
down_revision: str | None = "0117"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Promote SF to the Bay Area label and add the reviewed New York cursor."""
    op.execute(
        """
        UPDATE catalog_sources
        SET display_name = 'Luma Bay Area',
            approved_origins = ARRAY[
                'https://api.luma.com',
                'https://api2.luma.com'
            ],
            source_revision = 2,
            updated_at = now()
        WHERE source_key = 'luma-sf'
        """
    )
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit, source_revision)
        VALUES
            ('luma-nyc', 'Luma New York', 'Luma Discover',
             'https://api.luma.com/discover/get-paginated-events?discover_place_api_id=discplace-Izx1rQVSh8njYpP&pagination_limit=25',
             ARRAY['https://api.luma.com', 'https://api2.luma.com'],
             'new_york_metro', 'luma_discover_json',
             true, true, now(), NULL, 120, 1500, 40, 1)
        ON CONFLICT (source_key) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            publisher = EXCLUDED.publisher,
            seed_url = EXCLUDED.seed_url,
            approved_origins = EXCLUDED.approved_origins,
            region = EXCLUDED.region,
            mode = EXCLUDED.mode,
            handoff_only = EXCLUDED.handoff_only,
            enabled = EXCLUDED.enabled,
            reviewed_at = EXCLUDED.reviewed_at,
            review_expires_at = EXCLUDED.review_expires_at,
            refresh_interval_minutes = EXCLUDED.refresh_interval_minutes,
            min_interval_ms = EXCLUDED.min_interval_ms,
            page_limit = EXCLUDED.page_limit,
            source_revision = EXCLUDED.source_revision,
            updated_at = now()
        """
    )


def downgrade() -> None:
    """Retire New York safely and restore the pre-enrichment SF contract."""
    op.execute(
        """
        UPDATE catalog_sources
        SET seed_url = 'https://luma.com/nyc',
            approved_origins = ARRAY['https://luma.com'],
            mode = 'public_jsonld',
            enabled = false,
            page_limit = 1,
            source_revision = 1,
            updated_at = now()
        WHERE source_key = 'luma-nyc'
        """
    )
    op.execute(
        """
        UPDATE catalog_sources
        SET display_name = 'Luma San Francisco',
            approved_origins = ARRAY['https://api.luma.com'],
            source_revision = 1,
            updated_at = now()
        WHERE source_key = 'luma-sf'
        """
    )
