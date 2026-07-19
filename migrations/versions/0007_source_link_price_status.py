"""Retain per-source price observations for conservative catalog aggregation.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store each source's current price and fail closed on mixed observations (FR-3.7/FR-5.10)."""
    op.execute(
        """
        ALTER TABLE event_source_links
        ADD COLUMN price_status text NOT NULL DEFAULT 'unknown'
        """
    )
    # A single retained link can inherit an already explicit canonical price.  Any multi-link
    # record may conceal disagreement, so its provenance remains unknown until a fresh refresh.
    op.execute(
        """
        UPDATE event_source_links AS link
        SET price_status = canonical.price_status
        FROM canonical_events AS canonical
        WHERE link.canonical_event_id = canonical.canonical_event_id
          AND canonical.price_status IN ('free', 'paid')
          AND (
              SELECT count(*)
              FROM event_source_links AS sibling
              WHERE sibling.canonical_event_id = link.canonical_event_id
          ) = 1
        """
    )
    op.execute(
        """
        UPDATE canonical_events AS canonical
        SET price_status = (
            SELECT CASE
                WHEN count(*) > 0 AND bool_and(link.price_status = 'free') THEN 'free'
                WHEN count(*) > 0 AND bool_and(link.price_status = 'paid') THEN 'paid'
                ELSE 'unknown'
            END
            FROM event_source_links AS link
            WHERE link.canonical_event_id = canonical.canonical_event_id
        )
        """
    )
    op.execute(
        """
        ALTER TABLE event_source_links
        ADD CONSTRAINT ck_event_source_links_price_status
        CHECK (price_status IN ('free', 'paid', 'unknown'))
        """
    )


def downgrade() -> None:
    """Drop per-link price provenance while retaining the 0006 canonical price column."""
    op.execute("ALTER TABLE event_source_links DROP CONSTRAINT ck_event_source_links_price_status")
    op.execute("ALTER TABLE event_source_links DROP COLUMN price_status")
