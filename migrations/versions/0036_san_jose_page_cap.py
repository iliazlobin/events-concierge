"""Increase San José Public Library's source-bound RSS page cap.

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Use the measured all-time physical-branch upper bound to keep SJPL's 90-day feed complete (FR-3.1)."""
    # The anonymous public UI reports 3,997 events for the same closed 25-branch physical allowlist across all
    # dates. BiblioCommons RSS has 25 items per page, so 160 pages cover that broader source set; the narrower
    # 90-day RSS request still stops at its first short page and fails rather than retaining a partial window.
    op.execute(
        "UPDATE catalog_sources SET page_limit = 160 "
        "WHERE source_key = 'san-jose-public-library-events'"
    )


def downgrade() -> None:
    """Restore the initial conservative fail-closed probe cap."""
    op.execute(
        "UPDATE catalog_sources SET page_limit = 60 "
        "WHERE source_key = 'san-jose-public-library-events'"
    )
