"""Correct Midpen's reviewed bounded page cap.

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Set the reviewed nine-page Midpen next-chain cap (FR-10.3/NFR-8)."""
    op.execute("UPDATE catalog_sources SET page_limit = 9 WHERE source_key = 'midpen-events'")


def downgrade() -> None:
    """Restore the original seven-page registry cap for downgrade symmetry."""
    op.execute("UPDATE catalog_sources SET page_limit = 7 WHERE source_key = 'midpen-events'")
