"""Add the reviewed Gardens of Golden Gate Park event-list source.

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Gardens of Golden Gate Park's anonymous official event list (FR-3.1/FR-10.3)."""
    # The public API declares its finite total/page count. Five 50-record pages cover 250 rows;
    # the shared closed adapter fails above that review ceiling rather than retaining a partial feed.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('gardens-golden-gate-park-events', 'Gardens of Golden Gate Park Events',
             'Gardens of Golden Gate Park',
             'https://gggp.org/wp-json/tribe/events/v1/events',
             ARRAY['https://gggp.org'], 'bay_area_9_county',
             'tribe_events_json', true, true, now(), NULL, 360, 1500, 5)
        """
    )


def downgrade() -> None:
    """Remove Gardens of Golden Gate Park provenance without affecting OMCA's source profile."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'gardens-golden-gate-park-events'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'gardens-golden-gate-park-events'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'gardens-golden-gate-park-events'")
