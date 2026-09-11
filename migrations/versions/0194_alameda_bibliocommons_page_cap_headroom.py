"""Restore Alameda physical-branch feed headroom without overwriting operator controls.

Revision ID: 0194
Revises: 0193

The live feed now exceeds the original 40 x 25 item cap and fails the complete-feed contract.
Raise only the unchanged original cap to 80 pages. The existing 5-second pacing plus bounded
persistence reserve requires a 960-second lease, within the 3600-second ceiling. Fetching still
stops at the feed's end. Review, activation, origins, filters, cadence, and pacing stay untouched;
the existing registry trigger advances source_revision when the cap changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0194"
down_revision: str | None = "0193"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE catalog_sources
        SET page_limit = 80,
            updated_at = CURRENT_TIMESTAMP
        WHERE source_key = 'alameda-county-library-all-physical-branches-events'
          AND page_limit = 40
        """
    )


def downgrade() -> None:
    # An existing operator-selected 80-page cap is indistinguishable from one changed above.
    # Preserve runtime controls on rollback; a deliberate cap reduction needs an explicit edit.
    pass
