"""Classify no-egress catalog Pacer waits as paused coordination work.

Revision ID: 0129
Revises: 0128
Create Date: 2026-07-30

A denied shared-Pacer admission performs no provider request. Older generic refresh workers
released that claim through the failure capability, which made reliability charts count ordinary
throttle coordination as a crawler failure. Current workers use the existing lease-fenced pause
capability; this data correction gives retained operational history the same meaning.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0129"
down_revision: str | None = "0128"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Reclassify only the worker's closed Pacer forms and restore paused-row invariants."""
    op.execute(
        """
        UPDATE public.catalog_refresh_runs AS refresh
        SET status = 'paused',
            lease_token = NULL,
            lease_expires_at = NULL,
            completed_at = NULL
        WHERE refresh.status IN ('failed', 'paused')
          AND pg_catalog.char_length(refresh.error) BETWEEN 13 AND 2000
          AND refresh.error ~ '^Pacer (wait|degrade|saturated): [^[:cntrl:]]+$'
        """
    )


def downgrade() -> None:
    """Retain the corrected historical classification.

    The row does not record whether a paused Pacer wait predated this migration or was written by a
    newer worker. Reversing every matching paused row would corrupt legitimate paged-refresh
    history, so downgrade removes no schema and intentionally leaves the safe classification.
    """
