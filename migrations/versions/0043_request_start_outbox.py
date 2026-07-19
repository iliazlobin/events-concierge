"""Persist replay-safe EventRequest workflow starts outside the HTTP request lifecycle.

Revision ID: 0043
Revises: 0042
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ADR-003 start-outbox with lease/retry state and no user text payload."""
    op.execute(
        """
        CREATE TABLE request_start_outbox (
            request_id         uuid PRIMARY KEY REFERENCES event_requests (request_id) ON DELETE CASCADE,
            tenant_id          uuid NOT NULL,
            dedup_key          text NOT NULL UNIQUE,
            created_at         timestamptz NOT NULL DEFAULT now(),
            started_at         timestamptz,
            attempt_count      integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at    timestamptz NOT NULL DEFAULT now(),
            lease_token        text,
            lease_expires_at   timestamptz,
            last_error         text
        )
        """
    )
    op.execute(
        """CREATE INDEX ix_request_start_outbox_ready
               ON request_start_outbox (next_attempt_at, request_id)
               WHERE started_at IS NULL"""
    )
    op.execute(
        """CREATE INDEX ix_request_start_outbox_tenant
               ON request_start_outbox (tenant_id, request_id)"""
    )
    # This is an opaque cross-tenant relay queue like ``outbox``/``notification_ledger``. It has no
    # raw request text or constraints; the worker reopens ``event_requests`` with SET LOCAL tenant
    # context before starting Temporal. RLS here would prevent one durable starter from claiming all
    # tenants' pending rows and make an engine outage lossy (ADR-003, FR-6.8).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON request_start_outbox TO ec_app")


def downgrade() -> None:
    """Remove the replay queue; historical EventRequest rows remain intact."""
    op.execute("DROP TABLE IF EXISTS request_start_outbox")
