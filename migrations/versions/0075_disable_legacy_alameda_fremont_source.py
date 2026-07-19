"""Disable the superseded Alameda County Library Fremont-only source.

Revision ID: 0075
Revises: 0074
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Disable the duplicate Fremont refresh while preserving durable provenance (FR-10.3/NFR-8)."""
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false
        WHERE source_key = 'alameda-county-library-fremont-events'
        """
    )


def downgrade() -> None:
    """Fail closed; explicit owner review is required to reactivate the legacy Fremont seed.

    The prior operator-controlled ``enabled`` value was not recorded when this migration
    disabled the duplicate source. Re-enabling here could override an intentional safety
    disable, so durable provenance remains intact until explicit owner/operator reactivation
    after review (FR-10.3/NFR-8).
    """
    # Intentionally no SQL: a downgrade cannot safely reconstruct the pre-upgrade operator state.
