"""Promote UCSF's reviewed homepage seed to its official Localist API.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Use UCSF's reviewed anonymous Localist occurrence API without duplicating publisher provenance."""
    op.execute(
        """
        UPDATE catalog_sources
        SET display_name = 'UCSF Events Localist Calendar',
            seed_url = 'https://calendar.ucsf.edu/api/2/events',
            mode = 'localist_json',
            page_limit = 20,
            reviewed_at = now()
        WHERE source_key = 'ucsf-events'
        """
    )


def downgrade() -> None:
    """Restore UCSF's prior reviewed homepage source."""
    op.execute(
        """
        UPDATE catalog_sources
        SET display_name = 'UCSF Events',
            seed_url = 'https://calendar.ucsf.edu/',
            mode = 'public_jsonld',
            page_limit = 1
        WHERE source_key = 'ucsf-events'
        """
    )
