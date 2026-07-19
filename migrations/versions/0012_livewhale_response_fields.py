"""Request the reviewed LiveWhale fields needed for a useful public catalog.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RICH_PATH = "/live/json/events/response_fields/location,summary,description"
_BASIC_PATH = "/live/json/events/"


def upgrade() -> None:
    """Ask only for documented location and text fields, retaining LiveWhale v2 performance (FR-3.7)."""
    op.execute(
        f"""
        UPDATE catalog_sources
        SET seed_url = CASE source_key
            WHEN 'berkeley-events' THEN 'https://events.berkeley.edu{_RICH_PATH}'
            WHEN 'scu-events' THEN 'https://events.scu.edu{_RICH_PATH}'
            WHEN 'smccd-events' THEN 'https://events.smccd.edu{_RICH_PATH}'
            ELSE seed_url
        END,
        updated_at = now()
        WHERE source_key IN ('berkeley-events', 'scu-events', 'smccd-events')
        """
    )


def downgrade() -> None:
    """Restore the initial minimal event feed paths."""
    op.execute(
        f"""
        UPDATE catalog_sources
        SET seed_url = CASE source_key
            WHEN 'berkeley-events' THEN 'https://events.berkeley.edu{_BASIC_PATH}'
            WHEN 'scu-events' THEN 'https://events.scu.edu{_BASIC_PATH}'
            WHEN 'smccd-events' THEN 'https://events.smccd.edu{_BASIC_PATH}'
            ELSE seed_url
        END,
        updated_at = now()
        WHERE source_key IN ('berkeley-events', 'scu-events', 'smccd-events')
        """
    )
