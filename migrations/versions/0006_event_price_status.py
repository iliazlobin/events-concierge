"""Preserve free/paid/unknown pricing truth in the tenant-neutral catalog.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace lossy ``is_free`` with explicit price truth (FR-3.7/FR-4.6).

    Historical ``is_free=true`` rows may have originated from an absent price, because the old
    ingest path collapsed unknown into free. They are therefore conservatively migrated to
    ``unknown``; a subsequent source refresh can promote verified-free records.
    """
    op.execute("ALTER TABLE canonical_events ADD COLUMN price_status text")
    op.execute(
        """
        UPDATE canonical_events
        SET price_status = CASE
            WHEN is_free IS FALSE THEN 'paid'
            ELSE 'unknown'
        END
        """
    )
    op.execute("ALTER TABLE canonical_events ALTER COLUMN price_status SET NOT NULL")
    op.execute(
        """
        ALTER TABLE canonical_events
        ADD CONSTRAINT ck_canonical_events_price_status
        CHECK (price_status IN ('free', 'paid', 'unknown'))
        """
    )
    op.execute("ALTER TABLE canonical_events DROP COLUMN is_free")
    op.execute("CREATE INDEX ix_canonical_price_date ON canonical_events (price_status, start_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_canonical_price_date")
    op.execute("ALTER TABLE canonical_events ADD COLUMN is_free boolean NOT NULL DEFAULT true")
    op.execute("UPDATE canonical_events SET is_free = (price_status = 'free')")
    op.execute("ALTER TABLE canonical_events DROP CONSTRAINT ck_canonical_events_price_status")
    op.execute("ALTER TABLE canonical_events DROP COLUMN price_status")
