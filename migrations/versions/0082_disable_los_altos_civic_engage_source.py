"""Disable the held City of Los Altos CivicEngage source.

Revision ID: 0082
Revises: 0081
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stop Los Altos refresh egress while its explicit reuse prohibition remains unresolved (FR-10.3)."""
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false
        WHERE source_key = 'los-altos-events'
        """
    )


def downgrade() -> None:
    """Fail closed; only a later owner-reviewed migration may reactivate Los Altos.

    The prior operator-controlled ``enabled`` value is not recorded here. Restoring it during a
    downgrade could override an intentional authorization/safety hold and restart network egress,
    so this downgrade deliberately emits no SQL (FR-10.3/NFR-8).
    """
    # Intentionally no SQL.
