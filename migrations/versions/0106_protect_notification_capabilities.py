"""Remove plaintext notification capabilities and enforce protected outbox projections.

Revision ID: 0106
Revises: 0105
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0106"
down_revision: str | None = "0105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_outbox_no_plaintext_completion_url"


def upgrade() -> None:
    """Irreversibly scrub legacy bearers before rejecting future plaintext projections."""
    op.execute(
        """
        UPDATE public.outbox AS outbox_row
        SET payload = outbox_row.payload - 'completion_url',
            delivered_at = COALESCE(ledger.delivered_at, pg_catalog.clock_timestamp()),
            last_error = NULL,
            lease_token = NULL,
            lease_expires_at = NULL
        FROM public.notification_ledger AS ledger
        WHERE outbox_row.payload ? 'completion_url'
          AND outbox_row.delivered_at IS NULL
          AND outbox_row.failed_at IS NULL
          AND ledger.outbox_id = outbox_row.id
          AND ledger.tenant_id = outbox_row.tenant_id
          AND ledger.state = 'delivered'
        """
    )
    op.execute(
        """
        UPDATE public.outbox
        SET payload = payload - 'completion_url',
            failed_at = pg_catalog.clock_timestamp(),
            last_error = '0106 quarantined an unreconstructable plaintext completion capability',
            lease_token = NULL,
            lease_expires_at = NULL
        WHERE payload ? 'completion_url'
          AND delivered_at IS NULL
          AND failed_at IS NULL
        """
    )
    op.execute(
        """
        DELETE FROM public.notification_ledger AS ledger
        USING public.outbox AS outbox_row
        WHERE ledger.outbox_id = outbox_row.id
          AND outbox_row.failed_at IS NOT NULL
          AND outbox_row.last_error =
              '0106 quarantined an unreconstructable plaintext completion capability'
          AND ledger.state <> 'delivered'
        """
    )
    op.execute(
        """
        UPDATE public.outbox
        SET payload = payload - 'completion_url',
            last_error = NULL
        WHERE payload ? 'completion_url'
        """
    )
    op.execute(
        """
        UPDATE public.outbox
        SET payload = payload - 'protected_completion_url'
        WHERE (delivered_at IS NOT NULL OR failed_at IS NOT NULL)
          AND payload ? 'protected_completion_url'
        """
    )
    op.execute(
        f"""
        ALTER TABLE public.outbox
        ADD CONSTRAINT {_CONSTRAINT}
        CHECK (NOT (payload ? 'completion_url'))
        NOT VALID
        """
    )
    op.execute(f"ALTER TABLE public.outbox VALIDATE CONSTRAINT {_CONSTRAINT}")


def downgrade() -> None:
    """Remove enforcement without ever reconstructing or restoring scrubbed plaintext."""
    op.execute(f"ALTER TABLE public.outbox DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
