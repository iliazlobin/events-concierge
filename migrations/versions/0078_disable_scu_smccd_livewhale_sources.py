"""Disable the held SCU and SMCCD LiveWhale sources.

Revision ID: 0078
Revises: 0077
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stop SCU and SMCCD refresh egress while source-truth and rights review remain open (FR-10.3)."""
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false
        WHERE source_key IN ('scu-events', 'smccd-events')
        """
    )


def downgrade() -> None:
    """Fail closed; only a later owner-reviewed migration may reactivate either source.

    The prior operator-controlled ``enabled`` values are not recorded here. Restoring them during
    a downgrade could override an intentional safety/legal hold and restart network egress, so this
    downgrade deliberately emits no SQL (FR-10.3/NFR-8).
    """
    # Intentionally no SQL.
