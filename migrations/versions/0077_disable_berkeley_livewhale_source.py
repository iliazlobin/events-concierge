"""Disable the held UC Berkeley LiveWhale source.

Revision ID: 0077
Revises: 0076
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stop Berkeley refresh egress while rights and source-truth review remain open (FR-10.3)."""
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false
        WHERE source_key = 'berkeley-events'
        """
    )


def downgrade() -> None:
    """Fail closed; only a later owner-reviewed migration may reactivate Berkeley.

    The prior operator-controlled ``enabled`` value is not recorded here.  Restoring it during a
    downgrade could override the intentional legal/source-quality hold and restart network egress,
    so this downgrade deliberately emits no SQL (FR-10.3/NFR-8).
    """
    # Intentionally no SQL.
