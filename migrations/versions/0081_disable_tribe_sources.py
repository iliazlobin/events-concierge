"""Disable the held Oakland Museum and Gardens of Golden Gate Park Tribe sources.

Revision ID: 0081
Revises: 0080
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stop Tribe refresh egress while authorization and source-truth review remain open (FR-10.3)."""
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false
        WHERE source_key IN ('omca-events', 'gardens-golden-gate-park-events')
        """
    )


def downgrade() -> None:
    """Fail closed; only a later owner-reviewed migration may reactivate these sources.

    The prior operator-controlled ``enabled`` values are not recorded here. Restoring them during
    a downgrade could override an intentional authorization/safety hold and restart network
    egress, so this downgrade deliberately emits no SQL (FR-10.3/NFR-8).
    """
    # Intentionally no SQL.
