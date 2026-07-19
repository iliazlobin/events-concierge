"""Durable leases and notification ledger for the ADR-009 outbox relay.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make each relay attempt leaseable/retryable and persist send deduplication."""
    op.execute("ALTER TABLE outbox ADD COLUMN attempt_count integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE outbox ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE outbox ADD COLUMN lease_token text")
    op.execute("ALTER TABLE outbox ADD COLUMN lease_expires_at timestamptz")
    op.execute("ALTER TABLE outbox ADD COLUMN last_error text")
    op.execute("ALTER TABLE outbox ADD COLUMN failed_at timestamptz")
    op.execute(
        """CREATE INDEX ix_outbox_relay_ready ON outbox (next_attempt_at, id)
           WHERE delivered_at IS NULL AND failed_at IS NULL"""
    )
    op.execute(
        """CREATE TABLE notification_ledger (
               dedup_key         text PRIMARY KEY,
               outbox_id         bigint NOT NULL,
               tenant_id         uuid NOT NULL,
               state             text NOT NULL CHECK (state IN ('pending', 'sending', 'delivered')),
               lease_token       text,
               lease_expires_at  timestamptz,
               delivered_at      timestamptz,
               created_at        timestamptz NOT NULL DEFAULT now(),
               updated_at        timestamptz NOT NULL DEFAULT now()
           )"""
    )
    op.execute("CREATE INDEX ix_notification_ledger_tenant ON notification_ledger (tenant_id)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON notification_ledger TO ec_app")


def downgrade() -> None:
    """Remove relay bookkeeping while preserving the original transactional outbox rows."""
    op.execute("DROP TABLE IF EXISTS notification_ledger")
    op.execute("DROP INDEX IF EXISTS ix_outbox_relay_ready")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS failed_at")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS last_error")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS lease_expires_at")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS lease_token")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS next_attempt_at")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS attempt_count")
