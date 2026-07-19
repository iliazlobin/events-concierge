"""Disable the superseded SCCLD Milpitas and Saratoga-only sources.

Revision ID: 0073
Revises: 0072
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Disable duplicate SCCLD branch refreshes while preserving durable provenance (FR-10.3)."""
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false
        WHERE source_key IN ('sccld-milpitas-events', 'sccld-saratoga-events')
        """
    )


def downgrade() -> None:
    """Fail closed; explicit owner review is required to reactivate legacy seeds.

    The prior operator-controlled ``enabled`` values were not recorded when this migration
    disabled duplicate sources.  Re-enabling here could therefore override an intentional
    safety disable.  Keep durable provenance intact and require explicit owner/operator
    reactivation after review (FR-10.3/NFR-8).
    """
    # Intentionally no SQL: a downgrade cannot safely reconstruct the pre-upgrade operator state.
