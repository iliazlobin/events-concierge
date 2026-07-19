"""Persist Google Calendar sync cursors and watched-channel metadata under FORCE RLS.

Revision ID: 0047
Revises: 0046
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Store only RLS-scoped cursors and hashed webhook tokens for FR-9.4 incremental sync."""
    op.execute(
        """
        CREATE TABLE google_calendar_sync_state (
            tenant_id             uuid NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
            calendar_id           text NOT NULL,
            sync_token            text,
            channel_id            text,
            resource_id           text,
            channel_token_digest  text,
            channel_expires_at    timestamptz,
            created_at            timestamptz NOT NULL DEFAULT now(),
            updated_at            timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, calendar_id),
            UNIQUE (channel_id),
            CHECK (
                (channel_id IS NULL AND resource_id IS NULL
                 AND channel_token_digest IS NULL AND channel_expires_at IS NULL)
                OR
                (channel_id IS NOT NULL AND resource_id IS NOT NULL
                 AND channel_token_digest IS NOT NULL AND channel_expires_at IS NOT NULL)
            ),
            CHECK (
                channel_token_digest IS NULL
                OR channel_token_digest ~ '^[0-9a-f]{64}$'
            )
        )
        """
    )
    op.execute(
        """CREATE INDEX ix_google_calendar_sync_state_tenant_expiration
           ON google_calendar_sync_state (tenant_id, channel_expires_at)"""
    )
    op.execute("ALTER TABLE google_calendar_sync_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE google_calendar_sync_state FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""CREATE POLICY tenant_isolation ON google_calendar_sync_state
            USING ({_TENANT_POLICY}) WITH CHECK ({_TENANT_POLICY})"""
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON google_calendar_sync_state TO ec_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS google_calendar_sync_state")
