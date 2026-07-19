"""Disable the superseded Millbrae-only SMCL source.

Revision ID: 0071
Revises: 0070
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Disable the superseded Millbrae-only refresh seed (FR-3.1/FR-10.3/NFR-8)."""
    # The reviewed all-physical SMCL source includes 1M. Retain this row and its durable provenance,
    # but stop a second cadence/lease from duplicating that location's bounded catalog refreshes.
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false
        WHERE source_key = 'smcl-millbrae-events'
        """
    )


def downgrade() -> None:
    """Fail closed; explicit owner review is required to reactivate the legacy seed.

    The prior operator-controlled ``enabled`` value was not recorded when this migration
    disabled the duplicate source.  Re-enabling here could therefore override an intentional
    safety disable.  Keep the durable provenance intact and require explicit owner/operator
    reactivation after review (FR-10.3/NFR-8).
    """
    # Intentionally no SQL: a downgrade cannot safely reconstruct the pre-upgrade operator state.
