"""Seed the first reviewed official Bay Area JSON-LD calendars.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add only reviewed publisher calendars; they remain explicit/manual read-only refresh targets."""
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms)
        VALUES
            ('stanford-events', 'Stanford Events', 'Stanford University',
             'https://events.stanford.edu/', ARRAY['https://events.stanford.edu'], 'bay_area_9_county',
             'public_jsonld', true, true, now(), NULL, 360, 1500),
            ('ucsf-events', 'UCSF Events', 'University of California, San Francisco',
             'https://calendar.ucsf.edu/', ARRAY['https://calendar.ucsf.edu'], 'bay_area_9_county',
             'public_jsonld', true, true, now(), NULL, 360, 1500),
            ('sjsu-events', 'SJSU Events', 'San José State University',
             'https://events.sjsu.edu/', ARRAY['https://events.sjsu.edu'], 'bay_area_9_county',
             'public_jsonld', true, true, now(), NULL, 360, 1500)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key IN ('stanford-events', 'ucsf-events', 'sjsu-events')
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key IN ('stanford-events', 'ucsf-events', 'sjsu-events')
        """
    )
    op.execute(
        """
        DELETE FROM catalog_sources
        WHERE source_key IN ('stanford-events', 'ucsf-events', 'sjsu-events')
        """
    )
